from datetime import UTC, date, datetime
from pathlib import Path

import market_history
import numpy as np
import polars as pl
import pytest
from b2_fixtures import P_A, feature_tables, goalie_id
from b3_fixtures import GAMMA, WEIGHT, league, player_tables

from nhl_edge.backtest import b2_report, b3_report, reports
from nhl_edge.backtest.market import PREDICTION_LAG
from nhl_edge.backtest.subsets import SUBSETS
from nhl_edge.backtest.walk_forward import HOCKEY, fold_start, hockey_only, run
from nhl_edge.features import team_strength as ts
from nhl_edge.game import b2, b3
from nhl_edge.ratings import rapm

LEAGUE = league()
TEST = 20182019
START = fold_start(LEAGUE.games.select("season", "start_utc"), TEST)
DAY = date(2018, 10, 10)
EARLY = datetime(2018, 10, 10, 12, tzinfo=UTC)
KNOWN = datetime(2018, 10, 10, 14, tzinfo=UTC)
LATEST = datetime(2018, 10, 10, 15, tzinfo=UTC)
UTC_TYPE = pl.Datetime("us", "UTC")


def hand_tables() -> b3.Tables:
    """One game, BOS at home to TOR, small enough to work out by hand."""
    game = {
        "game_id": pl.lit(1, pl.Int64),
        "season": pl.lit(TEST, pl.Int32),
        "game_date": pl.lit(DAY),
    }
    lineups = pl.DataFrame(
        [
            # player, team, role, exp_5v5, exp_pp, exp_pk
            (11, "BOS", "F", 30.0, 4.0, 0.0),
            (12, "BOS", "D", 20.0, 1.0, 4.0),
            (19, "BOS", "G", 60.0, 6.0, 4.0),
            (21, "TOR", "F", 40.0, 2.0, 2.0),
            (22, "TOR", "F", 20.0, 0.0, 0.0),
        ],
        schema=["player_id", "team", "role", "exp_5v5", "exp_pp", "exp_pk"],
        orient="row",
    ).with_columns(**game, p_available=pl.lit(1.0), observed_utc=pl.lit(KNOWN, UTC_TYPE))
    spare = pl.DataFrame(
        {
            "game_id": [1],
            "team": ["BOS"],
            "role": ["F"],
            "exp_5v5": [10.0],
            "exp_pp": [0.0],
            "exp_pk": [1.0],
            "observed_utc": [KNOWN],
        }
    ).with_columns(pl.col("observed_utc").cast(UTC_TYPE))
    ratings = pl.DataFrame(
        [
            (11, "ev_off", 0.2, KNOWN),
            (11, "ev_def", 0.1, KNOWN),
            (11, "pp", 0.5, KNOWN),
            (11, "pk", 0.0, KNOWN),
            (12, "ev_off", 0.0, LATEST),
            (12, "ev_def", -0.3, LATEST),
            (12, "pp", 0.2, LATEST),
            (12, "pk", 0.4, LATEST),
            (21, "ev_off", 0.1, KNOWN),
            (21, "ev_def", 0.2, KNOWN),
            (21, "pp", 0.0, KNOWN),
            (21, "pk", 0.5, KNOWN),
            # 22 has no ratings: the reference skater, 0.
        ],
        schema=["player_id", "component", "mean", "observed_utc"],
        orient="row",
    ).with_columns(**game, observed_utc=pl.col("observed_utc").cast(UTC_TYPE))
    terms = pl.DataFrame(
        [
            ("ev", "intercept", 2.4),
            ("ev", f"season:{TEST}", 0.1),
            # Another season's term and the home term are not the game's rate.
            ("ev", "season:20172018", 9.0),
            ("ev", "home", 0.3),
            ("pp", "intercept", 6.0),
            ("pp", f"season:{TEST}", -0.5),
        ],
        schema=["model", "term", "value"],
        orient="row",
    ).with_columns(season=pl.lit(TEST), game_date=pl.lit(DAY), observed_utc=pl.lit(EARLY, UTC_TYPE))
    power = pl.DataFrame(
        {
            "team": ["BOS", "TOR"],
            "opponent": ["TOR", "BOS"],
            "is_home": [True, False],
            "pp_minutes": [6.0, 4.0],
            "sh_xg": [0.05, None],
        }
    ).with_columns(**game, observed_utc=pl.lit(EARLY, UTC_TYPE))
    multipliers = pl.DataFrame(
        {
            "team": ["BOS", "BOS", "TOR"],
            "opponent": ["TOR", "TOR", "BOS"],
            "goalie_id": [7, 8, None],
            "phi": [0.98, 0.98, 1.05],
            "gamma": [0.9, 1.1, 1.0],
            "league_finishing": [1.02, 1.02, None],
        }
    ).with_columns(**game, observed_utc=pl.lit(EARLY, UTC_TYPE))
    empty = pl.DataFrame()
    return b3.Tables(
        games=empty,
        schedule_terms=empty,
        goalie_starts=empty,
        actual_lineups=empty,
        lineups=lineups,
        lineup_replacements=spare,
        player_ratings=ratings,
        rapm_terms=terms,
        expected_power_plays=power,
        goal_multipliers=multipliers,
    )


