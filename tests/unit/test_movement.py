"""Entry to close (#251): each predicted game's move from its decision pair to its close, and each
model's disagreement with the market at the decision."""

import math
from datetime import UTC, datetime

import polars as pl
import predict_fixtures as pf
import pytest

from nhl_edge.live import movement
from nhl_edge.live import predict as lp
from nhl_edge.live import report as lr
from nhl_edge.live import settle as ls
from nhl_edge.market import closing
from nhl_edge.market.devig import fair_probabilities

PRE7 = datetime(2026, 10, 7, 22, 45, 30, tzinfo=UTC)  # 18:45 ET, 45 minutes before 053 and 054
NOW = datetime(2026, 10, 8, 9, tzinfo=UTC)
WSH, WPG, ANA = 2026020053, 2026020054, 2026020055


def ledger() -> pl.DataFrame:
    inputs = pf.day()
    return lp.ledger(lp.decide(inputs), inputs)


def odds() -> pl.DataFrame:
    """The day's quotes and a pre7 snapshot of 053 and 054 at Pinnacle, as the lake keeps them."""
    rows = pl.concat(
        [
            pf.day_quotes(),
            pf.quotes(
                pf.quote(PRE7, WSH, "pinnacle", 2.0, 1.9, slot="pre7"),
                pf.quote(PRE7, WPG, "pinnacle", 1.8, 2.1, slot="pre7"),
            ),
        ]
    )
    return rows.with_columns(game_id=pl.col("event_id").str.strip_prefix("e").cast(pl.Int64))


def fair(home: float, away: float) -> float:
    return float(fair_probabilities([[home, away]])[0, 0])


def logit(p: float) -> float:
    return math.log(p / (1 - p))


def test_each_game_moves_from_its_decision_pair_to_its_close() -> None:
    decided = ledger()
    moves = movement.table(decided, ls.closes(odds(), NOW))
    rows = {row["game_id"]: row for row in moves.iter_rows(named=True)}
    wsh = rows[WSH]
    # The decision's fair pair, de-vigged as B0 is, from the ledger's own prices.
    assert wsh["p_decision"] == pytest.approx(fair(2.10, 1.80))
    p_b0, p_b3, p_blend = (
        decided.filter(pl.col("game_id") == WSH).select("p_b0", "p_b3", "p_blend").row(0)
    )
    assert wsh["p_decision"] == pytest.approx(p_b0)
    assert wsh["p_close"] == pytest.approx(fair(2.0, 1.9))
    assert wsh["move"] == pytest.approx(logit(fair(2.0, 1.9)) - logit(fair(2.10, 1.80)))
    assert wsh["d_B3"] == pytest.approx(logit(p_b3) - logit(p_b0))
    assert wsh["d_BLEND"] == pytest.approx(logit(p_blend) - logit(p_b0))
    # The proxy's lead, against the start the proxy was chosen by (Codex on #285).
    leads = lr.proxy_leads(decided, odds(), NOW)
    assert leads.filter(pl.col("game_id") == WSH)["lead_min"].item() == pytest.approx(44.5)
    assert rows[WPG]["move"] == pytest.approx(logit(fair(1.8, 2.1)) - logit(fair(1.90, 2.00)))
    # ANA has no Pinnacle quote before its start: no close, with its reason kept.
    assert rows[ANA]["move"] is None and rows[ANA]["close_status"] != closing.PROXY


def test_a_proxy_at_or_before_the_decision_is_no_close() -> None:
    # As in settlement: a close never comes from the decision's own snapshot or an earlier one.
    late = ledger().with_columns(decision_snapshot_utc=pl.lit(PRE7, dtype=pl.Datetime("us", "UTC")))
    moves = movement.table(late, ls.closes(odds(), NOW))
    both = moves.filter(pl.col("game_id").is_in([WSH, WPG]))
    assert both["close_status"].to_list() == [closing.MISSING] * 2
    assert moves["move"].is_null().all() and moves["close_snapshot_utc"].is_null().all()
