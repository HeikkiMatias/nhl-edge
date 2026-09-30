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
    REMOVED,
    GameDiff,
    diff_game,
    due_dates,
    recheck,
)
from nhl_edge.ingest.nhl_api import FEED_KINDS, NhlApi
from nhl_edge.lake.raw import RawStore

# The nightly copy of the opening week's feeds, the morning after its last game.
FIRST = datetime(2010, 10, 9, 9, 0, tzinfo=UTC)
GAME = OPENING_WEEK_GAMES[0]


def games() -> pl.DataFrame:
    return pl.DataFrame([games_row(game_id) for game_id in OPENING_WEEK_GAMES])


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


def test_due_dates_are_the_last_game_dates_a_week_old() -> None:
    now = datetime(2010, 10, 15, 9, 30, tzinfo=UTC)
    assert due_dates(now, 3) == [date(2010, 10, 6), date(2010, 10, 7), date(2010, 10, 8)]


def test_a_recheck_is_stored_apart_and_reused(tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    store_first_copies(store)
    first = store.latest(f"nhl/play-by-play/20102011/{GAME}")
    now = FIRST + timedelta(days=7, minutes=5)
    server = Server()
    summary = recheck(api(store, server, now), games(), now, 3)
    assert (summary.games, summary.fetched, summary.reused) == (3, 9, 0)
    assert server.calls == {"play-by-play": 3, "boxscore": 3, "shiftcharts": 3}
    # The tables' copy stays the newest under its own kind.
    assert store.latest(f"nhl/play-by-play/20102011/{GAME}") == first
    assert store.latest(f"nhl/play-by-play-recheck/20102011/{GAME}") is not None
    again = recheck(api(store, server, now + timedelta(days=1)), games(), now, 3)
    assert (again.fetched, again.reused) == (0, 9)
    assert sum(server.calls.values()) == 9


def test_a_game_is_rechecked_only_a_week_after_the_tables_copy(tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    store_first_copies(store)
    early = FIRST + timedelta(days=6, hours=23)
    server = Server()
    summary = recheck(api(store, server, early), games(), early, 3)
    assert summary.games == 0
    assert sorted(summary.not_due) == sorted(OPENING_WEEK_GAMES)
    assert not server.calls
    # A game never ingested has no copy to compare with.
    empty = RawStore(tmp_path / "empty")
    later = FIRST + timedelta(days=8)
    assert len(recheck(api(empty, server, later), games(), later, 5).never_ingested) == 3


def credit_first_goal_to(player_id: int) -> Any:
    def edit(body: bytes) -> bytes:
        data = json.loads(body)
        goal = next(play for play in data["plays"] if play["typeDescKey"] == "goal")
        goal["details"]["scoringPlayerId"] = player_id
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
    now = FIRST + timedelta(days=8)
    recheck(api(store, Server(edit), now), games(), now, 5)
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
    assert found[("shots", "shooter_id")] == 1
    assert found[("shots", REMOVED)] == 1
    assert found[("shots", CHANGED)] == 1
    assert ("shots", ADDED) not in found
    assert found[("actual_lineups", "toi_s")] == 1
    assert ("shifts", CHANGED) not in found


def test_a_game_without_its_recheck_is_not_compared(tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    store_first_copies(store)
    assert diff_game(store, games_row(GAME)) is None


def test_the_report_lists_what_more_than_the_scorer_changed(tmp_path: Path) -> None:
    only_scorer = rechecked(tmp_path / "a", {"play-by-play": credit_first_goal_to(8_000_001)})
    more = rechecked(tmp_path / "b", {"boxscore": change_first_forwards_toi})
    assert only_scorer is not None and more is not None
    moved = GameDiff(2026020001, only_scorer.rows, only_scorer.changes)
    toi = GameDiff(2026020002, more.rows, more.changes)
    result = audit.Corrections(due=[2026020001, 2026020002, 2026020003], diffs=[moved, toi])
    report = audit.markdown_report(result)
    assert "| shots | 2 |" in report and "| actual_lineups | toi_s | 1 | 1 |" in report
    assert audit.problems(result) == [
        "1 games due for a recheck have none, e.g. 2026020003",
        # A player's time on ice no longer adds up to his shifts, so the chart is incomplete.
        "2026020002: more than the scorer changed: actual_lineups toi_s (1 rows), "
        "shift_coverage players_toi_off (1 rows), shift_coverage complete (1 rows)",
    ]


def test_only_live_games_a_week_and_a_night_old_are_due(tmp_path: Path) -> None:
    live = games().with_columns(
        season=pl.lit(20262027, pl.Int64),
        game_date=pl.lit(date(2026, 9, 29)),
    )
    store = RawStore(tmp_path)
    result = audit.corrections(store, pl.concat([games(), live]), date(2026, 10, 6))
    assert result.due == []
    result = audit.corrections(store, pl.concat([games(), live]), date(2026, 10, 7))
    assert sorted(result.due) == sorted(OPENING_WEEK_GAMES) and result.diffs == []
    assert audit.markdown_report(audit.Corrections([], [])).startswith("No live-season game")