# Worked by hand. BOS: 60 skater-minutes at 5v5 with the replacement (12 minutes), offense
# 30 * 0.2 * 5 / 60 = 0.5, defense (30 * 0.1 - 20 * 0.3) * 5 / 60 = -0.25, power play
# (4 * 0.5 + 0.2) * 5 / 5 = 2.2, penalty kill 4 * 0.4 * 4 / 5 = 1.28. TOR: 12 minutes, offense
# 40 * 0.1 * 5 / 60 = 1/3, defense 40 * 0.2 * 5 / 60 = 2/3, power play 0, penalty kill
# 2 * 0.5 * 4 / 2 = 2. The rates: 2.4 + 0.1 = 2.5 at 5v5, 6.0 - 0.5 = 5.5 on the power play.
BOS_5V5 = 12 / 60 * (2.5 + 0.5 - 2 / 3)
BOS_PP = 6 / 60 * (5.5 + 2.2 - 2.0)
TOR_5V5 = 12 / 60 * (2.5 + 1 / 3 + 0.25)
TOR_PP = 4 / 60 * (5.5 + 0.0 - 1.28)
BOS_RAW = BOS_5V5 + BOS_PP + 0.05
TOR_RAW = TOR_5V5 + TOR_PP


def test_a_teams_expected_goals_worked_by_hand() -> None:
    goals = b3.team_goals(hand_tables()).sort("team")
    assert goals["team"].to_list() == ["BOS", "TOR"]
    assert goals["xg_5v5"].to_list() == pytest.approx([BOS_5V5, TOR_5V5])
    assert goals["xg_pp"].to_list() == pytest.approx([BOS_PP, TOR_PP])
    assert goals["xg_sh"].to_list() == pytest.approx([0.05, 0.0])
    assert goals["raw"].to_list() == pytest.approx([BOS_RAW, TOR_RAW])
    # The latest row behind either team's goals: a BOS defenseman's ratings.
    assert goals["observed_utc"].to_list() == [LATEST, LATEST]


def test_a_team_without_candidates_plays_replacements() -> None:
    # TOR's first game: no candidates, its minutes all replacements, rated 0.
    tables = hand_tables()
    spare = pl.concat(
        [
            tables.lineup_replacements,
            tables.lineup_replacements.with_columns(
                team=pl.lit("TOR"), exp_5v5=pl.lit(60.0), exp_pp=pl.lit(2.0), exp_pk=pl.lit(2.0)
            ),
        ]
    )
    expansion = b3.Tables(
        **{
            **tables.__dict__,
            "lineups": tables.lineups.filter(pl.col("team") != "TOR"),
            "lineup_replacements": spare,
        }
    )
    goals = b3.team_goals(expansion).sort("team")
    assert goals["xg_5v5"].to_list() == pytest.approx(
        [12 / 60 * (2.5 + 0.5), 12 / 60 * (2.5 + 0.25)]
    )
    assert goals["xg_pp"].to_list() == pytest.approx([6 / 60 * (5.5 + 2.2), 4 / 60 * (5.5 - 1.28)])


def test_two_rapm_fits_on_one_date_are_refused() -> None:
    terms = hand_tables().rapm_terms
    with pytest.raises(ValueError, match="more than one fit"):
        b3.league_rates(pl.concat([terms, terms]))


