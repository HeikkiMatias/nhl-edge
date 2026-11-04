"""The close forecaster (#369, ADR 0054): B3's split at the morning snapshot forecasts Pinnacle's
close, as a fair value for early quotes. No bet or order reads it.

On the training seasons, B3's four parts anticipated part of the market's move from the opener to
the close (ADR 0053, the claim early-information), but added nothing beyond the close: the model
can't beat the closing price, but it can partly forecast it. Each game day, right after the morning
view (ADR 0052) and from its bundle:

- **o:** the home log-odds of the morning view's Pinnacle pair (its p_b0, de-vigged
  multiplicatively, hard rule 3).
- **x:** B3's four parts (b3.split_inputs, ADR 0044) from the morning bundle's B3 rows, the goalie
  part averaged over the goalie scenarios the morning's B3 used (bundle.b3_scenarios).
- **The forecast:** p_close_hat = sigmoid(o + lam * y_hat_hist), y_hat_hist = a + b_o*o + b*x with
  the frozen weights (WEIGHTS, fitted on SBR's 2012-13 to 2017-18, pinned by WEIGHTS_SHA256), and
  lam the week's size.
- **lam:** fitted once per ET week (Monday to Sunday) at the posterior mode of y = lam * y_hat_hist
  + e under lam ~ N(PRIOR_MEAN, PRIOR_SD^2), the residual variance estimated, on the forecasts of
  earlier weeks whose Pinnacle closing proxy came after their morning snapshot. Every row and its
  close were public before the week's fold start, Monday at 07:00 ET, before any morning of the
  week (hard rule 1). Without such rows lam is PRIOR_MEAN. The fit and its rows are written once
  under WEEKS, so every morning of the week reads the same lam, and a replay reads it back.
- **The record:** each date is written once (forecast/live/<date>.parquet), and every forecast is
  published before its game's start. check() replays a stored day from the morning's bundle and
  rows and the week's stored fit.

The report (live/report.py) scores it on the move from the morning snapshot to the close, with no
verdict before the formal review, and #358's quoting simulator quotes at it.

**Version 2** (ADR 0056) runs beside version 1, which it leaves unchanged. Each is a Spec in
VERSIONS, with its own registration, weights, record (PREFIX, PREFIX_2) and weekly sizes:
- **d:** #370's five schedule density columns (features/density.py) at the morning bundle's input
  cutoff, from the bundle's slate and the season's games whose results were public before it
  (ADR 0003, ingest/games.results_known_at), never tonight's or a later game's result.
- **The forecast:** p_close_hat = sigmoid(o + lam * y_hat_hist + lam_d * y_hat_density), with
  y_hat_hist = a + b_o*o + b*x and y_hat_density = b_d*d from version 2's frozen weights
  (WEIGHTS_2, fitted on SBR's 2012-13 to 2017-18 beside the split, pinned by WEIGHTS_2_SHA256).
- **lam and lam_d:** fitted once per ET week, at the posterior mode of y = lam*y_hat_hist +
  lam_d*y_hat_density + e under independent N(PRIOR_MEAN, PRIOR_SD^2) priors, with the residual
  variance estimated, on version 2's own forecasts of earlier weeks, as version 1's lam. Without
  rows both are PRIOR_MEAN.
"""

import io
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from nhl_edge.features.density import DENSITY, density
from nhl_edge.game import b3
from nhl_edge.ingest.games import results_known_at
from nhl_edge.ingest.odds import ET
from nhl_edge.lake.schemas import CloseForecasts, dtypes
from nhl_edge.live import blend_fit, movement
from nhl_edge.live import bundle as lb
from nhl_edge.live import morning as lm
from nhl_edge.live import predict as lp
from nhl_edge.live import settle as ls

# The experiment as ADR 0054 registers it: a changed specification is a new version, and its
# evidence starts again. Only game days after the registration count.
VERSION = "close_forecast/1"
REGISTERED_ON = date(2026, 11, 2)
PREFIX = "forecast/live"
WEEKS = f"{PREFIX}/weeks"
WEIGHTS = blend_fit.REPORTS / "close-forecast-20261102-1ecc8a2.json"
WEIGHTS_SHA256 = "19b4aafd25364cf555997fd7a4449b2832c8e21c5847a0208d94a4cfe268a36e"
PRIOR_MEAN, PRIOR_SD = 1.0, 0.5
FOLD_START = time(7, 0)
# The residual variance's least, so a week of moves all alike still fits.
VARIANCE_FLOOR = 1e-8
ITERATIONS = 200
CONVERGED = 1e-10

