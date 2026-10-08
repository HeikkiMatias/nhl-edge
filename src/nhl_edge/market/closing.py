"""The closing proxy (#21, ADR 0033): the quote CLV is measured against, since the Odds API gives
no true close. For each started game, book and market (h2h only), the proxy is the quote from the
latest snapshot that:
- was taken after the day's decision snapshot and before the game's start;
- has both sides, each at most MAX_AGE old at that snapshot (snapshot_utc - last_update_utc);
- was taken at most MAX_LEAD before the start.

A snapshot that fails the freshness limit is skipped, and an earlier one stands in if it
qualifies. The day's decision snapshot is the first snapshot in the decision window (12:45 to
13:15 ET) on the game's ET date: the midday slot's, which nhl predict decides on as soon as it
lands, so a later one in the window was taken after the decision. It is never a game's close:
CLV against a bet's own price would measure only the margin.

A game is its Odds API event, and its start the commence time its latest snapshot gives: the Odds
API moves a start by minutes between snapshots, and a postponed game keeps its event. Quotes taken
before a postponement are then far more than MAX_LEAD before the new start, and never its close.
Only started games are marked: until the start, a later snapshot can still replace the proxy.

When Pinnacle has no proxy for a game, the reason counts for ADR 0032's coverage floor:
- NO_PREGAME when no pre-game slot is scheduled within MAX_LEAD before the start (matinees and
  21:30 starts), outside the floor;
- STALE when a snapshot in that span holds Pinnacle's quote but never fresh, and MISSING when
  none does: both count against it.

These read quote ages, snapshot times and the slot schedule only, never a result or a CLV.
"""

from datetime import UTC, datetime, time, timedelta

import polars as pl

from nhl_edge.ingest.odds import ET, SLOT_PLANS

MAX_AGE = timedelta(minutes=5)
MAX_LEAD = timedelta(minutes=90)
MARKETS = ("h2h",)
BOOK = "pinnacle"
PLAN = "free-tier"
# nhl predict's decision window (ADR 0033), on the game's ET date.
WINDOW = (time(12, 45), time(13, 15))
QUOTE = ("event_id", "book", "market")

PROXY = "proxy"
NO_PREGAME = "no pre-game snapshot"
STALE = "stale"
MISSING = "missing"

UTC_TYPE = pl.Datetime("us", "UTC")


def _at(moment: datetime) -> pl.Expr:
    return pl.lit(moment.astimezone(UTC), dtype=UTC_TYPE)


def _et(column: str) -> pl.Expr:
    return pl.col(column).dt.convert_time_zone(str(ET))


def decision_snapshots(quotes: pl.DataFrame) -> pl.DataFrame:
    """Each ET date's decision snapshot (et_date, decision_utc): its first snapshot in the
    window."""
    local = _et("snapshot_utc")
    return (
        quotes.select("snapshot_utc")
        .unique()
        .filter(local.dt.time().is_between(*WINDOW))
        .group_by(et_date=local.dt.date())
        .agg(decision_utc=pl.col("snapshot_utc").min())
    )


def starts(quotes: pl.DataFrame) -> pl.DataFrame:
    """Each event's start (event_id, start_utc): the commence time of its latest snapshot."""
    return (
        quotes.sort("snapshot_utc", "commence_time_utc")
        .group_by("event_id")
        .agg(start_utc=pl.col("commence_time_utc").last())
    )


def _span(quotes: pl.DataFrame, now: datetime) -> pl.DataFrame:
    """Each game started by now, its quotes in the proxy's span: after its day's decision
    snapshot, before its start and at most MAX_LEAD before it, one row per snapshot, book and
    market, with how many sides it has and the older side's age."""
    h2h = quotes.filter(pl.col("market").is_in(MARKETS))
    started = starts(h2h).filter(pl.col("start_utc") <= _at(now))
    pairs = (
        h2h.join(started, on="event_id")
        .group_by("snapshot_utc", *QUOTE, "start_utc")
        .agg(
            sides=pl.col("side").n_unique(),
            age=(pl.col("snapshot_utc") - pl.col("last_update_utc")).max(),
        )
        .with_columns(et_date=_et("start_utc").dt.date())
        .join(decision_snapshots(quotes), on="et_date", how="left")
    )
    return pairs.filter(
        pl.col("decision_utc").is_null() | (pl.col("snapshot_utc") > pl.col("decision_utc")),
        pl.col("snapshot_utc") < pl.col("start_utc"),
        pl.col("start_utc") - pl.col("snapshot_utc") <= MAX_LEAD,
    )


