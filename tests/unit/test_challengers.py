"""Challengers (#255, ADR 0038): scored on each complete run bundle of a day after their
registration, written once, and checked by recomputing a stored day from its bundle."""

import json
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
import predict_fixtures as pf
import pytest

from nhl_edge.betting.selection import POLICY_V2, POLICY_VERSION
from nhl_edge.ingest.games import result_public_utc
from nhl_edge.lake.schemas import ChallengerPredictions
from nhl_edge.live import blend_fit
from nhl_edge.live import bundle as lb
from nhl_edge.live import challengers as lc
from nhl_edge.live import predict as lp
from nhl_edge.market.recalibration import logit, sigmoid

FIT = blend_fit.REPORTS / "blend-live-20261007-89ec631.json"
RECORD = pl.DataFrame({"table": ["slate"]})
NOW = datetime(2026, 10, 8, 9, tzinfo=UTC)
# market_only and conservative_blend as if registered the day before the fixture's decision day.
MARKET_ONLY = replace(lc.REGISTRY[0], registered_on=pf.DAY - timedelta(days=1))
CONSERVATIVE = replace(lc.REGISTRY[1], registered_on=pf.DAY - timedelta(days=1))
# The fixture day's ET week starts on Monday 2026-10-05.
WEEK = date(2026, 10, 5)


class NoLake:
    """A lake no test challenger may read."""

    def read(self, table: str, seasons: Any = None) -> pl.DataFrame:
        raise AssertionError(f"read {table}")


class Lake:
    """A lake of one table per name."""

    def __init__(self, **tables: pl.DataFrame) -> None:
        self.tables = tables

    def read(self, table: str, seasons: Any = None) -> pl.DataFrame:
        return self.tables[table]


