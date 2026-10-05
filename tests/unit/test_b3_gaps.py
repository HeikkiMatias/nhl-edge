import numpy as np
import polars as pl
import pytest
from b3_fixtures import league

from nhl_edge.audit import b3_gaps
from nhl_edge.backtest.walk_forward import fold_start
from nhl_edge.game import b3

# The fixture's φ is drawn afresh for every game, moving a team's goals by up to half a goal
# between games: with φ fixed, the clean league has no jump.
DRAWN = league()
LEAGUE = b3.Tables(
    **{
        **DRAWN.__dict__,
        "goal_multipliers": DRAWN.goal_multipliers.with_columns(
            phi=pl.lit(1.0), multiplier=pl.col("gamma")
        ),
    }
)
TEST = 20182019
START = fold_start(LEAGUE.games.select("season", "start_utc"), TEST)
UTC_TYPE = pl.Datetime("us", "UTC")
GAP_GAMES = 30


def gaps_for(tables: b3.Tables, gap: float = 0.1) -> pl.DataFrame:
    """The first GAP_GAMES games of TEST as gaps: B3's probability at the start, and a market gap
    points lower (the third game's 0.25 lower)."""
    games = tables.games.filter(pl.col("season") == TEST).sort("game_id").head(GAP_GAMES)
    moments = games.select("game_id", prediction_utc="start_utc")
    predicted, _ = b3.predictions(tables, moments, TEST, START)
    third = pl.col("game_id") == games["game_id"][2]
    return (
        games.select("season", "game_id", "game_date", "home", "away")
        .join(predicted.select("game_id", p_b3="p_home"), on="game_id")
        .with_columns(p_b1=pl.col("p_b3") - pl.when(third).then(0.25).otherwise(gap))
        .with_columns(gap=pl.col("p_b3") - pl.col("p_b1"))
        .sort("game_id")
    )


def screened(tables: b3.Tables = LEAGUE) -> pl.DataFrame:
    return b3_gaps.screen(tables, gaps_for(tables), {TEST: START})


def replaced(**frames: pl.DataFrame) -> b3.Tables:
    return b3.Tables(**{**LEAGUE.__dict__, **frames})


def test_the_screen_explains_each_gap_and_flags_nothing_in_a_clean_league() -> None:
    frame = screened()
    assert frame.height == GAP_GAMES
    assert not frame.select(pl.any_horizontal(*b3_gaps.FLAGS).any()).item()
    # The terms add up to B3's log-odds, up to the goalie mixture's curvature.
    total = frame.select(
        pl.col("intercept") + pl.col("offset") + pl.sum_horizontal(*b3_gaps.TERMS)
    ).to_series()
    assert np.abs(total.to_numpy() - frame["logit_b3"].to_numpy()).max() < 0.05
    assert set(frame["driver"]) <= set(b3.INPUTS)
    assert (frame["delta_low"] <= frame["delta_g_hat"]).all()
    assert (frame["delta_g_hat"] <= frame["delta_high"]).all()
    # Each team's goals are its parts times κ·φ.
    parts = pl.col("home_xg_5v5") + pl.col("home_xg_pp") + pl.col("home_xg_sh")
    assert frame.select(((parts * pl.col("home_base") - pl.col("home_goals")).abs() < 1e-9).all())[
        0, 0
    ]


def test_a_gaps_file_from_other_code_is_refused() -> None:
    drifted = gaps_for(LEAGUE).with_columns(pl.col("p_b3") + 0.01)
    with pytest.raises(ValueError, match="rerun nhl backtest"):
        b3_gaps.screen(LEAGUE, drifted, {TEST: START})
    # A gap game the refit no longer predicts is not dropped from the screen.
    gaps = gaps_for(LEAGUE)
    first = gaps["game_id"][0]
    late = LEAGUE.schedule_terms.filter(pl.col("game_id") != first)
    with pytest.raises(ValueError, match="does not predict 1 gap games"):
        b3_gaps.screen(replaced(schedule_terms=late), gaps, {TEST: START})


def test_the_screen_refits_at_each_gaps_own_prediction_time() -> None:
    gaps = gaps_for(LEAGUE)

    def before(hours: int) -> pl.DataFrame:
        return gaps.join(
            LEAGUE.games.select(
                "game_id", prediction_utc=pl.col("start_utc") - pl.duration(hours=hours)
            ),
            on="game_id",
        )

    # Three hours before the start, B3's inputs are known and it is checked there.
    frame = b3_gaps.screen(LEAGUE, before(3), {TEST: START})
    assert frame.height == GAP_GAMES
    assert frame.sort("game_id")["prediction_utc"].equals(
        before(3).sort("game_id")["prediction_utc"].cast(UTC_TYPE)
    )
    # Nine hours before, they aren't yet: a screen at the start instead would explain the gap
    # with inputs the backtest's B3 never saw.
    with pytest.raises(ValueError, match="does not predict 30 gap games"):
        b3_gaps.screen(LEAGUE, before(9), {TEST: START})


