"""Time at each strength state per team-game (#72): the seconds each team spent at 5v5, on the power
play, short-handed, 4v4 and so on, with its own or the opponent's net empty. Per-60 rates and
expected power-play opportunities need it, and `shots` holds only the moments of shots.

The play-by-play gives every play a situationCode, and play is logged every few seconds. The
timeline takes each stretch of play between consecutive plays at the skater counts in force at
the first of them, and a period's first play's counts from the period's start. Plays are ordered
by game time, since old feeds log a few out of order.

The counts follow ADR 0009, as a shot's do: from the shift chart when the game's chart is
complete and puts 3 to 6 skaters of each team on the ice, otherwise from situationCode. Whether
a net is empty always comes from situationCode's goalie digits, so a few seconds a game can show
six skaters with the goalie in, around delayed penalties and line changes. Penalty shots, which
the clock stops for, and the shootout are left out. A penalty that expires between two plays
counts until the next play, since no play marks the expiry; the gap is seconds.

Rows count as public at 10:00 UTC the morning after the game, with the rest of its feeds
(ADR 0004).
"""

import json

import polars as pl

from nhl_edge.ingest.feeds import FeedGame, check_game_id, clock_s
from nhl_edge.ingest.shift_coverage import CHART_SKATERS, on_ice_counts
from nhl_edge.lake.schemas import OT_PERIOD, PERIOD_S, STRENGTH_SOURCES, StrengthTime, dtypes

SHOOTOUT = "SO"
PERIOD_END = "period-end"
EVENT_SCHEMA = {
    "event_id": pl.Int32,
    "sort_order": pl.Int32,
    "period": pl.Int8,
    "seconds": pl.Int32,
    "situation_code": pl.String,
    "is_penalty_shot": pl.Boolean,
}


def events(body: bytes, game: FeedGame) -> tuple[pl.DataFrame, dict[int, int]]:
    """Every play of periods 1 to 4 with a four-digit situationCode, penalty shots flagged, in
    time order; and each period's end in elapsed game seconds: its period-end play, or for a
    regulation period without one, the full 20 minutes, or for overtime, its last play."""
    data = json.loads(body)
    check_game_id(data, game.game_id, "play-by-play")
    rows, ends = [], {}
    for play in data["plays"]:
        descriptor = play["periodDescriptor"]
        period, clock = descriptor["number"], clock_s(play.get("timeInPeriod"))
        if (
            descriptor.get("periodType") == SHOOTOUT
            or not 1 <= period <= OT_PERIOD
            or clock is None
        ):
            continue
        seconds = (period - 1) * PERIOD_S + clock
        if play["typeDescKey"] == PERIOD_END:
            ends[period] = seconds
        code = play.get("situationCode")
        if not (isinstance(code, str) and len(code) == 4 and code.isdigit()):
            continue
        rows.append(
            {
                "event_id": play["eventId"],
                "sort_order": play["sortOrder"],
                "period": period,
                "seconds": seconds,
                "situation_code": code,
                # One skater against none, the way situationCode shows a penalty shot.
                "is_penalty_shot": code[1:3] in ("10", "01"),
            }
        )
    # By game time, then play order: old feeds log a few plays out of time order.
    frame = pl.DataFrame(rows, schema=EVENT_SCHEMA).sort("period", "seconds", "sort_order")
    for period in frame["period"].unique().to_list():
        if period not in ends:
            last = frame.filter(pl.col("period") == period)["seconds"].max()
            ends[period] = period * PERIOD_S if period < OT_PERIOD else int(last)  # type: ignore[arg-type]
    return frame, ends


def parse_strength_time(
    body: bytes,
    game: FeedGame,
    raw_key: str,
    shifts: pl.DataFrame | None = None,
    lineups: pl.DataFrame | None = None,
) -> pl.DataFrame:
    """The game's seconds at each strength state for each team, as StrengthTime rows. shifts and
    lineups are passed only when the game's shift chart is complete (ADR 0009)."""
    timeline, ends = events(body, game)
    timeline = timeline.filter(~pl.col("is_penalty_shot"))
    code = pl.col("situation_code")
    counted = timeline.with_columns(
        away_net_empty=code.str.slice(0, 1) == "0",
        away_skaters=code.str.slice(1, 1).cast(pl.Int8),
        home_skaters=code.str.slice(2, 1).cast(pl.Int8),
        home_net_empty=code.str.slice(3, 1) == "0",
        strength_source=pl.lit(STRENGTH_SOURCES[0]),
    )
    if shifts is not None and lineups is not None and counted.height:
        low, high = CHART_SKATERS
        chart = (
            on_ice_counts(game, timeline, shifts, lineups)
            .filter(
                pl.col("home_skaters_on").is_between(low, high),
                pl.col("away_skaters_on").is_between(low, high),
            )
            .select("event_id", "home_skaters_on", "away_skaters_on")
        )
        found = pl.col("home_skaters_on").is_not_null()
        counted = counted.join(chart, on="event_id", how="left").with_columns(
            home_skaters=pl.when(found)
            .then(pl.col("home_skaters_on"))
            .otherwise(pl.col("home_skaters"))
            .cast(pl.Int8),
            away_skaters=pl.when(found)
            .then(pl.col("away_skaters_on"))
            .otherwise(pl.col("away_skaters"))
            .cast(pl.Int8),
            strength_source=pl.when(found)
            .then(pl.lit(STRENGTH_SOURCES[1]))
            .otherwise(pl.col("strength_source")),
        )
    end = pl.col("period").replace_strict(ends, return_dtype=pl.Int32)
    # A period's first logged play can come seconds after the puck drop in old feeds; its state
    # stands from the period's start.
    first = pl.int_range(pl.len()).over("period") == 0
    start = pl.when(first).then((pl.col("period") - 1) * PERIOD_S).otherwise(pl.col("seconds"))
    spans = counted.with_columns(
        duration=(pl.col("seconds").shift(-1).over("period").fill_null(end) - start)
    ).filter(pl.col("duration") > 0)
    sides = [
        spans.select(
            team=pl.lit(team),
            is_home=pl.lit(is_home),
            strength=pl.format("{}v{}", pl.col(f"{own}_skaters"), pl.col(f"{other}_skaters")),
            own_net_empty=pl.col(f"{own}_net_empty"),
            opp_net_empty=pl.col(f"{other}_net_empty"),
            strength_source=pl.col("strength_source"),
            duration=pl.col("duration"),
        )
        for team, is_home, own, other in (
            (game.home, True, "home", "away"),
            (game.away, False, "away", "home"),
        )
    ]
    keys = ["team", "is_home", "strength", "own_net_empty", "opp_net_empty", "strength_source"]
    rows = (
        pl.concat(sides)
        .group_by(keys)
        .agg(seconds=pl.col("duration").sum().cast(pl.Int32))
        .with_columns(
            **{name: pl.lit(value) for name, value in game.keys().items()},
            # The game's length, overtime included and shootout left out: each team's seconds
            # should add up to it (the audit's check).
            game_seconds=pl.lit(sum(e - (p - 1) * PERIOD_S for p, e in ends.items())),
            observed_utc=pl.lit(game.observed_utc),
            raw_key=pl.lit(raw_key),
        )
    )
    frame = rows.select(list(dtypes(StrengthTime))).cast(dtypes(StrengthTime))  # type: ignore[arg-type]
    return StrengthTime.validate(frame.sort(keys))
