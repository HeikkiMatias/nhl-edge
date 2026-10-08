"""The day's paper decisions (#164, docs/plans/phase-5.md task 3; ADRs 0028, 0030 and 0033).

At about 12:47 ET, after the midday odds snapshot, every slate game gets one ledger row: its
prediction and bet, or why it has none. The decision instant is fixed when the run starts, and
everything read must be known before it. The ledger is published minutes later, once the models
are read: published_utc, the actual clock read at the write and stamped on the ledger, also read
after the ledger is built, with the day decided again if a game starts or its quote ages
past the limit meanwhile (#170, ADR 0033's amendment).

- **The window (ADR 0033):** the midday snapshot and the decision both fall between 12:45 and
  13:15 ET, read from their times, not the slot's label. Outside it the day is skipped.
- **Per game,** in order:
  1. a game that started before the decision, or that starts before the decision is published,
     has no prediction: a weekend matinee can start inside the window;
  2. Pinnacle's h2h quote at the decision snapshot, at most 5 minutes old at the decision and at
     most 15 minutes old at publication;
  3. B0, Pinnacle's price de-vigged (`market/devig.py`), and B1 its recalibration;
  4. B2 and B3 from the season's fits, mixing over the goalie-start model's likely starters, and
     u's parts from the same model: never a confirmed starter (ADR 0030);
  5. the live blend and its twins (task 2's artifact);
  6. the frozen selection, guard and staking.
- **The bankroll** is 100 units at the season's start, plus the profit of earlier logged bets whose
  results were public before the decision.

The functions here are pure: the CLI loads the inputs and writes the ledger once.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from typing import Any

import numpy as np
import polars as pl

from nhl_edge.backtest.walk_forward import refused
from nhl_edge.betting import guard as market_guard
from nhl_edge.betting import selection, staking
from nhl_edge.betting.selection import POLICY, POLICY_VERSION
from nhl_edge.game import b2, b3, uncertainty
from nhl_edge.game.b2 import B2Model
from nhl_edge.game.b3 import B3Model
from nhl_edge.ingest.nhl_api import parse_utc
from nhl_edge.ingest.odds import ET, ODDS_FRAME_SCHEMA, SOURCE, available_at, parse_odds
from nhl_edge.ingest.odds_lake import dated_raw_keys, is_complete, match_games
from nhl_edge.lake.raw import RawStore
from nhl_edge.lake.schemas import PaperLedger, dtypes
from nhl_edge.live import blend_fit
from nhl_edge.live.targets import with_targets
from nhl_edge.market.devig import fair_probabilities

WINDOW = (time(12, 45), time(13, 15))
MAX_QUOTE_AGE = timedelta(minutes=5)
# A quote's age at publication: a stalled run can't log a bet at a price long gone (ADR 0033's
# amendment).
MAX_PUBLISHED_AGE = timedelta(minutes=15)
BOOK = market_guard.BOOK
LEDGER_PREFIX = "ledger/live"
# The tables a run reads, pulled from R2 first on a fresh machine: B2's and B3's history and the
# day's target rows, the slate and its build record, and the earlier ledgers for the bankroll.
# No table of confirmed starters (pregame_goalies, dailyfaceoff_goalies): ADR 0030.
LAKE_TABLES = (
    "games",
    "actual_lineups",
    "player_league_seasons",
    "team_strength",
    "schedule_terms",
    "goalie_starts",
    "goalie_effects",
    "lineups",
    "lineup_replacements",
    "player_ratings",
    "rapm_terms",
    "expected_power_plays",
    "goal_multipliers",
    "slate",
    "feature_builds",
    "paper_ledger",
)

PREDICTED = "predicted"
STARTED = "started before the decision"
STARTS_BEFORE_PUBLISHED = "starts before the decision is published"
NO_PRICE = "no Pinnacle midday price"
STALE = "no fresh price"
MISSING = "missing input"
# The whole day, when the decision can't be made in the window.
NO_SNAPSHOT = "day skipped: no midday snapshot in the window"
LATE = "day skipped: the decision came after the window"


def in_window(moment: datetime) -> bool:
    """Whether moment falls between 12:45 and 13:15 US Eastern on its own date."""
    local = moment.astimezone(ET).time()
    return WINDOW[0] <= local <= WINDOW[1]


def after_window(moment: datetime) -> bool:
    return moment.astimezone(ET).time() > WINDOW[1]


def decision_snapshot(quotes: pl.DataFrame, decision_utc: datetime) -> datetime | None:
    """The latest snapshot taken in the window, on the decision's date, before the decision."""
    day = decision_utc.astimezone(ET).date()
    times = sorted(
        {
            t
            for t in quotes["snapshot_utc"].unique().to_list()
            if t < decision_utc and t.astimezone(ET).date() == day and in_window(t)
        }
    )
    return times[-1] if times else None


