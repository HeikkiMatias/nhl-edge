import json
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import polars as pl
from feed_fixtures import OPENING_WEEK_GAMES, feed, games_row

from nhl_edge.audit import corrections as audit
from nhl_edge.ingest import nhl_api
from nhl_edge.ingest.corrections import (
    ADDED,
    CHANGED,
    GOAL_SCORER,
    REMOVED,
    GameDiff,
    diff_game,
    due_window,
    first_fetches,
    recheck,
)
from nhl_edge.ingest.nhl_api import FEED_KINDS, NhlApi
from nhl_edge.lake.raw import RawStore

# The nightly copy of the opening week's feeds, the morning after its last game.
FIRST = datetime(2010, 10, 9, 9, 0, tzinfo=UTC)
GAME = OPENING_WEEK_GAMES[0]


def games() -> pl.DataFrame:
    return pl.DataFrame([games_row(game_id) for game_id in OPENING_WEEK_GAMES])


def fetched(at: datetime = FIRST) -> pl.DataFrame:
    """first_fetches of the opening week's lineups, their boxscores fetched at `at`."""
    lineups = pl.DataFrame(
        {
            "game_id": list(OPENING_WEEK_GAMES),
            "raw_key": [
                f"nhl/boxscore/20102011/{g}/{at:%Y%m%dT%H%M%SZ}" for g in OPENING_WEEK_GAMES
            ],
        }
    )
    return first_fetches(lineups)


def store_first_copies(store: RawStore, fetched: datetime = FIRST) -> None:
    """The tables' copy of every opening-week feed, as the nightly ingest stores it."""
    for game in games().iter_rows(named=True):
        for kind in FEED_KINDS:
            key = f"{kind}/{game['season']}/{game['game_id']}/{fetched:%Y%m%dT%H%M%SZ}"
            store.put("nhl", key, feed(kind, game["game_id"]), {"fetched_utc": fetched.isoformat()})


