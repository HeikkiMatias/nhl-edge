"""#423 (#417): when are a game's officials published? A measurement on the right-rail pages the
pre-game goalie poll already stores (#48), each definition fixed on the issue before any figure
was read (the comment of 2026-11-12). Counts and times only; it writes nothing to the lake.

    uv run nhl lake restore-raw --prefix nhl/pregame-right-rail
    uv run python scripts/officials_timing.py

**The copies:** `nhl/pregame-right-rail/<ET game date>/<game_id>/<fetch stamp>`, fetched fresh at
every odds slot (07:05, 12:45, 18:45, 19:45 and 21:45 ET) for the games within 18 hours, and
hourly at :50 UTC for the games within 75 minutes. A copy fetched at or after the scheduled start
is left out.

**The definitions:**
- the games: regular-season games of the lake's `games` with a copy fetched before their start;
- named: a copy's `gameInfo.referees` lists at least one referee (linesmen counted beside);
- first seen: the earliest copy that names them, as minutes before the start, bounded by the
  latest earlier copy that doesn't;
- known by the decision: a copy fetched before 13:30 ET on the game date names them (the midday
  slot polls at 12:45 ET, just before its snapshot and the decision, and 13:30 ET bounds the run).
  A game starting before 13:30 ET has no midday decision ahead of it and is counted apart. A
  game whose copies before 13:30 ET name none and include none from the midday slot (12:30 to
  13:30 ET) is uncovered: counted and left out of the share.

**The rule:** at least half of the covered evening games known by the decision: log the officials
through /add-data-source (step 2). Otherwise close #423 with the timing.

It writes reports/research/officials-timing-<yyyymmdd>-<shortsha>.md and .json.
"""

import argparse
import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from nhl_edge.backtest import reports
from nhl_edge.ingest.nhl_api import parse_utc
from nhl_edge.ingest.pregame import ET, RIGHT_RAIL
from nhl_edge.lake.raw import SUFFIX, RawStore
from nhl_edge.lake.tables import Lake, Reader

PREFIX = f"nhl/{RIGHT_RAIL}"
# The midday slot polls at 12:45 ET just before its snapshot and decision; this bounds its run.
DECISION = time(13, 30)
MIDDAY_FROM = time(12, 30)
REGULAR_SEASON = 2
# Bands of first seen, in hours before the start: (low, high].
BANDS = ((0, 1), (1, 3), (3, 6), (6, 9), (9, 12), (12, 18), (18, 48))
THRESHOLD = 0.5
UTC_TYPE = pl.Datetime("us", "UTC")


def officials(body: bytes) -> tuple[int, int]:
    """The referees and linesmen a right-rail page names."""
    info = json.loads(body).get("gameInfo") or {}
    return len(info.get("referees") or []), len(info.get("linesmen") or [])


@dataclass(frozen=True)
class Copy:
    game_id: int
    fetched_utc: datetime
    referees: int
    linesmen: int
    raw_key: str


def copies(store: RawStore) -> tuple[list[Copy], list[str]]:
    """Every stored right-rail copy with its fetch time and the officials it names, and the keys
    without a sidecar (incomplete), which are left out."""
    root = store.base_dir / PREFIX
    found, incomplete = [], []
    paths: Iterator[Path] = iter(sorted(root.rglob(f"*{SUFFIX}"))) if root.is_dir() else iter(())
    for path in paths:
        raw_key = path.relative_to(store.base_dir).as_posix().removesuffix(SUFFIX)
        if not (store.base_dir / f"{raw_key}.meta.json").exists():
            incomplete.append(raw_key)
            continue
        game_id = int(raw_key.split("/")[-2])
        referees, linesmen = officials(store.get(raw_key))
        fetched = parse_utc(store.meta(raw_key)["fetched_utc"])
        found.append(Copy(game_id, fetched, referees, linesmen, raw_key))
    return found, incomplete


