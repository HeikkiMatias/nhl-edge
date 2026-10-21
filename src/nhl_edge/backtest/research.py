"""Research run bundles (#186): every backtest run keeps its per-game predictions, written once,
as a decision day keeps its run bundle (#171, live/bundle.py).

A report alone can't be recomputed: ADR 0031 had to pool its historical results by
approximation, and two configurations of an experiment write the same mutable feature
partitions, so the second can overwrite what the first read. So each `nhl backtest` run writes
`bundles/research/<run id>/`, in R2 with --r2 and under data/bundles/ otherwise:

- **predictions.parquet:** every model's per-game rows (walk_forward.PREDICTION_SCHEMA), for
  every game whether bet or not, with role marking a tested season's rows (TEST) apart from those
  of the folds that only teach the market blend (BLEND_TRAINING).
- **blend_inputs.parquet** (the development run): blend.rows, each game's market, B2, B3 and u's
  parts with their cutoffs. **flags.parquet:** gate 2's subsets.
- **coverage.json, fits.json:** the coverage per experiment and season; B1's recalibrations, and
  B2's and B3's fits.
- **report.json:** the run's report as written. **inputs.json:** the sha256 of every lake table
  the run read (Recorded), so a comparison of two runs names what they read differently.
- **manifest.json,** written last: the run's identity and each file's sha256 and rows. It is
  written only once the files, read back, reproduce the report's estimates (reproduce): a bundle
  without it never reads as complete.

compare() pairs two kept runs' models game by game, with weekly block bootstrap intervals (hard
rule 7). No result is read as a pass or fail.
"""

import io
import json
import re
from collections.abc import Collection, Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

import polars as pl

from nhl_edge.backtest import b2_report, b3_report, blend, reports, walk_forward
from nhl_edge.game.b2 import B2Model
from nhl_edge.game.b3 import B3Model
from nhl_edge.lake.tables import Reader
from nhl_edge.live import bundle as lb
from nhl_edge.market.recalibration import Recalibration

PREFIX = "bundles/research"
TEST, BLEND_TRAINING = "test", "blend_training"
DEVELOPMENT, HOCKEY = "development", "hockey"
PREDICTIONS = "predictions.parquet"
BLEND_INPUTS = "blend_inputs.parquet"
FLAGS = "flags.parquet"
COVERAGE, FITS, REPORT, INPUTS = "coverage.json", "fits.json", "report.json", "inputs.json"
# Report keys that are no estimate from the kept rows: fits shown beside the estimates, and the
# train cutoffs the add-ons raise from the fits.
NOT_REPRODUCED = frozenset({"fits", "train_cutoff"})
# <version>-<HHMMSS>-<microseconds>; the first bundles of 2026-10-21 have no microseconds.
RUN_ID = re.compile(r"^[a-z0-9-]+-\d{6}(-\d{6})?$")


@dataclass
class Recorded:
    """The lake as a run reads it, with the sha256 of each read's rows (reads), as challengers'
    PointInTime records them: a table read whole by its name, a season's read as
    <table>:<seasons>."""

    lake: Reader
    reads: dict[str, str] = field(default_factory=dict)

    def read(self, table: str, seasons: Collection[int] | None = None) -> pl.DataFrame:
        rows = self.lake.read(table, seasons)
        name = table if seasons is None else f"{table}:{','.join(map(str, sorted(seasons)))}"
        self.reads[name] = lb.sha256(lb.parquet(rows))
        return rows


def run_id(version: str, run_utc: datetime) -> str:
    """The run's bundle name: its version and UTC start time to the microsecond, so two runs of
    one commit, even started in the same second, never share one (Codex on #303)."""
    return f"{version}-{run_utc:%H%M%S-%f}"


def where(run: str) -> str:
    if not RUN_ID.match(run):
        raise ValueError(f"{run} is no research run id")
    return f"{PREFIX}/{run}"


