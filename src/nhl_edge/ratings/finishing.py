"""Finishing φ and goalie conversion gamma (#105, ADR 0022, docs/plan.md §5): the multipliers that
turn a team's expected goals into goals, its projected shooters' finishing and the opposing
goalie's conversion.

**Shots.** Unblocked shots with xG (ADR 0010) whose shooter is a skater in the game's boxscore,
summed per skater-game with stints, beside his minutes at 5v5, on the power play and on the
penalty kill (lineup.minutes.player_minutes). Games without xG or stints add nothing.

**Memory.** Every sum decays by 0.5 ** (d / half-life), d league game days back from the latest
date read, with RAPM's frozen half-life (#103), over a skater's earlier games on any team public
before team strength's as-of time.

**A skater's φ** = (G + c) / (κ·X + c): G his weighted goals, X his weighted xG, κ the league's
goals over xG with the same weights (so φ = 1 is league-average finishing) and c the pull in
expected goals for his role (season_pulls). His sd is √(G + c) / (κ·X + c). A candidate without
shots is at 1, and so is every candidate of a season without an earlier season to measure c on.

**A skater's xG rate** = (X + c_x·rho) / (H + c_x): H his weighted hours, rho his role's xG
per hour with the same weights, c_x the pull in hours.

**A team's φ** weights each candidate by his share of its expected xG, e·r over the team's
Σ e·r, e his expected minutes (exp_5v5 + exp_pp + exp_pk, his probability of dressing in) and r
his xG rate; the replacement skaters take their minutes at their role's rate and φ = 1.

**A goalie's gamma** = 1 - effect / s for each candidate goalie of goalie_effects, s the
league's xG per unblocked shot over the shots public before the as-of time in the game's season
and the one before (team strength's league window); 1 before any shot with xG is public. B3
multiplies a team's expected goals by its φ times the opposing goalie's gamma.

Nothing is tuned: the memory is RAPM's, the goalie effect keeps its frozen settings (ADR 0011),
and the pulls and league figures are measured on earlier data.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

import numpy as np
import polars as pl

from nhl_edge.features import goalie as ge
from nhl_edge.features import team_strength as ts
from nhl_edge.lake.schemas import Finishing, GoalMultipliers, dtypes
from nhl_edge.lineup.goalie_start import season_cutoff
from nhl_edge.lineup.minutes import STATES
from nhl_edge.lineup.projection import SKATER_ROLES
from nhl_edge.ratings import decayed, rapm

COMPONENT = "finishing"
HALF_LIFE_DAYS = rapm.TUNED.half_life_days
# Skaters with fewer games with stints across the earlier seasons don't enter the pulls.
PULL_MIN_GAMES = 20
# The memory (#103) and the goalie effect's settings (#75) were tuned on the training seasons
# (ADR 0011).
TRAIN_CUTOFF = max(rapm.TRAIN_CUTOFF, ge.TUNED_CUTOFF)
# The sums kept per player and role.
VALUES = {"goals": "goals", "xg": "xg", "hours": "hours"}


def shooter_games(
    minutes: pl.DataFrame, shots: pl.DataFrame, shot_xg: pl.DataFrame, games: pl.DataFrame
) -> pl.DataFrame:
    """One row per skater and game with stints and xG (minutes, lineup.minutes.player_minutes):
    his role, hours at 5v5, on the power play and on the penalty kill, his xG, the sum of his
    shots' squared xG (xg_sq, for the noise of the pulls), his goals on those shots, the game's
    league day, and when both the stints and the shots were public."""
    numbers = rapm.league_days(games)
    shot_rows = shot_xg.select("game_id", "event_id", "xg", xg_utc="observed_utc").join(
        shots.select("game_id", "event_id", "team", "shooter_id", "is_goal", "observed_utc"),
        on=["game_id", "event_id"],
    )
    per = shot_rows.group_by("game_id", "team", player_id="shooter_id").agg(
        xg=pl.col("xg").sum(),
        xg_sq=(pl.col("xg") ** 2).sum(),
        goals=pl.col("is_goal").sum().cast(pl.Float64),
    )
    published = shot_rows.group_by("game_id").agg(
        shots_utc=decayed.later(pl.col("observed_utc").max(), pl.col("xg_utc").max())
    )
    return (
        minutes.join(published, on="game_id", how="inner")
        .join(per, on=["game_id", "team", "player_id"], how="left")
        .select(
            "game_id",
            "season",
            "game_date",
            "team",
            "player_id",
            "role",
            hours=pl.sum_horizontal(*STATES) / 60,
            xg=pl.col("xg").fill_null(0.0),
            xg_sq=pl.col("xg_sq").fill_null(0.0),
            goals=pl.col("goals").fill_null(0.0),
            day=pl.col("game_date").replace_strict(numbers, return_dtype=pl.Float64),
            observed_utc=decayed.later(pl.col("observed_utc"), pl.col("shots_utc")),
        )
        .sort("observed_utc", "game_date", "game_id", "player_id")
    )


@dataclass(frozen=True)
class SeasonPulls:
    """A season's pulls per role, measured on every earlier season: finishing's in expected
    goals and the xG rate's in hours, infinite without an earlier season. cutoff is when the
    last of the earlier games was public, None without any."""

    season: int
    finishing: dict[str, float]
    rate: dict[str, float]
    cutoff: datetime | None


def season_pulls(rows: pl.DataFrame, season: int, games: pl.DataFrame) -> SeasonPulls:
    """The season's pulls from every earlier season's shooter_games, over the skaters of a role
    with PULL_MIN_GAMES games or more across them, each counted once with his totals
    (decayed.moments_pull). For finishing, each season's shots are set against that season's
    own goals over xG (its expected goals, E), and a goal's noise is binomial: 1 - ΣE² / ΣE of
    the shots' expected goals. For the xG rate, xG is a sum of the shots' probabilities, whose
    noise is Σ xG² / Σ xG. Refuses when an earlier game was public only at or after the season's
    first as-of time."""
    earlier = rows.filter(pl.col("season") < season)
    if earlier.is_empty():
        infinite = {role: np.inf for role in SKATER_ROLES}
        return SeasonPulls(season, dict(infinite), dict(infinite), None)
    kappa = earlier.group_by("season").agg(kappa=pl.col("goals").sum() / pl.col("xg").sum())
    scaled = earlier.join(kappa, on="season").with_columns(
        expected=pl.col("xg") * pl.col("kappa"),
        expected_sq=pl.col("xg_sq") * pl.col("kappa") ** 2,
    )
    players = decayed.totals(
        scaled,
        ["role", "player_id"],
        ["goals", "expected", "expected_sq", "xg", "xg_sq", "hours"],
    ).filter(pl.col("games") >= PULL_MIN_GAMES, pl.col("hours") > 0, pl.col("xg") > 0)
    finishing, rate = {}, {}
    for role in SKATER_ROLES:
        own = players.filter(pl.col("role") == role)
        if own.height < 2:
            raise ValueError(f"too few {role} with {PULL_MIN_GAMES} games before {season}")
        # Seasons without a goal give no expected goals and nothing to measure.
        scored = own.filter(pl.col("expected") > 0)
        if scored.height < 2:
            finishing[role] = np.inf
        else:
            goal_noise = 1 - float(scored["expected_sq"].sum()) / float(scored["expected"].sum())
            finishing[role] = decayed.moments_pull(
                scored["goals"].to_numpy(), scored["expected"].to_numpy(), goal_noise
            )
        xg_noise = float(own["xg_sq"].sum()) / float(own["xg"].sum())
        rate[role] = decayed.moments_pull(own["xg"].to_numpy(), own["hours"].to_numpy(), xg_noise)
    cutoff = earlier["observed_utc"].max()
    assert isinstance(cutoff, datetime)
    first = season_cutoff(games, season)
    if cutoff >= first:
        raise ValueError(
            f"games before {season} were public at {cutoff}, not before its first as-of {first}"
        )
    return SeasonPulls(season, finishing, rate, cutoff)


def league_rates(rows: pl.DataFrame, times: pl.DataFrame) -> pl.DataFrame:
    """At each as_of_utc of times, over the shooter_games public before it with the same
    weights: the league's goals over xG (league_finishing) and each role's xG per hour
    (rho_<role>); null before any."""
    targets = (
        times.select("as_of_utc")
        .unique()
        .join(pl.DataFrame({"role": list(SKATER_ROLES)}), how="cross")
    )
    joined = decayed.before(targets, decayed.role_history(rows, VALUES, HALF_LIFE_DAYS), by="role")
    wide = joined.pivot(on="role", index="as_of_utc", values=["r_goals", "r_xg", "r_hours"])
    goals = pl.sum_horizontal(*(pl.col(f"r_goals_{r}").fill_null(0.0) for r in SKATER_ROLES))
    xg = pl.sum_horizontal(*(pl.col(f"r_xg_{r}").fill_null(0.0) for r in SKATER_ROLES))
    return wide.select(
        "as_of_utc",
        league_finishing=pl.when(xg > 0).then(goals / xg),
        **{f"rho_{r}": pl.col(f"r_xg_{r}") / pl.col(f"r_hours_{r}") for r in SKATER_ROLES},
    )


def rates(
    candidates: pl.DataFrame, rows: pl.DataFrame, pulls: Mapping[int, SeasonPulls]
) -> pl.DataFrame:
    """Each candidate's φ and xG rate (Finishing's columns but the share and the stamps):
    candidates holds game_id, season, game_date, team, player_id, role and as_of_utc; rows is
    shooter_games; pulls the scored seasons' SeasonPulls."""
    missing = sorted(set(candidates["season"].unique().to_list()) - set(pulls))
    if missing:
        raise ValueError(f"no pulls for {missing}")
    table = pl.DataFrame(
        [
            {
                "season": s,
                "role": r,
                "finishing_pull": p.finishing[r],
                "rate_pull": p.rate[r],
            }
            for s, p in pulls.items()
            for r in SKATER_ROLES
        ],
        schema={
            "season": pl.Int32,
            "role": pl.String,
            "finishing_pull": pl.Float64,
            "rate_pull": pl.Float64,
        },
    )
    league = league_rates(rows, candidates)
    joined = decayed.before(
        candidates, decayed.player_history(rows, VALUES, HALF_LIFE_DAYS), by="player_id"
    )
    joined = decayed.before(joined.drop("observed_utc"), decayed.batches(rows))
    scale = decayed.scale(HALF_LIFE_DAYS)
    frame = (
        joined.drop("observed_utc")
        .join(league, on="as_of_utc", how="left")
        .join(table, on=["season", "role"], how="left")
        .with_columns(
            goals=(pl.col("g_goals") * scale).fill_null(0.0),
            xg=(pl.col("g_xg") * scale).fill_null(0.0),
            hours=(pl.col("g_hours") * scale).fill_null(0.0),
            xg_rate_prior=pl.when(pl.col("role") == "D")
            .then(pl.col("rho_D"))
            .otherwise(pl.col("rho_F")),
        )
        .with_columns(expected_goals=pl.col("xg") * pl.col("league_finishing").fill_null(1.0))
    )
    c, cx = pl.col("finishing_pull"), pl.col("rate_pull")
    pulled = pl.col("goals") + c
    return frame.select(
        "game_id",
        "season",
        "game_date",
        "team",
        "player_id",
        "role",
        phi=pl.when(c.is_finite()).then(pulled / (pl.col("expected_goals") + c)).otherwise(1.0),
        phi_sd=pl.when(c.is_finite())
        .then(pulled.sqrt() / (pl.col("expected_goals") + c))
        .otherwise(0.0),
        goals="goals",
        expected_goals="expected_goals",
        xg_rate=pl.when(cx.is_finite())
        .then((pl.col("xg") + cx * pl.col("xg_rate_prior")) / (pl.col("hours") + cx))
        .otherwise(pl.col("xg_rate_prior")),
        xg_rate_prior="xg_rate_prior",
        hours="hours",
        finishing_pull="finishing_pull",
        rate_pull="rate_pull",
        league_finishing="league_finishing",
        known_utc="known_utc",
        half_life_days=pl.lit(HALF_LIFE_DAYS),
        as_of_utc="as_of_utc",
    ).sort("game_id", "player_id")


