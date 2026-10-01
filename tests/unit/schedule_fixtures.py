"""A synthetic league for the schedule-terms tests (#77): four real teams in their real arenas
(BOS, NYR, LAK, VAN) over four seasons, with a back-to-back every fourth night and one game a
season at a neutral site in Stockholm. Games start at 23:00 UTC and their results are public at
10:00 UTC the next morning (ADR 0003); a game's schedule is public a day before it starts (ADR
0005)."""

from datetime import UTC, date, datetime, time, timedelta

import numpy as np
import polars as pl

from nhl_edge.lake.schemas import SCHEDULE_LEAD

TEAMS = ("BOS", "NYR", "LAK", "VAN")
VENUES = {
    "BOS": "TD Garden",
    "NYR": "Madison Square Garden",
    "LAK": "Crypto.com Arena",
    "VAN": "Rogers Arena",
}
NEUTRAL_VENUE = "Avicii Arena"
SEASONS = (20092010, 20102011, 20112012, 20122013)
NIGHTS = 30


def game_row(
    game_id: int,
    season: int,
    day: date,
    home: str,
    away: str,
    venue: str | None = None,
    home_score: int = 3,
    away_score: int = 2,
    neutral: bool = False,
) -> dict[str, object]:
    start = datetime.combine(day, time(23), UTC)
    return {
        "game_id": game_id,
        "season": season,
        "game_date": day,
        "start_utc": start,
        "home": home,
        "away": away,
        "venue": venue or VENUES[home],
        "home_score": home_score,
        "away_score": away_score,
        "neutral_site": neutral,
        "limited_attendance": False,
        "observed_utc": datetime.combine(day + timedelta(days=1), time(10), UTC),
    }


def frames(rows: list[dict[str, object]]) -> dict[str, pl.DataFrame]:
    """games (with results) and schedule (pre-game facts, public a day before the start)."""
    games = pl.DataFrame(rows).with_columns(
        pl.col("season").cast(pl.Int32),
        pl.col("home_score", "away_score").cast(pl.Int16),
    )
    schedule = games.drop("home_score", "away_score").with_columns(
        observed_utc=pl.col("start_utc") - SCHEDULE_LEAD
    )
    return {"games": games, "schedule": schedule}


def league(seed: int = 11) -> dict[str, pl.DataFrame]:
    rng = np.random.default_rng(seed)
    rows = []
    for season in SEASONS:
        day = date(season // 10000, 10, 5)
        for night in range(NIGHTS):
            day += timedelta(days=1 if night % 4 == 1 else 2)
            order = rng.permutation(TEAMS)
            for k, (home, away) in enumerate(zip(order[0::2], order[1::2], strict=True)):
                game_id = season // 10000 * 1_000_000 + 20_000 + 2 * night + k + 1
                neutral = night == 12 and k == 0
                rows.append(
                    game_row(
                        game_id,
                        season,
                        day,
                        str(home),
                        str(away),
                        NEUTRAL_VENUE if neutral else None,
                        int(rng.integers(0, 6)),
                        int(rng.integers(0, 6)),
                        neutral,
                    )
                )
    for row in rows:
        if row["home_score"] == row["away_score"]:
            row["home_score"] = int(row["home_score"]) + 1  # type: ignore[call-overload]
    return frames(rows)
