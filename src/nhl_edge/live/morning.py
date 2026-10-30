"""The morning view (#357, ADR 0052): the decision's models at the 07:05 ET snapshot, with no bet.

Live, the 12:45 ET decision's disagreement with Pinnacle says nothing about the move to the close:
whatever the model knows, the market has mostly priced by midday. The morning view asks whether
the model's earlier view carries it, which posting early and two-sided quotes (#358) would earn
from. Each game day at the morning slot, after the goalie poll and the odds snapshot of the same
dispatch, every slate game gets one row: its probabilities, or why it has none.

- **The window:** the morning snapshot and the run both fall between 07:00 and 07:35 ET, read from
  their times. Outside it the day is written as skipped, never reconstructed.
- **The cutoff (hard rule 1):** the morning snapshot. Every model input precedes it: the feature
  build finished before it, and the starters come from the polls observed before it.
- **Per game,** as at the decision (live/predict.py): no prediction for a game that starts before
  the run or its publication, or without a fresh Pinnacle pair at the snapshot; otherwise B0 to
  B3, u's parts, u and the frozen live blend and its twins, through predict.predictions itself.
  No selection, guard or stake.
- **The record:** each date is written once (morning/live/<date>.parquet) beside its run bundle
  (bundles/morning/<date>/, live/bundle.py's shape), and check() replays a stored day from its
  bundle alone.

The report (live/report.py) scores it on the move from the morning snapshot to the close, with no
verdict before the formal review. Nothing here feeds a decision.
"""

import io
from collections.abc import Callable, Collection
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta

import polars as pl

from nhl_edge.ingest.games import result_public_utc
from nhl_edge.ingest.odds import ET
from nhl_edge.ingest.odds_lake import match_games
from nhl_edge.lake.schemas import MorningPredictions, dtypes
from nhl_edge.lake.tables import Reader
from nhl_edge.live import blend_fit
from nhl_edge.live import bundle as lb
from nhl_edge.live import predict as lp

# The experiment as ADR 0052 registers it: a changed specification is a new version, and its
# evidence starts again. Only game days after the registration count.
VERSION = "morning_view/1"
REGISTERED_ON = date(2026, 10, 30)
WINDOW = (time(7, 0), time(7, 35))
PREFIX = "morning/live"
BUNDLES = "bundles/morning"

PREDICTED = lp.PREDICTED
STARTED = "started before the run"
STARTS_BEFORE_PUBLISHED = "starts before the run is published"
NO_PRICE = "no Pinnacle morning price"
STALE = lp.STALE
MISSING = lp.MISSING
# The whole day, when the run can't be made in the window.
EARLY = "day skipped: the run came before the window"
LATE = "day skipped: the run came after the window"
NO_SNAPSHOT = "day skipped: no morning snapshot in the window"


@dataclass(frozen=True)
class AtBuild:
    """The lake with the day's own feature rows counted as observed when they were known (ADR
    0052): the rows its feature build wrote (record, the day's feature_builds rows, by table and
    artifact_version) at the later of the build's finish and the morning the night's results
    turned public (ingest/games.RESULT_PUBLIC_AT). Their observed_utc is history's as-of time of
    10:00 ET (live/targets.py), which says what they rate, not when they were known, and falls
    after the morning snapshot; the build reads nothing after it finishes, which must precede the
    snapshot (predict.build_problems). Every other row is the lake's."""

    lake: Reader
    record: pl.DataFrame
    day: date

    def known(self) -> datetime:
        finished = self.record["finished_utc"].max()
        assert isinstance(finished, datetime)
        return max(finished, result_public_utc(self.day - timedelta(days=1)))

    def read(self, table: str, seasons: Collection[int] | None = None) -> pl.DataFrame:
        frame = self.lake.read(table, seasons)
        versions = self.record.filter(pl.col("table") == table)["artifact_version"]
        needed = {"game_date", "artifact_version", "observed_utc"}
        if versions.is_empty() or not needed <= set(frame.columns):
            return frame
        own = (pl.col("game_date") == self.day) & pl.col("artifact_version").is_in(
            versions.implode()
        )
        known = pl.lit(self.known()).cast(frame.schema["observed_utc"])
        return frame.with_columns(
            observed_utc=pl.when(own & (pl.col("observed_utc") > known))
            .then(known)
            .otherwise(pl.col("observed_utc"))
        )


def in_window(moment: datetime) -> bool:
    """Whether moment falls between 07:00 and 07:35 US Eastern on its own date."""
    local = moment.astimezone(ET).time()
    return WINDOW[0] <= local <= WINDOW[1]


def after_window(moment: datetime) -> bool:
    return moment.astimezone(ET).time() > WINDOW[1]


def morning_snapshot(quotes: pl.DataFrame, run_utc: datetime) -> datetime | None:
    """The latest snapshot taken in the window, on the run's date, before the run."""
    day = run_utc.astimezone(ET).date()
    times = sorted(
        {
            t
            for t in quotes["snapshot_utc"].unique().to_list()
            if t < run_utc and t.astimezone(ET).date() == day and in_window(t)
        }
    )
    return times[-1] if times else None


