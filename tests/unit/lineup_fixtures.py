"""A synthetic league for the lineup tests (#99): four teams over three seasons, each with 14
forwards, 8 defense and four farm players. A team dresses 12 forwards and 6 defense, its healthy
skaters by rank with a scratch now and then, and farm players when too few are healthy. A skater
hurt in a game leaves it early and misses a few. Each summer a forward and a defenseman per team
leave and new ones join, and on the 20th night of 2011-12 BOS trades its first forward to BUF.

The goalies are those of goalie_fixtures: a starter and a backup who dress every game. Games
start at 23:00 UTC, and each boxscore is public at 10:00 UTC the next morning (ADR 0004)."""

from datetime import date, timedelta
from itertools import count

import numpy as np
import polars as pl
from goalie_fixtures import GOALIES, STARTER_SHARE, lineup_frame, public_after, start_of

TEAMS = ("BOS", "BUF", "DET", "TOR")
SEASONS = (20102011, 20112012, 20122013)
NIGHTS = 40
ROSTER = {"F": 14, "D": 8}
NEEDED = {"F": 12, "D": 6}
FARM = 4
HURT = 0.03
SCRATCH = 0.3
FULL_TOI, EARLY_TOI = 1000, 200
TRADE_SEASON, TRADE_NIGHT = 20112012, 20


def league(seed: int = 11) -> dict[str, pl.DataFrame]:
    rng = np.random.default_rng(seed)
    ids = count(1000)
    rosters = {
        team: {role: [next(ids) for _ in range(n)] for role, n in ROSTER.items()} for team in TEAMS
    }
    farms = {team: [next(ids) for _ in range(FARM)] for team in TEAMS}
    out: dict[int, int] = {}
    games, rows = [], []
    for season in SEASONS:
        if season != SEASONS[0]:
            for team in TEAMS:
                for role in ROSTER:
                    rosters[team][role].pop(int(rng.integers(len(rosters[team][role]))))
                    rosters[team][role].append(next(ids))
        day = date(season // 10000, 10, 5)
        number = 0
        played: dict[str, tuple[date, int]] = {}
        for night in range(NIGHTS):
            day += timedelta(days=1 if night % 4 == 1 else 2)
            if season == TRADE_SEASON and night == TRADE_NIGHT:
                rosters["BUF"]["F"].insert(0, rosters["BOS"]["F"].pop(0))
                rosters["BOS"]["F"].append(next(ids))
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
                    dressed = _dress(team, rosters, farms, out, rng)
                    rows += _rows(game_id, season, day, team, is_home, dressed, out, rng)
                    rows += _goalies(game_id, season, day, team, is_home, played, rng)
    frame = pl.DataFrame(games).with_columns(pl.col("season").cast(pl.Int32))
    return {"games": frame, "lineups": lineup_frame(rows)}


def _dress(
    team: str,
    rosters: dict[str, dict[str, list[int]]],
    farms: dict[str, list[int]],
    out: dict[int, int],
    rng: np.random.Generator,
) -> list[tuple[int, str]]:
    dressed: list[tuple[int, str]] = []
    farm = [p for p in farms[team] if out.get(p, 0) == 0]
    for role, needed in NEEDED.items():
        healthy = [p for p in rosters[team][role] if out.get(p, 0) == 0]
        if len(healthy) > needed and rng.random() < SCRATCH:
            healthy.pop(int(rng.integers(len(healthy))))
        while len(healthy) < needed:
            healthy.append(farm.pop(0))
        dressed += [(player, role) for player in healthy[:needed]]
    # The skaters out of this game sit out one game fewer.
    playing = {player for player, _ in dressed}
    for player in [*rosters[team]["F"], *rosters[team]["D"], *farms[team]]:
        if out.get(player, 0) > 0 and player not in playing:
            out[player] -= 1
    return dressed


def _rows(
    game_id: int,
    season: int,
    day: date,
    team: str,
    is_home: bool,
    dressed: list[tuple[int, str]],
    out: dict[int, int],
    rng: np.random.Generator,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for player, role in dressed:
        toi = FULL_TOI + int(rng.integers(-100, 100))
        if rng.random() < HURT:
            toi, out[player] = EARLY_TOI, int(rng.integers(1, 6))
        rows.append(
            {
                "game_id": game_id,
                "season": season,
                "game_date": day,
                "team": team,
                "is_home": is_home,
                "player_id": player,
                "role": role,
                "sweater_number": player % 100,
                "starting_goalie": False,
                "toi_s": toi,
                "observed_utc": public_after(day),
                "raw_key": f"boxscore/{season}/{game_id}",
            }
        )
    return rows


def _goalies(
    game_id: int,
    season: int,
    day: date,
    team: str,
    is_home: bool,
    played: dict[str, tuple[date, int]],
    rng: np.random.Generator,
) -> list[dict[str, object]]:
    first, backup = GOALIES[team]
    last_day, last_starter = played.get(team, (None, None))
    if last_day == day - timedelta(days=1):
        starter = backup if last_starter == first else first
    else:
        starter = first if rng.random() < STARTER_SHARE else backup
    played[team] = (day, starter)
    return [
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
        for k, goalie in enumerate((first, backup))
    ]
