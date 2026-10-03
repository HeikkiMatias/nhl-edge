import json
from pathlib import Path
from typing import Any

import pytest
from fakes import MemoryBucket

from nhl_edge.backtest import one_time


class ConditionalBucket(MemoryBucket):
    """A bucket that, as R2 and S3 do, refuses a write with IfNoneMatch="*" to a key that
    exists."""

    def put_object(self, **kwargs: Any) -> None:
        if kwargs.get("IfNoneMatch") == "*" and kwargs["Key"] in self.objects:
            raise RuntimeError("PreconditionFailed")
        super().put_object(**kwargs)


def places(tmp_path: Path, bucket: MemoryBucket | None = None) -> one_time.Places:
    lake = tmp_path / "shared" / "lake"
    lake.mkdir(parents=True)
    reports = tmp_path / "repo" / "reports" / "backtest"
    return one_time.Places(
        lake, (reports, tmp_path / "elsewhere"), bucket or ConditionalBucket(), "b"
    )


def test_a_claim_is_recorded_everywhere_before_the_run_and_refuses_a_second(
    tmp_path: Path,
) -> None:
    where = places(tmp_path)
    assert one_time.records(where) == []
    one_time.claim(where, "backtest-hockey-x")
    assert where.objects.objects[one_time.R2_KEY].decode().startswith("backtest-hockey-x ")  # type: ignore[attr-defined]
    assert where.local == tmp_path / "shared" / one_time.LEDGER and where.local.exists()
    assert where.repo.read_text().startswith("backtest-hockey-x ")
    with pytest.raises(ValueError, match="already ran"):
        one_time.claim(where, "backtest-hockey-y")


def test_another_machines_claim_in_r2_refuses_the_run(tmp_path: Path) -> None:
    bucket = ConditionalBucket()
    bucket.put_object(Bucket="b", Key=one_time.R2_KEY, Body=b"backtest-hockey-x then\n")
    where = places(tmp_path, bucket)
    assert one_time.records(where) == [f"R2 {one_time.R2_KEY}: backtest-hockey-x then"]
    with pytest.raises(ValueError, match="already ran"):
        one_time.claim(where, "backtest-hockey-y")
    assert not where.local.exists() and not where.repo.exists()


def test_r2_refusing_the_claim_stops_the_run_before_any_local_record(tmp_path: Path) -> None:
    class Racing(ConditionalBucket):
        def put_object(self, **kwargs: Any) -> None:
            raise RuntimeError("PreconditionFailed")

    where = places(tmp_path, Racing())
    with pytest.raises(ValueError, match="could not claim"):
        one_time.claim(where, "backtest-hockey-x")
    assert not where.local.exists() and not where.repo.exists()


def test_a_report_that_scored_the_season_counts_as_a_run(tmp_path: Path) -> None:
    where = places(tmp_path)
    reports = where.report_dirs[0]
    reports.mkdir(parents=True)
    (reports / "runs.csv").write_text(
        "run_utc,version,seasons,experiment,model,method,games,log_loss,low,high,train_cutoff\n"
        "t,backtest-hockey-a,20232024 20242025,hockey,B3,none,2624,0.66,0.65,0.67,\n"
    )
    (reports / "hockey-a.json").write_text(json.dumps({"seasons": [20232024, 20242025]}))
    assert one_time.records(where) == []
    other = where.report_dirs[1]
    other.mkdir(parents=True)
    (other / "hockey-b.json").write_text(json.dumps({"seasons": [20252026]}))
    with (reports / "runs.csv").open("a") as handle:
        handle.write("t,backtest-hockey-c,20252026,hockey,B3,none,1312,0.66,0.65,0.67,\n")
    assert one_time.records(where) == sorted(
        [str(other / "hockey-b.json"), f"{reports / 'runs.csv'}: backtest-hockey-c"]
    )


def test_a_claim_needs_the_runs_version(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="version"):
        one_time.claim(places(tmp_path), "")