@dataclass(frozen=True)
class Bundle:
    manifest: Mapping[str, Any]
    predictions: pl.DataFrame
    coverage: dict[str, dict[int, dict[str, int]]]
    fits: dict[str, Any]
    report: dict[str, Any]
    inputs: dict[str, str]
    blend_inputs: pl.DataFrame | None = None
    flags: pl.DataFrame | None = None


def _json(value: Any) -> bytes:
    return json.dumps(value, indent=1, sort_keys=True, default=str).encode()


def _recalibration(fit: Recalibration) -> dict[str, Any]:
    record = asdict(fit)
    record["train_cutoff"] = fit.train_cutoff.isoformat()
    return record


def files(
    predictions: pl.DataFrame,
    tested: Collection[int],
    coverage: walk_forward.Coverage,
    report: Mapping[str, Any],
    inputs: Mapping[str, str],
    b1_fits: walk_forward.Fits | None = None,
    b2_fits: Mapping[str, Mapping[int, B2Model]] | None = None,
    b3_fits: Mapping[str, Mapping[int, B3Model]] | None = None,
    blend_inputs: pl.DataFrame | None = None,
    flags: pl.DataFrame | None = None,
) -> dict[str, bytes]:
    """A run's files by name: its predictions with each row's role, and whatever else it kept."""
    keep = pl.Series(sorted(tested), dtype=pl.Int32).implode()
    role = (
        pl.when(pl.col("season").is_in(keep)).then(pl.lit(TEST)).otherwise(pl.lit(BLEND_TRAINING))
    )
    fitted: dict[str, Any] = {}
    for name, fits in (("b1", b1_fits), ("b2", b2_fits), ("b3", b3_fits)):
        if fits is None:
            continue
        record = _recalibration if name == "b1" else lb.fit_record
        fitted[name] = {
            experiment: {str(season): record(fit) for season, fit in sorted(by.items())}  # type: ignore[arg-type]
            for experiment, by in sorted(fits.items())
        }
    out = {
        PREDICTIONS: lb.parquet(predictions.with_columns(role=role)),
        COVERAGE: _json(
            {e: {str(s): c for s, c in sorted(by.items())} for e, by in sorted(coverage.items())}
        ),
        FITS: _json(fitted),
        REPORT: _json(report),
        INPUTS: _json(dict(sorted(inputs.items()))),
    }
    if blend_inputs is not None:
        out[BLEND_INPUTS] = lb.parquet(blend_inputs)
    if flags is not None:
        out[FLAGS] = lb.parquet(flags)
    return out


def write_files(store: lb.Store, run: str, bodies: Mapping[str, bytes]) -> None:
    """Write the run's files once each (lb.put_once), before its manifest."""
    for name, body in bodies.items():
        lb.put_once(store, f"{where(run)}/{name}", body)


def seal(
    store: lb.Store, run: str, identity: Mapping[str, Any], bodies: Mapping[str, bytes]
) -> str:
    """Write the manifest, the bundle's completion mark, once, and return the bundle's prefix."""
    manifest = lb.manifest_of({"run": run, **identity}, bodies, {})
    lb.put_once(store, f"{where(run)}/{lb.MANIFEST}", _json(manifest))
    return where(run)


def _bundle(manifest: Mapping[str, Any], bodies: Mapping[str, bytes]) -> Bundle:
    def frame(name: str) -> pl.DataFrame | None:
        return pl.read_parquet(io.BytesIO(bodies[name])) if name in bodies else None

    predictions = frame(PREDICTIONS)
    assert predictions is not None
    coverage = {
        e: {int(s): c for s, c in by.items()} for e, by in json.loads(bodies[COVERAGE]).items()
    }
    return Bundle(
        manifest,
        predictions,
        coverage,
        json.loads(bodies[FITS]),
        json.loads(bodies[REPORT]),
        json.loads(bodies[INPUTS]),
        frame(BLEND_INPUTS),
        frame(FLAGS),
    )


def read(store: lb.Store, run: str) -> Bundle:
    """A kept run, refused without its manifest or with any file not matching it."""
    return _bundle(*lb.verified(store, where(run)))


