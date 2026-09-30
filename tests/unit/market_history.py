"""Synthetic seasons of SBR moneylines and results, for backtest tests that need earlier seasons to
fit B1 on. The results follow a known recalibration of the market, so a fit can be checked."""

from datetime import UTC, date, datetime, time, timedelta

import numpy as np
import polars as pl

from nhl_edge.ingest.sbr import american_to_decimal
from nhl_edge.lake.schemas import Games, SbrOdds, dtypes

TEAMS = ["BOS", "TOR", "MTL", "NYR", "CHI", "DET", "EDM", "CGY"]
VIG = 1.045


def american(q: float) -> int:
    """The American price of implied probability q."""
    return -round(100 * q / (1 - q)) if q >= 0.5 else round(100 * (1 - q) / q)


def season(
    season: int, games: int, seed: int, intercept: float = 0.0, slope: float = 1.0
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """sbr_odds rows (the moneyline's open and close) and final games for one season. The home
    team wins with probability sigmoid(intercept + slope * logit(p)), where p is the close's
    multiplicative de-vigged home probability."""
    rng = np.random.default_rng(seed)
    year = season // 10_000
    odds, finals = [], []
    for i in range(games):
        game_id = year * 1_000_000 + 20_000 + i + 1
        day = date(year, 10, 10) + timedelta(days=i // 4)
        start = datetime.combine(day, time(23), UTC)
        home, away = TEAMS[i % 8], TEAMS[(i + 1 + i // 8) % 8]
        if home == away:
            away = TEAMS[(i + 4) % 8]
        p = float(rng.uniform(0.3, 0.72))
        prices = {}
        for quote, fair in (("open", p + float(rng.normal(0, 0.02))), ("close", p)):
            prices[quote] = {"home": american(fair * VIG), "away": american((1 - fair) * VIG)}
        close = {side: 1 / american_to_decimal(a) for side, a in prices["close"].items()}
        p_close = close["home"] / (close["home"] + close["away"])
        logit = np.log(p_close / (1 - p_close))
        home_win = rng.random() < 1 / (1 + np.exp(-(intercept + slope * logit)))
        for quote, sides in prices.items():
            assumed = start if quote == "close" else datetime.combine(day, time(14), UTC)
            for side, price in sides.items():
                odds.append(
                    {
                        "game_id": game_id,
                        "season": season,
                        "game_date": day,
                        "start_utc": start,
                        "home": home,
                        "away": away,
                        "market": "h2h",
                        "side": side,
                        "line": None,
                        "quote": quote,
                        "price_american": price,
                        "price_decimal": american_to_decimal(price),
                        "observed_utc": start,
                        "assumed_available_utc": assumed,
                        "raw_key": "sbr/synthetic",
                    }
                )
        finals.append(
            {
                "game_id": game_id,
                "season": season,
                "game_date": day,
                "start_utc": start,
                "home": home,
                "away": away,
                "venue": "x",
                "home_score": 3 if home_win else 1,
                "away_score": 1 if home_win else 3,
                "decided_in": "REG",
                "neutral_site": False,
                "limited_attendance": False,
                "observed_utc": datetime.combine(day + timedelta(days=1), time(10), UTC),
                "raw_key": "games/synthetic",
            }
        )
    return (
        pl.DataFrame(odds, schema=dtypes(SbrOdds)),
        pl.DataFrame(finals, schema=dtypes(Games)),
    )


def seasons(
    wanted: list[int], games: int = 40, intercept: float = 0.0, slope: float = 1.0
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Several synthetic seasons, each with its own seed."""
    parts = [season(s, games, s, intercept, slope) for s in wanted]
    return pl.concat([odds for odds, _ in parts]), pl.concat([finals for _, finals in parts])


def implausible_opener(odds: pl.DataFrame, game_id: int, quote: str = "open") -> pl.DataFrame:
    """odds with the game's opening moneyline (or another quote's) replaced by an implausible one,
    Edmonton's -1010 and Minnesota's 705 of 2022-02-20: a home probability of about 0.88."""
    prices = {"home": -1010, "away": 705}
    rows = (pl.col("game_id") == game_id) & (pl.col("market") == "h2h") & (pl.col("quote") == quote)
    home = pl.col("side") == "home"
    return odds.with_columns(
        price_american=pl.when(rows)
        .then(pl.when(home).then(prices["home"]).otherwise(prices["away"]))
        .otherwise(pl.col("price_american"))
        .cast(pl.Int32),
        price_decimal=pl.when(rows)
        .then(
            pl.when(home)
            .then(american_to_decimal(prices["home"]))
            .otherwise(american_to_decimal(prices["away"]))
        )
        .otherwise(pl.col("price_decimal")),
    )
