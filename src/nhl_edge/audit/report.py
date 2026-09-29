"""The data audit report (#9): every check over the local lake and raw cache, in one markdown file
under reports/audit/, named for the last day it covers.

Each section lists its problems, so that every one can become an issue once the report is
reviewed. One section waits for data that is not in the lake yet: the pre-game data that would
show when starting goalies are confirmed (#42 and #43).
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import polars as pl

from nhl_edge.audit import games as game_audit
from nhl_edge.audit import sbr as sbr_audit
from nhl_edge.audit import snapshots as snapshot_audit
from nhl_edge.backtest.seasons import SEASON_ROLES, SeasonRole
from nhl_edge.ingest import shift_coverage
from nhl_edge.ingest.sbr import SBR_SEASONS
from nhl_edge.lake.raw import RawStore
from nhl_edge.lake.tables import Lake
from nhl_edge.reference import check_games

DEFAULT_OUT = Path("reports/audit")


@dataclass
class Section:
    title: str
    body: str
    problems: list[str] = field(default_factory=list)


def build(lake: Lake, store: RawStore, as_of: date) -> list[Section]:
    """Every section of the report, over data up to and including as_of (an ET game date): games
    and shift charts dated after it, and snapshot runs for later slot days, are left out."""
    games = lake.read("games").filter(pl.col("game_date") <= as_of)
    listed = game_audit.listed_games(store)
    seasons = game_audit.season_report(games, listed, as_of)
    sections = [
        Section(
            "Games per season",
            "Final regular-season games in `games`, against each season's length and the NHL "
            "schedule listings in the raw cache.\n\n" + game_audit.markdown_report(seasons),
            game_audit.problems(seasons, games, listed, as_of),
        ),
        _sbr_section(lake, store, listed, games, as_of),
        _shift_section(lake.read("shift_coverage").filter(pl.col("game_date") <= as_of)),
        _reference_section(games),
        _snapshot_section(lake, store, listed, as_of),
        Section(
            "Starting goalies",
            "Not yet: the pre-game goalie poll (#43) logs the NHL's pre-game data from 2026-27 "
            "on, and #42 measures it. This section will say whether, and how long before the "
            "start, that data names each team's starting goalie.",
        ),
    ]
    return sections


def _sbr_section(
    lake: Lake, store: RawStore, listed: pl.DataFrame, games: pl.DataFrame, as_of: date
) -> Section:
    over = set(game_audit.seasons_over(listed, as_of))
    seasons = [season for season in SBR_SEASONS if season in over]
    if not seasons:
        return Section("SBR odds", "No SBR season is over by the audit date.")
    schedule = lake.read("schedule").filter(pl.col("game_date") <= as_of)
    reports, pages, coverage = sbr_audit.join_reports(store, schedule, games, seasons)
    priced = sbr_audit.price_seasons(seasons)
    odds = lake.read("sbr_odds").filter(pl.col("season").is_in(priced))
    coverage += sbr_audit.unpriced(odds, priced, pages)
    if not reports and odds.is_empty():
        return Section(
            "SBR odds",
            "No SBR season could be joined, and `sbr_odds` has no prices to check: run "
            "`nhl odds sbr`.",
            sorted(coverage),
        )
    lines = sbr_audit.moneylines(odds)
    moved = sbr_audit.moves(lines)
    conflicts = sbr_audit.puck_line_conflicts(odds, lines)
    held_out = [season for season in seasons if season not in priced]
    body = (
        "The SBR archive for the seasons over by the audit date. Prices are de-vigged with the "
        "multiplicative method (`market/devig.py`)"
        + (
            f"; the price checks leave out {', '.join(map(str, held_out))}, which phase 1 does "
            "not inspect (#10)"
            if held_out
            else ""
        )
        + ".\n\n"
        + sbr_audit.markdown_report(
            sbr_audit.join_report(reports, listed, held_out),
            sbr_audit.vig_report(lines),
            moved,
            sbr_audit.puck_line_report(odds, conflicts),
        )
    )
    found = sbr_audit.problems(reports, listed, lines, moved, conflicts, coverage, held_out)
    return Section("SBR odds", body, found)


def _shift_section(coverage: pl.DataFrame) -> Section:
    # As `nhl audit shifts`: the one-time test season stays out of design evidence.
    design = [s for s, role in SEASON_ROLES.items() if role is not SeasonRole.ONE_TIME_TEST]
    coverage = coverage.filter(pl.col("season").is_in(design))
    if coverage.is_empty():
        return Section("Shift coverage", "No shift coverage in the lake.", ["no shift coverage"])
    report = shift_coverage.season_report(coverage)
    return Section(
        "Shift coverage",
        "Shift charts per season, from `shift_coverage` (as `nhl audit shifts`), without the "
        "one-time test season.\n\n" + shift_coverage.markdown_report(report),
    )


def _reference_section(games: pl.DataFrame) -> Section:
    found = check_games(games)
    return Section(
        "Reference files",
        f"The reference files checked against {games.height:,} games (as `nhl audit reference`): "
        f"{len(found)} problems.",
        found,
    )


def _snapshot_section(lake: Lake, store: RawStore, listed: pl.DataFrame, as_of: date) -> Section:
    runs = snapshot_audit.slot_runs(store).filter(pl.col("slot_day") <= as_of)
    if runs.is_empty():
        return Section("Live odds snapshots", "No stored odds snapshots.", ["no odds snapshots"])
    first = runs["slot_day"].min()
    assert isinstance(first, date)
    days = [first + timedelta(days=i) for i in range((as_of - first).days + 1)]
    due = snapshot_audit.due_slots(listed, days)
    odds = lake.read("odds_snapshots").filter(pl.col("raw_key").is_in(runs["raw_key"].implode()))
    events = snapshot_audit.event_matches(odds)
    body = (
        f"Every stored `nhl odds snapshot` run from {first} to {as_of} (ET), against the slots "
        "due for the listed regular-season and playoff games.\n\n"
        + snapshot_audit.markdown_report(
            snapshot_audit.slot_report(runs, due),
            snapshot_audit.quote_age(odds),
            snapshot_audit.credit_report(runs),
            events,
        )
    )
    problems = snapshot_audit.problems(runs, due, events, as_of)
    return Section("Live odds snapshots", body, problems)


def markdown(sections: list[Section], as_of: date, generated: datetime) -> str:
    total = sum(len(section.problems) for section in sections)
    lines = [
        f"# Data audit, data to {as_of}",
        "",
        f"Written by `nhl audit report` at {generated:%Y-%m-%d %H:%M} UTC: {total} problems.",
        "Review it by hand, open an issue for every problem, and record any season or source "
        "dropped in an ADR (#9).",
    ]
    for section in sections:
        lines += ["", f"## {section.title}", "", section.body]
        if section.problems:
            lines += ["", f"Problems ({len(section.problems)}):", ""]
            lines += [f"- {problem}" for problem in section.problems]
    return "\n".join(lines) + "\n"
