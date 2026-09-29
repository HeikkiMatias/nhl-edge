"""Point-in-time rules for actual lineups (hard rule 9). Backtest lineups come only from earlier
games' boxscores:
- A game's own lineup is public at 10:00 UTC the morning after its game date, like its result
  (ADR 0003, ADR 0004), so no prediction for a game sees who dressed for it.
- Lineups are read from boxscores and never from cached rosters, which are an after-the-fact view
  of a season.
- Scoring stats that post-game corrections change most (goals, assists, points, plus-minus,
  penalty minutes, shots, decision) stay out. So does the position code, which is the position
  listed today. The column set is locked here. A corrected time on ice is a small look-ahead that
  ADR 0004 accepts and #30 measures.
"""

from pathlib import Path
from typing import Any, cast

import polars as pl
import pytest
from feed_fixtures import (
    MTL_ARI,
    OPENING_WEEK_GAMES,
    assert_public_the_morning_after,
    feed,
    games_row,
    parsed_feeds,
)

from nhl_edge.ingest.nhl_api import NhlApi, Response
from nhl_edge.ingest.nhl_ingest import Ingest
from nhl_edge.lake.schemas import ActualLineups, dtypes
from nhl_edge.lake.tables import FEED_TABLES, TABLES, Lake

COLUMNS = [
    "game_id",
    "season",
    "game_date",
    "team",
    "is_home",
    "player_id",
    "role",
    "sweater_number",
    "starting_goalie",
    "toi_s",
    "observed_utc",
    "raw_key",
]


@pytest.mark.parametrize("game_id", (*OPENING_WEEK_GAMES, MTL_ARI))
def test_a_game_never_sees_its_own_lineup(game_id: int) -> None:
    assert_public_the_morning_after(parsed_feeds(game_id)["actual_lineups"], game_id)


def test_lineups_hold_no_corrected_stats_and_no_listed_position() -> None:
    assert list(dtypes(ActualLineups)) == COLUMNS


class FeedsOnly:
    """An NHL API that serves a game's three feeds and fails on anything else."""

    def __init__(self) -> None:
        self.kinds: list[str] = []

    def _serve(self, kind: str, season: int, game_id: int) -> Response:
        self.kinds.append(kind)
        key = f"nhl/{kind}/{season}/{game_id}/20260928T130000Z"
        return Response(feed(kind, game_id), key, fetched_utc=cast(Any, None), cached=True)

    def play_by_play(self, season: int, game_id: int) -> Response:
        return self._serve("play-by-play", season, game_id)

    def boxscore(self, season: int, game_id: int) -> Response:
        return self._serve("boxscore", season, game_id)

    def shift_chart(self, season: int, game_id: int) -> Response:
        return self._serve("shiftcharts", season, game_id)

    def roster(self, *args: object) -> Response:
        raise AssertionError("a lineup must never read a cached roster")

    def player_landing(self, *args: object) -> Response:
        raise AssertionError("lineups need no player landing pages")


def test_lineups_come_from_boxscores_never_rosters(tmp_path: Path) -> None:
    api = FeedsOnly()
    ingest = Ingest(
        api=cast(NhlApi, api),
        lake=Lake(tmp_path),
        supabase=None,
        feeds=True,
        players=False,
        echo=print,
    )
    frames: dict[str, list[pl.DataFrame]] = {
        table: [TABLES[table].empty()] for table in FEED_TABLES
    }
    ingest.game_feeds(games_row(MTL_ARI), frames)
    assert api.kinds == ["play-by-play", "boxscore", "shiftcharts"]
    lineups = pl.concat(frames["actual_lineups"])
    assert lineups.height == 40
    assert lineups["raw_key"].str.starts_with("nhl/boxscore/").all()
