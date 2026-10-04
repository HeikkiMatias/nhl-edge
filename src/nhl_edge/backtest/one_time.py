"""Gate 2's one-time hockey-only test on 2025-26 (docs/plan.md section 5, #107): run once, on the
owner's go-ahead. Before anything is scored, the run is claimed where every later run looks:
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

from nhl_edge.backtest.seasons import ONE_TIME_SEASONS
from nhl_edge.lake.r2 import ObjectStore, R2Config

R2_KEY = "ledger/one_time_test.txt"
LEDGER = "one_time_test.txt"


@dataclass(frozen=True)
class Places:
    """Where the one-time test is claimed and looked for: the lake's directory, the report
    directories (the first holds the repository's record), and the R2 bucket."""

    lake_dir: Path
    report_dirs: tuple[Path, ...]
    objects: ObjectStore
    bucket: str

    @property
    def local(self) -> Path:
        return self.lake_dir.resolve().parent / LEDGER

    @property
    def repo(self) -> Path:
        return self.report_dirs[0] / LEDGER


def places(lake_dir: Path, report_dirs: tuple[Path, ...]) -> Places:
    """The places, with the lake's R2 bucket: the one-time test needs R2."""
    config = R2Config.require("--one-time-test")
    return Places(lake_dir, report_dirs, config.client(), config.bucket)


def _scores_one_time(seasons: list[int]) -> bool:
    return bool(set(seasons) & set(ONE_TIME_SEASONS))


def records(where: Places) -> list[str]:
    """Every record of an earlier one-time run: a claim, or a report that scored the season."""
    found = []
    listed = where.objects.list_objects_v2(Bucket=where.bucket, Prefix=R2_KEY).get("Contents", [])
    if any(item["Key"] == R2_KEY for item in listed):
        body = where.objects.get_object(Bucket=where.bucket, Key=R2_KEY)["Body"].read()
        found.append(f"R2 {R2_KEY}: {body.decode().strip()}")
    for ledger in (where.local, where.repo):
        if ledger.exists():
            found.append(f"{ledger}: {ledger.read_text().strip()}")
    for directory in where.report_dirs:
        runs = directory / "runs.csv"
        if runs.exists():
            with runs.open(newline="") as handle:
                for row in csv.DictReader(handle):
                    seasons = [int(s) for s in (row.get("seasons") or "").split()]
                    if _scores_one_time(seasons):
                        found.append(f"{runs}: {row['version']}")
        for report in sorted(directory.glob("hockey-*.json")):
            if _scores_one_time(json.loads(report.read_text()).get("seasons", [])):
                found.append(str(report))
    return sorted(set(found))


def claim(where: Places, run_version: str) -> None:
    """Claim the one-time test in R2, beside the lake and in the reports, before it scores, or
    refuse if any record of an earlier run exists or R2 will not take the claim."""
    if not run_version:
        raise ValueError("the one-time test's claim needs the run's version")
    earlier = records(where)
    if earlier:
        raise ValueError(
            f"the one-time test already ran: {'; '.join(earlier)} (docs/plan.md section 5)"
        )
    stamp = f"{run_version} {datetime.now(UTC).isoformat()}\n"
    try:
        where.objects.put_object(
            Bucket=where.bucket, Key=R2_KEY, Body=stamp.encode(), IfNoneMatch="*"
        )
    except Exception as exc:  # a precondition failure means another claim got there first
        raise ValueError(f"could not claim the one-time test in R2 ({exc}): not run") from None
    for ledger in (where.local, where.repo):
        ledger.parent.mkdir(parents=True, exist_ok=True)
        try:
            with ledger.open("x") as handle:
                handle.write(stamp)
        except FileExistsError:
            raise ValueError(f"the one-time test already ran: {ledger}") from None
