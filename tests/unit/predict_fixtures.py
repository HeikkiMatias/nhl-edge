"""A synthetic decision day for nhl predict's tests (#164): a slate of three games, the day's odds
snapshots, and a live fit, with the models' outputs given rather than fitted."""

from datetime import UTC, date, datetime
from typing import Any

import polars as pl

from nhl_edge.game import uncertainty
from nhl_edge.ingest.odds import ODDS_FRAME_SCHEMA
from nhl_edge.lake.schemas import Slate, dtypes
from nhl_edge.live import blend_fit
from nhl_edge.live import predict as lp
from nhl_edge.market import blend, recalibration
from nhl_edge.market.blend import Kind

DAY = date(2026, 10, 7)
MORNING = datetime(2026, 10, 7, 11, 6, tzinfo=UTC)  # 07:06 ET
MIDDAY = datetime(2026, 10, 7, 16, 45, 30, tzinfo=UTC)  # 12:45 ET
DECISION = datetime(2026, 10, 7, 16, 47, tzinfo=UTC)
START = datetime(2026, 10, 7, 23, 30, tzinfo=UTC)
GAMES = {
    2026020053: ("WSH", "PIT", START),
    2026020054: ("WPG", "COL", START),
    2026020055: ("ANA", "EDM", datetime(2026, 10, 8, 2, tzinfo=UTC)),
}


def slate(games: dict[int, tuple[str, str, datetime]] = GAMES) -> pl.DataFrame:
    rows = [
        {
            "game_id": game_id,
            "season": 20262027,
            "game_date": DAY,
            "start_utc": start,
            "home": home,
            "away": away,
            "venue": "Arena",
            "neutral_site": False,
            "limited_attendance": False,
            "game_state": "FUT",
            "observed_utc": datetime(2026, 10, 7, 9, 5, tzinfo=UTC),
            "raw_key": "nhl/schedule/2026-10-07/20261007T090500Z",
        }
        for game_id, (home, away, start) in games.items()
    ]
    return Slate.validate(pl.DataFrame(rows, schema=dtypes(Slate)))


def quote(
    snapshot: datetime,
    game_id: int,
    book: str,
    home_price: float,
    away_price: float,
    updated: datetime | None = None,
    slot: str = "midday",
) -> list[dict[str, Any]]:
    home, away, start = GAMES[game_id]
    return [
        {
            "snapshot_utc": snapshot,
            "last_update_utc": updated or snapshot,
            "event_id": f"e{game_id}",
            "commence_time_utc": start,
            "home": home,
            "away": away,
            "book": book,
            "market": "h2h",
            "side": side,
            "line": None,
            "price_decimal": price,
            "is_closing_proxy": False,
            "slot": slot,
            "raw_key": f"odds/{snapshot:%Y-%m-%d}/{snapshot:%Y%m%dT%H%M%SZ}_{slot}_eu",
        }
        for side, price in (("home", home_price), ("away", away_price))
    ]


def quotes(*rows: list[dict[str, Any]]) -> pl.DataFrame:
    return pl.DataFrame([r for group in rows for r in group], schema=ODDS_FRAME_SCHEMA)


def day_quotes() -> pl.DataFrame:
    """Morning and midday quotes of the three games at Pinnacle and one other book."""
    return quotes(
        *(
            quote(MORNING, g, "pinnacle", h, a, slot="morning")
            for g, h, a in (
                (2026020053, 2.10, 1.80),
                (2026020054, 1.90, 2.00),
                (2026020055, 2.5, 1.6),
            )
        ),
        *(
            quote(MIDDAY, g, "pinnacle", h, a)
            for g, h, a in (
                (2026020053, 2.10, 1.80),
                (2026020054, 1.90, 2.00),
                (2026020055, 2.5, 1.6),
            )
        ),
        quote(MIDDAY, 2026020053, "bet365", 2.15, 1.75),
    )


def fitted(p_b3: float = 0.62) -> lp.Models:
    """Models for every slate game: B3 well above the market on the home sides."""
    ids = list(GAMES)
    parts = {name: [0.3, 1.6, 0.18][k] for k, name in enumerate(uncertainty.PARTS)}
    return lp.Models(
        b2=pl.DataFrame({"game_id": ids, "p_b2": [0.55] * 3}),
        b3=pl.DataFrame({"game_id": ids, "p_b3": [p_b3] * 3}),
        parts=pl.DataFrame({"game_id": ids, **{k: [v] * 3 for k, v in parts.items()}}),
        b2_cutoff=datetime(2026, 4, 17, tzinfo=UTC),
        b3_cutoff=datetime(2026, 4, 17, tzinfo=UTC),
    )


def live() -> blend_fit.LiveFit:
    cutoff = datetime(2022, 11, 28, 10, tzinfo=UTC)
    model = blend.Blend(
        Kind.MODEL, (-0.065, 0.71, 0.49, 0.2), (0.03, 0.15, 0.17, 0.12), 4875, cutoff
    )
    return blend_fit.LiveFit(
        version="blend-live-20261007-abc1234",
        blends={
            "BLEND": model,
            "BLEND_B2": blend.Blend(
                Kind.MODEL, (-0.06, 0.97, 0.22, 0.15), (0.0,) * 4, 4875, cutoff
            ),
            "BLEND_MARKET": blend.Blend(Kind.MARKET, (-0.04, 1.09), (0.0,) * 2, 4875, cutoff),
        },
        scale=uncertainty.Scale((0.3, 1.6, 0.18), (0.086, 0.5, 0.068), 4875, cutoff, 0.687),
        b1=recalibration.Recalibration(-0.034, 1.099, 14245, cutoff),
        fold_start=datetime(2026, 9, 29, 21, tzinfo=UTC),
        train_cutoff=cutoff,
    )


def day(**changes: Any) -> lp.Day:
    base: dict[str, Any] = {
        "day": DAY,
        "decision_utc": DECISION,
        "slate": slate(),
        "quotes": day_quotes(),
        "fitted": fitted(),
        "live": live(),
        "bankroll": 100.0,
        "versions": {
            "blend_version": "blend-live-20261007-abc1234",
            "feature_build": "live-features-20261007-abc1234",
            "code_version": "abc1234",
            "b2_train_cutoff": datetime(2026, 4, 17, tzinfo=UTC),
            "b3_train_cutoff": datetime(2026, 4, 17, tzinfo=UTC),
        },
    }
    return lp.Day(**(base | changes))
