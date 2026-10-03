"""A made-up league for the penalty-rate tests (#104): rapm_fixtures' four teams over 2010-11 and
2011-12, the season before giving the second its pulls, with penalties whose takers and drawers
follow known weights, and each team-game's strength time, shots and xG.

Each team-game the team takes POISSON penalties on average, two minutes each, at random moments;
its committer is one of its skaters, TAKER three times as likely as any other, and its drawer one
of the other team's, DRAWER three times as likely. Every fifth game adds a pair of minors that
offset and a misconduct, neither of which gives a power play. Every power play lasts
PP_SECONDS. Each team-game has one 5v5 shot and one shorthanded shot; only 2011-12 has xG, as
2010-11 has none in the lake. Everything is public at 10:00 UTC the morning after."""

from datetime import date

import numpy as np
import polars as pl
import rapm_fixtures as rfx
from goalie_fixtures import public_after

SEASONS = (20102011, 20112012)
POISSON = 3.0
TAKER = 100  # BOS's first forward
DRAWER = 300  # DET's first forward
PP_SECONDS = 108
SH_XG = 0.05
UTC = pl.Datetime("us", "UTC")


def _public(day: date) -> object:
    return public_after(day)


def league(seed: int = 11) -> dict[str, pl.DataFrame]:
    """games, stints, actual_lineups, lineups (with expected minutes), lineup_replacements,
    shift_coverage, penalties, strength_time, shots, shot_xg and players."""
    base = rfx.league(seasons=SEASONS)
    rng = np.random.default_rng(seed)
    games, lineups = base["games"], base["lineups"]
    skaters = {
        (g, t): ids
        for g, t, ids in lineups.group_by("game_id", "team", maintain_order=True)
        .agg(pl.col("player_id"))
        .iter_rows()
    }
    penalties, times, shots, xgs = [], [], [], []
    for game in games.iter_rows(named=True):
        gid, day = game["game_id"], game["game_date"]
        event = 0
        opportunities = {}
        for team, other in ((game["home"], game["away"]), (game["away"], game["home"])):
            own, theirs = skaters[gid, team], skaters[gid, other]
            take = np.array([3.0 if p == TAKER else 1.0 for p in own])
            draw = np.array([3.0 if p == DRAWER else 1.0 for p in theirs])
            count = int(rng.poisson(POISSON))
            opportunities[other] = count
            for _ in range(count):
                event += 1
                penalties.append(
                    _penalty(
                        game,
                        event,
                        team,
                        int(rng.choice(own, p=take / take.sum())),
                        int(rng.choice(theirs, p=draw / draw.sum())),
                        period=int(rng.integers(1, 4)),
                        seconds=int(rng.integers(0, 3600)),
                    )
                )
        if gid % 5 == 0:
            # A pair of minors that offset, and a misconduct.
            for team, other in ((game["home"], game["away"]), (game["away"], game["home"])):
                event += 1
                penalties.append(
                    _penalty(
                        game, event, team, skaters[gid, team][1], skaters[gid, other][1], 2, 999
                    )
                )
            event += 1
            penalties.append(
                _penalty(
                    game, event, game["home"], skaters[gid, game["home"]][2], None, 3, 1500, 10
                )
            )
        for team, other, home in (
            (game["home"], game["away"], True),
            (game["away"], game["home"], False),
        ):
            pp, pk = opportunities[team] * PP_SECONDS, opportunities[other] * PP_SECONDS
            for strength, seconds in (("5v5", 3600 - pp - pk), ("5v4", pp), ("4v5", pk)):
                if seconds > 0:
                    times.append(
                        {
                            "game_id": gid,
                            "season": game["season"],
                            "game_date": day,
                            "team": team,
                            "is_home": home,
                            "strength": strength,
                            "own_net_empty": False,
                            "opp_net_empty": False,
                            "seconds": seconds,
                            "observed_utc": _public(day),
                        }
                    )
            for k, (for_, against) in enumerate(((5, 5), (4, 5))):
                shot_id = 1000 + 10 * int(home) + k
                shots.append(
                    {
                        "game_id": gid,
                        "season": game["season"],
                        "event_id": shot_id,
                        "team": team,
                        "is_home": home,
                        "skaters_for": for_,
                        "skaters_against": against,
                        "situation_code": None,
                    }
                )
                if game["season"] == SEASONS[1]:
                    xgs.append(
                        {
                            "game_id": gid,
                            "season": game["season"],
                            "event_id": shot_id,
                            "xg": SH_XG if for_ < against else 0.1,
                            "observed_utc": _public(day),
                        }
                    )
    boxscores = lineups.join(games.select("game_id", "home"), on="game_id").select(
        "game_id",
        "season",
        "game_date",
        "team",
        is_home=pl.col("team") == pl.col("home"),
        player_id="player_id",
        role="role",
        observed_utc=pl.col("game_date").map_elements(_public, return_dtype=UTC),
    )
    expected_minutes = pl.when(pl.col("role") == "D").then(18.0).otherwise(15.0)
    projected = lineups.with_columns(
        exp_5v5=expected_minutes, exp_pp=pl.lit(2.0), exp_pk=pl.lit(2.0)
    )
    replacements = (
        lineups.select("game_id", "season", "game_date", "team")
        .unique()
        .join(pl.DataFrame({"role": ["F", "D"]}), how="cross")
        .with_columns(
            count=pl.lit(0.0), exp_5v5=pl.lit(0.0), exp_pp=pl.lit(0.0), exp_pk=pl.lit(0.0)
        )
    )
    return {
        "games": games,
        "stints": base["stints"],
        "actual_lineups": boxscores,
        "lineups": projected,
        "lineup_replacements": replacements,
        "shift_coverage": base["stints"]
        .select("game_id", "season")
        .unique()
        .with_columns(complete=pl.lit(True)),
        "penalties": pl.DataFrame(penalties).with_columns(
            pl.col("season").cast(pl.Int32),
            pl.col("period").cast(pl.Int8),
            pl.col("duration_min").cast(pl.Int8),
            pl.col("committed_by", "drawn_by").cast(pl.Int64),
            pl.col("observed_utc").cast(UTC),
        ),
        "strength_time": pl.DataFrame(times).with_columns(
            pl.col("season").cast(pl.Int32), pl.col("observed_utc").cast(UTC)
        ),
        "shots": pl.DataFrame(shots, schema_overrides={"situation_code": pl.String}),
        "shot_xg": pl.DataFrame(xgs).with_columns(pl.col("observed_utc").cast(UTC)),
        "players": lineups.select("player_id").unique().with_columns(name=pl.lit("A Skater")),
    }


def _penalty(
    game: dict[str, object],
    event: int,
    team: str,
    committer: int | None,
    drawer: int | None,
    period: int,
    seconds: int,
    minutes: int = 2,
) -> dict[str, object]:
    day = game["game_date"]
    assert isinstance(day, date)
    return {
        "game_id": game["game_id"],
        "season": game["season"],
        "game_date": day,
        "event_id": event,
        "period": period,
        "seconds": seconds,
        "team": team,
        "is_home": team == game["home"],
        "committed_by": committer,
        "drawn_by": drawer,
        "duration_min": minutes,
        "observed_utc": _public(day),
    }
