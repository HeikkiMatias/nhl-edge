import gzip
from pathlib import Path

import pytest
from fakes import MemoryBucket

from nhl_edge.lake.r2 import R2Config
from nhl_edge.lake.raw import RawStore
from nhl_edge.settings import MissingSettingError

BODY = b'[{"id": "evt1", "home_team": "Montr\xc3\xa9al Canadiens"}]'
PREFIX = "nhl/boxscore/20102011/2010020003"


def test_round_trip_is_byte_for_byte(tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    raw_key = store.put("odds", "2026-09-28/snap", BODY, {"fetched_utc": "2026-09-28T12:00:53"})
    assert raw_key == "odds/2026-09-28/snap"
    assert store.get(raw_key) == BODY
    assert store.meta(raw_key) == {"fetched_utc": "2026-09-28T12:00:53"}
    assert gzip.decompress((tmp_path / "odds/2026-09-28/snap.json.gz").read_bytes()) == BODY


def test_raw_files_are_never_overwritten(tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    store.put("odds", "snap", BODY, {})
    with pytest.raises(FileExistsError):
        store.put("odds", "snap", b"[]", {})
    assert store.get("odds/snap") == BODY


def test_mirror_uploads_the_same_bytes_under_raw(tmp_path: Path) -> None:
    bucket = MemoryBucket()
    store = RawStore(tmp_path, bucket="lake", objects=bucket)
    store.put("odds", "snap", BODY, {"slot": "morning"})
    assert set(bucket.objects) == {"raw/odds/snap.json.gz", "raw/odds/snap.meta.json"}
    assert bucket.objects["raw/odds/snap.json.gz"] == (tmp_path / "odds/snap.json.gz").read_bytes()


def test_latest_is_the_newest_complete_response(tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    assert store.latest(PREFIX) is None
    store.put("nhl", "boxscore/20102011/2010020003/20260928T120000Z", BODY, {})
    store.put("nhl", "boxscore/20102011/2010020003/20260928T130000Z", BODY, {})
    assert store.latest(PREFIX) == f"{PREFIX}/20260928T130000Z"
    # A body without its sidecar is a write that was interrupted, so it never counts.
    (tmp_path / f"{PREFIX}/20260928T140000Z.json.gz").write_bytes(b"partial")
    assert store.latest(PREFIX) == f"{PREFIX}/20260928T130000Z"


def test_a_local_miss_is_served_from_r2(tmp_path: Path) -> None:
    bucket = MemoryBucket()
    RawStore(tmp_path / "laptop", "b", bucket).put(
        "nhl", "boxscore/20102011/2010020003/20260928T120000Z", BODY, {"status": 200}
    )
    runner = RawStore(tmp_path / "runner", "b", bucket)
    raw_key = f"{PREFIX}/20260928T120000Z"
    assert runner.latest(PREFIX) == raw_key
    assert runner.get(raw_key) == BODY
    assert runner.meta(raw_key) == {"status": 200}
    # Now cached locally: the next lookup does not touch R2.
    gets = len(bucket.gets)
    assert runner.latest(PREFIX) == raw_key
    assert len(bucket.gets) == gets


def test_r2_responses_without_a_sidecar_or_below_the_prefix_are_ignored(tmp_path: Path) -> None:
    bucket = MemoryBucket()
    bucket.put_object(Key=f"raw/{PREFIX}/20260928T150000Z.json.gz", Body=b"partial")
    bucket.put_object(Key=f"raw/{PREFIX}/deeper/20260928T160000Z.json.gz", Body=b"x")
    bucket.put_object(Key=f"raw/{PREFIX}/deeper/20260928T160000Z.meta.json", Body=b"{}")
    store = RawStore(tmp_path, "b", bucket)
    assert store.latest(PREFIX) is None
    assert RawStore(tmp_path).latest(PREFIX) is None  # no mirror, no R2 lookup


def test_mirror_needs_bucket_and_client(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="both"):
        RawStore(tmp_path, bucket="lake")


def clear_r2(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"):
        monkeypatch.delenv(name, raising=False)


def test_r2_config_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    clear_r2(monkeypatch)
    assert R2Config.from_env() is None
    with pytest.raises(MissingSettingError, match="R2_ACCOUNT_ID"):
        RawStore.from_env(tmp_path, mirror=True)
    monkeypatch.setenv("R2_BUCKET", "lake")
    with pytest.raises(MissingSettingError, match="missing: R2_ACCOUNT_ID"):
        R2Config.from_env()
    assert RawStore.from_env(tmp_path, mirror=False).objects is None
