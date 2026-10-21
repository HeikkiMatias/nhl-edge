from datetime import date

import numpy as np
import polars as pl
import pytest
from goalie_fixtures import lineup_frame, public_after, start_of
from lineup_fixtures import league, scored_with_minutes
from scipy.optimize import check_grad

from nhl_edge.audit import projection as report
from nhl_edge.lake.schemas import DRESSED_SKATERS, Lineups
from nhl_edge.lineup import goalie_start as gs
from nhl_edge.lineup import projection as pr

LEAGUE = league()
VERSION = "lineup-20261002-abc1234"
GOALIE_VERSION = "goalie-start-20261001-abc1234"


def schedule(*games: tuple[int, int, date, str, str]) -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "game_id": game_id,
                "season": season,
                "game_date": day,
                "start_utc": start_of(day),
                "home": home,
                "away": away,
            }
            for game_id, season, day, home, away in games
        ]
    ).with_columns(pl.col("season").cast(pl.Int32))


def boxscores(*games: tuple[int, int, date, str, tuple[tuple[int, str, int | None], ...]]):
    """Skater rows per team-game: (player, role, time on ice)."""
    rows = [
        {
            "game_id": game_id,
            "season": season,
            "game_date": day,
            "team": team,
            "is_home": True,
            "player_id": player,
            "role": role,
            "sweater_number": player % 100,
            "starting_goalie": False,
            "toi_s": toi,
            "observed_utc": public_after(day),
            "raw_key": f"boxscore/{season}/{game_id}",
        }
        for game_id, season, day, team, skaters in games
        for player, role, toi in skaters
    ]
    return lineup_frame(rows)


# BOS's history: three games in 2011-12. 2 leaves the third early, 3 is listed as a forward in
# it, 4 dresses only in the first and 5 from the second on. The game to rate is the next night.
HISTORY = boxscores(
    (2011020001, 20112012, date(2011, 10, 5), "BOS", ((1, "F", 1000), (2, "F", 900),
                                                      (3, "D", 1200), (4, "F", 800))),
    (2011020002, 20112012, date(2011, 10, 6), "BOS", ((1, "F", 1000), (2, "F", 900),
                                                      (3, "D", 1200), (5, "F", 700))),
    (2011020003, 20112012, date(2011, 10, 8), "BOS", ((1, "F", 1000), (2, "F", 400),
                                                      (3, "F", 1100), (5, "F", 700))),
)  # fmt: skip
TARGET = schedule((2011020004, 20112012, date(2011, 10, 9), "BOS", "BUF"))


def inputs(games: pl.DataFrame, lineups: pl.DataFrame, **lines: str) -> dict[int, dict]:
    """The home team's candidates."""
    frame = pr.candidates(games, lineups, lines).join(
        games.select("game_id", team="home"), on=["game_id", "team"], how="semi"
    )
    return {row["player_id"]: row for row in frame.iter_rows(named=True)}


def test_each_candidates_inputs() -> None:
    rows = inputs(TARGET, HISTORY)
    assert sorted(rows) == [1, 2, 3, 4, 5]
    expected = {
        # dressed_last, recent_share, since_last, early_exit, streak, defense, and the two
        # products with the first game of a season
        1: (1.0, 1.0, 0.0, 0.0, 0.3, 0.0, 0.0, 0.0),
        # 400 seconds is under half of his 900 in the other games.
        2: (1.0, 1.0, 0.0, 1.0, 0.3, 0.0, 0.0, 0.0),
        # Listed as a forward in his latest game.
        3: (1.0, 1.0, 0.0, 0.0, 0.3, 0.0, 0.0, 0.0),
        4: (0.0, 1 / 3, 0.2, 0.0, 0.0, 0.0, 0.0, 0.0),
        5: (1.0, 2 / 3, 0.0, 0.0, 0.2, 0.0, 0.0, 0.0),
    }
    for player, values in expected.items():
        assert tuple(rows[player][name] for name in pr.FEATURES) == pytest.approx(values)
    assert rows[3]["role"] == "F" and not rows[1]["opener"]
    # The game's own boxscore is not public yet, so the labels are null; BUF has no history.
    assert all(row["dressed"] is None for row in rows.values())
    assert set(pr.candidates(TARGET, HISTORY, {})["team"]) == {"BOS"}