def test_the_multipliers_and_the_goalie_pairs() -> None:
    tables = hand_tables()
    base, gammas = b3.multipliers(tables)
    bases = dict(base.select("team", "base").iter_rows())
    # League finishing is taken as 1 before any is public.
    assert bases == pytest.approx({"BOS": 1.02 * 0.98, "TOR": 1.05})
    assert gammas.height == 2
    usable = pl.DataFrame(
        {
            "game_id": [1],
            "home": ["BOS"],
            "away": ["TOR"],
            "home_raw": [BOS_RAW],
            "home_base": [bases["BOS"]],
            "away_raw": [TOR_RAW],
            "away_base": [bases["TOR"]],
        }
    )
    # TOR has two candidates; BOS none, so its goalie is average for TOR's attack.
    candidates = pl.DataFrame(
        {"game_id": [1, 1], "team": ["TOR", "TOR"], "goalie_id": [7, 8], "p_start": [0.6, 0.4]}
    )
    pairs = b3.scenarios(usable, candidates, gammas).sort("weight")
    assert pairs["weight"].to_list() == pytest.approx([0.4, 0.6])
    away = TOR_RAW * 1.05
    assert pairs["delta_g_hat"].to_list() == pytest.approx(
        [BOS_RAW * bases["BOS"] * 1.1 - away, BOS_RAW * bases["BOS"] * 0.9 - away]
    )


def test_the_prediction_averages_over_the_pairs() -> None:
    model = b3.B3Model(
        season=TEST,
        settings=b2.Settings(1.0),
        intercept=0.1,
        weights=tuple(1.0 if name == "delta_g_hat" else 0.0 for name in b3.INPUTS),
        means=tuple(0.0 for _ in b3.INPUTS),
        scales=tuple(1.0 for _ in b3.INPUTS),
        games=1,
        train_cutoff=datetime(2018, 4, 9, tzinfo=UTC),
    )
    inputs = pl.DataFrame(
        {"game_id": [1], "offset": [0.2], **{n: [0.0] for n in b3.INPUTS if n != "delta_g_hat"}}
    )
    pairs = pl.DataFrame({"game_id": [1, 1], "weight": [0.25, 0.75], "delta_g_hat": [1.0, -1.0]})
    expected = 0.25 / (1 + np.exp(-(0.3 + 1.0))) + 0.75 / (1 + np.exp(-(0.3 - 1.0)))
    assert model.predict(inputs, pairs)["p_home"].item() == pytest.approx(expected)


def test_training_uses_the_goalies_who_started() -> None:
    inputs = b3.game_inputs(LEAGUE)
    delta = b3.starters_delta(LEAGUE, inputs).join(inputs, on="game_id").sort("game_id")
    starters = LEAGUE.actual_lineups.filter("starting_goalie").select(
        "game_id", "team", "player_id"
    )
    row = delta.row(0, named=True)

    def gamma(game: int, team: str) -> float:
        started = starters.filter(pl.col("game_id") == game, pl.col("team") == team)
        return GAMMA["A"] if started["player_id"].item() == goalie_id(team, "A") else GAMMA["B"]

    # Each attack faces the other side's starter.
    expected = row["home_raw"] * row["home_base"] * gamma(row["game_id"], row["away"]) - row[
        "away_raw"
    ] * row["away_base"] * gamma(row["game_id"], row["home"])
    assert row["delta_g_hat"] == pytest.approx(expected)
    # A starter who was not a candidate faces each attack at gamma 1.
    game = row["game_id"]
    missing = b3.Tables(
        **{
            **LEAGUE.__dict__,
            "goal_multipliers": LEAGUE.goal_multipliers.with_columns(
                goalie_id=pl.when(pl.col("game_id") == game)
                .then(pl.lit(None, pl.Int64))
                .otherwise(pl.col("goalie_id"))
            ),
        }
    )
    plain = row["home_raw"] * row["home_base"] - row["away_raw"] * row["away_base"]
    assert b3.starters_delta(missing, inputs).filter(pl.col("game_id") == game)[
        "delta_g_hat"
    ].item() == pytest.approx(plain)


def test_the_fit_recovers_the_known_weight() -> None:
    inputs = b3.game_inputs(LEAGUE)
    train = b3.training_games(LEAGUE, inputs, TEST, START, "observed_utc")
    assert set(train["season"]) == {20162017, 20172018} and train.height == 800
    assert (train["known_utc"] < START).all()
    model = b3.fit(train, b2.Settings(0.1), TEST)
    weights = dict(zip(b3.INPUTS, model.weights, strict=True))
    scales = dict(zip(b3.INPUTS, model.scales, strict=True))
    # On standardized inputs: per goal, close to the truth (about two standard errors).
    assert weights["delta_g_hat"] / scales["delta_g_hat"] == pytest.approx(WEIGHT, rel=0.25)
    # Inputs that do not move the result stay near 0, and β0 absorbs nothing beyond h_s.
    for name in b2.SCHEDULE_INPUTS:
        assert abs(weights[name]) < 0.25
    assert abs(model.intercept) < 0.15