def devigable(quotes: pl.DataFrame) -> pl.DataFrame:
    """The quotes without any h2h pair de-vigging refuses (implied probabilities summing below
    100%, such as both sides at plus money): that pair is no price, so its game alone gets none,
    and neither B0 nor the guard ever reads it."""
    keys = ["snapshot_utc", "event_id", "book"]
    h2h = quotes.filter(pl.col("market") == "h2h")
    pairs = _wide(h2h, keys)
    if pairs.is_empty():
        return quotes
    bad = pairs.filter(refused(pairs)).select(keys).with_columns(market=pl.lit("h2h"))
    return quotes.join(bad, on=[*keys, "market"], how="anti")


def usable_quotes(quotes: pl.DataFrame, decision_utc: datetime) -> pl.DataFrame:
    """The quotes a decision may read: from snapshots before it, of games not under way by then
    (odds.available_at), without pairs de-vigging refuses."""
    known = quotes.filter(pl.col("snapshot_utc") < decision_utc)
    return devigable(available_at(known, decision_utc))


def input_cutoff(quotes: pl.DataFrame, decision_utc: datetime) -> datetime:
    """The instant every model input must precede: the decision snapshot's, since the price bet
    was observed then, so nothing learned between the price and the decision informs a bet
    against it. Without a decision snapshot the day is skipped, and the decision instant
    stands."""
    return decision_snapshot(usable_quotes(quotes, decision_utc), decision_utc) or decision_utc


def listings(slate: pl.DataFrame) -> pl.DataFrame:
    """The slate as listings to match odds events against (odds_lake.match_games)."""
    return slate.select(
        "game_id", game_type=pl.lit(2, pl.Int8), start_utc="start_utc", home="home", away="away"
    )


def _wide(h2h: pl.DataFrame, keys: list[str]) -> pl.DataFrame:
    home = h2h.filter(pl.col("side") == "home").select(
        *keys, home_price="price_decimal", home_update="last_update_utc"
    )
    away = h2h.filter(pl.col("side") == "away").select(
        *keys, away_price="price_decimal", away_update="last_update_utc"
    )
    return home.join(away, on=keys)


def pinnacle(quotes: pl.DataFrame, snapshot_utc: datetime) -> pl.DataFrame:
    """Per game, Pinnacle's two h2h prices at the snapshot, its event and its last update (the
    older side's)."""
    h2h = quotes.filter(
        pl.col("snapshot_utc") == snapshot_utc,
        pl.col("book") == BOOK,
        pl.col("market") == "h2h",
        pl.col("game_id").is_not_null(),
    )
    return _wide(h2h, ["game_id", "event_id"]).select(
        "game_id",
        "event_id",
        "home_price",
        "away_price",
        last_update_utc=pl.min_horizontal("home_update", "away_update"),
    )


