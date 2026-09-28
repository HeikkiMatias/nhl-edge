"""The Odds API: live NHL snapshots on the free tier (docs/data-sources.md, docs/plan.md section 3).

A call costs one credit per market per region and returns every listed game, so the slot plan
spends credits on the markets that matter at each time of day and skips days without games. Slots
are defined in US Eastern time. The workflow has one cron line per slot for each UTC offset, and
resolve_slot maps the line that fired to a slot, which keeps the plan right across DST changes.
"""

import json
import os
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import polars as pl

from nhl_edge.ingest.nhl_api import PLAYOFFS, REGULAR_SEASON, NhlApi, ScheduledGame, parse_utc
from nhl_edge.lake.raw import RawStore
from nhl_edge.lake.schemas import OddsSnapshots

BASE_URL = "https://api.the-odds-api.com"
SPORT = "icehockey_nhl"
SOURCE = "odds"
ET = ZoneInfo("America/New_York")
LOW_CREDITS = 100
# Supabase holds the live window only; the raw store keeps every listed game.
SUPABASE_HORIZON = timedelta(hours=36)

# Market keys requested from the Odds API. The stored markets add h2h_3_way (see parse_odds).
REQUEST_MARKETS = ("h2h", "spreads", "totals")
FULL = REQUEST_MARKETS


@dataclass(frozen=True)
class Slot:
    name: str
    et_time: time
    markets: tuple[str, ...]
    # None: the slot runs when any game today (ET) has not started. Otherwise a game has to start
    # within this lead time, so the pre-game slots only spend credits close to a start.
    lead: timedelta | None


SLOT_PLANS: dict[str, tuple[Slot, ...]] = {
    "free-tier": (
        Slot("morning", time(7, 5), FULL, None),
        Slot("midday", time(12, 45), FULL, None),
        Slot("pre7", time(18, 45), ("h2h", "totals"), timedelta(minutes=90)),
        Slot("pre8", time(19, 45), ("h2h",), timedelta(minutes=90)),
        Slot("pre10", time(21, 45), ("h2h",), timedelta(minutes=90)),
    ),
}


def slot_by_name(plan: str, name: str) -> Slot:
    for slot in SLOT_PLANS[plan]:
        if slot.name == name:
            return slot
    raise KeyError(f"no slot {name!r} in slot plan {plan!r}")


def resolve_slot(plan: str, cron: str, on: date) -> Slot | None:
    """The slot a daily UTC cron line serves on a given date, or None when the line belongs to the
    other half of the DST year. Uses the scheduled time, so a late-starting run keeps its slot."""
    fields = cron.split()
    if len(fields) != 5 or fields[2:] != ["*", "*", "*"]:
        raise ValueError(f"expected a daily cron line 'M H * * *', got {cron!r}")
    scheduled = datetime.combine(on, time(int(fields[1]), int(fields[0])), tzinfo=UTC)
    et_time = scheduled.astimezone(ET).time()
    return next((slot for slot in SLOT_PLANS[plan] if slot.et_time == et_time), None)


def slot_has_games(slot: Slot, games: Sequence[ScheduledGame], now: datetime) -> bool:
    upcoming = [
        game
        for game in games
        if game.game_type in (REGULAR_SEASON, PLAYOFFS) and game.start_utc > now
    ]
    if slot.lead is None:
        today = now.astimezone(ET).date()
        return any(game.start_utc.astimezone(ET).date() == today for game in upcoming)
    return any(game.start_utc <= now + slot.lead for game in upcoming)


# Odds API team names to NHL triCodes, matched after normalize_team().
ODDS_API_TEAMS = {
    "anaheim ducks": "ANA",
    "boston bruins": "BOS",
    "buffalo sabres": "BUF",
    "calgary flames": "CGY",
    "carolina hurricanes": "CAR",
    "chicago blackhawks": "CHI",
    "colorado avalanche": "COL",
    "columbus blue jackets": "CBJ",
    "dallas stars": "DAL",
    "detroit red wings": "DET",
    "edmonton oilers": "EDM",
    "florida panthers": "FLA",
    "los angeles kings": "LAK",
    "minnesota wild": "MIN",
    "montreal canadiens": "MTL",
    "nashville predators": "NSH",
    "new jersey devils": "NJD",
    "new york islanders": "NYI",
    "new york rangers": "NYR",
    "ottawa senators": "OTT",
    "philadelphia flyers": "PHI",
    "pittsburgh penguins": "PIT",
    "san jose sharks": "SJS",
    "seattle kraken": "SEA",
    "st louis blues": "STL",
    "tampa bay lightning": "TBL",
    "toronto maple leafs": "TOR",
    "utah hockey club": "UTA",
    "utah mammoth": "UTA",
    "vancouver canucks": "VAN",
    "vegas golden knights": "VGK",
    "washington capitals": "WSH",
    "winnipeg jets": "WPG",
}


