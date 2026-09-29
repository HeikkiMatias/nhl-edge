"""The SBR odds archive (#7): opening and closing moneylines, the puck line and totals for
2010-11 to 2022-23, the free historical market for E1 (closes) and E2 (openers).

Each season is one HTML page under /scoresoddsarchives/; the Excel files the plan mentions are
gone. The site answers 404 to a request without a browser User-Agent, so the client sends one.
Every page is stored untouched under sbr/<season>/<fetch stamp> before it is parsed, and a stored
page is reused, since the archive is no longer updated; --replay never touches the network.

A page is a table of two rows per game, visitor (V) then home (H), or two N rows at a neutral
site (once, in 2019-20, H then V). Each row has the date (MMDD), rotation number, team, goals by
period and final (OT and shootout included), the opening and closing moneyline, from 2014-15 the
closing puck line (the team's handicap and price), and the opening and closing total (line and
price). The first row's total price is the over and the second's the under: when the first is the
favourite, the over lands 53% of the time against 47% when the second is (13,733 games of 2010-11
to 2021-22, pushes left out).

Rows are matched to NHL regular-season games through the schedule table on game date and the pair
of teams; the schedule, not SBR's V and H, says which team was home. Playoff rows and rows that
match no game are counted in the report and left out of the table, which needs a start time.
"""

import time
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from datetime import time as clock
from html.parser import HTMLParser
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import polars as pl

from nhl_edge.ingest.nhl_api import NhlApiError, NotCachedError, parse_utc, utc_now
from nhl_edge.lake.raw import RawStore
from nhl_edge.lake.schemas import SbrOdds, dtypes

BASE_URL = "https://www.sportsbookreviewsonline.com"
SOURCE = "sbr"
# Any current desktop browser string works; the site rejects the default httpx one with a 404.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)
MIN_INTERVAL_S = 2.0
BACKOFF_S = (5.0, 10.0, 20.0)
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
ET = ZoneInfo("America/New_York")

# The archive's page per season. 2020-21 is the one page named after a single year.
SEASON_PAGES = {
    20102011: "nhl-odds-2010-11",
    20112012: "nhl-odds-2011-12",
    20122013: "nhl-odds-2012-13",
    20132014: "nhl-odds-2013-14",
    20142015: "nhl-odds-2014-15",
    20152016: "nhl-odds-2015-16",
    20162017: "nhl-odds-2016-17",
    20172018: "nhl-odds-2017-18",
    20182019: "nhl-odds-2018-19",
    20192020: "nhl-odds-2019-20",
    20202021: "nhl-odds-2021",
    20212022: "nhl-odds-2021-22",
    20222023: "nhl-odds-2022-23",
}
SBR_SEASONS = tuple(SEASON_PAGES)

# SBR team names to NHL triCodes, matched after sbr_key(): SBR mostly drops spaces, not always.
SBR_TEAMS = {
    "anaheim": "ANA",
    "arizona": "ARI",
    "arizonas": "ARI",
    "atlanta": "ATL",
    "boston": "BOS",
    "buffalo": "BUF",
    "calgary": "CGY",
    "carolina": "CAR",
    "chicago": "CHI",
    "colorado": "COL",
    "columbus": "CBJ",
    "dallas": "DAL",
    "detroit": "DET",
    "edmonton": "EDM",
    "florida": "FLA",
    "losangeles": "LAK",
    "minnesota": "MIN",
    "montreal": "MTL",
    "nashville": "NSH",
    "newjersey": "NJD",
    "nyislanders": "NYI",
    "nyrangers": "NYR",
    "ottawa": "OTT",
    "philadelphia": "PHI",
    "phoenix": "PHX",
    "pittsburgh": "PIT",
    "sanjose": "SJS",
    "seattle": "SEA",
    "seattlekraken": "SEA",
    "stlouis": "STL",
    "tampa": "TBL",
    "tampabay": "TBL",
    "toronto": "TOR",
    "vancouver": "VAN",
    "vegas": "VGK",
    "washington": "WSH",
    "winnipeg": "WPG",
    "winnipegjets": "WPG",
}

# When a price counts as observed (hard rule 1). SBR gives no time for either line. The close is
# the last price before the start. The opener is taken as public at 10:00 US Eastern on the game
# date, or at the start when that is earlier (proposed in #7, pending its ADR).
OPEN_PUBLIC_AT_ET = clock(10, 0)