def test_a_defenseman_and_an_early_exit_need_his_other_games() -> None:
    history = boxscores(
        (2011020001, 20112012, date(2011, 10, 5), "BOS", ((7, "D", 300),)),
        (2011020002, 20112012, date(2011, 10, 6), "BOS", ((8, "D", None), (9, "F", 0))),
    )
    target = schedule((2011020003, 20112012, date(2011, 10, 8), "BOS", "BUF"))
    rows = inputs(target, history)
    assert rows[7]["defense"] == 1.0 and rows[7]["since_last"] == pytest.approx(0.1)
    # One game only, or no time on ice: no early exit.
    assert rows[8]["early_exit"] == 0.0 and rows[9]["early_exit"] == 0.0


def test_the_games_own_boxscore_labels_its_candidates() -> None:
    own = boxscores(
        (2011020004, 20112012, date(2011, 10, 9), "BOS", ((1, "F", 1000), (5, "F", 900),
                                                          (6, "D", 1100))),
    )  # fmt: skip
    rows = inputs(TARGET, pl.concat([HISTORY, own]))
    assert {p: row["dressed"] for p, row in rows.items()} == {
        1: True,
        2: False,
        3: False,
        4: False,
        5: True,
    }
    # 6 dressed but was not a candidate: one newcomer.
    assert {row["skaters_dressed"] for row in rows.values()} == {3}
    assert {row["label_utc"] for row in rows.values()} == {public_after(date(2011, 10, 9))}
    (newcomers,) = pr.team_game_newcomers(pr.candidates(TARGET, pl.concat([HISTORY, own]), {}))[
        "newcomers"
    ]
    assert newcomers == 1


def test_a_player_who_dressed_for_another_team_since_drops_out() -> None:
    traded = boxscores((2011020010, 20112012, date(2011, 10, 7), "BUF", ((4, "F", 900),)))
    assert 4 not in inputs(TARGET, pl.concat([HISTORY, traded]))
    # He is BUF's candidate now.
    buf = pr.candidates(TARGET, pl.concat([HISTORY, traded]), {}).filter(pl.col("team") == "BUF")
    assert buf["player_id"].to_list() == [4]
    # Back with BOS since: a candidate again.
    back = boxscores((2011020011, 20112012, date(2011, 10, 8), "BOS", ((4, "F", 900),)))
    assert 4 in inputs(TARGET, pl.concat([HISTORY, traded, back]))
    # A BUF game not yet public at the as-of time does not count.
    late = boxscores((2011020012, 20112012, date(2011, 10, 9), "BUF", ((4, "F", 900),)))
    assert 4 in inputs(TARGET, pl.concat([HISTORY, late]))


def test_candidates_come_from_the_last_ten_games_and_runs_are_capped() -> None:
    days = [date(2011, 10, 1 + 2 * k) for k in range(13)]
    games = [
        (2011020100 + k, 20112012, day, "BOS", ((1, "F", 900), (9 if k == 0 else 2, "F", 900)))
        for k, day in enumerate(days[:12])
    ]
    target = schedule((2011020200, 20112012, days[12], "BOS", "BUF"))
    rows = inputs(target, boxscores(*games))
    # 9 dressed only in the first of twelve games; 1's run of 12 is capped at 10.
    assert sorted(rows) == [1, 2]
    assert rows[1]["streak"] == 1.0 and rows[2]["streak"] == 1.0


def test_a_new_season_turns_on_the_products() -> None:
    opener = schedule((2012020001, 20122013, date(2012, 10, 10), "BOS", "BUF"))
    rows = inputs(opener, HISTORY)
    for row in rows.values():
        assert row["opener"]
        assert row["opener_dressed_last"] == row["dressed_last"]
        assert row["opener_recent_share"] == row["recent_share"]


def test_a_renamed_team_keeps_its_skaters() -> None:
    history = boxscores((2023020001, 20232024, date(2024, 4, 1), "ARI", ((7, "F", 900),)))
    target = schedule((2024020001, 20242025, date(2024, 10, 8), "UTA", "CHI"))
    assert sorted(inputs(target, history, ARI="PHX", UTA="PHX")) == [7]
    assert inputs(target, history) == {}


def test_the_objectives_gradient_is_right() -> None:
    rng = np.random.default_rng(0)
    x = np.column_stack([np.ones(50), rng.normal(size=(50, len(pr.FEATURES)))])
    y = (rng.random(50) < 0.6).astype(float)
    loss = pr.objective(x, y)
    beta = rng.normal(size=len(pr.FEATURES) + 1)
    assert check_grad(lambda b: loss(b)[0], lambda b: loss(b)[1], beta) < 1e-5


