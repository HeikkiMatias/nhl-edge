"""Shadow evaluations on the live ledger (#252): paper only, never a decision.

The frozen policy's CLV mixes two questions. These shadows separate them, each holding the other
part constant, and each is valued against the same Pinnacle closing proxy as the policy's own bets
(live/movement.py), fixed here before its first reading:

- **Market-only selection:** does B3 improve the betting decision beyond the recalibrated market?
  The frozen selection, guard and staking (betting/, ADR 0028 and 0029) applied to the market-only
  control (p_blend_market, fitted beside the blend on the same games) in place of the blend, at
  the same Pinnacle price, snapshot, u and bankroll.
- **Best-price execution:** how much CLV does the entry price cost? The policy's own bets, held
  constant, priced at the best fresh h2h quote of the other books at the decision snapshot when
  it beats Pinnacle's: fresh as ADR 0033 requires of Pinnacle's own quote, at the decision and at
  publication. h2h is the full-game moneyline (hard rule 2). Bookmakers and exchanges are kept
  apart: an exchange's price is before its commission, and neither shows the stake a book would
  take or that the price was still there when bet, so both are upper bounds.

The same rules value a registered challenger's shadow bets (live/challengers.py, ADR 0038).

Nothing here feeds a decision, and no figure here is part of ADR 0032's verdicts.
"""

import polars as pl

from nhl_edge.betting import guard, selection, staking
from nhl_edge.betting.selection import HOME, POLICY
from nhl_edge.live.predict import BOOK, MAX_PUBLISHED_AGE, MAX_QUOTE_AGE

# Betting exchanges among the Odds API's books: their prices are quoted before commission.
EXCHANGES = frozenset({"matchbook", "betfair_ex_eu", "betfair_ex_uk", "smarkets"})
# The policy's own decision columns, chosen again by a shadow.
DECISION = (
    "side",
    "price",
    "p_side",
    "ev",
    "hurdle",
    "picked",
    "moved_against",
    "guarded",
    "bet",
    "fraction",
    "stake",
)


def selected(rows: pl.DataFrame, column: str) -> pl.DataFrame:
    """The bets the frozen selection, guard and staking would make on the predicted rows with
    column's home probability in place of p_blend: side, price, p_side, ev, fraction and stake,
    at each row's own Pinnacle price, u and bankroll. On p_blend itself, the policy's own bets."""
    games = rows.filter(pl.col(column).is_not_null()).drop(DECISION, strict=False)
    chosen = selection.select(games.with_columns(p_home=pl.col(column)), POLICY)
    # Without both of the day's quotes the guard reads no move and doesn't apply (ADR 0029).
    guarded = guard.guard(chosen.drop("p_home")).with_columns(pl.col("guarded").fill_null(False))
    bets = guarded.filter(pl.col("picked") & ~pl.col("guarded"))
    return staking.fractions(bets, POLICY).with_columns(
        stake=pl.col("fraction") * pl.col("bankroll")
    )


def market_only(rows: pl.DataFrame) -> pl.DataFrame:
    """The bets the frozen rules would make on the market-only control (#252)."""
    return selected(rows, "p_blend_market")


def _side(home: pl.Expr) -> pl.Expr:
    """A home-side probability turned to the bet's side."""
    return pl.when(pl.col("side") == HOME).then(home).otherwise(1 - home)


def valued(bets: pl.DataFrame, moves: pl.DataFrame) -> pl.DataFrame:
    """bets (game_date, game_id, side, price) with a valid close in moves (movement.table): the
    fair closing probability of their side, their CLV at price and their fair move, as
    settlement computes them (backtest/e3.closing_value)."""
    # Named apart from the ledger's own p_decision, the guard's.
    closes = moves.filter(pl.col("p_close").is_not_null()).select(
        "game_date", "game_id", fair_decision="p_decision", fair_close="p_close"
    )
    return (
        bets.join(closes, on=["game_date", "game_id"])
        .with_columns(p_close_side=_side(pl.col("fair_close")))
        .with_columns(
            clv=pl.col("price") * pl.col("p_close_side") - 1,
            fair_move=pl.col("p_close_side") / _side(pl.col("fair_decision")) - 1,
        )
    )


def best_quotes(bets: pl.DataFrame, odds: pl.DataFrame) -> pl.DataFrame:
    """Each bet (event_id, side, decision_snapshot_utc, prediction_utc, published_utc) with the
    best fresh h2h price of the other books at its decision snapshot, for its side: among the
    bookmakers (best_bookmaker, its book) and among the exchanges (best_exchange, its book), null
    when none is fresh."""
    fresh = (pl.col("prediction_utc") - pl.col("last_update_utc") <= MAX_QUOTE_AGE) & (
        pl.col("published_utc") - pl.col("last_update_utc") <= MAX_PUBLISHED_AGE
    )
    quotes = (
        bets.select("game_date", "game_id", "event_id", "side", "decision_snapshot_utc")
        .join(
            odds.filter(pl.col("market") == "h2h", pl.col("book") != BOOK).select(
                "event_id",
                "side",
                "book",
                "price_decimal",
                "last_update_utc",
                decision_snapshot_utc="snapshot_utc",
            ),
            on=["event_id", "side", "decision_snapshot_utc"],
        )
        .join(
            bets.select("game_date", "game_id", "prediction_utc", "published_utc"),
            on=["game_date", "game_id"],
        )
        .filter(fresh)
    )
    result = bets
    for kind, books in (("bookmaker", ~pl.col("book").is_in(EXCHANGES)), ("exchange", None)):
        group = quotes.filter(books if books is not None else pl.col("book").is_in(EXCHANGES))
        best = (
            group.sort("price_decimal", "book", descending=[True, False])
            .group_by("game_date", "game_id", maintain_order=True)
            .first()
            .select(
                "game_date",
                "game_id",
                **{f"best_{kind}": "price_decimal", f"best_{kind}_book": "book"},
            )
        )
        result = result.join(best, on=["game_date", "game_id"], how="left")
    return result


def best_price(bets: pl.DataFrame, odds: pl.DataFrame, moves: pl.DataFrame) -> pl.DataFrame:
    """The policy's own bets with a valid close, valued at Pinnacle's price (clv) and at the best
    fresh price when it beats Pinnacle's: among the bookmakers (clv_bookmaker, better_bookmaker)
    and with the exchanges too, before their commission (clv_any, better_any). gain_* is the CLV
    added over Pinnacle's price."""
    quoted = valued(best_quotes(bets, odds), moves)
    bookmaker = pl.max_horizontal("price", "best_bookmaker")
    any_book = pl.max_horizontal("price", "best_bookmaker", "best_exchange")
    return quoted.with_columns(
        better_bookmaker=(pl.col("best_bookmaker") > pl.col("price")).fill_null(False),
        better_any=(any_book > pl.col("price")),
        clv_bookmaker=bookmaker * pl.col("p_close_side") - 1,
        clv_any=any_book * pl.col("p_close_side") - 1,
    ).with_columns(
        gain_bookmaker=pl.col("clv_bookmaker") - pl.col("clv"),
        gain_any=pl.col("clv_any") - pl.col("clv"),
    )
