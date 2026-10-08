"""A decision day's run bundle (#171): written once, resumable when a write fails part way, read
back only whole, and replayed from its own rows to the day's ledger."""

import json
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl
import predict_fixtures as pf
import pytest
from b2_fixtures import feature_tables
from b3_fixtures import league

from nhl_edge.backtest.walk_forward import fold_start
from nhl_edge.game import uncertainty
from nhl_edge.ingest.odds import ODDS_FRAME_SCHEMA
from nhl_edge.lake.schemas import PlayerLeagueSeasons, Slate, dtypes
from nhl_edge.live import blend_fit
from nhl_edge.live import bundle as lb
from nhl_edge.live import predict as lp

LEAGUE = league()
TEST = 20182019
START = fold_start(LEAGUE.games.select("season", "start_utc"), TEST)
B2_TABLES = replace(feature_tables(LEAGUE.games, 4), games=LEAGUE.games)
# B3 shares B2's schedule terms, goalie starts and boxscores, as nhl predict reads them.
B3_TABLES = replace(
    LEAGUE,
    schedule_terms=B2_TABLES.schedule_terms,
    goalie_starts=B2_TABLES.goalie_starts,
    actual_lineups=B2_TABLES.actual_lineups,
)
U_TABLES = uncertainty.Tables(
    LEAGUE.games,
    B2_TABLES.goalie_starts,
    LEAGUE.lineups,
    LEAGUE.lineup_replacements,
    B2_TABLES.actual_lineups,
    pl.DataFrame(schema=dtypes(PlayerLeagueSeasons)),
)
RECORD = pl.DataFrame({"table": ["slate"]})
NO_QUOTES = pl.DataFrame(schema=ODDS_FRAME_SCHEMA)
FIT = blend_fit.REPORTS / "blend-live-20261007-89ec631.json"


def fixture_slate() -> pl.DataFrame:
    """The B3 fixture's test season, its tenth game date, as a day's slate."""
    games = LEAGUE.games.filter(pl.col("season") == TEST)
    day = games["game_date"].unique().sort()[9]
    return Slate.validate(
        games.filter(pl.col("game_date") == day)
        .with_columns(game_state=pl.lit("FUT"))
        .select(list(dtypes(Slate)))
    )


def model_bundle(store: lb.Store) -> lp.Models:
    """The fixture day's models on the whole lake, and its bundle in store."""
    games = fixture_slate()
    cutoff = games["start_utc"].min()
    assert isinstance(cutoff, datetime)
    moments = games.select("game_id", prediction_utc=pl.lit(cutoff))
    fitted = lp.models(B2_TABLES, B3_TABLES, U_TABLES, games, moments, START, TEST)
    assert fitted.b2_model is not None and fitted.b3_model is not None
    rows = lb.inputs(games, RECORD, NO_QUOTES, (B2_TABLES, B3_TABLES, U_TABLES))
    identity = {"day": str(games["game_date"][0]), "input_cutoff": cutoff.isoformat()}
    raw = {"odds/2018-10-19/x_midday_eu": b"{}", "odds/2018-10-19/x_midday_eu.meta": b"{}"}
    fits = {"b2": fitted.b2_model, "b3": fitted.b3_model}
    lb.write_once(store, games["game_date"][0], rows, fits, identity, raw)
    return fitted


def test_the_models_replayed_from_the_bundle_alone_match_those_on_the_whole_lake(
    tmp_path: Path,
) -> None:
    store = lb.LocalStore(tmp_path)
    fitted = model_bundle(store)
    saved = lb.read(store, fixture_slate()["game_date"][0])
    # The bundle holds only the slate's rows, far from the whole lake the models read.
    assert saved.inputs["lineups"]["game_id"].n_unique() == fixture_slate().height
    assert saved.inputs["player_ratings"].height < LEAGUE.player_ratings.height / 100
    replayed = lb.models(saved)
    assert replayed is not None
    assert replayed.b2.height == replayed.b3.height == fixture_slate().height
    for frame, again in ((fitted.b2, replayed.b2), (fitted.b3, replayed.b3)):
        made, column = frame.sort("game_id"), frame.columns[1]
        assert again.sort("game_id")[column].to_list() == pytest.approx(
            made[column].to_list(), abs=1e-12
        )
    for part in uncertainty.PARTS:
        assert replayed.parts.sort("game_id")[part].to_list() == pytest.approx(
            fitted.parts.sort("game_id")[part].to_list(), abs=1e-12
        )
    assert (replayed.b2_model, replayed.b3_model) == (fitted.b2_model, fitted.b3_model)


