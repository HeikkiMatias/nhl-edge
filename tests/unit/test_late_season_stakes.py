"""scripts/late_season_stakes.py (#422): the lake's counts against the standings, the
elimination and clinch bounds in both playoff formats, the lineup surprise and the family's
decision, on SYNTHETIC data."""

from datetime import UTC, date, datetime, time, timedelta

import late_season_fixtures as f
import numpy as np
import polars as pl
import pytest

ls = f.load()
DAY = date(2014, 4, 1)
MODERN, SHORT = 20132014, 20122013


def test_team_games_give_points_and_numbers_in_order_of_start() -> None:
    games = f.games(
        [
            (2013020002, date(2013, 10, 3), "BOS", "TOR", 3, 2, "OT"),
            (2013020001, date(2013, 10, 1), "TOR", "BOS", 4, 1, "REG"),
            (2013020003, date(2013, 10, 5), "BOS", "MTL", 2, 3, "SO"),
            # A playoff game is no team-game of the regular season.
            (2013030111, date(2014, 4, 20), "BOS", "TOR", 1, 0, "REG"),
        ],
        MODERN,
    )
    rows = {(r["team"], r["game_id"]): r for r in ls.team_games(games).iter_rows(named=True)}
    assert len(rows) == 6
    assert (rows["TOR", 2013020001]["points"], rows["BOS", 2013020001]["points"]) == (2, 0)
    assert (rows["BOS", 2013020002]["points"], rows["TOR", 2013020002]["points"]) == (2, 1)
    assert (rows["MTL", 2013020003]["points"], rows["BOS", 2013020003]["points"]) == (2, 1)
    assert [rows["BOS", g]["number"] for g in (2013020001, 2013020002, 2013020003)] == [1, 2, 3]


def test_the_count_check_reads_games_through_the_date_and_finds_a_mismatch() -> None:
    games = f.games(
        [
            (2013020001, date(2013, 10, 1), "TOR", "BOS", 4, 1, "REG"),
            (2013020002, date(2013, 10, 2), "BOS", "TOR", 3, 2, "OT"),
        ],
        MODERN,
    )
    # Through 10-02: TOR 2 games, 3 points; BOS 2 games, 2 points. A date's games count.
    right = f.standings(
        [("TOR", "E", "A", 2, 3), ("BOS", "E", "A", 2, 2)], date(2013, 10, 2), MODERN
    )
    assert ls.count_check(games, right)["mismatches"] == 0
    early = f.standings(
        [("TOR", "E", "A", 1, 2), ("BOS", "E", "A", 1, 0)], date(2013, 10, 1), MODERN
    )
    assert ls.count_check(games, early)["mismatches"] == 0
    wrong = f.standings(
        [("TOR", "E", "A", 2, 4), ("BOS", "E", "A", 2, 2)], date(2013, 10, 2), MODERN
    )
    found = ls.count_check(games, wrong)
    assert found["mismatches"] == 1 and found["examples"][0]["team"] == "TOR"


def conference(other_fourth: int) -> pl.DataFrame:
    """One date of a two-division conference of the wild-card format. T (division A) has 70
    points after 80 games, a best case of 74. Four A teams and four M teams lie above that; the
    fourth M team has other_fourth points. A fifth M team lies far below."""
    rows = [
        ("TTT", "E", "A", 80, 70),
        ("AAA", "E", "A", 80, 80),
        ("ABB", "E", "A", 80, 79),
        ("ACC", "E", "A", 80, 78),
        ("ADD", "E", "A", 80, 76),
        ("MAA", "E", "M", 80, 90),
        ("MBB", "E", "M", 80, 85),
        ("MCC", "E", "M", 80, 80),
        ("MDD", "E", "M", 80, other_fourth),
        ("MEE", "E", "M", 80, 50),
    ]
    return f.standings(rows, DAY, MODERN)


def test_elimination_needs_the_division_places_and_the_wild_cards_surely_taken() -> None:
    # A's top three and its fourth, and M's fourth beyond M's top three: two beyond the division
    # places, the two wild cards, so T can't make it.
    out = {r["team"]: r for r in ls.flags(conference(77)).iter_rows(named=True)}
    assert out["TTT"]["eliminated"] and not out["TTT"]["clinched_bound"]
    # M's fourth no longer surely above T's best case: one wild card left open.
    out = {r["team"]: r for r in ls.flags(conference(74)).iter_rows(named=True)}
    assert not out["TTT"]["eliminated"]
    # A team far below everyone, with no games left, is eliminated; the leader is not.
    assert out["MEE"]["eliminated"] and not out["MAA"]["eliminated"]


def test_the_clinch_bound_needs_no_way_to_be_passed() -> None:
    rows = [("TOP", "E", "A", 78, 110)] + [
        (f"A{i}A", "E", "A" if i < 4 else "M", 78, 90) for i in range(8)
    ]
    out = {r["team"]: r for r in ls.flags(f.standings(rows, DAY, MODERN)).iter_rows(named=True)}
    # Every other team's best case is 94, under TOP's 110: TOP can't miss.
    assert out["TOP"]["clinched_bound"] and not out["TOP"]["clinched"]
    # Eight teams level at 90 with 4 games left: any of them can still be passed.
    assert not any(out[f"A{i}A"]["clinched_bound"] for i in range(8))