def input_cutoff(quotes: pl.DataFrame, run_utc: datetime) -> datetime:
    """The instant every model input must precede: the morning snapshot's. Without one the day is
    skipped, and the run's instant stands."""
    return morning_snapshot(lp.usable_quotes(quotes, run_utc), run_utc) or run_utc


def predict(inputs: lp.Day) -> pl.DataFrame:
    """One row per slate game: the models at the morning snapshot, or why it has none. The run's
    instant is inputs.decision_utc, and inputs.bankroll is never read."""
    run = inputs.decision_utc
    published = inputs.published_utc or run
    if published < run:
        raise ValueError(f"published at {published}, before the run at {run}")
    slate = inputs.slate.select("game_id", "season", "game_date", "start_utc", "home", "away")
    if not in_window(run):
        return lp.skipped(slate, run, published, LATE if after_window(run) else EARLY)
    # Only quotes known before the run, of games not under way by then (predict.usable_quotes).
    usable = lp.usable_quotes(inputs.quotes, run)
    snapshot = morning_snapshot(usable, run)
    if snapshot is None:
        return lp.skipped(slate, run, published, NO_SNAPSHOT)
    matched = match_games(usable, lp.listings(inputs.slate))
    under_way = lp.starting(inputs, run)
    starts_first = lp.starting(inputs, published)
    rows = slate.join(lp.pinnacle(matched, snapshot), on="game_id", how="left").with_columns(
        prediction_utc=pl.lit(run),
        published_utc=pl.lit(published),
        morning_snapshot_utc=pl.lit(snapshot),
    )
    stale = ((pl.col("prediction_utc") - pl.col("last_update_utc")) > lp.MAX_QUOTE_AGE) | (
        (pl.col("published_utc") - pl.col("last_update_utc")) > lp.MAX_PUBLISHED_AGE
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
        lp.predictions(candidates, inputs.fitted, inputs.live)
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
    added = [c for c in predicted.columns if c not in rows.columns or c == "game_id"]
    return rows.join(predicted.select(added), on="game_id", how="left").sort("game_id")


def rows(predicted: pl.DataFrame, inputs: lp.Day) -> pl.DataFrame:
    """The predicted rows in MorningPredictions' columns, every one present, with the
    experiment's and the inputs' versions, validated."""
    columns = dtypes(MorningPredictions)
    versions = {"morning_version": VERSION, **inputs.versions}
    frame = predicted.with_columns(**{name: pl.lit(value) for name, value in versions.items()})
    frame = frame.with_columns(
        pl.lit(None, dtype).alias(name)
        for name, dtype in columns.items()
        if name not in frame.columns
    )
    return MorningPredictions.validate(frame.select(list(columns)).cast(columns))  # type: ignore[arg-type]


def outdated(built: pl.DataFrame, inputs: lp.Day, now: datetime) -> pl.DataFrame:
    """The rows a run published at now would change (predict.outdated, with the morning's own
    statuses): a game that starts by then, or a priced row whose quote is older than
    MAX_PUBLISHED_AGE by then."""
    open_rows = pl.col("status").is_in([PREDICTED, NO_PRICE, STALE, MISSING])
    priced = pl.col("status").is_in([PREDICTED, MISSING])
    return built.filter(
        (open_rows & pl.col("game_id").is_in(lp.starting(inputs, now).implode()))
        | (priced & ((pl.lit(now) - pl.col("last_update_utc")) > lp.MAX_PUBLISHED_AGE))
    )


def stamped(built: pl.DataFrame, now: datetime) -> pl.DataFrame:
    """built with published_utc set to now, a clock no predicted row is outdated by."""
    published = pl.lit(now).cast(built.schema["published_utc"])
    return MorningPredictions.validate(built.with_columns(published_utc=published))  # type: ignore[return-value]


def publish(inputs: lp.Day, clock: Callable[[], datetime], tries: int = 5) -> pl.DataFrame:
    """The day's rows, made against the publication clock as the decision's are (predict.publish):
    while a row no longer holds by the clock read after it was built, the day is made again at
    that later clock. The rows returned are stamped with the last reading."""
    published = clock()
    for _ in range(tries):
        day = replace(inputs, published_utc=published)
        built = rows(predict(day), day)
        now = clock()
        if outdated(built, inputs, now).is_empty():
            return stamped(built, now)
        published = now
    raise ValueError(f"games kept changing while the morning rows were built ({tries} tries)")


def key(day: date) -> str:
    return f"{PREFIX}/{day.isoformat()}.parquet"


def write_once(
    store: lb.Store, inputs: lp.Day, built: pl.DataFrame, clock: Callable[[], datetime]
) -> tuple[str, pl.DataFrame]:
    """Write the day's rows once, refused if the day exists (a second run, or a late one after a
    skipped day), and only if every prediction precedes its game's start. A row outdated by the
    clock at the write is refused (predict.Republish), never written; otherwise the rows are
    stamped with that reading, their publication. Returns the key and the rows written."""
    lp.pre_game(built)
    if (late := outdated(built, inputs, now := clock())).height:
        raise lp.Republish(f"{late.height} morning predictions outdated by {now}")
    written = stamped(built, now)
    store.put(key(inputs.day), lb.parquet(written))
    return key(inputs.day), written


def write_published(
    store: lb.Store, inputs: lp.Day, clock: Callable[[], datetime], tries: int = 3
) -> tuple[pl.DataFrame, str]:
    """publish() the day and write it once: a row outdated between the build and the write has
    the day made again at the later clock."""
    for _ in range(tries):
        built = publish(inputs, clock)
        try:
            where, written = write_once(store, inputs, built, clock)
        except lp.Republish:
            continue
        return written, where
    raise ValueError(f"games kept changing before the morning rows were written ({tries} tries)")


def stored_days(store: lb.Store) -> list[date]:
    """The days whose morning rows are kept."""
    return sorted(
        date.fromisoformat(k.rsplit("/", 1)[1].removesuffix(".parquet"))
        for k in store.keys(PREFIX)
        if k.endswith(".parquet")
    )


def stored(store: lb.Store, day: date) -> pl.DataFrame:
    return pl.read_parquet(io.BytesIO(store.get(key(day))))


def empty() -> pl.DataFrame:
    return pl.DataFrame(schema=dtypes(MorningPredictions))


def sync(store: lb.Store) -> pl.DataFrame:
    """Every stored day's rows, as the lake's morning_predictions."""
    frames = [stored(store, day) for day in stored_days(store)]
    every = pl.concat([empty(), *frames])
    return MorningPredictions.validate(every.cast(dtypes(MorningPredictions)))  # type: ignore[arg-type]


def replay(bundle: lb.Bundle, live: blend_fit.LiveFit) -> pl.DataFrame:
    """The day's rows made again from its bundle alone and the live fit: the models through
    bundle.models at the saved input cutoff, then predict on the saved quotes and slate, at the
    saved run and publication times."""
    manifest = bundle.manifest
    if live.version != manifest["blend_version"]:
        raise ValueError(f"the morning ran with {manifest['blend_version']}, not {live.version}")
    if manifest["morning_version"] != VERSION:
        raise ValueError(f"the morning ran as {manifest['morning_version']}, not {VERSION}")
    run = datetime.fromisoformat(manifest["run_utc"])
    cutoff = datetime.fromisoformat(manifest["input_cutoff"])
    # The models are read at the saved cutoff: it must be the saved quotes' morning snapshot, or
    # the run itself, as on a day skipped for want of one.
    if cutoff != input_cutoff(bundle.inputs["quotes"], run):
        raise ValueError(f"the saved input cutoff {cutoff} is not the morning snapshot")
    fitted = lb.models(bundle)
    day = lp.Day(
        day=date.fromisoformat(manifest["day"]),
        decision_utc=run,
        slate=bundle.inputs["slate"],
        quotes=bundle.inputs["quotes"],
        fitted=fitted,
        live=live,
        bankroll=0.0,
        versions={
            "blend_version": manifest["blend_version"],
            "feature_build": manifest["feature_build"],
            "code_version": manifest["code_version"],
            "b2_train_cutoff": fitted.b2_cutoff if fitted else None,
            "b3_train_cutoff": fitted.b3_cutoff if fitted else None,
        },
        published_utc=datetime.fromisoformat(manifest["published_utc"]),
    )
    return rows(predict(day), day)


def check(store: lb.Store, day: date, live: blend_fit.LiveFit) -> list[str]:
    """Where a stored day's rows and those replayed from its bundle disagree (bundle.differences),
    or why the day can't be replayed."""
    try:
        bundle = lb.read(store, day, BUNDLES)
        again = replay(bundle, live)
    except Exception as exc:
        return [f"{day}: {exc}"]
    return [f"{day}: {problem}" for problem in lb.differences(again, stored(store, day))]


def identity(
    inputs: lp.Day, cutoff: datetime, published: datetime, live_fit: str, live_sha256: str
) -> dict[str, object]:
    """The bundle manifest's identity of the day's run: what replay() reads back."""
    return {
        "day": inputs.day.isoformat(),
        "run_utc": inputs.decision_utc.isoformat(),
        "published_utc": published.isoformat(),
        "input_cutoff": cutoff.isoformat(),
        "morning_version": VERSION,
        "blend_version": inputs.live.version,
        "live_fit": live_fit,
        "live_fit_sha256": live_sha256,
        "feature_build": inputs.versions["feature_build"],
        "code_version": inputs.versions["code_version"],
    }


def counts(frame: pl.DataFrame) -> str:
    """The rows by status, for a log that shows counts only."""
    by = frame.group_by("status").len().sort("status").iter_rows()
    return ", ".join(f"{n} {status}" for status, n in by)
