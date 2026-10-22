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
- **Output:** one row per slate game (ChallengerPredictions), with the day's fit for a
  challenger that fits one, written once per challenger, version and day under PREFIX in R2 and
  rebuilt into the lake's challenger_predictions. check() recomputes a stored day and names what
  differs.

The live report (live/report.py) scores each against the frozen blend and B1, with its shadow
bets under the frozen rules (live/shadow.py) and its disagreement against the move to the close
(live/movement.py), with no verdict before the formal review.
"""

import io
import json
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol

import numpy as np
import polars as pl
from numpy.typing import ArrayLike, NDArray

from nhl_edge.backtest.walk_forward import outcomes
from nhl_edge.betting.selection import POLICY_V2
from nhl_edge.ingest.games import results_known_at, schedule_known_at
from nhl_edge.ingest.odds import ET
from nhl_edge.lake.schemas import ChallengerPredictions, dtypes
from nhl_edge.live import blend_fit
from nhl_edge.live import bundle as lb
from nhl_edge.live import predict as lp
from nhl_edge.live.settle import POSTPONED
from nhl_edge.market.recalibration import logit, sigmoid

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


def _ledger(frame: pl.DataFrame, cutoff: datetime, predicting: tuple[int, ...]) -> pl.DataFrame:
    """The paper decisions published before the cutoff (ADR 0041). A ledger is written once
    (ADR 0033) and has no column set after its publication: settlements are their own table.
    Sorted, so its sha256 doesn't depend on where the ledgers were read from."""
    return frame.filter(pl.col("published_utc") < cutoff).sort("game_date", "game_id")


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
    "paper_ledger": _ledger,
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


@dataclass
class WithLedgers:
    """The lake with paper_ledger read from the ledgers themselves, the record (ledger/live/ in
    R2, or a dry run's files), rather than from the lake's copy of them."""

    lake: Reader
    ledgers: pl.DataFrame

    def read(self, table: str, seasons: Collection[int] | None = None) -> pl.DataFrame:
        if table != "paper_ledger":
            return self.lake.read(table, seasons)
        if seasons is None:
            return self.ledgers
        return self.ledgers.filter(pl.col("season").is_in(list(seasons)))


# A challenger's probabilities for the day: from its bundle, the day replayed (the ledger's
# columns), the live fit and the lake point in time, one row per game it predicted (game_id,
# p_home, status when p_home is null, and fit, the day's fit as JSON, when it fits one).
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


# conservative_blend (ADR 0041): logit p = a + b_m·logit p_mkt + b_d·(logit p_B3 - logit p_mkt),
# fitted by maximum a posteriori under fixed normal priors on the earlier ET weeks' v2 decisions.
CONSERVATIVE_TERMS = ("a", "b_m", "b_d")
PRIOR_MEAN = np.array([0.0, 1.0, 0.0])
PRIOR_SD = np.array([0.05, 0.15, 0.5])
# Fewer training games leave the week unfitted, with no probability.
MINIMUM_GAMES = 200
UNFITTED = "unfitted"
# The policy whose decisions it trains on, named: v1's B3 read no confirmed starters, and a later
# policy's decisions join only as a new version.
TRAINS_ON = POLICY_V2
ITERATIONS = 50
CONVERGED = 1e-10


def conservative_design(p_mkt: ArrayLike, p_b3: ArrayLike) -> NDArray[np.float64]:
    """The fit's columns: 1, logit p_mkt and B3's disagreement with it, logit p_B3 - logit
    p_mkt."""
    market = logit(p_mkt)
    return np.column_stack([np.ones_like(market), market, logit(p_b3) - market])