class Server:
    """Serves each game's feeds, rewritten by `edit` when given, and counts the requests."""

    def __init__(self, edit: dict[str, Any] | None = None) -> None:
        self.calls: Counter[str] = Counter()
        self.edit = edit or {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if "shiftcharts" in request.url.path:
            kind = "shiftcharts"
            game_id = int(request.url.params["cayenneExp"].removeprefix("gameId="))
        else:
            *_, game, kind = request.url.path.split("/")
            game_id = int(game)
        self.calls[kind] += 1
        body = feed(kind, game_id)
        if kind in self.edit:
            body = self.edit[kind](body)
        return httpx.Response(200, content=body)


def api(store: RawStore, server: Server, now: datetime) -> NhlApi:
    client = httpx.Client(transport=httpx.MockTransport(server), base_url=nhl_api.BASE_URL)
    return NhlApi(store, client, min_interval_s=0, now=lambda: now)


def test_the_due_window_reaches_back_n_days_and_the_charts_settling_time() -> None:
    now = datetime(2010, 10, 16, 9, 30, tzinfo=UTC)
    # A week before now, back three days, and three more for a shift chart fetched again later.
    assert due_window(now, 3) == (
        datetime(2010, 10, 3, 9, 30, tzinfo=UTC),
        datetime(2010, 10, 9, 9, 30, tzinfo=UTC),
    )
    stamps = fetched()["fetched_utc"].to_list()
    assert stamps == [FIRST] * len(OPENING_WEEK_GAMES)


def test_a_recheck_is_stored_apart_and_reused(tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    store_first_copies(store)
    first = store.latest(f"nhl/play-by-play/20102011/{GAME}")
    now = FIRST + timedelta(days=7, minutes=5)
    server = Server()
    summary = recheck(api(store, server, now), games(), fetched(), now, 3, first_season=20102011)
    assert (summary.games, summary.fetched, summary.reused) == (3, 9, 0)
    assert server.calls == {"play-by-play": 3, "boxscore": 3, "shiftcharts": 3}
    # The tables' copy stays the newest under its own kind.
    assert store.latest(f"nhl/play-by-play/20102011/{GAME}") == first
    assert store.latest(f"nhl/play-by-play-recheck/20102011/{GAME}") is not None
    again = recheck(
        api(store, server, now + timedelta(days=1)),
        games(),
        fetched(),
        now,
        3,
        first_season=20102011,
    )
    assert (again.fetched, again.reused) == (0, 9)
    assert sum(server.calls.values()) == 9


def test_a_game_is_rechecked_only_a_week_after_the_tables_copy(tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    store_first_copies(store)
    early = FIRST + timedelta(days=6, hours=23)
    server = Server()
    summary = recheck(
        api(store, server, early), games(), fetched(), early, 3, first_season=20102011
    )
    assert summary.games == 0 and not server.calls
    # A boxscore stamped just before the play-by-play: in the window, but the play-by-play the
    # tables read is not a week old yet.
    edge = FIRST + timedelta(days=7) - timedelta(minutes=30)
    near = fetched(FIRST - timedelta(hours=1))
    summary = recheck(api(store, server, edge), games(), near, edge, 3, first_season=20102011)
    assert sorted(summary.not_due) == sorted(OPENING_WEEK_GAMES) and not server.calls
    # A game never ingested has no copy to compare with.
    empty = RawStore(tmp_path / "empty")
    later = FIRST + timedelta(days=7, hours=1)
    assert (
        len(
            recheck(
                api(empty, server, later), games(), fetched(), later, 3, first_season=20102011
            ).never_ingested
        )
        == 3
    )


def test_only_live_seasons_are_rechecked_by_default(tmp_path: Path) -> None:
    # The backfill fetched every earlier game in September 2026; those are never rechecked.
    store = RawStore(tmp_path)
    store_first_copies(store)
    now = FIRST + timedelta(days=7, hours=1)
    server = Server()
    assert recheck(api(store, server, now), games(), fetched(), now, 3).games == 0
    assert not server.calls


def test_a_chart_fetched_again_later_sets_the_recheck_date(tmp_path: Path) -> None:
    # Codex on #80: the nightly lookback fetches an incomplete chart again, and the tables read
    # that newer copy, so the recheck waits until it too is a week old.
    store = RawStore(tmp_path)
    store_first_copies(store)
    chart = FIRST + timedelta(days=2)
    for game in games().iter_rows(named=True):
        key = f"shiftcharts/{game['season']}/{game['game_id']}/{chart:%Y%m%dT%H%M%SZ}"
        body = feed("shiftcharts", game["game_id"])
        store.put("nhl", key, body, {"fetched_utc": chart.isoformat()})
    server = Server()
    week = FIRST + timedelta(days=7, hours=1)
    summary = recheck(api(store, server, week), games(), fetched(), week, 3, first_season=20102011)
    assert len(summary.not_due) == 3 and not server.calls
    later = chart + timedelta(days=7, hours=1)
    summary = recheck(
        api(store, server, later), games(), fetched(), later, 3, first_season=20102011
    )
    assert (summary.games, summary.fetched) == (3, 9)


def test_a_game_ingested_nights_late_is_rechecked_a_week_after_its_copy(tmp_path: Path) -> None:
    # Codex on #80: chosen by game date, a game first ingested three nights late left the window
    # before its copy was a week old. Chosen by fetch time, it is rechecked on time.
    late = FIRST + timedelta(days=3)
    store = RawStore(tmp_path)
    store_first_copies(store, late)
    now = late + timedelta(days=7, minutes=5)
    summary = recheck(
        api(store, Server(), now), games(), fetched(late), now, 3, first_season=20102011
    )
    assert (summary.games, summary.fetched) == (3, 9)


def credit_first_goal_to(player_id: int) -> Any:
    def edit(body: bytes) -> bytes:
        data = json.loads(body)
        goal = next(play for play in data["plays"] if play["typeDescKey"] == "goal")
        goal["details"]["scoringPlayerId"] = player_id
        return json.dumps(data).encode()

    return edit


def credit_first_saved_shot_to(player_id: int) -> Any:
    def edit(body: bytes) -> bytes:
        data = json.loads(body)
        shot = next(play for play in data["plays"] if play["typeDescKey"] == "shot-on-goal")
        shot["details"]["shootingPlayerId"] = player_id
        return json.dumps(data).encode()

    return edit


def drop_first_shot_and_credit_a_goal(body: bytes) -> bytes:
    data = json.loads(credit_first_goal_to(8_000_001)(body))
    shot = next(i for i, play in enumerate(data["plays"]) if play["typeDescKey"] == "shot-on-goal")
    del data["plays"][shot]
    return json.dumps(data).encode()


def change_first_forwards_toi(body: bytes) -> bytes:
    data = json.loads(body)
    data["playerByGameStats"]["homeTeam"]["forwards"][0]["toi"] = "01:00"
    return json.dumps(data).encode()


def rechecked(tmp_path: Path, edit: dict[str, Any]) -> GameDiff | None:
    store = RawStore(tmp_path)
    store_first_copies(store)
    now = FIRST + timedelta(days=7, hours=1)
    recheck(api(store, Server(edit), now), games(), fetched(), now, 3, first_season=20102011)
    return diff_game(store, games_row(GAME))


def counts(diff: GameDiff | None) -> dict[tuple[str, str], int]:
    assert diff is not None
    return {(table, name): rows for table, name, rows in diff.changes.rows()}


def test_an_unchanged_recheck_has_no_difference(tmp_path: Path) -> None:
    diff = rechecked(tmp_path, {})
    assert diff is not None and diff.changes.is_empty()
    assert diff.rows["shots"] > 0 and diff.rows["shift_coverage"] == 1


def test_a_diff_finds_a_moved_scorer_a_removed_shot_and_a_changed_time_on_ice(
    tmp_path: Path,
) -> None:
    edits = {
        "play-by-play": drop_first_shot_and_credit_a_goal,
        "boxscore": change_first_forwards_toi,
    }
    found = counts(rechecked(tmp_path, edits))
    assert found[("shots", GOAL_SCORER)] == 1
    assert ("shots", "shooter_id") not in found
    assert found[("shots", REMOVED)] == 1
    assert ("shots", ADDED) not in found
    # The corrected time on ice makes the chart incomplete, so every shot's skater counts fall
    # back from the chart to situationCode (ADR 0009): the diff shows that too.
    assert found[("shift_coverage", "complete")] == 1
    flipped = found[("shots", "strength_source")]
    assert flipped > 1 and found[("shots", CHANGED)] == flipped
    assert found[("actual_lineups", "toi_s")] == 1
    assert ("shifts", CHANGED) not in found


def test_a_game_without_its_recheck_is_not_compared(tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    store_first_copies(store)
    assert diff_game(store, games_row(GAME)) is None


def test_the_report_lists_what_more_than_the_scorer_changed(tmp_path: Path) -> None:
    only_scorer = rechecked(tmp_path / "a", {"play-by-play": credit_first_goal_to(8_000_001)})
    more = rechecked(tmp_path / "b", {"boxscore": change_first_forwards_toi})
    shooter = rechecked(tmp_path / "c", {"play-by-play": credit_first_saved_shot_to(8_000_001)})
    assert only_scorer is not None and more is not None and shooter is not None
    moved = GameDiff(2026020001, only_scorer.rows, only_scorer.changes)
    toi = GameDiff(2026020002, more.rows, more.changes)
    saved = GameDiff(2026020004, shooter.rows, shooter.changes)
    due = [2026020001, 2026020002, 2026020003, 2026020004]
    result = audit.Corrections(due=due, diffs=[moved, toi, saved])
    report = audit.markdown_report(result)
    assert "| shots | 3 |" in report and "| actual_lineups | toi_s | 1 | 1 |" in report
    assert (
        "| shots | goal scorer | 1 | 1 |" in report and "| shots | shooter_id | 1 | 1 |" in report
    )
    assert audit.problems(result) == [
        "1 games due for a recheck have none, e.g. 2026020003",
        # A player's time on ice no longer adds up to his shifts, so the chart is incomplete.
        # ... which moves every shot's skater counts back to situationCode (ADR 0009).
        "2026020002: more than the scorer changed: shots strength_source (77 rows), "
        "actual_lineups toi_s (1 rows), shift_coverage players_toi_off (1 rows), "
        "shift_coverage complete (1 rows), strength_time row added (10 rows), "
        "strength_time row removed (12 rows)",
        # Only a goal's scorer is exempt: a saved shot credited to another shooter is listed.
        "2026020004: more than the scorer changed: shots shooter_id (1 rows)",
    ]


def test_only_live_games_a_week_and_a_night_after_their_copy_are_due(tmp_path: Path) -> None:
    live = games().with_columns(
        season=pl.lit(20262027, pl.Int64),
        game_date=pl.lit(date(2026, 9, 29)),
    )
    copy = datetime(2026, 9, 30, 10, 30, tzinfo=UTC)
    lineups = pl.concat(
        [
            pl.DataFrame(
                {
                    "game_id": list(OPENING_WEEK_GAMES),
                    "season": [season] * 3,
                    "raw_key": [
                        f"nhl/boxscore/{season}/{g}/{at:%Y%m%dT%H%M%SZ}" for g in OPENING_WEEK_GAMES
                    ],
                }
            )
            for season, at in ((20262027, copy), (20102011, FIRST))
        ]
    )
    both = pl.concat([games(), live])
    store = RawStore(tmp_path)
    # Due 2026-10-07 10:30 UTC, so the nightly of 2026-10-08 rechecks it: not missing on 10-06.
    assert audit.corrections(store, both, lineups, date(2026, 10, 6)).due == []
    result = audit.corrections(store, both, lineups, date(2026, 10, 7))
    assert sorted(result.due) == sorted(OPENING_WEEK_GAMES) and result.diffs == []
    assert audit.markdown_report(audit.Corrections([], [])).startswith("No live-season game")