def unsealed(store: lb.Store, run: str) -> Bundle:
    """A run's files as written, before its manifest: what reproduce checks before sealing."""
    prefix = where(run)
    names = [key.removeprefix(f"{prefix}/") for key in store.keys(prefix)]
    return _bundle({}, {name: store.get(f"{prefix}/{name}") for name in names})


def manifest(store: lb.Store, run: str) -> dict[str, Any]:
    """A kept run's manifest alone, as a listing reads it: read() verifies every file."""
    return json.loads(store.get(f"{where(run)}/{lb.MANIFEST}"))


def runs(store: lb.Store) -> list[str]:
    """Every kept run, complete with its manifest, in name order."""
    return sorted(
        key.removeprefix(f"{PREFIX}/").split("/")[0]
        for key in store.keys(PREFIX)
        if key.endswith(f"/{lb.MANIFEST}")
    )


def _b1_fits(bundle: Bundle) -> walk_forward.Fits:
    return {
        experiment: {
            int(season): Recalibration(
                intercept=fit["intercept"],
                slope=fit["slope"],
                games=fit["games"],
                train_cutoff=datetime.fromisoformat(fit["train_cutoff"]),
            )
            for season, fit in by.items()
        }
        for experiment, by in bundle.fits.get("b1", {}).items()
    }


def _rows(bundle: Bundle) -> pl.DataFrame:
    return bundle.predictions.filter(pl.col("role") == TEST).drop("role")


def development(bundle: Bundle) -> dict[str, Any]:
    """The development run's estimates from its kept rows: every experiment's summary (each
    model's log loss, against the multiplicative method and against B1, and E2 against E1), B3
    against B2 and on gate 2's subsets, and the B3 blend against the B2 blend and the market
    control, by the report's own code."""
    report = bundle.report
    seasons = [int(s) for s in report["seasons"]]
    rows = _rows(bundle)
    fits = _b1_fits(bundle)
    tested, coverage, fits = walk_forward.tests_only(rows, bundle.coverage, fits, seasons)
    again = reports.summary(
        tested,
        coverage,
        fits,
        seasons,
        report["version"],
        datetime.fromisoformat(report["run_utc"]),
    )
    for experiment, body in again["experiments"].items():
        models = body["models"]
        by_experiment = tested.filter(pl.col("experiment") == experiment)
        if b3_report.MODEL in models:
            models[b3_report.MODEL][f"paired_against_{b3_report.REFERENCE}"] = b3_report._estimates(
                b3_report.against(by_experiment), "difference"
            )
            if bundle.flags is not None:
                models[b3_report.MODEL]["gate_2_subsets"] = b3_report.subsets(
                    by_experiment, bundle.flags
                )
        if blend.BLEND in models:
            for reference in (blend.BLEND_B2, blend.BLEND_MARKET):
                models[blend.BLEND][f"paired_against_{reference}"] = blend._estimates(
                    blend.paired(by_experiment, blend.BLEND, reference), "difference"
                )
        # The calibrations the add-ons report, from the same rows (Codex on #303).
        for model in (b2_report.MODEL, b3_report.MODEL, blend.BLEND):
            if model in models:
                models[model]["calibration"] = b2_report.calibrated(
                    by_experiment.filter(pl.col("model") == model)
                )
    return {key: again[key] for key in ("experiments", "e2_against_e1")}


def hockey(bundle: Bundle) -> dict[str, Any]:
    """The hockey-only run's estimates from its kept rows: each model's log loss and calibration,
    B3 against B2 and on gate 2's subsets, and each B3 variant against B3."""
    report = bundle.report
    if bundle.flags is None:
        raise ValueError("a hockey-only bundle keeps gate 2's flags")
    names = {name: {} for name in bundle.fits.get("b3", {})}
    again = b3_report.hockey(
        _rows(bundle),
        bundle.coverage,
        names,  # type: ignore[arg-type]
        bundle.flags,
        [int(s) for s in report["seasons"]],
        report["version"],
        datetime.fromisoformat(report["run_utc"]),
    )
    return {"models": again["models"]}