def sbr_key(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


def team_code(name: str) -> str:
    try:
        return SBR_TEAMS[sbr_key(name)]
    except KeyError:
        raise ValueError(f"unknown SBR team name {name!r}") from None


def american_to_decimal(price: int) -> float:
    """American odds to decimal: +150 is 2.5, -150 is 1.667. Both +100 and -100 are 2.0."""
    if -100 < price < 100:
        raise ValueError(f"American odds must be at most -100 or at least +100, got {price}")
    return 1 + (price / 100 if price > 0 else 100 / -price)


def open_observed_utc(game_date: date, start_utc: datetime) -> datetime:
    public = datetime.combine(game_date, OPEN_PUBLIC_AT_ET, tzinfo=ET).astimezone(UTC)
    return min(public, start_utc)


class _Rows(HTMLParser):
    """The text of every table cell, row by row."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._row is not None and self._cell is not None:
            self._row.append("".join(self._cell).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def table_rows(body: bytes) -> list[list[str]]:
    parser = _Rows()
    parser.feed(body.decode("utf-8", errors="replace"))
    return parser.rows


@dataclass(frozen=True)
class SbrGame:
    """One game as SBR lists it, its two teams in page order."""

    season: int
    game_date: date
    rotation: int
    neutral: bool
    teams: tuple[str, str]
    finals: tuple[int | None, int | None]
    # Each quote: (market, quote, team index or None for totals, side, line, American price),
    # only where both sides of the market have a price.
    quotes: tuple[tuple[str, str, int | None, str, float | None, int], ...]
    # Prices shown as NL, blank or malformed. The other side of each is left out too.
    missing: int


def _price(text: str) -> int | None:
    try:
        return int(text)
    except ValueError:
        return None


def _line(text: str) -> float | None:
    try:
        return float(text)
    except ValueError:
        return None


def _game_date(mmdd: str, season: int) -> date:
    """SBR dates have no year: October to December are in the season's first year, the rest in
    its second (the 2020 bubble playoffs ran to September 2020, in 2019-20's second year)."""
    month, day = int(mmdd[:-2]), int(mmdd[-2:])
    first_year = season // 10_000
    return date(first_year if month >= 10 else first_year + 1, month, day)


def parse_season(body: bytes, season: int) -> list[SbrGame]:
    """Every game on a season page, in page order. Raises on a layout it does not know."""
    rows = table_rows(body)
    if not rows or rows[0][:4] != ["Date", "Rot", "VH", "Team"]:
        raise ValueError(f"{season}: no SBR odds table in the page")
    header = rows[0]
    puck_line = "PuckLine" in header
    width = 16 if puck_line else 14
    games: list[SbrGame] = []
    data = rows[1:]
    if len(data) % 2:
        raise ValueError(f"{season}: odd number of team rows ({len(data)})")
    previous: date | None = None
    for first, second in zip(data[::2], data[1::2], strict=True):
        for row in (first, second):
            if len(row) != width:
                raise ValueError(f"{season}: expected {width} cells, got {row}")
        sides = (first[2], second[2])
        if sides not in (("V", "H"), ("H", "V"), ("N", "N")):
            raise ValueError(f"{season}: rows {first[:4]} and {second[:4]} are not one game")
        game_date = _game_date(first[0], season)
        if first[0] != second[0] or (previous is not None and game_date < previous):
            raise ValueError(f"{season}: dates out of order at {first[:4]}")
        previous = game_date
        quotes: list[tuple[str, str, int | None, str, float | None, int]] = []
        missing = 0
        for index, row in enumerate((first, second)):
            candidates: list[tuple[str, str, int | None, str, float | None, str]] = [
                ("h2h", "open", index, "", None, row[8]),
                ("h2h", "close", index, "", None, row[9]),
            ]
            totals_at = 10
            if puck_line:
                candidates.append(("spreads", "close", index, "", _line(row[10]), row[11]))
                totals_at = 12
            over_under = "over" if index == 0 else "under"
            for quote, at in (("open", totals_at), ("close", totals_at + 2)):
                candidates.append(("totals", quote, None, over_under, _line(row[at]), row[at + 1]))
            for market, quote, team, side, line, text in candidates:
                price = _price(text)
                if price is None or -100 < price < 100 or (market != "h2h" and line is None):
                    missing += 1
                    continue
                quotes.append((market, quote, team, side, line, price))
        # A price is only usable with the other side's: de-vigging needs both.
        pairs = Counter((market, quote) for market, quote, *_ in quotes)
        quotes = [q for q in quotes if pairs[(q[0], q[1])] == 2]
        games.append(
            SbrGame(
                season=season,
                game_date=game_date,
                rotation=int(first[1]),
                neutral=sides == ("N", "N"),
                teams=(team_code(first[3]), team_code(second[3])),
                finals=(_price(first[7]), _price(second[7])),
                quotes=tuple(quotes),
                missing=missing,
            )
        )
    return games


@dataclass
class SeasonReport:
    """How one season's SBR games joined the NHL's regular-season games."""

    season: int
    sbr_games: int = 0
    matched: int = 0
    after_regular_season: int = 0
    unmatched: list[tuple[date, str, str]] = field(default_factory=list)
    nhl_games: int = 0
    nhl_without_sbr: int = 0
    score_mismatches: list[tuple[int, str]] = field(default_factory=list)
    missing_prices: int = 0
    quotes: int = 0

    @property
    def join_rate(self) -> float:
        """The share of the NHL's regular-season games that have an SBR row."""
        return (self.nhl_games - self.nhl_without_sbr) / self.nhl_games if self.nhl_games else 0.0


def match_season(
    games: Sequence[SbrGame],
    schedule: pl.DataFrame,
    results: pl.DataFrame,
    raw_key: str,
) -> tuple[pl.DataFrame, SeasonReport]:
    """The season's quotes as SbrOdds rows, and how its games joined the schedule.

    A game matches the scheduled game on the same date with the same two teams, whichever SBR
    listed first. results (games) only checks SBR's final score against the NHL's, to catch a
    mis-mapped row; nothing from it goes into the table.
    """
    season = games[0].season if games else 0
    report = SeasonReport(season, sbr_games=len(games), nhl_games=schedule.height)
    by_teams: dict[tuple[date, frozenset[str]], tuple[int, datetime, str, str]] = {
        (game_date, frozenset((home, away))): (game_id, start, home, away)
        for game_id, game_date, start, home, away in schedule.select(
            "game_id", "game_date", "start_utc", "home", "away"
        ).iter_rows()
    }
    finals = {
        game_id: (home_score, away_score)
        for game_id, home_score, away_score in results.select(
            "game_id", "home_score", "away_score"
        ).iter_rows()
    }
    last_regular: date | None = schedule["game_date"].max() if schedule.height else None  # type: ignore[assignment]
    rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    for game in games:
        report.missing_prices += game.missing
        found = by_teams.get((game.game_date, frozenset(game.teams)))
        if found is None or found[0] in seen:
            if last_regular is not None and game.game_date > last_regular:
                report.after_regular_season += 1
            else:
                report.unmatched.append((game.game_date, *game.teams))
            continue
        game_id, start_utc, home, away = found
        seen.add(game_id)
        report.matched += 1
        home_index = game.teams.index(home)
        score = (game.finals[home_index], game.finals[1 - home_index])
        if game_id in finals and score != finals[game_id]:
            report.score_mismatches.append(
                (
                    game_id,
                    f"SBR {score[0]}-{score[1]}, NHL {finals[game_id][0]}-{finals[game_id][1]}",
                )
            )
        observed = {
            "open": open_observed_utc(game.game_date, start_utc),
            "close": start_utc,
        }
        for market, quote, team, side, line, price in game.quotes:
            if team is not None:
                side = "home" if team == home_index else "away"
            rows.append(
                {
                    "game_id": game_id,
                    "season": game.season,
                    "game_date": game.game_date,
                    "start_utc": start_utc,
                    "home": home,
                    "away": away,
                    "market": market,
                    "side": side,
                    "line": line,
                    "quote": quote,
                    "price_american": price,
                    "price_decimal": american_to_decimal(price),
                    "observed_utc": observed[quote],
                    "raw_key": raw_key,
                }
            )
    report.nhl_without_sbr = schedule.height - len(seen)
    report.quotes = len(rows)
    frame = pl.DataFrame(rows, schema=dtypes(SbrOdds))
    return frame, report


class SbrArchive:
    """Fetches season pages, storing each raw before returning it. A stored page is always
    reused: the archive stopped at 2022-23."""

    def __init__(
        self,
        store: RawStore,
        client: httpx.Client | None = None,
        *,
        offline: bool = False,
        min_interval_s: float = MIN_INTERVAL_S,
        backoff_s: Sequence[float] = BACKOFF_S,
        now: Callable[[], datetime] = utc_now,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.store = store
        self.client = client or httpx.Client(
            base_url=BASE_URL,
            headers={"User-Agent": BROWSER_USER_AGENT},
            timeout=60,
            follow_redirects=True,
        )
        self.offline = offline
        self.min_interval_s = min_interval_s
        self.backoff_s = tuple(backoff_s)
        self.now = now
        self.sleep = sleep
        self.monotonic = monotonic
        self._last_request = float("-inf")
        self.requests = 0

    def page(self, season: int) -> tuple[bytes, str]:
        """The season's page and its raw key."""
        prefix = f"{SOURCE}/{season}"
        cached = self.store.latest(prefix)
        if cached is not None:
            return self.store.get(cached), cached
        if self.offline:
            raise NotCachedError(f"{prefix} is not in the raw cache; run without --replay")
        path = f"/scoresoddsarchives/{SEASON_PAGES[season]}/"
        body, meta = self._get(path)
        fetched = parse_utc(meta["fetched_utc"])
        raw_key = self.store.put(SOURCE, f"{season}/{fetched:%Y%m%dT%H%M%SZ}", body, meta)
        return body, raw_key

    def _get(self, path: str) -> tuple[bytes, dict[str, Any]]:
        error = ""
        for attempt, backoff in enumerate((*self.backoff_s, None), start=1):
            wait = self._last_request + self.min_interval_s - self.monotonic()
            if wait > 0:
                self.sleep(wait)
            self._last_request = self.monotonic()
            self.requests += 1
            try:
                response = self.client.get(path)
            except httpx.TransportError as exc:
                error = type(exc).__name__
            else:
                if response.status_code not in RETRY_STATUS:
                    response.raise_for_status()
                    return response.content, {
                        "url": str(response.request.url),
                        "status": response.status_code,
                        "fetched_utc": self.now().isoformat(),
                        "content_type": response.headers.get("content-type", ""),
                        "attempts": attempt,
                    }
                error = f"status {response.status_code}"
            if backoff is None:
                break
            self.sleep(backoff)
        raise NhlApiError(f"GET {path} failed after {len(self.backoff_s) + 1} attempts: {error}")


def import_seasons(
    archive: SbrArchive,
    seasons: Iterable[int],
    schedule: pl.DataFrame,
    results: pl.DataFrame,
) -> tuple[dict[int, pl.DataFrame], list[SeasonReport]]:
    """Each season's SbrOdds rows and join report. The caller writes the frames."""
    frames: dict[int, pl.DataFrame] = {}
    reports: list[SeasonReport] = []
    for season in seasons:
        if season not in SEASON_PAGES:
            raise ValueError(f"SBR has no NHL odds page for {season}")
        body, raw_key = archive.page(season)
        games = parse_season(body, season)
        in_season = pl.col("season") == season
        frame, report = match_season(
            games, schedule.filter(in_season), results.filter(in_season), raw_key
        )
        frames[season] = SbrOdds.validate(frame)
        reports.append(report)
    return frames, reports


def report_lines(reports: Iterable[SeasonReport]) -> list[str]:
    """A Markdown table of the join per season, then every unmatched game and score mismatch."""
    lines = [
        "| season | SBR games | matched | after regular season | unmatched | NHL games "
        "| NHL games without SBR | join rate | score mismatches | missing prices |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    details: list[str] = []
    for r in reports:
        lines.append(
            f"| {r.season} | {r.sbr_games} | {r.matched} | {r.after_regular_season} "
            f"| {len(r.unmatched)} | {r.nhl_games} | {r.nhl_without_sbr} | {r.join_rate:.1%} "
            f"| {len(r.score_mismatches)} | {r.missing_prices} |"
        )
        details += [f"- {r.season} unmatched: {d} {a} and {b}" for d, a, b in r.unmatched]
        details += [
            f"- {r.season} score mismatch: game {g}, {text}" for g, text in r.score_mismatches
        ]
    return [*lines, "", *details] if details else lines
