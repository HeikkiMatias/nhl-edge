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
        "role": ["G", "G", "G", "G", "G"],
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


def test_names_match_without_accents_case_punctuation_or_spaces() -> None:
    assert goalies.name_key("Jakub Dobeš") == goalies.name_key("jakub dobes")
    assert goalies.name_key("J.-F. Bérubé") == goalies.name_key("JF Berube")
    assert goalies.name_key("Ukko-Pekka Luukkonen") == goalies.name_key("Ukko Pekka Luukkonen")
    assert goalies.name_key(None) is None


def test_each_daily_faceoff_listing_keeps_its_own_polls() -> None:
    # A stale listing of MTL three hours earlier on the same date names the wrong goalie at its
    # later polls. The game at START takes its own listing, and its picks are right.
    stale = START - timedelta(hours=3)
    dfo = dfo_polls(
        [
            ("MTL", stale, 200, "Jakub Dobeš", "Likely"),
            ("MTL", stale, 10, "Sam Montembeault", "Likely"),
            ("MTL", START, 60, "Jakub Dobeš", "Confirmed"),
            ("MTL", START, 20, "Jakub Dobeš", "Confirmed"),
        ]
    )
    frame = goalies.team_games(STARTERS, nhl_polls([]), dfo)
    assert frame.height == STARTERS.height
    mtl = team(frame, "MTL")
    assert (mtl["dfo_polls"], mtl["dfo_last_min"], mtl["dfo_last_right"]) == (2, 20, True)
    assert (mtl["dfo_reported_min"], mtl["dfo_confirmed_min"]) == (60, 60)


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


def test_the_calls_at_1245_et_give_each_sources_share_right() -> None:
    # 12:45 EDT on 2026-10-03 is 16:45 UTC, 375 minutes before the 23:00 start and 495 before
    # the late one's. Dobeš is confirmed and starts; Woll is likely and doesn't; Jarry is likely
    # and starts; VAN has no call. A Confirmed after 12:45 ET isn't the call at the decision.
    frame = goalies.at_decision(
        STARTERS,
        nhl_polls([]),
        dfo_polls(
            [
                ("MTL", START, 400, "Jakub Dobeš", "Confirmed"),
                ("TOR", START, 400, "Joseph Woll", "Likely"),
                ("TOR", START, 300, "Anthony Stolarz", "Confirmed"),
                ("EDM", LATE, 600, "Tristan Jarry", "Likely"),
            ]
        ),
    )
    assert frame.height == 4
    likely = goalies.decision_share(frame, pl.col("dfo_status") == "Likely", "dfo_right")
    assert likely is not None and (likely.mean, likely.games) == (0.5, 2)
    report = goalies.decision_markdown(frame)
    assert "| Daily Faceoff, Confirmed | 1 | 1 | 1.000 [1.000, 1.000] |" in report
    assert "| Daily Faceoff, Likely (ADR 0037's L) | 2 | 1 |" in report
    assert "| NHL starter flag | 0 | - | - |" in report
    assert "| No call from either | 1 | - | - |" in report
    assert goalies.decision_problems(frame) == [
        f"{DAY} TOR (1): Daily Faceoff had Joseph Woll Likely at 12:45 ET, Anthony Stolarz started"
    ]


def test_a_game_starting_by_1245_et_has_no_call_at_the_decision() -> None:
    early = STARTERS.with_columns(start_utc=pl.lit(datetime(2026, 10, 3, 16, 30, tzinfo=UTC)))
    assert goalies.at_decision(early, nhl_polls([]), dfo_polls([])).is_empty()


def test_the_decision_is_the_dates_midday_snapshot_which_its_goalie_poll_precedes() -> None:
    # The midday dispatch polls the goalies, then snapshots the odds at 16:47 UTC: a Confirmed
    # polled at 16:46 is the call at the decision, though it is after 12:45:00 ET.
    snapshot = datetime(2026, 10, 3, 16, 47, tzinfo=UTC)
    odds = pl.DataFrame(
        {"slot": ["morning", "midday"], "snapshot_utc": [snapshot.replace(hour=11), snapshot]}
    )
    decisions = goalies.midday_snapshots(odds)
    assert decisions.rows() == [(DAY, snapshot)]
    polls = dfo_polls([("TOR", START, 374, "Anthony Stolarz", "Confirmed")])
    frame = goalies.at_decision(STARTERS, nhl_polls([]), polls, decisions)
    tor = team(frame, "TOR")
    assert (tor["moment_utc"], tor["dfo_status"], tor["dfo_right"]) == (snapshot, "Confirmed", True)
    # Without the snapshot, 12:45 ET stands and the poll comes too late.
    assert team(goalies.at_decision(STARTERS, nhl_polls([]), polls), "TOR")["dfo_status"] is None


def test_a_pick_with_another_first_name_is_the_starter_by_surname() -> None:
    # Daily Faceoff's Matthew Murray is the NHL's Matt Murray: the same surname is the starter,
    # as policy v2 matches him (ADR 0037).
    frame = goalies.team_games(
        STARTERS, nhl_polls([]), dfo_polls([("MTL", START, 30, "Jake Dobes", "Confirmed")])
    )
    mtl = team(frame, "MTL")
    assert (mtl["dfo_confirmed_right"], mtl["dfo_last_right"]) == (True, True)
    # When the team dressed another goalie of that surname, a surname alone names neither
    # (Codex on #239).
    twins = PLAYERS.with_columns(
        name=pl.when(pl.col("player_id") == 11).then(pl.lit("Jan Dobeš")).otherwise("name")
    )
    shared = goalies.team_games(
        goalies.starters(GAMES, LINEUPS, twins),
        nhl_polls([]),
        dfo_polls([("MTL", START, 30, "Jake Dobes", "Confirmed")]),
    )
    assert team(shared, "MTL")["dfo_confirmed_right"] is False
