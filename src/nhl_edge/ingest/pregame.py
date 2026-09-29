"""Pre-game goalie poll (#42): what the NHL API says about each team's starting goalie before
puck drop, recorded at the odds slot times and shortly before each start, so the audit (#9, plan
section 10) can tell whether and how early starters are confirmed.

The ingest reads a game's boxscore only once the game is final, so nothing else records the
pre-game state, and it cannot be fetched after the fact. Each poll fetches the gamecenter boxscore,
landing and right-rail of every game in its window that has not started and stores them raw, under
their own kinds (nhl/pregame-boxscore/, nhl/pregame-landing/, nhl/pregame-right-rail/) keyed by
the game's US Eastern date and id, apart from the post-game nhl/boxscore/ cache: the ingest reuses
the newest cached boxscore of a game, and a pre-game copy there would stand in for the final one.

A pre-game boxscore has no playerByGameStats hours before the game, and the landing page lists
each team's goalies with season stats but flags no starter (checked on 2026-09-29, nine hours
before FLA at CAR). The boxscore's starter flag is the one signal parsed here; the landing and
right-rail copies are kept so a later check can look for others without having polled again.
"""

import json
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl

from nhl_edge.ingest.nhl_api import (
    PLAYOFFS,
    REGULAR_SEASON,
    NhlApi,
    NhlApiError,
    ScheduledGame,
    never,
    parse_utc,
)
from nhl_edge.lake.raw import SUFFIX, RawStore
from nhl_edge.lake.schemas import PregameGoalies, dtypes
from nhl_edge.lake.tables import Lake

ET = ZoneInfo("America/New_York")
BOXSCORE = "pregame-boxscore"
LANDING = "pregame-landing"
RIGHT_RAIL = "pregame-right-rail"
BOXSCORE_PREFIX = f"nhl/{BOXSCORE}"
# Games starting this far ahead are polled; the morning slot (07:05 ET) reaches every game of the
# day, the latest of which start around 22:30 ET.
POLL_HORIZON = timedelta(hours=18)
TABLE = "pregame_goalies"


def games_to_poll(
    games: Sequence[ScheduledGame], now: datetime, horizon: timedelta = POLL_HORIZON
) -> list[ScheduledGame]:
    """Regular-season and playoff games that start after now and within the horizon."""
    return sorted(
        (
            game
            for game in games
            if game.game_type in (REGULAR_SEASON, PLAYOFFS)
            and now < game.start_utc <= now + horizon
        ),
        key=lambda game: (game.start_utc, game.game_id),
    )


def entity(game: ScheduledGame) -> str:
    return f"{game.start_utc.astimezone(ET).date().isoformat()}/{game.game_id}"


def parse_pregame_goalies(body: bytes, observed_utc: datetime, raw_key: str) -> pl.DataFrame:
    """One row per team of a pre-game boxscore: how many goalies it lists and which one, if
    exactly one, carries the starter flag. observed_utc is the fetch time, when the state was
    public. A response fetched at or after the scheduled start is not pre-game and gives no rows."""
    data = json.loads(body)
    start_utc = parse_utc(data["startTimeUTC"])
    if observed_utc >= start_utc:
        return pl.DataFrame(schema=dtypes(PregameGoalies))
    stats: dict[str, Any] = data.get("playerByGameStats") or {}
    rows = []
    for side, is_home in (("homeTeam", True), ("awayTeam", False)):
        goalies = (stats.get(side) or {}).get("goalies") or []
        starters = [goalie["playerId"] for goalie in goalies if goalie.get("starter")]
        rows.append(
            {
                "season": data["season"],
                "game_date": date.fromisoformat(data["gameDate"]),
                "game_id": data["id"],
                "team": data[side]["abbrev"],
                "is_home": is_home,
                "start_utc": start_utc,
                "observed_utc": observed_utc,
                "game_state": data["gameState"],
                "goalies_listed": len(goalies),
                "starters_flagged": len(starters),
                "starter_id": starters[0] if len(starters) == 1 else None,
                "raw_key": raw_key,
            }
        )
    return PregameGoalies.validate(pl.DataFrame(rows, schema=dtypes(PregameGoalies)))