def best_other(quotes: pl.DataFrame, snapshot_utc: datetime) -> pl.DataFrame:
    """Per game and side, the best h2h price of the other EU books at the snapshot, its book and
    its last update: logged beside Pinnacle's (ADR 0028), never bet at."""
    h2h = quotes.filter(
        pl.col("snapshot_utc") == snapshot_utc,
        pl.col("book") != BOOK,
        pl.col("market") == "h2h",
        pl.col("game_id").is_not_null(),
    )
    frames = []
    for side in ("home", "away"):
        best = (
            h2h.filter(pl.col("side") == side)
            .sort("price_decimal", "book", descending=[True, False])
            .group_by("game_id", maintain_order=True)
            .first()
            .select(
                "game_id",
                **{
                    f"best_{side}_price": "price_decimal",
                    f"best_{side}_book": "book",
                    f"best_{side}_update_utc": "last_update_utc",
                },
            )
        )
        frames.append(best)
    return frames[0].join(frames[1], on="game_id", how="full", coalesce=True)


@dataclass(frozen=True)
class Models:
    """B2's and B3's predictions and u's parts for the games that have them, with the fits'
    cutoffs, and the fits themselves for the day's run bundle (#171)."""

    b2: pl.DataFrame
    b3: pl.DataFrame
    parts: pl.DataFrame
    b2_cutoff: datetime | None
    b3_cutoff: datetime | None
    b2_model: B2Model | None = None
    b3_model: B3Model | None = None


def models(
    tables: b2.Tables,
    b3_tables: b3.Tables,
    u_tables: uncertainty.Tables,
    slate: pl.DataFrame,
    moments: pl.DataFrame,
    start: datetime,
    season: int = blend_fit.LIVE_SEASON,
) -> Models:
    """B2, B3 and u's parts at the decision for the slate's games, from the season's fits trained
    on results public before start (the live fold start), the history path's code with the slate
    as targets. No table of confirmed starters is read (ADR 0030)."""
    games = with_targets(tables.games, slate)
    tables = _with_games(tables, games)
    b3_tables = _with_games(b3_tables, games)
    u_tables = _with_games(u_tables, games)
    b2_rows, b2_fit = b2.predictions(tables, moments, season, start, b2.TUNED)
    b3_rows, b3_fit = b3.predictions(b3_tables, moments, season, start)
    b3_moments = moments.join(b3_rows.select("game_id"), on="game_id", how="semi")
    parts = uncertainty.parts(u_tables, b3_moments)
    late = parts.join(b3_moments, on="game_id").filter(
        (pl.col("train_cutoff") >= start) | (pl.col("observed_utc") >= pl.col("prediction_utc"))
    )
    if late.height:
        raise ValueError(f"u read rows known after the fold start or the decision ({late.height})")
    return Models(
        b2_rows.select("game_id", p_b2="p_home"),
        b3_rows.select("game_id", p_b3="p_home"),
        parts.select("game_id", *uncertainty.PARTS),
        b2_fit.train_cutoff,
        b3_fit.train_cutoff,
        b2_fit,
        b3_fit,
    )


def _with_games(tables: Any, games: pl.DataFrame) -> Any:
    from dataclasses import replace

    return replace(tables, games=games)


def devig(prices: pl.DataFrame) -> pl.Series:
    """B0: the home probability of each home and away price pair, de-vigged multiplicatively."""
    pairs = prices.select("home_price", "away_price").to_numpy()
    fair = fair_probabilities(pairs)[:, 0] if len(pairs) else np.empty(0)
    return pl.Series("p_b0", fair, dtype=pl.Float64)