PREDICTED = lp.PREDICTED
NO_MORNING = "no morning prediction"
NO_PARTS = "no B3 inputs"
NO_DENSITY = "no schedule density"
STARTS_FIRST = "starts before the forecast is published"

# Version 2 (ADR 0056), registered on the day its code merged: density beside the split, on its
# own size.
VERSION_2 = "close_forecast/2"
REGISTERED_ON_2 = date(2026, 11, 5)
PREFIX_2 = "forecast/live-2"
WEIGHTS_2 = blend_fit.REPORTS / "close-forecast-2-20261104-863eb1a.json"
WEIGHTS_2_SHA256 = "65a5c95a9c87d61b80e41974721504698a785518afd57b655dbe4676266a057f"
UTC_TYPE = pl.Datetime("us", "UTC")


@dataclass(frozen=True)
class Spec:
    """A registered version of the forecaster: its name, registration, record and weekly sizes'
    prefix, its pinned weights, and whether it reads schedule density (ADR 0056)."""

    version: str
    registered_on: date
    prefix: str
    weights: Path
    weights_sha256: str
    density: bool = False

    @property
    def weeks(self) -> str:
        return f"{self.prefix}/weeks"


V1 = Spec(VERSION, REGISTERED_ON, PREFIX, WEIGHTS, WEIGHTS_SHA256)
V2 = Spec(VERSION_2, REGISTERED_ON_2, PREFIX_2, WEIGHTS_2, WEIGHTS_2_SHA256, density=True)
VERSIONS = (V1, V2)
SPECS = {spec.version: spec for spec in VERSIONS}


def registered(game_date: date) -> tuple[Spec, ...]:
    """The versions that forecast a game date: those registered by then."""
    return tuple(spec for spec in VERSIONS if spec.registered_on <= game_date)


@dataclass(frozen=True)
class Weights:
    """The frozen weights (ADR 0054): the move a + b_o*o + parts . x, in home log-odds, and for
    version 2 density's b_d . d beside it (ADR 0056)."""

    version: str
    train_cutoff: datetime
    a: float
    b_o: float
    parts: dict[str, float]
    density: dict[str, float] = field(default_factory=dict)


def weights(spec: Spec | None = None) -> Weights:
    """The version's committed weights (version 1's by default), refused unless their bytes are
    the pinned ones."""
    spec = spec or V1
    body = spec.weights.read_bytes()
    if lb.sha256(body) != spec.weights_sha256:
        raise ValueError(f"{spec.weights} is not the weights {spec.version} was registered with")
    record = json.loads(body)
    found = record["weights"]
    return Weights(
        version=record["version"],
        train_cutoff=datetime.fromisoformat(record["train_cutoff"]),
        a=float(found["a"]),
        b_o=float(found["b_o"]),
        parts={part: float(found[part]) for part in b3.SPLIT},
        density={column: float(found[column]) for column in DENSITY} if spec.density else {},
    )


def _logit(p: pl.Expr) -> pl.Expr:
    return (p / (1 - p)).log()


def week_of(day: date) -> date:
    """The Monday of day's ET week."""
    return day - timedelta(days=day.weekday())


def fold_start(week: date) -> datetime:
    """The week's fold start: its Monday at 07:00 ET, before any morning of the week."""
    return datetime.combine(week, FOLD_START, ET).astimezone(UTC)


