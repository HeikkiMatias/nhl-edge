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
# The challengers as if registered the day before the fixture's decision day.
MARKET_ONLY = replace(lc.REGISTRY[0], registered_on=pf.DAY - timedelta(days=1))
CONSERVATIVE = replace(lc.REGISTRY[1], registered_on=pf.DAY - timedelta(days=1))
SPREAD = replace(lc.REGISTRY[2], registered_on=pf.DAY - timedelta(days=1))
RESIDUAL = replace(lc.REGISTRY[3], registered_on=pf.DAY - timedelta(days=1))
# The fixture day's ET week starts on Monday 2026-10-05.
WEEK = date(2026, 10, 5)
TEAMS = ("BOS", "TOR", "MTL", "OTT", "DET", "BUF", "FLA", "TBL", "NYR")


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
            "home": [TEAMS[i % len(TEAMS)] for i in range(n)],
            "away": [TEAMS[(i + 1 + i // len(TEAMS)) % len(TEAMS)] for i in range(n)],
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


class Scenarios:
    """A B3 fit whose goalie scenarios' log-odds are given (B3Model.log_odds)."""

    def __init__(self, frame: pl.DataFrame) -> None:
        self.frame = frame

    def log_odds(self, inputs: Any, pairs: Any) -> pl.DataFrame:
        return self.frame


def with_scenarios(monkeypatch: pytest.MonkeyPatch, bundle: lb.Bundle) -> lb.Bundle:
    """The bundle with B3's scenarios for the fixture's three games: one scenario; two of equal
    weight, 0.2 apart; and weights 0.9 and 0.1 (unnormalized), 1 apart."""
    first, second, third = sorted(pf.GAMES)
    frame = pl.DataFrame(
        {
            "game_id": [first, second, second, third, third],
            "weight": [1.0, 0.5, 0.5, 0.45, 0.05],
            "eta": [0.3, 0.2, 0.4, 0.0, 1.0],
        }
    )
    monkeypatch.setattr(lb, "b3_scenarios", lambda _: (None, None))
    return replace(bundle, b3=Scenarios(frame))  # type: ignore[arg-type]


def test_a_goalie_spread_is_the_weighted_sd_of_b3s_log_odds_over_its_scenarios(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = lb.LocalStore(tmp_path)
    decided(store, monkeypatch)
    bundle, _ = lc.load(store, pf.DAY)
    # Without the day's models, no spread.
    assert lc.goalie_spreads(bundle).is_empty()
    spreads = lc.goalie_spreads(with_scenarios(monkeypatch, bundle))
    assert spreads["scenarios"].to_list() == [1, 2, 2]
    # 0 for one scenario; sd 0.1 about 0.3; and about 0.9·0 + 0.1·1 = 0.1, √(0.9·0.01 + 0.1·0.81).
    assert np.allclose(spreads["goalie_spread"].to_numpy(), [0.0, 0.1, 0.3], rtol=0, atol=1e-12)


def test_b3s_mix_is_its_scenarios_log_odds_weighted() -> None:
    from nhl_edge.game import b3

    model = b3.B3Model(
        season=20262027,
        settings=None,  # type: ignore[arg-type]
        intercept=0.1,
        weights=(0.5, 0.2),
        means=(0.0, 1.0),
        scales=(1.0, 2.0),
        games=10,
        train_cutoff=NOW,
        inputs=("delta_g_hat", "rest"),
    )
    inputs = pl.DataFrame({"game_id": [1, 2], "rest": [1.0, 3.0], "offset": [0.0, 0.05]})
    pairs = pl.DataFrame(
        {"game_id": [1, 1, 2], "weight": [0.7, 0.3, 1.0], "delta_g_hat": [0.4, -0.2, 0.1]}
    )
    scenarios = model.log_odds(inputs, pairs)
    # Each scenario: intercept + offset + the weights on the standardized inputs.
    assert np.allclose(
        scenarios["eta"].to_numpy(),
        [0.1 + 0.5 * 0.4, 0.1 + 0.5 * -0.2, 0.1 + 0.05 + 0.5 * 0.1 + 0.2 * 1.0],
    )
    mixed = model.predict(inputs, pairs).sort("game_id")["p_home"].to_numpy()
    p = sigmoid(scenarios["eta"].to_numpy())
    assert np.allclose(mixed, [0.7 * p[0] + 0.3 * p[1], p[2]])


def test_each_v2_days_spreads_are_written_once_and_checked_against_its_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = lb.LocalStore(tmp_path)
    decided(store, monkeypatch)
    real = lc.goalie_spreads
    monkeypatch.setattr(
        lc, "goalie_spreads", lambda bundle: real(with_scenarios(monkeypatch, bundle))
    )
    written, problems = lc.write_spreads(store, "challengers-x", NOW)
    assert written == ["challengers/inputs/goalie_spread/2026-10-07.parquet"] and problems == []
    assert lc.write_spreads(store, "challengers-x", NOW) == ([], [])
    kept = lc.sync_spreads(store)
    assert kept.height == 3 and set(kept["game_date"]) == {pf.DAY}
    assert lc.check_spreads(store, pf.DAY) == []
    # An altered spread is named.
    key = tmp_path / lc.spread_key(pf.DAY)
    pl.read_parquet(key).with_columns(goalie_spread=pl.col("goalie_spread") + 0.01).write_parquet(
        key
    )
    assert lc.check_spreads(store, pf.DAY)[0].startswith("goalie_spread differs")


def test_a_v1_day_keeps_no_spreads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from nhl_edge.betting.selection import POLICY_V1

    store = lb.LocalStore(tmp_path)
    live = blend_fit.load(json.loads(FIT.read_text()))
    inputs = pf.day(live=live, versions={**pf.day().versions, "blend_version": live.version})
    rows = lb.inputs(inputs.slate, RECORD, inputs.quotes, None)
    lb.write_once(store, pf.DAY, rows, {}, identity(inputs) | {"policy_version": POLICY_V1}, {})
    assert lc.spread_days(store) == []
    assert lc.write_spreads(store, "challengers-x", NOW) == ([], [])


def spreads_of(rows: pl.DataFrame, spread: np.ndarray) -> pl.DataFrame:
    """The kept spreads of history()'s decisions."""
    return rows.select(
        "game_date",
        "season",
        "game_id",
        scenarios=pl.lit(2, pl.Int32),
        goalie_spread=pl.Series(spread),
        bundle_sha256=pl.lit("0" * 64),
        input_cutoff_utc=pl.col("published_utc") - timedelta(minutes=5),
        code_version=pl.lit("challengers-x"),
        computed_utc=pl.col("published_utc") + timedelta(hours=16),
    )


def test_goalie_spread_blend_trains_only_on_games_with_a_spread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = lb.LocalStore(tmp_path)
    ledger = decided(store, monkeypatch)
    rows, games = history(250)
    # 60 of the 250 training games lack a spread: 190 are too few.
    spreads = spreads_of(rows, np.full(250, 0.05)).head(190)
    output = computed(
        store, ledger, SPREAD, Lake(paper_ledger=rows, games=games, goalie_spreads=spreads)
    )
    assert output["status"].to_list() == [lc.UNFITTED] * 3
    assert json.loads(output["fit"][0]) == {
        "week": "2026-10-05",
        "games": 190,
        "without_spread": 60,
        "minimum": 200,
    }
    reads = set(json.loads(output["reads"][0]))
    assert reads == {"paper_ledger:20262027", "games:20262027", "goalie_spreads:20262027"}


def test_goalie_spread_blend_predicts_from_the_weeks_fit_with_the_days_own_spreads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = lb.LocalStore(tmp_path)
    ledger = decided(store, monkeypatch)
    rows, games = history(400)
    spread = np.random.default_rng(3).uniform(0, 0.3, 400)
    lake = Lake(paper_ledger=rows, games=games, goalie_spreads=spreads_of(rows, spread))
    real = lc.goalie_spreads
    monkeypatch.setattr(
        lc, "goalie_spreads", lambda bundle: real(with_scenarios(monkeypatch, bundle))
    )
    output = computed(store, ledger, SPREAD, lake)
    ChallengerPredictions.validate(output)
    assert output["status"].to_list() == [lc.PREDICTED] * 3
    y = games["home_score"] > games["away_score"]
    x = lc.spread_design(rows["p_b0"], rows["p_b3"], spread)
    weights, errors = lc.map_fit(x, y, lc.SPREAD_PRIOR_MEAN, lc.SPREAD_PRIOR_SD)
    fit = json.loads(output["fit"][0])
    assert list(fit["weights"]) == ["a", "b_m", "b_d", "b_s"] and fit["without_spread"] == 0
    assert np.allclose(list(fit["weights"].values()), weights, rtol=0, atol=1e-12)
    assert np.allclose(list(fit["standard_errors"].values()), errors, rtol=0, atol=1e-12)
    # Each slate game's probability from the fit on its replayed prices and its own spread.
    day = ledger.sort("game_id")
    expected = sigmoid(lc.spread_design(day["p_b0"], day["p_b3"], [0.0, 0.1, 0.3]) @ weights)
    assert np.allclose(output.sort("game_id")["p_home"].to_numpy(), expected, rtol=0, atol=1e-12)


def test_the_spread_fit_is_the_posterior_mode_and_finds_the_spreads_weight() -> None:
    ledger, _ = history(4000, seed=4)
    rng = np.random.default_rng(5)
    spread = rng.uniform(0, 0.3, 4000)
    # Results drawn with b_s = -0.5: B3's weight falls by 0.5 per 0.1 of spread.
    truth = sigmoid(lc.spread_design(ledger["p_b0"], ledger["p_b3"], spread) @ [0, 1, 0.3, -0.5])
    y = rng.random(4000) < truth
    x = lc.spread_design(ledger["p_b0"], ledger["p_b3"], spread)
    weights, errors = lc.map_fit(x, y, lc.SPREAD_PRIOR_MEAN, lc.SPREAD_PRIOR_SD)
    p = sigmoid(x @ weights)
    prior = (weights - lc.SPREAD_PRIOR_MEAN) / lc.SPREAD_PRIOR_SD**2
    assert np.abs(x.T @ (y - p) - prior).max() < 1e-8
    assert abs(weights[3] + 0.5) < 3 * errors[3] and errors[3] < 0.5


def test_a_baseline_names_a_registered_challenger_or_a_ledger_model() -> None:
    keys = {f"{c.name}/{c.version}" for c in lc.REGISTRY} | set(lc.LEDGER_BASELINES)
    for challenger in lc.REGISTRY:
        assert challenger.baseline is None or challenger.baseline in keys
    assert lc.REGISTRY[2].baseline == "conservative_blend/1"
    assert lc.REGISTRY[4].baseline == "B3" and lc.LEDGER_BASELINES["B3"] == "p_b3"


def test_nhl_live_challengers_keeps_the_spreads_before_scoring(
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
    for day, frame in rows.group_by("game_date"):
        frame.write_parquet(out / f"{day[0]}.parquet")
    real = lc.goalie_spreads
    monkeypatch.setattr(
        lc, "goalie_spreads", lambda bundle: real(with_scenarios(monkeypatch, bundle))
    )
    lake = Lake(games=games, paper_ledger=pl.DataFrame(), goalie_spreads=pl.DataFrame())
    monkeypatch.setattr(tables.Lake, "from_env", lambda *args, **kwargs: lake)
    monkeypatch.setattr(lc, "REGISTRY", (CONSERVATIVE, SPREAD))
    runner = CliRunner()
    result = runner.invoke(app, ["live", "challengers", "--from", str(out)])
    assert result.exit_code == 0, result.output
    assert "goalie spreads written for 1 days" in result.output
    # The earlier week's days have no bundle, so no spreads: the challenger can't fit yet.
    assert "goalie_spread_blend 1, 2026-10-07: 3 games, 0 predicted" in result.output
    written = pl.read_parquet(out / SPREAD.key(pf.DAY))
    assert json.loads(written["fit"][0])["without_spread"] == 250
    assert (out / lc.spread_key(pf.DAY)).exists()
    check = runner.invoke(app, ["live", "challengers", "--check", "--from", str(out)])
    assert check.exit_code == 0, check.output
    # An altered spread fails the check.
    key = out / lc.spread_key(pf.DAY)
    pl.read_parquet(key).with_columns(goalie_spread=pl.col("goalie_spread") + 1).write_parquet(key)
    check = runner.invoke(app, ["live", "challengers", "--check", "--from", str(out)])
    assert check.exit_code == 1
    assert "goalie spreads, 2026-10-07: goalie_spread differs" in check.output


def test_team_effects_are_the_weighted_posterior_mode_on_b3s_log_odds() -> None:
    rows, _ = history(3000, seed=6)
    truth = {"BOS": 0.4, "TOR": -0.4}
    rng = np.random.default_rng(7)
    shift = np.array(
        [
            truth.get(h, 0.0) - truth.get(a, 0.0)
            for h, a in zip(rows["home"], rows["away"], strict=True)
        ]
    )
    y = rng.random(rows.height) < sigmoid(logit(rows["p_b3"].to_numpy()) + shift)
    trained = rows.with_columns(home_win=pl.Series(y.astype(np.int8)))
    effects = lc.team_effects(trained, WEEK)
    assert effects["BOS"] > 0.1 > -0.1 > effects["TOR"]
    assert all(abs(effects[t]) < 0.1 for t in effects if t not in truth)
    # The gradient of the weighted log posterior is zero at the effects.
    teams = sorted(effects)
    x = np.zeros((rows.height, len(teams)))
    x[np.arange(rows.height), [teams.index(t) for t in rows["home"]]] = 1
    x[np.arange(rows.height), [teams.index(t) for t in rows["away"]]] = -1
    age = np.array([(WEEK - d).days for d in rows["game_date"]])
    w = 0.5 ** (age / lc.HALF_LIFE_DAYS)
    theta = np.array([effects[t] for t in teams])
    p = sigmoid(logit(rows["p_b3"].to_numpy()) + x @ theta)
    assert np.abs(x.T @ (w * (y - p)) - theta / lc.TEAM_PRIOR_SD**2).max() < 1e-8
    assert lc.team_effects(trained.head(0), WEEK) == {}


def test_a_games_weight_halves_every_half_life() -> None:
    # One home win of BOS over TOR moves BOS's effect less the older it is.
    def effect(age: int) -> float:
        one = pl.DataFrame(
            {
                "game_date": [WEEK - timedelta(days=age)],
                "home": ["BOS"],
                "away": ["TOR"],
                "p_b3": [0.5],
                "home_win": [1],
            }
        )
        return lc.team_effects(one, WEEK)["BOS"]

    assert effect(0) > effect(42) > effect(84) > 0
    # With one game the mode is nearly linear in its weight: halved per half-life.
    assert effect(42) / effect(0) == pytest.approx(0.5, rel=0.02)


def test_b3_is_corrected_by_the_home_teams_effect_less_the_away_teams() -> None:
    frame = pl.DataFrame({"home": ["BOS", "NJD"], "away": ["TOR", "BOS"], "p_b3": [0.5, 0.6]})
    corrected = lc.corrected(frame, {"BOS": 0.1, "TOR": -0.05})
    # A team without an effect keeps 0.
    assert np.allclose(
        corrected.to_numpy(), sigmoid(logit(np.array([0.5, 0.6])) + np.array([0.15, -0.1]))
    )


def two_weeks() -> tuple[pl.DataFrame, pl.DataFrame]:
    """history()'s decisions over the two weeks before the fixture's."""
    first = history(150, first=WEEK - timedelta(days=14), seed=8)
    second = history(150, first=WEEK - timedelta(days=7), seed=9)
    return pl.concat([first[0], second[0]]), pl.concat([first[1], second[1]])


def test_team_residual_blend_is_unfitted_below_its_minimum_with_the_weeks_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = lb.LocalStore(tmp_path)
    ledger = decided(store, monkeypatch)
    rows, games = history(120)
    output = computed(store, ledger, RESIDUAL, Lake(paper_ledger=rows, games=games))
    assert output["status"].to_list() == [lc.UNFITTED] * 3
    fit = json.loads(output["fit"][0])
    assert (fit["games"], fit["minimum"]) == (120, 200)
    assert sorted(fit["team_effects"]) == sorted(set(rows["home"]) | set(rows["away"]))


def test_team_residual_blend_predicts_on_b3_corrected_by_each_weeks_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = lb.LocalStore(tmp_path)
    ledger = decided(store, monkeypatch)
    rows, games = two_weeks()
    output = computed(store, ledger, RESIDUAL, Lake(paper_ledger=rows, games=games))
    ChallengerPredictions.validate(output)
    assert output["status"].to_list() == [lc.PREDICTED] * 3
    trained, effects = lc.corrected_training(rows, games, pf.DAY)
    # The first week's rows have no earlier games to correct them; the second's are corrected
    # by the first's.
    first = trained.filter(pl.col("game_date") < WEEK - timedelta(days=7))
    raw = rows.join(first.select("game_id"), on="game_id").sort("game_date", "game_id")
    assert np.allclose(first["p_b3"].to_numpy(), raw["p_b3"].to_numpy(), rtol=0, atol=1e-15)
    second = trained.filter(pl.col("game_date") >= WEEK - timedelta(days=7))
    early = lc.team_effects(lc.training_rows(rows, games, WEEK - timedelta(days=7)), WEEK)
    assert (
        early
        and second["p_b3"].to_list()
        != rows.filter(pl.col("game_date") >= WEEK - timedelta(days=7))
        .sort("game_date", "game_id")["p_b3"]
        .to_list()
    )
    weights, _ = lc.conservative_fit(trained["home_win"], trained["p_b0"], trained["p_b3"])
    day = ledger.sort("game_id")
    expected = sigmoid(lc.conservative_design(day["p_b0"], lc.corrected(day, effects)) @ weights)
    assert np.allclose(output.sort("game_id")["p_home"].to_numpy(), expected, rtol=0, atol=1e-12)
    fit = json.loads(output["fit"][0])
    assert fit["games"] == 300 and fit["team_effects"] == pytest.approx(effects)


def test_b3_split_predicts_the_slate_from_its_bundle_beside_the_days_b3(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from b3_fixtures import day_bundle, league

    from nhl_edge.backtest.walk_forward import fold_start
    from nhl_edge.game import b3

    tables = league()
    start = fold_start(tables.games.select("season", "start_utc"), 20182019)
    plain = b3.fit_fold(tables, 20182019, start)
    split = b3.fit_fold(tables, 20182019, start, terms=b3.Terms(split=True))
    bundle = replace(day_bundle(tables), b3=plain)
    monkeypatch.setattr(lc, "split_fit", lambda: (split, plain))
    replayed = bundle.inputs["slate"].select("game_id", status=pl.lit(lp.PREDICTED))
    output = lc.REGISTRY[4].predict(bundle, replayed, None, None)  # type: ignore[arg-type]
    usable, pairs = lb.b3_scenarios(bundle)
    expected = split.predict(b3.split_inputs(usable), pairs).sort("game_id")
    assert output.sort("game_id")["p_home"].to_list() == expected["p_home"].to_list()
    assert output.height == 4 and output["p_home"].null_count() == 0
    # The split's probabilities are not B3's.
    assert (
        expected["p_home"].to_list()
        != plain.predict(usable, pairs).sort("game_id")["p_home"].to_list()
    )
    # A day whose live B3 differs from the one the split was fitted beside is refused.
    moved = replace(plain, intercept=plain.intercept + 0.01)
    with pytest.raises(ValueError, match="not the split's: intercept differs"):
        lc.REGISTRY[4].predict(replace(bundle, b3=moved), replayed, None, None)  # type: ignore[arg-type]
    fewer = replace(plain, games=plain.games - 1)
    with pytest.raises(ValueError, match="games"):
        lc.REGISTRY[4].predict(replace(bundle, b3=fewer), replayed, None, None)  # type: ignore[arg-type]
    # A day without its models keeps its statuses, with no probability (Codex on #307).
    none = lc.REGISTRY[4].predict(replace(bundle, b3=None), replayed, None, None)  # type: ignore[arg-type]
    assert none["p_home"].null_count() == none.height
    assert none["status"].to_list() == replayed["status"].to_list()


def test_the_split_fit_is_pinned_and_refused_when_altered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nhl_edge.live import b3_split

    record = {
        "version": "b3-split-x",
        "split": {
            "season": 20262027,
            "settings": {"l2": 1.0},
            "intercept": 0.1,
            "weights": [0.3, 0.05, 0.15, 0.05],
            "means": [0.0] * 4,
            "scales": [1.0] * 4,
            "games": 10,
            "train_cutoff": "2026-10-01T00:00:00+00:00",
            "inputs": list(b3_split.SPLIT.inputs())[:4],
        },
    }
    path = tmp_path / "b3-split-x.json"
    body = json.dumps(record).encode()
    path.write_bytes(body)
    monkeypatch.setattr(lc, "B3_SPLIT_FIT", path)
    monkeypatch.setattr(lc, "B3_SPLIT_SHA256", lb.sha256(body + b" "))
    with pytest.raises(ValueError, match="not the split fit"):
        lc.split_fit()


def test_the_registered_split_fit_is_the_committed_one_beside_the_live_b3() -> None:
    from nhl_edge.game import b3

    split, plain = lc.split_fit()
    assert split.inputs == b3.Terms(split=True).inputs() and plain.inputs == b3.INPUTS
    assert split.games == plain.games and split.train_cutoff == plain.train_cutoff
    record = json.loads(lc.B3_SPLIT_FIT.read_text())
    assert record["live_fit"] == blend_fit.load(json.loads(FIT.read_text())).version
    assert record["season"] == blend_fit.LIVE_SEASON


def test_b3_split_scores_a_day_without_models_as_its_ledger_says(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The fixture's bundle has no fits, as a day whose feature build was refused: run() keeps
    # each game's replayed status rather than failing (Codex on #307).
    store = lb.LocalStore(tmp_path)
    ledger = decided(store, monkeypatch)
    split = replace(lc.REGISTRY[4], registered_on=pf.DAY - timedelta(days=1))
    output = computed(store, ledger, split)
    ChallengerPredictions.validate(output)
    assert output["p_home"].null_count() == output.height
    assert output["status"].to_list() == [lc.NO_PROBABILITY] * output.height


# close_model (ADR 0049)

CLOSE_MODEL = replace(lc.REGISTRY[5], registered_on=pf.DAY - timedelta(days=1))
# The fixture day's games' closing proxies at the 18:45 ET slot.
PROXY = datetime(2026, 10, 7, 22, 45, tzinfo=UTC)


def lake_odds(quotes: pl.DataFrame) -> pl.DataFrame:
    """Quotes as the lake's odds_snapshots holds them, with the columns set afterwards."""
    return quotes.with_columns(
        game_id=pl.col("event_id").str.strip_prefix("e").cast(pl.Int64), game_type=pl.lit(2)
    )


def moves(n: int, seed: int = 0) -> pl.DataFrame:
    """n training rows of close_rows' columns, their moves drawn from a = 0.005, b_d = 0.1,
    b_m = 0.3 and b_g = 0.2 with a residual sd of 0.04."""
    rng = np.random.default_rng(seed)
    d_b3, m, g = rng.normal(0, 0.25, n), rng.normal(0, 0.05, n), rng.normal(0, 0.03, n)
    move = 0.005 + 0.1 * d_b3 + 0.3 * m + 0.2 * g + rng.normal(0, 0.04, n)
    days = [WEEK - timedelta(days=7) + timedelta(days=i % 7) for i in range(n)]
    return pl.DataFrame(
        {
            "game_date": days,
            "game_id": 2026010001 + np.arange(n),
            "d_b3": d_b3,
            "m": m,
            "g": g,
            "move": move,
            "close_snapshot_utc": [datetime.combine(day, time(22, 45), UTC) for day in days],
            "published_utc": [datetime.combine(day, time(16, 50), UTC) for day in days],
        }
    )


def test_the_close_fit_is_the_posterior_mode_under_adr_0049s_priors() -> None:
    rows = moves(3000)
    x = lc.close_design(rows)
    weights, errors, variance = lc.close_fit(x, rows["move"])
    y = rows["move"].to_numpy()
    # The log posterior's gradient is zero at the weights, at the residual variance there.
    assert variance == pytest.approx(np.mean((y - x @ weights) ** 2), rel=1e-6)
    prior = (weights - 0.0) / np.array([0.02, 0.5, 0.5, 0.5]) ** 2
    assert np.abs(x.T @ (y - x @ weights) / variance - prior).max() < 1e-6
    # On 3,000 games it finds the weights drawn from, within its standard errors.
    assert np.abs(weights - np.array([0.005, 0.1, 0.3, 0.2])).max() < 3 * errors.max()
    assert np.sqrt(variance) == pytest.approx(0.04, rel=0.05)
    # On a handful of moves all alike, the priors keep it finite and the intercept near 0.
    few = moves(5).with_columns(move=pl.lit(0.0))
    weights, _, variance = lc.close_fit(lc.close_design(few), few["move"])
    assert np.isfinite(weights).all() and variance >= lc.VARIANCE_FLOOR


def test_close_inputs_are_b3s_disagreement_the_morning_move_and_the_other_books_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nhl_edge.market.devig import fair_probabilities

    store = lb.LocalStore(tmp_path)
    # Pinnacle moved on WSH since the morning; bet365 quotes it beside Pinnacle at midday.
    quotes = pf.day_quotes().with_columns(
        price_decimal=pl.when((pl.col("slot") == "morning") & (pl.col("event_id") == "e2026020053"))
        .then(pl.when(pl.col("side") == "home").then(2.30).otherwise(1.67))
        .otherwise("price_decimal")
    )
    ledger = decided(store, monkeypatch, quotes=quotes)
    inputs = lc.close_inputs(ledger, quotes).sort("game_id")
    assert inputs["game_id"].to_list() == list(pf.GAMES)
    p_b0 = inputs["p_b0"].to_numpy()
    assert np.allclose(inputs["d_b3"], logit(inputs["p_b3"].to_numpy()) - logit(p_b0))
    p_morning = fair_probabilities([[2.30, 1.67]])[0, 0]
    assert inputs["m"][0] == pytest.approx(logit(p_b0[0]) - logit(p_morning))
    assert inputs["m"][1:].to_list() == [0.0, 0.0]
    p_bet365 = fair_probabilities([[2.15, 1.75]])[0, 0]
    assert inputs["g"][0] == pytest.approx(logit(p_bet365) - logit(p_b0[0]))
    # No other book quotes the other games: no gap.
    assert inputs["g"][1:].to_list() == [0.0, 0.0]
    # A pair de-vigging refuses is no price: both sides at plus money leave WSH without a gap,
    # never the day without its inputs.
    refused = pl.concat(
        [quotes, pf.quotes(pf.quote(pf.MIDDAY, 2026020054, "unibet", 2.10, 2.10))]
    ).with_columns(
        price_decimal=pl.when(pl.col("book") == "bet365").then(2.05).otherwise("price_decimal")
    )
    again = lc.close_inputs(ledger, refused).sort("game_id")
    assert again["g"][0] == 0.0 and again["g"][1] == 0.0


def test_close_rows_train_on_earlier_weeks_with_a_close_public_by_the_fold_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nhl_edge.market.devig import fair_probabilities

    store = lb.LocalStore(tmp_path)
    ledger = decided(store, monkeypatch).with_columns(policy_version=pl.lit(POLICY_V2))
    proxy = pf.quotes(
        pf.quote(PROXY, 2026020053, "pinnacle", 1.95, 1.95, slot="pre7"),
        pf.quote(PROXY, 2026020054, "pinnacle", 1.90, 2.00, slot="pre7"),
    )
    odds = lake_odds(pl.concat([pf.day_quotes(), proxy])).drop("game_type")
    # The fixture day's week trains nothing on itself; the next week trains on its games with
    # a proxy, ANA's starting after the pre7 slot without one.
    assert lc.close_rows(ledger, odds, pf.DAY + timedelta(days=1)).is_empty()
    rows = lc.close_rows(ledger, odds, pf.DAY + timedelta(days=7))
    assert rows["game_id"].to_list() == [2026020053, 2026020054]
    p_decision = fair_probabilities([[2.10, 1.80], [1.90, 2.00]])[:, 0]
    p_close = fair_probabilities([[1.95, 1.95], [1.90, 2.00]])[:, 0]
    assert np.allclose(rows["move"], logit(p_close) - logit(p_decision))
    # Without the game column the challengers' reader leaves out, the same rows.
    plain = lc.close_rows(ledger, odds.drop("game_id"), pf.DAY + timedelta(days=7))
    assert plain.equals(rows)
    # A decision published after the week's fold start isn't read.
    late = ledger.with_columns(published_utc=pl.lit(lc.fold_start(pf.DAY + timedelta(days=7))))
    assert lc.close_rows(late, odds, pf.DAY + timedelta(days=7)).is_empty()


def test_close_model_is_unfitted_below_its_minimum(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = lb.LocalStore(tmp_path)
    ledger = decided(store, monkeypatch)
    monkeypatch.setattr(lc, "close_rows", lambda ledger, odds, day: moves(lc.MINIMUM_GAMES - 1))
    lake = Lake(paper_ledger=ledger, odds_snapshots=lake_odds(pf.day_quotes()))
    output = computed(store, ledger, CLOSE_MODEL, lake)
    ChallengerPredictions.validate(output)
    assert output["status"].to_list() == [lc.UNFITTED] * 3
    assert json.loads(output["fit"][0]) == {"week": "2026-10-05", "games": 199, "minimum": 200}
    assert set(json.loads(output["reads"][0])) == {"paper_ledger:20262027", "odds_snapshots"}


def test_close_model_forecasts_the_close_from_the_weeks_fit_and_saves_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = lb.LocalStore(tmp_path)
    ledger = decided(store, monkeypatch)
    training = moves(300)
    monkeypatch.setattr(lc, "close_rows", lambda ledger, odds, day: training)
    lake = Lake(paper_ledger=ledger, odds_snapshots=lake_odds(pf.day_quotes()))
    output = computed(store, ledger, CLOSE_MODEL, lake).sort("game_id")
    assert output["status"].to_list() == [lc.PREDICTED] * 3
    weights, errors, variance = lc.close_fit(lc.close_design(training), training["move"])
    # The forecast close: Pinnacle's decision price moved by the forecast, from the bundle's
    # quotes as the decision saw them.
    inputs = lc.close_inputs(ledger, pf.day_quotes()).sort("game_id")
    expected = sigmoid(logit(inputs["p_b0"].to_numpy()) + lc.close_design(inputs) @ weights)
    assert np.allclose(output["p_home"].to_numpy(), expected, rtol=0, atol=1e-12)
    fit = json.loads(output["fit"][0])
    assert list(fit["weights"]) == ["a", "b_d", "b_m", "b_g"] and fit["games"] == 300
    assert np.allclose(list(fit["standard_errors"].values()), errors, rtol=0, atol=1e-12)
    assert fit["residual_sd"] == pytest.approx(np.sqrt(variance))
    assert fit["train_cutoff"] == training["close_snapshot_utc"].max().isoformat()  # type: ignore[union-attr]
    # A decision published after the latest close is the cutoff instead.
    latest = training["close_snapshot_utc"].max()
    late = training.with_columns(
        published_utc=pl.when(pl.col("close_snapshot_utc") == latest)
        .then(pl.col("close_snapshot_utc") + timedelta(minutes=30))
        .otherwise("published_utc")
    )
    monkeypatch.setattr(lc, "close_rows", lambda ledger, odds, day: late)
    store = lb.LocalStore(tmp_path / "again")
    ledger = decided(store, monkeypatch)
    later = json.loads(computed(store, ledger, CLOSE_MODEL, lake)["fit"][0])
    assert later["train_cutoff"] == (latest + timedelta(minutes=30)).isoformat()  # type: ignore[operator]


def test_close_model_forecasts_the_close_and_is_registered_as_its_adr_says() -> None:
    registered = lc.REGISTRY[5]
    assert (registered.name, registered.version, registered.adr) == ("close_model", "1", "0049")
    assert registered.forecasts == lc.CLOSE and registered.baseline is None
    assert set(registered.reads) == {"paper_ledger", "odds_snapshots"}
    assert all(c.forecasts == lc.RESULT for c in lc.REGISTRY[:5])


def test_every_candidate_names_the_primary_measure_its_adr_fixes() -> None:
    # ADR 0048's family: every registered challenger but the mechanism's control, each tested on
    # the primary measure its ADR fixed (ADR 0038 rule 4), and one the report computes for it.
    primaries = {c.name: c.primary for c in lc.REGISTRY}
    assert primaries == {
        "market_only": None,
        "conservative_blend": "BLEND",  # ADR 0041
        "goalie_spread_blend": "conservative_blend/1",  # ADR 0042
        "team_residual_blend": "conservative_blend/1",  # ADR 0043
        "b3_split": "B3",  # ADR 0044
        "close_model": lc.MOVE,  # ADR 0049
    }
    for challenger in lc.REGISTRY:
        if challenger.primary == lc.MOVE:
            assert challenger.forecasts == lc.CLOSE
        elif challenger.primary not in (None, "BLEND", "B1"):
            assert challenger.primary == challenger.baseline
