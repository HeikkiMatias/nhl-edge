"""The data audit report (#9): every check over the local lake and raw cache, in one markdown file
under reports/audit/, named for the last day it covers.

Each section lists its problems, so that every one can become an issue once the report is
reviewed.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import polars as pl

from nhl_edge.audit import corrections as correction_audit
from nhl_edge.audit import games as game_audit
from nhl_edge.audit import goalies as goalie_audit
from nhl_edge.audit import player_seasons as player_season_audit
from nhl_edge.audit import plays as play_audit
from nhl_edge.audit import sbr as sbr_audit
from nhl_edge.audit import snapshots as snapshot_audit
from nhl_edge.audit import stints as stint_audit
from nhl_edge.audit import strength_time as strength_audit
from nhl_edge.backtest.seasons import OPEN_SEASONS, SEASON_ROLES, SeasonRole
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
        _strength_section(lake.read("strength_time"), games),
        _plays_section(lake, store, games),
        _stints_section(lake, games),
        _player_seasons_section(lake, store, as_of),
        _reference_section(games),
        _snapshot_section(lake, store, listed, as_of),
        _goalie_section(lake, games, as_of),
        _corrections_section(store, games, lake.read("actual_lineups"), as_of),
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
        "default multiplicative method (`market/devig.py`, ADR 0008)"
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


def _strength_section(frame: pl.DataFrame, games: pl.DataFrame) -> Section:
    # As the shift section: the one-time test season stays out, and so do seasons under way. The
    # minutes are hockey figures a feature could be shaped by, so they show only for open seasons.
    design = [s for s, role in SEASON_ROLES.items() if role is not SeasonRole.ONE_TIME_TEST]
    games = games.filter(pl.col("season").is_in(design))
    frame = frame.filter(pl.col("game_id").is_in(games["game_id"].implode()))
    report = strength_audit.season_report(frame, games, OPEN_SEASONS)
    body = (
        "Seconds at each strength state per team-game, from `strength_time` (#72, ADR 0009), "
        "without the one-time test season: whether each game's seconds add up to its length, "
        "and, for the seasons open now, the mean minutes per team-game.\n\n"
        + strength_audit.markdown_report(report)
    )
    return Section("Strength time", body, strength_audit.problems(frame, games))


def _plays_section(lake: Lake, store: RawStore, games: pl.DataFrame) -> Section:
    # As the strength section: the one-time test season stays out, and the per-game rates show
    # only for open seasons.
    design = [s for s, role in SEASON_ROLES.items() if role is not SeasonRole.ONE_TIME_TEST]
    games = games.filter(pl.col("season").is_in(design))
    wanted = games["game_id"].implode()
    penalties, faceoffs = (
        lake.read(table).filter(pl.col("game_id").is_in(wanted))
        for table in ("penalties", "faceoffs")
    )
    lineups = lake.read("actual_lineups").filter(pl.col("game_id").is_in(wanted))
    checked = play_audit.pim_check(penalties, play_audit.boxscore_pims(store, lineups), games)
    report = play_audit.season_report(penalties, faceoffs, checked, games, OPEN_SEASONS)
    body = (
        "Penalties and faceoffs from play-by-play (`penalties`, `faceoffs`, #96), without the "
        "one-time test season: the share of penalties that name the player who took it and the "
        "one who drew it, and the team-games whose penalties with a player add up to the "
        "penalty minutes of its players in the boxscore the tables read. For the seasons open "
        "now, penalties, minors (bench minors included) and faceoffs per game.\n\n"
        + play_audit.markdown_report(report)
    )
    return Section("Penalties and faceoffs", body, play_audit.problems(faceoffs, checked, games))


def _stints_section(lake: Lake, games: pl.DataFrame) -> Section:
    # As the strength section: the one-time test season stays out, and the shares of time and xG
    # show only for open seasons.
    design = [s for s, role in SEASON_ROLES.items() if role is not SeasonRole.ONE_TIME_TEST]
    games = games.filter(pl.col("season").is_in(design))
    wanted = games["game_id"].implode()
    stints, coverage, strength_time, shot_xg, faceoffs = (
        lake.read(table).filter(pl.col("game_id").is_in(wanted))
        for table in ("stints", "shift_coverage", "strength_time", "shot_xg", "faceoffs")
    )
    if stints.is_empty():
        return Section("Stints", "No stints in the lake: run `nhl stints`.", ["no stints"])
    report = stint_audit.season_report(
        stints, coverage, strength_time, shot_xg, faceoffs, games, OPEN_SEASONS
    )
    body = (
        "Stints from complete shift charts (`stints`, #97, ADR 0015), without the one-time test "
        "season: the games with stints, and for the seasons open now the stints RAPM leaves out "
        "and why (a team with fewer than 3 or more than 6 skaters, or two goalies), the share of "
        "faceoffs that open a stint and so give it a zone, and the share of all games' playing "
        "time and of all xG in the stints RAPM keeps. Goals cut stints, so a held-out season shows "
        "only its chart coverage.\n\n" + stint_audit.markdown_report(report)
    )
    return Section("Stints", body, stint_audit.problems(stints, coverage))


def _player_seasons_section(lake: Lake, store: RawStore, as_of: date) -> Section:
    # Counts only: the landing pages hold held-out seasons' goals and assists. As the stints
    # section, the one-time test season and the live seasons stay out.
    found = player_season_audit.problems(lake.read("players"), store)
    table = lake.read("player_league_seasons")
    found += player_season_audit.first_game_problems(table, lake.read("actual_lineups"))
    frame = table.filter(
        pl.col("observed_utc").dt.date() <= as_of, player_season_audit.shown(pl.col("season"))
    )
    if frame.is_empty():
        return Section(
            "Player league seasons",
            "No player league seasons in the lake: run `nhl player-seasons`.",
            ["no player league seasons", *found],
        )
    leagues = player_season_audit.top_leagues(frame)
    shared = player_season_audit.shared_leagues(frame)
    body = (
        "Each player's season lines in every league, from his cached landing page "
        "(`player_league_seasons`, #98), for the NHLe priors: per season public by the audit "
        "date, without the one-time test season and the live seasons, the players with lines and "
        "the lines (one per player, league abbreviation and game type, his teams summed) in the "
        f"{len(leagues)} leagues with the most of them. Counts only: no goals, assists or rates. "
        f"{shared:,} player-season-game types have lines of one league under two abbreviations, "
        "which a reader adding up by league must check.\n\n"
        + player_season_audit.markdown_report(
            player_season_audit.season_report(frame, leagues), leagues
        )
    )
    return Section("Player league seasons", body, found)


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


def _goalie_section(lake: Lake, games: pl.DataFrame, as_of: date) -> Section:
    starters = goalie_audit.starters(games, lake.read("actual_lineups"), lake.read("players"))
    polls = [lake.read(table) for table in ("pregame_goalies", "dailyfaceoff_goalies")]
    before = [poll.filter(pl.col("game_date") <= as_of) for poll in polls]
    frame = goalie_audit.team_games(starters, *before)
    body = (
        "Each pre-game goalie poll (#42, #48) against the starter in `actual_lineups`: when "
        "each source first named a goalie, and whether it was the starter.\n\n"
        + goalie_audit.markdown_report(frame)
    )
    return Section("Starting goalies", body, goalie_audit.problems(frame))


def _corrections_section(
    store: RawStore, games: pl.DataFrame, lineups: pl.DataFrame, as_of: date
) -> Section:
    result = correction_audit.corrections(store, games, lineups, as_of)
    body = (
        "Each live-season game's play-by-play, boxscore and shift chart fetched again a week "
        "after the copy the tables read (`nhl recheck`, #30), parsed the same way and compared "
        "row by row. A difference is a correction the backtest's late copies carry and live's "
        "first copies lack (ADR 0004).\n\n" + correction_audit.markdown_report(result)
    )
    return Section("Post-game corrections", body, correction_audit.problems(result))


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
