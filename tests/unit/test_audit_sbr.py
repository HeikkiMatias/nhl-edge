from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl
import pytest
from test_reference import OPENING
from test_sbr import OLD, OLD_SCHEDULE, results, results_empty
from typer.testing import CliRunner

from nhl_edge.audit import games as game_audit
from nhl_edge.audit import sbr as sbr_audit
from nhl_edge.cli import app
from nhl_edge.ingest.sbr import SeasonReport, american_to_decimal, match_season, parse_season
from nhl_edge.lake.raw import RawStore
from nhl_edge.lake.tables import Lake

NO_LISTINGS = pl.DataFrame(schema=game_audit.LISTED_SCHEMA)


def odds(rows: list[tuple[int, int, str, str, str, float | None, int]]) -> pl.DataFrame:
    """sbr_odds rows as (season, game_id, market, quote, side, line, American price), with the
    columns the audit reads."""
    return pl.DataFrame(
        [
            {
                "season": season,
                "game_id": game_id,
                "market": market,
                "quote": quote,
                "side": side,
                "line": line,
                "price_american": price,
                "price_decimal": american_to_decimal(price),
            }
            for season, game_id, market, quote, side, line, price in rows
        ],
        schema={
            "season": pl.Int32,
            "game_id": pl.Int64,
            "market": pl.String,
            "quote": pl.String,
            "side": pl.String,
            "line": pl.Float64,
            "price_american": pl.Int32,
            "price_decimal": pl.Float64,
        },
    )


def moneyline(
    season: int, game_id: int, quote: str, home: int, away: int
) -> list[tuple[int, int, str, str, str, float | None, int]]:
    return [
        (season, game_id, "h2h", quote, "home", None, home),
        (season, game_id, "h2h", quote, "away", None, away),
    ]


def puck_line(
    season: int, game_id: int, home_line: float
) -> list[tuple[int, int, str, str, str, float | None, int]]:
    return [
        (season, game_id, "spreads", "close", "home", home_line, -110),
        (season, game_id, "spreads", "close", "away", -home_line, -110),
    ]


def problems(
    reports: list[SeasonReport],
    frame: pl.DataFrame | None = None,
    listed: pl.DataFrame = NO_LISTINGS,
) -> list[str]:
    frame = odds([]) if frame is None else frame
    lines = sbr_audit.moneylines(frame)
    conflicts = sbr_audit.puck_line_conflicts(frame, lines)
    return sbr_audit.problems(reports, listed, lines, sbr_audit.moves(lines), conflicts)


def stored_2010(tmp_path: Path) -> RawStore:
    store = RawStore(tmp_path / "raw")
    store.put("sbr", "20102011/20260929T120000Z", OLD, {"fetched_utc": "x"})
    return store


def test_the_join_is_rerun_from_the_stored_page(tmp_path: Path) -> None:
    # Without the 2010-12-31 game in the schedule, SBR's NYR and STL row matches nothing.
    schedule = OLD_SCHEDULE.filter(pl.col("game_id") != 2010020500)
    reports = sbr_audit.join_reports(
        stored_2010(tmp_path), schedule, results_empty(schedule), [20102011, 20112012]
    )
    assert [r.season for r in reports] == [20102011]  # no stored page for 2011-12
    row = sbr_audit.join_report(reports, NO_LISTINGS).row(0, named=True)
    assert (row["sbr_games"], row["matched"], row["playoffs"], row["unmatched"]) == (4, 2, 1, 1)
    assert (row["nhl_games"], row["without_sbr"]) == (2, 0)
    assert problems(reports) == [
        "20102011: 1 SBR games match no NHL game, e.g. 2010-12-31 NYR and STL"
    ]


def test_an_unmatched_row_the_listings_name_as_a_playoff_game_is_not_a_problem(
    tmp_path: Path,
) -> None:
    schedule = OLD_SCHEDULE.filter(pl.col("game_id") != 2010020500)
    reports = sbr_audit.join_reports(
        stored_2010(tmp_path), schedule, results_empty(schedule), [20102011]
    )
    # As in 2020-21: a playoff game before the regular season's last date.
    listed = pl.DataFrame(
        [
            {
                "game_id": 2010030111,
                "season": 20102011,
                "game_type": 3,
                "game_date": date(2010, 12, 31),
                "start_utc": datetime(2011, 1, 1, 0, tzinfo=UTC),
                "home": "STL",
                "away": "NYR",
                "game_state": "OFF",
                "listed_key": "nhl/schedule/x",
            }
        ],
        schema=game_audit.LISTED_SCHEMA,
    )
    row = sbr_audit.join_report(reports, listed).row(0, named=True)
    assert (row["playoffs"], row["unmatched"]) == (2, 0)
    assert problems(reports, listed=listed) == []


def test_nhl_games_without_sbr_prices_and_score_mismatches_are_problems(tmp_path: Path) -> None:
    # A fourth scheduled game SBR does not list.
    extra = OLD_SCHEDULE.head(1).with_columns(
        game_id=pl.lit(2010020700, pl.Int64), game_date=pl.lit(date(2011, 1, 3))
    )
    schedule = pl.concat([OLD_SCHEDULE, extra])
    scores = results([(2010020001, 3, 4), (2010020500, 3, 1), (2010020600, 4, 2)], schedule)
    reports = sbr_audit.join_reports(stored_2010(tmp_path), schedule, scores, [20102011])
    assert problems(reports) == [
        "20102011: SBR prices for 3 of 4 games (75.0%)",
        "20102011: 1 SBR final scores differ from the NHL's, e.g. 2010020500 (SBR 3-2, NHL 3-1)",
    ]


