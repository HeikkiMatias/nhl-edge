"""Projected ice time (#100, ADR 0018)."""

from datetime import UTC, date, datetime

import polars as pl
import pytest
from lineup_fixtures import league, minutes_frame
from stint_fixtures import (
    AWAY_GOALIE,
    GAME,
    GAME_DATE,
    HOME_GOALIES,
    SEASON,
    build,
    full_period,
    lineups_frame,
)

from nhl_edge.audit import projection as report
from nhl_edge.lake.schemas import LineupReplacements, Lineups
from nhl_edge.lineup import goalie_start as gs
from nhl_edge.lineup import minutes as mins
from nhl_edge.lineup import projection as pr

LEAGUE = league()
MINUTES = minutes_frame(LEAGUE["lineups"])
ROWS = pr.candidates(LEAGUE["games"], LEAGUE["lineups"], {})
SEASONS = [20112012, 20122013]
VERSION = "lineup-20261002-abc1234"


def constants() -> dict[int, mins.SeasonConstants]:
    return {
        season: mins.season_constants(MINUTES, ROWS, season, LEAGUE["games"]) for season in SEASONS
    }


def projected() -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    skaters, scored, _ = pr.score(LEAGUE["lineups"], LEAGUE["games"], SEASONS, VERSION, {}, ROWS)
    rows, replacements = mins.project(scored, MINUTES, constants(), {})
    return skaters, rows, replacements


SKATERS, PROJECTED, REPLACEMENTS = projected()


# Minutes per game from stints


def test_player_minutes_reads_each_skater_s_seconds_per_state() -> None:
    stints = build(full_period())
    boxscores = lineups_frame().with_columns(
        season=pl.lit(SEASON, pl.Int32), game_date=pl.lit(GAME_DATE)
    )
    frame = mins.player_minutes(stints, boxscores, {})
    # Five skaters a side play the whole 20-minute period at 5v5; skaters 6 and 7 dressed but
    # had no shift, and goalies have no row.
    skaters = frame.filter(pl.col("player_id").is_in([1, 2, 3, 4, 5, 11, 12, 13, 14, 15]))
    assert skaters.height == 10
    assert (skaters["5v5"] == 20.0).all() and (skaters["pp"] == 0.0).all()
    idle = frame.filter(pl.col("player_id").is_in([6, 7]))
    assert idle["5v5"].to_list() == [0.0, 0.0]
    goalies = {*HOME_GOALIES, AWAY_GOALIE}
    assert frame.filter(pl.col("player_id").is_in(list(goalies))).is_empty()
    assert frame["game_id"].unique().to_list() == [GAME]
    # Without a line of team codes, each team is its own line.
    assert (frame["line"] == frame["team"]).all()


# The decayed history


def test_history_weights_each_game_by_half_every_ten_games() -> None:
    games = pl.DataFrame(
        {
            "game_id": [1, 2, 3],
            "game_date": [date(2015, 10, d) for d in (1, 3, 5)],
            "observed_utc": [datetime(2015, 10, d, 10, tzinfo=UTC) for d in (2, 4, 6)],
            "line": ["BOS"] * 3,
            "player_id": [7] * 3,
            "5v5": [10.0, 20.0, 30.0],
            "pp": [0.0, 1.0, 2.0],
            "pk": [1.0, 1.0, 1.0],
        }
    )
    last = mins.history(games).row(-1, named=True)
    d = mins.DECAY
    assert d**10 == pytest.approx(0.5)
    assert last["w"] == pytest.approx(1 + d + d**2)
    assert last["sum_5v5"] == pytest.approx(30 + 20 * d + 10 * d**2)
    assert last["last_5v5"] == 30.0


# The constants from the season before


def test_season_constants_come_from_the_season_before() -> None:
    c = mins.season_constants(MINUTES, ROWS, 20122013, LEAGUE["games"])
    assert (c.season, c.source) == (20122013, 20112012)
    before = MINUTES.filter(pl.col("season") == 20112012)
    forwards = before.filter(pl.col("role") == "F")
    assert c.mean["F", "5v5"] == pytest.approx(forwards["5v5"].mean())
    per_team_game = forwards.group_by("game_id", "team").agg(pl.col("5v5").sum())
    assert c.total["F", "5v5"] == pytest.approx(per_team_game["5v5"].mean())
    assert c.cutoff == before["observed_utc"].max()
    for role in ("F", "D"):
        for state in mins.STATES:
            assert c.pull[role, state] > 0


