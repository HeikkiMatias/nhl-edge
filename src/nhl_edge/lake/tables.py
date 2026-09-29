"""Parquet tables in the lake: data/lake/<table>/ locally, mirrored to lake/<table>/ in R2.

Partitioned tables are laid out as <table>/season=S/game_date=D/part-0.parquet. A write replaces
every partition it touches, so the writer supplies each partition's full content and reruns are
idempotent. Unpartitioned tables are one file, <table>/part-0.parquet, rewritten whole. Every
write validates against the table's pandera schema.

The local copy is what readers use. A fresh machine, such as the nightly runner, pulls a table
from R2 before a read-modify-write.
"""

import io
from collections.abc import Collection
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import pandera.polars as pa
import polars as pl

from nhl_edge.lake.r2 import ObjectStore, R2Config, list_keys
from nhl_edge.lake.schemas import (
    ODDS_KEY,
    PREGAME_GOALIES_KEY,
    SBR_KEY,
    ActualLineups,
    Games,
    LakeOddsSnapshots,
    Players,
    PregameGoalies,
    SbrOdds,
    Schedule,
    ShiftCoverage,
    Shifts,
    Shots,
    dtypes,
)

LAKE_DIR = Path("data/lake")
R2_PREFIX = "lake"
PART = "part-0.parquet"


@dataclass(frozen=True)
class Table:
    schema: type[pa.DataFrameModel]
    key: tuple[str, ...]
    partition_by: tuple[str, ...] = ()

    def empty(self) -> pl.DataFrame:
        return pl.DataFrame(schema=dtypes(self.schema))


BY_DATE = ("season", "game_date")
TABLES: dict[str, Table] = {
    "games": Table(Games, ("game_id",), BY_DATE),
    "schedule": Table(Schedule, ("game_id",), BY_DATE),
    "players": Table(Players, ("player_id",)),
    "shots": Table(Shots, ("game_id", "event_id"), BY_DATE),
    "shifts": Table(Shifts, ("game_id", "player_id", "period", "shift_number"), BY_DATE),
    "actual_lineups": Table(ActualLineups, ("game_id", "player_id"), BY_DATE),
    "shift_coverage": Table(ShiftCoverage, ("game_id",), BY_DATE),
    "odds_snapshots": Table(LakeOddsSnapshots, ODDS_KEY, ("snapshot_date",)),
    "sbr_odds": Table(SbrOdds, SBR_KEY, ("season",)),
    "pregame_goalies": Table(PregameGoalies, PREGAME_GOALIES_KEY, BY_DATE),
}
# Partition columns replace_dates can replace a date at a time.
DATE_PARTITIONS = ("game_date", "snapshot_date")
# The tables parsed from each game's play-by-play, boxscore and shift chart.
FEED_TABLES = ("shots", "shifts", "actual_lineups", "shift_coverage")


def known_at(frame: pl.DataFrame, prediction_utc: datetime) -> pl.DataFrame:
    """The rows a prediction at prediction_utc may use: observed strictly before that moment."""
    return frame.filter(pl.col("observed_utc") < prediction_utc)


def _partition_value(value: object) -> str:
    return value.isoformat() if isinstance(value, date) else str(value)


