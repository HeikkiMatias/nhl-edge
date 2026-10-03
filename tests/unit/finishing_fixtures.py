"""A made-up league for the finishing tests (#105): rapm_fixtures' four teams over 2011-12 and
2012-13, the first giving the second its pulls, with shots whose shooters and goals follow known
weights, two goalies per team, and their goalie effects.

Each team-game the team takes SHOTS unblocked shots with xG drawn from 0.02 to 0.2; its shooter
is one of its skaters, SNIPER three times as likely as any other and scoring at FINISH times his
xG, everyone else at his xG. Each team has a good goalie (effect GOOD, saving more than expected)
and a weak one (effect -GOOD), starting on alternate nights. Everything is public at 10:00 UTC
the morning after."""

from datetime import date

import numpy as np
import polars as pl
import rapm_fixtures as rfx
from goalie_fixtures import public_after

SEASONS = (20112012, 20122013)
SHOTS = 30
SNIPER = 100  # BOS's first forward
FINISH = 2.0
GOOD = 0.006
UTC = pl.Datetime("us", "UTC")


def goalies(team: str) -> tuple[int, int]:
    """The team's good goalie and its weak one."""
    base = 9000 + 10 * rfx.TEAMS.index(team)
    return base, base + 1


def league(seed: int = 13) -> dict[str, pl.DataFrame]:
    """games, stints, actual_lineups (with goalies), lineups (with expected minutes),
    lineup_replacements, shift_coverage, shots, shot_xg, goalie_effects and players."""
    base = rfx.league(seasons=SEASONS)
    rng = np.random.default_rng(seed)
    games, lineups = base["games"], base["lineups"]
    cutoff = public_after(date(2011, 5, 31))
    skaters = {
        (g, t): ids
        for g, t, ids in lineups.group_by("game_id", "team", maintain_order=True)
        .agg(pl.col("player_id"))
        .iter_rows()
    }
    shots, xgs, goalie_rows, effects = [], [], [], []
    for k, game in enumerate(games.iter_rows(named=True)):
        gid, day = game["game_id"], game["game_date"]
        public = public_after(day)
        event = 0
        for team, home in ((game["home"], True), (game["away"], False)):
            own = skaters[gid, team]
            weights = np.array([3.0 if p == SNIPER else 1.0 for p in own])
            for _ in range(SHOTS):
                event += 1
                shooter = int(rng.choice(own, p=weights / weights.sum()))
                xg = float(rng.uniform(0.02, 0.2))
                finish = FINISH if shooter == SNIPER else 1.0
                shots.append(
                    {
                        "game_id": gid,
                        "season": game["season"],
                        "event_id": event,
                        "team": team,
                        "shooter_id": shooter,
                        "is_goal": bool(rng.random() < min(1.0, xg * finish)),
                        "observed_utc": public,
                    }
                )
                xgs.append(
                    {
                        "game_id": gid,
                        "season": game["season"],
                        "event_id": event,
                        "xg": xg,
                        "observed_utc": public,
                    }
                )
            good, weak = goalies(team)
            starter = good if k % 2 == 0 else weak
            for goalie, effect in ((good, GOOD), (weak, -GOOD)):
                goalie_rows.append(
                    {
                        "game_id": gid,
                        "season": game["season"],
                        "game_date": day,
                        "team": team,
                        "is_home": home,
                        "player_id": goalie,
                        "role": "G",
                        "starting_goalie": goalie == starter,
                        "observed_utc": public,
                    }
                )
                effects.append(
                    {
                        "game_id": gid,
                        "season": game["season"],
                        "game_date": day,
                        "team": team,
                        "goalie_id": goalie,
                        "effect": effect,
                        "train_cutoff": cutoff,
                    }
                )
    skater_rows = lineups.join(games.select("game_id", "home"), on="game_id").select(
        "game_id",
        "season",
        "game_date",
        "team",
        is_home=pl.col("team") == pl.col("home"),
        player_id="player_id",
        role="role",
        starting_goalie=pl.lit(False),
        observed_utc=pl.col("game_date").map_elements(public_after, return_dtype=UTC),
    )
    boxscores = pl.concat(
        [
            skater_rows,
            pl.DataFrame(goalie_rows).select(skater_rows.columns).cast(skater_rows.schema),
        ]
    )
    expected_minutes = pl.when(pl.col("role") == "D").then(18.0).otherwise(15.0)
    projected = lineups.with_columns(
        exp_5v5=expected_minutes,
        exp_pp=pl.lit(2.0),
        exp_pk=pl.lit(2.0),
        train_cutoff=pl.lit(cutoff, UTC),
    )
    replacements = (
        lineups.select("game_id", "season", "game_date", "team")
        .unique()
        .join(pl.DataFrame({"role": ["F", "D"]}), how="cross")
        .with_columns(
            count=pl.lit(0.0),
            exp_5v5=pl.lit(0.0),
            exp_pp=pl.lit(0.0),
            exp_pk=pl.lit(0.0),
            train_cutoff=pl.lit(cutoff, UTC),
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
        "shots": pl.DataFrame(shots).with_columns(
            pl.col("season").cast(pl.Int32), pl.col("observed_utc").cast(UTC)
        ),
        "shot_xg": pl.DataFrame(xgs).with_columns(
            pl.col("season").cast(pl.Int32), pl.col("observed_utc").cast(UTC)
        ),
        "goalie_effects": pl.DataFrame(effects).with_columns(
            pl.col("season").cast(pl.Int32), pl.col("train_cutoff").cast(UTC)
        ),
        "players": lineups.select("player_id").unique().with_columns(name=pl.lit("A Skater")),
    }
