"""Challengers (#255, ADR 0038): candidate models scored on future games only, from each decision
day's run bundle, after the decision and never as part of it.

Every history season that could test a new model has been seen, so a challenger earns its
evidence on this season's decision days alongside the frozen policy, without touching it:

- **Registration:** a challenger is an entry in REGISTRY: its name, version, ADR, registration
  date and the lake tables it reads beyond the bundle. Its ADR fixes its specification before its
  first scored game; a changed challenger is a new version with a new registration.
- **Evidence:** only decision days after the registration date, never backfilled. A day's
  output comes from the day's complete run bundle (live/bundle.py), the day replayed with the
  committed live fit it names (and refused unless it reproduces the day's ledger), and the lake
  read point in time (PointInTime): only the tables in READERS, each through its reviewed
  reader, and each read recorded by the sha256 of its rows. The lake is mutable: the nightly
  rewrites a date's rows once its games are final, so the slate's own inputs come from the
  bundle, and the lake gives only rows of earlier games.
- **Output:** one row per slate game (ChallengerPredictions), written once per challenger,
  version and day under PREFIX in R2 and rebuilt into the lake's challenger_predictions. check()
  recomputes a stored day and names what differs.

The live report (live/report.py) scores each against the frozen blend and B1, with its shadow
bets under the frozen rules (live/shadow.py) and its disagreement against the move to the close
(live/movement.py), with no verdict before the formal review.
"""

import io
import json
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Protocol

import polars as pl

from nhl_edge.ingest.games import results_known_at, schedule_known_at
from nhl_edge.lake.schemas import ChallengerPredictions, dtypes
from nhl_edge.live import blend_fit
from nhl_edge.live import bundle as lb
from nhl_edge.live import predict as lp

PREFIX = "challengers/live"
PREDICTED = lp.PREDICTED
# A null probability the challenger gave no reason for, and a slate game it left out.
NO_PROBABILITY = "no probability"
LEFT_OUT = "not returned by the challenger"
# Odds columns set after a quote was observed (Codex on #265): the closing proxy flag, set once the
# game has started, and the game a quote was matched to, which follows a postponement to the
# rescheduled game (lake/schemas.LakeOddsSnapshots).
ODDS_AFTERWARDS = ("is_closing_proxy", "game_id", "game_type")


class Reader(Protocol):
    """The lake as PointInTime reads it (lake/tables.Lake)."""

    def read(self, table: str, seasons: Collection[int] | None = None) -> pl.DataFrame: ...


def _odds(frame: pl.DataFrame, cutoff: datetime, predicting: tuple[int, ...]) -> pl.DataFrame:
    """The quotes observed before the cutoff, as they were then: by their event, without the
    columns set afterwards."""
    return frame.filter(pl.col("snapshot_utc") < cutoff).drop(ODDS_AFTERWARDS)


# The lake tables a challenger may read, each through a reader that returns only what was known
# before the cutoff, given the slate's games (predicting). Not every table is safe by its
# observed_utc alone (Codex on #265): the schedule holds only games that went on to be played,
# and the odds carry columns set after the fact. Any other table is refused until a challenger's
# ADR reviews it and adds its reader here, with a test in tests/leakage/test_challengers.py.
READERS: dict[str, Callable[[pl.DataFrame, datetime, tuple[int, ...]], pl.DataFrame]] = {
    # Results public before the cutoff.
    "games": lambda frame, cutoff, predicting: results_known_at(frame, cutoff),
    # Games already played, and the slate's own once their schedule is public.
    "schedule": lambda frame, cutoff, predicting: schedule_known_at(frame, cutoff, predicting),
    "odds_snapshots": _odds,
}


@dataclass
class PointInTime:
    """The lake as a challenger may read it for one bundle: only the tables it registered, each
    through its reader in READERS at the bundle's input cutoff, for the slate's games
    (predicting). reads records the sha256 of the rows each read returned, so a later change to
    them shows when the day is checked."""

    lake: Reader
    cutoff: datetime
    allowed: tuple[str, ...]
    predicting: tuple[int, ...] = ()
    reads: dict[str, str] = field(default_factory=dict)

    def read(self, table: str, seasons: Collection[int] | None = None) -> pl.DataFrame:
        if table not in self.allowed:
            raise ValueError(f"{table} is not among the tables the challenger registered")
        if table not in READERS:
            raise ValueError(f"{table} has no point-in-time reader, so a challenger can't read it")
        rows = READERS[table](self.lake.read(table, seasons), self.cutoff, self.predicting)
        name = table if seasons is None else f"{table}:{','.join(map(str, sorted(seasons)))}"
        self.reads[name] = lb.sha256(lb.parquet(rows))
        return rows


