"""One live game for policy v2's starters (ADR 0037): MTL hosts TOR on 2026-10-12, decided at
12:45 ET (16:45 UTC). Each team has two goalie-start candidates, and Bobrovsky (30) has an
earlier goalie-effect row with FLA."""

from datetime import UTC, date, datetime, timedelta

import polars as pl

from nhl_edge.lake.schemas import GoalieEffects, GoalieStarts, GoalMultipliers, dtypes

DAY = date(2026, 10, 12)
GAME = 2026020100
SEASON = 20262027
START = datetime(2026, 10, 12, 23, 0, tzinfo=UTC)
CUTOFF = datetime(2026, 10, 12, 16, 45, tzinfo=UTC)
AS_OF = datetime(2026, 10, 12, 14, 0, tzinfo=UTC)
FIT = datetime(2026, 9, 28, 10, 0, tzinfo=UTC)
XG_PER_SHOT = 0.07
EXPECTED_SHOTS = {"MTL": 28.0, "TOR": 30.0}
CANDIDATES = {"MTL": {10: 0.7, 11: 0.3}, "TOR": {20: 0.6, 21: 0.4}}
EFFECTS = {10: 0.002, 11: -0.001, 20: 0.003, 21: 0.0}
OPPONENT = {"MTL": "TOR", "TOR": "MTL"}
NAMES = {
    10: "Sam Montembeault",
    11: "Jakub Dobeš",
    20: "Anthony Stolarz",
    21: "Joseph Woll",
    30: "Sergei Bobrovsky",
}
BOBROVSKY_EFFECT = 0.004
TEAM_GAMES = pl.DataFrame(
    {
        "game_id": [GAME, GAME],
        "game_date": [DAY, DAY],
        "team": ["MTL", "TOR"],
        "start_utc": [START, START],
        "moment_utc": [CUTOFF, CUTOFF],
    }
)


def goalie_starts() -> pl.DataFrame:
    rows = [
        {
            "game_id": GAME,
            "season": SEASON,
            "game_date": DAY,
            "team": team,
            "goalie_id": goalie,
            "p_start": p,
            "train_cutoff": FIT,
            "artifact_version": "goalie-start-20261012-abc1234",
            "observed_utc": AS_OF,
        }
        for team, picks in CANDIDATES.items()
        for goalie, p in picks.items()
    ]
    return pl.DataFrame(rows, schema=dtypes(GoalieStarts))


def _effect_row(game: int, day: date, team: str, goalie: int, effect: float, seen: datetime):
    return {
        "game_id": game,
        "season": SEASON,
        "game_date": day,
        "team": team,
        "goalie_id": goalie,
        "effect": effect,
        "goalie_games": 300,
        "expected_shots": EXPECTED_SHOTS.get(team, 29.0),
        "goals_saved": effect * EXPECTED_SHOTS.get(team, 29.0),
        "half_life": 60.0,
        "prior_shots": 900.0,
        "train_cutoff": FIT,
        "artifact_version": "goalie-effect-20261012-abc1234",
        "as_of_utc": seen,
        "observed_utc": seen,
    }


def goalie_effects(later: bool = False) -> pl.DataFrame:
    """The game's candidates, Bobrovsky's earlier row with FLA, and with later a row of his
    known only after the cutoff."""
    rows = [
        _effect_row(GAME, DAY, team, goalie, EFFECTS[goalie], AS_OF)
        for team, picks in CANDIDATES.items()
        for goalie in picks
    ]
    earlier = DAY - timedelta(days=3)
    rows.append(
        _effect_row(2026020050, earlier, "FLA", 30, BOBROVSKY_EFFECT, AS_OF - timedelta(days=3))
    )
    if later:
        rows.append(_effect_row(2026020101, DAY, "FLA", 30, 0.02, CUTOFF))
    return pl.DataFrame(rows, schema=dtypes(GoalieEffects))


def goal_multipliers() -> pl.DataFrame:
    rows = []
    for team in ("MTL", "TOR"):
        for goalie in CANDIDATES[OPPONENT[team]]:
            gamma = 1 - EFFECTS[goalie] / XG_PER_SHOT
            rows.append(
                {
                    "game_id": GAME,
                    "season": SEASON,
                    "game_date": DAY,
                    "team": team,
                    "opponent": OPPONENT[team],
                    "goalie_id": goalie,
                    "phi": 1.02,
                    "gamma": gamma,
                    "multiplier": 1.02 * gamma,
                    "xg_per_shot": XG_PER_SHOT,
                    "league_finishing": 1.0,
                    "known_utc": AS_OF - timedelta(hours=4),
                    "as_of_utc": AS_OF,
                    "train_cutoff": FIT,
                    "artifact_version": "finishing-20261012-abc1234",
                    "observed_utc": AS_OF,
                }
            )
    return pl.DataFrame(rows, schema=dtypes(GoalMultipliers))


def candidates() -> pl.DataFrame:
    """The goalie-start rows with each goalie's name, as starters() reads them."""
    return (
        goalie_starts()
        .select("game_id", "team", "goalie_id", "p_start")
        .join(goalies(), on="goalie_id")
    )


def goalies() -> pl.DataFrame:
    return pl.DataFrame({"goalie_id": list(NAMES), "name": list(NAMES.values())})


def nhl(rows: list[tuple[str, datetime, int | None]]) -> pl.DataFrame:
    """(team, observed, starter) per NHL poll."""
    return pl.DataFrame(
        [
            {
                "game_id": GAME,
                "game_date": DAY,
                "team": team,
                "observed_utc": seen,
                "starter_id": starter,
            }
            for team, seen, starter in rows
        ],
        schema={
            "game_id": pl.Int64,
            "game_date": pl.Date,
            "team": pl.String,
            "observed_utc": pl.Datetime("us", "UTC"),
            "starter_id": pl.Int64,
        },
    )


def dailyfaceoff(
    rows: list[tuple[str, datetime, str | None, str | None]], start: datetime = START
) -> pl.DataFrame:
    """(team, observed, goalie, status) per Daily Faceoff poll of the listing starting at start."""
    return pl.DataFrame(
        [
            {
                "game_date": DAY,
                "team": team,
                "start_utc": start,
                "observed_utc": seen,
                "goalie_name": goalie,
                "status": status,
            }
            for team, seen, goalie, status in rows
        ],
        schema={
            "game_date": pl.Date,
            "team": pl.String,
            "start_utc": pl.Datetime("us", "UTC"),
            "observed_utc": pl.Datetime("us", "UTC"),
            "goalie_name": pl.String,
            "status": pl.String,
        },
    )


def at(hour: int, minute: int) -> datetime:
    return datetime(2026, 10, 12, hour, minute, tzinfo=UTC)
