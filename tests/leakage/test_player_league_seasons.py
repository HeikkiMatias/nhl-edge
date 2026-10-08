"""Point-in-time rules for player_league_seasons (#98). A season's lines count as public on July 1
(00:00 UTC) after it, when nearly every league's season and the NHL playoffs are over, so known_at
shows none of them before then. The late leagues (such as the Australian league, the World Cup of
Hockey, the Brick Invitational and the Olympic qualification) wait until October 1, the 2019-20
and 2020-21 seasons, whose NHL playoffs ran past July 1, until their end, and the 2021-22 World
Juniors, replayed in August 2022, until October 1, 2022. Every player here reached the NHL, so a
player's lines also wait until his first boxscore is public, and a player without one has none.
The landing pages were fetched in 2026, and a line whose season was still under way at the fetch is
a partial season that never enters the table. The column set is locked: a new column, such as a
later stat or the page's fetch time, needs a look at when it became public first."""

from datetime import UTC, date, datetime, timedelta

import pandera.errors
import polars as pl
import pytest
from player_season_fixtures import (
    GOALIE,
    SKATER,
    boxscores,
    debuts,
    first_boxscore_utc,
    table,
)

from nhl_edge.ingest.player_seasons import (
    LINE_SCHEMA,
    first_boxscores,
    player_league_seasons,
    season_lines_public,
)
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
    "first_boxscore_utc",
    "observed_utc",
    "raw_key",
]


def test_a_season_is_unknown_before_july_1_after_it_and_known_right_after() -> None:
    frame = table()
    keys = frame.select("player_id", "season", "league_abbrev").unique().rows()
    for player_id, season, abbrev in keys:
        # The later of the season's public date and the player's first boxscore.
        public = max(season_lines_public(season, abbrev), first_boxscore_utc(player_id))
        line = (
            (pl.col("player_id") == player_id)
            & (pl.col("season") == season)
            & (pl.col("league_abbrev") == abbrev)
        )
        assert known_at(frame.filter(line), public).is_empty()
        later = (pl.col("player_id") == player_id) & (pl.col("season") > season)
        assert known_at(frame, public).filter(later).is_empty()
        after = known_at(frame.filter(line), public + timedelta(seconds=1))
        assert after.height == frame.filter(line).height


def test_a_player_s_lines_stay_hidden_until_his_first_boxscore_is_public() -> None:
    # Having lines here tells that a player reaches the NHL. The goalie's first game is on
    # 2012-02-04, so his junior and minor-league lines, public for years, wait for its boxscore.
    goalie = table().filter(pl.col("player_id") == GOALIE)
    debut = first_boxscore_utc(GOALIE)
    assert debut == datetime(2012, 2, 5, 10, tzinfo=UTC)
    before = goalie.filter(pl.col("season") < 20112012)
    assert before.height > 0
    assert known_at(before, debut).is_empty()
    assert known_at(before, debut + timedelta(seconds=1)).height == before.height


def test_a_player_s_first_boxscore_is_his_earliest() -> None:
    # The goalie's later game is listed first: his lines still count from the earlier one.
    later = boxscores(GOALIE).with_columns(
        game_id=pl.lit(2011020900, pl.Int64),
        game_date=pl.lit(date(2012, 3, 1)),
        observed_utc=pl.lit(datetime(2012, 3, 2, 10, tzinfo=UTC)),
    )
    first = first_boxscores(pl.concat([later, boxscores(GOALIE)]))
    assert first.rows() == [(GOALIE, first_boxscore_utc(GOALIE))]


def test_a_player_without_a_boxscore_has_no_rows() -> None:
    lines = pl.DataFrame(
        [(SKATER, 20082009, "OHL", 2, 10, 1, 1, "k")], schema=LINE_SCHEMA, orient="row"
    )
    players = pl.DataFrame(
        {"player_id": [SKATER], "birth_date": [None]}, schema_overrides={"birth_date": pl.Date}
    )
    assert player_league_seasons(lines, players, debuts(SKATER).clear()).is_empty()


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