def parts(bundle: lb.Bundle) -> pl.DataFrame:
    """Each slate game the morning's B3 could read, with B3's four parts: the three no goalie
    changes from its inputs, and the goalie part averaged over its goalie pairs by their weights,
    as B3 mixes them (bundle.b3_scenarios). Empty when the morning read no models."""
    if bundle.b3 is None:
        return pl.DataFrame(schema={"game_id": pl.Int64, **dict.fromkeys(b3.SPLIT, pl.Float64)})
    usable, pairs = lb.b3_scenarios(bundle)
    fixed = b3.split_inputs(usable).select("game_id", *b3.SPLIT[:3])
    goalie = pairs.group_by("game_id").agg(
        goalie_diff=(pl.col("goalie_diff") * pl.col("weight")).sum() / pl.col("weight").sum()
    )
    return fixed.join(goalie, on="game_id").select("game_id", *b3.SPLIT).sort("game_id")


GAME_COLUMNS = ("game_id", "season", "game_date", "start_utc", "home", "away")


def density_inputs(bundle: lb.Bundle, games: pl.DataFrame) -> pl.DataFrame:
    """Each slate game of the morning bundle with its density columns (features/density.py, ADR
    0056) at the bundle's input cutoff: from the bundle's slate, public a day before (ADR 0005),
    and the season's games whose results were public before the cutoff (ADR 0003,
    results_known_at). Neither a slate game's result nor any later game is read."""
    slate = bundle.inputs["slate"]
    cutoff = datetime.fromisoformat(bundle.manifest["input_cutoff"])
    seasons = slate["season"].unique().implode()
    known = results_known_at(games.filter(pl.col("season").is_in(seasons)), cutoff).filter(
        ~pl.col("game_id").is_in(slate["game_id"].implode())
    )
    types: dict[Any, Any] = {"game_id": pl.Int64, "season": pl.Int32, "start_utc": UTC_TYPE}
    every = pl.concat(
        [
            known.select(*GAME_COLUMNS, "observed_utc").cast(types | {"observed_utc": UTC_TYPE}),
            # Tonight's games count as no one's earlier game: their result is not public.
            slate.select(*GAME_COLUMNS)
            .cast(types)
            .with_columns(observed_utc=pl.lit(None, dtype=UTC_TYPE)),
        ]
    )
    moments = slate.select("game_id", prediction_utc=pl.lit(cutoff, dtype=UTC_TYPE))
    return density(every, moments).sort("game_id")


def history_move(frame: pl.DataFrame, w: Weights) -> pl.Expr:
    """The frozen weights' move for frame's rows (o, the four parts): version 2's split part."""
    move = pl.lit(w.a) + w.b_o * pl.col("o")
    for part, weight in w.parts.items():
        move = move + weight * pl.col(part)
    return move


def density_move(w: Weights) -> pl.Expr:
    """Version 2's density part of the move, b_d . d (ADR 0056)."""
    move = pl.lit(0.0)
    for column, weight in w.density.items():
        move = move + weight * pl.col(column)
    return move


# The week's size


@dataclass(frozen=True)
class Fit:
    """A week's size: lam at the posterior mode, its residual variance, and the rows it read
    (game_date, game_id, y_hat_hist, y), with the cutoff they were public by. For version 2, lam
    is the split's size and lam_d density's, and its rows hold y_hat_density too (ADR 0056)."""

    week: date
    cutoff: datetime
    lam: float
    variance: float | None
    rows: pl.DataFrame
    lam_d: float | None = None
    version: str = VERSION

    def record(self, weights_version: str) -> dict[str, Any]:
        record = {
            "forecast_version": self.version,
            "weights_version": weights_version,
            "week": self.week.isoformat(),
            "cutoff": self.cutoff.isoformat(),
            "lam": self.lam,
            "variance": self.variance,
            "games": self.rows.height,
        }
        return record if self.lam_d is None else record | {"lam_d": self.lam_d}


ROW_SCHEMA = {
    "game_date": pl.Date,
    "game_id": pl.Int64,
    "y_hat_hist": pl.Float64,
    "y": pl.Float64,
}
ROW_SCHEMA_2 = {
    "game_date": pl.Date,
    "game_id": pl.Int64,
    "y_hat_hist": pl.Float64,
    "y_hat_density": pl.Float64,
    "y": pl.Float64,
}


def row_schema(spec: Spec) -> dict[str, Any]:
    return ROW_SCHEMA_2 if spec.density else ROW_SCHEMA


