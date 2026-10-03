import csv
import json
import math
import subprocess
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import market_history
import polars as pl
import pytest
from b2_fixtures import feature_tables
from b3_fixtures import player_tables
from test_sbr import NEW, NEW_SCHEDULE, results_empty
from typer.testing import CliRunner

from nhl_edge.backtest import reports
from nhl_edge.backtest.metrics import bootstrap, log_loss
from nhl_edge.backtest.walk_forward import Coverage, b0, outcomes, run
from nhl_edge.cli import app
from nhl_edge.game import b2, b3
from nhl_edge.ingest.games import EXPECTED_GAMES
from nhl_edge.ingest.sbr import match_season, parse_season
from nhl_edge.lake.schemas import Games, SbrOdds, dtypes
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

# A synthetic 2020-21 for B1 to fit on before the 2021-22 fixture season.
HISTORY_ODDS, HISTORY_GAMES = market_history.seasons([20202021], games=40)


def run_2021(
    odds: pl.DataFrame | None = None, games: pl.DataFrame | None = None
) -> tuple[pl.DataFrame, Coverage]:
    """run on the 2021-22 fixture page, with the synthetic 2020-21 before it."""
    odds = sbr_odds_2021() if odds is None else odds
    games = games_of(NEW_SCHEDULE, SCORES) if games is None else games
    predictions, coverage, _ = run(
        pl.concat([HISTORY_ODDS, odds.select(list(dtypes(SbrOdds)))]),
        pl.concat([HISTORY_GAMES, games]),
        [20212022],
    )
    return predictions, coverage


def test_the_moneyline_settles_on_the_full_game() -> None:
    results = dict(outcomes(games_of(NEW_SCHEDULE, SCORES)).select("game_id", "home_win").rows())
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


def test_run_scores_b0_and_b1_for_both_experiments() -> None:
    predictions, coverage = run_2021()
    assert set(predictions["experiment"]) == {"E1", "E2"}
    assert set(predictions["season"]) == {20212022}
    b0_rows = predictions.filter(pl.col("model") == "B0")
    assert set(b0_rows["method"]) == {m.value for m in Method}
    counts = b0_rows.group_by("experiment", "method").len()
    assert set(counts["len"]) == {coverage["E1"][20212022]["priced"]}
    b1_rows = predictions.filter(pl.col("model") == "B1")
    assert set(b1_rows["method"]) == {"multiplicative"}
    assert b1_rows.group_by("experiment").len()["len"].to_list() == [3, 3]
    # BOS and DAL (2021020030) are not on the fixture page, so they have no price.
    counts = {
        "games": 4,
        "priced": 3,
        "unsettled": 0,
        "refused": 0,
        "scored": 3,
        "b1_trained_on": 40,
    }
    assert coverage["E1"][20212022] == counts
    assert coverage["E2"][20212022] == {**counts, "implausible": 0, "opener_differs_from_close": 3}


def test_a_season_asked_for_twice_is_predicted_once() -> None:
    odds = pl.concat([HISTORY_ODDS, sbr_odds_2021().select(list(dtypes(SbrOdds)))])
    games = pl.concat([HISTORY_GAMES, games_of(NEW_SCHEDULE, SCORES)])
    once, coverage, _ = run(odds, games, [20212022])
    twice, again, _ = run(odds, games, [20212022, 20212022])
    assert twice.equals(once)
    assert again == coverage


def test_a_priced_game_without_a_result_is_counted_and_not_scored() -> None:
    games = games_of(NEW_SCHEDULE, SCORES).filter(pl.col("game_id") != 2021020001)
    predictions, coverage = run_2021(games=games)
    assert coverage["E1"][20212022] == {
        "games": 3,
        "priced": 3,
        "unsettled": 1,
        "refused": 0,
        "scored": 2,
        "b1_trained_on": 40,
    }
    assert 2021020001 not in predictions["game_id"].to_list()


