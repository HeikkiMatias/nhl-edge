"""Settling the paper bets (#165): the result on the full game, the profit, and the CLV against
Pinnacle's closing proxy, or why there is none."""

from datetime import UTC, datetime, timedelta
from typing import Any

import polars as pl
import predict_fixtures as pf
import pytest

from nhl_edge.lake.schemas import PaperSettlements, dtypes
from nhl_edge.live import predict as lp
from nhl_edge.live import settle as ls
from nhl_edge.market import closing
from nhl_edge.market.devig import fair_probabilities

PRE7 = datetime(2026, 10, 7, 22, 45, 30, tzinfo=UTC)  # 18:45 ET, 45 minutes before 053 and 054
NOW = datetime(2026, 10, 8, 9, tzinfo=UTC)
VERSIONS = {"code_version": "settle-20261008-abc1234"}


def ledger() -> pl.DataFrame:
    """The fixture day's ledger: a home bet on each of its three games."""
    inputs = pf.day()
    decided = lp.ledger(lp.decide(inputs), inputs)
    assert decided["bet"].all()
    return decided


def games(**moved: timedelta) -> pl.DataFrame:
    """The three games final: WSH win 3-2 after a shootout, WPG lose 1-4, ANA win 4-1; a game
    named in moved was played that much after the ledger's start."""
    scores = {2026020053: (3, 2), 2026020054: (1, 4), 2026020055: (4, 1)}
    rows = []
    for game_id, (home_score, away_score) in scores.items():
        start = pf.GAMES[game_id][2] + moved.get(f"g{game_id}", timedelta())
        rows.append(
            {
                "game_id": game_id,
                "start_utc": start,
                "home_score": home_score,
                "away_score": away_score,
                "observed_utc": start + timedelta(hours=12),
            }
        )
    return pl.DataFrame(rows, schema_overrides={"home_score": pl.Int16, "away_score": pl.Int16})


def odds(*extra: list[dict[str, Any]], flagged: datetime = NOW) -> pl.DataFrame:
    """The day's quotes, a pre7 snapshot of 053 and 054 at Pinnacle, as the lake keeps them
    with their game_id, and the closing proxies marked as of flagged."""
    rows = pl.concat(
        [
            pf.day_quotes(),
            pf.quotes(
                pf.quote(PRE7, 2026020053, "pinnacle", 2.0, 1.9, slot="pre7"),
                pf.quote(PRE7, 2026020054, "pinnacle", 1.8, 2.1, slot="pre7"),
                *extra,
            ),
        ]
    ).with_columns(game_id=pl.col("event_id").str.strip_prefix("e").cast(pl.Int64))
    return closing.flag(rows, flagged)


def fair_home(home: float, away: float) -> float:
    return float(fair_probabilities([[home, away]])[0, 0])


def test_each_bet_is_settled_on_the_full_game_and_valued_against_the_close() -> None:
    settled = ls.settle(ledger(), games(), odds(), NOW, VERSIONS)
    rows = {row["game_id"]: row for row in settled.iter_rows(named=True)}
    wsh, wpg, ana = rows[2026020053], rows[2026020054], rows[2026020055]
    # A shootout win is a win (hard rule 2), and a loss costs the stake.
    assert (wsh["won"], wpg["won"], ana["won"]) == (True, False, True)
    assert wsh["profit"] == pytest.approx(wsh["stake"] * (2.1 - 1))
    assert wpg["profit"] == pytest.approx(-wpg["stake"])
    # CLV = price taken x fair closing probability of the side - 1, at Pinnacle's pre7 pair.
    assert wsh["close_status"] == closing.PROXY
    assert wsh["close_snapshot_utc"] == PRE7
    assert wsh["clv"] == pytest.approx(2.1 * fair_home(2.0, 1.9) - 1)
    assert wpg["clv"] == pytest.approx(1.9 * fair_home(1.8, 2.1) - 1)
    # The fair move: the close's fair probability over the decision's.
    assert wsh["fair_move"] == pytest.approx(fair_home(2.0, 1.9) / fair_home(2.1, 1.8) - 1)
    # ANA had no Pinnacle quote in its last 90 minutes though the 21:45 slot was due.
    assert ana["close_status"] == closing.MISSING
    assert ana["clv"] is None and ana["profit"] > 0
    assert set(settled["code_version"]) == {VERSIONS["code_version"]}
    assert set(settled["status"]) == {ls.SETTLED}


