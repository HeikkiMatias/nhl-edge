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

from nhl_edge.ingest.player_seasons import season_lines_public
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
    for season in frame["season"].unique().to_list():
        public = season_lines_public(season)
        before = known_at(frame, public)
        after = known_at(frame, public + timedelta(seconds=1))
        assert before.filter(pl.col("season") >= season).is_empty()
        assert after.filter(pl.col("season") == season).height == (
            frame.filter(pl.col("season") == season).height
        )


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