def test_predictions_mix_over_starters_and_carry_the_cutoff() -> None:
    moments = LEAGUE.games.filter(pl.col("season") == TEST).select(
        "game_id", prediction_utc="start_utc"
    )
    predicted, model = b3.predictions(LEAGUE, moments, TEST, START)
    assert predicted.height == moments.height
    assert (predicted["train_cutoff"] == model.train_cutoff).all()
    assert b3.TUNED_CUTOFF <= model.train_cutoff < START
    assert model.settings == b2.TUNED
    assert predicted["p_home"].is_between(0, 1, closed="none").all()
    assert model.weights[b3.SKILL] > 0
    # The mixture lies between the probabilities of the goalie pairs.
    inputs = b3.game_inputs(LEAGUE).filter(pl.col("season") == TEST)
    _, gammas = b3.multipliers(LEAGUE)
    pool = LEAGUE.goalie_starts.filter(pl.col("season") == TEST)
    pairs = b3.scenarios(inputs, pool, gammas)
    assert pairs.group_by("game_id").len()["len"].unique().to_list() == [4]
    assert pairs.group_by("game_id").agg(pl.col("weight").sum())["weight"].to_list() == (
        pytest.approx([1.0] * moments.height)
    )
    first = pairs.filter(pl.col("game_id") == moments["game_id"][0]).sort("weight")
    assert first["weight"].to_list() == pytest.approx(
        sorted([P_A * P_A, P_A * (1 - P_A), (1 - P_A) * P_A, (1 - P_A) ** 2])
    )


def test_a_fold_before_the_tuning_cutoff_is_refused() -> None:
    early = league((20152016, 20162017), games=40)
    start = fold_start(early.games.select("season", "start_utc"), 20162017)
    moments = early.games.filter(pl.col("season") == 20162017).select(
        "game_id", prediction_utc="start_utc"
    )
    with pytest.raises(ValueError, match="before the tuning cutoff"):
        b3.predictions(early, moments, 20162017, start)


def test_input_problems() -> None:
    assert b3.input_problems(LEAGUE, TEST) == []
    first = int(LEAGUE.games["game_id"].sort()[0])
    day = LEAGUE.games.filter(pl.col("game_id") == first)["game_date"].item()
    on_day = LEAGUE.games.filter(pl.col("game_date") == day).height
    gap = b3.Tables(
        **{
            **LEAGUE.__dict__,
            "lineups": LEAGUE.lineups.filter(pl.col("game_id") != first),
            "rapm_terms": LEAGUE.rapm_terms.filter(pl.col("game_date") != day),
        }
    )
    assert b3.input_problems(gap, TEST) == sorted(
        [
            f"20162017: 1 games without lineups, e.g. {first}",
            f"20162017: {on_day} games without rapm_terms, e.g. {first}, {first + 1}, {first + 2}",
        ]
    )


def test_only_rapms_first_day_may_lack_a_league_rate(monkeypatch: pytest.MonkeyPatch) -> None:
    # RAPM's first day has no fit by construction; any later day without one is a problem,
    # whatever the table's first remaining date.
    monkeypatch.setattr(rapm, "FIRST_SEASON", 20162017)
    days = LEAGUE.rapm_terms["game_date"].unique().sort()
    on_day = [LEAGUE.games.filter(pl.col("game_date") == d)["game_id"].sort() for d in days[:2]]
    opening = LEAGUE.rapm_terms.filter(pl.col("game_date") != days[0])
    assert b3.input_problems(b3.Tables(**{**LEAGUE.__dict__, "rapm_terms": opening}), TEST) == []
    span = opening.filter(pl.col("game_date") != days[1])
    (problem,) = b3.input_problems(b3.Tables(**{**LEAGUE.__dict__, "rapm_terms": span}), TEST)
    examples = ", ".join(str(g) for g in on_day[1].head(3))
    assert problem == f"20162017: {on_day[1].len()} games without rapm_terms, e.g. {examples}"


def test_a_game_without_replacement_rows_is_a_problem() -> None:
    first = int(LEAGUE.games["game_id"].sort()[0])
    gap = b3.Tables(
        **{
            **LEAGUE.__dict__,
            "lineup_replacements": LEAGUE.lineup_replacements.filter(pl.col("game_id") != first),
        }
    )
    assert b3.input_problems(gap, TEST) == [
        f"20162017: 1 games without lineup_replacements, e.g. {first}"
    ]


