"""Parquet tables in the lake: data/lake/<table>/ locally, mirrored to lake/<table>/ in R2.

Partitioned tables are laid out as <table>/season=S/game_date=D/part-0.parquet. A write replaces
every partition it touches, so the writer supplies each partition's full content and reruns are
idempotent. Unpartitioned tables are one file, <table>/part-0.parquet, rewritten whole. Every
write validates against the table's pandera schema.

The local copy is what readers use. A fresh machine, such as the nightly runner, pulls a table
from R2 before a read-modify-write. A pull downloads only the files that differ from the local
copy, and can be limited to some seasons.
"""

import hashlib
import io
from collections.abc import Collection
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import pandera.polars as pa
import polars as pl

from nhl_edge.lake.r2 import ObjectStore, R2Config, list_etags, list_keys
from nhl_edge.lake.schemas import (
    DAILYFACEOFF_GOALIES_KEY,
    ODDS_KEY,
    PREGAME_GOALIES_KEY,
    SBR_KEY,
    ActualLineups,
    DailyFaceoffGoalies,
    Faceoffs,
    Games,
    GoalieEffects,
    GoalieStarts,
    LakeOddsSnapshots,
    Penalties,
    Players,
    PregameGoalies,
    SbrOdds,
    Schedule,
    ScheduleTerms,
    ShiftCoverage,
    Shifts,
    Shots,
    ShotXg,
    StrengthTime,
    TeamStrength,
    dtypes,
)

LAKE_DIR = Path("data/lake")
R2_PREFIX = "lake"
PART = "part-0.parquet"
PULL_WORKERS = 16


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
    "strength_time": Table(
        StrengthTime,
        ("game_id", "team", "strength", "own_net_empty", "opp_net_empty", "strength_source"),
        BY_DATE,
    ),
    "penalties": Table(Penalties, ("game_id", "event_id"), BY_DATE),
    "faceoffs": Table(Faceoffs, ("game_id", "event_id"), BY_DATE),
    "shot_xg": Table(ShotXg, ("game_id", "event_id"), BY_DATE),
    "team_strength": Table(TeamStrength, ("game_id",), BY_DATE),
    "goalie_starts": Table(GoalieStarts, ("game_id", "team", "goalie_id"), BY_DATE),
    "goalie_effects": Table(GoalieEffects, ("game_id", "team", "goalie_id"), BY_DATE),
    "schedule_terms": Table(ScheduleTerms, ("game_id",), BY_DATE),
    "odds_snapshots": Table(LakeOddsSnapshots, ODDS_KEY, ("snapshot_date",)),
    "sbr_odds": Table(SbrOdds, SBR_KEY, ("season",)),
    "pregame_goalies": Table(PregameGoalies, PREGAME_GOALIES_KEY, BY_DATE),
    "dailyfaceoff_goalies": Table(DailyFaceoffGoalies, DAILYFACEOFF_GOALIES_KEY, BY_DATE),
}
# Partition columns replace_dates can replace a date at a time.
DATE_PARTITIONS = ("game_date", "snapshot_date")
# The tables parsed from each game's play-by-play, boxscore and shift chart.
FEED_TABLES = (
    "shots",
    "shifts",
    "actual_lineups",
    "shift_coverage",
    "strength_time",
    "penalties",
    "faceoffs",
)


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
        """The whole local table in its schema's columns, or an empty frame with them. A partition
        written before a nullable column joined the schema reads it as null, so a table stays
        readable while a replay rewrites its older partitions (#73). A partition without a
        required column raises: replay it."""
        spec = TABLES[table]
        paths = sorted((self.base_dir / table).rglob("*.parquet"))
        if not paths:
            return spec.empty()
        frame = pl.read_parquet(paths, schema=dtypes(spec.schema), missing_columns="insert")
        columns = spec.schema.to_schema().columns
        missing = [n for n, c in columns.items() if not c.nullable and frame[n].null_count()]
        if missing:
            raise ValueError(f"{table} has partitions without {missing}: replay them")
        return frame.sort(spec.key)

    def pull(
        self, table: str, seasons: Collection[int] | None = None, workers: int = PULL_WORKERS
    ) -> int:
        """Download the table's files from R2 over the local copy, or only the given seasons'
        partitions of a table partitioned by season. A local file whose MD5 equals R2's ETag is
        already the same bytes and is not downloaded again. Returns the number of files
        downloaded."""
        spec = TABLES[table]
        if seasons is not None and spec.partition_by[:1] != ("season",):
            raise ValueError(f"{table} is not partitioned by season")
        objects = self.objects
        if objects is None:
            return 0
        root = f"{R2_PREFIX}/{table}/"
        prefixes = [root] if seasons is None else [f"{root}season={s}/" for s in sorted(seasons)]
        listed: dict[str, str] = {}
        for prefix in prefixes:
            listed |= list_etags(objects, self.bucket or "", prefix)
        stale = sorted(key for key, etag in listed.items() if not self._same_bytes(key, etag))

        def download(r2_key: str) -> None:
            body = objects.get_object(Bucket=self.bucket, Key=r2_key)["Body"].read()
            self._write_local(r2_key.removeprefix(f"{R2_PREFIX}/"), body)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(download, stale))
        return len(stale)

    def _same_bytes(self, r2_key: str, etag: str) -> bool:
        path = self.base_dir / r2_key.removeprefix(f"{R2_PREFIX}/")
        if not path.is_file():
            return False
        return hashlib.md5(path.read_bytes(), usedforsecurity=False).hexdigest() == etag

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
