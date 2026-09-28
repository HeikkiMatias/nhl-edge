"""Writes to the Supabase serving database over its REST API (PostgREST), with the service key.

Only what the dashboard and bet ledger need lives in Supabase; history lives in the lake.
"""

from collections.abc import Sequence
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
        rows = frame.with_columns(pl.col(pl.Datetime).dt.strftime("%Y-%m-%dT%H:%M:%SZ")).to_dicts()
        for start in range(0, len(rows), BATCH_SIZE):
            try:
                response = self.client.post(
                    f"{self.url}/rest/v1/{table}",
                    params={"on_conflict": ",".join(key)},
                    headers={"Prefer": "resolution=ignore-duplicates,return=minimal"},
                    json=rows[start : start + BATCH_SIZE],
                )
            except httpx.HTTPError as exc:
                raise SupabaseError(f"insert into {table} failed: {type(exc).__name__}") from None
            if response.status_code not in (200, 201, 204):
                raise SupabaseError(
                    f"insert into {table} returned {response.status_code}: {response.text[:300]}"
                )
        return len(rows)