class Lake:
    def __init__(
        self,
        base_dir: Path = LAKE_DIR,
        bucket: str | None = None,
        objects: ObjectStore | None = None,
    ) -> None:
        if (bucket is None) != (objects is None):
            raise ValueError("an R2 mirror needs both a bucket and an object store client")
        self.base_dir = base_dir
        self.bucket = bucket
        self.objects = objects

    @classmethod
    def from_env(cls, base_dir: Path = LAKE_DIR, *, mirror: bool, flag: str = "--r2") -> "Lake":
        """A local lake, mirrored to R2 when mirror is set. Mirroring without R2 is an error."""
        if not mirror:
            return cls(base_dir)
        config = R2Config.require(flag)
        return cls(base_dir, config.bucket, config.client())

    def write(self, table: str, frame: pl.DataFrame) -> list[str]:
        """Validate and write, replacing each partition the frame touches. Returns the file keys
        written, relative to the lake root."""
        spec = TABLES[table]
        validated = spec.schema.validate(frame).sort(spec.key)
        if not spec.partition_by:
            files = {f"{table}/{PART}": validated}
        else:
            files = {
                "/".join(
                    [table]
                    + [
                        f"{column}={_partition_value(value)}"
                        for column, value in zip(spec.partition_by, values, strict=True)
                    ]
                    + [PART]
                ): part
                for values, part in validated.partition_by(
                    list(spec.partition_by), as_dict=True, maintain_order=True
                ).items()
            }
        for key, part in files.items():
            buffer = io.BytesIO()
            part.write_parquet(buffer)
            self._put(key, buffer.getvalue())
        return sorted(files)

    def replace_dates(self, table: str, frame: pl.DataFrame, dates: Collection[date]) -> list[str]:
        """Make the frame the whole content of the given dates (game_date, or snapshot_date for
        odds): write its partitions, then delete any partition for those dates that the frame no
        longer has, locally and in R2, so a replay or parser fix that drops rows leaves nothing
        stale. Returns the keys written."""
        column = TABLES[table].partition_by[-1] if TABLES[table].partition_by else ""
        if column not in DATE_PARTITIONS:
            raise ValueError(f"{table} is not partitioned by a date")
        written = self.write(table, frame) if frame.height else []
        days = {f"{column}={day.isoformat()}" for day in dates}
        for key in self._file_keys(table) - set(written):
            if key.split("/")[-2] in days:
                self._delete(key)
        return written

    def read(self, table: str) -> pl.DataFrame:
        """The whole local table, or an empty frame with the schema's columns."""
        spec = TABLES[table]
        paths = sorted((self.base_dir / table).rglob("*.parquet"))
        if not paths:
            return spec.empty()
        return pl.read_parquet(paths).sort(spec.key)

    def pull(self, table: str) -> int:
        """Download the table's files from R2 over the local copy. Returns the number of files."""
        if self.objects is None:
            return 0
        keys = list_keys(self.objects, self.bucket or "", f"{R2_PREFIX}/{table}/")
        for r2_key, _ in keys:
            body = self.objects.get_object(Bucket=self.bucket, Key=r2_key)["Body"].read()
            self._write_local(r2_key.removeprefix(f"{R2_PREFIX}/"), body)
        return len(keys)

    def upsert(self, table: str, frame: pl.DataFrame) -> pl.DataFrame:
        """Merge rows into an unpartitioned table by its key, new rows winning, and write it back.
        Pulls from R2 first when mirrored, so rows another machine added are kept."""
        spec = TABLES[table]
        if spec.partition_by:
            raise ValueError(f"{table} is partitioned: write whole partitions instead")
        self.pull(table)
        existing = self.read(table)
        merged = pl.concat([existing.join(frame, on=list(spec.key), how="anti"), frame])
        self.write(table, merged)
        return merged.sort(spec.key)

    def _file_keys(self, table: str) -> set[str]:
        """Every file key of a table, local and in R2."""
        keys = {
            path.relative_to(self.base_dir).as_posix()
            for path in (self.base_dir / table).rglob("*.parquet")
        }
        if self.objects is not None:
            remote = list_keys(self.objects, self.bucket or "", f"{R2_PREFIX}/{table}/")
            keys |= {key.removeprefix(f"{R2_PREFIX}/") for key, _ in remote}
        return keys

    def _delete(self, key: str) -> None:
        (self.base_dir / key).unlink(missing_ok=True)
        if self.objects is not None:
            self.objects.delete_object(Bucket=self.bucket, Key=f"{R2_PREFIX}/{key}")

    def _put(self, key: str, data: bytes) -> None:
        self._write_local(key, data)
        if self.objects is not None:
            self.objects.put_object(
                Bucket=self.bucket,
                Key=f"{R2_PREFIX}/{key}",
                Body=data,
                ContentType="application/vnd.apache.parquet",
            )

    def _write_local(self, key: str, data: bytes) -> None:
        path = self.base_dir / key
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
