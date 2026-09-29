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


def machine(root: Path, bucket: MemoryBucket) -> RawStore:
    return RawStore(root, bucket="lake", objects=bucket)


def test_sync_uploads_only_complete_responses_r2_lacks(tmp_path: Path) -> None:
    bucket = MemoryBucket()
    mirrored = machine(tmp_path, bucket)
    mirrored.put("odds", "2026-09-28/a", BODY, {"slot": "morning"})  # already in R2
    local_only = RawStore(tmp_path)  # a run without --r2
    local_only.put("odds", "2026-09-28/b", BODY, {"slot": "midday"})
    local_only.put("nhl", "schedule/2026-09-29/c", b"{}", {})
    (tmp_path / "odds/2026-09-28/d.json.gz").write_bytes(gzip.compress(b"[]"))  # interrupted

    counts = mirrored.sync_to_r2("odds/")
    assert (counts.local, counts.remote, counts.copied, counts.skipped) == (2, 1, 1, 1)
    assert "raw/odds/2026-09-28/b.json.gz" in bucket.objects
    assert "raw/odds/2026-09-28/d.json.gz" not in bucket.objects
    assert not any(key.startswith("raw/nhl/") for key in bucket.objects)  # outside the prefix
    assert mirrored.sync_to_r2("odds/").copied == 0
    assert mirrored.sync_to_r2().copied == 1  # the NHL schedule, with no prefix


def test_restore_downloads_what_is_missing_and_overwrites_nothing(tmp_path: Path) -> None:
    bucket = MemoryBucket()
    runner = machine(tmp_path / "runner", bucket)  # the nightly runner, mirroring everything
    runner.put("odds", "2026-09-28/a", BODY, {"slot": "morning"})
    runner.put("odds", "2026-09-28/b", b"[]", {"slot": "midday"})
    runner.put("nhl", "schedule/2026-09-29/c", b"{}", {})
    bucket.objects["raw/odds/2026-09-28/e.json.gz"] = gzip.compress(b"[]")  # no sidecar in R2

    laptop = machine(tmp_path / "laptop", bucket)
    laptop.put("odds", "2026-09-28/b", b"[1]", {"slot": "mine"})  # a local copy stays as it is
    (tmp_path / "laptop/odds/2026-09-28/a.json.gz").write_bytes(b"partial")  # interrupted

    counts = laptop.restore_from_r2("odds/")
    assert (counts.local, counts.remote, counts.copied, counts.skipped) == (1, 2, 0, 2)
    assert laptop.get("odds/2026-09-28/b") == b"[1]"
    assert (tmp_path / "laptop/odds/2026-09-28/a.json.gz").read_bytes() == b"partial"
    assert not (tmp_path / "laptop/odds/2026-09-28/e.json.gz").exists()

    restored = laptop.restore_from_r2()
    assert restored.copied == 1  # the NHL schedule
    assert laptop.get("nhl/schedule/2026-09-29/c") == b"{}"
    assert laptop.restore_from_r2().copied == 0


def test_a_fresh_machine_restores_identical_responses(tmp_path: Path) -> None:
    bucket = MemoryBucket()
    laptop = machine(tmp_path / "laptop", bucket)
    RawStore(tmp_path / "laptop").put("odds", "2026-09-28/a", BODY, {"slot": "morning"})
    laptop.put("nhl", "boxscore/20102011/2010020003/20260928T131311Z", b'{"id": 1}', {"n": 1})
    laptop.sync_to_r2()

    fresh = machine(tmp_path / "fresh", bucket)
    assert fresh.restore_from_r2(workers=2).copied == 2
    for raw_key in ("odds/2026-09-28/a", "nhl/boxscore/20102011/2010020003/20260928T131311Z"):
        assert fresh.get(raw_key) == laptop.get(raw_key)
        assert fresh.meta(raw_key) == laptop.meta(raw_key)
    assert fresh.latest("nhl/boxscore/20102011/2010020003") == laptop.latest(
        "nhl/boxscore/20102011/2010020003"
    )


def test_sync_and_restore_need_r2(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="needs R2"):
        RawStore(tmp_path).sync_to_r2()
    with pytest.raises(ValueError, match="needs R2"):
        RawStore(tmp_path).restore_from_r2()


class FlakyBucket(MemoryBucket):
    """Fails the first download of each sidecar, like a transient R2 or network error."""

    def __init__(self) -> None:
        super().__init__()
        self.failed: set[str] = set()

    def get_object(self, **kwargs: object) -> dict[str, object]:
        key = str(kwargs["Key"])
        if key.endswith(".meta.json") and key not in self.failed:
            self.failed.add(key)
            raise ConnectionError(f"reset while reading {key}")
        return super().get_object(**kwargs)


def test_an_interrupted_restore_leaves_nothing_and_resumes(tmp_path: Path) -> None:
    bucket = FlakyBucket()
    machine(tmp_path / "runner", bucket).put("odds", "2026-09-28/a", BODY, {"slot": "morning"})
    laptop = machine(tmp_path / "laptop", bucket)
    with pytest.raises(ConnectionError):
        laptop.restore_from_r2()
    assert not [p for p in (tmp_path / "laptop").rglob("*") if p.is_file()]
    counts = laptop.restore_from_r2()
    assert (counts.copied, counts.skipped) == (1, 0)
    assert laptop.get("odds/2026-09-28/a") == BODY


def test_a_body_cut_off_from_its_sidecar_is_completed_if_it_matches(tmp_path: Path) -> None:
    bucket = MemoryBucket()
    runner = machine(tmp_path / "runner", bucket)
    runner.put("odds", "2026-09-28/a", BODY, {"slot": "morning"})
    runner.put("odds", "2026-09-28/b", b"[2]", {"slot": "midday"})
    laptop = machine(tmp_path / "laptop", bucket)
    # a: the process died between publishing the body and the sidecar; b: a body of its own.
    for key, body in (("a", gzip.compress(BODY, mtime=0)), ("b", gzip.compress(b"[1]", mtime=0))):
        path = tmp_path / f"laptop/odds/2026-09-28/{key}.json.gz"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)

    counts = laptop.restore_from_r2()
    assert (counts.copied, counts.skipped) == (1, 1)
    assert laptop.meta("odds/2026-09-28/a") == {"slot": "morning"}
    assert laptop.get("odds/2026-09-28/a") == BODY
    assert not (tmp_path / "laptop/odds/2026-09-28/b.meta.json").exists()
    assert gzip.decompress((tmp_path / "laptop/odds/2026-09-28/b.json.gz").read_bytes()) == b"[1]"
    assert laptop.restore_from_r2().copied == 0