def predictions(rows: pl.DataFrame, fitted: Models, live: blend_fit.LiveFit) -> pl.DataFrame:
    """The rows (game_id, home_price, away_price) that have every input, with B0 to B3, u's parts,
    u and u_sd on the live scale, and the blend and its twins."""
    ready = (
        rows.join(fitted.b2, on="game_id")
        .join(fitted.b3, on="game_id")
        .join(fitted.parts, on="game_id")
        .sort("game_id")
    )
    if ready.is_empty():
        return ready.with_columns(
            *(
                pl.lit(None, pl.Float64).alias(c)
                for c in ("p_b0", "p_b1", "u", "u_sd", "p_blend", "p_blend_b2", "p_blend_market")
            )
        )
    p_b0 = devig(ready).to_numpy()
    u = live.scale.score(ready).to_numpy()
    blends = live.blends
    return ready.with_columns(
        p_b0=pl.Series(p_b0, dtype=pl.Float64),
        p_b1=pl.Series(live.b1.predict(p_b0), dtype=pl.Float64),
        u=pl.Series(u, dtype=pl.Float64),
        u_sd=live.scale.in_sds(ready),
        p_blend=pl.Series(
            blends["BLEND"].predict(p_b0, ready["p_b3"].to_numpy(), u), dtype=pl.Float64
        ),
        p_blend_b2=pl.Series(
            blends["BLEND_B2"].predict(p_b0, ready["p_b2"].to_numpy(), u), dtype=pl.Float64
        ),
        p_blend_market=pl.Series(blends["BLEND_MARKET"].predict(p_b0), dtype=pl.Float64),
    )


def bankroll(earlier: pl.DataFrame, results: pl.DataFrame, decision_utc: datetime) -> float:
    """The season's paper bankroll at the decision: 100 units plus the profit of earlier logged
    bets (side, price, stake) whose results (home_win, result_utc) were public before it."""
    bets = earlier.filter(pl.col("bet"))
    if bets.is_empty():
        return POLICY.bankroll
    settled = bets.join(results, on="game_id").filter(pl.col("result_utc") < decision_utc)
    profit = settled.select(
        pl.when(staking.won(pl.col("side"), pl.col("home_win")))
        .then(pl.col("stake") * (pl.col("price") - 1))
        .otherwise(-pl.col("stake"))
        .sum()
    ).item()
    return POLICY.bankroll + float(profit or 0.0)


@dataclass(frozen=True)
class Day:
    """What a day's decision reads, all known before decision_utc, and when it is published
    (published_utc, the actual clock; the decision instant if not given)."""

    day: date
    decision_utc: datetime
    slate: pl.DataFrame
    quotes: pl.DataFrame
    fitted: Models | None
    live: blend_fit.LiveFit
    bankroll: float
    versions: Mapping[str, object]
    published_utc: datetime | None = None


def skipped(
    slate: pl.DataFrame, decision_utc: datetime, published_utc: datetime, reason: str
) -> pl.DataFrame:
    """Every slate game with the day's reason and nothing predicted."""
    return slate.select("game_id", "season", "game_date", "start_utc", "home", "away").with_columns(
        prediction_utc=pl.lit(decision_utc),
        published_utc=pl.lit(published_utc),
        status=pl.lit(reason),
    )


def starting(inputs: Day, by: datetime) -> pl.Series:
    """The slate's games that start by the instant by: by the slate's start, or by the commence
    time of the odds known before the decision, which can be earlier."""
    known = inputs.quotes.filter(pl.col("snapshot_utc") < inputs.decision_utc)
    odds = match_games(known.filter(pl.col("commence_time_utc") <= by), listings(inputs.slate))
    return pl.concat(
        [inputs.slate.filter(pl.col("start_utc") <= by)["game_id"], odds["game_id"].drop_nulls()]
    ).unique()


