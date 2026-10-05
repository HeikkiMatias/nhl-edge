"""Runs that may happen once: gate 2's one-time hockey-only test on 2025-26 (docs/plan.md section 5,
#107), and phase 4's one run on 2022-23's 342 priced games after the freeze (#145, ADR 0025). Each
is a Test with its own claim. Before anything is scored, the run is claimed where every later run
looks:
- in R2 (R2_KEY, in the lake bucket), the store every machine shares, with a write that creates
  the object only if it is absent;
- beside the lake's real directory, for every checkout sharing that lake;
- in the backtest reports (reports/backtest/one_time_test.txt), committed and pushed with the
  run's report.
A run is refused when any of them, or any report it can see, records an earlier one. No claim is
ever released, even if the run fails: a rerun is the owner's call.
"""

import csv
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from nhl_edge.backtest.seasons import MARKET_VALIDATION_SEASONS, ONE_TIME_SEASONS
from nhl_edge.lake.r2 import ObjectStore, R2Config


@dataclass(frozen=True)
class Test:
    """A run that may happen once: its name, where it is claimed (an R2 key, and a ledger file
    beside the lake and in the reports), and the seasons whose scoring counts as the run."""

    name: str
    r2_key: str
    ledger: str
    seasons: tuple[int, ...]


GATE_2 = Test(
    "the one-time test", "ledger/one_time_test.txt", "one_time_test.txt", ONE_TIME_SEASONS
)
MARKET_VALIDATION = Test(
    "2022-23's market validation run",
    "ledger/market_validation.txt",
    "market_validation.txt",
    MARKET_VALIDATION_SEASONS,
)
# Gate 2's test, as before the market validation run was added.
R2_KEY = GATE_2.r2_key
LEDGER = GATE_2.ledger


@dataclass(frozen=True)
class Places:
    """Where a once-only run is claimed and looked for: the lake's directory, the report
    directories (the first holds the repository's record), and the R2 bucket."""

    lake_dir: Path
    report_dirs: tuple[Path, ...]
    objects: ObjectStore
    bucket: str
    test: Test = GATE_2

    @property
    def local(self) -> Path:
        return self.lake_dir.resolve().parent / self.test.ledger

    @property
    def repo(self) -> Path:
        return self.report_dirs[0] / self.test.ledger


def places(lake_dir: Path, report_dirs: tuple[Path, ...], test: Test = GATE_2) -> Places:
    """The places, with the lake's R2 bucket: a once-only run needs R2."""
    config = R2Config.require("--one-time-test" if test is GATE_2 else "--market-validation")
    return Places(lake_dir, report_dirs, config.client(), config.bucket, test)


def _scores(seasons: list[int], test: Test) -> bool:
    return bool(set(seasons) & set(test.seasons))


def records(where: Places) -> list[str]:
    """Every record of an earlier run of the test: a claim, or a report that scored its season."""
    found = []
    key = where.test.r2_key
    listed = where.objects.list_objects_v2(Bucket=where.bucket, Prefix=key).get("Contents", [])
    if any(item["Key"] == key for item in listed):
        body = where.objects.get_object(Bucket=where.bucket, Key=key)["Body"].read()
        found.append(f"R2 {key}: {body.decode().strip()}")
    for ledger in (where.local, where.repo):
        if ledger.exists():
            found.append(f"{ledger}: {ledger.read_text().strip()}")
    for directory in where.report_dirs:
        runs = directory / "runs.csv"
        if runs.exists():
            with runs.open(newline="") as handle:
                for row in csv.DictReader(handle):
                    seasons = [int(s) for s in (row.get("seasons") or "").split()]
                    if _scores(seasons, where.test):
                        found.append(f"{runs}: {row['version']}")
        for report in sorted([*directory.glob("hockey-*.json"), *directory.glob("market-*.json")]):
            if _scores(json.loads(report.read_text()).get("seasons", []), where.test):
                found.append(str(report))
    return sorted(set(found))


def claim(where: Places, run_version: str) -> None:
    """Claim the test in R2, beside the lake and in the reports, before it scores, or refuse if
    any record of an earlier run exists or R2 will not take the claim."""
    name = where.test.name
    if not run_version:
        raise ValueError(f"{name}'s claim needs the run's version")
    earlier = records(where)
    if earlier:
        raise ValueError(f"{name} already ran: {'; '.join(earlier)} (docs/plan.md section 5)")
    stamp = f"{run_version} {datetime.now(UTC).isoformat()}\n"
    try:
        where.objects.put_object(
            Bucket=where.bucket, Key=where.test.r2_key, Body=stamp.encode(), IfNoneMatch="*"
        )
    except Exception as exc:  # a precondition failure means another claim got there first
        raise ValueError(f"could not claim {name} in R2 ({exc}): not run") from None
    for ledger in (where.local, where.repo):
        ledger.parent.mkdir(parents=True, exist_ok=True)
        try:
            with ledger.open("x") as handle:
                handle.write(stamp)
        except FileExistsError:
            raise ValueError(f"{name} already ran: {ledger}") from None