def test_the_summary_pairs_each_model_with_b1_and_each_method_with_multiplicative(
    tmp_path: Path,
) -> None:
    odds = pl.concat([HISTORY_ODDS, sbr_odds_2021().select(list(dtypes(SbrOdds)))])
    games = pl.concat([HISTORY_GAMES, games_of(NEW_SCHEDULE, SCORES)])
    predictions, coverage, fits = run(odds, games, [20212022])
    report = reports.summary(predictions, coverage, fits, [20212022], "backtest-20260929-abc", NOW)
    b0_e1 = report["experiments"]["E1"]["models"]["B0"]
    assert set(b0_e1["log_loss"]) == {"multiplicative", "power", "shin"}
    assert set(b0_e1["paired_against_multiplicative"]) == {"power", "shin"}
    assert set(b0_e1["paired_against_B1"]) == {"multiplicative", "power", "shin"}
    pooled = b0_e1["log_loss"]["power"]["pooled"]
    assert pooled["games"] == 3
    assert pooled["low"] <= pooled["mean"] <= pooled["high"]
    # B0 against B1: per game, B0's log loss minus B1's.
    b1 = predictions.filter(pl.col("experiment") == "E1", pl.col("model") == "B1")
    b0 = predictions.filter(
        pl.col("experiment") == "E1", pl.col("model") == "B0", pl.col("method") == "multiplicative"
    )
    against = b0_e1["paired_against_B1"]["multiplicative"]["pooled"]
    difference = b0["log_loss"].to_numpy().mean() - b1["log_loss"].to_numpy().mean()
    assert against["mean"] == pytest.approx(difference)
    b1_e1 = report["experiments"]["E1"]["models"]["B1"]
    assert set(b1_e1) == {"log_loss", "fits"}
    fit = b1_e1["fits"]["20212022"]
    assert fit["games"] == 40
    cutoff = HISTORY_GAMES["observed_utc"].max()
    assert isinstance(cutoff, datetime)
    assert fit["train_cutoff"] == cutoff.isoformat()
    assert report["train_cutoff"] == {"20212022": cutoff.isoformat()}
    assert "10:00 US Eastern" in report["experiments"]["E2"]["market"]
    later = report["e2_against_e1"]["B0"]["multiplicative"]["pooled"]
    assert later["games"] == 3
    b0_multiplicative = (pl.col("model") == "B0") & (pl.col("method") == "multiplicative")
    e1 = predictions.filter(pl.col("experiment") == "E1", b0_multiplicative)
    e2 = predictions.filter(pl.col("experiment") == "E2", b0_multiplicative)
    difference = e2["log_loss"].to_numpy().mean() - e1["log_loss"].to_numpy().mean()
    assert later["mean"] == pytest.approx(difference)
    reports.write(report, tmp_path)
    reports.write(report, tmp_path)
    assert json.loads((tmp_path / "summary.json").read_text())["version"] == report["version"]
    rows = list(csv.DictReader((tmp_path / "runs.csv").open()))
    assert len(rows) == 16  # two runs of two experiments by B0's three methods and B1
    assert rows[0]["seasons"] == "20212022"
    b1_rows = [row for row in rows if row["model"] == "B1"]
    assert {row["train_cutoff"] for row in b1_rows} == {cutoff.isoformat()}
    assert {row["train_cutoff"] for row in rows if row["model"] == "B0"} == {""}


