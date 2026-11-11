"""scripts/goalie_rest.py (#418): a goalie's rest from his starts, the shot-level rows and fits,
the game-level columns and the family's decision, on made-up data."""

import math
from datetime import UTC, date, datetime, time, timedelta

import goalie_rest_fixtures as f
import polars as pl
import pytest

from nhl_edge.backtest import yardstick as ys
from nhl_edge.game import b2

gr = f.load()
D = date(2013, 11, 1)
LATE = datetime.combine(D, time(23), UTC)


def day(n: int) -> date:
    return D + timedelta(days=n)


def test_rest_reads_his_starts_on_earlier_dates() -> None:
    started = f.started([(1, day(-6), 0), (1, day(-3), 0), (1, day(-1), 0), (1, day(-20), 0)])
    found = gr.rest(
        f.asks([(1, D, LATE), (1, day(14), LATE + timedelta(days=14)), (2, D, LATE)]), started
    ).to_dicts()
    # Tonight: last start yesterday, three starts in the 7 days before (days 6, 3 and 1 back).
    assert found[0]["yesterday"] == 1.0 and found[0]["starts7"] == 3 and found[0]["days"] == 1
    # Two weeks later: none in the week before, and 15 days since the last.
    assert found[1]["yesterday"] == 0.0 and found[1]["starts7"] == 0 and found[1]["days"] == 15
    # A goalie with no start has no days and no recent starts.
    assert found[2]["days"] is None and found[2]["starts7"] == 0 and found[2]["yesterday"] == 0.0


def test_the_bands_of_days_since_his_last_start() -> None:
    days = pl.Series("days", [1, 2, 3, 4, 6, 7, 30, None], dtype=pl.Int64)
    bands = pl.DataFrame(days).select(band=gr.band(pl.col("days")))["band"].to_list()
    assert bands == ["1", "2", "3", "4-6", "4-6", "7+", "7+", "7+"]


def test_played_yesterday_flags_only_a_team_that_played_the_day_before() -> None:
    games = pl.DataFrame(
        {
            "game_id": [1, 2, 3],
            "game_date": [day(0), day(1), day(3)],
            "home": ["NYR", "PHI", "BOS"],
            "away": ["BOS", "NYR", "PHI"],
        }
    )
    found = {
        (r["game_id"], r["team"]): r["team_b2b"]
        for r in gr.played_yesterday(games).iter_rows(named=True)
    }
    assert found[(2, "NYR")] == 1.0 and found[(2, "PHI")] == 0.0
    assert found[(1, "NYR")] == 0.0 and found[(3, "BOS")] == 0.0 and found[(3, "PHI")] == 0.0


def test_shot_rows_keep_the_starters_shots_with_terms_known_before_the_start() -> None:
    shots, xg = f.step1_shots()
    rows, counts = gr.shot_rows(shots, xg, f.step1_read())
    found = {r["goalie_id"]: r for r in rows.iter_rows(named=True)}
    assert set(found) == {1, 2}
    g1, g2 = found[1], found[2]
    # G1 started yesterday for NYR, at home tonight on a team back-to-back, gamma 0.9 known.
    assert g1["band"] == "1" and g1["band_1"] == 1.0 and g1["starts7"] == 1.0
    assert g1["defending_home"] == 1.0 and g1["team_b2b"] == 1.0 and g1["goal"] == 1.0
    assert g1["log_gamma"] == pytest.approx(math.log(0.9))
    assert g1["logit_xg"] == pytest.approx(math.log(0.1 / 0.9))
    # G2 has no earlier start (7 or more), BOS rested, and his gamma came after the start.
    assert g2["band"] == "7+" and g2["band_7_plus"] == 1.0 and g2["starts7"] == 0.0
    assert g2["defending_home"] == 0.0 and g2["team_b2b"] == 0.0 and g2["log_gamma"] == 0.0
    assert counts == {
        "xg_fitted_at_or_after_the_fold_start": 0,
        "starters_without_a_known_gamma": 1,
    }


def test_the_rest_terms_help_the_fit_when_rest_matters() -> None:
    shots = f.shots(tired_effect=1.0)
    scored = gr.shot_scores(shots, f.calendar(shots))
    assert set(scored["season"].unique()) == set(ys.EXPLORE)
    assert scored["difference"].mean() < 0  # type: ignore[operator]
    flat = gr.shot_scores(f.shots(tired_effect=0.0), f.calendar(shots))
    assert scored["difference"].mean() < flat["difference"].mean()  # type: ignore[operator]


def test_band_descriptives_count_starter_games_and_shots() -> None:
    shots = f.shots()
    found = gr.by_band(gr.shot_scores(shots, f.calendar(shots)))
    assert set(found) == set(gr.BANDS)
    assert found["1"]["shots"] == 2 * 40 * 30 and found["1"]["starter_games"] == 2 * 40
    assert found["3"]["shots"] == 0 and found["3"]["goals_less_xg_per_100"] is None


