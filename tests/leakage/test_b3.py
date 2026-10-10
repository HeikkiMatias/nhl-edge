"""Point-in-time rules for B3 (#106, ADR 0023, hard rules 1 and 9). A game's prediction reads its
projected lineups, ratings, league rates, power plays, multipliers and schedule terms only once
known, mixes over the goalie-start probabilities, and never reads its own result, lineup or
starters. Each fold's fit reads only games whose results, boxscores and rows were public before
the fold starts, and a fold before the tuning cutoff is refused."""

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest
from b2_fixtures import feature_tables
from b3_fixtures import league
from polars.testing import assert_frame_equal

from nhl_edge.backtest.seasons import HOCKEY_ROLES, ONE_TIME_SEASONS, season_role
from nhl_edge.backtest.walk_forward import HOCKEY, fold_start, hockey_only
from nhl_edge.features import team_strength as ts
from nhl_edge.game import b2, b3

LEAGUE = league()
TEST = 20182019
START = fold_start(LEAGUE.games.select("season", "start_utc"), TEST)
MOMENTS = LEAGUE.games.filter(pl.col("season") == TEST).select(
    "game_id", prediction_utc="start_utc"
)
# The per-game tables a prediction reads, each by game_id.
PER_GAME = (
    "schedule_terms",
    "lineups",
    "lineup_replacements",
    "player_ratings",
    "expected_power_plays",
    "goal_multipliers",
)
UTC_TYPE = pl.Datetime("us", "UTC")


def replaced(**frames: pl.DataFrame) -> b3.Tables:
    return b3.Tables(**{**LEAGUE.__dict__, **frames})


def predict(tables: b3.Tables = LEAGUE) -> pl.DataFrame:
    predicted, _ = b3.predictions(tables, MOMENTS, TEST, START)
    return predicted.sort("game_id")


BEFORE = predict()


def same(left: pl.DataFrame, right: pl.DataFrame) -> bool:
    try:
        assert_frame_equal(left, right, rel_tol=1e-12, abs_tol=1e-12)
    except AssertionError:
        return False
    return True


def retimed(table: pl.DataFrame, which: pl.Expr, moment: object) -> pl.DataFrame:
    return table.with_columns(
        observed_utc=pl.when(which).then(pl.lit(moment, UTC_TYPE)).otherwise(pl.col("observed_utc"))
    )


def test_a_games_own_result_lineup_and_starters_never_move_its_prediction() -> None:
    tested = pl.col("game_id").is_in(MOMENTS["game_id"].implode())
    games = LEAGUE.games.with_columns(
        home_score=pl.when(tested).then(pl.col("away_score")).otherwise(pl.col("home_score")),
        away_score=pl.when(tested).then(pl.col("home_score")).otherwise(pl.col("away_score")),
    )
    lineups = LEAGUE.actual_lineups.with_columns(
        starting_goalie=pl.when(tested)
        .then(~pl.col("starting_goalie"))
        .otherwise(pl.col("starting_goalie"))
    )
    assert same(predict(replaced(games=games, actual_lineups=lineups)), BEFORE)
    # Without its own boxscore at all, the same.
    unplayed = LEAGUE.actual_lineups.filter(~tested)
    assert same(predict(replaced(actual_lineups=unplayed)), BEFORE)


def test_earlier_results_do_move_the_fit() -> None:
    # The guard above is not vacuous.
    earlier = pl.col("season") < TEST
    games = LEAGUE.games.with_columns(
        home_score=pl.when(earlier).then(pl.col("away_score")).otherwise(pl.col("home_score")),
        away_score=pl.when(earlier).then(pl.col("home_score")).otherwise(pl.col("away_score")),
    )
    assert not same(predict(replaced(games=games)), BEFORE)


@pytest.mark.parametrize("name", PER_GAME)
def test_a_row_known_only_at_the_prediction_time_is_not_read(name: str) -> None:
    game = MOMENTS["game_id"][0]
    moment = MOMENTS.filter(pl.col("game_id") == game)["prediction_utc"].item()
    table = getattr(LEAGUE, name)
    late = retimed(table, pl.col("game_id") == game, moment)
    predicted = predict(replaced(**{name: late}))
    assert game not in predicted["game_id"].to_list()
    assert same(predicted, BEFORE.filter(pl.col("game_id") != game))


