"""A synthetic real-stakes record for nhl real's tests (#268): the paper ledger of predict's
synthetic day, games with results, weekly reports and entries."""

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

import polars as pl
import predict_fixtures as pf
from fakes import ConditionalBucket

from nhl_edge.betting.selection import POLICY_VERSION
from nhl_edge.live import bundle as lb
from nhl_edge.live import predict as lp
from nhl_edge.live import real
from nhl_edge.live import real_ledger as rl

ALERT = datetime(2026, 10, 7, 16, 52, tzinfo=UTC)
CODE = "real-20261007-abc1234"


def ledger() -> pl.DataFrame:
    """The synthetic day's paper ledger: three bets, the first at 2.10 with the full 1.5%."""
    inputs = pf.day()
    return lp.ledger(lp.decide(inputs), inputs)


def games(
    results: dict[int, tuple[int, int]] | None = None,
    observed: datetime = datetime(2026, 10, 8, 6, tzinfo=UTC),
    played: dict[int, datetime] | None = None,
) -> pl.DataFrame:
    """The slate's games with final scores (home, away), public at observed; played moves a
    game's start, as a postponement does."""
    results = results or {2026020053: (3, 2), 2026020054: (1, 4), 2026020055: (2, 1)}
    played = played or {}
    return pl.DataFrame(
        {
            "game_id": list(results),
            "start_utc": [played.get(g, pf.GAMES[g][2]) for g in results],
            "home_score": [results[g][0] for g in results],
            "away_score": [results[g][1] for g in results],
            "observed_utc": [observed] * len(results),
        },
        schema={
            "game_id": pl.Int64,
            "start_utc": pl.Datetime("us", "UTC"),
            "home_score": pl.Int16,
            "away_score": pl.Int16,
            "observed_utc": pl.Datetime("us", "UTC"),
        },
    )


def report(
    as_of: date,
    verdict: str = "does not hold: (a) not yet measured",
    stop: bool | None = None,
    pause: bool | None = None,
) -> dict[str, Any]:
    """A committed weekly report's real-stakes section (live/report.real_stakes)."""
    return {
        "as_of": as_of.isoformat(),
        "policy_version": POLICY_VERSION,
        "real_stakes": {
            "weekly": True,
            "verdict": verdict,
            "clv_stop": {
                "measured": stop is not None,
                "weekly": True,
                "stop": stop,
                "pause": pause,
            },
        },
    }


def alert_rows(
    today: rl.Mode | None = None,
    bankroll: float = 100.0,
    venue: real.Venue = real.PRACTICE_VENUE,
    now: datetime = ALERT,
) -> pl.DataFrame:
    return rl.alert_rows(ledger(), today or rl.Mode(rl.PRACTICE), bankroll, venue, now, CODE)


def entry(
    game_id: int = 2026020053,
    outcome: str = "placed",
    price: float | None = 2.10,
    stake: float | None = 1.5,
    placed: datetime | None = datetime(2026, 10, 7, 17, 5, tzinfo=UTC),
    entered: datetime = datetime(2026, 10, 7, 17, 6, tzinfo=UTC),
    reason: str | None = None,
) -> dict[str, Any]:
    """An entry as Supabase's real_entries returns it."""
    is_placed = outcome == "placed"
    return {
        "game_date": pf.DAY.isoformat(),
        "game_id": game_id,
        "outcome": outcome,
        "matched_price": price if is_placed else None,
        "stake": stake if is_placed else None,
        "bet_id": f"B{game_id}" if is_placed else None,
        "placed_utc": placed.isoformat() if is_placed and placed else None,
        "reason": reason,
        "entered_utc": entered.isoformat(),
        "late": entered >= pf.GAMES[game_id][2],
    }


def table_of(rows: pl.DataFrame, game_id: int) -> list[list[float]]:
    return json.loads(rows.filter(pl.col("game_id") == game_id)["stakes"].item())


def later(hours: float) -> datetime:
    return ALERT + timedelta(hours=hours)


def alerted_store() -> lb.R2Store:
    """A record holding the synthetic day's practice alert, every bet alerted."""
    store = lb.R2Store(ConditionalBucket(), "bucket")
    empty = rl.bets(alert_rows().clear(), [])
    rl.write_alert(store, rl.prepare(pf.DAY, ledger(), [], empty, games(), ALERT, CODE))
    return store
