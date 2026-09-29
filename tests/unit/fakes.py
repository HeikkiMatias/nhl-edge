"""Test doubles shared by the unit tests."""

import hashlib
import io
from typing import Any


class MemoryBucket:
    """In-memory stand-in for the boto3 S3 client, paging listings like R2 does. As in R2, an
    object's ETag is the quoted MD5 of its bytes."""

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
            "Contents": [
                {
                    "Key": k,
                    "Size": len(self.objects[k]),
                    "ETag": f'"{hashlib.md5(self.objects[k]).hexdigest()}"',
                }
                for k in page
            ],
            "IsTruncated": more,
            **({"NextContinuationToken": str(start + self.page_size)} if more else {}),
        }
