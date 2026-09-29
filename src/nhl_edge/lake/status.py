"""What the lake holds, for `nhl status`: rows and the newest game date per table, read from parquet
metadata and partition names so the SessionStart hook stays fast and offline. With R2, which files
differ from the mirror, and the newest raw responses on each side, to show when this machine is
behind or ahead.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

import polars as pl

from nhl_edge.lake.r2 import list_keys
from nhl_edge.lake.raw import META, R2_RAW, SUFFIX, RawStore
from nhl_edge.lake.tables import R2_PREFIX, TABLES, Lake

GAME_DATE = "game_date="
# The raw responses that grow every day, from the nightly ingest and the odds snapshots.
DAILY_RAW = ("nhl/schedule/", "odds/")


def partition_dates(keys: Iterable[str]) -> list[str]:
    return [
        part.removeprefix(GAME_DATE)
        for key in keys
        for part in key.split("/")
        if part.startswith(GAME_DATE)
    ]


@dataclass(frozen=True)
class TableState:
    table: str
    files: Mapping[str, int]  # file key under the lake root: size in bytes
    rows: int | None = None  # None where only the listing was read (R2)
    latest: str | None = field(init=False)  # the newest game_date partition, if dated

    def __post_init__(self) -> None:
        object.__setattr__(self, "latest", max(partition_dates(self.files), default=None))


@dataclass(frozen=True)
class TableDiff:
    table: str
    only_here: list[str]
    only_there: list[str]
    differ: list[str]

    def __bool__(self) -> bool:
        return bool(self.only_here or self.only_there or self.differ)


def local_tables(lake: Lake) -> list[TableState]:
    states = []
    for table in TABLES:
        paths = sorted((lake.base_dir / table).rglob("*.parquet"))
        rows = pl.scan_parquet(paths).select(pl.len()).collect().item() if paths else 0
        files = {path.relative_to(lake.base_dir).as_posix(): path.stat().st_size for path in paths}
        states.append(TableState(table, files, rows))
    return states


def remote_tables(lake: Lake) -> list[TableState]:
    if lake.objects is None:
        raise ValueError("comparing with R2 needs a mirrored lake")
    states = []
    for table in TABLES:
        listed = list_keys(lake.objects, lake.bucket or "", f"{R2_PREFIX}/{table}/")
        files = {key.removeprefix(f"{R2_PREFIX}/"): size for key, size in listed}
        states.append(TableState(table, files))
    return states


def compare(here: TableState, there: TableState) -> TableDiff:
    """Files only on this machine, only in R2, and on both with different sizes."""
    shared = here.files.keys() & there.files.keys()
    return TableDiff(
        here.table,
        sorted(here.files.keys() - there.files.keys()),
        sorted(there.files.keys() - here.files.keys()),
        sorted(key for key in shared if here.files[key] != there.files[key]),
    )


def newest_raw(paths: Iterable[str]) -> str | None:
    """The newest complete response among raw paths: keys carry a date and end in a UTC fetch
    stamp, so the lexically largest complete key is the newest."""
    listed = list(paths)
    bodies = {path.removesuffix(SUFFIX) for path in listed if path.endswith(SUFFIX)}
    complete = bodies & {path.removesuffix(META) for path in listed if path.endswith(META)}
    return max(complete, default=None)


def raw_lag(
    store: RawStore, prefixes: Iterable[str] = DAILY_RAW
) -> list[tuple[str, str | None, str | None]]:
    """(prefix, newest here, newest in R2) for the raw responses that grow every day."""
    if store.objects is None:
        raise ValueError("comparing with R2 needs a mirrored raw store")
    rows = []
    for prefix in prefixes:
        root = store.base_dir / prefix
        local = root.rglob("*") if root.exists() else []
        here = newest_raw(
            path.relative_to(store.base_dir).as_posix() for path in local if path.is_file()
        )
        remote = list_keys(store.objects, store.bucket or "", f"{R2_RAW}{prefix}")
        there = newest_raw(key.removeprefix(R2_RAW) for key, _ in remote)
        rows.append((prefix, here, there))
    return rows


def replay_window(keys: Iterable[str]) -> str | None:
    """`--start A --end B` covering the game dates of the given partition keys, or None."""
    dates = partition_dates(keys)
    return f"--start {min(dates)} --end {max(dates)}" if dates else None


def compact(n: int) -> str:
    """1234 as 1,234, and larger counts as 1.6M or 766k."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 100_000:
        return f"{n / 1000:.0f}k"
    return f"{n:,}"


def brief_line(states: list[TableState]) -> str:
    """One line for the SessionStart hook: the newest date, rows per table, and any table that
    lags that date or is missing."""
    present = [state for state in states if state.files]
    if not present:
        return "lake empty: run nhl ingest"
    newest = max((s.latest for s in present if s.latest), default=None)
    counts = ", ".join(f"{s.table} {compact(s.rows or 0)}" for s in present)
    parts = [f"lake to {newest}: {counts}" if newest else f"lake: {counts}"]
    behind = [
        f"{s.table} to {s.latest}" for s in present if newest and s.latest and s.latest < newest
    ]
    if behind:
        parts.append(f"behind: {', '.join(behind)}")
    missing = [s.table for s in states if not s.files]
    if missing:
        parts.append(f"missing: {', '.join(missing)}")
    return " · ".join(parts)
