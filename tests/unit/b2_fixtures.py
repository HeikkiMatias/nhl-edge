"""Synthetic feature tables for the B2 tests (#78): for any games frame, the rows B2 reads from
team_strength, schedule_terms, goalie_starts, goalie_effects and actual_lineups. Each team has a
good goalie, A, who starts with probability P_A, and a poor one, B. Rows are known at the game's
as-of time, or from the tuning cutoff when that is later (ADR 0011); boxscores the morning after.

league() also draws each game's result from B2's own form, with known weights, so a fit can be
checked: P(home win) = sigmoid(H_S + S_WEIGHT * delta_s + G_WEIGHT * delta_g), with delta_g from
the goalies who started.
"""

from datetime import UTC, date, datetime, time, timedelta

import numpy as np
import polars as pl

from nhl_edge.features import team_strength as ts
from nhl_edge.game import b2
from nhl_edge.lake.schemas import Games, dtypes

TEAMS = ("BOS", "TOR", "MTL", "NYR", "CHI", "DET", "EDM", "CGY")
P_A = 0.7
SAVED = {"A": 0.4, "B": -0.4}
H_S = 0.15
S_WEIGHT = 1.5
G_WEIGHT = 1.0
# Every row is known by then: the outcomes are drawn from the goalies who started.
END_OF_TIME = datetime(9999, 1, 1, tzinfo=UTC)


def goalie_id(team: str, which: str) -> int:
    """A goalie id unique to the team code and the goalie, for any team code."""
    return 10 * int.from_bytes(team.encode(), "big") + (1 if which == "A" else 2)


def feature_tables(games: pl.DataFrame, seed: int = 0) -> b2.Tables:
    """The feature tables for every game of games from FIRST_SEASON on, with random ΔS, rest and
    travel, and starters drawn by the start probabilities."""
    rng = np.random.default_rng(seed)
    rated = games.filter(pl.col("season") >= b2.FIRST_SEASON).sort("game_id")
    as_of = rated.select(ts.as_of(pl.col("game_date"), pl.col("start_utc"))).to_series()
    strength, terms, starts, effects, lineups = [], [], [], [], []
    for row, moment in zip(rated.iter_rows(named=True), as_of, strict=True):
        known = max(moment, b2.TUNED_CUTOFF)
        game = row["game_id"]
        strength.append(
            {
                "game_id": game,
                "delta_s": float(rng.normal(0, 0.3)),
                "as_of_utc": moment,
                "observed_utc": known,
            }
        )
        rest = rng.integers(1, 5, size=2)
        terms.append(
            {
                "game_id": game,
                "season": row["season"],
                "game_date": row["game_date"],
                "home": row["home"],
                "away": row["away"],
                "neutral_site": False,
                "capacity_share": 1.0,
                "home_rest_days": int(rest[0]),
                "away_rest_days": int(rest[1]),
                "home_back_to_back": bool(rest[0] == 1),
                "away_back_to_back": bool(rest[1] == 1),
                "home_travel_km": float(rng.uniform(0, 3000)),
                "away_travel_km": float(rng.uniform(0, 3000)),
                "home_tz_shift": float(rng.integers(-3, 4)),
                "away_tz_shift": float(rng.integers(-3, 4)),
                "h_s": H_S,
                "as_of_utc": moment,
                "observed_utc": known,
            }
        )
        boxscore = datetime.combine(row["game_date"] + timedelta(days=1), time(10), UTC)
        for side in ("home", "away"):
            team = row[side]
            started = "A" if rng.random() < P_A else "B"
            for which, p in (("A", P_A), ("B", 1 - P_A)):
                goalie = goalie_id(team, which)
                key = {"game_id": game, "team": team, "goalie_id": goalie}
                starts.append(
                    key
                    | {
                        "season": row["season"],
                        "game_date": row["game_date"],
                        "p_start": p,
                        "observed_utc": moment,
                    }
                )
                effects.append(
                    key | {"goals_saved": SAVED[which], "as_of_utc": moment, "observed_utc": known}
                )
                lineups.append(
                    {
                        "game_id": game,
                        "season": row["season"],
                        "game_date": row["game_date"],
                        "team": team,
                        "player_id": goalie,
                        "role": "G",
                        "starting_goalie": which == started,
                        "observed_utc": boxscore,
                    }
                )
    utc = pl.Datetime("us", "UTC")
    return b2.Tables(
        games=games,
        team_strength=pl.DataFrame(strength).with_columns(
            pl.col("as_of_utc", "observed_utc").cast(utc)
        ),
        schedule_terms=pl.DataFrame(terms).with_columns(
            pl.col("season").cast(pl.Int32),
            pl.col("home_rest_days", "away_rest_days").cast(pl.Int8),
            pl.col("as_of_utc", "observed_utc").cast(utc),
        ),
        goalie_starts=pl.DataFrame(starts).with_columns(
            pl.col("season").cast(pl.Int32), pl.col("observed_utc").cast(utc)
        ),
        goalie_effects=pl.DataFrame(effects).with_columns(
            pl.col("as_of_utc", "observed_utc").cast(utc)
        ),
        actual_lineups=pl.DataFrame(lineups).with_columns(
            pl.col("season").cast(pl.Int32), pl.col("observed_utc").cast(utc)
        ),
    )


def league(
    seasons: tuple[int, ...] = (20162017, 20172018, 20182019), games: int = 400, seed: int = 4
) -> b2.Tables:
    """Games of the seasons whose results follow B2's form with known weights, and their tables."""
    rng = np.random.default_rng(seed)
    rows = []
    for season in seasons:
        year = season // 10_000
        for i in range(games):
            day = date(year, 10, 10) + timedelta(days=i // 4)
            home, away = TEAMS[i % 8], TEAMS[(i + 1 + i // 8) % 8]
            if home == away:
                away = TEAMS[(i + 4) % 8]
            rows.append(
                {
                    "game_id": year * 1_000_000 + 20_000 + i + 1,
                    "season": season,
                    "game_date": day,
                    "start_utc": datetime.combine(day, time(23), UTC),
                    "home": home,
                    "away": away,
                    "venue": "x",
                    "home_score": 0,
                    "away_score": 0,
                    "decided_in": "REG",
                    "neutral_site": False,
                    "limited_attendance": False,
                    "observed_utc": datetime.combine(day + timedelta(days=1), time(10), UTC),
                    "raw_key": "games/synthetic",
                }
            )
    frame = pl.DataFrame(rows, schema=dtypes(Games))
    tables = feature_tables(frame, seed)
    delta_g = b2.starters_delta(tables, END_OF_TIME)
    drawn = (
        frame.join(tables.team_strength.select("game_id", "delta_s"), on="game_id")
        .join(delta_g.select("game_id", "delta_g"), on="game_id")
        .with_columns(
            p=1 / (1 + (-(H_S + S_WEIGHT * pl.col("delta_s") + G_WEIGHT * pl.col("delta_g"))).exp())
        )
    )
    # Joins keep no row order: sort, so each game gets the same draw on every run.
    drawn = drawn.sort("game_id")
    wins = rng.random(drawn.height) < drawn["p"].to_numpy()
    scored = drawn.with_columns(
        home_score=pl.Series(np.where(wins, 3, 1), dtype=pl.Int16),
        away_score=pl.Series(np.where(wins, 1, 3), dtype=pl.Int16),
    ).select(list(dtypes(Games)))
    return b2.Tables(
        scored,
        *(
            getattr(tables, name)
            for name in (
                "team_strength",
                "schedule_terms",
                "goalie_starts",
                "goalie_effects",
                "actual_lineups",
            )
        ),
    )