def shot_figures(shots: pl.DataFrame, shot_xg: pl.DataFrame, times: pl.DataFrame) -> pl.DataFrame:
    """For each (season, as_of_utc) of times, over the games' unblocked shots with xG public
    before as_of_utc in that season and the one before: the league's xG per shot (xg_per_shot,
    null without any) and the latest game read (known_utc)."""
    per_game = (
        shot_xg.select("game_id", "season", "event_id", "xg", xg_utc="observed_utc")
        .join(shots.select("game_id", "event_id", "observed_utc"), on=["game_id", "event_id"])
        .group_by("game_id", "season")
        .agg(
            shots=pl.len().cast(pl.Float64),
            xg=pl.col("xg").sum(),
            observed_utc=decayed.later(pl.col("observed_utc").max(), pl.col("xg_utc").max()),
        )
    )
    out = []
    for (season,), targets in times.select("season", "as_of_utc").unique().group_by("season"):
        past = per_game.filter(pl.col("season").is_in([season, season - 10_001])).sort(
            "observed_utc", "game_id"
        )
        totals = past.select("shots", "xg").to_numpy().astype(float).cumsum(axis=0)
        seen = np.searchsorted(
            past["observed_utc"].to_numpy(), targets["as_of_utc"].to_numpy(), side="left"
        )
        has = seen > 0
        state = np.full((targets.height, 2), np.nan)
        state[has] = totals[seen[has] - 1]
        known = past["observed_utc"].gather(np.where(has, seen - 1, 0).tolist())
        out.append(
            targets.with_columns(
                xg_per_shot=pl.Series(state[:, 1] / state[:, 0]).fill_nan(None),
                known_utc=pl.when(pl.Series(has)).then(known),
            )
        )
    return pl.concat(out)