def test_the_2012_13_format_has_one_division_place_and_five_others() -> None:
    # Three divisions of a 48-game season. T has 30 points after 47 games, a best case of 32.
    rows = [("TTT", "E", "A", 47, 30), ("AAA", "E", "A", 47, 40)]
    rows += [(f"B{i}B", "E", "B", 47, 40) for i in range(3)]
    rows += [(f"C{i}C", "E", "C", 47, 40) for i in range(3)]
    # Above T: A's winner, then B's 2 and C's 2 beyond their winners, four of the five places.
    out = {r["team"]: r for r in ls.flags(f.standings(rows, DAY, SHORT)).iter_rows(named=True)}
    assert not out["TTT"]["eliminated"]
    rows.append(("ABB", "E", "A", 47, 40))
    out = {r["team"]: r for r in ls.flags(f.standings(rows, DAY, SHORT)).iter_rows(named=True)}
    assert out["TTT"]["eliminated"]
    # The same standings as an 82-game season leave T 35 games: nothing is settled.
    out = {r["team"]: r for r in ls.flags(f.standings(rows, DAY, MODERN)).iter_rows(named=True)}
    assert not out["TTT"]["eliminated"]


def test_the_api_flags_clinched_and_unknown_flags_stay_false() -> None:
    rows = [("BOS", "E", "A", 80, 110), ("TOR", "E", "A", 80, 60)]
    found = ls.flags(f.standings(rows, DAY, MODERN, {"BOS": "z", "TOR": "e"}))
    out = {r["team"]: r for r in found.iter_rows(named=True)}
    # The API's e is not what eliminated reads: that is our bound.
    assert out["BOS"]["clinched"] and not out["TOR"]["clinched"]


GAME, TEAM = 2013020100, "BOS"
PROJECTED = datetime.combine(DAY, time(14), UTC)


def one_game() -> pl.DataFrame:
    return f.games([(GAME, DAY, TEAM, "TOR", 3, 1, "REG")], MODERN)


def test_the_surprise_is_the_minutes_missing_beyond_what_the_projection_expected() -> None:
    lineups = f.lineups(
        [
            # m = minutes / p: 15, 10 and 10.
            (GAME, TEAM, 1, "F", 1.0, None, 15.0),
            (GAME, TEAM, 2, "D", 0.5, None, 5.0),
            (GAME, TEAM, 3, "F", 0.9, None, 9.0),
            (GAME, TEAM, 30, "G", None, 0.8, 0.0),
            (GAME, TEAM, 31, "G", None, 0.2, 0.0),
        ],
        PROJECTED,
    )
    actual = f.actual([(GAME, TEAM, 1, False), (GAME, TEAM, 2, False), (GAME, TEAM, 31, True)])
    flagged = f.flagged([(TEAM, DAY - timedelta(days=1), True, False)])
    row = ls.lineup_rows(lineups, actual, one_game(), flagged).row(0, named=True)
    # Skater 3 (10 minutes) missing, against 0.5 * 10 + 0.1 * 10 = 6 expected.
    assert row["missing"] == pytest.approx(10.0) and row["expected"] == pytest.approx(6.0)
    assert row["surprise"] == pytest.approx(4.0)
    # The projected starter (p 0.8) sat: 1 against 0.2 expected.
    assert row["goalie_surprise"] == pytest.approx(0.8)
    assert row["number"] == 1 and row["flagged"] == 1.0 and row["clinched"]


def late_rows(effect: float, seed: int = 1) -> pl.DataFrame:
    """SYNTHETIC step 2 rows over ten weeks: team-games 60 to 80, flagged from the 70th, with
    effect added to the flagged ones' surprise."""
    rng = np.random.default_rng(seed)
    rows = []
    for team in range(8):
        for number in range(60, 81):
            day = date(2014, 2, 1) + timedelta(days=(number - 60) * 3 + team % 3)
            flagged = number >= 70 and team < 4
            rows.append(
                {
                    "season": MODERN,
                    "team": f"T{team}T",
                    "game_date": day,
                    "number": number,
                    "surprise": rng.normal() + (effect if flagged else 0.0),
                    "goalie_surprise": rng.normal() * 0.1,
                    "clinched": flagged and team < 2,
                    "eliminated": flagged and team >= 2,
                    "flagged": float(flagged),
                    "clinched_f": float(flagged and team < 2),
                    "eliminated_f": float(flagged and team >= 2),
                }
            )
    return pl.DataFrame(rows, schema_overrides={"season": pl.Int32})


