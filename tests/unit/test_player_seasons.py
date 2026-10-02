from datetime import UTC, date, datetime
from pathlib import Path

import pandera.errors
import polars as pl
import pytest
from player_season_fixtures import (
    FETCHED,
    GOALIE,
    NO_PAGE,
    SKATER,
    page,
    players,
    raw_key,
    store_pages,
    table,
)

from nhl_edge.ingest.player_seasons import (
    LEAGUE_VARIANTS,
    LINE_SCHEMA,
    age_at_season,
    build,
    landing_lines,
    league_name,
    player_league_seasons,
    season_lines_public,
)
from nhl_edge.lake.raw import RawStore
from nhl_edge.lake.schemas import PlayerLeagueSeasons

COUNTS = ["teams", "games_played", "goals", "assists"]


def row(frame: pl.DataFrame, player_id: int, season: int, abbrev: str, game_type: int = 2) -> dict:
    found = frame.filter(
        (pl.col("player_id") == player_id)
        & (pl.col("season") == season)
        & (pl.col("league_abbrev") == abbrev)
        & (pl.col("game_type") == game_type)
    )
    assert found.height == 1
    return found.row(0, named=True)


def counts(frame: pl.DataFrame, player_id: int, season: int, abbrev: str) -> tuple:
    found = row(frame, player_id, season, abbrev)
    return tuple(found[column] for column in COUNTS)


def test_a_players_teams_in_a_league_season_are_summed() -> None:
    frame = table()
    # Two NHL teams in 2000-01 (17 and 30 games) and in 2009-10 (56 and 26).
    assert counts(frame, SKATER, 20002001, "NHL") == (2, 47, 5, 11)
    assert counts(frame, SKATER, 20092010, "NHL") == (2, 82, 16, 25)
    assert counts(frame, SKATER, 20012002, "NHL") == (1, 82, 8, 10)
    # 39 lines: one of game type 6, and two pairs summed into one row each.
    assert frame.filter(pl.col("player_id") == SKATER).height == 36
    assert frame.filter(pl.col("player_id") == GOALIE).height == 11


def test_goalie_lines_keep_games_played_without_goals_or_assists() -> None:
    frame = table()
    assert counts(frame, GOALIE, 20122013, "ECHL") == (2, 11, None, None)
    assert counts(frame, GOALIE, 20112012, "NHL") == (1, 1, 0, 0)


def test_a_count_one_team_line_lacks_leaves_the_sum_null() -> None:
    lines = pl.DataFrame(
        [
            (SKATER, 20002001, "AHL", 2, 10, 3, 4, "k"),
            (SKATER, 20002001, "AHL", 2, 5, None, 2, "k"),
        ],
        schema=LINE_SCHEMA,
        orient="row",
    )
    frame = player_league_seasons(lines, players(SKATER))
    assert counts(frame, SKATER, 20002001, "AHL") == (2, 15, None, 6)


def test_league_is_trimmed_upper_case_with_known_variants_mapped() -> None:
    expected = {
        " Swiss": "NL",
        "NLA": "NL",
        "NL": "NL",
        "MtJHL": "MTJHL",
        "Sweden": "SHL",
        "SHL": "SHL",
        "H-East": "NCAA",
        "WC-A": "WC",
        "W-Cup": "WCUP",
        "Russia": "RUSSIA",  # the Superleague, before the KHL: kept apart
        "CHL": "CHL",  # the Central Hockey League, not the Champions HL
    }
    frame = pl.DataFrame({"abbrev": list(expected)})
    leagues = frame.select(league_name(pl.col("abbrev"))).to_series().to_list()
    assert leagues == list(expected.values())
    # The raw name stays in the key, the league beside it.
    swiss = row(table(), SKATER, 20042005, "Swiss")
    assert swiss["league"] == "NL"


def test_league_variants_map_normalized_names_to_a_final_name() -> None:
    for variant, league in LEAGUE_VARIANTS.items():
        assert variant == variant.strip().upper() != league
        assert league == league.strip().upper()
        assert league not in LEAGUE_VARIANTS  # no chains


def test_only_regular_season_and_playoff_lines_are_kept() -> None:
    lines = landing_lines(page(SKATER), FETCHED, raw_key(SKATER))
    assert lines.other_game_types == 1  # the 2004 World Cup of Hockey, game type 6
    assert {line["game_type"] for line in lines.rows} == {2, 3}
    frame = table()
    assert set(frame["game_type"].to_list()) == {2, 3}
    assert row(frame, SKATER, 20022003, "NHL", 3)["games_played"] == 5


@pytest.mark.parametrize(
    ("birth_date", "season", "age"),
    [
        (date(1997, 9, 15), 20152016, 18),  # 18 on the cutoff day itself
        (date(1997, 9, 16), 20152016, 17),
        (date(1997, 1, 13), 20152016, 18),
        (date(1996, 2, 29), 20152016, 19),
        (None, 20152016, None),
    ],
)
def test_age_is_taken_on_september_15_of_the_first_year(
    birth_date: date | None, season: int, age: int | None
) -> None:
    frame = pl.DataFrame(
        {"season": [season], "birth_date": [birth_date]},
        schema={"season": pl.Int32, "birth_date": pl.Date},
    )
    assert frame.select(age_at_season(pl.col("season"), pl.col("birth_date"))).item() == age