def test_vig_per_season_and_moneylines_below_100_percent() -> None:
    frame = odds(
        [
            *moneyline(20172018, 2017020001, "open", -110, -110),
            *moneyline(20172018, 2017020001, "close", -110, -110),
            *moneyline(20182019, 2018020001, "open", -110, -110),
            *moneyline(20182019, 2018020001, "close", -105, -105),
            # Both sides at plus money, as 2021020945's opener: de-vigging refuses it.
            *moneyline(20182019, 2018020002, "open", 165, 162),
            *moneyline(20182019, 2018020002, "close", -105, -105),
        ]
    )
    lines = sbr_audit.moneylines(frame)
    assert lines.height == 6
    assert lines.filter(pl.col("game_id") == 2018020002, pl.col("quote") == "open")[
        "p_home"
    ].to_list() == [None]
    vig = {row["season"]: row for row in sbr_audit.vig_report(lines).iter_rows(named=True)}
    assert vig[20172018]["close_median"] == pytest.approx(2 / 1.909090909 - 1)
    assert vig[20182019]["close_median"] == pytest.approx(2 / 1.952380952 - 1)
    assert (vig[20182019]["games"], vig[20182019]["open_below_100"]) == (2, 1)
    moved = sbr_audit.moves(lines)
    # The refused opener leaves its game out of the moves.
    assert moved["game_id"].to_list() == [2017020001, 2018020001]
    assert problems([], frame) == [
        "20182019: 1 moneylines sum below 100%, which de-vigging refuses, "
        "e.g. 2018020002 open (home +165, away +162)",
        "20182019: median closing vig 2.4%, against 4.8% in 20172018",
    ]


def test_a_big_move_from_open_to_close_is_a_problem() -> None:
    frame = odds(
        [
            # -1010 against +705, as 2021020648's opener, then even.
            *moneyline(20212022, 2021020648, "open", -1010, 705),
            *moneyline(20212022, 2021020648, "close", -105, -105),
            # The favourite changes, by less than 15 points.
            *moneyline(20212022, 2021020650, "open", -120, 100),
            *moneyline(20212022, 2021020650, "close", 110, -130),
            *moneyline(20212022, 2021020660, "open", -150, 130),
            *moneyline(20212022, 2021020660, "close", -150, 130),
        ]
    )
    lines = sbr_audit.moneylines(frame)
    moved = sbr_audit.moves(lines)
    row = sbr_audit.move_report(moved).row(0, named=True)
    assert (row["games"], row["big"], row["favourite_changed"]) == (3, 1, 1)
    assert row["unchanged"] == pytest.approx(1 / 3)
    assert problems([], frame) == [
        "20212022: 1 games whose de-vigged home probability moves more than 15 points from open "
        "to close, e.g. 2021020648 (home 88% to 50%)"
    ]


def test_a_clear_favourite_at_plus_one_and_a_half_is_a_problem() -> None:
    frame = odds(
        [
            # A clear home favourite with +1.5: one of the two is on the wrong team.
            *moneyline(20152016, 2015020761, "close", -250, 210),
            *puck_line(20152016, 2015020761, 1.5),
            *moneyline(20152016, 2015020762, "close", -250, 210),
            *puck_line(20152016, 2015020762, -1.5),
            # Near even the puck line's side often differs from the moneyline favourite.
            *moneyline(20152016, 2015020763, "close", -125, 105),
            *puck_line(20152016, 2015020763, 1.5),
        ]
    )
    lines = sbr_audit.moneylines(frame)
    conflicts = sbr_audit.puck_line_conflicts(frame, lines)
    assert conflicts["game_id"].to_list() == [2015020761]
    assert sbr_audit.puck_line_report(frame, conflicts).row(0) == (20152016, 3, 1)
    assert problems([], frame) == [
        "20152016: 1 games whose closing moneyline favourite (55% or more) is +1.5 on the "
        "closing puck line, e.g. 2015020761 (home 69%, home puck line +1.5)"
    ]


def test_prices_of_the_market_validation_season_are_not_read() -> None:
    assert sbr_audit.price_seasons([20182019, 20212022, 20222023]) == [20182019, 20212022]


runner = CliRunner()


def test_audit_report_has_the_sbr_section(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    lake, store = Lake(), RawStore()
    lake.write("games", OPENING)
    # The lake's schedule is public a day before each start (ADR 0005).
    schedule = OLD_SCHEDULE.with_columns(observed_utc=pl.col("start_utc") - pl.duration(days=1))
    lake.write("schedule", schedule)
    store.put("sbr", "20102011/20260929T120000Z", OLD, {"fetched_utc": "x"})
    frame, _ = match_season(parse_season(OLD, 20102011), schedule, results_empty(schedule), "sbr/k")
    lake.write("sbr_odds", frame)
    # With no listings stored, 2010-11 is over from July 1, 2011.
    result = runner.invoke(app, ["audit", "report", "--as-of", "2011-07-01"])
    assert result.exit_code == 0, result.output
    text = (tmp_path / "reports" / "audit" / "2011-07-01.md").read_text()
    section = text.split("\n## SBR odds\n")[1].split("\n## ")[0]
    assert "| 20102011 | 4 | 3 | 1 | 0 | 3 | 0 | 100.0% | 0 |" in section
    assert "**Moneyline vig**" in section
    assert "leave out" not in section  # no market validation season in the report
    # Before then no SBR season is over, and the section says so.
    early = runner.invoke(app, ["audit", "report", "--as-of", "2010-10-07"])
    assert early.exit_code == 0, early.output
    text = (tmp_path / "reports" / "audit" / "2010-10-07.md").read_text()
    assert "- no SBR odds: run nhl odds sbr" in text
