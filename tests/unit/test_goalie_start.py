from datetime import date

import numpy as np
import polars as pl
import pytest
from goalie_fixtures import GOALIES, league, lineup_frame, lineup_rows, start_of
from scipy.optimize import check_grad

from nhl_edge.audit import goalie_start as report
from nhl_edge.lake.schemas import GoalieStarts
from nhl_edge.lineup import goalie_start as gs

LEAGUE = league()
VERSION = "goalie-start-20261001-abc1234"


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


def boxscores(*games: tuple[int, int, date, str, int, tuple[int, ...]]) -> pl.DataFrame:
    rows = []
    for game_id, season, day, team, starter, dressed in games:
        rows += lineup_rows(game_id, season, day, team, True, starter, dressed)
    return lineup_frame(rows)


# BOS's history: a game in April 2011, then three in 2011-12. Goalie 1 starts the first three;
# in the third he is hurt, 3 is called up and 2 starts. The game to rate is the next night.
HISTORY = boxscores(
    (2010021200, 20102011, date(2011, 4, 1), "BOS", 1, (1, 2)),
    (2011020001, 20112012, date(2011, 10, 5), "BOS", 1, (1, 2)),
    (2011020002, 20112012, date(2011, 10, 6), "BOS", 1, (1, 2)),
    (2011020003, 20112012, date(2011, 10, 8), "BOS", 2, (2, 3)),
)
TARGET = schedule((2011020004, 20112012, date(2011, 10, 9), "BOS", "BUF"))


def inputs(games: pl.DataFrame, lineups: pl.DataFrame, **lines: str) -> dict[int, dict]:
    frame = gs.candidates(games, lineups, lines)
    return {row["goalie_id"]: row for row in frame.iter_rows(named=True)}


def test_each_candidates_inputs() -> None:
    rows = inputs(TARGET, HISTORY)
    assert sorted(rows) == [1, 2, 3]
    expected = {
        # recent_share, season_share, started_last, streak, back_to_back_started, rest,
        # dressed_last
        1: (3 / 4, 2 / 3, 0.0, 0.0, 0.0, 3 / 14, 0.0),
        2: (1 / 4, 1 / 3, 1.0, 0.1, 1.0, 1 / 14, 1.0),
        3: (0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 1.0),
    }
    for goalie, values in expected.items():
        assert tuple(rows[goalie][name] for name in gs.FEATURES) == pytest.approx(values)
    # The game's own boxscore is not public yet, so the labels are null; BUF has no history.
    assert all(row["started"] is None for row in rows.values())
    assert {row["team"] for row in rows.values()} == {"BOS"}


def test_a_new_season_starts_its_shares_at_zero() -> None:
    opener = schedule((2012020001, 20122013, date(2012, 10, 10), "BOS", "BUF"))
    rows = inputs(opener, HISTORY)
    assert all(row["season_share"] == 0.0 for row in rows.values())
    # The last ten games run across the summer.
    assert rows[1]["recent_share"] == pytest.approx(3 / 4)


def test_candidates_come_from_the_last_ten_games() -> None:
    days = [date(2011, 10, 1 + 2 * k) for k in range(12)]
    games = [
        (2011020100 + k, 20112012, day, "BOS", 1, (1, 9) if k == 0 else (1, 2))
        for k, day in enumerate(days[:11])
    ]
    targets = schedule(
        (2011020200, 20112012, days[10], "BOS", "BUF"),
        (2011020201, 20112012, days[11], "BOS", "BUF"),
    )
    frame = gs.candidates(targets, boxscores(*games), {})
    after_ten = frame.filter(pl.col("game_id") == 2011020200)["goalie_id"].to_list()
    after_eleven = frame.filter(pl.col("game_id") == 2011020201)["goalie_id"].to_list()
    assert after_ten == [1, 2, 9]
    assert after_eleven == [1, 2]


def test_a_renamed_team_keeps_its_goalies() -> None:
    history = boxscores((2023020001, 20232024, date(2024, 4, 1), "ARI", 7, (7, 8)))
    target = schedule((2024020001, 20242025, date(2024, 10, 8), "UTA", "CHI"))
    assert sorted(inputs(target, history, ARI="PHX", UTA="PHX")) == [7, 8]
    assert inputs(target, history) == {}


def test_the_objectives_gradient_is_right() -> None:
    rng = np.random.default_rng(0)
    groups = np.repeat(np.arange(30), rng.integers(1, 4, size=30)).astype(np.int64)
    x = rng.normal(size=(groups.size, len(gs.FEATURES)))
    y = np.zeros(groups.size)
    y[np.flatnonzero(np.r_[True, groups[1:] != groups[:-1]])] = 1.0
    loss = gs.objective(x, y, groups)
    beta = rng.normal(size=len(gs.FEATURES))
    assert check_grad(lambda b: loss(b)[0], lambda b: loss(b)[1], beta) < 1e-5


def test_the_fit_learns_who_starts() -> None:
    rows = gs.candidates(LEAGUE["games"], LEAGUE["lineups"], {})
    model = gs.fit(rows, LEAGUE["games"], 20122013, VERSION)
    coefficients = dict(zip(gs.FEATURES, model.coefficients, strict=True))
    # The starter starts most games, and the goalie who started the first night of a back-to-back
    # rests the second.
    assert coefficients["recent_share"] > 0
    assert coefficients["back_to_back_started"] < 0
    assert model.seasons == (20102011, 20112012)
    test = rows.filter(pl.col("season") == 20122013)
    p = test.with_columns(p=pl.Series(model.predict(test)))
    sums = p.group_by("game_id", "team").agg(pl.col("p").sum())["p"]
    assert sums.to_numpy() == pytest.approx(1.0)
    top = p.group_by("game_id", "team").agg(
        pl.col("started").sort_by("p").last().alias("top_started")
    )
    # The best possible is 0.85: the starter on 80% of ordinary nights, every back-to-back right.
    assert top["top_started"].mean() > 0.8  # type: ignore[operator]