def test_one_skaters_rating_known_only_at_the_prediction_time_is_not_read() -> None:
    game = MOMENTS["game_id"][0]
    moment = MOMENTS.filter(pl.col("game_id") == game)["prediction_utc"].item()
    player = LEAGUE.player_ratings.filter(pl.col("game_id") == game)["player_id"][0]
    which = (pl.col("game_id") == game) & (pl.col("player_id") == player)
    predicted = predict(replaced(player_ratings=retimed(LEAGUE.player_ratings, which, moment)))
    assert game not in predicted["game_id"].to_list()


def test_league_rates_known_only_at_the_prediction_time_are_not_read() -> None:
    game = MOMENTS["game_id"][0]
    day = LEAGUE.games.filter(pl.col("game_id") == game)["game_date"].item()
    moment = MOMENTS.filter(pl.col("game_id") == game)["prediction_utc"].item()
    late = retimed(LEAGUE.rapm_terms, pl.col("game_date") == day, moment)
    on_day = LEAGUE.games.filter(pl.col("game_date") == day)["game_id"]
    predicted = predict(replaced(rapm_terms=late))
    assert not predicted["game_id"].is_in(on_day.implode()).any()
    assert same(predicted, BEFORE.filter(~pl.col("game_id").is_in(on_day.implode())))


def test_a_goalie_start_row_known_only_at_the_prediction_time_is_not_read() -> None:
    game = MOMENTS["game_id"][0]
    moment = MOMENTS.filter(pl.col("game_id") == game)["prediction_utc"].item()
    late = retimed(LEAGUE.goalie_starts, pl.col("game_id") == game, moment)
    dropped = LEAGUE.goalie_starts.filter(pl.col("game_id") != game)
    assert same(predict(replaced(goalie_starts=late)), predict(replaced(goalie_starts=dropped)))
    # Without candidates, both goalies count as average: the prediction moves.
    assert not same(predict(replaced(goalie_starts=dropped)), BEFORE)


def coefficients(tables: b3.Tables) -> np.ndarray:
    _, model = b3.predictions(tables, MOMENTS, TEST, START)
    return np.array([model.intercept, *model.weights])


LAST = LEAGUE.games.filter(pl.col("season") == TEST - 10001)["game_id"].max()
IS_LAST = pl.col("game_id") == LAST


def test_an_earlier_result_published_after_the_fold_start_is_not_fitted_on() -> None:
    published_late = LEAGUE.games.with_columns(
        observed_utc=pl.when(IS_LAST)
        .then(pl.lit(START + timedelta(hours=1)))
        .otherwise(pl.col("observed_utc")),
        home_score=pl.when(IS_LAST).then(pl.lit(9, pl.Int16)).otherwise(pl.col("home_score")),
    )
    np.testing.assert_allclose(
        coefficients(replaced(games=published_late)),
        coefficients(replaced(games=LEAGUE.games.filter(~IS_LAST))),
        atol=1e-9,
    )


def test_an_earlier_boxscore_published_after_the_fold_start_is_not_fitted_on() -> None:
    lineups = retimed(LEAGUE.actual_lineups, IS_LAST, START + timedelta(hours=1))
    np.testing.assert_allclose(
        coefficients(replaced(actual_lineups=lineups)),
        coefficients(replaced(games=LEAGUE.games.filter(~IS_LAST))),
        atol=1e-9,
    )


@pytest.mark.parametrize("name", PER_GAME)
def test_an_earlier_row_published_after_the_fold_start_leaves_its_game_out(name: str) -> None:
    late = retimed(getattr(LEAGUE, name), IS_LAST, START + timedelta(days=30))
    np.testing.assert_allclose(
        coefficients(replaced(**{name: late})),
        coefficients(replaced(games=LEAGUE.games.filter(~IS_LAST))),
        atol=1e-9,
    )


def test_a_fold_before_a_tuning_cutoff_is_refused() -> None:
    assert (BEFORE["train_cutoff"] < START).all()
    assert (BEFORE["train_cutoff"] >= b3.TUNED_CUTOFF).all()
    # A table whose own-season rows were retuned on results after the fold start moves the cutoff
    # past it.
    for name in b3.CUTOFF_TABLES:
        table = getattr(LEAGUE, name)
        retuned = table.with_columns(
            train_cutoff=pl.when(pl.col("season") == TEST)
            .then(pl.lit(START + timedelta(days=1), UTC_TYPE))
            .otherwise(pl.lit(b3.TUNED_CUTOFF, UTC_TYPE))
        )
        with pytest.raises(ValueError, match="before the tuning cutoff"):
            b3.predictions(replaced(**{name: retuned}), MOMENTS, TEST, START)
    early = league((20152016, 20162017), games=40)
    start = fold_start(early.games.select("season", "start_utc"), 20162017)
    moments = early.games.filter(pl.col("season") == 20162017).select(
        "game_id", prediction_utc="start_utc"
    )
    with pytest.raises(ValueError, match="before the tuning cutoff"):
        b3.predictions(early, moments, 20162017, start)