def decide(inputs: Day) -> pl.DataFrame:
    """One row per slate game: its prediction and bet, or why it has none."""
    decision = inputs.decision_utc
    published = inputs.published_utc or decision
    if published < decision:
        raise ValueError(f"published at {published}, before the decision at {decision}")
    slate = inputs.slate.select("game_id", "season", "game_date", "start_utc", "home", "away")
    if after_window(decision) or not in_window(decision):
        reason = LATE if after_window(decision) else "before the window"
        return skipped(slate, decision, published, reason)
    # Only quotes known before the decision: snapshots taken before it, and of those, prices of
    # games not under way by then. A later snapshot never matches a quote to a game, and an
    # in-play price never prices a bet.
    usable = usable_quotes(inputs.quotes, decision)
    snapshot = decision_snapshot(usable, decision)
    if snapshot is None:
        return skipped(slate, decision, published, NO_SNAPSHOT)
    matched = match_games(usable, listings(inputs.slate))
    # A game the odds already showed under way has started, whatever the slate's start says.
    under_way = starting(inputs, decision)
    # The bet is placed when the decision is published: a game under way by then is no bet's.
    starts_first = starting(inputs, published)
    quoted = pinnacle(matched, snapshot)
    rows = (
        slate.join(quoted, on="game_id", how="left")
        .join(best_other(matched, snapshot), on="game_id", how="left")
        .with_columns(
            prediction_utc=pl.lit(decision),
            published_utc=pl.lit(published),
            decision_snapshot_utc=pl.lit(snapshot),
        )
    )
    stale = ((pl.col("prediction_utc") - pl.col("last_update_utc")) > MAX_QUOTE_AGE) | (
        (pl.col("published_utc") - pl.col("last_update_utc")) > MAX_PUBLISHED_AGE
    )
    rows = rows.with_columns(
        status=pl.when(pl.col("game_id").is_in(under_way.implode()))
        .then(pl.lit(STARTED))
        .when(pl.col("game_id").is_in(starts_first.implode()))
        .then(pl.lit(STARTS_BEFORE_PUBLISHED))
        .when(pl.col("home_price").is_null())
        .then(pl.lit(NO_PRICE))
        .when(stale)
        .then(pl.lit(STALE))
        .otherwise(pl.lit(PREDICTED))
    )
    candidates = rows.filter(pl.col("status") == PREDICTED)
    predicted = (
        predictions(candidates, inputs.fitted, inputs.live)
        if inputs.fitted is not None
        else candidates.clear()
    )
    rows = rows.with_columns(
        status=pl.when(
            (pl.col("status") == PREDICTED)
            & ~pl.col("game_id").is_in(predicted["game_id"].implode())
        )
        .then(pl.lit(MISSING))
        .otherwise(pl.col("status"))
    )
    picked = _bets(predicted, inputs, matched)
    added = [c for c in picked.columns if c not in rows.columns or c == "game_id"]
    return rows.join(picked.select(added), on="game_id", how="left").sort("game_id")


def _bets(predicted: pl.DataFrame, inputs: Day, matched: pl.DataFrame) -> pl.DataFrame:
    """The frozen selection, guard and staking on the predicted games."""
    if predicted.is_empty():
        return predicted
    chosen = selection.select(predicted.with_columns(p_home=pl.col("p_blend")), POLICY).drop(
        "p_home"
    )
    moves = market_guard.live_moves(
        matched.filter(pl.col("game_id").is_not_null()), inputs.decision_utc
    )
    guarded = market_guard.guard(chosen.join(moves, on="event_id", how="left"))
    # Without both of the day's quotes, the guard can't read a move and doesn't apply (ADR 0029).
    guarded = guarded.with_columns(pl.col("guarded").fill_null(False))
    bets = guarded.with_columns(bet=pl.col("picked") & ~pl.col("guarded"))
    staked = staking.fractions(bets.filter(pl.col("bet")), POLICY).select("game_id", "fraction")
    return (
        bets.join(staked, on="game_id", how="left")
        .with_columns(
            fraction=pl.col("fraction").fill_null(0.0),
            bankroll=pl.lit(inputs.bankroll),
        )
        .with_columns(stake=pl.col("fraction") * pl.col("bankroll"))
    )


def stamp(ledger: pl.DataFrame, inputs: Day) -> pl.DataFrame:
    """The ledger with the policy, artifact and build versions every row carries."""
    return ledger.with_columns(
        policy_version=pl.lit(POLICY_VERSION),
        **{name: pl.lit(value) for name, value in inputs.versions.items()},
    )


class Republish(ValueError):
    """A predicted row no longer holds by the clock at the write: decide the day again."""


def pre_game(ledger: pl.DataFrame) -> None:
    """Refuse a ledger with any prediction decided or published at or after its game's start."""
    late = ledger.filter(
        pl.col("status") == PREDICTED,
        (pl.col("prediction_utc") >= pl.col("start_utc"))
        | (pl.col("published_utc") >= pl.col("start_utc")),
    )
    if late.height:
        raise ValueError(f"{late.height} predictions at or after their game's start")


