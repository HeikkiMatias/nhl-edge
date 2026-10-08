"""Settlement and closing line value of the paper bets (#165, docs/plans/phase-5.md task 5).

Each night, after the odds replay has marked the closing proxies (#21), every bet of the paper
ledger whose game is final gets one row of paper_settlements:
- **The result** on the full game, OT and shootout included (hard rule 2): games' scores count
  the shootout winner's goal, so home_score > away_score settles the moneyline. The profit is
  stake·(price - 1) for a win and -stake for a loss. A game played more than POSTPONED from the
  ledger's start is a postponed game, and its bet is void, as the books settle it.
- **The close:** Pinnacle's closing proxy of the bet's own Odds API event (ADR 0033), taken after
  the bet's decision snapshot. Without one, close_status gives why (market/closing.py):
  no pre-game snapshot (outside ADR 0032's coverage floor), stale or missing (counted against it).
- **CLV** = price·p_close - 1 and the fair move, through backtest/e3.closing_value, as on history:
  p_close is the bet's side in Pinnacle's closing pair de-vigged multiplicatively
  (market/devig.py), and the fair move compares it with the decision's own de-vigged pair.

Nothing here feeds a decision: the bankroll reads the ledgers and the games itself.
"""

from collections.abc import Mapping
from datetime import datetime, timedelta

import polars as pl

from nhl_edge.backtest import e3
from nhl_edge.backtest.walk_forward import outcomes
from nhl_edge.lake.schemas import PaperSettlements, dtypes
from nhl_edge.market import closing

COMPONENT = "settle"
# A game played this far from its ledger start was postponed: as the odds replay matches an
# event to its game (odds_lake.MATCH_WINDOW).
POSTPONED = timedelta(hours=12)
SETTLED, VOID = "settled", "void"


def closes(odds: pl.DataFrame, now: datetime) -> pl.DataFrame:
    """Pinnacle's close of each game started by now, by Odds API event: its status
    (closing.pinnacle_closes), and for a proxy its snapshot and h2h pair."""
    status = closing.pinnacle_closes(odds, now).select(
        "event_id", close_status="status", close_snapshot_utc="proxy_utc"
    )
    proxies = odds.filter(
        pl.col("is_closing_proxy"), pl.col("book") == closing.BOOK, pl.col("market") == "h2h"
    )
    pairs = (
        proxies.pivot(on="side", index=["event_id", "snapshot_utc"], values="price_decimal").select(
            "event_id", close_snapshot_utc="snapshot_utc", close_home="home", close_away="away"
        )
        if proxies.height
        else pl.DataFrame(
            schema={
                "event_id": pl.String,
                "close_snapshot_utc": closing.UTC_TYPE,
                "close_home": pl.Float64,
                "close_away": pl.Float64,
            }
        )
    )
    return status.join(pairs, on=["event_id", "close_snapshot_utc"], how="left")


def settle(
    ledger: pl.DataFrame,
    games: pl.DataFrame,
    odds: pl.DataFrame,
    now: datetime,
    versions: Mapping[str, object],
) -> pl.DataFrame:
    """The ledger's bets whose game is final, settled and valued against Pinnacle's close, as
    PaperSettlements rows."""
    columns = dtypes(PaperSettlements)
    bets = ledger.filter(pl.col("bet").fill_null(False))
    played = games.select("game_id", played_utc="start_utc").join(outcomes(games), on="game_id")
    final = bets.join(played, on="game_id")
    void = (pl.col("played_utc") - pl.col("start_utc")).abs() > POSTPONED
    priced = final.join(closes(odds, now), on="event_id", how="left").with_columns(
        status=pl.when(void).then(pl.lit(VOID)).otherwise(pl.lit(SETTLED)),
        # A bet without any Pinnacle quote left of its game has no close either.
        close_status=pl.col("close_status").fill_null(closing.MISSING),
    )
    # A close only from a proxy after the bet's own decision snapshot: never its own price.
    usable = (
        (pl.col("close_status") == closing.PROXY)
        & (pl.col("close_snapshot_utc") > pl.col("decision_snapshot_utc"))
        & (pl.col("status") == SETTLED)
    )
    priced = priced.with_columns(
        close_status=pl.when(
            (pl.col("close_status") == closing.PROXY)
            & (pl.col("close_snapshot_utc") <= pl.col("decision_snapshot_utc"))
        )
        .then(pl.lit(closing.MISSING))
        .otherwise(pl.col("close_status")),
        **{
            name: pl.when(usable).then(pl.col(name))
            for name in ("close_snapshot_utc", "close_home", "close_away")
        },
    )
    # A postponed game is decided again on its new date, under the same game_id.
    keys = ["game_date", "game_id"]
    pairs = priced.select(*keys, "close_home", "close_away")
    valued = e3.closing_value(priced.drop("close_home", "close_away"), pairs, keys).join(
        pairs, on=keys, how="left"
    )
    won = (pl.col("side") == "home") == (pl.col("home_win") == 1)
    return PaperSettlements.validate(
        valued.with_columns(
            won=pl.when(pl.col("status") == SETTLED).then(won),
            home_win=pl.when(pl.col("status") == SETTLED).then(pl.col("home_win")),
            profit=pl.when(pl.col("status") == VOID)
            .then(0.0)
            .when(won)
            .then(pl.col("stake") * (pl.col("price") - 1))
            .otherwise(-pl.col("stake")),
            settled_utc=pl.lit(now),
            **{name: pl.lit(value) for name, value in versions.items()},
        )
        .select(list(columns))
        .cast(columns)  # type: ignore[arg-type]
        .sort("game_date", "game_id")
    )
