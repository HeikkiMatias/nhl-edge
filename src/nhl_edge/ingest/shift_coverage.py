"""Shift coverage: how far a game's shift chart can be trusted (docs/plan.md sections 3 and 9).

Two checks per game. Each dressed player's shifts should add up to his boxscore time on ice. At
each unblocked shot, the players on the ice by the shift chart should match the situationCode: the
skaters and goalies of each team. RAPM depends on both, so the season report (`nhl audit shifts`)
is reviewed before it does.

A complete chart also supplies each shot's skater counts (chart_strength, ADR 0009), since
situationCode can stay a skater off for the rest of a game after a penalty (#28).
"""

import polars as pl

from nhl_edge.ingest.feeds import FeedGame
from nhl_edge.ingest.shifts import ShiftDrops
from nhl_edge.lake.schemas import (
    STRENGTH_SOURCES,
    TOI_TOLERANCE_S,
    ShiftCoverage,
    Shots,
    dtypes,
)


def on_ice_counts(
    game: FeedGame, shots: pl.DataFrame, shifts: pl.DataFrame, lineups: pl.DataFrame
) -> pl.DataFrame:
    """Skaters and goalies of each team on the ice at each checked shot, by the shift chart, next
    to what the situationCode says. A player is on the ice at second t when start_s < t <= end_s;
    a player missing from the lineup counts as a skater."""
    code = pl.col("situation_code")
    checked = shots.filter(~pl.col("is_penalty_shot") & code.str.contains(r"^[0-9]{4}$")).select(
        "event_id",
        "period",
        "seconds",
        code.str.slice(0, 1).cast(pl.Int8).alias("away_goalie"),
        code.str.slice(1, 1).cast(pl.Int8).alias("away_skaters"),
        code.str.slice(2, 1).cast(pl.Int8).alias("home_skaters"),
        code.str.slice(3, 1).cast(pl.Int8).alias("home_goalie"),
    )
    goalies = lineups.filter(pl.col("role") == "G").select("player_id", is_goalie=pl.lit(True))
    on_ice = (
        shifts.join(goalies, on="player_id", how="left")
        .select(
            "period",
            "start_s",
            "end_s",
            is_home=pl.col("team") == game.home,
            is_goalie=pl.col("is_goalie").fill_null(False),
        )
        .join(checked.select("event_id", "period", "seconds"), on="period")
        .filter((pl.col("start_s") < pl.col("seconds")) & (pl.col("seconds") <= pl.col("end_s")))
        .group_by("event_id")
        .agg(
            (pl.col("is_home") & ~pl.col("is_goalie")).sum().alias("home_skaters_on"),
            (~pl.col("is_home") & ~pl.col("is_goalie")).sum().alias("away_skaters_on"),
            (pl.col("is_home") & pl.col("is_goalie")).sum().alias("home_goalie_on"),
            (~pl.col("is_home") & pl.col("is_goalie")).sum().alias("away_goalie_on"),
        )
    )
    counts = ("home_skaters_on", "away_skaters_on", "home_goalie_on", "away_goalie_on")
    return checked.join(on_ice, on="event_id", how="left").with_columns(
        pl.col(c).fill_null(0) for c in counts
    )


# The skaters of one team the chart may put on the ice at a shot, its goalie in or pulled; outside
# this range a count is taken as the chart's error, and the shot keeps its situationCode counts.
CHART_SKATERS = (3, 6)


def chart_strength(
    game: FeedGame, shots: pl.DataFrame, shifts: pl.DataFrame, lineups: pl.DataFrame
) -> pl.DataFrame:
    """shots with skater counts from the shift chart (ADR 0009) at each shot on_ice_counts checks
    (it has a situationCode and is not a penalty shot) where the chart puts CHART_SKATERS of each
    team on the ice. The caller passes only a complete chart. is_empty_net and situation_code keep
    what situationCode says."""
    low, high = CHART_SKATERS
    counts = (
        on_ice_counts(game, shots, shifts, lineups)
        .filter(
            pl.col("home_skaters_on").is_between(low, high),
            pl.col("away_skaters_on").is_between(low, high),
        )
        .select("event_id", "home_skaters_on", "away_skaters_on")
    )
    home, chart = pl.col("is_home"), pl.col("home_skaters_on").is_not_null()
    own = pl.when(home).then(pl.col("home_skaters_on")).otherwise(pl.col("away_skaters_on"))
    other = pl.when(home).then(pl.col("away_skaters_on")).otherwise(pl.col("home_skaters_on"))
    counted = shots.join(counts, on="event_id", how="left").with_columns(
        skaters_for=pl.when(chart).then(own).otherwise(pl.col("skaters_for")).cast(pl.Int8),
        skaters_against=pl.when(chart)
        .then(other)
        .otherwise(pl.col("skaters_against"))
        .cast(pl.Int8),
        strength_source=pl.when(chart)
        .then(pl.lit(STRENGTH_SOURCES[1]))
        .otherwise(pl.col("strength_source")),
    )
    rebuilt = pl.format("{}v{}", pl.col("skaters_for"), pl.col("skaters_against"))
    counted = counted.with_columns(
        strength=pl.when(chart).then(rebuilt).otherwise(pl.col("strength"))
    )
    return Shots.validate(counted.select(list(dtypes(Shots))))