# A challenger's probabilities for the day: from its bundle, the day replayed (the ledger's
# columns), the live fit and the lake point in time, one row per game it predicted (game_id,
# p_home, and status when p_home is null).
Predict = Callable[[lb.Bundle, pl.DataFrame, blend_fit.LiveFit, PointInTime], pl.DataFrame]


@dataclass(frozen=True)
class Challenger:
    name: str
    version: str
    adr: str
    registered_on: date
    reads: tuple[str, ...]
    predict: Predict

    def __post_init__(self) -> None:
        unread = sorted(set(self.reads) - set(READERS))
        if unread:
            raise ValueError(f"{self.name}: no point-in-time reader for {', '.join(unread)}")

    def counts(self, day: date) -> bool:
        """Whether a decision day is evidence: only days after the registration (ADR 0038)."""
        return day > self.registered_on

    def key(self, day: date) -> str:
        return f"{PREFIX}/{self.name}/{self.version}/{day.isoformat()}.parquet"


def _market_only(
    bundle: lb.Bundle, replayed: pl.DataFrame, live: blend_fit.LiveFit, lake: PointInTime
) -> pl.DataFrame:
    """The market-only control's probability as the decision computed it (p_blend_market): a
    check of the mechanism, whose output is the ledger's own."""
    return replayed.select("game_id", "status", p_home="p_blend_market")


REGISTRY: tuple[Challenger, ...] = (
    Challenger(
        name="market_only",
        version="1",
        adr="0038",
        registered_on=date(2026, 10, 15),
        reads=(),
        predict=_market_only,
    ),
)


def complete_days(store: lb.Store) -> list[date]:
    """The decision days whose run bundle is complete, with its manifest."""
    return sorted(
        date.fromisoformat(key.removeprefix(f"{lb.PREFIX}/").split("/")[0])
        for key in store.keys(lb.PREFIX)
        if key.endswith(f"/{lb.MANIFEST}")
    )


def due(store: lb.Store, registry: tuple[Challenger, ...]) -> list[tuple[Challenger, date]]:
    """Each challenger and complete decision day after its registration without an output yet."""
    stored = set(store.keys(PREFIX))
    days = complete_days(store)
    return [
        (challenger, day)
        for challenger in registry
        for day in days
        if challenger.counts(day) and challenger.key(day) not in stored
    ]


def stored(store: lb.Store, registry: tuple[Challenger, ...]) -> list[tuple[Challenger, date]]:
    """Each registered challenger and day with a stored output, whether or not its bundle is still
    complete."""
    keys = set(store.keys(PREFIX))
    return [
        (challenger, date.fromisoformat(key.rsplit("/", 1)[1].removesuffix(".parquet")))
        for challenger in registry
        for key in sorted(keys)
        if key.startswith(f"{PREFIX}/{challenger.name}/{challenger.version}/")
        and key.endswith(".parquet")
    ]


def load(store: lb.Store, day: date) -> tuple[lb.Bundle, str]:
    """The day's complete bundle and its manifest's sha256."""
    bundle = lb.read(store, day)
    return bundle, lb.sha256(store.get(f"{lb.prefix(day)}/{lb.MANIFEST}"))


def live_fit(bundle: lb.Bundle) -> blend_fit.LiveFit:
    """The committed live fit the bundle names, refused unless its bytes are the ones decided
    with."""
    path = blend_fit.REPORTS / bundle.manifest["live_fit"]
    body = path.read_bytes()
    if lb.sha256(body) != bundle.manifest["live_fit_sha256"]:
        raise ValueError(f"{path} is not the live fit the day was decided with")
    return blend_fit.load(json.loads(body))


