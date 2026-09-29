from typing import Any

import polars as pl
import pytest
from feed_fixtures import MTL_ARI, OPENING_WEEK_GAMES, feed, feed_game, raw_key

from nhl_edge.ingest.lineups import parse_actual_lineups
from nhl_edge.ingest.shift_coverage import (
    markdown_report,
    on_ice_counts,
    season_report,
    shift_coverage,
)
from nhl_edge.ingest.shifts import ShiftDrops, parse_shifts
from nhl_edge.ingest.shots import parse_shots
from nhl_edge.lake.schemas import TOI_TOLERANCE_S

Feeds = tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame, ShiftDrops]


def parsed(game_id: int) -> Feeds:
    game = feed_game(game_id)
    shots = parse_shots(feed("play-by-play", game_id), game, "k")
    shifts, drops = parse_shifts(feed("shiftcharts", game_id), game, "k")
    lineups = parse_actual_lineups(feed("boxscore", game_id), game, "k")
    return shots, shifts, lineups, drops


def coverage(game_id: int, feeds: Feeds | None = None) -> dict[str, Any]:
    game = feed_game(game_id)
    shots, shifts, lineups, drops = feeds or parsed(game_id)
    frame = shift_coverage(game, shots, shifts, lineups, drops, raw_key("shiftcharts", game))
    return frame.row(0, named=True)


@pytest.mark.parametrize("game_id", [*OPENING_WEEK_GAMES[:2], MTL_ARI])
def test_a_clean_chart_is_complete_and_agrees_with_every_shot(game_id: int) -> None:
    row = coverage(game_id)
    assert row["complete"] is True
    assert (row["bad_rows"], row["players_without_shifts"], row["players_toi_off"]) == (0, 0, 0)
    assert (row["skater_mismatches"], row["goalie_mismatches"]) == (0, 0)
    assert row["players_dressed"] == 40


def test_penalty_shots_are_not_checked() -> None:
    shots, *_ = parsed(MTL_ARI)
    row = coverage(MTL_ARI)
    assert row["shots_checked"] == shots.height - 1 == 72


def test_on_ice_counts_at_a_pulled_goalie() -> None:
    # ARI pulled its goalie for a sixth skater before the empty-net goal (situation 0651).
    game = feed_game(MTL_ARI)
    shots, shifts, lineups, _ = parsed(MTL_ARI)
    counts = on_ice_counts(game, shots, shifts, lineups).filter(pl.col("event_id") == 856)
    assert counts.select(
        "away_goalie_on", "away_skaters_on", "home_skaters_on", "home_goalie_on"
    ).row(0) == (0, 6, 5, 1)


def test_a_real_contradiction_is_counted() -> None:
    # CAR at MIN, 2010-10-08: the chart is complete, yet at one shot it has a different group on
    # the ice than situationCode.
    row = coverage(2010020008)
    assert row["complete"] is True
    assert (row["skater_mismatches"], row["goalie_mismatches"]) == (1, 1)


def test_a_player_without_shifts_makes_the_chart_incomplete() -> None:
    shots, shifts, lineups, drops = parsed(MTL_ARI)
    skater = lineups.filter(pl.col("role") == "D")["player_id"][0]
    row = coverage(MTL_ARI, (shots, shifts.filter(pl.col("player_id") != skater), lineups, drops))
    assert (row["players_without_shifts"], row["complete"]) == (1, False)
    assert row["skater_mismatches"] > 0


def test_shifts_that_do_not_add_up_make_the_chart_incomplete() -> None:
    # A goalie's first-period shift cut short by more than the tolerance.
    shots, shifts, lineups, drops = parsed(MTL_ARI)
    longest = (shifts["end_s"] - shifts["start_s"]).arg_max()
    shorter = shifts.with_columns(
        pl.when(pl.int_range(pl.len()) == longest)
        .then(pl.col("start_s") + TOI_TOLERANCE_S + 1)
        .otherwise(pl.col("start_s"))
        .alias("start_s")
    )
    assert (shorter["start_s"] != shifts["start_s"]).sum() == 1
    row = coverage(MTL_ARI, (shots, shorter, lineups, drops))
    assert (row["players_toi_off"], row["complete"]) == (1, False)


def test_bad_rows_make_the_chart_incomplete_and_drops_do_not() -> None:
    shots, shifts, lineups, _ = parsed(MTL_ARI)
    dropped = coverage(MTL_ARI, (shots, shifts, lineups, ShiftDrops(dropped=6, foreign=3)))
    assert (dropped["dropped_rows"], dropped["foreign_rows"], dropped["complete"]) == (6, 3, True)
    bad = coverage(MTL_ARI, (shots, shifts, lineups, ShiftDrops(bad=1)))
    assert (bad["bad_rows"], bad["complete"]) == (1, False)


def test_season_report_sums_games_per_season() -> None:
    games = (*OPENING_WEEK_GAMES, MTL_ARI)
    rows = pl.concat(
        shift_coverage(feed_game(g), *parsed(g)[:3], ShiftDrops(bad=int(g == MTL_ARI)), "k")
        for g in games
    )
    report = season_report(rows)
    assert report["season"].to_list() == [20102011, 20222023]
    old, new = report.iter_rows(named=True)
    assert (old["games"], old["complete"], old["with_bad_rows"]) == (3, 3, 0)
    assert (new["games"], new["complete"], new["with_bad_rows"]) == (1, 0, 1)
    assert old["shots_checked"] == rows.filter(pl.col("season") == 20102011)["shots_checked"].sum()
    assert old["skater_mismatch_share"] == pytest.approx(1 / old["shots_checked"])
    table = markdown_report(report)
    assert table.splitlines()[2].startswith("| 20102011 | 3 | 3 (100.0%) | 0 | 0 | 0 | 0 |")
    assert table.splitlines()[3].startswith("| 20222023 | 1 | 0 (0.0%) | 0 | 1 |")
