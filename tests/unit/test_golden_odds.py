"""Golden odds (#10, docs/plan.md section 11): a real Odds API snapshot, frozen in
tests/golden/odds/ by scripts/freeze_golden_odds.py with the schedule of VAN at EDM on 2026-09-29,
a game the snapshot quotes that went to overtime.

Every book's moneyline carries vig, and only market/devig.py takes it out (hard rule 3). The
moneyline settles on the full game and the 3-way line on regulation, which an overtime game ends
tied, so a regulation probability is never a moneyline probability (hard rule 2).
"""

import gzip
import hashlib
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from nhl_edge.backtest.walk_forward import outcomes
from nhl_edge.ingest.games import listed_games, parse_games
from nhl_edge.ingest.odds import parse_odds
from nhl_edge.market.devig import fair_probabilities

GOLDEN_DIR = Path(__file__).parents[1] / "golden"
MANIFEST: dict[str, Any] = json.loads((GOLDEN_DIR / "odds" / "manifest.json").read_text())
GAME: dict[str, Any] = MANIFEST["overtime_game"]
RAW_KEY = "golden"
INDEX = ["event_id", "home_team", "away_team", "book"]


def frozen(path: str) -> bytes:
    return gzip.decompress((GOLDEN_DIR / path).read_bytes())


def parse_snapshot() -> pl.DataFrame:
    path = MANIFEST["snapshot"]["file"]
    meta = json.loads((GOLDEN_DIR / path.replace(".json.gz", ".meta.json")).read_text())
    return parse_odds(
        frozen(path), datetime.fromisoformat(meta["fetched_utc"]), meta["slot"], RAW_KEY
    )


QUOTES = parse_snapshot()


def markets(market: str, sides: list[str]) -> pl.DataFrame:
    """One row per book and game on the market, with a price column per side."""
    return (
        QUOTES.filter(pl.col("market") == market)
        .rename({"home": "home_team", "away": "away_team"})
        .pivot(on="side", index=INDEX, values="price_decimal")
        .select(*INDEX, *sides)
    )


MONEYLINES = markets("h2h", ["home", "away"])
THREE_WAY = markets("h2h_3_way", ["home", "draw", "away"])


def test_the_manifest_matches_the_frozen_files() -> None:
    for path, sha256 in MANIFEST["files"].items():
        assert hashlib.sha256((GOLDEN_DIR / path).read_bytes()).hexdigest() == sha256, path
    frozen_files = {
        p.relative_to(GOLDEN_DIR).as_posix()
        for p in (GOLDEN_DIR / "odds").iterdir()
        if p.name != "manifest.json"
    }
    assert set(MANIFEST["files"]) == frozen_files


def test_draw_quotes_never_reach_the_moneyline() -> None:
    # Books that quote the 3-way regulation line under the h2h key are stored as h2h_3_way.
    sides = QUOTES.group_by("market").agg(pl.col("side").unique().sort())
    assert dict(sides.iter_rows()) == {
        "h2h": ["away", "home"],
        "h2h_3_way": ["away", "draw", "home"],
        "totals": ["over", "under"],
    }
    assert (MONEYLINES.height, MONEYLINES["book"].n_unique()) == (63, 8)
    assert (THREE_WAY.height, THREE_WAY["book"].n_unique()) == (183, 13)
    # Every market is complete: one price per side.
    assert MONEYLINES.null_count().sum_horizontal().item() == 0
    assert THREE_WAY.null_count().sum_horizontal().item() == 0


def test_every_book_s_moneyline_carries_vig() -> None:
    implied = 1 / MONEYLINES.select("home", "away").to_numpy()
    assert (implied.sum(axis=1) > 1).all()


def test_de_vigging_takes_the_vig_out() -> None:
    for table in (MONEYLINES.select("home", "away"), THREE_WAY.select("home", "draw", "away")):
        prices = table.to_numpy()
        fair = fair_probabilities(prices)
        np.testing.assert_allclose(fair.sum(axis=1), 1.0, atol=1e-12)
        # Each side gives up part of the margin, and the favourite stays the favourite.
        assert (fair < 1 / prices).all()
        assert (fair.argmax(axis=1) == prices.argmin(axis=1)).all()


def test_the_overtime_game_settles_the_moneyline_on_the_full_game() -> None:
    schedule = frozen(GAME["schedule"])
    games = parse_games(listed_games(schedule, {date.fromisoformat(GAME["game_date"])}), RAW_KEY)
    game = games.filter(pl.col("game_id") == GAME["game_id"])
    row = game.row(0, named=True)
    assert (row["away"], row["home"], row["away_score"], row["home_score"]) == ("VAN", "EDM", 6, 5)
    # Regulation ended tied, so the 3-way line pays the draw, while the moneyline pays VAN.
    assert row["decided_in"] == "OT"
    assert outcomes(game)["home_win"].to_list() == [0]


def test_a_regulation_probability_is_not_a_moneyline_probability() -> None:
    game = (pl.col("home_team") == "EDM") & (pl.col("away_team") == "VAN")
    moneyline = fair_probabilities(MONEYLINES.filter(game).select("home", "away").to_numpy())
    regulation = fair_probabilities(
        THREE_WAY.filter(game).select("home", "draw", "away").to_numpy()
    )
    assert (len(moneyline), len(regulation)) == (8, 13)
    # The 3-way line gives the draw about a fifth, so every book's regulation win probability for
    # VAN is below every book's full-game one. Priced against the moneyline, it would understate
    # VAN's chance by the share of overtimes and shootouts VAN wins.
    assert (regulation[:, 1] > 0.15).all()
    assert regulation[:, 2].max() < moneyline[:, 1].min()
