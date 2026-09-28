"""Raw source responses, stored untouched so parsers can be fixed and replayed without the network.

Each response body is written byte-for-byte, gzipped, as data/raw/<source>/<key>.json.gz with a
<key>.meta.json sidecar (fetch time, request parameters without secrets, status, headers). When R2
is configured, both files are mirrored to raw/<source>/ in the lake bucket. Raw files are never
overwritten.
"""

import gzip
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from nhl_edge.lake.r2 import ObjectStore, R2Config

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
        """The raw key of the newest response stored directly under <prefix>/, or None. Keys end in
        a UTC stamp (YYYYMMDDTHHMMSSZ), so the newest sorts last."""
        directory = self.base_dir / prefix
        if not directory.is_dir():
            return None
        names = sorted(path.name.removesuffix(SUFFIX) for path in directory.glob(f"*{SUFFIX}"))
        return f"{prefix}/{names[-1]}" if names else None
