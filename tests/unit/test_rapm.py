"""RAPM (#101, ADR 0019): rows, design, the decayed weighted sums, and the ratings it gives."""

from datetime import date

import numpy as np
import polars as pl
import pytest
import rapm_fixtures as fx
import scipy.sparse as sp

from nhl_edge.lake.schemas import PlayerRatings, RapmTerms, dtypes
from nhl_edge.ratings import rapm

LEAGUE = fx.league()
GAMES, STINTS, ROLES, VENUES = (LEAGUE[k] for k in ("games", "stints", "roles", "venues"))
VERSION = "rapm-20261002-abc1234"
# A small pull, so the ratings come close to the truth.
LOOSE = rapm.Settings(half_life_days=180.0, pull_hours=0.5)
WANTED = rapm.targets(LEAGUE["lineups"], GAMES, fx.SEASONS)
RATINGS, TERMS = rapm.rate(fx.seasons_of(STINTS), GAMES, ROLES, VENUES, WANTED, LOOSE, VERSION)
LAST_DAY = RATINGS["game_date"].max()
TRUTH = fx.truth()


def _one_stint(**changes: object) -> pl.DataFrame:
    not_neutral = GAMES.filter(~pl.col("neutral_site"))["game_id"].implode()
    first = STINTS.filter(pl.col("strength") == "5v5", pl.col("game_id").is_in(not_neutral))
    first = first.head(1)
    return first.with_columns(**{k: pl.lit(v, dtype=first.schema[k]) for k, v in changes.items()})


def test_a_5v5_stint_gives_one_row_per_team_attacking() -> None:
    stint = _one_stint(score_state=1, zone_start="O")
    rows = rapm.model_rows(stint, GAMES, ROLES, VENUES, rapm.EV)
    assert rows.height == 2
    home, away = rows.row(0, named=True), rows.row(1, named=True)
    assert home["attackers"] == stint["home_skaters"][0].to_list()
    assert away["attackers"] == stint["away_skaters"][0].to_list()
    assert (home["score"], away["score"]) == (1, -1)
    assert (home["zone"], away["zone"]) == ("O", "D")
    assert home["home"] and not away["home"]
    hours = stint["seconds"][0] / 3600
    assert home["hours"] == pytest.approx(hours)
    assert home["y"] == pytest.approx(stint["home_xg"][0] / hours)
    assert away["y"] == pytest.approx(stint["away_xg"][0] / hours)


def test_the_score_is_capped_at_two() -> None:
    rows = rapm.model_rows(_one_stint(score_state=4), GAMES, ROLES, VENUES, rapm.EV)
    assert rows["score"].to_list() == [2, -2]


def test_a_neutral_site_gives_no_home_team() -> None:
    neutral = GAMES.filter(pl.col("neutral_site"))["game_id"]
    stints = STINTS.filter(pl.col("game_id").is_in(neutral.implode()))
    assert stints.height
    assert not rapm.model_rows(stints, GAMES, ROLES, VENUES, rapm.EV)["home"].any()


@pytest.mark.parametrize(
    "change",
    [
        {"home_goalie": None},
        {"away_goalie": None},
        {"drop_reason": "skaters"},
        {"home_xg": None},
        {"strength": "4v4"},
        {"strength": "6v5"},
    ],
)
def test_stints_rapm_leaves_out(change: dict[str, object]) -> None:
    stint = _one_stint(**change)
    for model in rapm.MODELS:
        assert rapm.model_rows(stint, GAMES, ROLES, VENUES, model).is_empty()


def test_power_plays_come_from_either_side() -> None:
    home_pp = _one_stint(strength="5v3")
    away_pp = _one_stint(strength="4v5", score_state=1, zone_start="D")
    rows = rapm.model_rows(pl.concat([home_pp, away_pp]), GAMES, ROLES, VENUES, rapm.PP)
    assert rows["situation"].to_list() == ["5v3", "5v4"]
    away = rows.row(1, named=True)
    assert away["attackers"] == away_pp["away_skaters"][0].to_list()
    assert (away["score"], away["zone"], away["home"]) == (-1, "O", False)
    assert rows["y"][1] == pytest.approx(away_pp["away_xg"][0] / (away_pp["seconds"][0] / 3600))


def test_the_power_play_counts_its_defensemen() -> None:
    rows = rapm.model_rows(STINTS, GAMES, ROLES, VENUES, rapm.PP)
    assert set(rows["attack_d"].unique().to_list()) == {1, 2}
    role = dict(zip(ROLES["player_id"], ROLES["role"], strict=False))
    first = rows.row(0, named=True)
    assert first["attack_d"] == sum(role[p] == "D" for p in first["attackers"])