def test_the_fits_cutoff_is_the_last_row_it_read() -> None:
    # An earlier game's boxscore public a day before the fold start, after the tuning cutoff: the
    # fit reads it, so its train_cutoff is that moment, not the last result.
    moment = START - timedelta(days=1)
    lineups = retimed(LEAGUE.actual_lineups, IS_LAST, moment)
    _, model = b3.predictions(replaced(actual_lineups=lineups), MOMENTS, TEST, START)
    assert model.train_cutoff == moment > b3.TUNED_CUTOFF
    # Likewise for a projection row.
    projection = retimed(LEAGUE.lineups, IS_LAST, moment)
    _, model = b3.predictions(replaced(lineups=projection), MOMENTS, TEST, START)
    assert model.train_cutoff == moment


def test_a_later_seasons_cutoffs_do_not_refuse_the_fold() -> None:
    # Tables refit each season carry the later seasons' cutoffs, which this fold never reads.
    later = {}
    for name in ("lineups", "expected_power_plays", "goal_multipliers"):
        table = getattr(LEAGUE, name)
        copied = table.filter(pl.col("season") == TEST).with_columns(
            pl.col("game_id") + 1_000_000_000,
            season=pl.lit(TEST + 10001, pl.Int32),
            train_cutoff=pl.lit(START + timedelta(days=200), UTC_TYPE),
            observed_utc=pl.lit(START + timedelta(days=200), UTC_TYPE),
        )
        later[name] = pl.concat([table, copied])
    tables = replaced(**later)
    assert b3.tuning_cutoff(tables, TEST) == b3.TUNED_CUTOFF
    assert b3.tuning_cutoff(tables, TEST + 10001) == START + timedelta(days=200)
    assert same(predict(tables), BEFORE)


def hockey_fits(tables: b3.Tables) -> tuple[np.ndarray, np.ndarray]:
    fits2: dict[str, dict[int, b2.B2Model]] = {}
    fits3: dict[str, dict[int, b3.B3Model]] = {}
    hockey_only(tables.games, [TEST], feature_tables(tables.games, 4), tables, fits2, fits3)
    two, three = fits2[HOCKEY][TEST], fits3[HOCKEY][TEST]
    return (
        np.array([two.intercept, *two.weights]),
        np.array([three.intercept, *three.weights]),
    )


def test_hockey_only_fits_on_results_public_before_its_first_prediction() -> None:
    # The last earlier result, published after the first as-of time but before the first puck
    # drop: neither fit reads it.
    first = LEAGUE.games.filter(pl.col("season") == TEST).sort("start_utc").row(0, named=True)
    as_of = LEAGUE.games.filter(pl.col("game_id") == first["game_id"]).select(
        ts.as_of(pl.col("game_date"), pl.col("start_utc"))
    )["game_date"][0]
    moment = as_of + timedelta(hours=1)
    assert moment < first["start_utc"]
    published = LEAGUE.games.with_columns(
        observed_utc=pl.when(IS_LAST).then(pl.lit(moment)).otherwise(pl.col("observed_utc")),
        home_score=pl.when(IS_LAST).then(pl.lit(9, pl.Int16)).otherwise(pl.col("home_score")),
    )
    late = hockey_fits(replaced(games=published))
    left_out = hockey_fits(replaced(games=LEAGUE.games.filter(~IS_LAST)))
    for got, expected in zip(late, left_out, strict=True):
        np.testing.assert_allclose(got, expected, atol=1e-9)


def test_the_predictions_cutoff_covers_every_tables_cutoff() -> None:
    # The season's power plays refit a day before the fold start: later than the fit's last row.
    moment = START - timedelta(days=1)
    refit = LEAGUE.expected_power_plays.with_columns(
        train_cutoff=pl.when(pl.col("season") == TEST)
        .then(pl.lit(moment, UTC_TYPE))
        .otherwise(pl.col("train_cutoff"))
    )
    predicted = predict(replaced(expected_power_plays=refit))
    assert (predicted["train_cutoff"] == moment).all()


