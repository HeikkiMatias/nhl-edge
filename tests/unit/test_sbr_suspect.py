import polars as pl
import pytest
from sbr_rows import ODDS

from nhl_edge.ingest.sbr_suspect import (
    CSV_DATETIME,
    SEASONS,
    SUSPECT_FILE,
    load_suspect_openers,
    suspect_openers,
    write_suspect_openers,
)
from nhl_edge.lake.schemas import SBR_SUSPECT_FLAGS, SbrSuspectOpeners


def flags(frame: pl.DataFrame) -> dict[int, set[str]]:
    return {
        row["game_id"]: {flag for flag in SBR_SUSPECT_FLAGS if row[flag]}
        for row in frame.iter_rows(named=True)
    }


def test_each_criterion_lists_its_games() -> None:
    assert flags(suspect_openers(ODDS)) == {
        2018020002: {"big_move", "swapped"},
        2018020003: {"big_move", "extreme_open"},
        2018020005: {"below_100"},
        2018020006: {"swapped"},
        2018020007: {"big_move"},
        2018020008: {"extreme_open"},
    }


def test_each_game_keeps_its_evidence() -> None:
    listed = SbrSuspectOpeners.validate(suspect_openers(ODDS))
    swapped = listed.row(by_predicate=pl.col("game_id") == 2018020002, named=True)
    assert (swapped["open_home"], swapped["open_away"]) == (-160, 140)
    assert (swapped["close_home"], swapped["close_away"]) == (160, -180)
    assert swapped["p_open"] > 0.5 > swapped["p_close"]
    assert swapped["move"] > 0.15
    assert swapped["unswapped_gap"] < 0.05
    assert swapped["close_home_line"] == 1.5
    below = listed.row(by_predicate=pl.col("game_id") == 2018020005, named=True)
    assert below["p_open"] is None and below["move"] is None
    no_close = listed.row(by_predicate=pl.col("game_id") == 2018020008, named=True)
    assert no_close["close_home"] is None and no_close["p_close"] is None
    assert (listed["raw_key"] == "sbr/20182019/20260929T120000Z").all()


def test_the_committed_list_loads_and_has_the_reviewed_games() -> None:
    listed = load_suspect_openers()
    assert set(listed["season"]) <= set(range(20102011, 20222023, 10_001))
    found = flags(listed)
    assert "swapped" in found[2018020006]  # NYR -160 / NSH +140, closing +160 / -180
    assert "extreme_open" in found[2021020648]  # EDM -1010 / MIN +705, closing -105 / -105
    assert "extreme_open" in found[2018020655]  # CHI +975 / CGY -1787
    assert listed.height == 40


def test_the_committed_list_is_as_written() -> None:
    # The file is generated; a hand edit would drift from what the module writes.
    text = load_suspect_openers().write_csv(datetime_format=CSV_DATETIME)
    assert SUSPECT_FILE.read_text() == text


def test_a_rebuild_refuses_a_lake_missing_a_covered_season() -> None:
    # ODDS has only 2018-19 of the covered seasons: rebuilding from it would drop the others.
    before = SUSPECT_FILE.read_bytes()
    with pytest.raises(ValueError, match="sbr_odds has no prices for") as refused:
        write_suspect_openers(ODDS)
    assert "20102011" in str(refused.value) and "20182019" not in str(refused.value)
    assert SUSPECT_FILE.read_bytes() == before


def test_the_list_covers_the_phase_one_seasons() -> None:
    assert tuple(range(20102011, 20222023, 10_001)) == SEASONS