def test_a_blend_gaps_file_without_b3s_own_probability_is_refused() -> None:
    old = gaps_for(LEAGUE).drop("p_b3").rename({"gap": "blend_gap"})
    with pytest.raises(ValueError, match="rerun nhl backtest"):
        b3_gaps.blend_gaps(old)


def test_each_bug_signature_is_flagged() -> None:
    games = gaps_for(LEAGUE).sort("game_id")
    first, second, third, fourth, fifth = games.head(5).iter_rows(named=True)

    def side(row: dict[str, object], which: str = "home") -> pl.Expr:
        return (pl.col("game_id") == pl.lit(row["game_id"])) & (
            pl.col("team") == pl.lit(row[which])
        )

    # The first game's home team has no candidate skaters or goalies.
    lineups = LEAGUE.lineups.filter(~side(first))
    starts = LEAGUE.goalie_starts.filter(~side(first))
    # The second game's home starter was given a 5% chance.
    starter = LEAGUE.actual_lineups.filter(side(second), pl.col("starting_goalie"))[
        "player_id"
    ].item()
    starts = starts.with_columns(
        p_start=pl.when(side(second) & (pl.col("goalie_id") == starter))
        .then(0.05)
        .otherwise(pl.col("p_start"))
    )
    # Four skaters nobody projected dressed for the third game's home team.
    extra = pl.DataFrame(
        {
            "game_id": [third["game_id"]] * 4,
            "season": [TEST] * 4,
            "game_date": [third["game_date"]] * 4,
            "team": [third["home"]] * 4,
            "player_id": [990_001 + k for k in range(4)],
            "role": ["F"] * 4,
            "starting_goalie": [False] * 4,
        }
    )
    actual = pl.concat([LEAGUE.actual_lineups, extra], how="diagonal_relaxed").with_columns(
        pl.col("observed_utc").fill_null(strategy="max")
    )
    # The fourth game's home skaters are rated half a goal better at 5v5: its strength jumps.
    skaters = LEAGUE.lineups.filter(side(fourth))["player_id"].implode()
    ratings = LEAGUE.player_ratings.with_columns(
        mean=pl.when(
            (pl.col("game_id") == fourth["game_id"])
            & pl.col("player_id").is_in(skaters)
            & (pl.col("component") == "ev_off")
        )
        .then(pl.col("mean") + 0.5)
        .otherwise(pl.col("mean"))
    )
    # The fifth game's home travel is far outside anything in training.
    terms = LEAGUE.schedule_terms.with_columns(
        home_travel_km=pl.when(pl.col("game_id") == fifth["game_id"])
        .then(1e6)
        .otherwise(pl.col("home_travel_km"))
    )
    tables = replaced(
        lineups=lineups,
        goalie_starts=starts,
        actual_lineups=actual,
        player_ratings=ratings,
        schedule_terms=terms,
    )
    frame = b3_gaps.screen(tables, gaps_for(tables), {TEST: START})
    flagged = {flag: frame.filter(pl.col(flag))["game_id"].to_list() for flag in b3_gaps.FLAGS}
    assert flagged["no_candidates"] == [first["game_id"]]
    assert flagged["average_goalie"] == [first["game_id"]]
    assert second["game_id"] in flagged["starter_surprise"]
    assert flagged["projection_miss"] == [third["game_id"]]
    assert fourth["game_id"] in flagged["jump"]
    assert fifth["game_id"] in flagged["out_of_range"]
    row = frame.filter(pl.col("game_id") == third["game_id"]).row(0, named=True)
    assert row["home_missed"] == 4 and row["away_missed"] == 0


def test_the_review_set_and_its_report() -> None:
    frame = screened()
    marked = b3_gaps.review_set(frame)
    assert marked.equals(b3_gaps.review_set(frame))  # seeded
    groups = dict(marked.group_by("review").len().rows())
    # One gap above 20 points; the sample takes the rest, up to SAMPLE.
    assert groups["large"] == 1
    assert groups["sample"] == min(b3_gaps.SAMPLE, GAP_GAMES - 1)
    text = b3_gaps.markdown(marked, "gaps_b3.csv", "b3-gaps-x")
    assert "## Games to review" in text and "| large |" in text
    assert text.count("| sample |") == groups["sample"]
    facts = b3_gaps.summary(marked)
    assert facts["games"] == GAP_GAMES and facts["flagged"] == 0
    assert 0 <= facts["timid"] <= 1
