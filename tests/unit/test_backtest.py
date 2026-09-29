import csv
import json
import math
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest
from test_sbr import NEW, NEW_SCHEDULE, results_empty
from typer.testing import CliRunner

from nhl_edge.backtest import reports
from nhl_edge.backtest.metrics import bootstrap, log_loss
from nhl_edge.backtest.walk_forward import b0, outcomes, run
from nhl_edge.cli import app
from nhl_edge.ingest.sbr import match_season, parse_season
from nhl_edge.lake.schemas import Games, dtypes
from nhl_edge.lake.tables import Lake
from nhl_edge.market.devig import Method

NOW = datetime(2026, 9, 29, 12, tzinfo=UTC)


def games_of(schedule: pl.DataFrame, scores: dict[int, tuple[int, int, str]]) -> pl.DataFrame:
    """Final games for the schedule's games, as (home score, away score, decided in)."""
    frame = pl.DataFrame(
        [(g, h, a, d) for g, (h, a, d) in scores.items()],
        schema={
            "game_id": pl.Int64,
            "home_score": pl.Int16,
            "away_score": pl.Int16,
            "decided_in": pl.String,
        },
        orient="row",
    )
    return (
        schedule.join(frame, on="game_id")
        .with_columns(observed_utc=pl.col("start_utc") + timedelta(days=1))
        .select(list(dtypes(Games)))
    )


def sbr_odds_2021() -> pl.DataFrame:
    frame, _ = match_season(
        parse_season(NEW, 20212022), NEW_SCHEDULE, results_empty(NEW_SCHEDULE), "sbr/k"
    )
    return frame


SCORES = {
    2021020001: (2, 1, "REG"),
    2021020010: (3, 4, "OT"),
    2021020020: (4, 3, "SO"),  # the shootout winner's goal counts
    2021020030: (1, 5, "REG"),
}


def test_the_moneyline_settles_on_the_full_game() -> None:
    results = dict(outcomes(games_of(NEW_SCHEDULE, SCORES)).rows())
    assert results == {2021020001: 1, 2021020010: 0, 2021020020: 1, 2021020030: 0}


def test_log_loss_per_game() -> None:
    frame = pl.DataFrame({"p": [0.5, 0.8, 0.8], "y": [1, 1, 0]})
    losses = frame.select(loss=log_loss(pl.col("p"), pl.col("y")))["loss"].to_list()
    assert losses == pytest.approx([math.log(2), -math.log(0.8), -math.log(0.2)])


def test_b0_de_vigs_each_market_and_drops_those_below_100_percent() -> None:
    prices = pl.DataFrame(
        {"game_id": [1, 2], "home_price": [1.909090909, 2.65], "away_price": [1.909090909, 2.62]}
    )
    fair = b0(prices, Method.MULTIPLICATIVE)
    assert fair["game_id"].to_list() == [1]
    assert fair["p_home"].to_list() == pytest.approx([0.5])


def week_frame(values: dict[tuple[int, date], list[float]]) -> pl.DataFrame:
    return pl.DataFrame(
        [
            {"season": season, "game_date": day, "value": value}
            for (season, day), vals in values.items()
            for value in vals
        ]
    )


def test_the_bootstrap_resamples_whole_weeks_within_each_season() -> None:
    # One week per season: every draw is the same games, so the interval is the mean.
    frame = week_frame(
        {(20182019, date(2018, 10, 8)): [1.0, 1.0], (20212022, date(2021, 10, 11)): [0.0]}
    )
    estimate = bootstrap(frame, "value")
    assert (estimate.mean, estimate.low, estimate.high) == pytest.approx((2 / 3, 2 / 3, 2 / 3))
    assert (estimate.games, estimate.weeks) == (3, 2)


def test_the_bootstrap_interval_covers_the_mean_and_is_repeatable() -> None:
    # Monday 2018-10-08 and Sunday 2018-10-14 are one week; 2018-10-15 starts the next.
    frame = week_frame(
        {
            (20182019, date(2018, 10, 8)): [0.2],
            (20182019, date(2018, 10, 14)): [0.4],
            (20182019, date(2018, 10, 15)): [0.9],
            (20182019, date(2018, 10, 22)): [0.6, 0.7],
        }
    )
    estimate = bootstrap(frame, "value")
    assert estimate.weeks == 3
    assert estimate.low < estimate.mean < estimate.high
    assert bootstrap(frame, "value") == estimate


def test_the_bootstrap_needs_games() -> None:
    with pytest.raises(ValueError, match="no games"):
        empty = pl.DataFrame(schema={"season": pl.Int32, "game_date": pl.Date, "value": pl.Float64})
        bootstrap(empty, "value")