def test_score_writes_valid_goalie_starts() -> None:
    table, scored, models = gs.score(
        LEAGUE["lineups"], LEAGUE["games"], [20112012, 20122013], VERSION, {}
    )
    GoalieStarts.validate(table)
    assert [model.season for model in models] == [20112012, 20122013]
    assert scored.height == table.height
    for model in models:
        rows = table.filter(pl.col("season") == model.season)
        assert (rows["train_cutoff"] == model.train_cutoff).all()
        assert model.train_cutoff < gs.season_cutoff(LEAGUE["games"], model.season)
    # The first night of each season has no candidates for a team with no earlier game.
    first = LEAGUE["games"]["game_date"].min()
    assert table.filter(pl.col("game_date") == first).is_empty()


def test_score_refuses_a_season_with_nothing_earlier() -> None:
    with pytest.raises(ValueError, match="no earlier season"):
        gs.score(LEAGUE["lineups"], LEAGUE["games"], [20102011], VERSION, {})


def test_input_problems() -> None:
    games, lineups = LEAGUE["games"], LEAGUE["lineups"]
    counts = dict(games.group_by("season").len().iter_rows())
    assert gs.input_problems(games, lineups, 20122013, counts) == []
    first = games.row(0, named=True)
    unflagged = lineups.with_columns(
        starting_goalie=pl.when(pl.col("game_id") == first["game_id"])
        .then(False)
        .otherwise(pl.col("starting_goalie"))
    )
    problems = gs.input_problems(games, unflagged, 20122013, counts)
    assert len(problems) == 1 and "2 team-games without a starter" in problems[0]
    short = gs.input_problems(games, lineups, 20122013, {**counts, 20112012: 1_000})
    assert short == [f"20112012: {counts[20112012]:,} of 1,000 games"]


# The report


def scored_frame(rows: list[tuple[int, str, int, float, float]]) -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "game_id": game_id,
                "season": 20112012,
                "game_date": date(2011, 10, 5),
                "team": team,
                "goalie_id": goalie,
                "p_start": p,
                "recent_share": share,
            }
            for game_id, team, goalie, p, share in rows
        ]
    ).with_columns(pl.col("season").cast(pl.Int32))


def test_team_game_scores() -> None:
    lineups = boxscores(
        (2011020001, 20112012, date(2011, 10, 5), "BOS", 1, (1, 2)),
        (2011020001, 20112012, date(2011, 10, 5), "BUF", 9, (5, 9)),
        (2011020002, 20112012, date(2011, 10, 5), "DET", 7, (7, 8)),
    )
    scored = scored_frame(
        [
            (2011020001, "BOS", 1, 0.7, 0.5),
            (2011020001, "BOS", 2, 0.3, 0.5),
            # BUF's starter 9 was not a candidate.
            (2011020001, "BUF", 5, 0.6, 1.0),
            (2011020001, "BUF", 6, 0.4, 0.0),
            # DET has no candidates at all.
        ]
    )
    scores = {row["team"]: row for row in report.team_game_scores(scored, lineups).to_dicts()}
    assert scores["BOS"]["brier"] == pytest.approx(0.3**2 + 0.3**2)
    assert scores["BOS"]["top"] == 1.0
    # The reference ties BOS's two goalies, so the top pick shares its credit.
    assert scores["BOS"]["top_reference"] == 0.5
    assert scores["BOS"]["missed"] == 0.0
    assert scores["BUF"]["brier"] == pytest.approx(0.6**2 + 0.4**2 + 1)
    assert (scores["BUF"]["missed"], scores["BUF"]["top"]) == (1.0, 0.0)
    assert (scores["DET"]["brier"], scores["DET"]["missed"], scores["DET"]["top"]) == (1, 1, 0)
    assert scores["BUF"]["brier_minus_reference"] == pytest.approx(0.6**2 + 0.4**2 - 1)


def test_the_report_hides_held_out_seasons_and_their_fits() -> None:
    _, scored, models = gs.score(
        LEAGUE["lineups"], LEAGUE["games"], [20112012, 20122013], VERSION, {}
    )
    scores = report.team_game_scores(scored, LEAGUE["lineups"])
    # 2011-12 held out: its figures are hidden, and so is the 2012-13 fit, which read it.
    text = report.markdown_report(scores, models, [20102011, 20122013], VERSION)
    lines = text.splitlines()

    def row(season: int, fit: bool) -> list[str]:
        """The season's row in the per-season table, or in the fits table."""
        rows = [
            [cell.strip() for cell in line.strip("|").split("|")]
            for line in lines
            if line.startswith(f"| {season} | ")
        ]
        (cells,) = [cells for cells in rows if cells[1].startswith("2010") is fit]
        return cells

    assert row(20112012, fit=False)[2:] == ["held out", *[""] * 6]
    assert "[" in row(20122013, fit=False)[3]
    shown_fit = row(20112012, fit=True)
    assert shown_fit[2].replace(",", "").isdigit() and shown_fit[3][0] in "+-"
    assert row(20122013, fit=True)[2:-1] == ["held out", *[""] * len(gs.FEATURES)]


def test_goalies_dress_for_their_own_team() -> None:
    # The fixture's sanity: each team's two goalies dress for it every game.
    dressed = LEAGUE["lineups"].filter(pl.col("role") == "G").group_by("team").agg("player_id")
    for team, players in dressed.iter_rows():
        assert set(players) == set(GOALIES[team])