def outdated(built: pl.DataFrame, inputs: Day, now: datetime) -> pl.DataFrame:
    """The rows whose status a decision published at now would change: a game not yet marked
    started that starts by then, by the slate or the odds, or a predicted or missing-input row
    whose quote is older than MAX_PUBLISHED_AGE by then. With none, the ledger stamped with now
    is the day decided at now."""
    open_rows = pl.col("status").is_in([PREDICTED, NO_PRICE, STALE, MISSING])
    priced = pl.col("status").is_in([PREDICTED, MISSING])
    return built.filter(
        (open_rows & pl.col("game_id").is_in(starting(inputs, now).implode()))
        | (priced & ((pl.lit(now) - pl.col("last_update_utc")) > MAX_PUBLISHED_AGE))
    )


def stamped(built: pl.DataFrame, now: datetime) -> pl.DataFrame:
    """built with published_utc set to now, a clock no predicted row is outdated by: every
    prediction and bet is the one the day decided at now would hold."""
    published = pl.lit(now).cast(built.schema["published_utc"])
    return PaperLedger.validate(built.with_columns(published_utc=published))  # type: ignore[return-value]


def publish(inputs: Day, clock: Callable[[], datetime], tries: int = 5) -> pl.DataFrame:
    """The day's ledger, decided against the publication clock (#170, ADR 0033's amendment).
    The ledger is built at a clock reading and the clock read again: while a predicted row no
    longer holds by then (outdated), the day is decided again at the later clock. That game gets
    its row, and the others keep their predictions, the stakes rescaled without it. The ledger
    returned is stamped with the last reading."""
    published = clock()
    for _ in range(tries):
        day = replace(inputs, published_utc=published)
        built = ledger(decide(day), day)
        now = clock()
        if outdated(built, inputs, now).is_empty():
            return stamped(built, now)
        published = now
    raise ValueError(f"games kept changing while the ledger was built ({tries} tries)")


def responses(store: RawStore, day: date) -> list[str]:
    """The raw keys of the day's complete odds responses (odds/<day>/, by UTC date): every one a
    decision attempts to read."""
    return [key for key in dated_raw_keys(SOURCE, store).get(day, []) if is_complete(store, key)]


def day_quotes(store: RawStore, day: date) -> tuple[pl.DataFrame, list[str]]:
    """Every quote of the day's stored odds snapshots (odds/<day>/, by UTC date: the morning and
    midday slots fall on the ET date's own), parsed as the odds replay parses them, and the raw
    keys that failed to parse. A malformed response is left out, so it can neither block the
    others nor stand in for a snapshot."""
    frames, failed = [], []
    for raw_key in responses(store, day):
        meta = store.meta(raw_key)
        try:
            snapshot_utc = parse_utc(meta["fetched_utc"])
            slot = str(meta.get("slot") or "unknown")
            frames.append(parse_odds(store.get(raw_key), snapshot_utc, slot, raw_key))
        except Exception as exc:  # any parse or validation failure of one response
            failed.append(f"{raw_key}: {type(exc).__name__}")
    if not frames:
        return pl.DataFrame(schema=ODDS_FRAME_SCHEMA), failed
    return pl.concat(frames), failed


def sync_ledgers(objects: Any, bucket: str, season: int) -> pl.DataFrame:
    """Every ledger of the season written to R2 (ledger/live/), the canonical record: the lake's
    paper_ledger is rebuilt from it, so a day whose lake copy failed is never missing from the
    bankroll."""
    import io

    frames = []
    token: dict[str, str] = {}
    while True:
        listed = objects.list_objects_v2(Bucket=bucket, Prefix=f"{LEDGER_PREFIX}/", **token)
        for item in listed.get("Contents", []):
            body = objects.get_object(Bucket=bucket, Key=item["Key"])["Body"].read()
            frames.append(pl.read_parquet(io.BytesIO(body)))
        if not listed.get("IsTruncated"):
            break
        token = {"ContinuationToken": listed["NextContinuationToken"]}
    columns = dtypes(PaperLedger)
    if not frames:
        return pl.DataFrame(schema=columns)
    every = pl.concat(frames).cast(columns)  # type: ignore[arg-type]
    return PaperLedger.validate(every.filter(pl.col("season") == season)) if every.height else every


