"""Writes to the Supabase serving database over its REST API (PostgREST), with the service key.

Only what the dashboard and bet ledger need lives in Supabase; history lives in the lake.
"""

from collections.abc import Sequence
from typing import Any
from urllib.parse import urlparse

import httpx
import polars as pl

from nhl_edge.settings import require

BATCH_SIZE = 500


class SupabaseError(RuntimeError):
    """A rejected write. Messages carry the status and PostgREST's error, never the key."""


class Supabase:
    def __init__(self, url: str, key: str, client: httpx.Client | None = None) -> None:
        headers = {"apikey": key, "Content-Type": "application/json"}
        # Legacy service_role keys are JWTs and go in Authorization too. New sb_secret_ keys are
        # sent as apikey only.
        if key.startswith("eyJ"):
            headers["Authorization"] = f"Bearer {key}"
        self.url = url.rstrip("/")
        self.client = client or httpx.Client(timeout=60)
        self.client.headers.update(headers)

    @classmethod
    def from_env(cls) -> "Supabase":
        return cls(require("SUPABASE_URL"), require("SUPABASE_SERVICE_ROLE_KEY"))

    @property
    def project_ref(self) -> str:
        return (urlparse(self.url).hostname or "").split(".")[0]

    def insert_new(self, table: str, frame: pl.DataFrame, key: Sequence[str]) -> int:
        """Insert rows, skipping any whose key already exists, so reruns are idempotent. Returns
        the number of rows sent."""
        return self._post(table, frame, key, "ignore-duplicates", "insert into")

    def upsert(self, table: str, frame: pl.DataFrame, key: Sequence[str]) -> int:
        """Insert rows, replacing any whose key already exists, so a corrected fact wins. Returns
        the number of rows sent."""
        return self._post(table, frame, key, "merge-duplicates", "upsert into")

    def ping(self, table: str, column: str) -> None:
        """One tiny read. The nightly job calls it every day, so the free project never sits
        idle long enough to pause, even in the off-season."""
        self._request(f"read from {table}", "GET", table, params={"select": column, "limit": "1"})

    def _post(
        self, table: str, frame: pl.DataFrame, key: Sequence[str], resolution: str, action: str
    ) -> int:
        rows = json_rows(frame)
        for start in range(0, len(rows), BATCH_SIZE):
            self._request(
                f"{action} {table}",
                "POST",
                table,
                params={"on_conflict": ",".join(key)},
                headers={"Prefer": f"resolution={resolution},return=minimal"},
                json=rows[start : start + BATCH_SIZE],
            )
        return len(rows)

    def _request(self, what: str, method: str, table: str, **kwargs: Any) -> None:
        try:
            response = self.client.request(method, f"{self.url}/rest/v1/{table}", **kwargs)
        except httpx.HTTPError as exc:
            raise SupabaseError(f"{what} failed: {type(exc).__name__}") from None
        if response.status_code not in (200, 201, 204):
            raise SupabaseError(f"{what} returned {response.status_code}: {response.text[:300]}")


def json_rows(frame: pl.DataFrame) -> list[dict[str, Any]]:
    """Rows as JSON-ready dicts: timestamps as ISO UTC strings, dates as YYYY-MM-DD."""
    return frame.with_columns(
        pl.col(pl.Datetime).dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        pl.col(pl.Date).dt.strftime("%Y-%m-%d"),
    ).to_dicts()