def test_runs_csv_keeps_earlier_rows_when_its_columns_change(tmp_path: Path) -> None:
    old = "run_utc,version,seasons,experiment,model,method,games,log_loss,low,high\n"
    (tmp_path / "runs.csv").write_text(old + "t,backtest-1,20212022,E1,B0,shin,3,0.6,0.5,0.7\n")
    predictions, coverage, fits = run(
        pl.concat([HISTORY_ODDS, sbr_odds_2021().select(list(dtypes(SbrOdds)))]),
        pl.concat([HISTORY_GAMES, games_of(NEW_SCHEDULE, SCORES)]),
        [20212022],
    )
    reports.write(
        reports.summary(predictions, coverage, fits, [20212022], "backtest-2", NOW), tmp_path
    )
    rows = list(csv.DictReader((tmp_path / "runs.csv").open()))
    assert list(rows[0]) == reports.RUNS_FIELDS
    assert (rows[0]["version"], rows[0]["train_cutoff"]) == ("backtest-1", "")
    assert len(rows) == 1 + 8
    # An earlier header with no rows is rewritten too.
    (tmp_path / "runs.csv").write_text(old)
    reports.write(
        reports.summary(predictions, coverage, fits, [20212022], "backtest-3", NOW), tmp_path
    )
    header = (tmp_path / "runs.csv").read_text().splitlines()[0]
    assert header.split(",") == reports.RUNS_FIELDS
    assert len(list(csv.DictReader((tmp_path / "runs.csv").open()))) == 8
    # A column that is gone is dropped from the earlier rows, which are kept.
    dropped = "run_utc,version,retired\nt,backtest-0,x\n"
    (tmp_path / "runs.csv").write_text(dropped)
    reports.write(
        reports.summary(predictions, coverage, fits, [20212022], "backtest-4", NOW), tmp_path
    )
    rows = list(csv.DictReader((tmp_path / "runs.csv").open()))
    assert list(rows[0]) == reports.RUNS_FIELDS
    assert (rows[0]["version"], rows[0]["model"]) == ("backtest-0", "")
    assert len(rows) == 1 + 8


def test_an_experiment_with_no_scored_game_keeps_its_coverage() -> None:
    predictions, coverage, fits = run(
        pl.concat([HISTORY_ODDS, sbr_odds_2021().select(list(dtypes(SbrOdds)))]),
        pl.concat([HISTORY_GAMES, games_of(NEW_SCHEDULE, SCORES)]),
        [20212022],
    )
    e1_only = predictions.filter(pl.col("experiment") == "E1")
    report = reports.summary(e1_only, coverage, fits, [20212022], "backtest-20260929-abc", NOW)
    assert report["experiments"]["E2"]["models"] == {}
    assert report["experiments"]["E2"]["coverage"]["20212022"]["priced"] == 3


def test_the_version_names_the_component_date_and_commit(tmp_path: Path) -> None:
    assert reports.version("backtest", NOW, cwd=tmp_path) == "backtest-20260929-nogit"


def test_the_version_says_when_the_code_has_uncommitted_changes(tmp_path: Path) -> None:
    def git(*args: str) -> str:
        done = subprocess.run(["git", *args], cwd=tmp_path, capture_output=True, text=True)
        return done.stdout.strip()

    git("init", "-q")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "model.py").write_text("SLOPE = 1\n")
    git("add", ".")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "m")
    sha = git("rev-parse", "--short", "HEAD")
    # A report written outside the code leaves the version clean.
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "runs.csv").write_text("x\n")
    assert reports.version("backtest", NOW, cwd=tmp_path) == f"backtest-20260929-{sha}"
    (tmp_path / "src" / "model.py").write_text("SLOPE = 2\n")
    assert reports.version("backtest", NOW, cwd=tmp_path) == f"backtest-20260929-{sha}-dirty"


runner = CliRunner()