def fit_lam(y_hat: np.ndarray, y: np.ndarray) -> tuple[float, float | None]:
    """lam at the posterior mode of y = lam * y_hat + e, e ~ N(0, variance), under lam ~
    N(PRIOR_MEAN, PRIOR_SD^2), and the variance estimated from the rows: from the moves' variance,
    the mode at the variance and the variance at the mode in turn, until it settles. With no
    rows, lam is PRIOR_MEAN and no variance."""
    if len(y) == 0:
        return PRIOR_MEAN, None
    precision = 1 / PRIOR_SD**2
    variance = max(float(np.var(y)), VARIANCE_FLOOR)
    for _ in range(ITERATIONS):
        lam = (float(y_hat @ y) / variance + precision * PRIOR_MEAN) / (
            float(y_hat @ y_hat) / variance + precision
        )
        again = max(float(np.mean((y - lam * y_hat) ** 2)), VARIANCE_FLOOR)
        settled = abs(again - variance) <= CONVERGED * variance
        variance = again
        if settled:
            break
    else:
        raise ValueError(f"lam's fit did not converge in {ITERATIONS} iterations")
    lam = (float(y_hat @ y) / variance + precision * PRIOR_MEAN) / (
        float(y_hat @ y_hat) / variance + precision
    )
    return lam, variance


def fit_sizes(x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, float | None]:
    """Version 2's sizes (ADR 0056): the posterior mode of y = x . lam + e, e ~ N(0, variance),
    under independent N(PRIOR_MEAN, PRIOR_SD^2) priors on each of x's columns' sizes, the
    variance estimated from the rows as fit_lam's is. With no rows, every size is PRIOR_MEAN and
    no variance."""
    k = x.shape[1]
    mean = np.full(k, PRIOR_MEAN)
    if len(y) == 0:
        return mean, None
    precision = np.eye(k) / PRIOR_SD**2

    def mode(variance: float) -> np.ndarray:
        gram = x.T @ x / variance + precision
        return np.linalg.solve(gram, x.T @ y / variance + precision @ mean)

    variance = max(float(np.var(y)), VARIANCE_FLOOR)
    for _ in range(ITERATIONS):
        again = max(float(np.mean((y - x @ mode(variance)) ** 2)), VARIANCE_FLOOR)
        settled = abs(again - variance) <= CONVERGED * variance
        variance = again
        if settled:
            break
    else:
        raise ValueError(f"the sizes' fit did not converge in {ITERATIONS} iterations")
    return mode(variance), variance


def training_rows(
    forecasts: pl.DataFrame,
    mornings: pl.DataFrame,
    odds: pl.DataFrame,
    week: date,
    spec: Spec | None = None,
) -> pl.DataFrame:
    """The week's training rows: the version's predicted forecasts of earlier weeks with their
    move from the morning snapshot to Pinnacle's closing proxy (movement.table on the morning
    rows, a proxy after the snapshot only), every forecast, morning and close public before the
    week's fold start."""
    spec = spec or V1
    schema = row_schema(spec)
    cutoff = fold_start(week)
    earlier = forecasts.filter(
        pl.col("forecast_version") == spec.version,
        pl.col("status") == PREDICTED,
        pl.col("game_date") < week,
        pl.col("published_utc") < cutoff,
    )
    if earlier.is_empty():
        return pl.DataFrame(schema=schema)
    keys = ["game_date", "game_id"]
    morning = mornings.filter(
        pl.col("status") == lm.PREDICTED, pl.col("published_utc") < cutoff
    ).join(earlier.select(keys), on=keys)
    known = odds.filter(pl.col("snapshot_utc") < cutoff)
    moves = movement.table(
        morning.rename({"morning_snapshot_utc": "decision_snapshot_utc"}),
        ls.closes(known, cutoff),
    ).filter(pl.col("move").is_not_null())
    hist = ["y_hat_hist", "y_hat_density"] if spec.density else ["y_hat_hist"]
    return (
        earlier.select(*keys, *hist)
        .join(moves.select(*keys, y="move"), on=keys)
        .sort(keys)
        .select(list(schema))
        .cast(schema)  # type: ignore[arg-type]
    )