def normalize_team(name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return " ".join(ascii_name.replace(".", "").lower().split())


def team_code(name: str) -> str:
    try:
        return ODDS_API_TEAMS[normalize_team(name)]
    except KeyError:
        raise ValueError(f"unknown Odds API team {name!r}: add it to ODDS_API_TEAMS") from None


class OddsApiError(RuntimeError):
    """A failed Odds API call. Messages never include the request URL, which holds the key."""


@dataclass(frozen=True)
class Credits:
    remaining: int | None
    used: int | None
    last: int | None

    @classmethod
    def from_headers(cls, headers: httpx.Headers) -> "Credits":
        def number(name: str) -> int | None:
            value = headers.get(name)
            return int(float(value)) if value else None

        return cls(
            number("x-requests-remaining"),
            number("x-requests-used"),
            number("x-requests-last"),
        )


@dataclass(frozen=True)
class OddsResponse:
    body: bytes
    snapshot_utc: datetime
    params: dict[str, str]
    credits: Credits


def utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


class OddsApi:
    def __init__(
        self,
        api_key: str,
        client: httpx.Client | None = None,
        *,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self.api_key = api_key
        self.client = client or httpx.Client(base_url=BASE_URL, timeout=30)
        self.now = now

    def odds(self, regions: str, markets: Sequence[str]) -> OddsResponse:
        """GET /v4/sports/icehockey_nhl/odds. Costs len(markets) credits per region."""
        unknown = set(markets) - set(REQUEST_MARKETS)
        if unknown:
            raise ValueError(f"unsupported markets: {sorted(unknown)}")
        params = {
            "regions": regions,
            "markets": ",".join(markets),
            "oddsFormat": "decimal",
            "dateFormat": "iso",
        }
        try:
            response = self.client.get(
                f"/v4/sports/{SPORT}/odds", params={**params, "apiKey": self.api_key}
            )
        except httpx.HTTPError as exc:
            raise OddsApiError(f"Odds API request failed: {type(exc).__name__}") from None
        snapshot_utc = self.now()
        if response.status_code != 200:
            raise OddsApiError(f"Odds API returned {response.status_code}: {response.text[:300]}")
        return OddsResponse(
            response.content, snapshot_utc, params, Credits.from_headers(response.headers)
        )


ODDS_FRAME_SCHEMA: dict[str, Any] = {
    "snapshot_utc": pl.Datetime("us", "UTC"),
    "last_update_utc": pl.Datetime("us", "UTC"),
    "event_id": pl.String,
    "commence_time_utc": pl.Datetime("us", "UTC"),
    "home": pl.String,
    "away": pl.String,
    "book": pl.String,
    "market": pl.String,
    "side": pl.String,
    "line": pl.Float64,
    "price_decimal": pl.Float64,
    "is_closing_proxy": pl.Boolean,
    "slot": pl.String,
    "raw_key": pl.String,
}


def parse_odds(body: bytes, snapshot_utc: datetime, slot: str, raw_key: str) -> pl.DataFrame:
    """One validated row per book, market and side. Markets outside h2h, spreads and totals, such as
    the exchanges' h2h_lay, are skipped.

    Many EU books quote the 3-way regulation line (home, draw, away over 60 minutes) under the h2h
    key. Those quotes are stored as h2h_3_way, so h2h only ever holds the two-way moneyline that
    includes OT and the shootout (hard rule 2).
    """
    rows: list[dict[str, Any]] = []
    for event in json.loads(body):
        home_name, away_name = event["home_team"], event["away_team"]
        home, away = team_code(home_name), team_code(away_name)
        for book in event.get("bookmakers", []):
            for market in book.get("markets", []):
                key = market["key"]
                if key not in REQUEST_MARKETS:
                    continue
                if key == "h2h" and any(o["name"] == "Draw" for o in market["outcomes"]):
                    key = "h2h_3_way"
                last_update = market.get("last_update") or book["last_update"]
                for outcome in market["outcomes"]:
                    rows.append(
                        {
                            "snapshot_utc": snapshot_utc,
                            "last_update_utc": parse_utc(last_update),
                            "event_id": event["id"],
                            "commence_time_utc": parse_utc(event["commence_time"]),
                            "home": home,
                            "away": away,
                            "book": book["key"],
                            "market": key,
                            "side": _side(key, outcome["name"], home_name, away_name),
                            "line": outcome_line(key, outcome),
                            "price_decimal": float(outcome["price"]),
                            "is_closing_proxy": False,
                            "slot": slot,
                            "raw_key": raw_key,
                        }
                    )
    return OddsSnapshots.validate(pl.DataFrame(rows, schema=ODDS_FRAME_SCHEMA))


def outcome_line(market: str, outcome: dict[str, Any]) -> float | None:
    return None if market in ("h2h", "h2h_3_way") else float(outcome["point"])


def _side(market: str, name: str, home_name: str, away_name: str) -> str:
    if market == "totals":
        if name in ("Over", "Under"):
            return name.lower()
    elif name == home_name:
        return "home"
    elif name == away_name:
        return "away"
    elif name == "Draw" and market == "h2h_3_way":
        return "draw"
    raise ValueError(f"unexpected {market} outcome {name!r}")


def supabase_window(snapshots: pl.DataFrame) -> pl.DataFrame:
    """Pre-game quotes for games starting within SUPABASE_HORIZON of the snapshot."""
    return snapshots.filter(
        pl.col("commence_time_utc") > pl.col("snapshot_utc"),
        pl.col("commence_time_utc") <= pl.col("snapshot_utc") + SUPABASE_HORIZON,
    )


def available_at(snapshots: pl.DataFrame, prediction_utc: datetime) -> pl.DataFrame:
    """Quotes a prediction at prediction_utc may use: observed before it, for games that have not
    started by then. A pre-game price for a game already under way is no longer executable."""
    return snapshots.filter(
        pl.col("snapshot_utc") < prediction_utc,
        pl.col("commence_time_utc") > prediction_utc,
    )


Sink = Callable[[pl.DataFrame], int]


def run_snapshot(
    *,
    slot: Slot,
    regions: str,
    skip_if_no_games: bool,
    nhl: NhlApi,
    odds: OddsApi,
    store: RawStore,
    sink: Sink | None,
    now: datetime,
    echo: Callable[[str], None],
) -> None:
    """Check the schedule, pull the slot's markets, store the raw response, then parse, validate
    and write the live window. The raw copy is stored before parsing, so a parser failure loses
    nothing that cannot be replayed."""
    if skip_if_no_games:
        games = nhl.schedule(now.astimezone(ET).date())
        if not slot_has_games(slot, games, now):
            echo(f"odds snapshot {slot.name}: no NHL games in this slot, no Odds API call made")
            return
    response = odds.odds(regions, slot.markets)
    credits = response.credits
    echo(f"odds api credits: remaining={credits.remaining} used={credits.used} last={credits.last}")
    if (
        os.environ.get("GITHUB_ACTIONS") == "true"
        and credits.remaining is not None
        and credits.remaining < LOW_CREDITS
    ):
        echo(f"::warning::Odds API credits low: {credits.remaining} remaining this month")
    stamp = f"{response.snapshot_utc:%Y%m%dT%H%M%SZ}"
    raw_key = store.put(
        SOURCE,
        f"{response.snapshot_utc:%Y-%m-%d}/{stamp}_{slot.name}_{regions}",
        response.body,
        {
            "fetched_utc": response.snapshot_utc.isoformat(),
            "endpoint": f"/v4/sports/{SPORT}/odds",
            "params": response.params,
            "slot": slot.name,
            "credits": vars(credits),
        },
    )
    snapshots = parse_odds(response.body, response.snapshot_utc, slot.name, raw_key)
    live = supabase_window(snapshots)
    events = snapshots["event_id"].n_unique()
    summary = f"odds snapshot {slot.name}: {len(snapshots)} quotes, {events} events, raw {raw_key}"
    if sink is None:
        echo(f"{summary}; dry run, {len(live)} rows in the live window not written")
        return
    written = sink(live)
    echo(f"{summary}; {written} of {len(live)} live-window rows sent to odds_snapshots")
