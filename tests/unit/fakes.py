"""Test doubles shared by the unit tests."""

import io
from typing import Any


class MemoryBucket:
    """In-memory stand-in for the boto3 S3 client, paging listings like R2 does."""

    def __init__(self, page_size: int = 1000) -> None:
        self.objects: dict[str, bytes] = {}
        self.page_size = page_size
        self.gets: list[str] = []

    def put_object(self, **kwargs: Any) -> None:
        self.objects[kwargs["Key"]] = kwargs["Body"]

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        self.gets.append(kwargs["Key"])
        return {"Body": io.BytesIO(self.objects[kwargs["Key"]])}

    def delete_object(self, **kwargs: Any) -> None:
        self.objects.pop(kwargs["Key"], None)

    def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]:
        keys = sorted(k for k in self.objects if k.startswith(kwargs["Prefix"]))
        start = int(kwargs.get("ContinuationToken", 0))
        page = keys[start : start + self.page_size]
        more = start + self.page_size < len(keys)
        return {
            "Contents": [{"Key": k, "Size": len(self.objects[k])} for k in page],
            "IsTruncated": more,
            **({"NextContinuationToken": str(start + self.page_size)} if more else {}),
        }