def sizes_of(rows: pl.DataFrame, spec: Spec) -> tuple[float, float | None, float | None]:
    """The version's sizes on its training rows: lam, lam_d (version 2's, else None) and the
    residual variance."""
    y = rows["y"].to_numpy()
    if not spec.density:
        lam, variance = fit_lam(rows["y_hat_hist"].to_numpy(), y)
        return lam, None, variance
    x = rows.select("y_hat_hist", "y_hat_density").to_numpy()
    (lam, lam_d), variance = fit_sizes(x, y)
    return float(lam), float(lam_d), variance


def fit_week(
    week: date,
    forecasts: pl.DataFrame,
    mornings: pl.DataFrame,
    odds: pl.DataFrame,
    spec: Spec | None = None,
) -> Fit:
    """The week's sizes from its training rows."""
    spec = spec or V1
    rows = training_rows(forecasts, mornings, odds, week, spec)
    lam, lam_d, variance = sizes_of(rows, spec)
    return Fit(week, fold_start(week), lam, variance, rows, lam_d, spec.version)


def week_keys(week: date, spec: Spec | None = None) -> tuple[str, str]:
    """Where a version's week's fit and its rows are kept, each written once."""
    base = f"{(spec or V1).weeks}/{week.isoformat()}"
    return f"{base}.json", f"{base}-rows.parquet"


def keep_week(store: lb.Store, fit: Fit, weights_version: str) -> None:
    """The week's rows, then its fit, the last, each written once under its version's weeks."""
    record_key, rows_key = week_keys(fit.week, SPECS[fit.version])
    lb.put_once(store, rows_key, lb.parquet(fit.rows))
    body = json.dumps(fit.record(weights_version), sort_keys=True).encode()
    lb.put_once(store, record_key, body)


def kept_week(store: lb.Store, week: date, spec: Spec | None = None) -> Fit | None:
    """The version's kept fit for the week, None if the week has none yet."""
    spec = spec or V1
    record_key, rows_key = week_keys(week, spec)
    if record_key not in store.keys(spec.weeks):
        return None
    record = json.loads(store.get(record_key))
    if record["forecast_version"] != spec.version:
        raise ValueError(f"{record_key} is {record['forecast_version']}'s, not {spec.version}'s")
    rows = pl.read_parquet(io.BytesIO(store.get(rows_key)))
    return Fit(
        week=date.fromisoformat(record["week"]),
        cutoff=datetime.fromisoformat(record["cutoff"]),
        lam=float(record["lam"]),
        variance=record["variance"],
        rows=rows,
        lam_d=record.get("lam_d"),
        version=spec.version,
    )


def week_fit(
    store: lb.Store,
    week: date,
    forecasts: Callable[[], pl.DataFrame],
    mornings: Callable[[], pl.DataFrame],
    odds: Callable[[], pl.DataFrame],
    weights_version: str,
    spec: Spec | None = None,
) -> Fit:
    """The version's kept fit for the week, or the week's first: fitted and kept once. The
    mornings and the odds are read only when an earlier week has a forecast of the version."""
    spec = spec or V1
    found = kept_week(store, week, spec)
    if found is not None:
        return found
    stored = forecasts()
    earlier = stored.filter(
        pl.col("forecast_version") == spec.version,
        pl.col("game_date") < week,
        pl.col("status") == PREDICTED,
    )
    if earlier.is_empty():
        fit = fit_week(week, stored, lm.empty(), pl.DataFrame(), spec)
    else:
        fit = fit_week(week, stored, mornings(), odds(), spec)
    keep_week(store, fit, weights_version)
    return fit


# The day