def test_a_later_seasons_rows_are_not_read() -> None:
    # Two RAPM fits on a date of a later season would be refused, but this fold never reads them.
    day = LEAGUE.rapm_terms["game_date"].max()
    assert isinstance(day, date)
    later = LEAGUE.rapm_terms.filter(pl.col("game_date") == day).with_columns(
        season=pl.lit(TEST + 10001, pl.Int32),
        game_date=pl.lit(day + timedelta(days=365)),
    )
    tables = replaced(rapm_terms=pl.concat([LEAGUE.rapm_terms, later, later]))
    assert same(predict(tables), BEFORE)


def test_hockey_only_opens_the_hockey_validation_seasons_alone() -> None:
    # Gate 2 opens 2023-24 and 2024-25 for B2 against B3 on outcomes (#107). 2022-23 waits for
    # phase 4 (#66), 2025-26 for the owner's go-ahead, and live seasons for live.
    for season in (20222023, 20252026, 20262027):
        with pytest.raises(ValueError, match="held out"):
            hockey_only(LEAGUE.games, [season], feature_tables(LEAGUE.games, 4), LEAGUE)
    assert {season_role(s) for s in (20232024, 20242025)} <= HOCKEY_ROLES
    assert not {season_role(s) for s in (20222023, 20252026, 20262027)} & HOCKEY_ROLES
    # The one-time test opens 2025-26 alone, and claims nothing for another season.
    assert ONE_TIME_SEASONS == (20252026,)
    claims: list[int] = []
    for season in (20222023, 20232024, 20262027):
        with pytest.raises(ValueError, match="alone"):
            hockey_only(
                LEAGUE.games,
                [season],
                feature_tables(LEAGUE.games, 4),
                LEAGUE,
                one_time=lambda: claims.append(1),
            )
    assert claims == []

    # A refused claim stops the run before anything is scored.
    def refused() -> None:
        raise ValueError("the one-time test already ran: v1")

    with pytest.raises(ValueError, match="already ran"):
        hockey_only(
            LEAGUE.games, [20252026], feature_tables(LEAGUE.games, 4), LEAGUE, one_time=refused
        )


# Policy v2's arena home edge (#223, ADR 0035): each fold's shifts read only the games whose
# results were public before the fold starts.
ARENA = b3.Terms(arena=True)


def arena_league() -> b3.Tables:
    """LEAGUE with each home team at a real arena, and one team winning every home game of the
    seasons before TEST, so the arenas truly differ and the shifts are not all 0."""
    from nhl_edge import reference

    teams = sorted(LEAGUE.games["home"].unique().to_list())
    venues = reference.load_venues().unique("arena_id").sort("arena_id")["venue"].to_list()
    strong = (pl.col("home") == teams[0]) & (pl.col("season") < TEST)
    games = LEAGUE.games.with_columns(
        venue=pl.col("home").replace_strict(dict(zip(teams, venues, strict=False))),
        home_score=pl.when(strong)
        .then(pl.max_horizontal("home_score", "away_score") + 1)
        .otherwise(pl.col("home_score")),
    )
    schedule = games.select(
        "game_id",
        "season",
        "start_utc",
        "home",
        "away",
        "venue",
        neutral_site=pl.lit(False),
        observed_utc=pl.col("start_utc") - timedelta(hours=24),
    )
    return replaced(games=games, schedule=schedule)


ARENAS = arena_league()


def shifts(tables: b3.Tables = ARENAS) -> dict[str, float]:
    _, model = b3.predictions(tables, MOMENTS, TEST, START, terms=ARENA)
    return dict(model.arenas)


def flipped(tables: b3.Tables, which: pl.Expr) -> b3.Tables:
    games = tables.games.with_columns(
        home_score=pl.when(which).then(pl.col("away_score")).otherwise(pl.col("home_score")),
        away_score=pl.when(which).then(pl.col("home_score")).otherwise(pl.col("away_score")),
    )
    return b3.Tables(**{**tables.__dict__, "games": games})


def test_the_arenas_differ_in_the_fixture() -> None:
    # The guards below are not vacuous: the strong home team's arena gets a clear edge.
    found = shifts()
    assert max(found.values()) > 0.1