def test_backtest_refuses_held_out_seasons(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    for season in ("20222023", "20232024", "20242025", "20252026", "20262027"):
        result = runner.invoke(app, ["backtest", "--seasons", season])
        assert result.exit_code == 2, result.output
        assert "held out" in result.output
    # The hockey-only mode opens the hockey validation seasons at gate 2, and only those.
    for season in ("20222023", "20252026", "20262027"):
        result = runner.invoke(app, ["backtest", "--seasons", season, "--hockey-only"])
        assert result.exit_code == 2, result.output
        assert "held out" in result.output
    for season in ("20232024", "20242025"):
        result = runner.invoke(app, ["backtest", "--seasons", season, "--hockey-only"])
        assert "held out" not in result.output
        assert "run the feature commands" in result.output  # past the gate, at the input check


def test_the_one_time_test_runs_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from test_one_time import ConditionalBucket

    from nhl_edge.backtest import one_time

    monkeypatch.chdir(tmp_path)
    bucket = ConditionalBucket()
    monkeypatch.setattr(
        one_time,
        "places",
        lambda lake_dir, report_dirs: one_time.Places(lake_dir, report_dirs, bucket, "b"),
    )
    once = ["backtest", "--seasons", "20252026", "--hockey-only", "--one-time-test"]
    refused = {
        "needs --hockey-only": ["backtest", "--seasons", "20252026", "--one-time-test"],
        "scores [20252026] alone": [
            "backtest",
            "--seasons",
            "20232024,20252026",
            "--hockey-only",
            "--one-time-test",
        ],
    }
    for message, args in refused.items():
        result = runner.invoke(app, args)
        assert result.exit_code == 2 and message in result.output, result.output
    # Not yet run: past the gate, to the empty lake's input check, which stops it before the
    # claim.
    first = runner.invoke(app, once)
    assert "held out" not in first.output and "already ran" not in first.output
    assert "run the feature commands" in first.output and not bucket.objects
    # Claimed on another machine: refused here, whatever the report directory.
    bucket.put_object(Bucket="b", Key=one_time.R2_KEY, Body=b"backtest-hockey-abc then\n")
    again = runner.invoke(app, [*once, "--out", "elsewhere"])
    flat = " ".join(again.output.replace("│", " ").split())  # the error box wraps lines
    assert again.exit_code == 2 and "already ran" in flat and "backtest-hockey-abc" in flat


def test_backtest_refuses_the_first_sbr_season(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["backtest", "--seasons", "20102011,20182019"])
    assert result.exit_code == 2, result.output
    assert "[20102011] have no earlier SBR season" in result.output


def test_backtest_writes_the_summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    missing = runner.invoke(app, ["backtest", "--seasons", "20212022"])
    assert missing.exit_code == 1
    assert "run nhl odds sbr" in missing.output
    lake = Lake()
    lake.write("sbr_odds", sbr_odds_2021())
    lake.write("games", games_of(NEW_SCHEDULE, SCORES))
    # B1 is fitted on every earlier season, so each needs its prices.
    history = runner.invoke(app, ["backtest", "--seasons", "20212022"])
    assert history.exit_code == 1
    assert "no SBR prices for [20102011," in history.output
    earlier = [s for s in range(20102011, 20212022, 10001)]
    odds, games = market_history.seasons(earlier, games=20)
    odds = market_history.implausible_opener(odds, 2010020001)
    lake.write("sbr_odds", odds)
    lake.write("games", games)
    short = runner.invoke(app, ["backtest", "--seasons", "20212022"])
    assert short.exit_code == 1
    assert "games has 20 of 1,230 in 20102011" in short.output
    for season in earlier:
        monkeypatch.setitem(EXPECTED_GAMES, season, 20)
    monkeypatch.setitem(EXPECTED_GAMES, 20212022, 4)
    # B2 needs its feature tables (ADR 0013).
    no_features = runner.invoke(app, ["backtest", "--seasons", "20212022"])
    assert no_features.exit_code == 1
    assert "games without team_strength" in no_features.output
    tables = feature_tables(lake.read("games"))
    stored = Lake.read
    monkeypatch.setattr(
        Lake,
        "read",
        lambda self, name, seasons=None: (
            getattr(tables, name)
            if name in b2.TABLES or name == "actual_lineups"
            else stored(self, name, seasons)
        ),
    )
    # B3 needs the player layer's tables (ADR 0023).
    no_players = runner.invoke(app, ["backtest", "--seasons", "20212022"])
    assert no_players.exit_code == 1
    assert "games without lineups" in no_players.output
    players = player_tables(lake.read("games"), tables)
    monkeypatch.setattr(
        Lake,
        "read",
        lambda self, name, seasons=None: (
            getattr(tables, name)
            if name in b2.TABLES or name == "actual_lineups"
            else getattr(players, name)
            if name in b3.TABLES or name == "lineup_replacements"
            else stored(self, name, seasons)
        ),
    )
    result = runner.invoke(app, ["backtest", "--seasons", "20212022", "--out", "out"])
    assert result.exit_code == 0, result.output
    assert "E1 B2 none: log loss" in result.output
    assert "E1 B0 multiplicative: log loss" in result.output
    assert "E2 B1 multiplicative: log loss" in result.output
    assert "E2 with_every_opener B1 multiplicative: log loss" in result.output
    summary = json.loads((tmp_path / "out" / "summary.json").read_text())
    assert summary["seasons"] == [20212022]
    # E2 refuses the implausible 2010-11 opener in B1's fit; E2 on every opener keeps it.
    e2 = summary["experiments"]["E2"]["coverage"]["20212022"]
    every = summary["sensitivity"]["with_every_opener"]["E2"]["coverage"]["20212022"]
    assert (e2["priced"], every["priced"]) == (3, 3)
    assert e2["b1_trained_on"] == every["b1_trained_on"] - 1
    # B2 scores the games B1 scores, trained on every earlier game from 2011-12.
    assert e2["b2_scored"] == e2["scored"]
    assert e2["b2_trained_on"] == 20 * len([s for s in earlier if s >= b2.FIRST_SEASON])
    assert "gaps_over_8_points" in summary["experiments"]["E1"]["models"]["B2"]
    assert (tmp_path / "out" / "gaps.csv").exists()
    assert "goalie_starts" in summary["experiments"]["E2"]["models"]["B2"]["lineup_quality"]
    # B3 beside B2, on the same games and training games, with its own gaps file.
    assert "E1 B3 none: log loss" in result.output
    assert e2["b3_scored"] == e2["scored"] and e2["b3_trained_on"] == e2["b2_trained_on"]
    b3_model = summary["experiments"]["E2"]["models"]["B3"]
    assert set(b3_model) >= {"paired_against_B2", "gate_2_subsets", "lineup_quality"}
    assert (tmp_path / "out" / "gaps_b3.csv").exists()
    # The hockey-only mode scores B2 and B3 on every game at the as-of time.
    hockey = runner.invoke(
        app, ["backtest", "--seasons", "20212022", "--hockey-only", "--out", "hockey"]
    )
    assert hockey.exit_code == 0, hockey.output
    (written_path,) = (tmp_path / "hockey").glob("hockey-*.json")
    written = json.loads(written_path.read_text())
    assert written_path.name == f"{written['version'].replace('backtest-hockey', 'hockey')}.json"
    assert set(written["models"]) == {"B2", "B3"}
    assert written["coverage"]["20212022"]["b3_scored"] == 4
    assert (
        set(written["models"]["B2"]["fits"]) == set(written["models"]["B3"]["fits"]) == {"20212022"}
    )
    # Every hockey-only run is logged, one row per model.
    with (tmp_path / "hockey" / "runs.csv").open(newline="") as handle:
        logged = list(csv.DictReader(handle))
    assert [(r["experiment"], r["model"], r["games"]) for r in logged] == [
        ("hockey", "B2", "4"),
        ("hockey", "B3", "4"),
    ]
    assert all(r["version"] == written["version"] and r["train_cutoff"] for r in logged)


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
    predictions, coverage = run_2021(odds)
    assert coverage["E2"][20212022]["refused"] == 1
    assert coverage["E2"][20212022]["scored"] == 2
    e2 = predictions.filter(pl.col("experiment") == "E2")
    assert 2021020001 not in e2["game_id"].to_list()
    assert e2.height == 2 * (len(Method) + 1)  # B0 under each method, and B1


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
    _, coverage = run_2021(odds)
    assert coverage["E2"][20212022]["opener_differs_from_close"] == 2
