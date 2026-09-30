import json

import polars as pl
import pytest
from feed_fixtures import MTL_ARI, OPENING_WEEK_GAMES, feed, feed_game, parsed_feeds

from nhl_edge.audit import strength_time as audit
from nhl_edge.ingest.lineups import parse_actual_lineups
from nhl_edge.ingest.shifts import parse_shifts
from nhl_edge.ingest.strength_time import parse_strength_time
from nhl_edge.lake.schemas import StrengthTime

# Regulation, overtime (4v4 for 3:40) and a shootout, which is left out.
LENGTHS = {2010020003: 3600, 2010020004: 3820, 2010020008: 3900, MTL_ARI: 3600}


@pytest.mark.parametrize("game_id", [*OPENING_WEEK_GAMES, MTL_ARI])
def test_each_teams_seconds_add_up_to_the_game(game_id: int) -> None:
    # 2010020003 logs plays out of time order, and 2010020008's third period starts 24 seconds
    # before its first play: neither loses or double counts a second.
    frame = parsed_feeds(game_id)["strength_time"]
    totals = frame.group_by("team").agg(pl.col("seconds").sum(), pl.col("game_seconds").first())
    assert set(totals["seconds"]) == {LENGTHS[game_id]}
    assert set(totals["game_seconds"]) == {LENGTHS[game_id]}


@pytest.mark.parametrize("game_id", [*OPENING_WEEK_GAMES, MTL_ARI])
def test_the_two_teams_mirror_each_other(game_id: int) -> None:
    frame = parsed_feeds(game_id)["strength_time"]
    home = frame.filter("is_home").select(
        pl.col("strength").str.reverse(),
        pl.col("opp_net_empty").alias("own_net_empty"),
        pl.col("own_net_empty").alias("opp_net_empty"),
        "strength_source",
        "seconds",
    )
    away = frame.filter(~pl.col("is_home")).select(home.columns)
    assert home.sort(home.columns).equals(away.sort(away.columns))


def test_a_power_play_is_one_teams_5v4_and_the_others_4v5() -> None:
    frame = parsed_feeds(MTL_ARI)["strength_time"]
    seconds = {
        (team, strength): total
        for team, strength, total in frame.group_by("team", "strength")
        .agg(pl.col("seconds").sum())
        .iter_rows()
    }
    assert seconds[("ARI", "5v4")] == seconds[("MTL", "4v5")] > 0
    assert seconds[("MTL", "5v4")] == seconds[("ARI", "4v5")] > 0
    assert seconds[("MTL", "5v5")] > 3000


def pbp_with_codes(game_id: int, code: str, first: int, last: int) -> bytes:
    """The game's play-by-play with every play of period 2 from `first` to `last` seconds into
    the period given `code`, the way situationCode drifts after a penalty (#28)."""
    data = json.loads(feed("play-by-play", game_id))
    for play in data["plays"]:
        minutes, seconds = play.get("timeInPeriod", "99:99").split(":")
        clock = int(minutes) * 60 + int(seconds)
        if play["periodDescriptor"]["number"] == 2 and first <= clock <= last:
            play["situationCode"] = code
    return json.dumps(data).encode()


def test_a_complete_chart_overrides_a_drifted_situation_code() -> None:
    game = feed_game(MTL_ARI)
    shifts, _ = parse_shifts(feed("shiftcharts", MTL_ARI), game, "k")
    lineups = parse_actual_lineups(feed("boxscore", MTL_ARI), game, "k")
    drifted = pbp_with_codes(MTL_ARI, "1451", 200, 600)
    by_code = parse_strength_time(drifted, game, "k")
    by_chart = parse_strength_time(drifted, game, "k", shifts, lineups)

    clean = parsed_feeds(MTL_ARI)["strength_time"]

    def home_5v4(frame: pl.DataFrame) -> int:
        return int(frame.filter("is_home", pl.col("strength") == "5v4")["seconds"].sum())

    # 1451 gives ARI, at home, a skater more than MTL: by the code alone, most of the stretch
    # turns into an ARI power play. The chart keeps the clean game's time, second for second.
    assert home_5v4(by_code) >= home_5v4(clean) + 300
    sort = ["team", "strength", "own_net_empty", "opp_net_empty", "strength_source"]
    columns = [*sort, "seconds"]
    assert by_chart.select(columns).sort(sort).equals(clean.select(columns).sort(sort))
    assert set(by_code["strength_source"]) == {"situation_code"}


def test_without_a_complete_chart_every_second_comes_from_situation_code() -> None:
    frame = parse_strength_time(feed("play-by-play", MTL_ARI), feed_game(MTL_ARI), "k")
    assert set(frame["strength_source"]) == {"situation_code"}
    StrengthTime.validate(frame)


def test_the_audit_counts_games_that_add_up_and_names_those_that_do_not() -> None:
    frames = [parsed_feeds(game_id)["strength_time"] for game_id in (*OPENING_WEEK_GAMES,)]
    frame = pl.concat(frames)
    games = pl.DataFrame({"game_id": [*OPENING_WEEK_GAMES, 2010020999], "season": [20102011] * 4})
    report = audit.season_report(frame, games, [20102011]).row(0, named=True)
    assert (report["games"], report["with_time"], report["adds_up"]) == (4, 3, 3)
    assert report["even"] > 40 and report["chart_share"] > 0.9
    assert audit.problems(frame, games) == [
        "20102011: 1 games without strength time, e.g. 2010020999"
    ]
    short = frame.with_columns(
        seconds=pl.when(pl.col("game_id") == 2010020003)
        .then(pl.col("seconds") - 1)
        .otherwise(pl.col("seconds"))
    )
    first = audit.problems(short, games)[0]
    assert first.startswith("20102011: 1 games whose seconds do not add up, e.g. 2010020003")
    shown = audit.season_report(frame, games, [20102011])
    assert "| 20102011 | 4 | 3 | 3 |" in audit.markdown_report(shown)
    # A held-out season keeps its checks, but not its minutes.
    held = audit.season_report(frame, games, [])
    assert held.select("adds_up", "even", "chart_share").row(0) == (3, None, None)
    assert "| held out |" in audit.markdown_report(held)


def test_a_shift_that_starts_at_a_play_counts_for_the_stretch_after_it() -> None:
    # Codex on #82: at the faceoff that starts a power play, the penalized player's shift ends at
    # the play and the stretch after it is short-handed. MTL at ARI's chart agrees with every
    # situationCode, so read after each play it gives the same seconds, state by state. Read at
    # the moment of the play instead, it would miss 56 seconds of ARI's penalty kill.
    game = feed_game(MTL_ARI)
    shifts, _ = parse_shifts(feed("shiftcharts", MTL_ARI), game, "k")
    lineups = parse_actual_lineups(feed("boxscore", MTL_ARI), game, "k")
    pbp = feed("play-by-play", MTL_ARI)
    keys = ["team", "strength", "own_net_empty", "opp_net_empty"]

    def seconds(frame: pl.DataFrame) -> pl.DataFrame:
        return frame.group_by(keys).agg(pl.col("seconds").sum()).sort(keys)

    by_chart = parse_strength_time(pbp, game, "k", shifts, lineups)
    assert seconds(by_chart).equals(seconds(parse_strength_time(pbp, game, "k")))
