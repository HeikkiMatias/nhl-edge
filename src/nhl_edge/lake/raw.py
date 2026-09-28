"""Raw source responses, stored untouched so parsers can be fixed and replayed without the network.

Each response body is written byte-for-byte, gzipped, as data/raw/<source>/<key>.json.gz with a
<key>.meta.json sidecar (fetch time, request parameters without secrets, status, headers). When R2
is configured, both files are mirrored to raw/<source>/ in the lake bucket. Raw files are never
overwritten.
"""

import gzip
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

from nhl_edge.settings import MissingSettingError, optional

RAW_DIR = Path("data/raw")
R2_ENV = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET")


class ObjectStore(Protocol):
    def put_object(self, **kwargs: Any) -> Any: ...


@dataclass(frozen=True)
class R2Config:
    account_id: str
    access_key_id: str
    secret_access_key: str
    bucket: str

    @classmethod
    def from_env(cls) -> "R2Config | None":
        """All four R2 variables, or None when none is set. A partial set is an error."""
        values = {name: optional(name) for name in R2_ENV}
        if not any(values.values()):
            return None
        missing = [name for name, value in values.items() if value is None]
        if missing:
            raise MissingSettingError(f"R2 is partly configured, missing: {', '.join(missing)}")
        account_id, access_key_id, secret_access_key, bucket = (
            values[name] or "" for name in R2_ENV
        )
        return cls(account_id, access_key_id, secret_access_key, bucket)

    def client(self) -> ObjectStore:
        import boto3

        client = boto3.client(
            "s3",
            endpoint_url=f"https://{self.account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=self.access_key_id,
            aws_secret_access_key=self.secret_access_key,
            region_name="auto",
        )
        return cast(ObjectStore, client)


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
    def from_env(cls, base_dir: Path = RAW_DIR, *, mirror: bool) -> "RawStore":
        """A local store, mirrored to R2 when mirror is set. Mirroring without R2 is an error."""
        if not mirror:
            return cls(base_dir)
        config = R2Config.from_env()
        if config is None:
            raise MissingSettingError(f"--mirror-raw needs {', '.join(R2_ENV)}")
        return cls(base_dir, config.bucket, config.client())

    def put(self, source: str, key: str, body: bytes, meta: Mapping[str, Any]) -> str:
        """Store one response and return its raw key, <source>/<key>."""
        raw_key = f"{source}/{key}"
        data = gzip.compress(body, mtime=0)
        sidecar = json.dumps(meta, indent=2, sort_keys=True, default=str).encode() + b"\n"
        data_path = self.base_dir / f"{raw_key}.json.gz"
        data_path.parent.mkdir(parents=True, exist_ok=True)
        with data_path.open("xb") as f:
            f.write(data)
        with (self.base_dir / f"{raw_key}.meta.json").open("xb") as f:
            f.write(sidecar)
        if self.objects is not None:
            self.objects.put_object(
                Bucket=self.bucket,
                Key=f"raw/{raw_key}.json.gz",
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
        return gzip.decompress((self.base_dir / f"{raw_key}.json.gz").read_bytes())

    def meta(self, raw_key: str) -> dict[str, Any]:
        return json.loads((self.base_dir / f"{raw_key}.meta.json").read_text())
