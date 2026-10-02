"""Shifts from the NHL's HTML time-on-ice reports, for the games whose shift chart is empty (#68).

The shift chart API returns no shifts for 57 games of 2024-25, 2024021235 to 2024021291 (2025-04-08
to 2025-04-15). The NHL's time-on-ice reports hold the same shifts, one page per team: TH for the
home team, TV for the visitors (docs/data-sources.md). NHL.com's terms forbid unauthorized
automated harvesting, and the owner allowed one fetch of those 114 pages (2026-10-02). So
`nhl toi-reports` fetches only those games' pages, once, into the raw store (nhl/toi-home/ and
nhl/toi-visitor/), and `nhl ingest` builds a game's shifts from its two stored reports when its
chart has no shifts. The ingest never fetches a report.

A page lists every player of its team who played: a heading with his sweater number and name
("4 BYRAM, BOWEN"), one row per shift (shift number, period, start and end as "elapsed / remaining"
period clocks, duration and a goal or penalty mark), then a summary by period, which is not read.
The sweater number maps to the player id through the game's boxscore (actual_lineups), which gives
each dressed player's number in that game. The shifts then go through the shift chart's own row
checks (shifts.shift_rows), so they are kept, dropped or counted as bad exactly as a chart's are.
These rows also count as bad:
- a clock that is not "m:ss / m:ss", or whose elapsed and remaining time do not add up to the period
- a period other than 1 to 3 or OT
- every shift of a player whose sweater number is not in his team's lineup (or is there twice), or
  whose heading has no number

A bad row makes the game's coverage incomplete (shift_coverage). An unmapped number or a heading
without one is also returned as a problem, which the ingest prints, so no player is dropped
silently. A page of another game or of the other team is an error.
"""

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

import polars as pl

from nhl_edge.ingest.feeds import FeedGame
from nhl_edge.ingest.nhl_api import SHIFT, SOURCE, TOI_KINDS, NhlApi, NotFoundError
from nhl_edge.ingest.shifts import ShiftDrops, shift_rows
from nhl_edge.lake.raw import RawStore
from nhl_edge.lake.schemas import OT_PERIOD, OT_S, PERIOD_S, Shifts, dtypes

# The owner allowed one fetch of these games' reports on 2026-10-02 (#68), and of no others:
# NHL.com's terms forbid unauthorized automated harvesting. Another game needs a new decision.
ALLOWED_GAMES = range(2024021235, 2024021292)
# The page title ends with the side it covers.
TITLES = {"home": "Time On Ice Report Home Team", "visitor": "Time On Ice Report Away Team"}
HEADING = re.compile(r"(\d{1,2})\s+(\S.*)")
GAME_NUMBER = re.compile(r"Game (\d{4})")
CLOCKS = re.compile(r"(\d{1,2}:\d{2})\s*/\s*(\d{1,2}:\d{2})")
# The period column reads 1, 2, 3 or OT: the only labels in the six pages checked (#68).
PERIODS = {"1": 1, "2": 2, "3": 3, "OT": OT_PERIOD}
# A shift row's cells: number, period, start, end, duration and the goal or penalty mark.
SHIFT_CELLS = 6
# The raw keys of home reports, which a game's shift_coverage row takes when its shifts come
# from the reports.
HOME_REPORTS = f"{SOURCE}/{TOI_KINDS['home']}/"


@dataclass(frozen=True)
class ReportPlayer:
    """One player's block of a report: his heading and the cells of each of his shift rows.
    sweater_number is None when the heading does not start with one."""

    sweater_number: int | None
    heading: str
    rows: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class TimeOnIceReport:
    """What one page says, before any lineup is read."""

    title: str
    game_number: int | None  # the game id's last four digits, from the page header
    team_name: str
    players: tuple[ReportPlayer, ...]

    @property
    def shift_count(self) -> int:
        return sum(len(player.rows) for player in self.players)


@dataclass(frozen=True)
class ReportShifts:
    """A game's shifts built from its two reports, the rows left out, and the problems found."""

    shifts: pl.DataFrame
    drops: ShiftDrops
    problems: tuple[str, ...]