def test_the_shift_meets_each_total_and_keeps_the_order() -> None:
    logits = np.array([2.0, 1.0, -1.0, -3.0, 0.5, 0.0, 3.0])
    groups = np.array([0, 0, 0, 0, 1, 1, 1], dtype=np.int64)
    p = pr.shift_to_total(logits, groups, np.array([2.5, 3.0]))
    assert p[:4].sum() == pytest.approx(2.5, abs=1e-12)
    assert np.all(np.diff(p[:4]) < 0)
    # Three rows cannot add up to more than three: each gets 1.
    assert p[4:].tolist() == [1.0, 1.0, 1.0]
    assert pr.shift_to_total(np.empty(0), np.empty(0, dtype=np.int64), np.empty(0)).size == 0


def test_the_fit_learns_who_dresses() -> None:
    rows = pr.candidates(LEAGUE["games"], LEAGUE["lineups"], {})
    model = pr.fit(rows, LEAGUE["games"], 20122013, VERSION)
    coefficients = dict(zip(("intercept", *pr.FEATURES), model.coefficients, strict=True))
    # Regulars dress, and a skater hurt last game misses the next.
    assert coefficients["dressed_last"] > 0 and coefficients["recent_share"] > 0
    assert coefficients["early_exit"] < 0
    assert model.seasons == (20102011, 20112012)
    other, first = model.newcomers
    newcomers = pr.team_game_newcomers(rows.filter(pl.col("season") < 20122013))
    assert other == pytest.approx(newcomers.filter(~pl.col("opener"))["newcomers"].mean())
    assert first == pytest.approx(newcomers.filter(pl.col("opener"))["newcomers"].mean())
    test = rows.filter(pl.col("season") == 20122013)
    p = test.with_columns(p=pl.Series(model.predict(test)))
    sums = p.group_by("game_id", "team").agg(pl.col("p").sum(), pl.col("opener").first())
    expected = DRESSED_SKATERS - pl.when(pl.col("opener")).then(first).otherwise(other)
    assert sums.select((pl.col("p") - expected).abs().max()).item() < 1e-9
    # A skater who dressed last game is likelier to dress than one who did not.
    means = p.group_by("dressed_last").agg(pl.col("p").mean()).sort("dressed_last")["p"]
    assert means[1] > means[0]


def test_the_first_seasons_fit_has_no_earlier_first_games() -> None:
    rows = pr.candidates(LEAGUE["games"], LEAGUE["lineups"], {})
    model = pr.fit(rows, LEAGUE["games"], 20112012, VERSION)
    # 2010-11's first games have no history, so first games take the other games' average,
    # and the products, never non-zero in training, stay at zero.
    assert model.newcomers[0] == model.newcomers[1]
    assert model.coefficients[-2:] == (0.0, 0.0)


def scored_league() -> tuple[pl.DataFrame, pl.DataFrame, list[pr.AvailabilityModel]]:
    # The skaters' rows carry their minutes (#100), as nhl lineups writes them.
    return scored_with_minutes(LEAGUE["lineups"], LEAGUE["games"], [20112012, 20122013], VERSION)


def goalie_starts() -> pl.DataFrame:
    table, _, _ = gs.score(
        LEAGUE["lineups"], LEAGUE["games"], [20112012, 20122013], GOALIE_VERSION, {}
    )
    return table


def test_score_writes_a_valid_lineups_table_with_the_goalies() -> None:
    skaters, scored, models = scored_league()
    starts = goalie_starts()
    table = pr.with_goalies(skaters, starts)
    Lineups.validate(table)
    assert [model.season for model in models] == [20112012, 20122013]
    assert scored.height == skaters.height
    goalies = table.filter(pl.col("role") == "G")
    assert goalies.height == starts.height
    assert set(goalies["artifact_version"]) == {GOALIE_VERSION}
    for model in models:
        rows = table.filter(pl.col("season") == model.season, pl.col("role") != "G")
        assert (rows["train_cutoff"] == model.train_cutoff).all()
        assert model.train_cutoff < gs.season_cutoff(LEAGUE["games"], model.season)
    # The first night of the lake has no candidates for a team with no earlier game.
    first = LEAGUE["games"]["game_date"].min()
    assert table.filter(pl.col("game_date") == first).is_empty()