def test_the_tested_seasons_own_results_never_move_an_arenas_edge() -> None:
    own = flipped(ARENAS, pl.col("season") == TEST)
    assert shifts(own) == pytest.approx(shifts(), rel=1e-12, abs=1e-12)
    before, _ = b3.predictions(ARENAS, MOMENTS, TEST, START, terms=ARENA)
    after, _ = b3.predictions(own, MOMENTS, TEST, START, terms=ARENA)
    assert same(after.sort("game_id"), before.sort("game_id"))


def test_an_earlier_result_published_after_the_fold_start_does_not_shape_an_edge() -> None:
    late_game = ARENAS.games.filter(pl.col("season") == TEST - 10_001)["game_id"][0]
    retimed_games = retimed(ARENAS.games, pl.col("game_id") == late_game, START)
    dropped = ARENAS.games.filter(pl.col("game_id") != late_game)
    assert shifts(replaced_from(ARENAS, games=retimed_games)) == pytest.approx(
        shifts(replaced_from(ARENAS, games=dropped))
    )


def test_earlier_results_do_move_an_arenas_edge() -> None:
    earlier = flipped(ARENAS, pl.col("season") < TEST)
    assert shifts(earlier) != pytest.approx(shifts())


def replaced_from(tables: b3.Tables, **frames: pl.DataFrame) -> b3.Tables:
    return b3.Tables(**{**tables.__dict__, **frames})


def test_a_schedule_row_known_only_at_the_prediction_time_is_not_read_by_the_arena() -> None:
    game = MOMENTS["game_id"][0]
    moment = MOMENTS.filter(pl.col("game_id") == game)["prediction_utc"].item()
    assert ARENAS.schedule is not None
    late = retimed(ARENAS.schedule, pl.col("game_id") == game, moment)
    before, _ = b3.predictions(ARENAS, MOMENTS, TEST, START, terms=ARENA)
    after, _ = b3.predictions(
        replaced_from(ARENAS, schedule=late), MOMENTS, TEST, START, terms=ARENA
    )
    assert game in before["game_id"].to_list() and game not in after["game_id"].to_list()


def test_the_venue_is_read_from_the_schedule_never_from_the_result() -> None:
    # A venue changed only in the games table, published with the result, moves nothing.
    games = ARENAS.games.with_columns(venue=pl.lit("Somewhere Else"))
    before, _ = b3.predictions(ARENAS, MOMENTS, TEST, START, terms=ARENA)
    after, _ = b3.predictions(replaced_from(ARENAS, games=games), MOMENTS, TEST, START, terms=ARENA)
    assert same(after.sort("game_id"), before.sort("game_id"))


# Policy v2's input terms (#224, ADR 0036): each reads only rows known before the prediction.
def term_league() -> b3.Tables:
    strength = feature_tables(LEAGUE.games, 4).team_strength
    epp = LEAGUE.expected_power_plays.with_columns(
        drawn_index=1.0 + (pl.col("game_id") % 5) / 10 + pl.col("is_home").cast(pl.Float64) / 20
    )
    return replaced(team_strength=strength, expected_power_plays=epp)


TERMS = term_league()


@pytest.mark.parametrize(
    ("terms", "table"),
    [(b3.Terms(team=True), "team_strength"), (b3.Terms(pest=True), "expected_power_plays")],
)
def test_an_input_terms_row_known_only_at_the_prediction_time_is_not_read(
    terms: b3.Terms, table: str
) -> None:
    game = MOMENTS["game_id"][0]
    moment = MOMENTS.filter(pl.col("game_id") == game)["prediction_utc"].item()
    late = retimed(getattr(TERMS, table), pl.col("game_id") == game, moment)
    before, _ = b3.predictions(TERMS, MOMENTS, TEST, START, terms=terms)
    after, _ = b3.predictions(
        b3.Tables(**{**TERMS.__dict__, table: late}), MOMENTS, TEST, START, terms=terms
    )
    assert game in before["game_id"].to_list() and game not in after["game_id"].to_list()
    assert same(after.sort("game_id"), before.filter(pl.col("game_id") != game).sort("game_id"))


