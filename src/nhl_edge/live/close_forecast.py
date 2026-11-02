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
verdict before the formal review; #358's simulator quotes at it (live/kalshi_quotes.py).
"""

import io
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import numpy as np
import polars as pl

from nhl_edge.game import b3
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
STARTS_FIRST = "starts before the forecast is published"


@dataclass(frozen=True)
class Weights:
    """The frozen weights (ADR 0054): the move a + b_o*o + parts . x, in home log-odds."""

    version: str
    train_cutoff: datetime
    a: float
    b_o: float
    parts: dict[str, float]


def weights() -> Weights:
    """The committed weights, refused unless their bytes are the pinned ones."""
    body = WEIGHTS.read_bytes()
    if lb.sha256(body) != WEIGHTS_SHA256:
        raise ValueError(f"{WEIGHTS} is not the weights close_forecast was registered with")
    record = json.loads(body)
    found = record["weights"]
    return Weights(
        version=record["version"],
        train_cutoff=datetime.fromisoformat(record["train_cutoff"]),
        a=float(found["a"]),
        b_o=float(found["b_o"]),
        parts={part: float(found[part]) for part in b3.SPLIT},
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


def history_move(frame: pl.DataFrame, w: Weights) -> pl.Expr:
    """The frozen weights' move for frame's rows (o, the four parts)."""
    move = pl.lit(w.a) + w.b_o * pl.col("o")
    for part, weight in w.parts.items():
        move = move + weight * pl.col(part)
    return move


# The week's size


@dataclass(frozen=True)
class Fit:
    """A week's size: lam at the posterior mode, its residual variance, and the rows it read
    (game_date, game_id, y_hat_hist, y), with the cutoff they were public by."""

    week: date
    cutoff: datetime
    lam: float
    variance: float | None
    rows: pl.DataFrame

    def record(self, weights_version: str) -> dict[str, Any]:
        return {
            "forecast_version": VERSION,
            "weights_version": weights_version,
            "week": self.week.isoformat(),
            "cutoff": self.cutoff.isoformat(),
            "lam": self.lam,
            "variance": self.variance,
            "games": self.rows.height,
        }


ROW_SCHEMA = {
    "game_date": pl.Date,
    "game_id": pl.Int64,
    "y_hat_hist": pl.Float64,
    "y": pl.Float64,
}


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


def training_rows(
    forecasts: pl.DataFrame, mornings: pl.DataFrame, odds: pl.DataFrame, week: date
) -> pl.DataFrame:
    """The week's training rows: the predicted forecasts of earlier weeks with their move from
    the morning snapshot to Pinnacle's closing proxy (movement.table on the morning rows, a proxy
    after the snapshot only), every forecast, morning and close public before the week's fold
    start."""
    cutoff = fold_start(week)
    earlier = forecasts.filter(
        pl.col("forecast_version") == VERSION,
        pl.col("status") == PREDICTED,
        pl.col("game_date") < week,
        pl.col("published_utc") < cutoff,
    )
    if earlier.is_empty():
        return pl.DataFrame(schema=ROW_SCHEMA)
    keys = ["game_date", "game_id"]
    morning = mornings.filter(
        pl.col("status") == lm.PREDICTED, pl.col("published_utc") < cutoff
    ).join(earlier.select(keys), on=keys)
    known = odds.filter(pl.col("snapshot_utc") < cutoff)
    moves = movement.table(
        morning.rename({"morning_snapshot_utc": "decision_snapshot_utc"}),
        ls.closes(known, cutoff),
    ).filter(pl.col("move").is_not_null())
    return (
        earlier.select(*keys, "y_hat_hist")
        .join(moves.select(*keys, y="move"), on=keys)
        .sort(keys)
        .select(list(ROW_SCHEMA))
        .cast(ROW_SCHEMA)  # type: ignore[arg-type]
    )


def fit_week(
    week: date, forecasts: pl.DataFrame, mornings: pl.DataFrame, odds: pl.DataFrame
) -> Fit:
    """The week's size from its training rows."""
    rows = training_rows(forecasts, mornings, odds, week)
    lam, variance = fit_lam(rows["y_hat_hist"].to_numpy(), rows["y"].to_numpy())
    return Fit(week, fold_start(week), lam, variance, rows)


def week_keys(week: date) -> tuple[str, str]:
    """Where a week's fit and its rows are kept, each written once."""
    base = f"{WEEKS}/{week.isoformat()}"
    return f"{base}.json", f"{base}-rows.parquet"


def keep_week(store: lb.Store, fit: Fit, weights_version: str) -> None:
    """The week's rows, then its fit, the last, each written once."""
    record_key, rows_key = week_keys(fit.week)
    lb.put_once(store, rows_key, lb.parquet(fit.rows))
    body = json.dumps(fit.record(weights_version), sort_keys=True).encode()
    lb.put_once(store, record_key, body)


def kept_week(store: lb.Store, week: date) -> Fit | None:
    """The week's kept fit, None if the week has none yet."""
    record_key, rows_key = week_keys(week)
    if record_key not in store.keys(WEEKS):
        return None
    record = json.loads(store.get(record_key))
    rows = pl.read_parquet(io.BytesIO(store.get(rows_key)))
    return Fit(
        week=date.fromisoformat(record["week"]),
        cutoff=datetime.fromisoformat(record["cutoff"]),
        lam=float(record["lam"]),
        variance=record["variance"],
        rows=rows,
    )


