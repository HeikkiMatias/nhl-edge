"""A made-up league for the RAPM tests (#101): four teams over two seasons with xG, each with
eight forwards and five defensemen whose true ratings are known. Every stint puts three forwards
and two defensemen of each team on the ice, drawn at random, so their ratings can be told apart.

A stint's xG is its true rate times its length, plus a little noise:
- 5v5: 2.4 per hour, plus 0.2 at home, plus the attackers' offense, minus the defenders' defense;
- the power play: 6.0 per hour, plus 4.0 at 5v3, minus 0.5 per defenseman on it, plus its
  skaters' power-play offense, minus the killers' penalty-kill defense.
Each night's stints are public at 10:00 UTC the next morning, and games start at 23:00 UTC."""

from datetime import date, timedelta

import numpy as np
import polars as pl
from goalie_fixtures import public_after, start_of

from nhl_edge.lake.schemas import Stints, dtypes

TEAMS = ("BOS", "BUF", "DET", "TOR")
ARENAS = {"BOS": "td_garden", "BUF": "keybank_center", "DET": "joe_louis_arena", "TOR": "acc"}
SEASONS = (20112012, 20122013)
NIGHTS = 30
STINTS = 60
FORWARDS, DEFENSE = 8, 5
EV_RATE, HOME_EDGE = 2.4, 0.2
PP_RATE, FIVE_ON_THREE, PER_DEFENSEMAN = 6.0, 4.0, -0.5
SPREAD = 0.4
NOISE = 0.02
# Every fifth stint is a power play, every seventh of those a 5v3.
PP_EVERY, FIVE_ON_THREE_EVERY = 5, 7
# A player who joins BUF for 2012-13 only.
LATE_PLAYER = 9999


def _players(team_number: int) -> dict[str, list[int]]:
    base = 100 * (team_number + 1)
    return {
        "F": [base + i for i in range(FORWARDS)],
        "D": [base + 50 + i for i in range(DEFENSE)],
    }


def truth(seed: int = 3) -> dict[int, dict[str, float]]:
    """Each player's true ratings, components as in PlayerRatings."""
    rng = np.random.default_rng(seed)
    out = {}
    for t, _ in enumerate(TEAMS):
        for role, ids in _players(t).items():
            for player in ids:
                out[player] = {
                    "role": role,
                    **{c: float(rng.normal(0, SPREAD)) for c in ("ev_off", "ev_def", "pp", "pk")},
                }
    out[LATE_PLAYER] = {"role": "F", "ev_off": 0.5, "ev_def": 0.0, "pp": 0.0, "pk": 0.0}
    return out  # type: ignore[return-value]