def test_the_design_puts_each_term_and_player_in_its_column() -> None:
    design = rapm.Design(rapm.EV, list(fx.SEASONS), sorted(fx.ARENAS.values()))
    stint = _one_stint(score_state=-1, zone_start="N")
    rows = rapm.model_rows(stint, GAMES, ROLES, VENUES, rapm.EV)
    x = design.matrix(rows).toarray()
    home = x[0]
    named = {t: home[i] for i, t in enumerate(design.terms) if home[i]}
    game = GAMES.filter(pl.col("game_id") == stint["game_id"][0]).row(0, named=True)
    arena = fx.ARENAS[game["home"]]
    assert named == {
        "intercept": 1,
        "home": 1,
        "score:-1": 1,
        "zone:N": 1,
        f"season:{fx.SEASONS[0]}": 1,
        f"arena:{arena}": 1,
    }
    k = design.positions(np.array(rows["attackers"][0].to_list()), add=False)
    j = design.positions(np.array(rows["defenders"][0].to_list()), add=False)
    assert (home[design.attack(k)] == 1).all() and (home[design.defend(j)] == -1).all()
    assert np.count_nonzero(home) == len(named) + 10
    assert x[1][design.index["score:+1"]] == 1 and x[1][design.index["zone:N"]] == 1


def test_the_power_play_design_has_its_situation_and_defensemen() -> None:
    design = rapm.Design(rapm.PP, list(fx.SEASONS), [])
    rows = rapm.model_rows(STINTS, GAMES, ROLES, VENUES, rapm.PP)
    x = design.matrix(rows)
    five_three = (rows["situation"] == "5v3").to_numpy()
    column = x[:, design.index["situation:5v3"]].toarray().ravel()
    assert (column == five_three).all()
    counts = x[:, design.index[rapm.DEFENSEMEN]].toarray().ravel()
    assert (counts == rows["attack_d"].to_numpy()).all()
    assert not any(t.startswith("arena:") for t in design.terms)


