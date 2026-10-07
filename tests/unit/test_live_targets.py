from datetime import UTC, date, datetime, timedelta

import polars as pl
import pytest
from pandera.errors import SchemaError

from nhl_edge.lake.schemas import FeatureBuilds, Games, Schedule, Slate, dtypes
from nhl_edge.live import features as lf
from nhl_edge.live.targets import rated, with_schedule_targets, with_targets

NIGHT = date(2026, 10, 7)
START = datetime(2026, 10, 7, 23, tzinfo=UTC)
FETCHED = datetime(2026, 10, 7, 9, tzinfo=UTC)


def slate(*ids: int, fetched: datetime = FETCHED) -> pl.DataFrame:
    rows = [
        {
            "game_id": game_id,
            "season": 20262027,
            "game_date": NIGHT,
            "start_utc": START,
            "home": "BOS",
            "away": "TOR",
            "venue": "TD Garden",
            "neutral_site": False,
            "limited_attendance": False,
            "game_state": "FUT",
            "observed_utc": fetched,
            "raw_key": "nhl/schedule/2026-10-07/20261007T090000Z",
        }
        for game_id in ids
    ]
    return Slate.validate(pl.DataFrame(rows, schema=dtypes(Slate)))


def played(game_id: int) -> dict[str, object]:
    day = NIGHT - timedelta(days=1)
    return {
        "game_id": game_id,
        "season": 20262027,
        "game_date": day,
        "start_utc": START - timedelta(days=1),
        "home": "TOR",
        "away": "BOS",
        "venue": "Scotiabank Arena",
        "home_score": 3,
        "away_score": 2,
        "decided_in": "REG",
        "neutral_site": False,
        "limited_attendance": False,
        "observed_utc": datetime(2026, 10, 7, 10, tzinfo=UTC),
        "raw_key": "nhl/schedule/2026-10-06/x",
    }


GAMES = Games.validate(pl.DataFrame([played(2026020050)], schema=dtypes(Games)))


def test_targets_join_games_with_no_result() -> None:
    games = with_targets(GAMES, slate(2026020060, 2026020061))
    assert games.columns == GAMES.columns
    targets = games.filter(pl.col("game_date") == NIGHT)
    assert targets["game_id"].to_list() == [2026020060, 2026020061]
    # No builder can read a result, or a time a result became public, for a target.
    for column in ("home_score", "away_score", "decided_in", "observed_utc"):
        assert targets[column].is_null().all()
    assert targets["start_utc"].to_list() == [START, START]
    assert_unchanged = games.filter(pl.col("game_id") == 2026020050)
    assert assert_unchanged.equals(GAMES)


def test_a_slate_game_already_final_is_rated_from_history_only() -> None:
    final = slate(2026020050, 2026020060)
    games = with_targets(GAMES, final)
    assert games["game_id"].to_list() == [2026020050, 2026020060]
    assert games.filter(pl.col("game_id") == 2026020050).equals(GAMES)


def test_a_targets_schedule_row_is_public_no_sooner_than_history_says() -> None:
    schedule = GAMES.select(list(dtypes(Schedule))).with_columns(
        observed_utc=pl.col("start_utc") - timedelta(hours=24)
    )
    # Fetched at 09:00 on the day: public when fetched, which is after a day before the start.
    rows = with_schedule_targets(schedule, slate(2026020060))
    assert rows.columns == schedule.columns
    assert rows.filter(pl.col("game_id") == 2026020060)["observed_utc"].item() == FETCHED
    # Fetched two days ahead: public a day before the start, as history's schedule rows are.
    early = with_schedule_targets(schedule, slate(2026020060, fetched=START - timedelta(days=2)))
    assert early.filter(pl.col("game_id") == 2026020060)["observed_utc"].item() == START - (
        timedelta(hours=24)
    )
    Schedule.validate(early)


def test_rows_of_games_not_rated_are_left_out() -> None:
    # A target row written for a night whose games were postponed is no game's row.
    lineups = pl.DataFrame({"game_id": [2026020050, 2026020060, 2026020099]})
    games = with_targets(GAMES, slate(2026020060))
    assert rated(lineups, games)["game_id"].to_list() == [2026020050, 2026020060]
    # Nor once the postponed game is played on another date: its old target rows stay out.
    dated = pl.DataFrame(
        {
            "game_id": [2026020060, 2026020060],
            "game_date": [NIGHT - timedelta(days=3), NIGHT],
        }
    )
    assert rated(dated, games)["game_date"].to_list() == [NIGHT]


