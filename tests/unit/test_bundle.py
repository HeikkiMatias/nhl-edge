"""A decision day's run bundle (#171): written once, resumable when a write fails part way, read
back only whole, and replayed from its own rows to the day's ledger."""

import json
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
    assert "pass a fresh --out" in result.output