def day_identity(inputs: lp.Day) -> dict[str, Any]:
    published = inputs.published_utc or inputs.decision_utc
    return {
        "day": inputs.day.isoformat(),
        "decision_utc": inputs.decision_utc.isoformat(),
        "published_utc": published.isoformat(),
        "input_cutoff": lp.input_cutoff(inputs.quotes, inputs.decision_utc).isoformat(),
        "bankroll": inputs.bankroll,
        "blend_version": inputs.live.version,
        "feature_build": inputs.versions["feature_build"],
        "code_version": inputs.versions["code_version"],
    }


def test_the_replay_decides_the_day_again_from_the_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The models replayed are tested above; here, that the decision made again on the saved
    # quotes, slate and bankroll, at the saved times, is the day's ledger, every column.
    published = pf.DECISION + timedelta(minutes=4)
    inputs = pf.day(published_utc=published, bankroll=98.5)
    day = pf.DAY
    ledger = lp.ledger(lp.decide(inputs), inputs)
    store = lb.LocalStore(tmp_path)
    rows = lb.inputs(inputs.slate, RECORD, inputs.quotes, None)
    lb.write_once(store, day, rows, {}, day_identity(inputs), {})
    monkeypatch.setattr(lb, "models", lambda _: inputs.fitted)
    replayed = lb.replay(lb.read(store, day), inputs.live)
    assert lb.differences(replayed, ledger) == []
    assert replayed["bet"].fill_null(False).any()
    # A ledger that differs anywhere is caught: a stake, a status, or another set of games.
    moved = ledger.with_columns(stake=pl.col("stake") * 1.01)
    assert lb.differences(replayed, moved) == [
        f"stake differs for {ledger.filter(pl.col('stake') > 0)['game_id'].to_list()}"
    ]
    # A NaN where the replay has a number differs too.
    bet = ledger.filter(pl.col("bet").fill_null(False))["game_id"].to_list()
    nan = ledger.with_columns(
        stake=pl.when(pl.col("game_id").is_in(bet)).then(float("nan")).otherwise(pl.col("stake"))
    )
    assert lb.differences(replayed, nan) == [f"stake differs for {bet}"]
    relabelled = ledger.with_columns(status=pl.lit(lp.STALE))
    assert any(p.startswith("status differs") for p in lb.differences(replayed, relabelled))
    assert lb.differences(replayed, ledger.head(2))[0].startswith("games differ")
    # Another live fit is refused.
    other = replace(inputs.live, version="blend-live-20261007-other")
    with pytest.raises(ValueError, match="decided with"):
        lb.replay(lb.read(store, day), other)


def test_a_bundle_is_written_once_and_read_only_whole(tmp_path: Path) -> None:
    store = lb.LocalStore(tmp_path)
    model_bundle(store)
    day = fixture_slate()["game_date"][0]
    where = tmp_path / lb.prefix(day)
    manifest = json.loads((where / lb.MANIFEST).read_text())
    assert set(manifest["raw"]) == {
        "odds/2018-10-19/x_midday_eu",
        "odds/2018-10-19/x_midday_eu.meta",
    }
    # A file whose bytes changed is refused, and a write over it with other bytes too.
    lineups = where / "inputs" / "lineups.parquet"
    good = lineups.read_bytes()
    lineups.write_bytes(good + b"x")
    with pytest.raises(ValueError, match="don't match the manifest"):
        lb.read(store, day)
    with pytest.raises(ValueError, match="with other bytes"):
        model_bundle(store)
    lineups.write_bytes(good)
    lb.read(store, day)
    # Written again with the same bytes, it is the same bundle.
    model_bundle(store)
    # Without its manifest, the bundle never finished.
    (where / lb.MANIFEST).unlink()
    with pytest.raises(ValueError, match="no manifest"):
        lb.read(store, day)