def history(
    n: int, first: date = WEEK - timedelta(days=7), policy: str = POLICY_V2, seed: int = 0
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """n decided games over the week from first, and their results: the ledger's rows a fit
    reads, and the games' full-game scores, drawn from a = 0.02, b_m = 1.05 and b_d = 0.4."""
    rng = np.random.default_rng(seed)
    days = [first + timedelta(days=i % 7) for i in range(n)]
    starts = [datetime.combine(day, time(23), UTC) for day in days]
    p_b0 = rng.uniform(0.3, 0.7, n)
    p_b3 = sigmoid(logit(p_b0) + rng.normal(0, 0.25, n))
    truth = sigmoid(0.02 + 1.05 * logit(p_b0) + 0.4 * (logit(p_b3) - logit(p_b0)))
    home_win = rng.random(n) < truth
    ids = 2026010001 + np.arange(n) + (seed * 10_000)
    ledger = pl.DataFrame(
        {
            "game_date": days,
            "season": [20262027] * n,
            "game_id": ids,
            "start_utc": starts,
            "published_utc": [datetime.combine(day, time(16, 50), UTC) for day in days],
            "status": [lp.PREDICTED] * n,
            "policy_version": [policy] * n,
            "p_b0": p_b0,
            "p_b3": p_b3,
        },
        schema_overrides={"season": pl.Int32},
    )
    games = pl.DataFrame(
        {
            "game_id": ids,
            "season": [20262027] * n,
            "game_date": days,
            "start_utc": starts,
            "home_score": np.where(home_win, 3, 2),
            "away_score": np.where(home_win, 2, 3),
            "observed_utc": [result_public_utc(day).replace(tzinfo=UTC) for day in days],
        },
        schema_overrides={"season": pl.Int32, "home_score": pl.Int16, "away_score": pl.Int16},
    )
    return ledger, games


def identity(inputs: lp.Day) -> dict[str, Any]:
    published = inputs.published_utc or inputs.decision_utc
    return {
        "day": inputs.day.isoformat(),
        "decision_utc": inputs.decision_utc.isoformat(),
        "published_utc": published.isoformat(),
        "input_cutoff": lp.input_cutoff(inputs.quotes, inputs.decision_utc).isoformat(),
        "bankroll": inputs.bankroll,
        "policy_version": POLICY_VERSION,
        "blend_version": inputs.live.version,
        "feature_build": inputs.versions["feature_build"],
        "code_version": inputs.versions["code_version"],
        "live_fit": FIT.name,
        "live_fit_sha256": lb.sha256(FIT.read_bytes()),
    }


def decided(store: lb.Store, monkeypatch: pytest.MonkeyPatch, **changes: Any) -> pl.DataFrame:
    """The fixture day, with bets, decided under the committed live fit, its bundle in store and
    its models replayed as decided; returns its ledger."""
    live = blend_fit.load(json.loads(FIT.read_text()))
    versions = {**pf.day().versions, "blend_version": live.version}
    inputs = pf.day(
        live=live, versions=versions, published_utc=pf.DECISION + timedelta(minutes=4), **changes
    )
    ledger = lp.ledger(lp.decide(inputs), inputs)
    rows = lb.inputs(inputs.slate, RECORD, inputs.quotes, None)
    lb.write_once(store, pf.DAY, rows, {}, identity(inputs), {})
    monkeypatch.setattr(lb, "models", lambda _: inputs.fitted)
    return ledger


def computed(
    store: lb.Store, ledger: pl.DataFrame, challenger: lc.Challenger, lake: Any = None
) -> pl.DataFrame:
    bundle, sha = lc.load(store, pf.DAY)
    return lc.run(
        challenger,
        bundle,
        sha,
        lc.live_fit(bundle),
        lake or NoLake(),
        "challengers-x",
        NOW,
        ledger,
    )


def test_market_only_is_the_ledgers_own_market_only_control(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = lb.LocalStore(tmp_path)
    ledger = decided(store, monkeypatch)
    output = computed(store, ledger, MARKET_ONLY)
    ChallengerPredictions.validate(output)
    joined = output.join(ledger, on=["game_date", "game_id"], suffix="_ledger")
    assert joined.height == ledger.height == 3
    # Exactly the ledger's value, game by game: the mechanism adds nothing of its own.
    assert joined["p_home"].equals(joined["p_blend_market"])
    assert joined["status"].to_list() == [lp.PREDICTED] * 3
    manifest = store.get(f"{lb.prefix(pf.DAY)}/{lb.MANIFEST}")
    assert set(output["bundle_sha256"]) == {lb.sha256(manifest)}
    assert set(output["reads"]) == {"{}"}
    assert set(output["challenger"]) == {"market_only"} and set(output["adr"]) == {"0038"}


def test_a_day_on_or_before_the_registration_is_never_due_nor_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = lb.LocalStore(tmp_path)
    ledger = decided(store, monkeypatch)
    on_the_day = replace(MARKET_ONLY, registered_on=pf.DAY)
    assert lc.due(store, (on_the_day,)) == []
    with pytest.raises(ValueError, match="never its evidence"):
        computed(store, ledger, on_the_day)
    # The real registration is later than the fixture's day: nothing of it is due.
    assert lc.due(store, lc.REGISTRY) == []
    assert lc.due(store, (MARKET_ONLY,)) == [(MARKET_ONLY, pf.DAY)]
    lc.write(store, MARKET_ONLY, pf.DAY, computed(store, ledger, MARKET_ONLY))
    assert lc.due(store, (MARKET_ONLY,)) == []


def test_an_incomplete_bundle_is_not_due(tmp_path: Path) -> None:
    store = lb.LocalStore(tmp_path)
    inputs = pf.day()
    lb.write_files(store, pf.DAY, lb.inputs(inputs.slate, RECORD, inputs.quotes, None), {})
    assert lc.complete_days(store) == []
    assert lc.due(store, (MARKET_ONLY,)) == []


def test_an_output_is_written_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = lb.LocalStore(tmp_path)
    ledger = decided(store, monkeypatch)
    output = computed(store, ledger, MARKET_ONLY)
    key = lc.write(store, MARKET_ONLY, pf.DAY, output)
    assert key == "challengers/live/market_only/1/2026-10-07.parquet"
    # The same bytes again count as written, as after a write that failed part way.
    lc.write(store, MARKET_ONLY, pf.DAY, output)
    later = output.with_columns(computed_utc=pl.lit(NOW + timedelta(hours=1)))
    with pytest.raises(lb.Conflict):
        lc.write(store, MARKET_ONLY, pf.DAY, later)
    assert lc.sync(store).equals(output)


def test_a_stored_day_is_checked_against_its_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = lb.LocalStore(tmp_path)
    ledger = decided(store, monkeypatch)
    bundle, _ = lc.load(store, pf.DAY)
    live = lc.live_fit(bundle)
    key = lc.write(store, MARKET_ONLY, pf.DAY, computed(store, ledger, MARKET_ONLY))
    later = NOW + timedelta(days=3)
    assert lc.check(store, MARKET_ONLY, pf.DAY, live, NoLake(), later, ledger) == []
    # Altered bytes are caught: a probability moved, and a status changed.
    stored = pl.read_parquet(tmp_path / key)
    first = stored["game_id"][0]
    moved = stored.with_columns(
        p_home=pl.when(pl.col("game_id") == first).then(pl.col("p_home") + 0.01).otherwise("p_home")
    )
    moved.write_parquet(tmp_path / key)
    assert lc.check(store, MARKET_ONLY, pf.DAY, live, NoLake(), later, ledger) == [
        f"p_home differs for [{first}]"
    ]
    stored.with_columns(status=pl.lit(lp.STALE), p_home=None).write_parquet(tmp_path / key)
    problems = lc.check(store, MARKET_ONLY, pf.DAY, live, NoLake(), later, ledger)
    assert problems[0].startswith("p_home differs") and problems[1].startswith("status differs")


def test_a_bundle_that_does_not_replay_its_ledger_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = lb.LocalStore(tmp_path)
    ledger = decided(store, monkeypatch)
    moved = ledger.with_columns(stake=pl.col("stake") * 1.01)
    with pytest.raises(ValueError, match="doesn't replay the ledger"):
        computed(store, moved, MARKET_ONLY)


def test_a_game_left_out_or_without_a_probability_says_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = lb.LocalStore(tmp_path)
    ledger = decided(store, monkeypatch)
    first, second, _ = sorted(ledger["game_id"].to_list())

    def partial(bundle: Any, replayed: pl.DataFrame, live: Any, lake: Any) -> pl.DataFrame:
        return pl.DataFrame(
            {"game_id": [first, second], "p_home": [0.55, None]},
            schema={"game_id": pl.Int64, "p_home": pl.Float64},
        )

    challenger = replace(MARKET_ONLY, name="partial", predict=partial)
    statuses = dict(computed(store, ledger, challenger).select("game_id", "status").iter_rows())
    assert statuses[first] == lc.PREDICTED
    assert statuses[second] == lc.LEFT_OUT
    assert sorted(statuses.values()).count(lc.LEFT_OUT) == 2

    def no_reason(bundle: Any, replayed: pl.DataFrame, live: Any, lake: Any) -> pl.DataFrame:
        return replayed.select("game_id", "status", p_home=pl.lit(None, pl.Float64))

    blank = replace(MARKET_ONLY, name="blank", predict=no_reason)
    assert set(computed(store, ledger, blank)["status"]) == {lc.NO_PROBABILITY}


def test_every_registered_challenger_names_its_adr_and_a_unique_version() -> None:
    decisions = Path("docs/decisions")
    for challenger in lc.REGISTRY:
        assert list(decisions.glob(f"{challenger.adr}-*.md")), challenger.name
    names = [(c.name, c.version) for c in lc.REGISTRY]
    assert len(names) == len(set(names))


def test_nhl_live_challengers_writes_the_due_days_once_and_checks_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from nhl_edge.cli import app

    out = tmp_path / "dry"
    store = lb.LocalStore(out)
    ledger = decided(store, monkeypatch)
    ledger.write_parquet(out / f"{pf.DAY.isoformat()}.parquet")
    monkeypatch.setattr(lc, "REGISTRY", (MARKET_ONLY,))
    runner = CliRunner()
    result = runner.invoke(app, ["live", "challengers", "--from", str(out)])
    assert result.exit_code == 0, result.output
    assert "market_only 1, 2026-10-07: 3 games, 3 predicted" in result.output
    assert "1 of 1 due outputs written" in result.output
    again = runner.invoke(app, ["live", "challengers", "--from", str(out)])
    assert "0 of 0 due outputs written" in again.output
    check = runner.invoke(app, ["live", "challengers", "--check", "--from", str(out)])
    assert check.exit_code == 0, check.output
    assert "recomputed from its bundle, identical" in check.output
    # A stored output that no longer matches fails the check.
    key = out / MARKET_ONLY.key(pf.DAY)
    pl.read_parquet(key).with_columns(p_home=pl.col("p_home") * 0.99).write_parquet(key)
    check = runner.invoke(app, ["live", "challengers", "--check", "--from", str(out)])
    assert check.exit_code == 1
    assert "p_home differs" in check.output
    # One of --r2 and --from.
    assert runner.invoke(app, ["live", "challengers"]).exit_code != 0


def test_one_broken_challenger_never_stops_the_others_and_check_reads_every_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from nhl_edge.cli import app

    out = tmp_path / "dry"
    store = lb.LocalStore(out)
    ledger = decided(store, monkeypatch)
    ledger.write_parquet(out / f"{pf.DAY.isoformat()}.parquet")

    def broken(bundle: Any, replayed: pl.DataFrame, live: Any, lake: Any) -> pl.DataFrame:
        raise IndexError("a bug in the challenger")

    # Codex on #265: an error of any kind is reported per pair, and the others still run.
    monkeypatch.setattr(
        lc, "REGISTRY", (replace(MARKET_ONLY, name="broken", predict=broken), MARKET_ONLY)
    )
    runner = CliRunner()
    result = runner.invoke(app, ["live", "challengers", "--from", str(out)])
    assert result.exit_code == 1
    assert "broken 1, 2026-10-07: a bug in the challenger" in result.output
    assert "market_only 1, 2026-10-07: 3 games, 3 predicted" in result.output
    assert "1 of 2 due outputs written" in result.output
    # A stored output whose bundle has gone is checked, and fails (Codex on #265).
    (out / lb.prefix(pf.DAY) / lb.MANIFEST).unlink()
    check = runner.invoke(app, ["live", "challengers", "--check", "--from", str(out)])
    assert check.exit_code == 1
    assert "market_only 1, 2026-10-07:" in check.output and "no manifest" in check.output
    assert "0 of 1 stored outputs reproduced" in check.output


def test_the_conservative_fit_is_the_posterior_mode_under_adr_0041s_priors() -> None:
    ledger, games = history(3000)
    y = games["home_score"] > games["away_score"]
    weights, errors = lc.conservative_fit(y, ledger["p_b0"], ledger["p_b3"])
    # The log posterior's gradient is zero at the weights: the likelihood's pull against the
    # priors' (a ~ N(0, 0.05²), b_m ~ N(1, 0.15²), b_d ~ N(0, 0.5²)).
    x = lc.conservative_design(ledger["p_b0"], ledger["p_b3"])
    p = sigmoid(x @ weights)
    prior = (weights - np.array([0.0, 1.0, 0.0])) / np.array([0.05, 0.15, 0.5]) ** 2
    assert np.abs(x.T @ (y.to_numpy() - p) - prior).max() < 1e-8
    # On 3,000 games it finds the weights drawn from, within its standard errors.
    assert np.abs(weights - np.array([0.02, 1.05, 0.4])).max() < 3 * errors.max()
    assert (errors < np.array([0.05, 0.15, 0.5])).all()
    # On a handful of games the priors hold it near the market; all home wins stay finite.
    few, _ = history(5, seed=1)
    weights, _ = lc.conservative_fit([1] * 5, few["p_b0"], few["p_b3"])
    assert np.isfinite(weights).all()
    assert np.abs(weights - np.array([0.0, 1.0, 0.0])).max() < 0.5
    assert abs(weights[0]) < 0.05


def test_conservative_blend_is_unfitted_below_its_minimum(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = lb.LocalStore(tmp_path)
    ledger = decided(store, monkeypatch)
    rows, games = history(lc.MINIMUM_GAMES - 1)
    output = computed(store, ledger, CONSERVATIVE, Lake(paper_ledger=rows, games=games))
    ChallengerPredictions.validate(output)
    assert output["status"].to_list() == [lc.UNFITTED] * 3
    assert output["p_home"].null_count() == 3
    assert output["fit"].n_unique() == 1
    assert json.loads(output["fit"][0]) == {"week": "2026-10-05", "games": 199, "minimum": 200}
    assert set(json.loads(output["reads"][0])) == {"paper_ledger:20262027", "games:20262027"}


def test_conservative_blend_predicts_from_the_weeks_fit_and_saves_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = lb.LocalStore(tmp_path)
    ledger = decided(store, monkeypatch)
    rows, games = history(300)
    lake = Lake(paper_ledger=rows, games=games)
    output = computed(store, ledger, CONSERVATIVE, lake)
    ChallengerPredictions.validate(output)
    assert output["status"].to_list() == [lc.PREDICTED] * 3
    y = games["home_score"] > games["away_score"]
    weights, errors = lc.conservative_fit(y, rows["p_b0"], rows["p_b3"])
    # Each slate game's probability from the fit, on the day's replayed p_b0 and p_b3.
    day = ledger.sort("game_id")
    expected = sigmoid(lc.conservative_design(day["p_b0"], day["p_b3"]) @ weights)
    assert np.allclose(output.sort("game_id")["p_home"].to_numpy(), expected, rtol=0, atol=1e-12)
    fit = json.loads(output["fit"][0])
    assert fit["week"] == "2026-10-05" and fit["games"] == 300
    assert list(fit["weights"]) == ["a", "b_m", "b_d"]
    assert np.allclose(list(fit["weights"].values()), weights, rtol=0, atol=1e-12)
    assert np.allclose(list(fit["standard_errors"].values()), errors, rtol=0, atol=1e-12)
    assert fit["train_cutoff"] == games["observed_utc"].max().isoformat()  # type: ignore[union-attr]
    # Stored, the day checks against its bundle and its lake reads; a changed fit is named.
    key = lc.write(store, CONSERVATIVE, pf.DAY, output)
    bundle, _ = lc.load(store, pf.DAY)
    live = lc.live_fit(bundle)
    assert lc.check(store, CONSERVATIVE, pf.DAY, live, lake, NOW, ledger) == []
    moved = {**fit, "weights": {**fit["weights"], "b_d": fit["weights"]["b_d"] + 0.01}}
    stored = pl.read_parquet(tmp_path / key)
    stored.with_columns(fit=pl.lit(json.dumps(moved))).write_parquet(tmp_path / key)
    assert lc.check(store, CONSERVATIVE, pf.DAY, live, lake, NOW, ledger) == ["the fit differs"]
    # Ledger rows read that have changed since are named too.
    more, more_games = history(310)
    changed = Lake(paper_ledger=more, games=more_games)
    stored.write_parquet(tmp_path / key)
    problems = lc.check(store, CONSERVATIVE, pf.DAY, live, changed, NOW, ledger)
    assert problems[-1] == (
        "the lake rows read have changed since: games:20262027, paper_ledger:20262027"
    )


def test_an_output_written_before_the_fit_column_syncs_with_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = lb.LocalStore(tmp_path)
    ledger = decided(store, monkeypatch)
    output = computed(store, ledger, MARKET_ONLY)
    assert output["fit"].null_count() == output.height
    key = lc.write(store, MARKET_ONLY, pf.DAY, output.drop("fit"))
    assert "fit" not in pl.read_parquet(tmp_path / key).columns
    assert lc.sync(store).equals(output)
    bundle, _ = lc.load(store, pf.DAY)
    assert lc.check(store, MARKET_ONLY, pf.DAY, lc.live_fit(bundle), NoLake(), NOW, ledger) == []


def test_nhl_live_challengers_trains_conservative_blend_on_the_ledgers_themselves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from nhl_edge.cli import app
    from nhl_edge.lake import tables

    out = tmp_path / "dry"
    store = lb.LocalStore(out)
    ledger = decided(store, monkeypatch)
    ledger.write_parquet(out / f"{pf.DAY.isoformat()}.parquet")
    rows, games = history(250)
    # The earlier week's ledgers sit beside the day's, as a dry run writes them; the lake's own
    # copy of the ledgers is never read.
    for day, frame in rows.group_by("game_date"):
        frame.write_parquet(out / f"{day[0]}.parquet")
    lake = Lake(games=games, paper_ledger=pl.DataFrame())
    monkeypatch.setattr(tables.Lake, "from_env", lambda *args, **kwargs: lake)
    monkeypatch.setattr(lc, "REGISTRY", (CONSERVATIVE,))
    runner = CliRunner()
    result = runner.invoke(app, ["live", "challengers", "--from", str(out)])
    assert result.exit_code == 0, result.output
    assert "conservative_blend 1, 2026-10-07: 3 games, 3 predicted" in result.output
    written = pl.read_parquet(out / CONSERVATIVE.key(pf.DAY))
    assert json.loads(written["fit"][0])["games"] == 250
    check = runner.invoke(app, ["live", "challengers", "--check", "--from", str(out)])
    assert check.exit_code == 0, check.output