def _direct(
    x: np.ndarray,
    hours: np.ndarray,
    y: np.ndarray,
    days: np.ndarray,
    half_life: float,
    pull: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """The same ridge fit from scratch, with each row's decay written out."""
    w = hours * 0.5 ** ((days.max() - days) / half_life)
    a = x.T @ (w[:, None] * x)
    m = a + np.diag(pull + rapm.FLOOR)
    beta = np.linalg.solve(m, x.T @ (w * y))
    rss = w @ (y - x @ beta) ** 2
    count = (0.5 ** ((days.max() - days) / half_life)).sum()
    return beta, np.sqrt(rss / count * np.diag(np.linalg.inv(m)))


@pytest.mark.parametrize("half_life", [3.0, 0.01])
def test_the_running_sums_give_the_direct_fit(half_life: float) -> None:
    # A half-life of 0.01 days rebases the sums along the way.
    rng = np.random.default_rng(1)
    n, p = 400, 6
    x = rng.normal(size=(n, p))
    hours = rng.uniform(0.01, 0.03, n)
    y = x @ rng.normal(size=p) + rng.normal(0, 0.1, n)
    days = np.repeat(np.arange(8.0), n // 8)
    pull = np.array([0.0, 0.0, 0.5, 0.5, 0.5, 0.5])
    normal = rapm.Normal(half_life)
    for day in range(8):
        part = days == day
        normal.add(sp.csr_matrix(x[part]), hours[part], y[part], days[part])
    solution = normal.solve(pull, np.arange(2, p))
    beta, sd = _direct(x, hours, y, days, half_life, pull)
    np.testing.assert_allclose(solution.beta, beta, rtol=1e-6, atol=1e-9)
    np.testing.assert_allclose(solution.sd[2:], sd[2:], rtol=1e-6)
    assert np.isnan(solution.sd[:2]).all()


def test_a_fit_without_data_keeps_the_prior() -> None:
    solution = rapm.Normal(10.0).solve(np.ones(4), np.arange(4))
    assert (solution.beta == 0).all() and solution.sigma is None


def test_league_days_skip_the_summer() -> None:
    games = pl.DataFrame({"game_date": [date(2012, 4, 7), date(2012, 10, 10), date(2012, 4, 5)]})
    assert rapm.league_days(games) == {
        date(2012, 4, 5): 0,
        date(2012, 4, 7): 1,
        date(2012, 10, 10): 2,
    }


def _latest(component: str) -> tuple[np.ndarray, np.ndarray]:
    rows = RATINGS.filter(pl.col("game_date") == LAST_DAY, pl.col("component") == component)
    return rows["mean"].to_numpy(), np.array([TRUTH[p][component] for p in rows["player_id"]])


@pytest.mark.parametrize(
    ("component", "least"), [("ev_off", 0.95), ("ev_def", 0.95), ("pk", 0.9), ("pp", 0.7)]
)
def test_the_ratings_find_the_true_ones(component: str, least: float) -> None:
    estimate, true = _latest(component)
    assert np.corrcoef(estimate, true)[0, 1] > least


def test_the_terms_find_the_true_ones() -> None:
    last = TERMS.filter(pl.col("game_date") == LAST_DAY)
    value = {(m, t): v for m, t, v in last.select("model", "term", "value").iter_rows()}
    assert value[(rapm.EV, "home")] == pytest.approx(fx.HOME_EDGE, abs=0.05)
    assert value[(rapm.PP, "situation:5v3")] == pytest.approx(fx.FIVE_ON_THREE, abs=0.3)
    assert value[(rapm.PP, rapm.DEFENSEMEN)] == pytest.approx(fx.PER_DEFENSEMAN, abs=0.3)
    assert value[(rapm.EV, rapm.SIGMA)] > 0


def test_a_defenseman_s_power_play_rating_adds_his_role_s_average() -> None:
    last = RATINGS.filter(pl.col("game_date") == LAST_DAY, pl.col("component") == "pp")
    terms = TERMS.filter(pl.col("game_date") == LAST_DAY, pl.col("term") == rapm.DEFENSEMEN)
    shift = terms["value"][0]
    d = last.filter(pl.col("role") == "D")
    f = last.filter(pl.col("role") == "F")
    true_d = np.mean([TRUTH[p]["pp"] for p in d["player_id"]])
    true_f = np.mean([TRUTH[p]["pp"] for p in f["player_id"]])
    # Ratings are relative to a forward rated 0, so the role gap carries the term.
    gap = d["mean"].mean() - f["mean"].mean()  # type: ignore[operator]
    assert gap == pytest.approx(shift + true_d - true_f, abs=0.3)


def test_a_player_without_data_is_at_the_prior() -> None:
    first = RATINGS.filter(
        pl.col("player_id") == fx.LATE_PLAYER, pl.col("season") == fx.SEASONS[1]
    ).sort("as_of_utc")
    opening = first.filter(pl.col("game_date") == first["game_date"].min())
    assert (opening["mean"] == 0).all() and (opening["hours"] == 0).all()
    sigma = TERMS.filter(
        pl.col("game_date") == opening["game_date"][0], pl.col("term") == rapm.SIGMA
    )
    ev_sigma = sigma.filter(pl.col("model") == rapm.EV)["value"][0]
    ev = opening.filter(pl.col("component") == "ev_off")
    assert ev["sd"][0] == pytest.approx(ev_sigma / np.sqrt(LOOSE.pull_hours))
    later = first.filter(pl.col("game_date") == LAST_DAY, pl.col("component") == "ev_off")
    assert later["hours"][0] > 0 and later["mean"][0] > 0.2


def test_the_first_night_has_no_data_at_all() -> None:
    opening = RATINGS.filter(pl.col("game_date") == RATINGS["game_date"].min())
    assert (opening["mean"] == 0).all()
    assert opening["sd"].is_null().all() and opening["known_utc"].is_null().all()
    assert TERMS["game_date"].min() > RATINGS["game_date"].min()  # type: ignore[operator]


def test_every_candidate_gets_four_components() -> None:
    assert RATINGS.height == 4 * WANTED.height
    counts = RATINGS.group_by("game_id", "player_id").len()["len"]
    assert (counts == 4).all()
    assert set(RATINGS["component"].unique()) == {"ev_off", "ev_def", "pp", "pk"}


def test_the_tables_have_their_schemas() -> None:
    assert list(RATINGS.columns) == list(dtypes(PlayerRatings))
    assert list(TERMS.columns) == list(dtypes(RapmTerms))
    PlayerRatings.validate(RATINGS)
    RapmTerms.validate(TERMS)
    assert (RATINGS["artifact_version"] == VERSION).all()
    assert (RATINGS["train_cutoff"] == rapm.TRAIN_CUTOFF).all()


def test_seasons_out_of_order_are_refused() -> None:
    with pytest.raises(ValueError, match="after those of"):
        rapm.rate(
            list(reversed(fx.seasons_of(STINTS))), GAMES, ROLES, VENUES, WANTED, LOOSE, VERSION
        )


def test_a_wanted_season_without_stints_is_refused() -> None:
    with pytest.raises(ValueError, match="no stints"):
        rapm.rate(fx.seasons_of(STINTS)[:1], GAMES, ROLES, VENUES, WANTED, LOOSE, VERSION)


def test_an_arena_without_a_column_is_refused() -> None:
    design = rapm.Design(rapm.EV, list(fx.SEASONS), ["td_garden"])
    rows = rapm.model_rows(STINTS.head(200), GAMES, ROLES, VENUES, rapm.EV)
    with pytest.raises(ValueError, match="no column"):
        design.matrix(rows)