def test_a_write_that_failed_part_way_is_finished_by_writing_again() -> None:
    from fakes import ConditionalBucket

    bucket = ConditionalBucket()
    order: list[str] = []
    put = bucket.put_object
    fail = {"manifest": True}

    def flaky(**kwargs: Any) -> None:
        if kwargs["Key"].endswith(lb.MANIFEST) and fail["manifest"]:
            fail["manifest"] = False
            raise ConnectionError("lost")
        order.append(kwargs["Key"])
        put(**kwargs)

    bucket.put_object = flaky  # type: ignore[method-assign]
    store = lb.R2Store(bucket, "b")
    day = fixture_slate()["game_date"][0]
    with pytest.raises(ConnectionError):
        model_bundle(store)
    with pytest.raises(ValueError, match="no manifest"):
        lb.read(store, day)
    # Every object is written with IfNoneMatch: the inputs already there count as written.
    model_bundle(store)
    assert order[-1].endswith(lb.MANIFEST)
    assert all(key.startswith(lb.prefix(day)) for key in order)
    lb.read(store, day)


def test_nhl_live_replay_decides_the_day_again_from_the_bundle_and_the_fit(
    tmp_path: Path,
) -> None:
    from typer.testing import CliRunner

    from nhl_edge.cli import app

    # A day whose models were never read (no build): every game a missing input, decided with
    # the season's committed live fit.
    live = blend_fit.load(json.loads(FIT.read_text()))
    versions = {
        "blend_version": live.version,
        "feature_build": "live-features-20261007-abc1234",
        "code_version": "predict-20261007-abc1234",
        "b2_train_cutoff": None,
        "b3_train_cutoff": None,
    }
    published = pf.DECISION + timedelta(minutes=3)
    inputs = pf.day(fitted=None, live=live, versions=versions, published_utc=published)
    ledger = lp.ledger(lp.decide(inputs), inputs)
    identity = day_identity(inputs) | {
        "live_fit": FIT.name,
        "live_fit_sha256": lb.sha256(FIT.read_bytes()),
    }
    rows = lb.inputs(inputs.slate, RECORD, inputs.quotes, None)
    lb.write_once(lb.LocalStore(tmp_path), pf.DAY, rows, {}, identity, {})
    ledger.write_parquet(tmp_path / f"{pf.DAY.isoformat()}.parquet")
    command = ["live", "replay", "--date", pf.DAY.isoformat(), "--from", str(tmp_path)]
    result = CliRunner().invoke(app, command)
    assert result.exit_code == 0, result.output
    assert "the ledger is reproduced" in result.output
    # A ledger the bundle doesn't reproduce fails.
    ledger.with_columns(status=pl.lit(lp.STALE)).write_parquet(
        tmp_path / f"{pf.DAY.isoformat()}.parquet"
    )
    result = CliRunner().invoke(app, command)
    assert result.exit_code == 1
    assert "status differs" in result.output
    # So does a fit other than the one the day names.
    other = tmp_path / "other.json"
    other.write_bytes(FIT.read_bytes() + b"\n")
    result = CliRunner().invoke(app, [*command, "--fit", str(other)])
    assert result.exit_code == 1
    assert "is not the live fit" in result.output


