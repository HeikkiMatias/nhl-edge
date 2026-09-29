"""What the lake holds, for `nhl status`: rows and the newest game date per table, read from parquet
metadata and partition names so the SessionStart hook stays fast and offline. With R2, the same for
the mirror, and the newest raw responses on each side, to show when this machine is behind.
"""

from collections.abc import Iterable
from dataclasses import dataclass

import polars as pl

from nhl_edge.lake.r2 import list_keys
from nhl_edge.lake.raw import SUFFIX, RawStore
from nhl_edge.lake.tables import R2_PREFIX, TABLES, Lake

GAME_DATE = "game_date="
SIDECAR = ".meta.json"
R2_RAW = "raw/"
# The raw responses that grow every day, from the nightly ingest and the odds snapshots.
DAILY_RAW = ("nhl/schedule/", "odds/")


@dataclass(frozen=True)
class TableState:
    table: str
    files: int
    rows: int | None  # None where only file names were read (R2)
    latest: str | None  # the newest game_date partition, for tables partitioned by date


def latest_date(keys: Iterable[str]) -> str | None:
    dates = [part.removeprefix(GAME_DATE) for key in keys for part in key.split("/")]
    return max((d for d in dates if len(d) == 10 and d[4] == "-"), default=None)


def local_tables(lake: Lake) -> list[TableState]:
    states = []
    for table in TABLES:
        paths = sorted((lake.base_dir / table).rglob("*.parquet"))
        rows = pl.scan_parquet(paths).select(pl.len()).collect().item() if paths else 0
        keys = [path.relative_to(lake.base_dir).as_posix() for path in paths]
        states.append(TableState(table, len(paths), rows, latest_date(keys)))
    return states


def remote_tables(lake: Lake) -> list[TableState]:
    if lake.objects is None:
        raise ValueError("comparing with R2 needs a mirrored lake")
    states = []
    for table in TABLES:
        keys = [
            key for key, _ in list_keys(lake.objects, lake.bucket or "", f"{R2_PREFIX}/{table}/")
        ]
        states.append(TableState(table, len(keys), None, latest_date(keys)))
    return states


def newest_raw(paths: Iterable[str]) -> str | None:
    """The newest complete response among raw paths: keys end in a UTC fetch stamp or carry a date,
    so the lexically largest complete key is the newest."""
    listed = list(paths)
    bodies = {path.removesuffix(SUFFIX) for path in listed if path.endswith(SUFFIX)}
    complete = bodies & {path.removesuffix(SIDECAR) for path in listed if path.endswith(SIDECAR)}
    return max(complete, default=None)


def raw_lag(
    store: RawStore, prefixes: Iterable[str] = DAILY_RAW
) -> list[tuple[str, str | None, str | None]]:
    """(prefix, newest here, newest in R2) for the raw responses that grow every day."""
    if store.objects is None:
        raise ValueError("comparing with R2 needs a mirrored raw store")
    rows = []
    for prefix in prefixes:
        local = (store.base_dir / prefix).rglob("*") if (store.base_dir / prefix).exists() else []
        here = newest_raw(
            path.relative_to(store.base_dir).as_posix() for path in local if path.is_file()
        )
        remote = list_keys(store.objects, store.bucket or "", f"{R2_RAW}{prefix}")
        there = newest_raw(key.removeprefix(R2_RAW) for key, _ in remote)
        rows.append((prefix, here, there))
    return rows


def compact(n: int) -> str:
    """1234 as 1,234, and larger counts as 1.6M or 766k."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 100_000:
        return f"{n / 1000:.0f}k"
    return f"{n:,}"


def brief_line(states: list[TableState]) -> str:
    """One line for the SessionStart hook."""
    present = [state for state in states if state.files]
    if not present:
        return "lake empty: run nhl ingest"
    latest = max((s.latest for s in present if s.latest), default=None)
    counts = ", ".join(f"{s.table} {compact(s.rows or 0)}" for s in present)
    return f"lake to {latest}: {counts}" if latest else f"lake: {counts}"
