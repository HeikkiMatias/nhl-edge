"""The market move guard (docs/plan.md §10, #142, ADR 0029): a bet is skipped when the market moved
sharply against it between the morning snapshot and the decision. That pattern usually means
goalie or injury news the model doesn't have.

The move against a bet is how far the de-vigged probability of its side fell: for a home bet,
p_morning - p_decision; for an away bet, the reverse. A bet is skipped when that fall is larger
than MOVE_THRESHOLD.

MOVE_THRESHOLD is the 95th percentile of SBR's open-to-close move in the de-vigged home
probability (multiplicative, ADR 0008) over THRESHOLD_SEASONS, 2011-12 to 2017-18, when one book
set both prices. From 2018-19 on, SBR's close comes from another book (#65), which would add book
differences to the moves. It reads prices only, never a result or the model, and it is frozen:
threshold() recomputes it so the backtest can check that the lake still gives the same value.

Live, the morning snapshot is the 07:05 ET slot and the decision snapshot the 12:45 ET slot
(ADR 0028), both Pinnacle's. History has no midday price, so the guard can't act on SBR; the
backtest reports only how often it would have fired between the opener and the close.
"""

import polars as pl

from nhl_edge.audit.sbr import moneylines, moves
from nhl_edge.betting.selection import HOME
from nhl_edge.ingest.odds import available_at
from nhl_edge.market.devig import fair_probabilities

# Frozen by ADR 0029 from SBR's open-to-close moves of THRESHOLD_SEASONS: 8,140 games.
MOVE_THRESHOLD = 0.0535
QUANTILE = 0.95
THRESHOLD_SEASONS = tuple(range(20112012, 20182019, 10_001))
MORNING_SLOT = "morning"
DECISION_SLOT = "midday"
BOOK = "pinnacle"


def threshold(sbr_odds: pl.DataFrame) -> float:
    """The QUANTILE of the open-to-close move over THRESHOLD_SEASONS, from SBR's prices alone."""
    found = moves(moneylines(sbr_odds.filter(pl.col("season").is_in(THRESHOLD_SEASONS))))
    if found.is_empty():
        raise ValueError("no SBR openers and closes of 2011-12 to 2017-18 to set the guard on")
    value = found["move"].quantile(QUANTILE, "linear")
    assert isinstance(value, float)
    return round(value, 4)


def against(side: pl.Expr, p_morning: pl.Expr, p_decision: pl.Expr) -> pl.Expr:
    """How far the de-vigged probability of the bet's side fell between the two snapshots:
    positive when the market moved against the bet."""
    return pl.when(side == HOME).then(p_morning - p_decision).otherwise(p_decision - p_morning)


def guard(bets: pl.DataFrame, limit: float = MOVE_THRESHOLD) -> pl.DataFrame:
    """bets (side, p_morning, p_decision, the market's de-vigged home probability at each
    snapshot) with the move against each (moved_against) and whether the guard skips it."""
    moved = against(pl.col("side"), pl.col("p_morning"), pl.col("p_decision"))
    return bets.with_columns(moved_against=moved).with_columns(
        guarded=pl.col("moved_against") > limit
    )


def home_probability(quotes: pl.DataFrame) -> pl.DataFrame:
    """One row per event and snapshot from h2h quotes of one book: the multiplicative de-vigged
    home probability (ADR 0008) of the two prices quoted together."""
    h2h = quotes.filter(pl.col("market") == "h2h")
    keys = ["event_id", "snapshot_utc", "last_update_utc", "commence_time_utc"]
    wide = (
        h2h.filter(pl.col("side") == "home")
        .select(*keys, home_price="price_decimal")
        .join(
            h2h.filter(pl.col("side") == "away").select(*keys, away_price="price_decimal"),
            on=keys,
        )
    )
    if wide.is_empty():
        return wide.with_columns(p_home=pl.lit(None, pl.Float64))
    fair = fair_probabilities(wide.select("home_price", "away_price").to_numpy())[:, 0]
    return wide.with_columns(p_home=pl.Series(fair, dtype=pl.Float64))


def live_moves(snapshots: pl.DataFrame, decision_utc: object, book: str = BOOK) -> pl.DataFrame:
    """Per event priced in both slots before decision_utc and not started by then: the book's
    de-vigged home probability at the latest MORNING_SLOT snapshot (p_morning) and at the latest
    DECISION_SLOT snapshot (p_decision), with each snapshot's time. Only quotes available at the
    decision are read (odds.available_at)."""
    usable = available_at(snapshots.filter(pl.col("book") == book), decision_utc)  # type: ignore[arg-type]

    def latest(slot: str, name: str) -> pl.DataFrame:
        priced = home_probability(usable.filter(pl.col("slot") == slot))
        return (
            priced.sort("snapshot_utc")
            .group_by("event_id")
            .last()
            .select("event_id", **{f"p_{name}": "p_home", f"{name}_utc": "snapshot_utc"})
        )

    return latest(MORNING_SLOT, "morning").join(latest(DECISION_SLOT, "decision"), on="event_id")