class _Cells(HTMLParser):
    """The page title and every table row, each cell as its class and text. Tables nest (the
    period summary sits in a cell of the player's table), so rows and cells are stacks, and a
    cell's text is only the text directly in it."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title: list[str] = []
        self.rows: list[list[tuple[str, str]]] = []
        self._rows: list[list[tuple[str, str]]] = []
        self._cells: list[tuple[str, list[str]]] = []
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
        elif tag == "tr":
            self._rows.append([])
        elif tag in ("td", "th"):
            self._cells.append((dict(attrs).get("class") or "", []))
        elif tag == "br" and self._cells:
            self._cells[-1][1].append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag == "tr" and self._rows:
            self.rows.append(self._rows.pop())
        elif tag in ("td", "th") and self._cells:
            css, parts = self._cells.pop()
            if self._rows:
                self._rows[-1].append((css, " ".join("".join(parts).split())))

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title.append(data)
        elif self._cells:
            self._cells[-1][1].append(data)


def read_report(body: bytes) -> TimeOnIceReport:
    """One page into its players and their shift rows. A shift row has six cells, none of them a
    heading, and follows a player's heading; the period summaries have seven cells."""
    parser = _Cells()
    parser.feed(body.decode("utf-8", errors="replace"))
    parser.close()
    game_number, team_name = None, ""
    players: list[ReportPlayer] = []
    heading: tuple[int | None, str] | None = None
    rows: list[tuple[str, ...]] = []
    for row in parser.rows:
        classes = [css for css, _ in row]
        texts = [text for _, text in row]
        number = GAME_NUMBER.fullmatch(texts[0]) if len(row) == 1 else None
        if len(row) == 1 and "playerHeading" in classes[0]:
            if heading is not None:
                players.append(ReportPlayer(*heading, tuple(rows)))
            match = HEADING.fullmatch(texts[0])
            heading = (int(match[1]), match[2]) if match else (None, texts[0])
            rows = []
        elif len(row) == 1 and "teamHeading" in classes[0]:
            team_name = texts[0]
        elif number is not None and game_number is None:
            game_number = int(number[1])
        elif (
            heading is not None
            and len(row) == SHIFT_CELLS
            and not any("heading" in css for css in classes)
        ):
            rows.append(tuple(texts))
    if heading is not None:
        players.append(ReportPlayer(*heading, tuple(rows)))
    title = " ".join("".join(parser.title).split())
    return TimeOnIceReport(title, game_number, team_name, tuple(players))


def check_report(report: TimeOnIceReport, game_id: int, side: str) -> None:
    """Fails when the page is not the side's report of this game."""
    if report.title != TITLES[side]:
        raise ValueError(f"{side} report of {game_id} is titled {report.title!r}")
    if report.game_number != game_id % 10_000:
        raise ValueError(f"{side} report of {game_id} is for game {report.game_number}")


def elapsed_clock(cell: str, period: int) -> str | None:
    """The elapsed half of an "elapsed / remaining" clock, or None when the cell is malformed or
    its two halves do not add up to the period's length."""
    match = CLOCKS.fullmatch(cell)
    if match is None:
        return None
    length = OT_S if period == OT_PERIOD else PERIOD_S
    return match[1] if _seconds(match[1]) + _seconds(match[2]) == length else None


def _seconds(clock: str) -> int:
    minutes, seconds = clock.split(":")
    return int(minutes) * 60 + int(seconds)


def chart_row(
    cells: tuple[str, ...], game: FeedGame, team_id: int, player_id: int
) -> dict[str, Any]:
    """A report's shift row as the shift chart would list it, for shifts.shift_rows. A malformed
    number, period or clock becomes a missing value, which shift_rows counts as bad."""
    number, period_text, start, end, duration, _ = cells
    period = PERIODS.get(period_text, 0)
    return {
        "typeCode": SHIFT,
        "gameId": game.game_id,
        "teamId": team_id,
        "playerId": player_id,
        "shiftNumber": int(number) if number.isdigit() else None,
        "period": period,
        "startTime": elapsed_clock(start, period),
        "endTime": elapsed_clock(end, period),
        "duration": duration,
    }


def malformed_rows(report: TimeOnIceReport) -> int:
    """Shift rows whose number, period or clocks cannot be read, which the parser counts as bad."""
    return sum(
        not cells[0].isdigit()
        or cells[1] not in PERIODS
        or elapsed_clock(cells[2], PERIODS[cells[1]]) is None
        or elapsed_clock(cells[3], PERIODS[cells[1]]) is None
        for player in report.players
        for cells in player.rows
    )


def sweater_numbers(lineups: pl.DataFrame, team: str) -> dict[int, list[int]]:
    """The player ids dressed for a team under each sweater number: one, unless the boxscore
    repeats a number."""
    numbers: dict[int, list[int]] = {}
    dressed = lineups.filter(pl.col("team") == team, pl.col("sweater_number").is_not_null())
    for number, player_id in dressed.select("sweater_number", "player_id").iter_rows():
        numbers.setdefault(number, []).append(player_id)
    return numbers


def report_rows(
    report: TimeOnIceReport, side: str, game: FeedGame, lineups: pl.DataFrame, raw_key: str
) -> tuple[list[dict[str, Any]], ShiftDrops, list[str]]:
    """Shifts rows of one team's report, the rows left out and the problems found. A player is
    found by his sweater number in the team's lineup; when that fails, his shifts count as bad and
    the reason is a problem."""
    team, team_id = (game.home, game.home_id) if side == "home" else (game.away, game.away_id)
    numbers = sweater_numbers(lineups, team)
    chart: list[dict[str, Any]] = []
    unplaced = 0
    problems: list[str] = []
    for player in report.players:
        number = player.sweater_number
        ids = numbers.get(number, []) if number is not None else []
        if len(ids) == 1:
            chart += [chart_row(cells, game, team_id, ids[0]) for cells in player.rows]
            continue
        unplaced += len(player.rows)
        if number is None:
            reason = "has no sweater number"
        elif ids:
            reason = f"is the number of {len(ids)} players in the boxscore"
        else:
            reason = f"has a number not in {team}'s boxscore"
        problems.append(
            f"{side} report: {player.heading!r} {reason}; "
            f"{len(player.rows)} shifts counted as bad rows"
        )
    rows, drops = shift_rows(chart, game, raw_key)
    return rows, ShiftDrops(drops.dropped, drops.foreign, drops.bad + unplaced), problems


