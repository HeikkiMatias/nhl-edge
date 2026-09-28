import gzip
from pathlib import Path
from typing import Any

import pytest

from nhl_edge.lake.r2 import R2Config
from nhl_edge.lake.raw import RawStore
from nhl_edge.settings import MissingSettingError

BODY = b'[{"id": "evt1", "home_team": "Montr\xc3\xa9al Canadiens"}]'


class FakeObjects:
    def __init__(self) -> None:
        self.puts: list[dict[str, Any]] = []

    def put_object(self, **kwargs: Any) -> None:
        self.puts.append(kwargs)

    def get_object(self, **kwargs: Any) -> Any:
        raise NotImplementedError

    def list_objects_v2(self, **kwargs: Any) -> Any:
        raise NotImplementedError


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
    objects = FakeObjects()
    store = RawStore(tmp_path, bucket="lake", objects=objects)
    store.put("odds", "snap", BODY, {"slot": "morning"})
    by_key = {put["Key"]: put for put in objects.puts}
    assert set(by_key) == {"raw/odds/snap.json.gz", "raw/odds/snap.meta.json"}
    assert all(put["Bucket"] == "lake" for put in objects.puts)
    assert by_key["raw/odds/snap.json.gz"]["Body"] == (tmp_path / "odds/snap.json.gz").read_bytes()


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