def test_team_strength_retuned_after_the_fold_start_refuses_the_team_terms_fold() -> None:
    # B2's team strength is a fitted input of Terms(team=True): rows of the fold's season
    # retuned after its start move the tuning cutoff past it (Codex on #227). v1's B3 doesn't
    # read the table, so its fold stands.
    # The fixture's team strength has no season or train_cutoff; the lake's has both.
    strength = TERMS.team_strength.join(LEAGUE.games.select("game_id", "season"), on="game_id")
    tuned = strength.with_columns(train_cutoff=pl.lit(b3.TUNED_CUTOFF, UTC_TYPE))
    retuned = strength.with_columns(
        train_cutoff=pl.when(pl.col("season") == TEST)
        .then(pl.lit(START + timedelta(days=1), UTC_TYPE))
        .otherwise(pl.lit(b3.TUNED_CUTOFF, UTC_TYPE))
    )
    fine = b3.Tables(**{**TERMS.__dict__, "team_strength": tuned})
    late = b3.Tables(**{**TERMS.__dict__, "team_strength": retuned})
    b3.predictions(fine, MOMENTS, TEST, START, terms=b3.Terms(team=True))
    with pytest.raises(ValueError, match="before the tuning cutoff"):
        b3.predictions(late, MOMENTS, TEST, START, terms=b3.Terms(team=True))
    assert b3.tuning_cutoff(late, TEST, b3.Terms(team=True)) == START + timedelta(days=1)
    assert b3.tuning_cutoff(late, TEST) == b3.tuning_cutoff(TERMS, TEST)
    b3.predictions(late, MOMENTS, TEST, START)


def test_a_coach_counts_only_from_the_morning_after_his_first_game() -> None:
    from datetime import date as day

    games = LEAGUE.games.filter(pl.col("season") == TEST)
    team = games["home"][0]
    takeover = games.filter(pl.col("home") == team)["game_date"].sort()[10]
    lines = {t: t for t in pl.concat([games["home"], games["away"]]).unique().to_list()}
    old = pl.DataFrame({"team": [team], "first_game": [day(2018, 9, 1)]})
    both = pl.concat([old, pl.DataFrame({"team": [team], "first_game": [takeover]})])
    without = b3.coach_flags(games, old, lines)
    with_new = b3.coach_flags(games, both, lines)
    up_to = games.filter(pl.col("game_date") <= takeover).select("game_id")
    # The new coach's stint changes nothing up to and including his own first game.
    assert same(
        with_new.join(up_to, on="game_id").sort("game_id", "team"),
        without.join(up_to, on="game_id").sort("game_id", "team"),
    )
    # From the next morning on, his team's games are flagged.
    after = with_new.join(games.filter(pl.col("game_date") > takeover), on="game_id")
    assert (after.filter(pl.col("team") == team)["new"] == 1.0).any()


def test_a_game_reading_a_new_coach_is_known_no_earlier_than_the_morning_after_his_first_game(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Gated by the stint's public time, not by the calendar day alone (Codex on #227): a
    # prediction before 10:00 UTC the morning after his first game can't read the new stint.
    from datetime import UTC, datetime
    from datetime import date as day

    games = LEAGUE.games.filter(pl.col("season") == TEST)
    team = games["home"][0]
    takeover = games.filter(pl.col("home") == team)["game_date"].sort()[10]
    teams = pl.concat([LEAGUE.games["home"], LEAGUE.games["away"]]).unique().to_list()
    old = pl.DataFrame({"team": teams, "first_game": [day(2010, 9, 1)] * len(teams)})
    coaches = pl.concat([old, pl.DataFrame({"team": [team], "first_game": [takeover]})])
    monkeypatch.setattr(b3.reference, "load_coaches", lambda: coaches)
    monkeypatch.setattr(b3.reference, "lineage", lambda _: {t: t for t in teams})
    monkeypatch.setattr(b3.reference, "load_teams", lambda: pl.DataFrame())
    # Every other input known early, so only the coach's stint can move a game's known time.
    early = datetime(2015, 1, 1, tzinfo=UTC)
    plain = b3.game_inputs(TERMS).with_columns(observed_utc=pl.lit(early, UTC_TYPE))
    inputs = b3.with_terms(TERMS, plain, b3.Terms(coach=True))
    public = datetime.combine(takeover + timedelta(days=1), datetime.min.time(), UTC).replace(
        hour=10
    )
    involved = (pl.col("home") == team) | (pl.col("away") == team)
    later = inputs.filter(pl.col("game_date") > takeover, involved)
    assert later.height and (later["observed_utc"] >= public).all()
    # Before his first game, and for the other teams, the stints are long public.
    before = inputs.filter(~(involved & (pl.col("game_date") > takeover)))
    assert (before["observed_utc"] == early).all()