BUILD = {
    "build_id": "live-features-20261007-abc1234",
    "code_version": "abc1234",
    "slate_raw_key": "nhl/schedule/2026-10-07/20261007T090000Z",
    "slate_fetched_utc": FETCHED,
    "started_utc": datetime(2026, 10, 7, 9, 5, tzinfo=UTC),
    "finished_utc": datetime(2026, 10, 7, 9, 12, tzinfo=UTC),
}


def table(version: str, ids: list[int]) -> pl.DataFrame:
    return pl.DataFrame({"game_id": ids, "artifact_version": [version] * len(ids)})


def test_the_record_names_each_tables_version_and_coverage() -> None:
    games = slate(2026020060, 2026020061)
    tables = {
        "team_strength": table("team-strength-20261007-abc1234", [2026020060, 2026020061]),
        "lineups": table("lineup-20261007-abc1234", [2026020060] * 3),
        "rapm_terms": pl.DataFrame({"artifact_version": ["rapm-20261007-abc1234"] * 4}),
    }
    record = lf.record(games, NIGHT, 20262027, tables, BUILD)
    FeatureBuilds.validate(record)
    assert record["table"].to_list() == ["slate", "team_strength", "lineups", "rapm_terms"]
    assert record["rows"].to_list() == [2, 2, 3, 4]
    assert record["games"].to_list() == [2, 2, 1, None]
    assert record["artifact_version"][0] == BUILD["build_id"]
    assert set(record["slate_games"]) == {2}
    assert lf.uncovered(games, tables) == ["lineups: no rows for 1 slate games (2026020061)"]


def test_a_table_with_no_rows_or_two_versions_is_refused() -> None:
    games = slate(2026020060)
    with pytest.raises(ValueError, match="no rows"):
        lf.record(games, NIGHT, 20262027, {"lineups": table("lineup-x", [])}, BUILD)
    mixed = pl.concat(
        [
            table("lineup-20261007-abc1234", [2026020060]),
            table("lineup-20261007-def5678", [2026020060]),
        ]
    )
    with pytest.raises(ValueError, match="2 lineup versions"):
        lf.record(games, NIGHT, 20262027, {"lineups": mixed}, BUILD)


def test_a_table_may_hold_another_components_rows() -> None:
    # lineups copies each candidate goalie's row from goalie_starts, with its version.
    lineups = pl.concat(
        [
            table("lineup-20261007-abc1234", [2026020060] * 3),
            table("goalie-start-20261007-abc1234", [2026020060] * 2),
        ]
    )
    record = lf.record(slate(2026020060), NIGHT, 20262027, {"lineups": lineups}, BUILD)
    assert record.filter(pl.col("table") == "lineups").select(
        "artifact_version", "rows", "games"
    ).rows() == [("goalie-start-20261007-abc1234", 2, 1), ("lineup-20261007-abc1234", 3, 1)]
    assert lf.component("goalie-start-20261007-abc1234-dirty") == "goalie-start"


def test_a_record_must_finish_after_it_starts() -> None:
    backwards = {**BUILD, "finished_utc": datetime(2026, 10, 7, 9, tzinfo=UTC)}
    with pytest.raises(SchemaError):
        lf.record(slate(2026020060), NIGHT, 20262027, {}, backwards)


def test_every_target_table_is_in_the_lake_and_pulled() -> None:
    from nhl_edge.lake.tables import TABLES

    assert set(lf.TARGET_TABLES) <= set(lf.LAKE_TABLES) <= set(TABLES)
    assert TABLES["feature_builds"].key == ("game_date", "table", "artifact_version")


def test_a_day_without_games_is_recorded() -> None:
    empty = slate().head(0)
    record = lf.record(empty, NIGHT, 20262027, {}, BUILD)
    assert record.select("table", "rows", "games", "slate_games").rows() == [("slate", 0, 0, 0)]