def proxies(quotes: pl.DataFrame, now: datetime) -> pl.DataFrame:
    """The closing proxy of each game started by now, per book and market: its snapshot_utc, and
    the game's start_utc."""
    fresh = _span(quotes, now).filter(pl.col("sides") == 2, pl.col("age") <= MAX_AGE)
    return (
        fresh.group_by(*QUOTE, "start_utc")
        .agg(pl.col("snapshot_utc").max())
        .select("snapshot_utc", *QUOTE, "start_utc")
    )


def flag(quotes: pl.DataFrame, now: datetime) -> pl.DataFrame:
    """quotes with is_closing_proxy true on both sides of each closing proxy (proxies()), false
    elsewhere."""
    keys = ["snapshot_utc", *QUOTE]
    marked = proxies(quotes, now).select(keys).with_columns(proxy=pl.lit(True))
    return (
        quotes.join(marked, on=keys, how="left")
        .with_columns(is_closing_proxy=pl.col("proxy").fill_null(False))
        .drop("proxy")
        .select(quotes.columns)
    )


def scheduled_pregame(starts: pl.DataFrame, plan: str = PLAN) -> pl.Series:
    """Whether a pre-game slot (one run only close to a start) is scheduled within MAX_LEAD
    before each start (start_utc), on the start's ET date."""
    day = _et("start_utc").dt.date()
    within = pl.lit(False)
    for slot in SLOT_PLANS[plan]:
        if slot.lead is None:
            continue
        at = day.dt.combine(slot.et_time).dt.replace_time_zone(str(ET)).dt.convert_time_zone("UTC")
        within = within | ((at < pl.col("start_utc")) & (pl.col("start_utc") - at <= MAX_LEAD))
    return starts.select(within.alias("scheduled"))["scheduled"]


def pinnacle_closes(quotes: pl.DataFrame, now: datetime, plan: str = PLAN) -> pl.DataFrame:
    """Each game started by now that Pinnacle priced (h2h) in the lake's odds_snapshots, with its
    NHL game_id, its start, its closing proxy's snapshot (proxy_utc), how long before the start
    it was taken (lead), and its status: PROXY, or why there is none (NO_PREGAME, STALE,
    MISSING)."""
    pinnacle = quotes.filter(pl.col("book") == BOOK, pl.col("market").is_in(MARKETS))
    games = (
        pinnacle.group_by("event_id")
        .agg(game_id=pl.col("game_id").drop_nulls().first())
        .join(starts(pinnacle), on="event_id")
        .filter(pl.col("start_utc") <= _at(now))
    )
    proxy = proxies(pinnacle, now).select("event_id", proxy_utc="snapshot_utc")
    seen = _span(pinnacle, now).group_by("event_id").agg(in_span=pl.len())
    return (
        games.with_columns(scheduled=scheduled_pregame(games, plan))
        .join(proxy, on="event_id", how="left")
        .join(seen, on="event_id", how="left")
        .with_columns(
            lead=pl.col("start_utc") - pl.col("proxy_utc"),
            status=pl.when(pl.col("proxy_utc").is_not_null())
            .then(pl.lit(PROXY))
            .when(~pl.col("scheduled"))
            .then(pl.lit(NO_PREGAME))
            .when(pl.col("in_span").is_not_null())
            .then(pl.lit(STALE))
            .otherwise(pl.lit(MISSING)),
        )
        .select("event_id", "game_id", "start_utc", "proxy_utc", "lead", "status")
        .sort("start_utc", "event_id")
    )
