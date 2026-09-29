"""Point-in-time rules for the suspect SBR openers (#56). The criteria read the close, public only
at the start (ADR 0006), so every row is observed at its game's start, even though E2 assumes the
opener itself public hours earlier. known_at never shows the list before a game starts."""

from datetime import timedelta

import polars as pl
import pytest
from sbr_rows import ODDS

from nhl_edge.ingest.sbr_suspect import load_suspect_openers, suspect_openers
from nhl_edge.lake.schemas import SbrSuspectOpeners
from nhl_edge.lake.tables import known_at


def test_every_row_is_observed_at_its_game_start() -> None:
    listed = suspect_openers(ODDS)
    assert (listed["observed_utc"] == listed["start_utc"]).all()
    opens = ODDS.filter(pl.col("quote") == "open", pl.col("market") == "h2h")
    assert (opens["assumed_available_utc"] < opens["start_utc"]).all()


def test_known_at_shows_no_row_before_its_game_starts() -> None:
    listed = load_suspect_openers()
    for start in listed["start_utc"]:
        before = known_at(listed, start)
        assert (before["start_utc"] < start).all()
        assert (
            not known_at(listed, start - timedelta(microseconds=1))
            .filter(pl.col("start_utc") == start)
            .height
        )


def test_the_schema_rejects_a_row_observed_before_the_start() -> None:
    early = suspect_openers(ODDS).with_columns(
        observed_utc=pl.col("start_utc") - timedelta(hours=9)
    )
    with pytest.raises(Exception, match="observed_at_start"):
        SbrSuspectOpeners.validate(early)
