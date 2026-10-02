"""Two real landing pages trimmed to the fields the player_league_seasons parser reads (#98), all
of open seasons: a skater (8467844, 1991-92 to 2011-12) and a goalie (8476803, 2003-04 to 2012-13).

The skater has two NHL teams in 2000-01 and in 2009-10, a 2004-05 line under Swiss (the NL), and
one World Cup of Hockey line of game type 6. The goalie has two ECHL teams in 2012-13, and only
his NHL line has goals and assists.
"""

from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl

from nhl_edge.ingest.player_seasons import LINE_SCHEMA, landing_lines, player_league_seasons
from nhl_edge.lake.raw import RawStore
from nhl_edge.lake.schemas import Players, dtypes

FIXTURES = Path(__file__).parent / "fixtures" / "nhl_api"
SKATER = 8467844
GOALIE = 8476803
NO_PAGE = 8400001  # a player in players whose page is not cached
BIRTH_DATES = {SKATER: date(1973, 9, 2), GOALIE: date(1987, 4, 9), NO_PAGE: date(1990, 1, 1)}
# When the backfill fetched the two pages.
FETCHED = datetime(2026, 9, 28, 13, 30, tzinfo=UTC)
STAMP = "20260928T133000Z"


def page(player_id: int) -> bytes:
    return (FIXTURES / f"landing_{player_id}_trimmed.json").read_bytes()


def raw_key(player_id: int) -> str:
    return f"nhl/player-landing/{player_id}/{STAMP}"


def players(*ids: int) -> pl.DataFrame:
    """players rows for the given ids, with their birth dates."""
    rows = [
        {
            "player_id": player_id,
            "name": f"Player {player_id}",
            "birth_date": BIRTH_DATES[player_id],
            "shoots": "L",
            "draft_year": None,
            "draft_overall": None,
            "fetched_utc": FETCHED,
            "raw_key": raw_key(player_id),
        }
        for player_id in ids
    ]
    return Players.validate(pl.DataFrame(rows, schema=dtypes(Players)))


def table(fetched_utc: datetime = FETCHED, *ids: int) -> pl.DataFrame:
    """player_league_seasons of the given players' pages (both by default) as fetched then."""
    ids = ids or (SKATER, GOALIE)
    rows = [
        row
        for player_id in ids
        for row in landing_lines(page(player_id), fetched_utc, raw_key(player_id)).rows
    ]
    return player_league_seasons(pl.DataFrame(rows, schema=LINE_SCHEMA), players(*ids))


def store_pages(store: RawStore, *ids: int) -> None:
    """Cache the given players' pages as the ingest stores them."""
    for player_id in ids:
        meta = {"fetched_utc": FETCHED.isoformat(), "status": 200}
        store.put("nhl", f"player-landing/{player_id}/{STAMP}", page(player_id), meta)