def one_line(season: int, abbrev: str, game_type: int = 2) -> pl.DataFrame:
    """player_league_seasons of a single line."""
    lines = pl.DataFrame(
        [
            {
                "player_id": SKATER,
                "season": season,
                "league_abbrev": abbrev,
                "game_type": game_type,
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
    return player_league_seasons(lines, players, debuts(SKATER))


def hidden_until(frame: pl.DataFrame, public: datetime) -> None:
    """frame is unknown on July 1 after its season and up to public, and known right after."""
    july = datetime(public.year, 7, 1, 0, 0, 1, tzinfo=UTC)
    assert known_at(frame, july).is_empty()
    assert known_at(frame, public).is_empty()
    assert known_at(frame, public + timedelta(seconds=1)).height == frame.height


@pytest.mark.parametrize(
    "abbrev", ["AIHL", "Australia", "WCup", "W-Cup", "Brick Invitational", "JPL-Pro", "Exhib."]
)
def test_a_late_league_season_stays_hidden_past_july_1(abbrev: str) -> None:
    # The Australian league plays from April to September, so its 2017-18 line could hold games up
    # to September 2018. The World Cup of Hockey is played in August and September, the Brick
    # Invitational in early July, labelled with the season before, and JPL-Pro in the summer.
    # Exhibitions have no known dates.
    hidden_until(one_line(20172018, abbrev), datetime(2018, 10, 1, tzinfo=UTC))


def test_the_2025_olympic_qualification_stays_hidden_until_after_its_august_games() -> None:
    # The final round was played from August 28 to 31, 2025, under the 2024-25 label.
    frame = one_line(20242025, "OGQ")
    assert known_at(frame, datetime(2025, 9, 1, tzinfo=UTC)).is_empty()
    hidden_until(frame, datetime(2025, 10, 1, tzinfo=UTC))


def test_the_2022_world_juniors_stay_hidden_until_after_the_august_replay() -> None:
    # Stopped in December 2021 and replayed from August 9 to 20, 2022, under the 2021-22 label.
    frame = one_line(20212022, "WJC-20")
    assert known_at(frame, datetime(2022, 8, 21, tzinfo=UTC)).is_empty()
    hidden_until(frame, datetime(2022, 10, 1, tzinfo=UTC))


def test_the_2020_playoffs_stay_hidden_until_the_bubble_is_over() -> None:
    # The 2020 Stanley Cup Final ended on September 28, 2020: a 2019-20 playoff line holds games
    # up to then, and the season's other lines wait with it.
    for abbrev, game_type in (("NHL", 3), ("NHL", 2), ("AHL", 2)):
        frame = one_line(20192020, abbrev, game_type)
        assert known_at(frame, datetime(2020, 9, 28, 23, 59, tzinfo=UTC)).is_empty()
        hidden_until(frame, datetime(2020, 10, 1, tzinfo=UTC))


def test_the_2021_playoffs_stay_hidden_until_july_9() -> None:
    # The 2021 Stanley Cup Final ended on July 7, 2021.
    frame = one_line(20202021, "NHL", 3)
    assert known_at(frame, datetime(2021, 7, 8, 12, tzinfo=UTC)).is_empty()
    hidden_until(frame, datetime(2021, 7, 9, tzinfo=UTC))


def test_a_refetched_page_s_new_season_waits_for_july_1_not_the_refetch() -> None:
    # #117: the yearly refresh fetches a page again once the season just played is public. Its new
    # season's rows are dated by the season's public date, so the fetch time never moves them.
    for fetched in (datetime(2012, 7, 1, 0, 0, 1, tzinfo=UTC), datetime(2013, 2, 1, tzinfo=UTC)):
        frame = table(fetched, GOALIE)
        season = frame.filter(pl.col("season") == 20112012)
        assert not season.is_empty()
        assert (season["observed_utc"] == datetime(2012, 7, 1, tzinfo=UTC)).all()
        assert known_at(season, datetime(2012, 7, 1, tzinfo=UTC)).is_empty()
    # A copy fetched before July 1 holds the season as partial, and leaves it out.
    early = table(datetime(2012, 6, 30, 23, 59, tzinfo=UTC), GOALIE)
    assert early.filter(pl.col("season") >= 20112012).is_empty()