def test_age_comes_from_players_and_is_null_without_a_birth_date() -> None:
    # Born 1973-09-02, so 27 on 2000-09-15.
    assert row(table(), SKATER, 20002001, "NHL")["age_at_season"] == 27
    lines = landing_lines(page(SKATER), FETCHED, raw_key(SKATER)).rows
    unknown = player_league_seasons(pl.DataFrame(lines, schema=LINE_SCHEMA), players(GOALIE))
    assert unknown["age_at_season"].null_count() == unknown.height


def test_a_season_counts_as_public_on_july_1_after_it() -> None:
    frame = table()
    assert row(frame, SKATER, 20002001, "NHL")["observed_utc"] == datetime(2001, 7, 1, tzinfo=UTC)
    assert season_lines_public(20152016, "NHL") == datetime(2016, 7, 1, tzinfo=UTC)
    expected = [
        season_lines_public(season, abbrev)
        for season, abbrev in frame.select("season", "league_abbrev").rows()
    ]
    assert frame["observed_utc"].to_list() == expected


def test_the_late_leagues_count_as_public_on_october_1() -> None:
    # The Australian league plays from April to September, whichever calendar year the NHL's label
    # gives it, and the World Cup of Hockey in August and September.
    for abbrev in ("AIHL", "Australia", " aihl ", "WCup", "W-Cup"):
        assert season_lines_public(20172018, abbrev) == datetime(2018, 10, 1, tzinfo=UTC)
    assert season_lines_public(20172018, "Austria") == datetime(2018, 7, 1, tzinfo=UTC)


def test_the_late_nhl_seasons_count_as_public_from_their_end() -> None:
    # The 2020 playoffs ended on September 28, 2020 and the 2021 ones on July 7, 2021.
    for abbrev in ("NHL", "AHL", "AIHL"):
        assert season_lines_public(20192020, abbrev) == datetime(2020, 10, 1, tzinfo=UTC)
    assert season_lines_public(20202021, "NHL") == datetime(2021, 7, 9, tzinfo=UTC)
    assert season_lines_public(20202021, "AIHL") == datetime(2021, 10, 1, tzinfo=UTC)
    assert season_lines_public(20212022, "NHL") == datetime(2022, 7, 1, tzinfo=UTC)


def test_a_season_not_over_when_the_page_was_fetched_is_dropped() -> None:
    # A page fetched a second before July 1, 2010 has 2009-10 only in part.
    before = datetime(2010, 6, 30, 23, 59, 59, tzinfo=UTC)
    lines = landing_lines(page(SKATER), before, raw_key(SKATER))
    assert lines.partial == 5  # two lines of 2009-10, two of 2010-11, one of 2011-12
    assert max(line["season"] for line in lines.rows) == 20082009
    # From July 1 on, 2009-10 is over.
    on = landing_lines(page(SKATER), datetime(2010, 7, 1, tzinfo=UTC), raw_key(SKATER))
    assert on.partial == 3
    assert max(line["season"] for line in on.rows) == 20092010


def test_a_page_without_season_totals_has_no_lines() -> None:
    lines = landing_lines(b'{"playerId": 8400001}', FETCHED, "k")
    assert (lines.player_id, lines.rows, lines.other_game_types, lines.partial) == (
        NO_PAGE,
        [],
        0,
        0,
    )


def test_build_reads_the_newest_page_and_reports_a_player_without_one(tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    store_pages(store, SKATER, GOALIE)
    frame, report = build(store, players(SKATER, GOALIE, NO_PAGE))
    assert frame.equals(table())
    assert (report.players, report.pages, report.without_page) == (3, 2, [NO_PAGE])
    assert (report.lines, report.other_game_types, report.partial, report.rows) == (50, 1, 0, 47)
    assert set(frame["raw_key"].to_list()) == {raw_key(SKATER), raw_key(GOALIE)}


def test_build_refuses_a_page_of_another_player(tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    meta = {"fetched_utc": FETCHED.isoformat()}
    store.put("nhl", f"player-landing/{GOALIE}/20260928T133000Z", page(SKATER), meta)
    with pytest.raises(ValueError, match=f"landing page of player {SKATER}"):
        build(store, players(GOALIE))


def test_schema_rejects_a_wrong_time_league_or_game_type() -> None:
    frame = table()
    PlayerLeagueSeasons.validate(frame)
    for change in (
        pl.col("observed_utc") - pl.duration(days=1),
        pl.col("league").str.to_lowercase(),
        pl.lit(1, pl.Int8).alias("game_type"),
        pl.lit(20002002, pl.Int32).alias("season"),
    ):
        with pytest.raises(pandera.errors.SchemaError):
            PlayerLeagueSeasons.validate(frame.with_columns(change))
    with pytest.raises(pandera.errors.SchemaError):
        PlayerLeagueSeasons.validate(pl.concat([frame, frame.head(1)]))  # a key twice
