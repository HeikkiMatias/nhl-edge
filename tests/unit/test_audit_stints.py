import polars as pl
from stint_fixtures import (
    GAME,
    HOME_GOALIES,
    PERIOD_END,
    SEASON,
    build,
    coverage_frame,
    full_period,
    without,
)

from nhl_edge.audit import stints as audit

HELD_OUT = 20232024


def season_inputs() -> dict[str, pl.DataFrame]:
    """One game of the made-up season with a change at 600, a faceoff opening it and one in mid
    stint, two home skaters too many for 100 seconds, and xG of 0.5 in the stints RAPM keeps and
    0.25 in the one it leaves out; a second game with an incomplete chart."""
    shifts = [*without(full_period(), 5), (5, 0, 600), (6, 600, PERIOD_END), (7, 100, 200)]
    shifts = [*without(shifts, 6), (6, 100, 200), (6, 600, PERIOD_END)]
    shots = [(1, 150, True, False), (2, 300, True, False), (3, 700, False, False)]
    frame = build(
        shifts, shots=shots, xg={1: 0.25, 2: 0.2, 3: 0.3}, faceoffs=[(600, "O"), (650, "D")]
    )
    games = pl.DataFrame({"game_id": [GAME, GAME + 1], "season": [SEASON, SEASON]})
    coverage = pl.concat(
        [
            coverage_frame(),
            coverage_frame(complete=False).with_columns(game_id=pl.lit(GAME + 1, pl.Int64)),
        ]
    )
    strength_time = pl.DataFrame(
        {"season": [SEASON] * 2, "game_id": [GAME, GAME + 1], "game_seconds": [PERIOD_END] * 2}
    )
    shot_xg = pl.DataFrame({"season": [SEASON] * 4, "xg": [0.25, 0.2, 0.3, 0.25]})
    faceoffs = pl.DataFrame(
        {"game_id": [GAME] * 2, "season": [SEASON] * 2, "period": [1, 1], "seconds": [600, 650]},
        schema_overrides={"period": pl.Int8, "seconds": pl.Int32},
    )
    return {
        "stints": frame,
        "coverage": coverage,
        "strength_time": strength_time,
        "shot_xg": shot_xg,
        "faceoffs": faceoffs,
        "games": games,
    }


def report(open_seasons: tuple[int, ...]) -> pl.DataFrame:
    inputs = season_inputs()
    return audit.season_report(
        inputs["stints"],
        inputs["coverage"],
        inputs["strength_time"],
        inputs["shot_xg"],
        inputs["faceoffs"],
        inputs["games"],
        open_seasons,
    )


def test_the_season_report_counts_what_rapm_leaves_out_and_keeps() -> None:
    row = report((SEASON,)).row(0, named=True)
    assert (row["games"], row["complete"], row["with_stints"]) == (2, 1, 1)
    assert (row["stints"], row["dropped_skaters"], row["dropped_goalies"]) == (4, 1, 0)
    assert row["dropped_seconds"] == 100
    assert row["faceoff_starts"] == 0.5
    # 1,100 of the two games' 2,400 seconds, and 0.5 of the season's 1.0 xG.
    assert row["time_kept"] == 1100 / 2400
    assert row["xg_kept"] == 0.5


def test_a_held_out_season_keeps_its_counts_but_not_its_shares() -> None:
    row = report((HELD_OUT,)).row(0, named=True)
    assert row["stints"] == 4
    assert row["time_kept"] is None and row["xg_kept"] is None
    assert "| held out |" in audit.markdown_report(report((HELD_OUT,)))


def test_the_markdown_has_a_row_per_season() -> None:
    text = audit.markdown_report(report((SEASON,)))
    assert f"| {SEASON} | 2 | 1 | 1 | 4 | 1 | 0 | 100 | 50.0% | 45.8% | 50.0% |" in text


def test_a_complete_chart_without_stints_is_a_problem() -> None:
    inputs = season_inputs()
    assert audit.problems(inputs["stints"], inputs["coverage"]) == []
    missing = audit.problems(inputs["stints"].head(0), inputs["coverage"])
    assert missing == [f"{SEASON}: 1 games with a complete chart and no stints, e.g. {GAME}"]


def test_two_goalies_count_as_their_own_reason() -> None:
    shifts = [*full_period(), (HOME_GOALIES[1], 400, 500)]
    frame = build(shifts)
    inputs = season_inputs()
    row = audit.season_report(
        frame,
        inputs["coverage"],
        inputs["strength_time"],
        inputs["shot_xg"],
        inputs["faceoffs"],
        inputs["games"],
        (SEASON,),
    ).row(0, named=True)
    assert (row["dropped_skaters"], row["dropped_goalies"]) == (0, 1)