def test_run_scores_b0_for_both_experiments_and_every_method() -> None:
    predictions, coverage = run(sbr_odds_2021(), games_of(NEW_SCHEDULE, SCORES), [20212022])
    assert set(predictions["experiment"]) == {"E1", "E2"}
    assert set(predictions["method"]) == {m.value for m in Method}
    counts = predictions.group_by("experiment", "method").len()
    assert set(counts["len"]) == {coverage["E1"][20212022]["priced"]}
    # BOS and DAL (2021020030) are not on the fixture page, so they have no price.
    assert coverage["E1"][20212022] == {"games": 4, "priced": 3, "refused": 0, "scored": 3}
    assert coverage["E2"][20212022] == {
        "games": 4,
        "priced": 3,
        "refused": 0,
        "scored": 3,
        "opener_differs_from_close": 3,
    }


def test_the_summary_pairs_each_method_with_the_multiplicative_one(tmp_path: Path) -> None:
    predictions, coverage = run(sbr_odds_2021(), games_of(NEW_SCHEDULE, SCORES), [20212022])
    report = reports.summary(predictions, coverage, [20212022], "backtest-20260929-abc", NOW)
    b0_e1 = report["experiments"]["E1"]["models"]["B0"]
    assert set(b0_e1["log_loss"]) == {"multiplicative", "power", "shin"}
    assert set(b0_e1["paired_against_multiplicative"]) == {"power", "shin"}
    pooled = b0_e1["log_loss"]["power"]["pooled"]
    assert pooled["games"] == 3
    assert pooled["low"] <= pooled["mean"] <= pooled["high"]
    assert report["train_cutoff"] is None
    assert "10:00 US Eastern" in report["experiments"]["E2"]["market"]
    later = report["e2_against_e1"]["B0"]["multiplicative"]["pooled"]
    assert later["games"] == 3
    e1 = predictions.filter(pl.col("experiment") == "E1", pl.col("method") == "multiplicative")
    e2 = predictions.filter(pl.col("experiment") == "E2", pl.col("method") == "multiplicative")
    assert later["mean"] == pytest.approx(e2["log_loss"].mean() - e1["log_loss"].mean())
    reports.write(report, tmp_path)
    reports.write(report, tmp_path)
    assert json.loads((tmp_path / "summary.json").read_text())["version"] == report["version"]
    rows = list(csv.DictReader((tmp_path / "runs.csv").open()))
    assert len(rows) == 12  # two runs of two experiments by three methods, one header
    assert rows[0]["seasons"] == "20212022"


def test_the_version_names_the_component_date_and_commit(tmp_path: Path) -> None:
    assert reports.version("backtest", NOW, cwd=tmp_path) == "backtest-20260929-nogit"


runner = CliRunner()


def test_backtest_refuses_held_out_seasons(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    for season in ("20222023", "20252026", "20262027"):
        result = runner.invoke(app, ["backtest", "--seasons", season])
        assert result.exit_code == 2, result.output
        assert "held out" in result.output


def test_backtest_writes_the_summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    missing = runner.invoke(app, ["backtest", "--seasons", "20212022"])
    assert missing.exit_code == 1
    assert "run nhl odds sbr" in missing.output
    lake = Lake()
    lake.write("sbr_odds", sbr_odds_2021())
    lake.write("games", games_of(NEW_SCHEDULE, SCORES))
    result = runner.invoke(app, ["backtest", "--seasons", "20212022", "--out", "out"])
    assert result.exit_code == 0, result.output
    assert "E1 B0 multiplicative: log loss" in result.output
    summary = json.loads((tmp_path / "out" / "summary.json").read_text())
    assert summary["seasons"] == [20212022]
    assert summary["experiments"]["E2"]["coverage"]["20212022"]["priced"] == 3


def test_a_market_below_100_percent_is_counted_and_left_out() -> None:
    # Both sides of 2021020001's opener at plus money, as 2021020945's in the archive.
    opener = (
        (pl.col("game_id") == 2021020001)
        & (pl.col("market") == "h2h")
        & (pl.col("quote") == "open")
    )
    odds = sbr_odds_2021().with_columns(
        price_decimal=pl.when(opener).then(2.65).otherwise(pl.col("price_decimal"))
    )
    predictions, coverage = run(odds, games_of(NEW_SCHEDULE, SCORES), [20212022])
    assert coverage["E2"][20212022]["refused"] == 1
    assert coverage["E2"][20212022]["scored"] == 2
    e2 = predictions.filter(pl.col("experiment") == "E2")
    assert 2021020001 not in e2["game_id"].to_list()
    assert e2.height == 2 * len(Method)


def test_coverage_counts_the_openers_that_differ_from_the_close() -> None:
    # 2021020001's opener set to its close: of the three priced games, two openers moved.
    game = (pl.col("game_id") == 2021020001) & (pl.col("market") == "h2h")
    frame = sbr_odds_2021()
    close = frame.filter(game, pl.col("quote") == "close").select("side", close="price_decimal")
    odds = (
        frame.join(close, on="side", how="left")
        .with_columns(
            price_decimal=pl.when(game & (pl.col("quote") == "open"))
            .then(pl.col("close"))
            .otherwise(pl.col("price_decimal"))
        )
        .drop("close")
    )
    _, coverage = run(odds, games_of(NEW_SCHEDULE, SCORES), [20212022])
    assert coverage["E2"][20212022]["opener_differs_from_close"] == 2
