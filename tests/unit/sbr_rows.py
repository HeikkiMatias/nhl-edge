"""sbr_odds rows built by hand, for the suspect-opener tests (#56): one game per case, with its
moneylines at the open and the close."""

from datetime import UTC, datetime, timedelta
from typing import Any

import polars as pl

from nhl_edge.ingest.sbr import american_to_decimal
from nhl_edge.lake.schemas import SbrOdds, dtypes

Prices = tuple[int, int]


def game(
    game_id: int, open_: Prices, close: Prices | None = None, home_line: float | None = None
) -> list[dict[str, Any]]:
    """sbr_odds rows of one game: the (home, away) American moneylines at the open and the close,
    and the home team's closing puck line."""
    season = (game_id // 1_000_000) * 10_001 + 1
    start = datetime(game_id // 1_000_000, 11, 1, 23, tzinfo=UTC) + timedelta(days=game_id % 100)

    def row(market: str, side: str, line: float | None, quote: str, price: int) -> dict[str, Any]:
        opened = quote == "open"
        return {
            "game_id": game_id,
            "season": season,
            "game_date": start.date(),
            "start_utc": start,
            "home": "TOR",
            "away": "MTL",
            "market": market,
            "side": side,
            "line": line,
            "quote": quote,
            "price_american": price,
            "price_decimal": american_to_decimal(price),
            "observed_utc": start,
            "assumed_available_utc": start - timedelta(hours=9) if opened else start,
            "raw_key": f"sbr/{season}/20260929T120000Z",
        }

    quotes = [("open", open_)] + ([("close", close)] if close else [])
    rows = [
        row("h2h", side, None, quote, price)
        for quote, prices in quotes
        for side, price in zip(("home", "away"), prices, strict=True)
    ]
    if home_line is not None:
        rows += [
            row("spreads", "home", home_line, "close", -110),
            row("spreads", "away", -home_line, "close", -110),
        ]
    return rows


def odds(*games: list[dict[str, Any]]) -> pl.DataFrame:
    rows = [row for rows in games for row in rows]
    return SbrOdds.validate(pl.DataFrame(rows, schema=dtypes(SbrOdds)))


ODDS = odds(
    game(2018020001, (-150, 130), (-155, 135)),  # an ordinary move
    game(2018020002, (-160, 140), (160, -180), home_line=1.5),  # sides swapped, big move
    game(2018020003, (-1010, 705), (-105, -105)),  # a typo
    game(2018020004, (-115, -105), (105, -125)),  # favourite changes near even
    game(2018020005, (110, 110), (-110, -110)),  # opener sums below 100%
    game(2018020006, (120, -140), (-145, 125)),  # sides swapped, 13 points
    game(2018020007, (-125, 105), (-245, 205)),  # a big move with the same favourite
    game(2018020008, (-1010, 705)),  # a typo and no close
    # The close's favourite is +1.5 on its own puck line, which backs the opener: a bad close.
    game(2018020009, (-155, 135), (200, -240), home_line=-1.5),
    # The close contradicts its puck line, but the opener names no favourite to back either.
    game(2018020010, (-110, -110), (250, -320), home_line=-1.5),
    game(2022020001, (-1010, 705), (-105, -105)),  # 2022-23 is not inspected in phase 1
)
