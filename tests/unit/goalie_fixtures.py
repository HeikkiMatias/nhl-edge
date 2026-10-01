"""A synthetic league for the goalie-start tests (#76): four teams over three seasons, each with
a starter and a backup who dress every game. The starter starts most games, and the goalie who
rested the first night of a back-to-back starts the second. Games start at 23:00 UTC, and each
boxscore is public at 10:00 UTC the next morning (ADR 0004)."""

from datetime import UTC, date, datetime, time, timedelta

import numpy as np
import polars as pl

TEAMS = ("BOS", "BUF", "DET", "TOR")
SEASONS = (20102011, 20112012, 20122013)
NIGHTS = 45
# Each team's goalies: starter, backup.
GOALIES = {team: (100 + 10 * i, 101 + 10 * i) for i, team in enumerate(TEAMS)}
STARTER_SHARE = 0.8


def start_of(day: date) -> datetime:
    return datetime.combine(day, time(23), UTC)


def public_after(day: date) -> datetime:
    return datetime.combine(day + timedelta(days=1), time(10), UTC)


def lineup_rows(
    game_id: int,
    season: int,
    day: date,
    team: str,
    is_home: bool,
    starter: int,
    dressed: tuple[int, ...],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = [
        {
            "game_id": game_id,
            "season": season,
            "game_date": day,
            "team": team,
            "is_home": is_home,
            "player_id": goalie,
            "role": "G",
            "sweater_number": 30 + k,
            "starting_goalie": goalie == starter,
            "toi_s": 3600 if goalie == starter else 0,
            "observed_utc": public_after(day),
            "raw_key": f"boxscore/{season}/{game_id}",
        }
        for k, goalie in enumerate(dressed)
    ]
    # A skater, so the goalie filter has something to leave out.
    rows.append({**rows[0], "player_id": game_id * 10 + is_home, "role": "F", "sweater_number": 9})
    rows[-1]["starting_goalie"] = False
    return rows


def league(seed: int = 5) -> dict[str, pl.DataFrame]:
    rng = np.random.default_rng(seed)
    games, lineups = [], []
    for season in SEASONS:
        day = date(season // 10000, 10, 5)
        number = 0
        played: dict[str, tuple[date, int]] = {}
        for night in range(NIGHTS):
            # Every fourth night follows the previous one: a back-to-back for every team.
            day += timedelta(days=1 if night % 4 == 1 else 2)
            order = rng.permutation(TEAMS)
            for home, away in zip(order[0::2], order[1::2], strict=True):
                number += 1
                game_id = season // 10000 * 1_000_000 + 20_000 + number
                games.append(
                    {
                        "game_id": game_id,
                        "season": season,
                        "game_date": day,
                        "start_utc": start_of(day),
                        "home": str(home),
                        "away": str(away),
                        "observed_utc": public_after(day),
                    }
                )
                for team, is_home in ((str(home), True), (str(away), False)):
                    first, backup = GOALIES[team]
                    last_day, last_starter = played.get(team, (None, None))
                    if last_day == day - timedelta(days=1):
                        # The goalie who rested last night starts the back-to-back.
                        starter = backup if last_starter == first else first
                    else:
                        starter = first if rng.random() < STARTER_SHARE else backup
                    lineups += lineup_rows(
                        game_id, season, day, team, is_home, starter, (first, backup)
                    )
                    played[team] = (day, starter)
    frame = pl.DataFrame(games).with_columns(pl.col("season").cast(pl.Int32))
    return {"games": frame, "lineups": lineup_frame(lineups)}


def lineup_frame(rows: list[dict[str, object]]) -> pl.DataFrame:
    return pl.DataFrame(
        rows,
        schema={
            "game_id": pl.Int64,
            "season": pl.Int32,
            "game_date": pl.Date,
            "team": pl.String,
            "is_home": pl.Boolean,
            "player_id": pl.Int64,
            "role": pl.String,
            "sweater_number": pl.Int16,
            "starting_goalie": pl.Boolean,
            "toi_s": pl.Int32,
            "observed_utc": pl.Datetime("us", "UTC"),
            "raw_key": pl.String,
        },
    )
