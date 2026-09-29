"""Raw source responses, stored untouched so parsers can be fixed and replayed without the network.

Each response body is written byte-for-byte, gzipped, as data/raw/<source>/<key>.json.gz with a
<key>.meta.json sidecar (fetch time, request parameters without secrets, status, headers). When R2
is configured, both files are mirrored to raw/<source>/ in the lake bucket, and a lookup that
misses locally falls back to R2. A stored response is never overwritten.

A response is complete once its sidecar exists: the body is always written first, locally and in
R2, so a body alone is an interrupted write. sync_to_r2 and restore_from_r2 copy complete responses
the other side lacks, which keeps the local cache a second copy of R2 (docs/plan.md section 10)
and lets a fresh machine replay.
"""

import gzip
import json
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nhl_edge.lake.r2 import ObjectStore, R2Config, list_keys

RAW_DIR = Path("data/raw")
SUFFIX = ".json.gz"
META = ".meta.json"
R2_RAW = "raw/"
WORKERS = 16


@dataclass(frozen=True)
class CopyCounts:
    """Responses under a prefix on each side, and what one sync or restore did."""

    local: int
    remote: int
    copied: int
    skipped: int


def _responses(paths: Iterable[str]) -> tuple[set[str], set[str]]:
    """Complete and incomplete raw keys among file paths relative to the raw root."""
    listed = list(paths)
    bodies = {path.removesuffix(SUFFIX) for path in listed if path.endswith(SUFFIX)}
    sidecars = {path.removesuffix(META) for path in listed if path.endswith(META)}
    return bodies & sidecars, bodies ^ sidecars


def _each(work: Callable[[str], None], raw_keys: Iterable[str], workers: int) -> None:
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(work, sorted(raw_keys)))


