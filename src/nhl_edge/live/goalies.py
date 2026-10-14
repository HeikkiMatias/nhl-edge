"""Policy v2's starting goalies in the day's decision (ADR 0037, #222).

At the midday decision, each slate team's starter is read from the day's pre-game polls, the NHL's
starter flag and Daily Faceoff's page, stored raw minutes earlier by the same dispatch. The lake's
goalie tables are rebuilt from those copies only overnight, so the decision parses its day's raw
polls itself. Every poll read was observed before the input cutoff, the decision snapshot (hard
rule 1).

The starters (lineup/confirmed.py) then replace the slate games' goalie rows in the tables B2, B3
and u read: goalie_starts, goalie_effects and goal_multipliers. B2, the yardstick, reads the same
starters as B3 (hard rule 3). The models' fits read only earlier games, so a call never reaches a
fit. The run bundle saves the rows the models read, so a replay reads the same starters.

Under policy v1 (selection.LIKELY_STARTS None) none of this runs (ADR 0030).
"""

from dataclasses import replace
from datetime import date, datetime

import polars as pl

from nhl_edge.game import b2, b3, uncertainty
from nhl_edge.ingest import dailyfaceoff, pregame
from nhl_edge.lake.raw import RawStore
from nhl_edge.lineup import confirmed

UTC = pl.Datetime("us", "UTC")
# The tables a v2 decision pulls beyond v1's (live/predict.py LAKE_TABLES): the names Daily
# Faceoff's goalies are matched against.
TABLES = ("players",)


def raw_prefixes(day: date) -> list[str]:
    """The raw prefixes of a game date's pre-game polls, to restore from R2 before reading them."""
    return [
        f"{pregame.BOXSCORE_PREFIX}/{day.isoformat()}/",
        f"{dailyfaceoff.PREFIX}/{day.isoformat()}/",
    ]


def day_polls(store: RawStore, day: date) -> tuple[pl.DataFrame, pl.DataFrame]:
    """The game date's NHL and Daily Faceoff polls, parsed from the raw copies."""
    nhl, _ = pregame.stored_pregame_goalies(store, [day])
    dfo, _ = dailyfaceoff.stored_starting_goalies(store, [day])
    return nhl, dfo


def team_games(slate: pl.DataFrame, cutoff: datetime) -> pl.DataFrame:
    """Each slate team-game (game_id, game_date, team, start_utc) at the moment cutoff."""
    return pl.concat(
        [
            slate.select("game_id", "game_date", "start_utc", team=pl.col(side))
            for side in ("home", "away")
        ]
    ).with_columns(moment_utc=pl.lit(cutoff, UTC))


def with_starters(
    tables: b2.Tables,
    b3_tables: b3.Tables,
    u_tables: uncertainty.Tables,
    slate: pl.DataFrame,
    polls: tuple[pl.DataFrame, pl.DataFrame],
    players: pl.DataFrame,
    cutoff: datetime,
    likely_share: float,
) -> tuple[b2.Tables, b3.Tables, uncertainty.Tables, pl.DataFrame]:
    """The three models' tables with each slate team's starter at the cutoff in place, and the
    starters (game_id, team, source, goalie_id, p, known_utc). polls holds the day's NHL and
    Daily Faceoff rows; players the names Daily Faceoff's are matched against."""
    nhl, dfo = polls
    called = confirmed.calls(team_games(slate, cutoff), nhl, dfo)
    names = players.select(goalie_id="player_id", name="name")
    slate_ids = slate["game_id"].implode()
    candidates = (
        tables.goalie_starts.filter(pl.col("game_id").is_in(slate_ids))
        .select("game_id", "team", "goalie_id", "p_start")
        .join(names, on="goalie_id", how="left")
    )
    rated = (
        tables.goalie_effects.filter(pl.col("observed_utc") < cutoff)
        .select("goalie_id")
        .unique()
        .join(names, on="goalie_id")
    )
    named = confirmed.starters(called, candidates, rated, likely_share)
    starts, effects, multipliers = confirmed.adjusted(
        tables.goalie_starts, tables.goalie_effects, b3_tables.goal_multipliers, named, cutoff
    )
    return (
        replace(tables, goalie_starts=starts, goalie_effects=effects),
        replace(b3_tables, goalie_starts=starts, goal_multipliers=multipliers),
        replace(u_tables, goalie_starts=starts),
        named,
    )