def build_problems(
    record: pl.DataFrame,
    slate: pl.DataFrame,
    decision_utc: datetime,
    rows: Mapping[str, pl.DataFrame],
    *,
    committed: bool = True,
) -> list[str]:
    """Why the day's feature rows can't be read (#170): no record of a finished build, a build
    finished at or after the decision, a slate other than the one it rated, or a target table
    holding rows of a version it didn't record. A logged decision also needs a build of committed
    code, as it names its own commit; a dry run (committed=False) may read a local build."""
    if record.is_empty():
        return ["no finished feature build for the date"]
    problems = []
    dirty = sorted(set(record.filter(pl.col("build_id").str.ends_with("-dirty"))["build_id"]))
    if committed and dirty:
        problems.append(f"the feature build {dirty[0]} ran uncommitted code")
    finished = record["finished_utc"].max()
    assert isinstance(finished, datetime)
    if finished >= decision_utc:
        problems.append(f"the feature build finished at {finished}, not before the decision")
    keys = set(slate["raw_key"].unique().to_list())
    if keys != set(record["slate_raw_key"].unique().to_list()):
        problems.append("the slate is not the one the feature build rated")
    for table, frame in rows.items():
        recorded = set(record.filter(pl.col("table") == table)["artifact_version"].to_list())
        found = set(frame["artifact_version"].unique().to_list())
        if found - recorded:
            problems.append(f"{table} holds rows of {sorted(found - recorded)}, not its build's")
    return problems


def ledger(decided: pl.DataFrame, inputs: Day) -> pl.DataFrame:
    """The decided rows in PaperLedger's columns, every one present, validated."""
    columns = dtypes(PaperLedger)
    frame = stamp(decided, inputs).rename({"decision_utc": "guard_decision_utc"}, strict=False)
    frame = frame.with_columns(
        pl.lit(None, dtype).alias(name)
        for name, dtype in columns.items()
        if name not in frame.columns
    )
    return PaperLedger.validate(frame.select(list(columns)).cast(columns))  # type: ignore[arg-type]


def ledger_key(day: date) -> str:
    return f"{LEDGER_PREFIX}/{day.isoformat()}.parquet"


def write_once(
    objects: Any, bucket: str, inputs: Day, ledger: pl.DataFrame, clock: Callable[[], datetime]
) -> tuple[str, pl.DataFrame]:
    """Write the day's ledger to R2 once, refused if the day exists (a second run, or a late one
    after a skipped day), and only if every prediction precedes its game's start. The clock is
    read at the write: a predicted row outdated by then is refused (Republish), never written;
    otherwise the ledger is stamped with that reading, its publication, and put milliseconds
    later. Returns the key and the ledger written."""
    import io

    pre_game(ledger)
    if (late := outdated(ledger, inputs, now := clock())).height:
        raise Republish(f"{late.height} predictions outdated by {now}")
    written = stamped(ledger, now)
    body = io.BytesIO()
    written.write_parquet(body)
    key = ledger_key(inputs.day)
    objects.put_object(Bucket=bucket, Key=key, Body=body.getvalue(), IfNoneMatch="*")
    return key, written


def write_published(
    objects: Any, bucket: str, inputs: Day, clock: Callable[[], datetime], tries: int = 3
) -> tuple[pl.DataFrame, str]:
    """publish() the day and write it once. A row outdated between the ledger and the write has
    the day decided again at the later clock, never the whole day refused (#170)."""
    for _ in range(tries):
        built = publish(inputs, clock)
        try:
            key, written = write_once(objects, bucket, inputs, built, clock)
        except Republish:
            continue
        return written, key
    raise ValueError(f"games kept changing before the ledger was written ({tries} tries)")
