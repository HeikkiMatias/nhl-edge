"""Cloudflare R2, the lake bucket: raw responses under raw/, parquet tables under lake/.

R2 has no hard spending cap, so bucket_usage lets the nightly job warn well before the free 10 GB.
"""

from dataclasses import dataclass
from typing import Any, Protocol, cast

from nhl_edge.settings import MissingSettingError, optional

R2_ENV = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET")


class ObjectStore(Protocol):
    """The part of the boto3 S3 client the lake uses."""

    def put_object(self, **kwargs: Any) -> Any: ...

    def get_object(self, **kwargs: Any) -> Any: ...

    def list_objects_v2(self, **kwargs: Any) -> Any: ...

    def delete_object(self, **kwargs: Any) -> Any: ...


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

    @classmethod
    def require(cls, flag: str) -> "R2Config":
        config = cls.from_env()
        if config is None:
            raise MissingSettingError(f"{flag} needs {', '.join(R2_ENV)}")
        return config

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


def list_keys(objects: ObjectStore, bucket: str, prefix: str) -> list[tuple[str, int]]:
    """Every (key, size) under a prefix, following list_objects_v2 pagination."""
    keys: list[tuple[str, int]] = []
    token: str | None = None
    while True:
        kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
        if token is not None:
            kwargs["ContinuationToken"] = token
        page = objects.list_objects_v2(**kwargs)
        keys.extend((item["Key"], int(item["Size"])) for item in page.get("Contents", []))
        if not page.get("IsTruncated"):
            return keys
        token = page["NextContinuationToken"]


def bucket_usage(objects: ObjectStore, bucket: str, prefix: str = "") -> tuple[int, int]:
    """Object count and total bytes under a prefix."""
    keys = list_keys(objects, bucket, prefix)
    return len(keys), sum(size for _, size in keys)