class RawStore:
    def __init__(
        self,
        base_dir: Path = RAW_DIR,
        bucket: str | None = None,
        objects: ObjectStore | None = None,
    ) -> None:
        if (bucket is None) != (objects is None):
            raise ValueError("an R2 mirror needs both a bucket and an object store client")
        self.base_dir = base_dir
        self.bucket = bucket
        self.objects = objects

    @classmethod
    def from_env(
        cls, base_dir: Path = RAW_DIR, *, mirror: bool, flag: str = "--mirror-raw"
    ) -> "RawStore":
        """A local store, mirrored to R2 when mirror is set. Mirroring without R2 is an error."""
        if not mirror:
            return cls(base_dir)
        config = R2Config.require(flag)
        return cls(base_dir, config.bucket, config.client())

    def put(self, source: str, key: str, body: bytes, meta: Mapping[str, Any]) -> str:
        """Store one response and return its raw key, <source>/<key>."""
        raw_key = f"{source}/{key}"
        data = gzip.compress(body, mtime=0)
        sidecar = json.dumps(meta, indent=2, sort_keys=True, default=str).encode() + b"\n"
        data_path = self.base_dir / f"{raw_key}{SUFFIX}"
        data_path.parent.mkdir(parents=True, exist_ok=True)
        with data_path.open("xb") as f:
            f.write(data)
        with (self.base_dir / f"{raw_key}.meta.json").open("xb") as f:
            f.write(sidecar)
        if self.objects is not None:
            self.objects.put_object(
                Bucket=self.bucket,
                Key=f"raw/{raw_key}{SUFFIX}",
                Body=data,
                ContentType="application/gzip",
            )
            self.objects.put_object(
                Bucket=self.bucket,
                Key=f"raw/{raw_key}.meta.json",
                Body=sidecar,
                ContentType="application/json",
            )
        return raw_key

    def get(self, raw_key: str) -> bytes:
        return gzip.decompress((self.base_dir / f"{raw_key}{SUFFIX}").read_bytes())

    def meta(self, raw_key: str) -> dict[str, Any]:
        return json.loads((self.base_dir / f"{raw_key}.meta.json").read_text())

    def latest(self, prefix: str) -> str | None:
        """The raw key of the newest complete response stored directly under <prefix>/, or None.

        Keys end in a UTC stamp (YYYYMMDDTHHMMSSZ), so the newest sorts last. A response counts
        only once its sidecar exists: put writes the body first, so a body without a sidecar is
        an interrupted write. When the local cache has nothing and R2 is configured, the newest
        complete response under raw/<prefix>/ is downloaded, so a fresh machine such as the
        nightly runner reuses what earlier runs stored instead of fetching it again.
        """
        directory = self.base_dir / prefix
        if directory.is_dir():
            names = [
                path.name.removesuffix(SUFFIX)
                for path in directory.glob(f"*{SUFFIX}")
                if path.with_name(path.name.removesuffix(SUFFIX) + ".meta.json").exists()
            ]
            if names:
                return f"{prefix}/{max(names)}"
        return self._pull_latest(prefix)

    def _pull_latest(self, prefix: str) -> str | None:
        if self.objects is None:
            return None
        remote = f"raw/{prefix}/"
        keys = {
            key.removeprefix(remote)
            for key, _ in list_keys(self.objects, self.bucket or "", remote)
        }
        complete = [
            name.removesuffix(SUFFIX)
            for name in keys
            if "/" not in name
            and name.endswith(SUFFIX)
            and name.removesuffix(SUFFIX) + ".meta.json" in keys
        ]
        if not complete:
            return None
        raw_key = f"{prefix}/{max(complete)}"
        self._download(raw_key)
        return raw_key

    def _download(self, raw_key: str) -> None:
        """Copy one complete response from R2. Both files are staged under temporary names
        first and published, body first as in put, only once both have arrived, so a failed
        download leaves nothing behind and the next restore retries it."""
        objects = self._objects()
        staged: list[tuple[Path, Path]] = []
        try:
            for suffix in (SUFFIX, META):
                body = objects.get_object(Bucket=self.bucket, Key=f"{R2_RAW}{raw_key}{suffix}")
                path = self.base_dir / f"{raw_key}{suffix}"
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_name(f"{path.name}.tmp")
                tmp.write_bytes(body["Body"].read())
                staged.append((tmp, path))
        except BaseException:
            for tmp, _ in staged:
                tmp.unlink(missing_ok=True)
            raise
        for tmp, path in staged:
            tmp.replace(path)

    def _objects(self) -> ObjectStore:
        if self.objects is None:
            raise ValueError("syncing the raw cache needs R2: pass a bucket and an object store")
        return self.objects

    def _local_responses(self, prefix: str) -> tuple[set[str], set[str]]:
        paths = (
            path.relative_to(self.base_dir).as_posix()
            for path in self.base_dir.rglob("*")
            if path.is_file()
        )
        return _responses(path for path in paths if path.startswith(prefix))

    def _remote_responses(self, prefix: str) -> tuple[set[str], set[str]]:
        keys = list_keys(self._objects(), self.bucket or "", f"{R2_RAW}{prefix}")
        return _responses(key.removeprefix(R2_RAW) for key, _ in keys)

    def sync_to_r2(self, prefix: str = "", workers: int = WORKERS) -> CopyCounts:
        """Upload every complete local response under prefix that R2 lacks, body first. A local
        body without its sidecar is an interrupted write and is left alone (skipped)."""
        objects = self._objects()
        local, partial = self._local_responses(prefix)
        remote, _ = self._remote_responses(prefix)
        missing = local - remote

        def upload(raw_key: str) -> None:
            for suffix, content_type in ((SUFFIX, "application/gzip"), (META, "application/json")):
                objects.put_object(
                    Bucket=self.bucket,
                    Key=f"{R2_RAW}{raw_key}{suffix}",
                    Body=(self.base_dir / f"{raw_key}{suffix}").read_bytes(),
                    ContentType=content_type,
                )

        _each(upload, missing, workers)
        return CopyCounts(len(local), len(remote), len(missing), len(partial))

    def restore_from_r2(self, prefix: str = "", workers: int = WORKERS) -> CopyCounts:
        """Download every complete R2 response under prefix that is missing locally. Nothing
        stored locally is overwritten: a response with any local file is skipped, and so is an
        R2 body without its sidecar."""
        local, local_partial = self._local_responses(prefix)
        remote, remote_partial = self._remote_responses(prefix)
        missing = remote - local - local_partial
        _each(self._download, missing, workers)
        skipped = len(remote_partial) + len(remote & local_partial)
        return CopyCounts(len(local), len(remote), len(missing), skipped)