def _differences(again: Any, kept: Any, path: str) -> list[str]:
    """Where again, recomputed, isn't in kept, the report: every value again holds must be
    kept's, after a JSON round trip; kept may hold more (the add-ons' other sections)."""
    if isinstance(again, dict):
        if not isinstance(kept, dict):
            return [f"{path}: not in the report"]
        found = []
        for key, value in again.items():
            if key in NOT_REPRODUCED:
                continue
            if key not in kept:
                found.append(f"{path}/{key}: not in the report")
                continue
            found += _differences(value, kept[key], f"{path}/{key}")
        return found
    return [] if again == kept else [f"{path}: {kept} in the report, {again} from the rows"]


def reproduce(bundle: Bundle) -> list[str]:
    """Where the report's estimates don't follow from the kept rows alone: none for a bundle
    that reproduces its report."""
    estimates = hockey(bundle) if bundle.report.get("mode", "").startswith(HOCKEY) else None
    if estimates is None:
        estimates = development(bundle)
    again = json.loads(json.dumps(estimates, default=str))
    return _differences(again, bundle.report, "")


def compare(
    base: Bundle,
    other: Bundle,
    base_model: str,
    other_model: str,
    experiment: str,
    method: str = walk_forward.NO_METHOD,
) -> dict[str, Any]:
    """other_model's log loss in other minus base_model's in base, per game of the experiment
    that both runs scored for the method, pooled and per season with weekly block bootstrap
    intervals; and the input tables whose rows differ between the two runs, or that only one
    read. A digest is the sha256 of a table's rows as parquet, which another polars version may
    encode differently: runs kept with different versions get no input comparison (None), and
    a note saying why (Codex on #303). No pass or fail is read from it."""

    def scored(bundle: Bundle, model: str) -> pl.DataFrame:
        rows = _rows(bundle).filter(
            pl.col("experiment") == experiment,
            pl.col("model") == model,
            pl.col("method") == method,
        )
        if rows.is_empty():
            raise ValueError(f"{bundle.manifest.get('run')} has no {experiment} {model} {method}")
        return rows

    a, b = scored(base, base_model), scored(other, other_model)
    keys = ["season", "game_id", "game_date"]
    paired = a.select(*keys, base=pl.col("log_loss")).join(
        b.select(*keys, other=pl.col("log_loss")), on=keys
    )
    paired = paired.with_columns(difference=pl.col("other") - pl.col("base"))
    tables = sorted(set(base.inputs) | set(other.inputs))
    versions = [bundle.manifest.get("polars") for bundle in (base, other)]
    comparable = versions[0] == versions[1]
    return {
        "base": {"run": base.manifest.get("run"), "model": base_model},
        "other": {"run": other.manifest.get("run"), "model": other_model},
        "experiment": experiment,
        "method": method,
        "games": {"base": a.height, "other": b.height, "paired": paired.height},
        "difference": reports._estimates(paired, "difference") if paired.height else None,
        "inputs_differ": [t for t in tables if base.inputs.get(t) != other.inputs.get(t)]
        if comparable
        else None,
        "inputs_note": None
        if comparable
        else f"the runs' input digests come from polars {versions[0]} and {versions[1]}, which "
        "may encode the same rows differently",
    }


def identity(
    mode: str,
    report: Mapping[str, Any],
    options: Mapping[str, Any],
    uv_lock: bytes | None,
) -> dict[str, Any]:
    """The run's identity for its manifest."""
    return {
        "mode": mode,
        "version": report["version"],
        "run_utc": report["run_utc"],
        "seasons": report["seasons"],
        "options": dict(options),
        "polars": pl.__version__,
        "uv_lock_sha256": lb.sha256(uv_lock) if uv_lock is not None else None,
    }


def keep(
    store: lb.Store,
    run: str,
    bodies: Mapping[str, bytes],
    identity: Mapping[str, Any],
) -> list[str]:
    """Write the run's files, read them back and seal the bundle only if they reproduce its
    report. Returns what doesn't reproduce: with any, no manifest is written."""
    write_files(store, run, bodies)
    problems = reproduce(unsealed(store, run))
    if not problems:
        seal(store, run, identity, bodies)
    return problems