def forecast(
    morning: pl.DataFrame,
    found: pl.DataFrame,
    w: Weights,
    fit: Fit,
    published: datetime,
    code_version: str,
    spec: Spec | None = None,
) -> pl.DataFrame:
    """One row per slate game of the day's morning rows: the version's forecast, or why it has
    none. found holds B3's four parts by game (parts()), and for version 2 the density columns
    beside them (density_inputs())."""
    spec = spec or V1
    rows = morning.select(
        "game_date",
        "season",
        "game_id",
        "start_utc",
        "home",
        "away",
        "prediction_utc",
        "morning_snapshot_utc",
        "morning_version",
        morning_status="status",
        p_morning="p_b0",
    ).join(found, on="game_id", how="left")
    needed = [*b3.SPLIT, *DENSITY] if spec.density else list(b3.SPLIT)
    if spec.density:
        status = (
            pl.when(pl.col("morning_status") != lm.PREDICTED)
            .then(pl.lit(NO_MORNING))
            .when(pl.any_horizontal(pl.col(*b3.SPLIT).is_null()))
            .then(pl.lit(NO_PARTS))
            .when(pl.any_horizontal(pl.col(*DENSITY).is_null()))
            .then(pl.lit(NO_DENSITY))
            .when(pl.col("start_utc") <= pl.lit(published))
            .then(pl.lit(STARTS_FIRST))
            .otherwise(pl.lit(PREDICTED))
        )
    else:
        status = (
            pl.when(pl.col("morning_status") != lm.PREDICTED)
            .then(pl.lit(NO_MORNING))
            .when(pl.any_horizontal(pl.col(*b3.SPLIT).is_null()))
            .then(pl.lit(NO_PARTS))
            .when(pl.col("start_utc") <= pl.lit(published))
            .then(pl.lit(STARTS_FIRST))
            .otherwise(pl.lit(PREDICTED))
        )
    missing = [c for c in needed if c not in rows.columns]
    if missing:
        raise ValueError(f"{spec.version} needs {missing} beside the morning rows")
    if not spec.density:
        # Version 1 reads no density, whatever it is handed: its rows leave it null.
        rows = rows.drop([c for c in DENSITY if c in rows.columns])
    rows = rows.with_columns(status=status, o=_logit(pl.col("p_morning")))
    hist = pl.when(pl.col("status") == PREDICTED).then(history_move(rows, w))
    rows = rows.with_columns(y_hat_hist=hist)
    if spec.density:
        assert fit.lam_d is not None, f"{spec.version}'s week needs density's size"
        rows = rows.with_columns(
            y_hat_density=pl.when(pl.col("status") == PREDICTED).then(density_move(w))
        ).with_columns(
            y_hat=fit.lam * pl.col("y_hat_hist") + fit.lam_d * pl.col("y_hat_density"),
            lam_d=pl.lit(fit.lam_d),
        )
    else:
        rows = rows.with_columns(y_hat=fit.lam * pl.col("y_hat_hist"))
    rows = rows.with_columns(
        p_close_hat=1 / (1 + (-(pl.col("o") + pl.col("y_hat"))).exp()),
        p_morning=pl.when(pl.col("morning_status") == lm.PREDICTED).then(pl.col("p_morning")),
        published_utc=pl.lit(published),
        lam=pl.lit(fit.lam),
        lam_week=pl.lit(fit.week),
        lam_games=pl.lit(fit.rows.height),
        forecast_version=pl.lit(spec.version),
        weights_version=pl.lit(w.version),
        code_version=pl.lit(code_version),
    )
    return complete(rows.sort("game_id"))


def complete(frame: pl.DataFrame) -> pl.DataFrame:
    """frame as close_forecasts: its columns in order, version 2's left null where a row has
    none, such as version 1's and every day stored before version 2 (ADR 0056)."""
    columns = dtypes(CloseForecasts)
    absent = [pl.lit(None, dtype=t).alias(c) for c, t in columns.items() if c not in frame.columns]
    frame = frame.with_columns(absent).select(list(columns)).cast(columns)  # type: ignore[arg-type]
    return CloseForecasts.validate(frame)  # type: ignore[return-value]


def key(day: date, spec: Spec | None = None) -> str:
    return f"{(spec or V1).prefix}/{day.isoformat()}.parquet"


def write_once(store: lb.Store, day: date, rows: pl.DataFrame, spec: Spec | None = None) -> str:
    """Write the version's day once, refused if the day exists with other bytes, if a row is
    another version's, and unless every forecast precedes its game's start."""
    spec = spec or V1
    others = set(rows["forecast_version"].to_list()) - {spec.version}
    if others:
        raise ValueError(f"rows of {sorted(others)} can't be written as {spec.version}'s")
    lp.pre_game(rows)
    lb.put_once(store, key(day, spec), lb.parquet(rows))
    return key(day, spec)