def test_2011_12_first_games_use_the_other_newcomer_average() -> None:
    # 2010-11's first games had no candidates, so no newcomers to average.
    c = mins.season_constants(MINUTES, ROWS, 20112012, LEAGUE["games"])
    for role in ("F", "D"):
        for state in mins.STATES:
            assert c.newcomer[role, True, state] == c.newcomer[role, False, state]


def test_season_constants_refuse_a_season_without_the_one_before() -> None:
    with pytest.raises(ValueError, match="no games with stints in 20092010"):
        mins.season_constants(MINUTES, ROWS, 20102011, LEAGUE["games"])


# The projection


def test_minutes_are_his_decayed_average_pulled_toward_his_role() -> None:
    c = constants()
    row = PROJECTED.filter(pl.col("w").is_not_null()).row(0, named=True)
    mean = c[row["season"]].mean[row["role"], "5v5"]
    pull = c[row["season"]].pull[row["role"], "5v5"]
    expected = (row["sum_5v5"] + pull * mean) / (row["w"] + pull)
    assert row["raw_5v5"] == pytest.approx(expected)
    assert row["min_5v5"] == pytest.approx(row["raw_5v5"] * row["scale_5v5"])
    assert row["exp_5v5"] == pytest.approx(row["p_available"] * row["min_5v5"])


def test_a_candidate_without_an_earlier_game_gets_the_role_average() -> None:
    # A player whose games all lack stints has no history for his team.
    player = PROJECTED["player_id"][0]
    _, scored, _ = pr.score(LEAGUE["lineups"], LEAGUE["games"], SEASONS, VERSION, {}, ROWS)
    c = constants()
    rows, _ = mins.project(scored, MINUTES.filter(pl.col("player_id") != player), c, {})
    fresh = rows.filter(pl.col("player_id") == player)
    assert fresh.height > 0 and fresh["w"].null_count() == fresh.height
    for row in fresh.iter_rows(named=True):
        assert row["raw_pp"] == pytest.approx(c[row["season"]].mean[row["role"], "pp"])


def test_candidates_and_replacements_add_up_to_the_league_total() -> None:
    c = constants()
    sums = PROJECTED.group_by("game_id", "team", "role", "season").agg(
        pl.col(*mins.EXPECTED.values()).sum(), dressing=pl.col("p_available").sum()
    )
    both = sums.join(REPLACEMENTS, on=["game_id", "team", "role"], suffix="_r")
    for row in both.iter_rows(named=True):
        for state, column in mins.EXPECTED.items():
            total = c[row["season"]].total[row["role"], state]
            assert row[column] + row[f"{column}_r"] == pytest.approx(total)
        slots = {"F": 12, "D": 6}[row["role"]]
        assert row["count"] == pytest.approx(max(0.0, slots - row["dressing"]))


def test_a_replacement_plays_a_newcomer_s_minutes() -> None:
    c = constants()
    opener = PROJECTED.select("game_id", "team", "opener").unique()
    for row in REPLACEMENTS.join(opener, on=["game_id", "team"]).iter_rows(named=True):
        each = c[row["season"]].newcomer[row["role"], row["opener"], "5v5"]
        assert row["exp_5v5"] == pytest.approx(row["count"] * each)


def test_power_play_unit_one_is_the_top_five_by_expected_minutes() -> None:
    for (_, _), team_game in PROJECTED.group_by("game_id", "team"):
        unit = team_game.filter(pl.col("pp_unit"))
        assert unit.height == min(5, team_game.height)
        rest = team_game.filter(~pl.col("pp_unit"))
        if rest.height:
            assert unit["exp_pp"].min() >= rest["exp_pp"].max()  # type: ignore[operator]


def test_project_refuses_a_season_without_constants() -> None:
    _, scored, _ = pr.score(LEAGUE["lineups"], LEAGUE["games"], SEASONS, VERSION, {}, ROWS)
    with pytest.raises(ValueError, match="no ice-time constants"):
        mins.project(scored, MINUTES, {20112012: constants()[20112012]}, {})


# The tables


def lineup_table() -> tuple[pl.DataFrame, pl.DataFrame]:
    c = constants()
    skaters = mins.with_minutes(SKATERS, PROJECTED, c)
    starts, _, _ = gs.score(
        LEAGUE["lineups"], LEAGUE["games"], SEASONS, "goalie-start-20261002-abc1234", {}
    )
    return pr.with_goalies(skaters, starts), mins.replacement_table(REPLACEMENTS, skaters)


