from datetime import UTC, date, datetime, timedelta

import polars as pl

from nhl_edge.audit import goalies

DAY = date(2026, 10, 3)
START = datetime(2026, 10, 3, 23, 0, tzinfo=UTC)
LATE = datetime(2026, 10, 4, 2, 0, tzinfo=UTC)
GAMES = pl.DataFrame(
    {
        "game_id": [1, 2],
        "game_date": [DAY, DAY],
        "start_utc": [START, LATE],
        "home": ["MTL", "EDM"],
        "away": ["TOR", "VAN"],
    }
)
LINEUPS = pl.DataFrame(
    {
        "game_id": [1, 1, 1, 2, 2],
        "team": ["MTL", "MTL", "TOR", "EDM", "VAN"],
        "player_id": [10, 11, 20, 30, 40],
        "starting_goalie": [True, False, True, True, True],
    }
)
PLAYERS = pl.DataFrame(
    {
        "player_id": [10, 11, 20, 30, 40],
        "name": [
            "Jakub Dobeš",
            "Sam Montembeault",
            "Anthony Stolarz",
            "Tristan Jarry",
            "Kevin Lankinen",
        ],
    }
)
STARTERS = goalies.starters(GAMES, LINEUPS, PLAYERS)


def before(start: datetime, minutes: int) -> datetime:
    return start - timedelta(minutes=minutes)


def nhl_polls(rows: list[tuple[int, str, datetime, int, int | None]]) -> pl.DataFrame:
    """(game_id, team, start, minutes before it, starter_id) per poll."""
    return pl.DataFrame(
        [
            {
                "game_id": game_id,
                "game_date": DAY,
                "team": team,
                "start_utc": start,
                "observed_utc": before(start, minutes),
                "starter_id": starter,
            }
            for game_id, team, start, minutes, starter in rows
        ],
        schema={
            "game_id": pl.Int64,
            "game_date": pl.Date,
            "team": pl.String,
            "start_utc": pl.Datetime("us", "UTC"),
            "observed_utc": pl.Datetime("us", "UTC"),
            "starter_id": pl.Int64,
        },
    )


def dfo_polls(rows: list[tuple[str, datetime, int, str | None, str | None]]) -> pl.DataFrame:
    """(team, start, minutes before it, goalie, status) per poll."""
    return pl.DataFrame(
        [
            {
                "game_date": DAY,
                "team": team,
                "start_utc": start,
                "observed_utc": before(start, minutes),
                "goalie_name": goalie,
                "status": status,
            }
            for team, start, minutes, goalie, status in rows
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


def team(frame: pl.DataFrame, code: str) -> dict[str, object]:
    return frame.filter(pl.col("team") == code).row(0, named=True)


def test_starters_come_from_the_lineups_with_their_names_and_opponents() -> None:
    assert STARTERS.select("game_id", "team", "opponent", "starter_id", "starter_name").rows() == [
        (1, "MTL", "TOR", 10, "Jakub Dobeš"),
        (1, "TOR", "MTL", 20, "Anthony Stolarz"),
        (2, "EDM", "VAN", 30, "Tristan Jarry"),
        (2, "VAN", "EDM", 40, "Kevin Lankinen"),
    ]


def test_names_match_without_accents_case_or_punctuation() -> None:
    assert goalies.name_key("Jakub Dobeš") == goalies.name_key("jakub dobes")
    assert goalies.name_key("J.-F. Bérubé") == goalies.name_key("JF Berube")
    assert goalies.name_key(None) is None


def test_each_source_s_first_pick_and_last_poll_are_judged_against_the_starter() -> None:
    nhl = nhl_polls(
        [
            (1, "MTL", START, 300, None),
            (1, "MTL", START, 20, 10),
            (1, "TOR", START, 300, None),
            (1, "TOR", START, 20, None),
        ]
    )
    dfo = dfo_polls(
        [
            # A wrong Likely report, then the right goalie Confirmed: a changed pick.
            ("MTL", START, 300, "Sam Montembeault", "Likely"),
            ("MTL", START, 20, "Jakub Dobes", "Confirmed"),
            ("TOR", START, 300, "Anthony Stolarz", None),
            ("TOR", START, 20, "Anthony Stolarz", "Likely"),
        ]
    )
    frame = goalies.team_games(STARTERS, nhl, dfo)
    mtl = team(frame, "MTL")
    assert (mtl["nhl_polls"], mtl["nhl_last_min"], mtl["nhl_named_min"]) == (2, 20, 20)
    assert (mtl["nhl_named_right"], mtl["nhl_last_right"]) == (True, True)
    assert (mtl["dfo_reported_min"], mtl["dfo_reported_right"]) == (300, False)
    assert (mtl["dfo_confirmed_min"], mtl["dfo_confirmed_right"]) == (20, True)
    assert mtl["dfo_last_right"] is True
    tor = team(frame, "TOR")
    # The NHL named nobody, and Daily Faceoff's name without a status is not yet a report.
    assert (tor["nhl_named_min"], tor["nhl_last_right"]) == (None, None)
    assert (tor["dfo_reported_min"], tor["dfo_confirmed_min"]) == (20, None)
    assert tor["dfo_last_right"] is True
    # Game 2 was never polled, but it came after the first poll, so it counts.
    assert set(frame.filter(pl.col("game_id") == 2)["nhl_polls"]) == {0}


def test_games_before_the_first_poll_are_left_out() -> None:
    earlier = GAMES.with_columns(game_date=pl.lit(DAY - timedelta(days=1)))
    starters = goalies.starters(earlier, LINEUPS, PLAYERS)
    assert goalies.team_games(
        starters, nhl_polls([]), dfo_polls([("MTL", START, 20, "X", None)])
    ).is_empty()
    assert goalies.team_games(STARTERS, nhl_polls([]), dfo_polls([])).is_empty()


def test_late_or_missing_polls_and_wrong_last_picks_are_problems() -> None:
    nhl = nhl_polls([(1, "MTL", START, 90, 11), (1, "TOR", START, 90, None)])
    dfo = dfo_polls(
        [
            ("MTL", START, 90, "Jakub Dobes", "Confirmed"),
            ("TOR", START, 90, "Joseph Woll", "Likely"),
        ]
    )
    found = goalies.problems(goalies.team_games(STARTERS, nhl, dfo))
    assert found == [
        "2026-10-03 MTL and TOR (1): last goalie poll 90 minutes before the start",
        "2026-10-03 EDM and VAN (2): no goalie poll",
        "2026-10-03 MTL (1): the NHL's last pre-game starter was 11, Jakub Dobeš (10) started",
        "2026-10-03 TOR (1): Daily Faceoff's last pick was Joseph Woll, Anthony Stolarz started",
    ]


def test_the_report_says_when_the_nhl_named_no_starter() -> None:
    dfo = dfo_polls([("MTL", START, 20, "Jakub Dobes", "Confirmed")])
    frame = goalies.team_games(STARTERS, nhl_polls([(1, "MTL", START, 20, None)]), dfo)
    text = goalies.markdown_report(frame)
    assert "| NHL starter flag | 0 of 4 | - | 0 of 0 | 0 of 0 |" in text
    assert "| Daily Faceoff, Confirmed | 1 of 4 | 20 | 1 of 1 | - |" in text
    assert "named no starter before any start" in text
    assert "Games with a poll in the last 60 minutes before the start: 1 of 2." in text
