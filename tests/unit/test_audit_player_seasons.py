from pathlib import Path

import polars as pl
import pytest
from player_season_fixtures import (
    GOALIE,
    NO_PAGE,
    SKATER,
    debuts,
    players,
    store_pages,
    table,
)
from test_reference import OPENING
from typer.testing import CliRunner

from nhl_edge.audit import player_seasons as audit
from nhl_edge.cli import app
from nhl_edge.ingest.player_seasons import LINE_SCHEMA, player_league_seasons
from nhl_edge.lake.raw import RawStore
from nhl_edge.lake.tables import Lake


def test_season_report_counts_players_and_lines_in_the_top_leagues() -> None:
    frame = table()
    leagues = audit.top_leagues(frame, 3)
    # 20 NHL lines, 8 in NCAA conferences (CCHA, WCHA), 3 IHL.
    assert leagues == ["NHL", "NCAA", "IHL"]
    report = audit.season_report(frame, leagues)
    rows = {row[0]: row[1:] for row in report.rows()}
    # 1998-99: the skater's NHL, WC-Q, WCHA and two IHL lines (regular season and playoffs).
    assert rows[19981999] == (1, 5, 1, 1, 2, 1)
    # 2011-12: both players in the NHL, and the goalie in the CCHA.
    assert rows[20112012] == (2, 3, 2, 1, 0, 0)
    assert report.columns == ["season", "players", "lines", "NHL", "NCAA", "IHL", "other"]


def test_markdown_report_shows_counts_only() -> None:
    frame = table()
    leagues = audit.top_leagues(frame, 3)
    text = audit.markdown_report(audit.season_report(frame, leagues), leagues)
    lines = text.splitlines()
    assert lines[0] == "| Season | Players | Lines | NHL | NCAA | IHL | Other leagues |"
    assert "| 20122013 | 1 | 2 | 0 | 0 | 0 | 2 |" in lines
    assert len(lines) == 2 + frame["season"].n_unique()


def test_one_league_under_two_abbreviations_is_counted() -> None:
    frame = table()
    assert audit.shared_leagues(frame) == 0
    # The skater's 2004-05 season listed again as NL beside Swiss.
    swiss = frame.filter((pl.col("player_id") == SKATER) & (pl.col("league_abbrev") == "Swiss"))
    twice = pl.concat([frame, swiss.with_columns(league_abbrev=pl.lit("NL"))])
    assert audit.shared_leagues(twice) == 1


def test_a_player_without_a_cached_page_is_a_problem(tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    store_pages(store, SKATER, GOALIE)
    assert audit.problems(players(SKATER, GOALIE), store) == []
    found = audit.problems(players(SKATER, GOALIE, NO_PAGE), store)
    assert len(found) == 1
    assert found[0].startswith(
        f"1 players in players have no landing page in the raw cache, e.g. {NO_PAGE}"
    )


runner = CliRunner()


def test_audit_report_has_the_player_seasons_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    lake = Lake()
    lake.write("games", OPENING)
    lake.write("players", players(SKATER, GOALIE, NO_PAGE))
    lake.write("player_league_seasons", table())
    store_pages(RawStore(), SKATER, GOALIE)
    result = runner.invoke(app, ["audit", "report", "--as-of", "2010-10-08"])
    assert result.exit_code == 0, result.output
    early = (tmp_path / "reports" / "audit" / "2010-10-08.md").read_text()
    # The goalie's lines wait for his first boxscore, in February 2012.
    assert "| 20092010 | 1 | 1 | 1 | 0 |" in early
    result = runner.invoke(app, ["audit", "report", "--as-of", "2012-07-02"])
    assert result.exit_code == 0, result.output
    text = (tmp_path / "reports" / "audit" / "2012-07-02.md").read_text()
    section = text.split("\n## Player league seasons\n")[1].split("\n## ")[0]
    # Seasons public by the audit date only: 2011-12 is the last, from July 1, 2012.
    assert "| 20092010 | 2 | 2 | 1 | 1 |" in section
    assert "| 20112012 | 2 | 3 | 2 | 1 |" in section
    assert "| 20122013 |" not in section
    assert "0 player-season-game types have lines of one league under two" in section
    assert f"have no landing page in the raw cache, e.g. {NO_PAGE}" in section
    # The table holds counts only: players, lines and lines per league.
    counts = section.split("\n\n")[1]
    assert counts.startswith("| Season | Players | Lines | NHL | NCAA |")
    assert not {"goals", "assists", "points"} & set(counts.lower().replace("|", " ").split())


def test_the_section_leaves_out_the_one_time_test_season_and_the_live_seasons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    later = pl.DataFrame(
        [
            (SKATER, season, league, 2, 10, 1, 1, "k")
            for season in (20242025, 20252026, 20262027)
            for league in ("NHL", "KHL")
        ],
        schema=LINE_SCHEMA,
        orient="row",
    )
    lake = Lake()
    lake.write("games", OPENING)
    lake.write("players", players(SKATER, GOALIE))
    lake.write(
        "player_league_seasons",
        pl.concat([table(), player_league_seasons(later, players(SKATER), debuts(SKATER))]),
    )
    store_pages(RawStore(), SKATER, GOALIE)
    result = runner.invoke(app, ["audit", "report", "--as-of", "2027-07-02"])
    assert result.exit_code == 0, result.output
    text = (tmp_path / "reports" / "audit" / "2027-07-02.md").read_text()
    section = text.split("\n## Player league seasons\n")[1].split("\n## ")[0]
    assert "| 20242025 | 1 | 2 |" in section
    assert "| 20252026 |" not in section
    assert "| 20262027 |" not in section
    seasons = pl.Series([20092010, 20242025, 20252026, 20262027, 20272028])
    assert pl.select(audit.shown(pl.lit(seasons))).to_series().to_list() == [
        True,
        True,
        False,
        False,
        False,
    ]