def shift_coverage(
    game: FeedGame,
    shots: pl.DataFrame,
    shifts: pl.DataFrame,
    lineups: pl.DataFrame,
    drops: ShiftDrops,
    raw_key: str,
) -> pl.DataFrame:
    """One ShiftCoverage row for the game."""
    shift_toi = shifts.group_by("player_id").agg(
        (pl.col("end_s") - pl.col("start_s")).sum().alias("shift_toi")
    )
    players = lineups.select("player_id", "toi_s").join(shift_toi, on="player_id", how="left")
    without_shifts = players.filter((pl.col("toi_s") > 0) & pl.col("shift_toi").is_null()).height
    toi_off = players.filter(
        ((pl.col("shift_toi") - pl.col("toi_s")).abs() > TOI_TOLERANCE_S)
        | pl.col("toi_s").is_null()
    ).height

    counts = on_ice_counts(game, shots, shifts, lineups)
    skaters_differ = (pl.col("home_skaters_on") != pl.col("home_skaters")) | (
        pl.col("away_skaters_on") != pl.col("away_skaters")
    )
    goalies_differ = (pl.col("home_goalie_on") != pl.col("home_goalie")) | (
        pl.col("away_goalie_on") != pl.col("away_goalie")
    )
    row = {
        **game.keys(),
        "shift_rows": shifts.height,
        "dropped_rows": drops.dropped,
        "foreign_rows": drops.foreign,
        "bad_rows": drops.bad,
        "players_dressed": lineups.height,
        "players_without_shifts": without_shifts,
        "players_toi_off": toi_off,
        "shots_checked": counts.height,
        "skater_mismatches": counts.filter(skaters_differ).height,
        "goalie_mismatches": counts.filter(goalies_differ).height,
        "complete": drops.bad == 0 and without_shifts == 0 and toi_off == 0,
        "observed_utc": game.observed_utc,
        "raw_key": raw_key,
    }
    return ShiftCoverage.validate(pl.DataFrame([row], schema=dtypes(ShiftCoverage)))


# A game whose shift chart contradicts situationCode at more than this share of its checked shots
# is counted separately in the season report.
MISMATCH_SHARE = 0.05


def season_report(coverage: pl.DataFrame) -> pl.DataFrame:
    """shift_coverage summed per season: how many charts are complete, and how often the players
    on the ice contradict the strength state."""
    shots = pl.col("shots_checked").sum()
    return (
        coverage.group_by("season")
        .agg(
            pl.len().alias("games"),
            pl.col("complete").sum().alias("complete"),
            (pl.col("foreign_rows") > 0).sum().alias("with_foreign_rows"),
            (pl.col("bad_rows") > 0).sum().alias("with_bad_rows"),
            (pl.col("players_without_shifts") > 0).sum().alias("with_missing_players"),
            (pl.col("players_toi_off") > 0).sum().alias("with_toi_off"),
            shots.alias("shots_checked"),
            (pl.col("skater_mismatches").sum() / shots).alias("skater_mismatch_share"),
            (pl.col("goalie_mismatches").sum() / shots).alias("goalie_mismatch_share"),
            (pl.col("skater_mismatches") > MISMATCH_SHARE * pl.col("shots_checked"))
            .sum()
            .alias("games_over_5pct"),
        )
        .with_columns((pl.col("complete") / pl.col("games")).alias("complete_share"))
        .sort("season")
    )


def markdown_report(report: pl.DataFrame) -> str:
    """The season report as a markdown table, for the PR and the audit (#9)."""
    header = (
        "| Season | Games | Complete | Foreign rows | Bad rows | Players missing | TOI off "
        "| Shots checked | Skaters differ | Goalies differ | Games > 5% |\n"
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"
    )
    lines = [
        f"| {r['season']} | {r['games']:,} | {r['complete']:,} ({r['complete_share']:.1%}) "
        f"| {r['with_foreign_rows']} | {r['with_bad_rows']} | {r['with_missing_players']} "
        f"| {r['with_toi_off']} | {r['shots_checked']:,} | {r['skater_mismatch_share']:.2%} "
        f"| {r['goalie_mismatch_share']:.2%} | {r['games_over_5pct']} |"
        for r in report.iter_rows(named=True)
    ]
    return "\n".join([header, *lines])