def run(
    challenger: Challenger,
    bundle: lb.Bundle,
    bundle_sha256: str,
    live: blend_fit.LiveFit,
    lake: Reader,
    code_version: str,
    now: datetime,
    ledger: pl.DataFrame | None = None,
) -> pl.DataFrame:
    """The challenger's output for the bundle's day: one row per slate game. Refused for a day on
    or before its registration, and, given the day's ledger, unless the bundle replays it."""
    day = date.fromisoformat(bundle.manifest["day"])
    if not challenger.counts(day):
        raise ValueError(
            f"{challenger.name} {challenger.version} was registered on "
            f"{challenger.registered_on}, so {day} is never its evidence"
        )
    replayed = lb.replay(bundle, live)
    if ledger is not None:
        problems = lb.differences(replayed, ledger)
        if problems:
            raise ValueError(f"{day}: the bundle doesn't replay the ledger ({'; '.join(problems)})")
    cutoff = datetime.fromisoformat(bundle.manifest["input_cutoff"])
    slate = bundle.inputs["slate"]
    reader = PointInTime(lake, cutoff, challenger.reads, tuple(slate["game_id"].to_list()))
    predicted = challenger.predict(bundle, replayed, live, reader)
    if "status" not in predicted.columns:
        predicted = predicted.with_columns(status=pl.lit(None, pl.String))
    p = pl.col("p_home")
    rows = (
        slate.select("game_date", "season", "game_id")
        .join(predicted.select("game_id", "p_home", "status"), on="game_id", how="left")
        .with_columns(
            status=pl.when(p.is_not_null())
            .then(pl.lit(PREDICTED))
            .when(pl.col("status").is_null())
            .then(pl.lit(LEFT_OUT))
            .when(pl.col("status") == PREDICTED)
            .then(pl.lit(NO_PROBABILITY))
            .otherwise(pl.col("status")),
            challenger=pl.lit(challenger.name),
            challenger_version=pl.lit(challenger.version),
            adr=pl.lit(challenger.adr),
            bundle_sha256=pl.lit(bundle_sha256),
            input_cutoff_utc=pl.lit(cutoff),
            reads=pl.lit(json.dumps(reader.reads, sort_keys=True)),
            code_version=pl.lit(code_version),
            computed_utc=pl.lit(now),
        )
    )
    columns = dtypes(ChallengerPredictions)
    return ChallengerPredictions.validate(
        rows.select(list(columns)).cast(columns).sort("game_id")  # type: ignore[arg-type]
    )


def write(store: lb.Store, challenger: Challenger, day: date, output: pl.DataFrame) -> str:
    """Write the day's output once, and return its key: the same bytes again count as written,
    other bytes are a Conflict."""
    key = challenger.key(day)
    lb.put_once(store, key, lb.parquet(output))
    return key


def empty() -> pl.DataFrame:
    return pl.DataFrame(schema=dtypes(ChallengerPredictions))


def sync(store: lb.Store) -> pl.DataFrame:
    """Every stored output, as the lake's challenger_predictions."""
    frames = [
        pl.read_parquet(io.BytesIO(store.get(key)))
        for key in store.keys(PREFIX)
        if key.endswith(".parquet")
    ]
    if not frames:
        return empty()
    columns = dtypes(ChallengerPredictions)
    return ChallengerPredictions.validate(pl.concat(frames).cast(columns))  # type: ignore[arg-type]


def check(
    store: lb.Store,
    challenger: Challenger,
    day: date,
    live: blend_fit.LiveFit,
    lake: Reader,
    now: datetime,
    ledger: pl.DataFrame | None = None,
) -> list[str]:
    """Where the stored output of the day and the one recomputed from its bundle disagree: other
    games, a probability beyond lb.TOLERANCE, a status, another bundle or cutoff, or lake rows
    read that have changed since."""
    stored = pl.read_parquet(io.BytesIO(store.get(challenger.key(day)))).sort("game_id")
    bundle, sha = load(store, day)
    code = stored["code_version"][0] if stored.height else ""
    again = run(challenger, bundle, sha, live, lake, code, now, ledger)
    if not again["game_id"].equals(stored["game_id"]):
        return [f"games differ: {again['game_id'].to_list()} against {stored['game_id'].to_list()}"]
    problems = []
    a, b = again["p_home"], stored["p_home"]
    apart = (a.is_null() != b.is_null()) | ((a - b).abs() > lb.TOLERANCE).fill_null(False)
    for column, differs in (
        ("p_home", apart),
        *(
            (column, again[column].ne_missing(stored[column]))
            for column in ("status", "bundle_sha256", "input_cutoff_utc")
        ),
    ):
        if differs.any():
            problems.append(f"{column} differs for {again.filter(differs)['game_id'].to_list()}")
    if again.height and again["reads"][0] != stored["reads"][0]:
        changed = json.loads(again["reads"][0])
        before = json.loads(stored["reads"][0])
        tables = sorted(
            k for k in changed.keys() | before.keys() if changed.get(k) != before.get(k)
        )
        problems.append(f"the lake rows read have changed since: {', '.join(tables)}")
    return problems