def conservative_fit(
    home_win: ArrayLike, p_mkt: ArrayLike, p_b3: ArrayLike
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """The weights a, b_m and b_d at the posterior mode, by Newton-Raphson from the prior mean,
    and their standard errors from the posterior's curvature there. The priors keep the fit
    finite on any games, all home wins included."""
    x = conservative_design(p_mkt, p_b3)
    y = np.asarray(home_win, dtype=np.float64)
    precision = np.diag(1 / PRIOR_SD**2)
    beta = PRIOR_MEAN.copy()
    for _ in range(ITERATIONS):
        p = sigmoid(x @ beta)
        hessian = x.T @ (x * (p * (1 - p))[:, None]) + precision
        step = np.linalg.solve(hessian, x.T @ (y - p) - precision @ (beta - PRIOR_MEAN))
        beta = beta + step
        if np.max(np.abs(step)) < CONVERGED:
            break
    else:
        raise ValueError(f"conservative_blend did not converge in {ITERATIONS} iterations")
    p = sigmoid(x @ beta)
    hessian = x.T @ (x * (p * (1 - p))[:, None]) + precision
    return beta, np.sqrt(np.diag(np.linalg.inv(hessian)))


def week_of(day: date) -> date:
    """The Monday of the day's ET week: the slate's date is its ET date."""
    return day - timedelta(days=day.weekday())


def fold_start(day: date) -> datetime:
    """The day's week's fold start: its Monday at 12:45 ET, when the decision window opens
    (live/predict.WINDOW), so at or before every input cutoff of the week."""
    return datetime.combine(week_of(day), lp.WINDOW[0], ET).astimezone(UTC)


def conservative_training(day: date, seasons: list[int], lake: PointInTime) -> pl.DataFrame:
    """The games the day's fit learns from (ADR 0041): TRAINS_ON's predicted decisions of the
    ET weeks before the day's, published, and with their full-game result public, before the
    week's fold start (Codex on #304). Every day of a week so fits on the same games, never a
    result that turned public during the week. A decision whose game was played more than
    POSTPONED from the start it was decided for is void, as the evaluation counts it
    (live/report.scoreable); its game counts once, from its new date's decision."""
    start = fold_start(day)
    ledger = lake.read("paper_ledger", seasons).filter(
        pl.col("policy_version") == TRAINS_ON,
        pl.col("status") == PREDICTED,
        pl.col("game_date") < week_of(day),
        pl.col("published_utc") < start,
        pl.col("p_b0").is_not_null(),
        pl.col("p_b3").is_not_null(),
    )
    games = lake.read("games", seasons)
    results = (
        outcomes(games)
        .join(games.select("game_id", played_utc="start_utc"), on="game_id")
        .filter(pl.col("result_utc") < start)
    )
    return (
        ledger.select("game_date", "game_id", "start_utc", "p_b0", "p_b3")
        .join(results, on="game_id")
        .filter((pl.col("played_utc") - pl.col("start_utc")).abs() <= POSTPONED)
        .sort("game_date", "game_id")
    )


def _conservative_blend(
    bundle: lb.Bundle, replayed: pl.DataFrame, live: blend_fit.LiveFit, lake: PointInTime
) -> pl.DataFrame:
    """The day's probabilities from the week's fit, or none, as unfitted, while fewer than
    MINIMUM_GAMES games train it. The fit, its games and its week ride on every row (fit)."""
    day = date.fromisoformat(bundle.manifest["day"])
    rows = conservative_training(day, sorted(set(replayed["season"].to_list())), lake)
    fit: dict[str, Any] = {
        "week": week_of(day).isoformat(),
        "games": rows.height,
        "minimum": MINIMUM_GAMES,
    }
    if rows.height < MINIMUM_GAMES:
        return replayed.select(
            "game_id",
            p_home=pl.lit(None, pl.Float64),
            status=pl.when(pl.col("status") == PREDICTED)
            .then(pl.lit(UNFITTED))
            .otherwise(pl.col("status")),
            fit=pl.lit(json.dumps(fit)),
        )
    weights, errors = conservative_fit(rows["home_win"], rows["p_b0"], rows["p_b3"])
    fit |= {
        "weights": dict(zip(CONSERVATIVE_TERMS, weights.tolist(), strict=True)),
        "standard_errors": dict(zip(CONSERVATIVE_TERMS, errors.tolist(), strict=True)),
        "train_cutoff": rows["result_utc"].max().isoformat(),  # type: ignore[union-attr]
    }
    priced = replayed.filter(
        pl.col("status") == PREDICTED, pl.col("p_b0").is_not_null(), pl.col("p_b3").is_not_null()
    )
    p = sigmoid(conservative_design(priced["p_b0"], priced["p_b3"]) @ weights)
    return (
        replayed.select("game_id", "status")
        .join(
            priced.select("game_id", p_home=pl.Series(p, dtype=pl.Float64)),
            on="game_id",
            how="left",
        )
        .with_columns(fit=pl.lit(json.dumps(fit)))
    )


REGISTRY: tuple[Challenger, ...] = (
    Challenger(
        name="market_only",
        version="1",
        adr="0038",
        registered_on=date(2026, 10, 15),
        reads=(),
        predict=_market_only,
    ),
    Challenger(
        name="conservative_blend",
        version="1",
        adr="0041",
        registered_on=date(2026, 10, 22),
        reads=("paper_ledger", "games"),
        predict=_conservative_blend,
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
    for column in ("status", "fit"):
        if column not in predicted.columns:
            predicted = predicted.with_columns(pl.lit(None, pl.String).alias(column))
    p = pl.col("p_home")
    rows = (
        slate.select("game_date", "season", "game_id")
        .join(predicted.select("game_id", "p_home", "status", "fit"), on="game_id", how="left")
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
    """Every stored output, as the lake's challenger_predictions. An output written before fit
    joined the schema reads it as null."""
    frames = [
        pl.read_parquet(io.BytesIO(store.get(key)))
        for key in store.keys(PREFIX)
        if key.endswith(".parquet")
    ]
    if not frames:
        return empty()
    columns = dtypes(ChallengerPredictions)
    every = pl.concat([empty(), *frames], how="diagonal_relaxed").select(list(columns))
    return ChallengerPredictions.validate(every.cast(columns))  # type: ignore[arg-type]


def _apart(a: Any, b: Any) -> bool:
    """Whether two fits read from JSON differ: a number by more than lb.TOLERANCE, anything
    else at all."""
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() != b.keys() or any(_apart(a[k], b[k]) for k in a)
    if isinstance(a, int | float) and isinstance(b, int | float):
        return abs(a - b) > lb.TOLERANCE
    return a != b


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
    games, a probability beyond lb.TOLERANCE, a status, another bundle, cutoff or fit, or lake
    rows read that have changed since."""
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
    # An output written before fit joined the schema has none.
    before = stored["fit"] if "fit" in stored.columns else [None] * stored.height
    pairs = zip(again["fit"], before, strict=True)
    if any(_apart(*(json.loads(f) if f else None for f in pair)) for pair in pairs):
        problems.append("the fit differs")
    if again.height and again["reads"][0] != stored["reads"][0]:
        changed = json.loads(again["reads"][0])
        before = json.loads(stored["reads"][0])
        tables = sorted(
            k for k in changed.keys() | before.keys() if changed.get(k) != before.get(k)
        )
        problems.append(f"the lake rows read have changed since: {', '.join(tables)}")
    return problems