def test_the_game_columns_average_the_candidates_by_their_start_probability() -> None:
    read = f.league_read()
    columns, missing = gr.rest_columns(read)
    assert missing == 0 and columns.height == read.frame.height
    assert columns.null_count().sum_horizontal().item() == 0
    game = read.frame.sort("game_id").row(100, named=True)
    moments = pl.DataFrame(
        {"game_id": [game["game_id"]], "prediction_utc": [game["prediction_utc"]]}
    )
    pool = read.b3_tables.goalie_starts.filter(
        pl.col("game_id") == game["game_id"], pl.col("observed_utc") < game["prediction_utc"]
    )
    usable, _ = b2.known_before(
        b2.game_inputs(read.b2_tables), b2.candidates(read.b2_tables), moments, "observed_utc"
    )
    home = usable["home"].item()
    asks = pool.with_columns(as_of_utc=pl.lit(game["prediction_utc"], dtype=f.UTC_TYPE))
    found = gr.rest(asks, gr.starts(read.b2_tables.actual_lineups))
    side = pl.when(pl.col("team") == home).then(1.0).otherwise(-1.0)
    expected = found.select(
        b2b=(side * pl.col("p_start") * pl.col("yesterday")).sum(),
        starts7=(side * pl.col("p_start") * pl.col("starts7")).sum(),
        rest_days=(
            side * pl.col("p_start") * pl.col("days").fill_null(gr.CAP).clip(upper_bound=gr.CAP)
        ).sum(),
    ).row(0, named=True)
    row = columns.filter(pl.col("game_id") == game["game_id"]).row(0, named=True)
    assert row["goalie_b2b_diff"] == pytest.approx(expected["b2b"])
    assert row["starts7_diff"] == pytest.approx(expected["starts7"])
    assert row["rest_days_diff"] == pytest.approx(expected["rest_days"])


def per_game(b2b_rested_gain: float = 0.0) -> pl.DataFrame:
    """Made-up per-game move skills: goalie_workload better than the baseline by 0.01 on every
    game, goalie_b2b by 0.01 on the back-to-backs and by b2b_rested_gain on the rested ones."""
    rows = []
    for season in ys.EXPLORE:
        for i in range(120):
            rested = i % 2 == 0
            base = 0.001 * (i % 7)
            moves = {
                "goalie base": base,
                "goalie_workload": base - 0.01,
                "goalie_b2b": base - (b2b_rested_gain if rested else 0.01),
            }
            for name, move in moves.items():
                rows.append(
                    {
                        "candidate": name,
                        "game_id": season * 1000 + i,
                        "season": season,
                        "game_date": date(season // 10_000, 10, 10) + timedelta(days=i),
                        "rested": rested,
                        "move": move,
                        "result": 0.0,
                    }
                )
    return pl.DataFrame(rows).with_columns(pl.col("season").cast(pl.Int32))


def shot_differences(value: float) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "season": [s for s in ys.EXPLORE for _ in range(60)],
            "game_date": [
                date(s // 10_000, 10, 10) + timedelta(days=i) for s in ys.EXPLORE for i in range(60)
            ],
            "difference": [value + 1e-6 * (i % 3) for _ in ys.EXPLORE for i in range(60)],
        }
    ).with_columns(pl.col("season").cast(pl.Int32))


def test_the_decision_keeps_by_holm_with_each_candidates_own_rule() -> None:
    decision = gr.decide(per_game(), shot_differences(-0.001))["estimates"]
    assert all(e["holds"] for e in decision.values())
    assert "kept" not in decision["shots"]
    # Workload helps on the rested games too; b2b holds and step 1 holds.
    assert decision["goalie_workload"]["kept"] and decision["goalie_b2b"]["kept"]
    # Without step 1, goalie_b2b isn't kept, whatever its own test says.
    without = gr.decide(per_game(), shot_differences(+0.001))["estimates"]
    assert not without["shots"]["holds"] and without["goalie_b2b"]["holds"]
    assert not without["goalie_b2b"]["kept"] and without["goalie_workload"]["kept"]


def test_the_family_and_its_rule_are_the_issues() -> None:
    assert gr.FAMILY == ("shots", "goalie_workload", "goalie_b2b") and gr.ALPHA == 0.05
    assert gr.WINDOW == 7 and gr.CAP == 7 and gr.REFERENCE == "2"
    assert gr.REFERENCE not in gr.BAND_COLUMNS and set(gr.BAND_COLUMNS) | {"2"} == set(gr.BANDS)
    assert gr.CANDIDATES["goalie base"] == ("goalie_diff", "home_back_to_back", "away_back_to_back")
    assert set(gr.PRIMARY) == set(gr.FAMILY) - {"shots"}
    assert all(baseline == "goalie base" for _, baseline in gr.PRIMARY.values())
    assert gr.SHOT_BASE == ("logit_xg", "log_gamma", "defending_home", "team_b2b")
    # The single columns are descriptive: never a family member or its baseline.
    singles = {*gr.WORKLOAD, *gr.B2B}
    assert not singles & {name for pair in gr.PRIMARY.values() for name in pair}
