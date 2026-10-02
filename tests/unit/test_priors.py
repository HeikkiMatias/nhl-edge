"""Priors (#102, ADR 0020): league lines, NHLe factors and inputs, traits, age curves, and the
season-start fit inside RAPM."""

from datetime import UTC, date, datetime

import numpy as np
import polars as pl
import pytest
import rapm_fixtures as fx

from nhl_edge.audit import rapm as report
from nhl_edge.ratings import priors as pr
from nhl_edge.ratings import rapm

CUTOFF = datetime(2016, 10, 1, tzinfo=UTC)
SEASON = 20162017


def _lines(rows: list[tuple[int, int, str, str, int, int]], **extra: object) -> pl.DataFrame:
    """player_league_seasons rows (player, season, abbrev, league, games, points), regular
    season, public on July 1 after the season."""
    frame = pl.DataFrame(
        rows,
        schema=["player_id", "season", "league_abbrev", "league", "games_played", "points"],
        orient="row",
    )
    return frame.with_columns(
        pl.col("season").cast(pl.Int32),
        game_type=pl.lit(2, pl.Int8),
        games_played=pl.col("games_played").cast(pl.Int16),
        goals=(pl.col("points") // 2).cast(pl.Int16),
        assists=(pl.col("points") - pl.col("points") // 2).cast(pl.Int16),
        observed_utc=pl.datetime(pl.col("season") // 10_000 + 1, 7, 1, 10, time_zone="UTC"),
        **{k: pl.lit(v) for k, v in extra.items()},
    ).drop("points")


def test_league_lines_sum_splits_and_keep_one_name_per_league() -> None:
    lines = _lines(
        [
            (1, 20142015, "OHL", "OHL", 30, 20),
            (1, 20142015, "OHL", "OHL", 25, 10),  # traded: two teams
            (2, 20142015, "WJC-A", "WJC-20", 20, 8),
            (2, 20142015, "WJC-20", "WJC-20", 20, 8),  # one league, two names
            (3, 20142015, "AHL", "AHL", 70, 50),
        ]
    )
    playoffs = _lines([(3, 20142015, "AHL", "AHL", 20, 30)]).with_columns(
        game_type=pl.lit(3, pl.Int8)
    )
    out = pr.league_lines(pl.concat([lines, playoffs]), CUTOFF)
    got = {
        (r["player_id"], r["league"]): (r["games"], r["points"]) for r in out.iter_rows(named=True)
    }
    assert got == {(1, "OHL"): (55, 30), (2, "WJC-20"): (20, 8), (3, "AHL"): (70, 50)}


def test_league_lines_read_only_lines_public_before_the_cutoff() -> None:
    lines = _lines([(1, 20152016, "AHL", "AHL", 70, 50), (2, 20152016, "AHL", "AHL", 70, 50)])
    lines = lines.with_columns(
        observed_utc=pl.when(pl.col("player_id") == 2)
        .then(pl.lit(CUTOFF))
        .otherwise(pl.col("observed_utc"))
    )
    assert pr.league_lines(lines, CUTOFF)["player_id"].to_list() == [1]


def _moves(league: str, count: int, first: int, league_ppg: float, nhl_ppg: float) -> list:
    rows = []
    for k in range(count):
        player = hash((league, first, k)) % 10**8
        rows.append((player, 20142015, league, league, 50, round(league_ppg * 50)))
        rows.append((player, 20152016, "NHL", "NHL", 40, round(nhl_ppg * 40)))
    return rows


def test_nhle_factors_are_ratios_of_sums_with_small_leagues_pooled() -> None:
    rows = _moves("AHL", 40, 0, 1.0, 0.5) + _moves("SHL", 10, 1, 0.5, 0.4)
    rows += _moves("NL", 25, 2, 0.5, 0.3)
    # A tournament line never reaches MIN_GAMES, so it makes no move.
    rows += [(77, 20142015, "WC", "WC", 10, 10), (77, 20152016, "NHL", "NHL", 40, 40)]
    factors = pr.nhle_factors(pr.league_lines(_lines(rows), CUTOFF), SEASON)
    got = {r["league"]: (r["moves"], r["factor"]) for r in factors.iter_rows(named=True)}
    assert got["AHL"] == (40, pytest.approx(0.5))
    # SHL and NL pool: (10·16 + 25·12) NHL points over 35·40 games, against 35·25 points over
    # 35·50 games.
    assert got[pr.POOLED][0] == 35
    assert got[pr.POOLED][1] == pytest.approx((160 + 300) / 1400 / (875 / 1750))
    assert set(got) == {"AHL", pr.POOLED}


def test_nhle_factors_read_only_the_window_before_the_season() -> None:
    rows = _moves("AHL", 40, 0, 1.0, 0.5)
    old = [(p, s - 10_001 * pr.NHLE_WINDOW, a, lg, g, pt) for p, s, a, lg, g, pt in rows]
    factors = pr.nhle_factors(pr.league_lines(_lines(old), CUTOFF), SEASON)
    assert factors.is_empty()


def test_nhle_inputs_weigh_the_last_two_seasons_by_games() -> None:
    factors = pl.DataFrame({"league": ["AHL", pr.POOLED], "moves": [40, 30], "factor": [0.5, 0.8]})
    lines = pr.league_lines(
        _lines(
            [
                (1, 20152016, "AHL", "AHL", 60, 60),
                (1, 20142015, "LIIGA", "LIIGA", 40, 20),  # pooled
                (1, 20132014, "AHL", "AHL", 70, 70),  # three seasons back: not read
                (2, 20152016, "AHL", "AHL", 10, 10),  # too few games
                (3, 20152016, "NHL", "NHL", 82, 50),
            ]
        ),
        CUTOFF,
    )
    got = pr.nhle_inputs(lines, factors, SEASON)
    assert got["player_id"].to_list() == [1]
    assert got["nhle_ppg"][0] == pytest.approx((60 * 0.5 + 20 * 0.8) / 100)


def test_traits_centre_on_the_reference_skater() -> None:
    players = pl.DataFrame(
        {
            "player_id": [1, 2, 3],
            "birth_date": [date(1989, 10, 1), date(1996, 4, 1), None],
            "draft_overall": [60, 1, None],
        },
        schema={"player_id": pl.Int64, "birth_date": pl.Date, "draft_overall": pl.Int16},
    )
    nhle = pl.DataFrame({"player_id": [2], "nhle_ppg": [0.5]})
    got = {r["player_id"]: r for r in pr.traits(players, nhle, SEASON).iter_rows(named=True)}
    reference = got[1]
    assert reference["age"] == pytest.approx(0.0, abs=0.01)
    assert reference["slot"] == 0 and reference["undrafted"] == 0
    assert reference["nhle"] == 0 and reference["has_nhle"] == 0
    young = got[2]
    assert young["age"] == pytest.approx(20.5 - 27, abs=0.01)
    assert young["age2"] == pytest.approx(young["age"] ** 2)
    assert young["slot"] == pytest.approx(np.log(1 / 60))
    assert young["nhle"] == pytest.approx(0.5 - pr.REFERENCE_PPG) and young["has_nhle"] == 1
    unknown = got[3]
    assert unknown["age"] == 0 and unknown["slot"] == 0 and unknown["undrafted"] == 1


def _season_ends(change: float, slope: float) -> pl.DataFrame:
    """Season-end ratings whose change to the next season is change + slope * (age - 27)."""
    rows = []
    for player in range(40):
        age = -6 + 0.3 * player
        mean = 0.0
        for k, season in enumerate((20122013, 20132014, 20142015)):
            rows.append((season, player, "ev_off", mean, 15.0, age + k))
            mean += change + slope * (age + k + 1)
    return pl.DataFrame(
        rows,
        schema=["season", "player_id", "component", "mean", "hours", "age"],
        orient="row",
    ).with_columns(pl.col("season").cast(pl.Int32))


def test_age_curves_find_the_change_by_age() -> None:
    curves = pr.age_curves(_season_ends(0.01, -0.004), 20152016)
    curve = curves["ev_off"]
    np.testing.assert_allclose(curve.coefficients, (0.01, -0.004, 0.0), atol=1e-9)
    assert curve.pairs == 80
    assert curve.change(30) == pytest.approx(0.01 - 0.004 * 3)


def test_age_curves_read_only_seasons_that_ended_before() -> None:
    ends = _season_ends(0.01, -0.004)
    assert pr.age_curves(ends, 20132014) == {}
    assert pr.age_curves(ends, 20142015)["ev_off"].pairs == 40


def test_age_curves_skip_players_with_little_ice_time() -> None:
    ends = _season_ends(0.01, -0.004).with_columns(
        hours=pl.when(pl.col("player_id") < 39).then(1.0).otherwise(pl.col("hours"))
    )
    assert pr.age_curves(ends, 20152016) == {}


def test_prior_means_are_traits_times_effects() -> None:
    season_traits = pl.DataFrame(
        {
            "player_id": [1],
            "age": [2.0],
            "age2": [4.0],
            "slot": [-1.0],
            "undrafted": [0.0],
            "nhle": [0.2],
            "has_nhle": [1.0],
        }
    )
    effects = {"ev_off": {"age": 0.1, "slot": -0.3, "nhle": 0.5, "has_nhle": -0.05}}
    got = pr.prior_means(season_traits, effects).row(0, named=True)
    assert got["ev_off"] == pytest.approx(0.2 + 0.3 + 0.1 - 0.05)
    assert got["ev_def"] == 0 and got["pp"] == 0 and got["pk"] == 0


LEAGUE = fx.league()
GAMES, STINTS, ROLES, VENUES = (LEAGUE[k] for k in ("games", "stints", "roles", "venues"))
LOOSE = rapm.Settings(half_life_days=180.0, pull_hours=0.5)
WANTED = rapm.targets(LEAGUE["lineups"], GAMES, fx.SEASONS)
RATINGS, TERMS, FITS = rapm.rate(
    fx.seasons_of(STINTS),
    GAMES,
    ROLES,
    VENUES,
    WANTED,
    LOOSE,
    "rapm-20261002-abc1234",
    players=fx.players(),
    league_seasons=fx.league_seasons(),
)


def test_the_first_season_starts_without_effects() -> None:
    first = FITS[0]
    assert first.season == fx.SEASONS[0] and first.effects == {}
    # Only a defenseman's power-play prior moves: it carries his role's average there.
    opening = RATINGS.filter(pl.col("season") == fx.SEASONS[0], pl.col("component") != "pp")
    assert (opening["prior"] == 0).all()


def test_the_season_start_fit_finds_the_draft_effect() -> None:
    # In the fixture, earlier picks have better offense: a later pick lowers the prior.
    effects = FITS[1].effects
    assert effects["ev_off"]["slot"] < 0
    assert FITS[1].factors.to_dicts() == [
        {"league": "AHL", "moves": fx.HISTORY_PLAYERS, "factor": pytest.approx(fx.AHL_FACTOR)}
    ]


def test_a_player_without_data_is_at_his_prior() -> None:
    late = RATINGS.filter(pl.col("player_id") == fx.LATE_PLAYER).sort("as_of_utc")
    opening = late.filter(pl.col("game_date") == late["game_date"].min())
    assert (opening["hours"] == 0).all()
    np.testing.assert_allclose(opening["mean"].to_numpy(), opening["prior"].to_numpy())
    # The first pick's offense prior is above average.
    assert opening.filter(pl.col("component") == "ev_off")["prior"][0] > 0


def test_the_terms_carry_the_season_s_effects() -> None:
    second = TERMS.filter(pl.col("season") == fx.SEASONS[1])
    ev = second.filter(pl.col("model") == rapm.EV, pl.col("term") == "prior:ev_off:slot")
    assert ev["value"].unique().to_list() == [pytest.approx(FITS[1].effects["ev_off"]["slot"])]
    pp_in_ev = second.filter(pl.col("model") == rapm.EV, pl.col("term").str.starts_with("prior:pp"))
    assert pp_in_ev.is_empty()


def test_ratings_with_priors_still_find_the_true_ones() -> None:
    last = RATINGS.filter(
        pl.col("game_date") == RATINGS["game_date"].max(), pl.col("component") == "ev_off"
    )
    true = np.array([fx.truth()[p]["ev_off"] for p in last["player_id"]])
    assert np.corrcoef(last["mean"].to_numpy(), true)[0, 1] > 0.95


def test_the_report_shows_priors_only_for_seasons_that_read_shown_seasons() -> None:
    players = pl.DataFrame({"player_id": [100], "name": ["A Forward"]})
    settings = LOOSE
    shown_all = report.markdown_report(
        RATINGS, TERMS, players, list(fx.SEASONS), "rapm-20261002-abc1234", settings, FITS
    )
    assert f"| {fx.SEASONS[1]} | ev_off |" in shown_all
    # With the first season hidden, the second reads it through the decay and its own stints'
    # fit: it shows only its counts, and none of its priors.
    hidden_first = report.markdown_report(
        RATINGS, TERMS, players, [fx.SEASONS[1]], "rapm-20261002-abc1234", settings, FITS
    )
    (row,) = [line for line in hidden_first.splitlines() if line.startswith(f"| {fx.SEASONS[1]} |")]
    assert "held out" in row
    assert "## Priors" not in hidden_first
