"""Point-in-time rules for the backtest's market inputs (ADR 0006). E1 reads each game's own close
at its start. E2 reads only the opener, through assumed_available_at, just after its assumed time
and before the start. B0 reads the outcome only to score a prediction."""

from datetime import UTC, datetime, timedelta

import polars as pl
import pytest
from test_sbr import NEW, NEW_SCHEDULE, results_empty

from nhl_edge.backtest.market import PREDICTION_LAG, Experiment, market_prices
from nhl_edge.backtest.walk_forward import run
from nhl_edge.ingest.sbr import match_season, parse_season
from nhl_edge.lake.schemas import Games, dtypes

SJS_ARI = 2021020010  # a 14:00 EDT start in the fixture schedule


def table(sjs_ari_start: datetime | None = None) -> pl.DataFrame:
    """The 2021-22 fixture page's prices, matched to the fixture schedule."""
    schedule = NEW_SCHEDULE
    if sjs_ari_start is not None:
        moved = pl.col("game_id") == SJS_ARI
        schedule = schedule.with_columns(
            start_utc=pl.when(moved).then(pl.lit(sjs_ari_start)).otherwise(pl.col("start_utc")),
            observed_utc=pl.when(moved)
            .then(pl.lit(sjs_ari_start - timedelta(days=1)))
            .otherwise(pl.col("observed_utc")),
        )
    frame, _ = match_season(parse_season(NEW, 20212022), schedule, results_empty(schedule), "k")
    return frame


def quotes(frame: pl.DataFrame, quote: str) -> dict[int, tuple[float, float]]:
    rows = frame.filter(pl.col("market") == "h2h", pl.col("quote") == quote)
    home = dict(rows.filter(pl.col("side") == "home").select("game_id", "price_decimal").rows())
    away = dict(rows.filter(pl.col("side") == "away").select("game_id", "price_decimal").rows())
    return {game_id: (home[game_id], away[game_id]) for game_id in home.keys() & away.keys()}


def prices(frame: pl.DataFrame) -> dict[int, tuple[float, float]]:
    return {g: (h, a) for g, h, a in frame.select("game_id", "home_price", "away_price").rows()}


def test_e1_reads_each_games_own_close_at_its_start() -> None:
    frame = table()
    e1 = market_prices(frame, Experiment.E1)
    assert (e1["prediction_utc"] == e1["start_utc"]).all()
    assert prices(e1) == quotes(frame, "close")


def test_e2_reads_the_opener_after_its_assumed_time_and_before_the_start() -> None:
    frame = table()
    e2 = market_prices(frame, Experiment.E2)
    assert prices(e2) == quotes(frame, "open")
    assumed = (
        frame.filter(pl.col("quote") == "open")
        .group_by("game_id")
        .agg(pl.col("assumed_available_utc").first())
    )
    joined = e2.join(assumed, on="game_id")
    assert (joined["prediction_utc"] == joined["assumed_available_utc"] + PREDICTION_LAG).all()
    assert (joined["prediction_utc"] < joined["start_utc"]).all()


def test_e2_has_no_price_for_a_game_whose_opener_is_assumed_at_its_start() -> None:
    # A 05:00 EDT start caps the assumed opener at the start, when it can no longer be bet.
    early = datetime(2021, 10, 13, 9, tzinfo=UTC)
    frame = table(early)
    assert SJS_ARI in quotes(frame, "open")
    assert SJS_ARI not in market_prices(frame, Experiment.E2)["game_id"].to_list()


def test_run_refuses_a_held_out_season() -> None:
    # 2022-23 is the market validation season: phase 1 does not inspect it (#10).
    frame = table()
    with pytest.raises(ValueError, match="held out"):
        run(frame, pl.DataFrame(schema=dtypes(Games)), [20212022, 20222023])


def test_b0_reads_the_outcome_only_to_score_it() -> None:
    frame = table()
    games = (
        frame.select("game_id", "season", "game_date", "start_utc", "home", "away")
        .unique("game_id")
        .with_columns(
            venue=pl.lit("x"),
            home_score=pl.lit(3, pl.Int16),
            away_score=pl.lit(1, pl.Int16),
            decided_in=pl.lit("REG"),
            neutral_site=pl.lit(False),
            limited_attendance=pl.lit(False),
            observed_utc=pl.col("start_utc") + timedelta(days=1),
            raw_key=pl.lit("k"),
        )
        .select(list(dtypes(Games)))
    )
    flipped = games.with_columns(home_score=pl.lit(1, pl.Int16), away_score=pl.lit(3, pl.Int16))
    first, _ = run(frame, games, [20212022])
    second, _ = run(frame, flipped, [20212022])
    keys = ["experiment", "method", "game_id"]
    assert first.sort(keys)["p_home"].to_list() == second.sort(keys)["p_home"].to_list()
    assert set(first["home_win"]) == {1}
    assert set(second["home_win"]) == {0}