def test_with_goalies_refuses_a_team_game_without_goalie_starts() -> None:
    skaters, _, _ = scored_league()
    starts = goalie_starts()
    first = skaters["game_id"].min()
    with pytest.raises(ValueError, match="2 team-games have no goalie-start probabilities"):
        pr.with_goalies(skaters, starts.filter(pl.col("game_id") != first))


def test_the_schema_refuses_more_than_eighteen_skaters() -> None:
    skaters, _, _ = scored_league()
    table = pr.with_goalies(skaters, goalie_starts())
    first = table.filter(pl.col("role") != "G").row(0, named=True)
    extra = pl.DataFrame(
        [{**first, "player_id": 99_999 + k, "p_available": 1.0} for k in range(DRESSED_SKATERS)],
        schema=table.schema,
    )
    with pytest.raises(Exception, match="skaters_add_up_to_at_most_those_who_dress"):
        Lineups.validate(pl.concat([table, extra]))


def test_score_refuses_a_season_with_nothing_earlier() -> None:
    with pytest.raises(ValueError, match="no earlier season"):
        pr.score(LEAGUE["lineups"], LEAGUE["games"], [20102011], VERSION, {})


def test_input_problems() -> None:
    games, lineups = LEAGUE["games"], LEAGUE["lineups"]
    counts = dict(games.group_by("season").len().iter_rows())
    assert pr.input_problems(games, lineups, 20122013, counts) == []
    first = games.row(0, named=True)
    goalies_only = lineups.filter((pl.col("game_id") != first["game_id"]) | (pl.col("role") == "G"))
    problems = pr.input_problems(games, goalies_only, 20122013, counts)
    assert len(problems) == 1 and "2 team-games without skaters" in problems[0]
    short = pr.input_problems(games, lineups, 20122013, {**counts, 20112012: 1_000})
    assert short == [f"20112012: {counts[20112012]:,} of 1,000 games"]


# The report


def test_team_game_scores() -> None:
    own = boxscores(
        (2011020004, 20112012, date(2011, 10, 9), "BOS", ((1, "F", 1000), (5, "F", 900),
                                                          (6, "D", 1100))),
    )  # fmt: skip
    lineups = pl.concat([HISTORY, own])
    scored = pr.candidates(TARGET, lineups, {}).with_columns(p_available=pl.lit(0.5))
    scores = report.team_game_scores(scored, lineups)
    row = scores.filter(pl.col("game_id") == 2011020004).row(0, named=True)
    # Five candidates at 0.5 and one newcomer: 5 * 0.25 + 1. The reference says 1, 2, 3 and 5
    # dress: wrong on 2 and 3, plus the newcomer.
    assert row["newcomers"] == 1 and row["skaters"] == 3
    assert row["brier"] == pytest.approx(2.25)
    assert row["brier_reference"] == pytest.approx(3.0)
    assert row["brier_minus_reference"] == pytest.approx(-0.75)
    # The history's own team-games have no candidate rows here: all their skaters are newcomers.
    first = scores.filter(pl.col("game_id") == 2011020001).row(0, named=True)
    assert first["newcomers"] == 4 and first["brier"] == 4.0


def test_the_report_hides_held_out_seasons_and_fits() -> None:
    _, scored, models = scored_league()
    scores = report.team_game_scores(scored, LEAGUE["lineups"])
    text = report.markdown_report(scores, models, [20102011, 20112012], VERSION)
    assert f"# Lineup availability: {VERSION}" in text
    seasons = text.split("## Fits")[0].splitlines()
    rows = {line.split(" | ")[0]: line for line in seasons if line.startswith("| 20")}
    assert "held out" not in rows["| 20112012"] and "[" in rows["| 20112012"]
    assert "held out" in rows["| 20122013"]
    # 2012-13's fit read 2011-12, which this time is held out.
    fits = report.fits(models, [20102011])
    assert fits[0]["coefficients"] is not None and fits[0]["newcomers"] is not None
    assert fits[1]["coefficients"] is None and fits[1]["newcomers"] is None
    assert fits[1]["team_games"] is None
    season = report.season_report(scores, [20112012])[0]
    assert 0 < season["newcomers"] < 0.1 and season["brier"].mean > 0
