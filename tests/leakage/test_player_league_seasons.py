"""Point-in-time rules for player_league_seasons (#98). A season's lines in every league count as
public on July 1 (00:00 UTC) after it, when every league's season and the NHL playoffs are over, so
known_at shows none of them before then. The landing pages were fetched in 2026, and a line whose
season was still under way at the fetch is a partial season that never enters the table. The
column set is locked: a new column, such as a later stat or the page's fetch time, needs a look at
when it became public first."""

from datetime import UTC, datetime, timedelta

import pandera.errors
import polars as pl
import pytest
from player_season_fixtures import SKATER, table

from nhl_edge.ingest.player_seasons import LINE_SCHEMA, player_league_seasons, season_lines_public
from nhl_edge.lake.schemas import PlayerLeagueSeasons, dtypes
from nhl_edge.lake.tables import known_at

COLUMNS = [
    "player_id",
    "season",
    "league_abbrev",
    "league",
    "game_type",
    "teams",
    "games_played",
    "goals",
    "assists",
    "age_at_season",
    "observed_utc",
    "raw_key",
]


def test_a_season_is_unknown_before_july_1_after_it_and_known_right_after() -> None:
    frame = table()
    for season, abbrev in frame.select("season", "league_abbrev").unique().rows():
        public = season_lines_public(season, abbrev)
        line = (pl.col("season") == season) & (pl.col("league_abbrev") == abbrev)
        assert known_at(frame.filter(line), public).is_empty()
        assert known_at(frame, public).filter(pl.col("season") > season).is_empty()
        after = known_at(frame.filter(line), public + timedelta(seconds=1))
        assert after.height == frame.filter(line).height


def test_the_nhl_playoffs_of_a_season_stay_hidden_until_july_1() -> None:
    # The 2002-03 NHL playoffs ended in June 2003; neither they nor the season show before July.
    frame = table()
    june = known_at(frame, datetime(2003, 6, 30, 23, 59, tzinfo=UTC))
    assert june.filter(pl.col("season") == 20022003).is_empty()
    july = known_at(frame, datetime(2003, 7, 1, 0, 0, 1, tzinfo=UTC))
    assert july.filter(pl.col("season") == 20022003)["game_type"].sort().to_list() == [2, 3]


def test_no_line_is_public_after_its_page_was_fetched() -> None:
    # A page fetched in mid-season: the season under way and every later one stay out.
    fetched = datetime(2006, 1, 15, tzinfo=UTC)
    frame = table(fetched, SKATER)
    assert frame["season"].max() == 20042005
    assert (frame["observed_utc"] <= fetched).all()


def test_the_column_set_is_locked() -> None:
    assert list(dtypes(PlayerLeagueSeasons)) == COLUMNS
    frame = table()
    for column, value in (("fetched_utc", datetime(2026, 9, 28, tzinfo=UTC)), ("points", 1)):
        with pytest.raises(pandera.errors.SchemaError):
            PlayerLeagueSeasons.validate(frame.with_columns(pl.lit(value).alias(column)))


def test_a_summer_league_season_stays_hidden_past_july_1() -> None:
    # The Australian league plays from April to September: its 2017-18 line could hold games up
    # to September 2018, so it is unknown on July 1 and until October 1, 2018.
    lines = pl.DataFrame(
        [
            {
                "player_id": SKATER,
                "season": 20172018,
                "league_abbrev": "AIHL",
                "game_type": 2,
                "games_played": 10,
                "goals": 1,
                "assists": 1,
                "raw_key": "k",
            }
        ],
        schema=LINE_SCHEMA,
    )
    players = pl.DataFrame(
        {"player_id": [SKATER], "birth_date": [None]}, schema_overrides={"birth_date": pl.Date}
    )
    frame = player_league_seasons(lines, players)
    assert known_at(frame, datetime(2018, 7, 1, 0, 0, 1, tzinfo=UTC)).is_empty()
    assert known_at(frame, datetime(2018, 10, 1, tzinfo=UTC)).is_empty()
    assert known_at(frame, datetime(2018, 10, 1, 0, 0, 1, tzinfo=UTC)).height == 1
