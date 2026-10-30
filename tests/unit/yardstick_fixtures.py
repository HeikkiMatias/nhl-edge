"""Synthetic training seasons for the yardstick's tests (#361): B2's and B3's fixture tables
(b2_fixtures, b3_fixtures) stamped as the lake stamps them, SBR's openers and closes for the same
games (market_history), and frames of made-up moves for the measures."""

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta

import b2_fixtures as b2f
import b3_fixtures as b3f
import market_history as mh
import numpy as np
import polars as pl

from nhl_edge.features import team_strength as ts
from nhl_edge.game import b2, b3

SEASONS = (20112012, 20122013, 20132014, 20142015)
GAMES = 48


def lake_stamped(tables: b3.Tables) -> b3.Tables:
    """B3's fixture tables stamped as the lake stamps them: the projected lineups at their game's
    as-of time (their fits are of earlier seasons), and RAPM's terms with their date's as-of time
    and observed from the tuning cutoff (ADR 0011)."""
    as_of = tables.games.select(
        "game_id", observed_utc=ts.as_of(pl.col("game_date"), pl.col("start_utc"))
    )
    by_date = tables.games.group_by("game_date").agg(
        as_of_utc=ts.as_of(pl.col("game_date"), pl.col("start_utc")).min()
    )

    def at_as_of(frame: pl.DataFrame) -> pl.DataFrame:
        return frame.drop("observed_utc").join(as_of, on="game_id").select(frame.columns)

    return replace(
        tables,
        lineups=at_as_of(tables.lineups),
        lineup_replacements=at_as_of(tables.lineup_replacements),
        rapm_terms=tables.rapm_terms.join(by_date, on="game_date"),
    )


def league(
    seasons: tuple[int, ...] = SEASONS, games: int = GAMES, seed: int = 5
) -> tuple[b2.Tables, b3.Tables, pl.DataFrame]:
    """B2's and B3's tables for the seasons' games, as the lake stamps them, and SBR's openers
    and closes for the same games and a season before, whose closes bound the first openers."""
    players = lake_stamped(b3f.league(seasons, games, seed))
    teams = replace(
        b2f.feature_tables(players.games, seed),
        schedule_terms=players.schedule_terms,
        goalie_starts=players.goalie_starts,
        actual_lineups=players.actual_lineups,
    )
    before = seasons[0] - 10_001
    sbr, _ = mh.seasons([before, *seasons], games)
    return teams, players, sbr


def moves(
    seasons: tuple[int, ...] = SEASONS, games: int = 200, seed: int = 9, weight: float = 0.5
) -> pl.DataFrame:
    """Made-up yardstick rows: the move from the opener follows x by weight with noise, z is
    noise, and the home team wins with probability sigmoid(c + w), w a feature the close misses.
    Every game's result is public the next morning."""
    rng = np.random.default_rng(seed)
    rows = []
    for season in seasons:
        year = season // 10_000
        for i in range(games):
            day = date(year, 10, 10) + timedelta(days=i // 4)
            start = datetime.combine(day, time(23), UTC)
            x, z, w = rng.normal(0, 0.1, size=3)
            o = float(rng.normal(0.2, 0.3))
            y = weight * x + float(rng.normal(0, 0.05))
            c = o + y
            rows.append(
                {
                    "game_id": year * 1_000_000 + 20_000 + i + 1,
                    "season": season,
                    "game_date": day,
                    "start_utc": start,
                    "o": o,
                    "c": c,
                    "y": y,
                    "x": x,
                    "z": z,
                    "w": w,
                    "home_win": int(rng.random() < 1 / (1 + np.exp(-(c + 10 * w)))),
                    "result_utc": datetime.combine(day + timedelta(days=1), time(10), UTC),
                    "rested": i % 3 != 0,
                }
            )
    return pl.DataFrame(rows).with_columns(
        pl.col("season").cast(pl.Int32),
        pl.col("home_win").cast(pl.Int8),
        pl.col("start_utc", "result_utc").cast(pl.Datetime("us", "UTC")),
    )