def report_flags(games: pl.DataFrame) -> pl.DataFrame:
    """The first ten games after a trade, the first twenty after an injury, none after a lineup
    change."""
    ids = games.filter(pl.col("season") == TEST).sort("game_id")["game_id"]
    rank = pl.int_range(pl.len())
    return (
        pl.DataFrame({"game_id": ids})
        .with_columns(trade=rank < 10, injury=rank < 20, lineup_change=pl.lit(False))
        .with_columns(any=pl.any_horizontal(*SUBSETS))
    )


def actual_minutes(lineups: pl.DataFrame, games: pl.DataFrame) -> pl.DataFrame:
    """Each projected skater's actual minutes: 5v5 a minute off his projection either way, and
    on the power play the projected unit but its fifth man swapped for the sixth skater."""
    k = pl.col("player_id") % 100
    return lineups.filter(pl.col("game_id").is_in(games["game_id"].implode())).select(
        "game_id",
        "team",
        "player_id",
        "season",
        "game_date",
        **{
            "5v5": pl.col("exp_5v5") / pl.col("p_available")
            + pl.when(k % 2 == 0).then(1.0).otherwise(-1.0),
            "pp": pl.when(k == 4).then(0.5).when(k == 5).then(4.0).otherwise(3.0),
        },
    )


def test_the_walk_forward_scores_b3_beside_b2() -> None:
    odds, games = market_history.seasons([20172018, TEST], games=40)
    tables = feature_tables(games)
    players = player_tables(games, tables)
    fits2: dict[str, dict[int, b2.B2Model]] = {}
    fits3: dict[str, dict[int, b3.B3Model]] = {}
    predictions, coverage, b1_fits = run(
        odds, games, [TEST], b2_tables=tables, b2_fits=fits2, b3_tables=players, b3_fits=fits3
    )
    for experiment in ("E1", "E2"):
        rows = predictions.filter(pl.col("experiment") == experiment)
        b1 = rows.filter(pl.col("model") == "B1")
        b3_rows = rows.filter(pl.col("model") == "B3")
        assert sorted(b3_rows["game_id"]) == sorted(b1["game_id"])
        assert set(b3_rows["method"]) == {"none"}
        counts = coverage[experiment][TEST]
        assert counts["b3_scored"] == b3_rows.height == counts["b2_scored"] == counts["scored"]
        assert counts["b3_trained_on"] == fits3[experiment][TEST].games == 40
        assert (b3_rows["train_cutoff"] < b3_rows["prediction_utc"]).all()
    report = reports.summary(
        predictions, coverage, b1_fits, [TEST], "backtest-x", datetime.now(UTC)
    )
    report = b2_report.add(
        report, predictions, fits2, games, tables.goalie_starts, tables.actual_lineups
    )
    flags = report_flags(games)
    report = b3_report.add(
        report,
        predictions,
        fits3,
        games,
        tables.goalie_starts,
        tables.actual_lineups,
        players.lineups,
        actual_minutes(players.lineups, games),
        flags,
    )
    model = report["experiments"]["E1"]["models"]["B3"]
    assert set(model) >= {
        "log_loss",
        "paired_against_B1",
        "paired_against_B2",
        "fits",
        "gate_2_subsets",
        "calibration",
        "gaps_over_8_points",
        "lineup_quality",
    }
    fit = model["fits"][str(TEST)]
    assert fit["l2"] == b2.TUNED.l2 and set(fit["weights"]) == set(b3.INPUTS)
    # B3 less B2, game by game.
    rows = predictions.filter(pl.col("experiment") == "E1")
    paired = b3_report.against(rows)
    assert paired.height == 40
    per_model = {
        m: rows.filter(pl.col("model") == m).sort("game_id")["log_loss"].to_numpy()
        for m in ("B2", "B3")
    }
    assert model["paired_against_B2"]["pooled"]["mean"] == pytest.approx(
        float(np.mean(per_model["B3"] - per_model["B2"]))
    )
    subsets = model["gate_2_subsets"]
    assert {name: subsets[name]["games"] for name in (*SUBSETS, "any")} == {
        "trade": 10,
        "injury": 20,
        "lineup_change": 0,
        "any": 20,
    }
    assert subsets["lineup_change"]["pooled"] is None
    assert subsets["trade"]["pooled"]["games"] == 10
    # The 40 games span two weeks and have an interval; the first ten, in one week, do not.
    assert model["paired_against_B2"]["pooled"]["low"] is not None
    assert subsets["trade"]["pooled"]["low"] is None
    assert subsets["trade"]["per_season"][str(TEST)]["high"] is None
    gaps = model["gaps_over_8_points"]
    listed = b3_report.gap_rows(rows, games)
    assert gaps["games_compared"] == 40 and gaps["count"] == listed.height
    assert (listed["gap"].abs() > 0.08).all() and "home_win" not in listed.columns
    quality = model["lineup_quality"]
    assert quality["goalie_starts"]["pooled"]["brier"]["games"] == 80
    # Every candidate's minutes are a minute off; four of the five on the unit were named.
    ice = quality["ice_time"]
    assert ice["minutes_5v5_mae"]["pooled"]["mean"] == pytest.approx(1.0)
    assert ice["minutes_5v5_mae"]["pooled"]["games"] == 80
    assert ice["pp_unit_accuracy"]["pooled"]["mean"] == pytest.approx(0.8)
    # The fold's cutoff covers B3's fit as well.
    assert report["train_cutoff"][str(TEST)] >= fit["train_cutoff"]