def week_fit(
    store: lb.Store,
    week: date,
    forecasts: Callable[[], pl.DataFrame],
    mornings: Callable[[], pl.DataFrame],
    odds: Callable[[], pl.DataFrame],
    weights_version: str,
) -> Fit:
    """The week's kept fit, or the week's first: fitted and kept once."""
    found = kept_week(store, week)
    if found is not None:
        return found
    fit = fit_week(week, forecasts(), mornings(), odds())
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
) -> pl.DataFrame:
    """One row per slate game of the day's morning rows: its forecast, or why it has none. found
    holds B3's four parts by game (parts())."""
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
    status = (
        pl.when(pl.col("morning_status") != lm.PREDICTED)
        .then(pl.lit(NO_MORNING))
        .when(pl.any_horizontal(pl.col(*b3.SPLIT).is_null()))
        .then(pl.lit(NO_PARTS))
        .when(pl.col("start_utc") <= pl.lit(published))
        .then(pl.lit(STARTS_FIRST))
        .otherwise(pl.lit(PREDICTED))
    )
    rows = rows.with_columns(status=status, o=_logit(pl.col("p_morning")))
    hist = pl.when(pl.col("status") == PREDICTED).then(history_move(rows, w))
    rows = rows.with_columns(y_hat_hist=hist).with_columns(y_hat=fit.lam * pl.col("y_hat_hist"))
    rows = rows.with_columns(
        p_close_hat=1 / (1 + (-(pl.col("o") + pl.col("y_hat"))).exp()),
        p_morning=pl.when(pl.col("morning_status") == lm.PREDICTED).then(pl.col("p_morning")),
        published_utc=pl.lit(published),
        lam=pl.lit(fit.lam),
        lam_week=pl.lit(fit.week),
        lam_games=pl.lit(fit.rows.height),
        forecast_version=pl.lit(VERSION),
        weights_version=pl.lit(w.version),
        code_version=pl.lit(code_version),
    )
    columns = dtypes(CloseForecasts)
    frame = rows.select(list(columns)).cast(columns)  # type: ignore[arg-type]
    return CloseForecasts.validate(frame.sort("game_id"))  # type: ignore[return-value]


def key(day: date) -> str:
    return f"{PREFIX}/{day.isoformat()}.parquet"


def write_once(store: lb.Store, day: date, rows: pl.DataFrame) -> str:
    """Write the day's rows once, refused if the day exists with other bytes, and only if every
    forecast precedes its game's start."""
    lp.pre_game(rows)
    lb.put_once(store, key(day), lb.parquet(rows))
    return key(day)


def stored_days(store: lb.Store) -> list[date]:
    """The days whose forecasts are kept."""
    return sorted(
        date.fromisoformat(k.rsplit("/", 1)[1].removesuffix(".parquet"))
        for k in store.keys(PREFIX)
        if k.endswith(".parquet") and not k.startswith(f"{WEEKS}/")
    )


def stored(store: lb.Store, day: date) -> pl.DataFrame:
    return pl.read_parquet(io.BytesIO(store.get(key(day))))


def empty() -> pl.DataFrame:
    return pl.DataFrame(schema=dtypes(CloseForecasts))


def sync(store: lb.Store) -> pl.DataFrame:
    """Every stored day's rows, as the lake's close_forecasts."""
    frames = [stored(store, day) for day in stored_days(store)]
    every = pl.concat([empty(), *frames])
    return CloseForecasts.validate(every.cast(dtypes(CloseForecasts)))  # type: ignore[arg-type]


def replay(store: lb.Store, day: date) -> pl.DataFrame:
    """The day's rows made again from the morning's bundle and rows and the week's kept fit, at
    the stored publication and code version."""
    kept = stored(store, day)
    if kept.is_empty():
        raise ValueError(f"{day}: no forecast rows to replay")
    fit = kept_week(store, week_of(day))
    if fit is None:
        raise ValueError(f"{day}: its week's fit is not kept")
    w = weights()
    if set(kept["weights_version"].to_list()) != {w.version}:
        raise ValueError(f"{day}: forecast with other weights than {w.version}")
    morning = lm.stored(store, day)
    found = parts(lb.read(store, day, lm.BUNDLES))
    published = kept["published_utc"][0]
    return forecast(morning, found, w, fit, published, kept["code_version"][0])


def check(store: lb.Store, day: date) -> list[str]:
    """Where a stored day's rows and those replayed disagree (bundle.differences), where its
    week's kept lam differs from a refit on its kept rows, or why the day can't be replayed."""
    try:
        again = replay(store, day)
        fit = kept_week(store, week_of(day))
        assert fit is not None
        lam, _ = fit_lam(fit.rows["y_hat_hist"].to_numpy(), fit.rows["y"].to_numpy())
    except Exception as exc:
        return [f"{day}: {exc}"]
    problems = [f"{day}: {problem}" for problem in lb.differences(again, stored(store, day))]
    if abs(lam - fit.lam) > lb.TOLERANCE:
        problems.append(f"{day}: the week's lam {fit.lam} is not its rows' {lam}")
    return problems


def counts(frame: pl.DataFrame) -> str:
    """The rows by status, for a log that shows counts only."""
    by = frame.group_by("status").len().sort("status").iter_rows()
    return ", ".join(f"{n} {status}" for status, n in by)