def league(seed: int = 7, seasons: tuple[int, ...] = SEASONS) -> dict[str, pl.DataFrame]:
    """games, stints (Stints' columns), roles (actual_lineups), venues and lineups."""
    rng = np.random.default_rng(seed)
    true = truth()
    rosters = {team: _players(t) for t, team in enumerate(TEAMS)}
    games, stints, roles, lineups = [], [], [], []
    for season in seasons:
        if season == seasons[1]:
            rosters["BUF"]["F"][-1] = LATE_PLAYER
        day = date(season // 10000, 10, 6)
        number = 0
        for night in range(NIGHTS):
            day += timedelta(days=2)
            order = rng.permutation(TEAMS)
            for home, away in zip(order[0::2], order[1::2], strict=True):
                number += 1
                game_id = season // 10000 * 1_000_000 + 20_000 + number
                home, away = str(home), str(away)
                neutral = season == seasons[0] and night == 0
                games.append(
                    {
                        "game_id": game_id,
                        "season": season,
                        "game_date": day,
                        "start_utc": start_of(day),
                        "home": home,
                        "away": away,
                        "venue": f"{ARENAS[home]} hall",
                        "neutral_site": neutral,
                        # A result for the tuning's win model, public the morning after.
                        "home_score": int(rng.integers(0, 6)),
                        "away_score": int(rng.integers(0, 6)),
                        "observed_utc": public_after(day),
                    }
                )
                for team in (home, away):
                    for role, ids in rosters[team].items():
                        for player in ids:
                            roles.append({"game_id": game_id, "player_id": player, "role": role})
                            lineups.append(
                                {
                                    "game_id": game_id,
                                    "season": season,
                                    "game_date": day,
                                    "team": team,
                                    "player_id": player,
                                    "role": role,
                                }
                            )
                for k in range(STINTS):
                    stints.append(
                        _stint(rng, true, rosters, game_id, season, day, home, away, k, neutral)
                    )
    venues = pl.DataFrame(
        {"venue": [f"{a} hall" for a in ARENAS.values()], "arena_id": list(ARENAS.values())}
    )
    frame = pl.DataFrame(stints).with_columns(
        observed_utc=pl.col("game_date").map_elements(
            public_after, return_dtype=pl.Datetime("us", "UTC")
        )
    )
    return {
        "games": pl.DataFrame(games).with_columns(pl.col("season").cast(pl.Int32)),
        "stints": frame.select(list(dtypes(Stints))).cast(dtypes(Stints)),  # type: ignore[arg-type]
        "roles": pl.DataFrame(roles),
        "venues": venues,
        "lineups": pl.DataFrame(lineups).with_columns(pl.col("season").cast(pl.Int32)),
    }


def _stint(
    rng: np.random.Generator,
    true: dict[int, dict[str, float]],
    rosters: dict[str, dict[str, list[int]]],
    game_id: int,
    season: int,
    day: date,
    home: str,
    away: str,
    k: int,
    neutral: bool,
) -> dict[str, object]:
    seconds = int(rng.integers(20, 90))
    hours = seconds / 3600

    def unit(team: str, forwards: int, defense: int) -> list[int]:
        f = rng.choice(rosters[team]["F"], forwards, replace=False)
        d = rng.choice(rosters[team]["D"], defense, replace=False)
        return sorted(int(p) for p in (*f, *d))

    def noisy(rate: float) -> float:
        return max(0.0, rate * hours + float(rng.normal(0, NOISE)) * hours)

    if k % PP_EVERY:
        strength = "5v5"
        h, a = unit(home, 3, 2), unit(away, 3, 2)
        edge = 0.0 if neutral else HOME_EDGE
        home_xg = noisy(EV_RATE + edge + _sum(true, h, "ev_off") - _sum(true, a, "ev_def"))
        away_xg = noisy(EV_RATE + _sum(true, a, "ev_off") - _sum(true, h, "ev_def"))
    else:
        five_three = k % (PP_EVERY * FIVE_ON_THREE_EVERY) == 0
        killers = 3 if five_three else 4
        defensemen = 1 + (k // PP_EVERY) % 2
        h, a = unit(home, 5 - defensemen, defensemen), unit(away, killers - 2, 2)
        strength = f"5v{killers}"
        rate = PP_RATE + (FIVE_ON_THREE if five_three else 0.0) + PER_DEFENSEMAN * defensemen
        home_xg = noisy(rate + _sum(true, h, "pp") - _sum(true, a, "pk"))
        away_xg = 0.0
    return {
        "game_id": game_id,
        "season": season,
        "game_date": day,
        "stint_id": k + 1,
        "period": 1,
        "start_s": 60 * k,
        "end_s": 60 * k + seconds,
        "seconds": seconds,
        "home_skaters": h,
        "away_skaters": a,
        "home_goalie": 1,
        "away_goalie": 2,
        "strength": strength,
        "score_state": 0,
        "zone_start": None,
        "home_xg": home_xg,
        "away_xg": away_xg,
        "home_goals": 0,
        "away_goals": 0,
        "drop_reason": None,
        "xg_version": "xg-20261001-abc1234",
        "xg_train_cutoff": None,
    }


def _sum(true: dict[int, dict[str, float]], players: list[int], component: str) -> float:
    return sum(true[p][component] for p in players)


def seasons_of(stints: pl.DataFrame, seasons: tuple[int, ...] = SEASONS) -> list[pl.DataFrame]:
    """The stints a season at a time, in order, as the lake gives them."""
    return [stints.filter(pl.col("season") == s) for s in seasons]


# The priors' inputs (#102): a player's draft pick follows his true 5v5 offense, best first, and
# every seventh player is undrafted.
HISTORY_PLAYERS = 40
AHL_FACTOR = 0.5


def players(seed: int = 5) -> pl.DataFrame:
    """Birth dates and draft picks for every player of truth()."""
    rng = np.random.default_rng(seed)
    true = truth()
    ranked = sorted(true, key=lambda p: -true[p]["ev_off"])
    rows = []
    for rank, player in enumerate(ranked):
        born = date(1984, 1, 1) + timedelta(days=int(rng.integers(0, 12 * 365)))
        pick = None if rank % 7 == 6 else 1 + 4 * rank
        if player == LATE_PLAYER:
            born, pick = date(1993, 6, 1), 1
        rows.append({"player_id": player, "birth_date": born, "draft_overall": pick})
    return pl.DataFrame(
        rows, schema={"player_id": pl.Int64, "birth_date": pl.Date, "draft_overall": pl.Int16}
    )


def league_seasons() -> pl.DataFrame:
    """Career lines: HISTORY_PLAYERS moves from an AHL season straight into an NHL season, the
    NHL scoring AHL_FACTOR as many points per game, and the late player's AHL season before he
    joins, public on July 1 after each season."""
    rows = []

    def line(player: int, season: int, league: str, games: int, points: int) -> None:
        rows.append(
            {
                "player_id": player,
                "season": season,
                "league_abbrev": league,
                "league": league,
                "game_type": 2,
                "games_played": games,
                "goals": points // 2,
                "assists": points - points // 2,
                "observed_utc": public_after(date(season // 10000 + 1, 6, 30)),
            }
        )

    for k in range(HISTORY_PLAYERS):
        player = 5000 + k
        line(player, 20092010, "AHL", 60, 60)
        line(player, 20102011, "NHL", 40, int(40 * AHL_FACTOR))
    line(LATE_PLAYER, 20112012, "AHL", 70, 105)
    return pl.DataFrame(rows).with_columns(
        pl.col("season").cast(pl.Int32),
        pl.col("game_type").cast(pl.Int8),
        pl.col("games_played", "goals", "assists").cast(pl.Int16),
    )