def multipliers(
    rated: pl.DataFrame,
    candidates: pl.DataFrame,
    replacements: pl.DataFrame,
    roles: pl.DataFrame,
    effects: pl.DataFrame,
    figures: pl.DataFrame,
    games: pl.DataFrame,
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """rated with each candidate's share of his team's expected xG (share) and the game's
    lineups' cutoff (lineup_cutoff, for stamp), and the goal
    multipliers (GoalMultipliers' columns but the stamps, with lineup_cutoff and effect_cutoff
    for stamp): per game, attacking team and the opposing team's candidate goalie (effects,
    goalie_effects rows), the team's φ, the goalie's gamma and their product. candidates are the
    lineups rows of the skaters, replacements the lineup_replacements rows, roles is
    league_rates() at the games' as-of times and figures shot_figures(). A team-game without
    candidates is all replacements, at φ = 1."""
    seasons = rated["season"].unique().implode()
    scheduled = games.filter(pl.col("season").is_in(seasons)).select(
        "game_id",
        "season",
        "game_date",
        "home",
        "away",
        as_of_utc=ts.as_of(pl.col("game_date"), pl.col("start_utc")),
    )
    sides = pl.concat(
        [
            scheduled.select(
                "game_id",
                "season",
                "game_date",
                "as_of_utc",
                team=pl.col(side),
                opponent=pl.col(other),
            )
            for side, other in (("home", "away"), ("away", "home"))
        ]
    )
    expected_minutes = pl.sum_horizontal(
        *(pl.col(f"exp_{s}").fill_null(0.0) for s in ("5v5", "pp", "pk"))
    )
    weighted = rated.join(
        candidates.select("game_id", "team", "player_id", e=expected_minutes),
        on=["game_id", "team", "player_id"],
        how="left",
    ).with_columns(
        # Before any xG is public, shares follow minutes.
        weight=pl.col("e").fill_null(0.0) * pl.col("xg_rate").fill_null(1.0)
    )
    rho = roles.unpivot(
        index="as_of_utc", on=[f"rho_{r}" for r in SKATER_ROLES], variable_name="role"
    ).with_columns(pl.col("role").str.strip_prefix("rho_"), rho=pl.col("value").fill_null(1.0))
    spare = (
        replacements.select("game_id", "team", "role", e=expected_minutes)
        .join(sides.select("game_id", "team", "as_of_utc"), on=["game_id", "team"], how="inner")
        .join(rho.select("as_of_utc", "role", "rho"), on=["as_of_utc", "role"], how="left")
        .group_by("game_id", "team")
        .agg(spare=(pl.col("e") * pl.col("rho").fill_null(1.0)).sum())
    )
    team_weights = weighted.group_by("game_id", "team").agg(
        total=pl.col("weight").sum(), finishing=(pl.col("weight") * pl.col("phi")).sum()
    )
    teams = (
        sides.join(team_weights, on=["game_id", "team"], how="left")
        .join(spare, on=["game_id", "team"], how="left")
        .with_columns(pl.col("total", "finishing", "spare").fill_null(0.0))
        .with_columns(denominator=pl.col("total") + pl.col("spare"))
        .with_columns(
            phi=pl.when(pl.col("denominator") > 0)
            .then((pl.col("finishing") + pl.col("spare")) / pl.col("denominator"))
            .otherwise(1.0)
        )
    )
    shared = (
        weighted.join(
            teams.select("game_id", "team", "denominator"), on=["game_id", "team"], how="left"
        )
        .with_columns(
            share=pl.when(pl.col("denominator") > 0)
            .then(pl.col("weight") / pl.col("denominator"))
            .otherwise(0.0)
        )
        .drop("e", "weight", "denominator")
    )
    read = rated.group_by("game_id").agg(rates_utc=pl.col("known_utc").max())
    fitted = (
        pl.concat(
            [
                candidates.select("game_id", "train_cutoff"),
                replacements.select("game_id", "train_cutoff"),
            ]
        )
        .group_by("game_id")
        .agg(lineup_cutoff=pl.col("train_cutoff").max())
    )
    # A share reads the game's lineups too.
    shared = shared.join(fitted, on="game_id", how="left")
    finishing_by_as_of = roles.select("as_of_utc", "league_finishing")
    goalies = effects.select(
        "game_id",
        opponent="team",
        goalie_id="goalie_id",
        effect="effect",
        effect_cutoff="train_cutoff",
    )
    rows = (
        teams.select("game_id", "season", "game_date", "as_of_utc", "team", "opponent", "phi")
        .join(goalies, on=["game_id", "opponent"], how="inner")
        .join(
            figures.select("season", "as_of_utc", "xg_per_shot", shots_utc="known_utc"),
            on=["season", "as_of_utc"],
            how="left",
        )
        .join(finishing_by_as_of, on="as_of_utc", how="left")
        .join(read, on="game_id", how="left")
        .join(fitted, on="game_id", how="left")
        .with_columns(
            gamma=pl.when(pl.col("xg_per_shot").is_not_null())
            .then(1 - pl.col("effect") / pl.col("xg_per_shot"))
            .otherwise(1.0)
        )
        .select(
            "game_id",
            "season",
            "game_date",
            "team",
            "opponent",
            "goalie_id",
            "phi",
            "gamma",
            multiplier=pl.col("phi") * pl.col("gamma"),
            xg_per_shot="xg_per_shot",
            league_finishing="league_finishing",
            known_utc=decayed.later(pl.col("shots_utc"), pl.col("rates_utc")),
            as_of_utc="as_of_utc",
            lineup_cutoff="lineup_cutoff",
            effect_cutoff="effect_cutoff",
        )
        .sort("game_id", "team", "goalie_id")
    )
    return shared.sort("game_id", "player_id"), rows


def stamp(
    frame: pl.DataFrame,
    schema: type[Finishing] | type[GoalMultipliers],
    pulls: Mapping[int, SeasonPulls],
    version: str,
) -> pl.DataFrame:
    """The rows with train_cutoff, the latest of the memory's and the goalie settings' tuning
    cutoff, the season's pulls' cutoff, the game's lineups' (lineup_cutoff) and, for goal
    multipliers, the goalie effect's (effect_cutoff), artifact_version, and observed_utc, the
    later of as_of_utc and train_cutoff."""
    cutoffs = pl.DataFrame(
        {
            "season": list(pulls),
            "train_cutoff": [
                TRAIN_CUTOFF if p.cutoff is None else max(TRAIN_CUTOFF, p.cutoff)
                for p in pulls.values()
            ],
        },
        schema={"season": pl.Int32, "train_cutoff": pl.Datetime("us", "UTC")},
    )
    stamped = frame.join(cutoffs, on="season", how="left")
    for column in ("lineup_cutoff", "effect_cutoff"):
        if column in stamped.columns:
            stamped = stamped.with_columns(
                train_cutoff=decayed.later(pl.col("train_cutoff"), pl.col(column))
            )
    stamped = stamped.with_columns(
        artifact_version=pl.lit(version),
        observed_utc=decayed.later(pl.col("as_of_utc"), pl.col("train_cutoff")),
    )
    columns = dtypes(schema)
    return schema.validate(stamped.select(list(columns)).cast(columns))  # type: ignore[arg-type]
