from pathlib import Path

import polars as pl
from feed_fixtures import MTL_ARI, OPENING_WEEK_GAMES, feed, parsed_feeds

from nhl_edge.audit import plays as play_audit
from nhl_edge.lake.raw import RawStore

GAMES = (*OPENING_WEEK_GAMES, MTL_ARI)


def tables() -> dict[str, pl.DataFrame]:
    parsed = [parsed_feeds(game_id) for game_id in GAMES]
    return {
        name: pl.concat([p[name] for p in parsed])
        for name in ("penalties", "faceoffs", "actual_lineups")
    }


def games_of(frame: pl.DataFrame) -> pl.DataFrame:
    return frame.select("game_id", "season").unique().sort("game_id")


def test_boxscore_pims_read_the_boxscore_the_lineups_came_from(tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    lineups = tables()["actual_lineups"]
    for game_id in GAMES:
        key = lineups.filter(pl.col("game_id") == game_id)["raw_key"][0]
        source, rest = key.split("/", 1)
        store.put(source, rest, feed("boxscore", game_id), {"status": 200})
    box = play_audit.boxscore_pims(store, lineups)
    assert box.height == 2 * len(GAMES)
    assert box.filter(pl.col("game_id") == 2010020003)["box_pim"].sum() == 20


def test_pim_check_and_the_season_report() -> None:
    frames = tables()
    games = games_of(frames["actual_lineups"])
    box = pl.DataFrame(
        {
            "game_id": [2010020003, 2010020003, MTL_ARI, MTL_ARI],
            "team": ["MIN", "CAR", "MTL", "ARI"],
            # MTL's 11 minutes in play-by-play, and a misconduct the boxscore adds for ARI.
            "box_pim": [10, 10, 11, 17],
        }
    )
    checked = play_audit.pim_check(frames["penalties"], box, games)
    off = checked.filter(pl.col("feed_pim") != pl.col("box_pim"))
    assert off.select("game_id", "team", "feed_pim", "box_pim").rows() == [(MTL_ARI, "ARI", 7, 17)]

    report = play_audit.season_report(
        frames["penalties"], frames["faceoffs"], checked, games, open_seasons=[20102011]
    )
    rows = {row["season"]: row for row in report.iter_rows(named=True)}
    opening = rows[20102011]
    assert (opening["games"], opening["with_faceoffs"]) == (3, 3)
    assert opening["penalties"] == round((10 + 10 + 15) / 3, 1)
    assert opening["committer"] == 1.0 and opening["drawer"] == 1.0
    assert (opening["pim_agree"], opening["team_games"]) == (2, 2)
    # 2022-23 is not open here, so its rates are held out while its checks stay.
    held = rows[20222023]
    assert held["penalties"] is None and held["faceoffs"] is None
    assert (held["pim_agree"], held["team_games"]) == (1, 2)
    text = play_audit.markdown_report(report)
    assert "| 20222023 | 1 | 1 | 100.0% | 100.0% | 1 of 2 | held out | | |" in text

    found = play_audit.problems(frames["faceoffs"], checked, games)
    assert found == [
        "20222023: 1 team-games whose penalty minutes differ from the boxscore's, 1 of them by "
        "a multiple of 10, e.g. 2022020060 ARI (7 min in play-by-play, 17 in the boxscore)"
    ]


def test_a_game_without_faceoffs_is_a_problem() -> None:
    frames = tables()
    games = games_of(frames["actual_lineups"])
    faceoffs = frames["faceoffs"].filter(pl.col("game_id") != 2010020004)
    no_box = pl.DataFrame(schema=play_audit.PIM_SCHEMA)
    checked = play_audit.pim_check(frames["penalties"], no_box, games)
    found = play_audit.problems(faceoffs, checked, games)
    assert found == ["20102011: 1 games without faceoffs, e.g. 2010020004"]
