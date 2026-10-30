"""scripts/debut_nhle.py (#125, ADR 0050) loaded as a module, and its fits of the RAPM fixture
league. The late player (rapm_fixtures.LATE_PLAYER) debuts in its second season, with an AHL
season before it, so he is the debutant whose lines turn public."""

import importlib.util
from datetime import date, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import polars as pl
import rapm_fixtures as fx
from goalie_fixtures import public_after

from nhl_edge.ratings import rapm

SCRIPT = Path(__file__).parents[2] / "scripts" / "debut_nhle.py"
DEBUT = fx.SEASONS[1]
THREE = (*fx.SEASONS, 20132014)


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("debut_nhle", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


dn = _load()
LEAGUE = fx.league()
LONG = fx.league(seasons=THREE)


def as_ofs(league: dict[str, pl.DataFrame] = LEAGUE, season: int = DEBUT) -> list[datetime]:
    """The debut season's as-of times, in order."""
    wanted = rapm.targets(league["lineups"], league["games"], [season])
    return sorted(wanted["as_of_utc"].unique().to_list())


# Rostered players with an AHL season before the league's first, so the season-start fits have
# NHLe traits to learn from: each of the first teams' forwards, scoring with his true offense.
AHL_PLAYERS = [p for t in range(2) for p in range(100 * (t + 1), 100 * (t + 1) + fx.FORWARDS)]


def lines(public: datetime) -> pl.DataFrame:
    """The fixture's lines, with the late player's AHL season public at public, as
    player_league_seasons dates a debutant's lines at his first boxscore, and AHL_PLAYERS'
    seasons of 2010-11, public before the league starts."""
    base = fx.league_seasons()
    true = fx.truth()
    earlier = pl.DataFrame(
        {
            "player_id": AHL_PLAYERS,
            "season": 20102011,
            "league_abbrev": "AHL",
            "league": "AHL",
            "game_type": 2,
            "games_played": 60,
            "points": [int(60 * max(0.1, 0.8 + true[p]["ev_off"])) for p in AHL_PLAYERS],
            "observed_utc": public_after(date(2011, 6, 30)),
        }
    ).with_columns(goals=pl.col("points") // 2, assists=pl.col("points") - pl.col("points") // 2)
    return pl.concat([base, earlier.select(base.columns).cast(base.schema)]).with_columns(
        observed_utc=pl.when(pl.col("player_id") == fx.LATE_PLAYER)
        .then(pl.lit(public, dtype=pl.Datetime("us", "UTC")))
        .otherwise(pl.col("observed_utc"))
    )


# Mid-season: an hour before the debut season's eleventh as-of time.
MIDSEASON = as_ofs()[10] - timedelta(hours=1)


def fit(
    kind: str,
    public: datetime = MIDSEASON,
    league: dict[str, pl.DataFrame] = LEAGUE,
    seasons: tuple[int, ...] = fx.SEASONS,
    stints: pl.DataFrame | None = None,
) -> tuple[pl.DataFrame, pl.DataFrame, Any]:
    """The script's fit of the league: ratings and out-of-sample stints, sorted, and the run."""
    wanted = rapm.targets(league["lineups"], league["games"], list(seasons))
    run = dn.rate(
        fx.seasons_of(league["stints"] if stints is None else stints, seasons),
        league["games"],
        league["roles"],
        league["venues"],
        wanted,
        fx.players(),
        lines(public),
        kind,
    )
    ratings = pl.concat(run.ratings).sort("game_id", "player_id", "component")
    predicted = pl.concat(run.stints).sort("season", "row")
    return ratings, predicted, run


def late(ratings: pl.DataFrame) -> pl.DataFrame:
    """The late player's 5v5 offense ratings, in as-of order."""
    return ratings.filter(
        pl.col("player_id") == fx.LATE_PLAYER, pl.col("component") == "ev_off"
    ).sort("as_of_utc")