def test_step_two_tests_flagged_against_unflagged_late_team_games() -> None:
    found = ls.lineup_figures(late_rows(5.0))
    test = found["test"]
    assert test["mean"] == pytest.approx(5.0, abs=0.5) and test["low"] > 0
    assert test["p_value"] < 0.01
    # Only team-games after the 65th count: 15 per team.
    assert found["late_team_games"] == 8 * 15 and test["flagged"] == 4 * 11
    # Before any flag (games 50 to 65), the later-flagged teams don't differ.
    before = found["before_any_flag"]
    assert before["low"] < 0 < before["high"]
    null = ls.lineup_figures(late_rows(0.0, seed=2))["test"]
    assert null["low"] < 0 < null["high"]


def per_game(clinched_shift: float, eliminated_shift: float) -> tuple[pl.DataFrame, pl.DataFrame]:
    """SYNTHETIC yardstick rows of the three candidates over ten weeks, each candidate's move
    the baseline's plus a shift, and the games' flag columns."""
    rng = np.random.default_rng(5)
    days = [date(2014, 1, 6) + timedelta(days=d) for d in range(70)]
    base = rng.normal(0, 0.01, len(days))
    frames = []
    for name, shift in (
        (ls.BASE, 0.0),
        ("clinched", clinched_shift),
        ("eliminated", eliminated_shift),
    ):
        frames.append(
            pl.DataFrame(
                {
                    "candidate": [name] * len(days),
                    "game_id": list(range(len(days))),
                    "season": [MODERN] * len(days),
                    "game_date": days,
                    "rested": [d % 2 == 0 for d in range(len(days))],
                    "move": base + shift + rng.normal(0, 1e-4, len(days)),
                    "result": base * 0,
                },
                schema_overrides={"season": pl.Int32},
            )
        )
    frame = pl.DataFrame(
        {
            "game_id": list(range(len(days))),
            "clinched_diff": [1.0 if d % 5 == 0 else 0.0 for d in range(len(days))],
            "eliminated_diff": [-1.0 if d % 7 == 0 else 0.0 for d in range(len(days))],
        }
    )
    return pl.concat(frames), frame


def test_a_test_holds_only_when_it_passes_with_its_expected_sign() -> None:
    lineups = {"test": {"mean": 3.0, "low": 1.0, "high": 5.0, "p_value": 0.001}}
    rows, frame = per_game(-0.001, +0.001)
    found = ls.decide(lineups, rows, frame)["estimates"]
    assert found["lineups"]["holds"] and found["clinched"]["holds"]
    # A clear difference of the wrong sign passes Holm's step but doesn't hold.
    assert found["eliminated"]["p_value"] < 0.01 and not found["eliminated"]["holds"]
    assert found["clinched"]["flagged_games"] == 14
    wrong = {"test": {"mean": -3.0, "low": -5.0, "high": -1.0, "p_value": 0.001}}
    assert not ls.decide(wrong, rows, frame)["estimates"]["lineups"]["holds"]
    with pytest.raises(ValueError, match="step 2"):
        ls.decide({"test": None}, rows, frame)


def test_season_days_cover_every_date_from_first_to_last_regular_game() -> None:
    games = f.games(
        [
            (2013020001, date(2013, 10, 1), "TOR", "BOS", 4, 1, "REG"),
            (2013020002, date(2013, 10, 4), "BOS", "TOR", 3, 2, "OT"),
            (2013030111, date(2014, 4, 20), "BOS", "TOR", 1, 0, "REG"),
        ],
        MODERN,
    )
    assert ls.season_days(games, [MODERN]) == [date(2013, 10, d) for d in range(1, 5)]
    with pytest.raises(ValueError, match="no regular-season games"):
        ls.season_days(games, [20142015])


def checks(**changes: object) -> dict[str, object]:
    base: dict[str, object] = {
        "flags_lost": 0,
        "bound_before_api": 0,
        "clinched_missed_playoffs": 0,
        "eliminated_in_playoffs": 0,
        "api_lead_days": {"teams": 10, "median": 1.0, "max": 3},
    }
    return base | changes


def test_the_run_stops_on_a_check_that_must_be_zero_or_a_long_api_lead() -> None:
    ls.enforce(checks())
    for name in ("flags_lost", "bound_before_api", "clinched_missed_playoffs"):
        with pytest.raises(ValueError, match=name):
            ls.enforce(checks(**{name: 1}))
    ls.enforce(checks(api_lead_days={"teams": 1, "median": 14.0, "max": ls.MAX_API_LEAD_DAYS}))
    with pytest.raises(ValueError, match="leads our bound"):
        ls.enforce(checks(api_lead_days={"teams": 1, "median": 15.0, "max": 15}))


def test_flag_checks_count_a_clinch_that_disappears() -> None:
    days = [DAY + timedelta(days=d) for d in range(3)]
    rows = [
        f.standings([("BOS", "E", "A", 70 + d, 100)], day, MODERN, {"BOS": "x"} if d != 1 else {})
        for d, day in enumerate(days)
    ]
    flagged = ls.flags(pl.concat(rows))
    games = f.games([(2013030111, date(2014, 4, 20), "BOS", "TOR", 1, 0, "REG")], MODERN)
    found = ls.flag_checks(flagged, games)
    assert found["flags_lost"] == 1 and found["clinched_missed_playoffs"] == 0
