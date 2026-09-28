"""Raw source responses, stored untouched so parsers can be fixed and replayed without the network.

Each response body is written byte-for-byte, gzipped, as data/raw/<source>/<key>.json.gz with a
<key>.meta.json sidecar (fetch time, request parameters without secrets, status, headers). When R2
is configured, both files are mirrored to raw/<source>/ in the lake bucket, and a lookup that
misses locally falls back to R2. A stored response is never overwritten.
"""

import gzip
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from nhl_edge.lake.r2 import ObjectStore, R2Config, list_keys

RAW_DIR = Path("data/raw")
SUFFIX = ".json.gz"


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
        for suffix in (SUFFIX, ".meta.json"):  # body first, as in put
            body = self.objects.get_object(Bucket=self.bucket, Key=f"raw/{raw_key}{suffix}")
            path = self.base_dir / f"{raw_key}{suffix}"
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f"{path.name}.tmp")
            tmp.write_bytes(body["Body"].read())
            tmp.replace(path)
        return raw_key
