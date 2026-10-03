"""Synthetic player-layer tables for the B3 tests (#106): for any games frame and B2's tables
(b2_fixtures), the rows B3 reads from lineups, lineup_replacements, player_ratings, rapm_terms,
expected_power_plays and goal_multipliers. Each team has SKATERS projected skaters a game, four
forwards and two defensemen with fixed ratings, and B2's good and poor goalies, A converting at
GAMMA["A"] and B at GAMMA["B"]. Rows are known at the game's as-of time, or from the tuning
cutoff when that is later (ADR 0011).

league() draws each game's result from B3's own form, with a known weight, so a fit can be
checked: P(home win) = sigmoid(H_S + WEIGHT * delta_g_hat), with the goalies who started.
"""

from datetime import UTC, date, datetime, time, timedelta

import numpy as np
import polars as pl
from b2_fixtures import TEAMS, feature_tables, goalie_id

from nhl_edge.features import team_strength as ts
from nhl_edge.game import b2, b3
from nhl_edge.lake.schemas import Games, dtypes

SKATERS = 6
ROLES = ("F", "F", "F", "F", "D", "D")
GAMMA = {"A": 0.9, "B": 1.1}
MU_5V5, MU_PP = 2.4, 6.0
H_S = 0.15
WEIGHT = 1.2
UTC_TYPE = pl.Datetime("us", "UTC")


def skater_id(team: str, k: int) -> int:
    return 100 * int.from_bytes(team.encode(), "big") + k


def player_tables(games: pl.DataFrame, b2_tables: b2.Tables, seed: int = 0) -> b3.Tables:
    """B3's tables for every game of games from FIRST_SEASON on, sharing B2's schedule terms,
    goalie starts and boxscores."""
    rng = np.random.default_rng(seed)
    others = sorted(set(games["home"]) | set(games["away"]) - set(TEAMS))
    rating = {
        (team, k): rng.normal(0, 0.15, size=4) for team in (*TEAMS, *others) for k in range(SKATERS)
    }
    rated = games.filter(pl.col("season") >= b3.FIRST_SEASON).sort("game_id")
    as_of = rated.select(ts.as_of(pl.col("game_date"), pl.col("start_utc"))).to_series()
    lineups, spare, ratings, terms, power, mults = [], [], [], [], [], []
    dates = set()
    for row, moment in zip(rated.iter_rows(named=True), as_of, strict=True):
        known = max(moment, b3.TUNED_CUTOFF)
        game, season, day = row["game_id"], row["season"], row["game_date"]
        base = {"game_id": game, "season": season, "game_date": day}
        stamps = {"as_of_utc": moment, "observed_utc": known, "train_cutoff": b3.TUNED_CUTOFF}
        if day not in dates:
            dates.add(day)
            for model, mu in (("ev", MU_5V5), ("pp", MU_PP)):
                for term, value in (("intercept", mu), (f"season:{season}", 0.0)):
                    terms.append(
                        {"season": season, "game_date": day, "model": model, "term": term}
                        | {"value": value, "observed_utc": known, "train_cutoff": b3.TUNED_CUTOFF}
                    )
        for side, other in (("home", "away"), ("away", "home")):
            team, opponent = row[side], row[other]
            for k in range(SKATERS):
                player = skater_id(team, k)
                lineups.append(
                    base
                    | {
                        "team": team,
                        "player_id": player,
                        "role": ROLES[k],
                        "p_available": 1.0,
                        "exp_5v5": 50.0 * 5 / SKATERS,
                        "exp_pp": 5.0 * 5 / SKATERS,
                        "exp_pk": 5.0 * 4 / SKATERS,
                        "pp_unit": k < 5,
                        "observed_utc": known,
                        "train_cutoff": b3.TUNED_CUTOFF,
                    }
                )
                for component, value in zip(
                    ("ev_off", "ev_def", "pp", "pk"), rating[team, k], strict=True
                ):
                    ratings.append(
                        base
                        | {"player_id": player, "component": component, "mean": float(value)}
                        | stamps
                    )
            spare.append(
                base
                | {
                    "team": team,
                    "role": "F",
                    "exp_5v5": 0.0,
                    "exp_pp": 0.0,
                    "exp_pk": 0.0,
                    "observed_utc": known,
                    "train_cutoff": b3.TUNED_CUTOFF,
                }
            )
            power.append(
                base
                | {
                    "team": team,
                    "opponent": opponent,
                    "is_home": side == "home",
                    "pp_minutes": 5.0,
                    "sh_xg": 0.05,
                }
                | stamps
            )
            phi = float(rng.uniform(0.95, 1.05))
            for which in ("A", "B"):
                mults.append(
                    base
                    | {
                        "team": team,
                        "opponent": opponent,
                        "goalie_id": goalie_id(opponent, which),
                        "phi": phi,
                        "gamma": GAMMA[which],
                        "multiplier": phi * GAMMA[which],
                        "league_finishing": 1.0,
                    }
                    | stamps
                )

    def frame(rows: list[dict[str, object]]) -> pl.DataFrame:
        out = pl.DataFrame(rows)
        casts = {
            c: UTC_TYPE for c in ("as_of_utc", "observed_utc", "train_cutoff") if c in out.columns
        }
        return out.with_columns(
            pl.col("season").cast(pl.Int32), *(pl.col(c).cast(t) for c, t in casts.items())
        )

    return b3.Tables(
        games=games,
        schedule_terms=b2_tables.schedule_terms,
        goalie_starts=b2_tables.goalie_starts,
        actual_lineups=b2_tables.actual_lineups,
        lineups=frame(lineups),
        lineup_replacements=frame(spare),
        player_ratings=frame(ratings),
        rapm_terms=frame(terms),
        expected_power_plays=frame(power),
        goal_multipliers=frame(mults),
    )


def league(
    seasons: tuple[int, ...] = (20162017, 20172018, 20182019), games: int = 400, seed: int = 4
) -> b3.Tables:
    """Games of the seasons whose results follow B3's form with a known weight, and B3's
    tables."""
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
    tables = player_tables(frame, feature_tables(frame, seed), seed)
    inputs = b3.game_inputs(tables)
    delta = b3.starters_delta(tables, inputs)
    drawn = (
        frame.join(delta.select("game_id", "delta_g_hat"), on="game_id")
        .with_columns(p=1 / (1 + (-(H_S + WEIGHT * pl.col("delta_g_hat"))).exp()))
        .sort("game_id")
    )
    wins = rng.random(drawn.height) < drawn["p"].to_numpy()
    scored = drawn.with_columns(
        home_score=pl.Series(np.where(wins, 3, 1), dtype=pl.Int16),
        away_score=pl.Series(np.where(wins, 1, 3), dtype=pl.Int16),
    ).select(list(dtypes(Games)))
    return b3.Tables(
        scored,
        *(
            getattr(tables, name)
            for name in (
                "schedule_terms",
                "goalie_starts",
                "actual_lineups",
                "lineups",
                "lineup_replacements",
                "player_ratings",
                "rapm_terms",
                "expected_power_plays",
                "goal_multipliers",
            )
        ),
    )
