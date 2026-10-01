"""A synthetic league for the team strength tests (#74): six teams of known strength over two
seasons, as the inputs team_strength reads (games, shots, shot_xg, strength_time). Each night
three games start at 23:00 UTC, and each game's rows are public at 10:00 UTC the next morning
(ADR 0004)."""

from datetime import UTC, date, datetime, time, timedelta

import numpy as np
import polars as pl

TEAMS = ("BOS", "BUF", "DET", "FLA", "MTL", "TOR")
# Each team's expected goals per 5v5 shot: BOS is the best, TOR the worst.
QUALITY = dict(zip(TEAMS, (0.12, 0.10, 0.09, 0.08, 0.07, 0.05), strict=True))
SEASONS = (20112012, 20122013)
NIGHTS = 40


def league(seed: int = 3) -> dict[str, pl.DataFrame]:
    rng = np.random.default_rng(seed)
    games, shots, xg, time_on = [], [], [], []
    game_number = 0
    for season in SEASONS:
        first = date(season // 10000, 10, 5)
        for night in range(NIGHTS):
            day = first + timedelta(days=2 * night)
            start = datetime.combine(day, time(23), UTC)
            public = datetime.combine(day + timedelta(days=1), time(10), UTC)
            order = rng.permutation(TEAMS)
            for home, away in zip(order[0::2], order[1::2], strict=True):
                game_number += 1
                game_id = season // 10000 * 1_000_000 + 20_000 + game_number
                games.append(
                    {
                        "game_id": game_id,
                        "season": season,
                        "game_date": day,
                        "start_utc": start,
                        "home": home,
                        "away": away,
                        "home_score": int(rng.integers(0, 6)),
                        "away_score": int(rng.integers(0, 6)),
                        "observed_utc": public,
                    }
                )
                event = 0
                for team in (home, away):
                    for state, seconds in (("5v5", 2880), ("5v4", 240), ("4v5", 240)):
                        time_on.append(
                            {
                                "game_id": game_id,
                                "season": season,
                                "game_date": day,
                                "team": team,
                                "strength": state,
                                "own_net_empty": False,
                                "opp_net_empty": False,
                                "seconds": seconds,
                                "observed_utc": public,
                            }
                        )
                    is_home = team == home
                    # (skaters for, against, the shooting team's goalie in, shots). The last row is
                    # 5 on 5 with the shooting team's goalie pulled, which team strength leaves out.
                    for for_, against, goalie_in, count in (
                        (5, 5, True, 25),
                        (5, 4, True, 4),
                        (6, 5, False, 2),
                        (5, 5, False, 1),
                    ):
                        own = "1" if goalie_in else "0"
                        side = (for_, against) if is_home else (against, for_)
                        code = (
                            f"1{side[1]}{side[0]}{own}" if is_home else f"{own}{side[1]}{side[0]}1"
                        )
                        for _ in range(count):
                            event += 1
                            shots.append(
                                {
                                    "game_id": game_id,
                                    "event_id": event,
                                    "team": team,
                                    "is_home": is_home,
                                    "skaters_for": for_,
                                    "skaters_against": against,
                                    "situation_code": code,
                                }
                            )
                            quality = QUALITY[team] * (1.5 if (for_, against) == (5, 4) else 1.0)
                            if not goalie_in:
                                quality = 0.5
                            xg.append(
                                {
                                    "game_id": game_id,
                                    "event_id": event,
                                    "xg": float(np.clip(rng.normal(quality, 0.02), 0.01, 0.9)),
                                    "observed_utc": public,
                                }
                            )
    return {
        "games": pl.DataFrame(games).with_columns(
            pl.col("season").cast(pl.Int32), pl.col("game_id").cast(pl.Int64)
        ),
        "shots": pl.DataFrame(shots).with_columns(pl.col("game_id").cast(pl.Int64)),
        "shot_xg": pl.DataFrame(xg).with_columns(pl.col("game_id").cast(pl.Int64)),
        "strength_time": pl.DataFrame(time_on).with_columns(
            pl.col("season").cast(pl.Int32),
            pl.col("game_id").cast(pl.Int64),
            pl.col("seconds").cast(pl.Int32),
        ),
    }