def parse_reports(
    reports: Mapping[str, tuple[bytes, str]], game: FeedGame, lineups: pl.DataFrame
) -> ReportShifts:
    """A game's shifts from its two reports, each (body, raw_key) by side ("home", "visitor"),
    validated against Shifts. Each row keeps its own report's raw_key."""
    rows: list[dict[str, Any]] = []
    dropped = foreign = bad = 0
    problems: list[str] = []
    for side in TOI_KINDS:
        body, raw_key = reports[side]
        report = read_report(body)
        check_report(report, game.game_id, side)
        kept, drops, found = report_rows(report, side, game, lineups, raw_key)
        rows += kept
        dropped, foreign, bad = dropped + drops.dropped, foreign + drops.foreign, bad + drops.bad
        problems += found
    frame = Shifts.validate(pl.DataFrame(rows, schema=dtypes(Shifts)))
    return ReportShifts(frame, ShiftDrops(dropped, foreign, bad), tuple(problems))


@dataclass(frozen=True)
class CachedReports:
    """Where the ingest finds time-on-ice reports: the raw store only, never the network. warn
    gets each problem the parser reports."""

    store: RawStore
    warn: Callable[[str], None]

    def find(self, season: int, game_id: int) -> dict[str, tuple[bytes, str]] | None:
        """Both of the game's reports, each (body, raw_key) by side, or None unless both are
        stored."""
        keys = {
            side: self.store.latest(f"{SOURCE}/{kind}/{season}/{game_id}")
            for side, kind in TOI_KINDS.items()
        }
        found = {side: key for side, key in keys.items() if key is not None}
        if len(found) != len(keys):
            return None
        return {side: (self.store.get(key), key) for side, key in found.items()}


def chartless_games(coverage: pl.DataFrame) -> list[tuple[int, int]]:
    """(season, game_id) of each game whose shift chart gave no shifts, by its shift_coverage
    row: no shift rows, or shifts already built from its reports."""
    from_reports = pl.col("raw_key").str.starts_with(HOME_REPORTS)
    games = coverage.filter((pl.col("shift_rows") == 0) | from_reports)
    return [(season, game_id) for season, game_id in games.select("season", "game_id").rows()]


@dataclass
class FetchSummary:
    """What one `nhl toi-reports` run did: the reports it needed, the ones it fetched, the players
    and shifts they list, and the problems seen in them."""

    reports: int = 0
    stored_before: int = 0
    fetched: int = 0
    players: int = 0
    shifts: int = 0
    problems: list[str] = field(default_factory=list)


def stored_reports(store: RawStore, games: Iterable[tuple[int, int]]) -> int:
    """How many of the games' reports are in the raw store already (or in R2, when mirrored)."""
    return sum(
        store.latest(f"{SOURCE}/{kind}/{season}/{game_id}") is not None
        for season, game_id in games
        for kind in TOI_KINDS.values()
    )


def fetch_reports(api: NhlApi, games: Iterable[tuple[int, int]]) -> FetchSummary:
    """Both reports of each game: the stored copy when there is one, otherwise fetched once and
    stored raw (NhlApi.toi_report). Refuses, before any request, a game outside ALLOWED_GAMES. Each
    page is read to check it is the right one and lists shifts that can be read."""
    wanted = sorted(games)
    outside = [game_id for _, game_id in wanted if game_id not in ALLOWED_GAMES]
    if outside:
        raise ValueError(
            f"{len(outside)} games are outside the owner's decision of 2026-10-02 (#68), which "
            f"allows {ALLOWED_GAMES.start} to {ALLOWED_GAMES.stop - 1} only: "
            f"{', '.join(map(str, outside))}"
        )
    summary = FetchSummary(reports=2 * len(wanted))
    summary.stored_before = stored_reports(api.store, wanted)
    for season, game_id in wanted:
        for side in TOI_KINDS:
            try:
                response = api.toi_report(season, game_id, side)
            except NotFoundError as exc:
                summary.problems.append(str(exc))
                continue
            summary.fetched += not response.cached
            report = read_report(response.body)
            try:
                check_report(report, game_id, side)
            except ValueError as exc:
                summary.problems.append(f"{response.raw_key}: {exc}")
                continue
            summary.players += len(report.players)
            summary.shifts += report.shift_count
            malformed = malformed_rows(report)
            if not report.shift_count or malformed:
                summary.problems.append(
                    f"{response.raw_key}: {report.shift_count} shifts, {malformed} unreadable"
                )
    return summary