def test_the_close_does_not_wait_for_the_stored_flag() -> None:
    # The odds replay ran before the games started, so nothing is flagged yet: settled later,
    # each bet still gets its close and CLV, as of the settlement's clock (Codex on #190).
    early = odds(flagged=PRE7)
    assert not early["is_closing_proxy"].any()
    settled = ls.settle(ledger(), games(), early, NOW, VERSIONS)
    expected = ls.settle(ledger(), games(), odds(), NOW, VERSIONS)
    assert settled.drop("settled_utc").equals(expected.drop("settled_utc"))
    assert settled["clv"].is_not_null().sum() == 2


def test_a_game_not_final_is_not_settled() -> None:
    settled = ls.settle(
        ledger(), games().filter(pl.col("game_id") != 2026020055), odds(), NOW, {**VERSIONS}
    )
    assert settled["game_id"].to_list() == [2026020053, 2026020054]


def test_no_ledger_yet_settles_nothing() -> None:
    # The nightly runs before the season's first decision too.
    settled = ls.settle(ledger().clear(), games(), odds(), NOW, VERSIONS)
    assert settled.is_empty()
    assert settled.columns == list(dtypes(PaperSettlements))


def test_a_postponed_game_voids_its_bet() -> None:
    settled = ls.settle(ledger(), games(g2026020054=timedelta(days=2)), odds(), NOW, VERSIONS)
    row = settled.filter(pl.col("game_id") == 2026020054).row(0, named=True)
    assert row["status"] == ls.VOID
    assert (row["profit"], row["won"], row["home_win"], row["clv"]) == (0.0, None, None, None)


def test_a_close_never_comes_from_the_decision_snapshot_or_before() -> None:
    # A ledger decided after the pre7 snapshot (as if it were taken in the window): that
    # snapshot is no close for its bets.
    late = ledger().with_columns(decision_snapshot_utc=pl.lit(PRE7 + timedelta(minutes=1)))
    settled = ls.settle(late, games(), odds(), NOW, VERSIONS)
    assert set(settled["close_status"]) == {closing.MISSING}
    assert settled["clv"].is_null().all()


def test_a_day_without_any_proxy_settles_with_no_clv() -> None:
    quotes = pf.day_quotes().with_columns(
        game_id=pl.col("event_id").str.strip_prefix("e").cast(pl.Int64)
    )
    settled = ls.settle(ledger(), games(), closing.flag(quotes, NOW), NOW, VERSIONS)
    assert settled.height == 3
    assert settled["clv"].is_null().all()
    assert set(settled["close_status"]) == {closing.MISSING}


def test_nhl_live_settle_rebuilds_the_table_from_the_ledgers(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from nhl_edge.cli import app
    from nhl_edge.lake.tables import Lake

    tables = {"paper_ledger": ledger(), "games": games(), "odds_snapshots": odds()}
    written: dict[str, pl.DataFrame] = {}

    def read(self: Lake, table: str, seasons: Any = None) -> pl.DataFrame:
        return tables[table]

    def replace(self: Lake, table: str, frame: pl.DataFrame) -> list[str]:
        written[table] = frame
        return []

    monkeypatch.setattr(Lake, "read", read)
    monkeypatch.setattr(Lake, "replace", replace)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["live", "settle"])
    assert result.exit_code == 0, result.output
    assert written["paper_settlements"].height == 3
    assert "3 of 3 bets settled, 2 with a CLV" in result.output
    assert "missing: 1" in result.output and "proxy: 2" in result.output