def stored_days(store: lb.Store, spec: Spec | None = None) -> list[date]:
    """The days whose forecasts of the version are kept."""
    spec = spec or V1
    return sorted(
        date.fromisoformat(k.rsplit("/", 1)[1].removesuffix(".parquet"))
        for k in store.keys(spec.prefix)
        if k.endswith(".parquet") and not k.startswith(f"{spec.weeks}/")
    )


def stored(store: lb.Store, day: date, spec: Spec | None = None) -> pl.DataFrame:
    """The version's stored day as close_forecasts (complete)."""
    return complete(pl.read_parquet(io.BytesIO(store.get(key(day, spec)))))


def empty() -> pl.DataFrame:
    return pl.DataFrame(schema=dtypes(CloseForecasts))


def sync(store: lb.Store) -> pl.DataFrame:
    """Every version's stored days, as the lake's close_forecasts."""
    frames = [stored(store, day, spec) for spec in VERSIONS for day in stored_days(store, spec)]
    every = pl.concat([empty(), *frames])
    return CloseForecasts.validate(every.cast(dtypes(CloseForecasts)))  # type: ignore[arg-type]


def inputs(
    bundle: lb.Bundle, spec: Spec, games: Callable[[], pl.DataFrame] | None = None
) -> pl.DataFrame:
    """What the version's forecast reads beside the morning rows: B3's four parts from the
    morning bundle, and for version 2 the density columns from it and the season's games known
    at its cutoff."""
    found = parts(bundle)
    if not spec.density:
        return found
    if games is None:
        raise ValueError(f"{spec.version} reads the season's games for its density")
    return found.join(density_inputs(bundle, games()), on="game_id", how="left")


def replay(
    store: lb.Store,
    day: date,
    spec: Spec | None = None,
    games: Callable[[], pl.DataFrame] | None = None,
) -> pl.DataFrame:
    """The version's day made again from the morning's bundle and rows, for version 2 the
    season's games known then, and the week's kept fit, at the stored publication and code
    version."""
    spec = spec or V1
    kept = stored(store, day, spec)
    if kept.is_empty():
        raise ValueError(f"{day}: no forecast rows to replay")
    fit = kept_week(store, week_of(day), spec)
    if fit is None:
        raise ValueError(f"{day}: its week's fit is not kept")
    w = weights(spec)
    if set(kept["weights_version"].to_list()) != {w.version}:
        raise ValueError(f"{day}: forecast with other weights than {w.version}")
    morning = lm.stored(store, day)
    found = inputs(lb.read(store, day, lm.BUNDLES), spec, games)
    published = kept["published_utc"][0]
    return forecast(morning, found, w, fit, published, kept["code_version"][0], spec)


def check(
    store: lb.Store,
    day: date,
    spec: Spec | None = None,
    games: Callable[[], pl.DataFrame] | None = None,
) -> list[str]:
    """Where a version's stored day and its replay disagree (bundle.differences), where its
    week's kept sizes differ from a refit on its kept rows, or why the day can't be replayed."""
    spec = spec or V1
    try:
        again = replay(store, day, spec, games)
        fit = kept_week(store, week_of(day), spec)
        assert fit is not None
        lam, lam_d, _ = sizes_of(fit.rows, spec)
    except Exception as exc:
        return [f"{day}: {exc}"]
    found = stored(store, day, spec)
    problems = [f"{day}: {problem}" for problem in lb.differences(again, found)]
    if abs(lam - fit.lam) > lb.TOLERANCE:
        problems.append(f"{day}: the week's lam {fit.lam} is not its rows' {lam}")
    if lam_d is not None and (fit.lam_d is None or abs(lam_d - fit.lam_d) > lb.TOLERANCE):
        problems.append(f"{day}: the week's lam_d {fit.lam_d} is not its rows' {lam_d}")
    return problems


def counts(frame: pl.DataFrame) -> str:
    """The rows by status, for a log that shows counts only."""
    by = frame.group_by("status").len().sort("status").iter_rows()
    return ", ".join(f"{n} {status}" for status, n in by)