def test_the_tables_validate_with_minutes_for_skaters_only() -> None:
    table, replacements = lineup_table()
    Lineups.validate(table)
    LineupReplacements.validate(replacements)
    goalies = table.filter(pl.col("role") == "G")
    assert goalies.select(pl.col("exp_5v5", "pp_unit").null_count()).row(0) == (
        goalies.height,
        goalies.height,
    )
    skaters = table.filter(pl.col("role") != "G")
    assert skaters["exp_5v5"].null_count() == 0
    # train_cutoff covers the minutes' season-before figures too.
    c = constants()
    for season, constant in c.items():
        rows = skaters.filter(pl.col("season") == season)
        assert (rows["train_cutoff"] >= constant.cutoff).all()
        assert (rows["train_cutoff"] < rows["observed_utc"]).all()


def test_the_schemas_refuse_goalie_minutes_and_a_unit_of_six() -> None:
    table, replacements = lineup_table()
    goalie = table.filter(pl.col("role") == "G").head(1).with_columns(exp_5v5=pl.lit(1.0))
    with pytest.raises(Exception, match="only_skaters_have_minutes"):
        Lineups.validate(pl.concat([table.filter(pl.col("role") != "G"), goalie]))
    first = table.filter(pl.col("role") != "G").head(1)["game_id"].item()
    six = table.with_columns(
        pp_unit=pl.when((pl.col("game_id") == first) & (pl.col("role") != "G"))
        .then(True)
        .otherwise(pl.col("pp_unit"))
    )
    with pytest.raises(Exception, match="a_power_play_unit_of_at_most_five"):
        Lineups.validate(six)
    too_many = replacements.head(1).with_columns(count=pl.lit(13.0))
    with pytest.raises(Exception, match="at_most_the_role_s_slots"):
        LineupReplacements.validate(too_many)


# The report


def test_ice_time_scores_compare_with_last_game_s_minutes() -> None:
    scores = report.ice_time_scores(PROJECTED, MINUTES)
    assert set(scores["season"].unique()) == set(SEASONS)
    row = scores.drop_nulls("mae").row(0, named=True)
    keys = ["game_id", "team", "player_id"]
    dressed = (
        PROJECTED.filter(pl.col("game_id") == row["game_id"], pl.col("team") == row["team"])
        .join(MINUTES.select(*keys, actual="5v5"), on=keys)
        .drop_nulls("last_5v5")
    )
    mae = (dressed["min_5v5"] - dressed["actual"]).abs().mean()
    reference = (dressed["last_5v5"] - dressed["actual"]).abs().mean()
    assert row["mae"] == pytest.approx(mae)
    assert row["mae_reference"] == pytest.approx(reference)
    assert scores["pp_unit"].drop_nulls().is_between(0, 1).all()
    # The reference names the team's top five in its latest earlier game.
    tops = report._power_play_tops(MINUTES).sort("game_utc")  # pyright: ignore[reportPrivateUsage]
    row = scores.drop_nulls("pp_unit_reference").row(-1, named=True)
    as_of = PROJECTED.filter(pl.col("game_id") == row["game_id"], pl.col("team") == row["team"])
    earlier = tops.filter(
        pl.col("line") == row["team"], pl.col("game_utc") < as_of["as_of_utc"].min()
    ).row(-1, named=True)
    actual = tops.filter(pl.col("game_id") == row["game_id"], pl.col("team") == row["team"])
    overlap = len(set(earlier["top"]) & set(actual["top"].item()))
    assert row["pp_unit_reference"] == pytest.approx(overlap / 5)


def test_the_report_has_the_ice_time_sections() -> None:
    scores = report.team_game_scores(
        pr.score(LEAGUE["lineups"], LEAGUE["games"], SEASONS, VERSION, {}, ROWS)[1],
        LEAGUE["lineups"],
    )
    ice = report.ice_time_scores(PROJECTED, MINUTES)
    c = constants()
    text = report.markdown_report(scores, [], SEASONS, VERSION, ice, [c[s] for s in SEASONS])
    assert "## Ice time" in text and "## Ice-time figures from the season before" in text
    held = report.markdown_report(scores, [], [20122013], VERSION, ice, [c[s] for s in SEASONS])
    # 2011-12 is not shown, so neither are its figures nor those 2012-13 took from it.
    section = held.split("## Ice time")[1]
    assert "| 20112012 | " in section and "held out" in section