def test_a_dry_run_into_a_directory_holding_the_days_bundle_is_refused(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from nhl_edge.cli import app

    (tmp_path / lb.prefix(pf.DAY)).mkdir(parents=True)
    at = datetime(2026, 10, 7, 16, 47, tzinfo=UTC).strftime("%Y-%m-%dT%H:%M:%S%z")
    command = ["predict", "--date", "2026-10-07", "--dry-run", "--at", at, "--out", str(tmp_path)]
    result = CliRunner().invoke(app, command)
    assert result.exit_code == 2
    # Rich styles and boxes the message on CI runners, wrapping the long path.
    unstyled = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    text = " ".join(re.sub(r"[│╭╮╰╯─]", " ", unstyled).split())
    assert "pass a fresh --out" in text, result.output


def stopped_after_the_ledger(store: lb.Store) -> tuple[lp.Day, pl.DataFrame]:
    """A day with bets whose run wrote its rows and fits, then its ledger, and stopped before the
    manifest (#188)."""
    inputs = pf.day(published_utc=pf.DECISION + timedelta(minutes=4), bankroll=98.5)
    ledger = lp.ledger(lp.decide(inputs), inputs)
    lb.write_files(store, pf.DAY, lb.inputs(inputs.slate, RECORD, inputs.quotes, None), {})
    return inputs, ledger


def test_a_run_stopped_after_its_ledger_is_finished_and_replays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The done-when: killed between the ledger and the manifest, the bundle is unreadable; finished
    # from what is stored, it replays to the day's ledger.
    store = lb.LocalStore(tmp_path)
    inputs, ledger = stopped_after_the_ledger(store)
    with pytest.raises(ValueError, match="no manifest"):
        lb.read(store, pf.DAY)
    assert lb.unfinished(store, [pf.DAY]) == [pf.DAY]
    sha = lb.sha256(FIT.read_bytes())
    monkeypatch.setattr(lb, "models", lambda _: inputs.fitted)
    # Files that don't reproduce the ledger never seal (Codex on #204): a stake moved.
    moved = ledger.with_columns(stake=pl.col("stake") * 1.01)
    with pytest.raises(ValueError, match="don't reproduce the ledger"):
        lb.finish(store, pf.DAY, moved, "x", {}, inputs.live, FIT.name, sha)
    assert lb.unfinished(store, [pf.DAY]) == [pf.DAY]
    at = "R2 ledger/live/2026-10-07.parquet"
    lb.finish(store, pf.DAY, ledger, at, {}, inputs.live, FIT.name, sha)
    saved = lb.read(store, pf.DAY)
    assert lb.unfinished(store, [pf.DAY]) == []
    # The identity the run would have written, from the ledger and the saved quotes.
    for name, value in day_identity(inputs).items():
        assert saved.manifest[name] == value, name
    assert saved.manifest["live_fit_sha256"] == sha and saved.manifest["finished_later"]
    replayed = lb.replay(saved, inputs.live)
    assert lb.differences(replayed, ledger) == []
    assert replayed["bet"].fill_null(False).any()
    with pytest.raises(ValueError, match="complete already"):
        lb.finish(store, pf.DAY, ledger, "x", {}, inputs.live, FIT.name, sha)


def test_a_bundle_missing_an_input_is_never_finished(tmp_path: Path) -> None:
    sha = lb.sha256(FIT.read_bytes())
    inputs, ledger = stopped_after_the_ledger(lb.LocalStore(tmp_path / "none"))
    live = inputs.live
    # Nothing of the run's files was written.
    with pytest.raises(ValueError, match="never written"):
        lb.finish(lb.LocalStore(tmp_path / "empty"), pf.DAY, ledger, "x", {}, live, FIT.name, sha)
    # The models were read, so their rows are needed too, and they aren't there.
    store = lb.LocalStore(tmp_path / "fits")
    stopped_after_the_ledger(store)
    store.root.joinpath(lb.prefix(pf.DAY), lb.FITS).unlink()
    store.put(f"{lb.prefix(pf.DAY)}/{lb.FITS}", b'{"b2": {}, "b3": {}}')
    with pytest.raises(ValueError, match="missing team_strength"):
        lb.finish(store, pf.DAY, ledger, "x", {}, live, FIT.name, sha)


def test_the_inputs_a_decision_saves_are_the_ones_finish_requires() -> None:
    games = fixture_slate()
    rows = lb.inputs(games, RECORD, NO_QUOTES, (B2_TABLES, B3_TABLES, U_TABLES))
    assert set(rows) == set(lb.BASE_INPUTS + lb.MODEL_INPUTS)
    assert set(lb.inputs(games, RECORD, NO_QUOTES, None)) == set(lb.BASE_INPUTS)


def test_nhl_live_bundle_finishes_a_day_and_checks_every_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fakes import MemoryBucket
    from typer.testing import CliRunner

    from nhl_edge.cli import app
    from nhl_edge.lake.r2 import R2Config

    # A dry run's directory, stopped after its ledger: --finish completes it from there, and the
    # replay reproduces the day.
    live = blend_fit.load(json.loads(FIT.read_text()))
    versions = {
        "blend_version": live.version,
        "feature_build": "live-features-20261007-abc1234",
        "code_version": "predict-20261007-abc1234",
        "b2_train_cutoff": None,
        "b3_train_cutoff": None,
    }
    inputs = pf.day(
        fitted=None, live=live, versions=versions, published_utc=pf.DECISION + timedelta(minutes=3)
    )
    ledger = lp.ledger(lp.decide(inputs), inputs)
    out = tmp_path / "dry"
    lb.write_files(
        lb.LocalStore(out), pf.DAY, lb.inputs(inputs.slate, RECORD, inputs.quotes, None), {}
    )
    ledger.write_parquet(out / f"{pf.DAY.isoformat()}.parquet")
    runner = CliRunner()
    replay = ["live", "replay", "--date", pf.DAY.isoformat(), "--from", str(out)]
    assert runner.invoke(app, replay).exit_code == 1
    finish = ["live", "bundle", "--date", pf.DAY.isoformat(), "--finish", "--from", str(out)]
    result = runner.invoke(app, finish)
    assert result.exit_code == 0, result.output
    assert "finished, its manifest rebuilt" in result.output
    result = runner.invoke(app, replay)
    assert result.exit_code == 0, result.output
    assert "the ledger is reproduced" in result.output
    # --check lists, from R2, each ledger day whose bundle has no manifest.
    for name in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"):
        monkeypatch.setenv(name, "test")
    monkeypatch.setattr("nhl_edge.settings.load_env", lambda: None)
    bucket = MemoryBucket()
    monkeypatch.setattr(R2Config, "client", lambda self: bucket)
    r2 = lb.R2Store(bucket, "test")
    for day in (pf.DAY, pf.DAY + timedelta(days=1)):
        r2.put(lp.ledger_key(day), b"ledger")
        lb.write_files(r2, day, lb.inputs(inputs.slate, RECORD, inputs.quotes, None), {})
    lb.write_manifest(r2, pf.DAY, {"day": pf.DAY.isoformat()})
    result = runner.invoke(app, ["live", "bundle", "--check", "--r2"])
    assert result.exit_code == 1
    later = pf.DAY + timedelta(days=1)
    assert f"{later}: the run bundle has no manifest" in result.output
    assert "1 of 2 decision days' bundles complete" in result.output


def test_a_finished_bundle_records_only_the_odds_stored_by_its_publication(tmp_path: Path) -> None:
    # Codex on #204: the day's evening slots come after the midday decision on the same UTC date,
    # so a bundle finished later must leave them out.
    from nhl_edge.lake.raw import RawStore

    store = RawStore(tmp_path)
    day = pf.DAY
    for stamp, slot in (("12:47", "midday"), ("22:47", "pre7")):
        fetched = datetime.fromisoformat(f"{day}T{stamp}:00+00:00")
        meta = {"fetched_utc": fetched.isoformat(), "status": 200}
        store.put("odds", f"{day}/{fetched:%Y%m%dT%H%M%SZ}_{slot}_eu", b"[]", meta)
    every = lp.raw_responses(store, day)
    published = datetime.fromisoformat(f"{day}T16:51:00+00:00")
    kept = lp.raw_responses(store, day, by=published)
    assert len(every) == 4 and len(kept) == 2
    assert all("midday" in key for key in kept)
