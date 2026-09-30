"""The audit report's post-game corrections section (#30, ADR 0004).

Each live-season game's feeds are fetched again RECHECK_AFTER after the copy the tables read
(`nhl recheck`). Both copies are parsed as the ingest parses them and compared row by row. A
difference is a correction that the backtest's late copies carry and live's first copies lack.
ADR 0004 accepts a moved goal scorer as small, so every other kind of difference is listed as a
problem to review; if such differences reach more than a few games a season, ADR 0004 is reopened.
"""

from dataclasses import dataclass
from datetime import date, timedelta

import polars as pl

from nhl_edge.backtest.seasons import FIRST_LIVE_SEASON
from nhl_edge.ingest.corrections import ADDED, CHANGED, RECHECK_AFTER, REMOVED, GameDiff, diff_game
from nhl_edge.lake.raw import RawStore
from nhl_edge.lake.tables import FEED_TABLES

# The one difference ADR 0004 accepts as small: a goal credited to another shooter.
SCORER = ("shots", "shooter_id")
# The nightly run after a game date fetches the tables' copy; a recheck is due RECHECK_AFTER later,
# on the nightly run of that day.
DUE_AFTER = RECHECK_AFTER + timedelta(days=1)
EXAMPLES = 5


@dataclass(frozen=True)
class Corrections:
    due: list[int]
    diffs: list[GameDiff]

    @property
    def missing(self) -> list[int]:
        checked = {diff.game_id for diff in self.diffs}
        return [game_id for game_id in self.due if game_id not in checked]


def corrections(store: RawStore, games: pl.DataFrame, as_of: date) -> Corrections:
    """Every live-season game whose recheck was due by as_of, and the diffs of those rechecked."""
    due = games.filter(
        pl.col("season") >= FIRST_LIVE_SEASON, pl.col("game_date") <= as_of - DUE_AFTER
    ).sort("game_date", "game_id")
    diffs = [diff for game in due.iter_rows(named=True) if (diff := diff_game(store, game))]
    return Corrections(due["game_id"].to_list(), diffs)


def _changes(result: Corrections) -> pl.DataFrame:
    frames = [
        diff.changes.with_columns(game_id=pl.lit(diff.game_id, pl.Int64)) for diff in result.diffs
    ]
    return (
        pl.concat(frames)
        if frames
        else pl.DataFrame(
            schema={"table": pl.String, "field": pl.String, "rows": pl.Int64, "game_id": pl.Int64}
        )
    )


def markdown_report(result: Corrections) -> str:
    if not result.due:
        return "No live-season game is due for a recheck yet."
    changes = _changes(result)
    lines = [
        "| Table | Games checked | Rows | Games changed | Rows added | Rows removed "
        "| Rows changed |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for table in FEED_TABLES:
        mine = changes.filter(pl.col("table") == table)

        def total(name: str, frame: pl.DataFrame = mine) -> int:
            return int(frame.filter(pl.col("field") == name)["rows"].sum())

        rows = sum(diff.rows[table] for diff in result.diffs)
        games = mine["game_id"].n_unique()
        lines.append(
            f"| {table} | {len(result.diffs)} | {rows:,} | {games} | {total(ADDED)} | "
            f"{total(REMOVED)} | {total(CHANGED)} |"
        )
    fields = changes.filter(~pl.col("field").is_in([ADDED, REMOVED, CHANGED]))
    if fields.height:
        by_field = (
            fields.group_by("table", "field")
            .agg(games=pl.col("game_id").n_unique(), rows=pl.col("rows").sum())
            .sort("table", "field")
        )
        lines += ["", "| Table | Field | Games | Rows |", "| --- | --- | ---: | ---: |"]
        lines += [
            f"| {table} | {name} | {games} | {rows} |"
            for table, name, games, rows in by_field.rows()
        ]
    return "\n".join(lines)


def problems(result: Corrections) -> list[str]:
    found = []
    missing = result.missing
    if missing:
        examples = ", ".join(str(game_id) for game_id in missing[:EXAMPLES])
        found.append(f"{len(missing)} games due for a recheck have none, e.g. {examples}")
    changes = _changes(result).filter(pl.col("field") != CHANGED)
    beyond = changes.filter(~((pl.col("table") == SCORER[0]) & (pl.col("field") == SCORER[1])))
    for (game_id,), rows in beyond.sort("game_id").group_by("game_id", maintain_order=True):
        listed = ", ".join(
            f"{table} {name} ({count} rows)"
            for table, name, count in rows.select("table", "field", "rows").rows()
        )
        found.append(f"{game_id}: more than the scorer changed: {listed}")
    return found