def as_frame(found: list[Copy]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "game_id": [c.game_id for c in found],
            "fetched_utc": [c.fetched_utc for c in found],
            "referees": [c.referees for c in found],
            "linesmen": [c.linesmen for c in found],
        },
        schema={
            "game_id": pl.Int64,
            "fetched_utc": UTC_TYPE,
            "referees": pl.Int64,
            "linesmen": pl.Int64,
        },
    )


def at_et(day: date, moment: time) -> datetime:
    return datetime.combine(day, moment, ET).astimezone(UTC)


def measure(copied: pl.DataFrame, games: pl.DataFrame) -> pl.DataFrame:
    """One row per regular-season game with a copy fetched before its start: its copies, first
    seen and last empty (minutes before the start), whether the officials were known by the
    decision, and whether it is a matinee or uncovered."""
    regular = games.filter((pl.col("game_id") // 10_000 % 100) == REGULAR_SEASON).select(
        "game_id", "game_date", "start_utc"
    )
    before = copied.join(regular, on="game_id").filter(pl.col("fetched_utc") < pl.col("start_utc"))
    rows = []
    for (game_id,), part in before.sort("fetched_utc").group_by("game_id", maintain_order=True):
        start: datetime = part["start_utc"][0]
        day: date = part["game_date"][0]
        decision, midday_from = at_et(day, DECISION), at_et(day, MIDDAY_FROM)
        named = part.filter(pl.col("referees") > 0)
        first = named["fetched_utc"].min() if named.height else None
        empty = part.filter(pl.col("referees") == 0)
        if isinstance(first, datetime):
            empty = empty.filter(pl.col("fetched_utc") < first)
        last_empty = empty["fetched_utc"].max() if empty.height else None
        matinee = start < decision
        by_decision = part.filter(pl.col("fetched_utc") < decision)
        known = bool((by_decision["referees"] > 0).any()) if by_decision.height else False
        midday = by_decision.filter(pl.col("fetched_utc") >= midday_from).height > 0
        rows.append(
            {
                "game_id": game_id,
                "game_date": day,
                "start_utc": start,
                "copies": part.height,
                "first_seen_min": _minutes(start, first),
                "last_empty_min": _minutes(start, last_empty),
                "linesmen_with_first": int(named["linesmen"][0]) if named.height else None,
                "matinee": matinee,
                "known_by_decision": known,
                "covered": matinee or known or midday,
            }
        )
    return pl.DataFrame(
        rows,
        schema={
            "game_id": pl.Int64,
            "game_date": pl.Date,
            "start_utc": UTC_TYPE,
            "copies": pl.Int64,
            "first_seen_min": pl.Float64,
            "last_empty_min": pl.Float64,
            "linesmen_with_first": pl.Int64,
            "matinee": pl.Boolean,
            "known_by_decision": pl.Boolean,
            "covered": pl.Boolean,
        },
    ).sort("game_id")


def _minutes(start: datetime, moment: Any) -> float | None:
    if not isinstance(moment, datetime):
        return None
    return (start - moment) / timedelta(minutes=1)


def summary(per_game: pl.DataFrame) -> dict[str, Any]:
    """The counts the report gives, and the rule's outcome."""
    evening = per_game.filter(~pl.col("matinee"))
    covered = evening.filter(pl.col("covered"))
    known = int(covered["known_by_decision"].sum())
    share = known / covered.height if covered.height else None
    first = per_game["first_seen_min"].drop_nulls() / 60
    bands = {f"{low}-{high} h": int(((first > low) & (first <= high)).sum()) for low, high in BANDS}
    gap = (per_game["last_empty_min"] - per_game["first_seen_min"]).drop_nulls() / 60
    return {
        "games": per_game.height,
        "dates": [str(per_game["game_date"].min()), str(per_game["game_date"].max())],
        "copies": int(per_game["copies"].sum()),
        "copies_per_game_median": float(per_game["copies"].cast(pl.Float64).median() or 0.0),  # type: ignore[arg-type]
        "matinees": int(per_game["matinee"].sum()),
        "evening": evening.height,
        "uncovered": evening.height - covered.height,
        "covered": covered.height,
        "known_by_decision": known,
        "share_known": share,
        "never_named_before_start": int(per_game["first_seen_min"].null_count()),
        "first_seen_hours_median": float(first.median()) if first.len() else None,  # type: ignore[arg-type]
        "first_seen_bands": bands,
        "bracket_hours_median": float(gap.median()) if gap.len() else None,  # type: ignore[arg-type]
        "logged": share is not None and share >= THRESHOLD,
    }


def markdown(found: dict[str, Any], version: str) -> str:
    s = found["summary"]
    share = "none" if s["share_known"] is None else f"{s['share_known']:.0%}"
    median = s["first_seen_hours_median"]
    bracket = s["bracket_hours_median"]
    lines = [
        f"# When are a game's officials published? ({version})",
        "",
        "#423 (#417), each definition fixed on the issue before any figure was read (the comment",
        "of 2026-11-12). The pre-game goalie poll's stored right-rail pages (#48): every odds slot",
        "for the games within 18 hours, and hourly within 75 minutes of each start. Copies at or",
        "after the start are left out.",
        "",
        f"- **Games:** {s['games']:,} regular-season games from {s['dates'][0]} to "
        f"{s['dates'][1]}, with {s['copies']:,} copies before their starts (median "
        f"{s['copies_per_game_median']:g} a game). {found['incomplete']} incomplete copies left "
        "out.",
        f"- **Known by the decision** (a copy before 13:30 ET on the game date names a referee): "
        f"**{s['known_by_decision']:,} of {s['covered']:,}** covered evening games ({share}).",
        f"  {s['matinees']:,} games start before 13:30 ET and are counted apart; "
        f"{s['uncovered']:,} evening games have no copy from the midday slot and are left out.",
        f"- **First seen:** median {median:.1f} hours before the start"
        if median is not None
        else "- **First seen:** never before a start",
        f"  (the copy before it, without them, a median {bracket:.1f} hours earlier)."
        if bracket is not None
        else "",
        f"  {s['never_named_before_start']:,} games had no copy naming them before the start.",
        "",
        "| First seen, hours before the start | Games |",
        "|---|---|",
    ]
    for band, count in s["first_seen_bands"].items():
        lines.append(f"| {band} | {count:,} |")
    outcome = (
        "at least half the covered evening games had their officials by the decision: step 2, "
        "log them through /add-data-source."
        if s["logged"]
        else "fewer than half the covered evening games had their officials by the decision: "
        "close #423 with this timing."
    )
    lines += ["", f"**The rule's outcome:** {outcome}", ""]
    return "\n".join(line for line in lines if line is not None) + "\n"


def run(store: RawStore, lake: Reader) -> dict[str, Any]:
    found, incomplete = copies(store)
    copied = as_frame(found)
    seasons = sorted({game_id // 1_000_000 * 10_001 + 1 for game_id in copied["game_id"]})
    games = lake.read("games").filter(pl.col("season").is_in(seasons))
    per_game = measure(copied, games)
    return {
        "incomplete": len(incomplete),
        "summary": summary(per_game),
        "per_game": per_game.with_columns(
            pl.col("game_date", "start_utc").cast(pl.String)
        ).to_dicts(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="When are a game's officials published (#423)?")
    parser.add_argument("--out", type=Path, default=Path("reports/research"))
    args = parser.parse_args()
    found = run(RawStore(), Lake())
    version = reports.version("officials-timing", datetime.now(UTC))
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / f"{version}.json").write_text(json.dumps(found, indent=2, sort_keys=True))
    path = args.out / f"{version}.md"
    path.write_text(markdown(found, version))
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
