"""scripts/yardstick_diagnostics.py (#361 step 2): the game numbers, the gap between results and
expected goals as known before the opener, the goalie surprises, the subsets, and the report, on
synthetic training seasons."""

import importlib.util
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from types import ModuleType

import polars as pl
import pytest
import yardstick_fixtures as yf

from nhl_edge.backtest import yardstick as ys

UTC_TYPE = pl.Datetime("us", "UTC")


def _load() -> ModuleType:
    path = Path(__file__).parents[2] / "scripts" / "yardstick_diagnostics.py"
    spec = importlib.util.spec_from_file_location("yardstick_diagnostics", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


yd = _load()


def games(rows: list[tuple[int, int, str, str, int, int]]) -> pl.DataFrame:
    """(game_id, day of October, home, away, home score, away score) games of 2013-14."""
    return pl.DataFrame(
        {
            "game_id": [r[0] for r in rows],
            "season": [20132014] * len(rows),
            "start_utc": [datetime.combine(date(2013, 10, r[1]), time(23), UTC) for r in rows],
            "home": [r[2] for r in rows],
            "away": [r[3] for r in rows],
            "home_score": [r[4] for r in rows],
            "away_score": [r[5] for r in rows],
        }
    ).with_columns(pl.col("season").cast(pl.Int32), pl.col("start_utc").cast(UTC_TYPE))


GAMES = games(
    [
        (1, 3, "BOS", "TOR", 5, 1),  # BOS beats its expected difference of 0 by 4
        (2, 5, "TOR", "MTL", 2, 2),
        (3, 6, "BOS", "MTL", 1, 1),
    ]
)


def frame_of(played: pl.DataFrame) -> pl.DataFrame:
    """The yardstick's rows for the games: no expected difference, the opener at 14:00 UTC on the
    game day and the result public at 10:00 UTC the next."""
    day = pl.col("start_utc").dt.date()
    return played.select(
        "game_id",
        "season",
        "start_utc",
        delta_g_hat=pl.lit(0.0),
        prediction_utc=day.dt.combine(time(14)).dt.replace_time_zone("UTC"),
        result_utc=(day + timedelta(days=1)).dt.combine(time(10)).dt.replace_time_zone("UTC"),
    )


def test_each_teams_game_number_counts_its_own_games() -> None:
    found = yd.game_numbers(GAMES).sort("game_id")
    assert found.rows() == [(1, 1, 1), (2, 2, 1), (3, 2, 2)]


def test_the_gap_reads_only_results_public_before_the_opener() -> None:
    found = dict(yd.luck_gaps(frame_of(GAMES), GAMES).iter_rows())
    # Game 2: TOR, at home, is 4 a game behind its expected goals; MTL has no game yet.
    # Game 3: BOS is 4 a game ahead; MTL drew level with its expectation.
    assert found == {1: 0.0, 2: pytest.approx(-4.0), 3: pytest.approx(4.0)}
    # A result public only after game 3's opener is not read.
    late = frame_of(GAMES).with_columns(
        result_utc=pl.when(pl.col("game_id") == 1)
        .then(pl.lit(datetime(2013, 10, 7, tzinfo=UTC)).cast(UTC_TYPE))
        .otherwise(pl.col("result_utc"))
    )
    assert dict(yd.luck_gaps(late, GAMES).iter_rows())[3] == 0.0


def test_the_candidates_drop_one_part_at_a_time() -> None:
    found = yd.candidates()
    assert set(yd.SETS) <= set(found)
    for part in ys.CANDIDATES["all"]:
        assert part not in found[f"all less {part}"]
        assert len(found[f"all less {part}"]) == len(ys.CANDIDATES["all"]) - 1


def test_the_subsets_and_the_report_on_synthetic_seasons() -> None:
    teams, players, sbr = yf.league()
    teams, players = ys.through(teams, 20142015), ys.through(players, 20142015)
    market = ys.market(sbr, players.games)
    frame = ys.rows(
        market,
        ys.features(teams, players, market.select("game_id", "prediction_utc")),
        players.games,
    )
    read = ys.Read(players.games, teams, players, frame)
    flags = yd.subsets(read)
    assert flags.height == frame.filter(pl.col("season").is_in(list(ys.EXPLORE))).height
    assert set(yd.SUBSETS) <= set(flags.columns)
    # The back-to-back games are the rested games' complement.
    rested = frame.select("game_id", "rested")
    joined = flags.join(rested, on="game_id")
    assert (joined["back-to-back"] == ~joined["rested"]).all()
    surprised = yd.surprises(read)
    assert surprised.height == frame.height
    per_game, _ = ys.run(frame, "explore", yd.candidates())
    report = yd.markdown(yd.parts(per_game), yd.where(per_game, flags), "v")
    assert "| finishing_diff |" in report and "| not early season |" in report