@dataclass
class PollReport:
    games: int = 0
    teams: int = 0
    flagged: int = 0
    failed: list[int] = field(default_factory=list)


def run_poll(
    *,
    nhl: NhlApi,
    now: datetime,
    echo: Callable[[str], None],
    horizon: timedelta = POLL_HORIZON,
) -> PollReport:
    """Fetch and store the pre-game boxscore, landing and right-rail of every game starting within
    the horizon, then parse the boxscores to report how many teams have a flagged starter. The raw
    copies are the record; the lake table is rebuilt from them by replay_pregame_goalies."""
    report = PollReport()
    games = games_to_poll(nhl.schedule(now.astimezone(ET).date()), now, horizon)
    if not games:
        echo(f"goalie poll: no NHL games starting within {horizon}, nothing fetched")
        return report
    frames = []
    for game in games:
        try:
            boxscore = nhl.fetch(
                BOXSCORE, entity(game), f"/v1/gamecenter/{game.game_id}/boxscore", never
            )
            for kind, page in ((LANDING, "landing"), (RIGHT_RAIL, "right-rail")):
                nhl.fetch(kind, entity(game), f"/v1/gamecenter/{game.game_id}/{page}", never)
        except NhlApiError as exc:
            echo(f"::warning::goalie poll: {game.away} at {game.home} ({game.game_id}): {exc}")
            report.failed.append(game.game_id)
            continue
        frames.append(parse_pregame_goalies(boxscore.body, boxscore.fetched_utc, boxscore.raw_key))
        report.games += 1
    rows = pl.concat(frames) if frames else pl.DataFrame(schema=dtypes(PregameGoalies))
    report.teams = rows.height
    report.flagged = rows.filter(pl.col("starter_id").is_not_null()).height
    echo(
        f"goalie poll: {report.games} games, a starter flagged for {report.flagged} of "
        f"{report.teams} teams; {len(report.failed)} failed"
    )
    return report


@dataclass
class ReplayReport:
    responses: int = 0
    rows: int = 0
    after_start: int = 0
    incomplete: list[str] = field(default_factory=list)
    dates: list[date] = field(default_factory=list)


def replay_pregame_goalies(
    store: RawStore, lake: Lake, dates: Collection[date] | None = None
) -> ReplayReport:
    """Rebuild pregame_goalies for the given game dates (every stored date when None) from the raw
    pre-game boxscores. Each requested date's partition is replaced, and deleted when the date has
    no rows, so a parser fix leaves nothing stale. Never calls the NHL API."""
    report = ReplayReport()
    frames = []
    root = store.base_dir / BOXSCORE_PREFIX
    day_dirs = sorted(root.iterdir()) if root.is_dir() else []
    for day_dir in day_dirs:
        try:
            day = date.fromisoformat(day_dir.name)
        except ValueError:
            continue
        if dates is not None and day not in dates:
            continue
        report.dates.append(day)
        for path in sorted(day_dir.rglob(f"*{SUFFIX}")):
            raw_key = path.relative_to(store.base_dir).as_posix().removesuffix(SUFFIX)
            if not (store.base_dir / f"{raw_key}.meta.json").exists():
                report.incomplete.append(raw_key)
                continue
            observed_utc = parse_utc(store.meta(raw_key)["fetched_utc"])
            frame = parse_pregame_goalies(store.get(raw_key), observed_utc, raw_key)
            report.responses += 1
            report.after_start += frame.is_empty()
            frames.append(frame)
    table = pl.concat(frames) if frames else pl.DataFrame(schema=dtypes(PregameGoalies))
    report.rows = table.height
    requested = sorted(dates) if dates is not None else report.dates
    lake.replace_dates(TABLE, table, requested)
    return report