def test_the_gaps_file_lists_each_experiments_gaps(tmp_path: Path) -> None:
    rows = pl.DataFrame(
        {
            "experiment": ["E1"] * 4 + ["E2"] * 4,
            "model": ["B3", "B1", "B3", "B1"] * 2,
            "season": [TEST] * 8,
            "game_id": [1, 1, 2, 2] * 2,
            "game_date": [DAY] * 8,
            "p_home": [0.70, 0.55, 0.50, 0.48, 0.60, 0.55, 0.30, 0.45],
        }
    )
    games = pl.DataFrame({"game_id": [1, 2], "home": ["BOS", "MTL"], "away": ["TOR", "NYR"]})
    written = pl.read_csv(b3_report.write_gaps(rows, games, tmp_path))
    assert written.select("experiment", "game_id").rows() == [("E1", 1), ("E2", 2)]
    assert written["gap"].to_list() == pytest.approx([0.15, -0.15])


def test_hockey_only_scores_b2_and_b3_at_the_as_of_time() -> None:
    b2_tables = feature_tables(LEAGUE.games, 4)
    fits2: dict[str, dict[int, b2.B2Model]] = {}
    fits3: dict[str, dict[int, b3.B3Model]] = {}
    predictions, coverage = hockey_only(LEAGUE.games, [TEST], b2_tables, LEAGUE, fits2, fits3)
    assert set(predictions["experiment"]) == {HOCKEY} and set(predictions["method"]) == {"none"}
    # A second after the as-of time, when its rows are known.
    at = LEAGUE.games.select(
        "game_id", at=ts.as_of(pl.col("game_date"), pl.col("start_utc")) + PREDICTION_LAG
    )
    for model in ("B2", "B3"):
        rows = predictions.filter(pl.col("model") == model).join(at, on="game_id")
        assert rows.height == 400 and set(rows["season"]) == {TEST}
        assert (rows["train_cutoff"] < rows["prediction_utc"]).all()
        timed = rows.join(LEAGUE.games.select("game_id", "start_utc"), on="game_id")
        assert (timed["prediction_utc"] == timed["at"]).all()
        assert (timed["prediction_utc"] < timed["start_utc"]).all()
    counts = coverage[HOCKEY][TEST]
    assert counts == {
        "games": 400,
        "b2_scored": 400,
        "b2_trained_on": fits2[HOCKEY][TEST].games,
        "b3_scored": 400,
        "b3_trained_on": fits3[HOCKEY][TEST].games,
    }
    assert fits3[HOCKEY][TEST].games == 800
    flags = report_flags(LEAGUE.games)
    report = b3_report.hockey(
        predictions, coverage, fits3, flags, [TEST], "backtest-x", datetime.now(UTC)
    )
    assert set(report["models"]) == {"B2", "B3"}
    assert report["coverage"] == {str(TEST): counts}
    b3_model = report["models"]["B3"]
    assert set(b3_model) == {
        "log_loss",
        "calibration",
        "fits",
        "paired_against_B2",
        "gate_2_subsets",
    }
    assert b3_model["gate_2_subsets"]["trade"]["games"] == 10
    # B3 is fitted on the true form, B2 on a different one: B3 does better.
    assert b3_model["paired_against_B2"]["pooled"]["mean"] < 0


def test_hockey_only_refuses_held_out_seasons() -> None:
    with pytest.raises(ValueError, match="held out"):
        hockey_only(LEAGUE.games, [20222023], feature_tables(LEAGUE.games, 4), LEAGUE)
