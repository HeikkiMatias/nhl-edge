"""Projected ice time (#100, ADR 0018): before a game, each candidate skater's expected minutes at
5v5, on the power play and on the penalty kill, his team's power-play unit 1, and the replacement
skaters for the newcomers the lineup model expects. B3 weights each skater's ratings by these
minutes (docs/plan.md §5).

**Minutes if he dresses.** For each state, a decayed average of his earlier games for the team's
line of team codes that have stints, public before the prediction time: weights 0.5^(k / 10), k
games back from his latest (a half-life of HALF_LIFE of his games). It is pulled toward his role's
average minutes as if that were PULL games more, where the pull is the game-to-game variance of
one player's minutes over the variance between players' averages. The average and the pull come
from the season before, per role and state. A candidate with no such game gets the average.

**Expected minutes.** His probability of dressing (ADR 0017) times his minutes, scaled per
team-game, role and state so that the candidates and the replacement skaters add up to the
league's average skater-minutes of the role in the state per team-game, the season before. The
replacements of a role are the slots (12 forwards, 6 defense) the candidates leave short, each
with a newcomer's average minutes the season before: one average for a team's first game of a
season and one for its other games. 2011-12's first games use the other average, since 2010-11's
first games had no candidates and so no newcomers.

**Power-play unit 1:** the five candidates with the most expected power-play minutes.

Nothing is tuned: the half-life is the owner's choice, and the averages, pulls, totals and
newcomer minutes are measured on the season before, all public before the season's first as-of
time. A game's own stints are read only to score the projection.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

import polars as pl

from nhl_edge.features.stints import player_seconds
from nhl_edge.features.team_strength import as_of, team_lines
from nhl_edge.lake.schemas import PP_UNIT, SKATER_SLOTS, LineupReplacements, Lineups, dtypes
from nhl_edge.lineup.goalie_start import season_cutoff
from nhl_edge.lineup.projection import SKATER_ROLES

if TYPE_CHECKING:
    from nhl_edge.lake.tables import Lake

STATES = ("5v5", "pp", "pk")
EXPECTED = {state: f"exp_{state}" for state in STATES}
HALF_LIFE = 10
DECAY = 0.5 ** (1 / HALF_LIFE)
# Players with fewer games in the season before don't enter the pull's variances.
PULL_MIN_GAMES = 20


def player_minutes(
    stints: pl.DataFrame, boxscores: pl.DataFrame, lines: Mapping[str, str] | None = None
) -> pl.DataFrame:
    """One row per game with stints and skater who dressed in it: his team, its line of team
    codes, his role in the boxscore, his minutes in each state of STATES (0 when he had none) and
    when the game's stints became public."""
    lines = team_lines() if lines is None else lines
    seconds = player_seconds(stints).filter(pl.col("state").is_in(STATES))
    per_player = seconds.group_by("game_id", "player_id").agg(
        **{state: pl.col("seconds").filter(pl.col("state") == state).sum() / 60 for state in STATES}
    )
    games = stints.group_by("game_id").agg(pl.col("observed_utc").max())
    dressed = boxscores.filter(pl.col("role").is_in(SKATER_ROLES)).select(
        "game_id", "season", "game_date", "team", "player_id", "role"
    )
    return (
        dressed.join(games, on="game_id", how="inner")
        .join(per_player, on=["game_id", "player_id"], how="left")
        .with_columns(
            *(pl.col(state).fill_null(0.0).cast(pl.Float64) for state in STATES),
            line=pl.col("team").replace(dict(lines)),
        )
        .sort("game_id", "team", "player_id")
    )


@dataclass(frozen=True)
class SeasonConstants:
    """What a season's projection takes from the season before (source), per role and state: a
    dressed skater's average minutes, the pull in games, the skater-minutes per team-game, and a
    newcomer's average minutes in a team's first game of a season (True) and its other games.
    cutoff is when the last of the source season's games became public."""

    season: int
    source: int
    mean: dict[tuple[str, str], float]
    pull: dict[tuple[str, str], float]
    total: dict[tuple[str, str], float]
    newcomer: dict[tuple[str, bool, str], float]
    cutoff: datetime


def previous_season(season: int) -> int:
    return season - 10_001


def season_constants(
    minutes: pl.DataFrame, rows: pl.DataFrame, season: int, games: pl.DataFrame
) -> SeasonConstants:
    """The constants for season from the season before. minutes is player_minutes; rows are the
    lineup model's candidates (projection.candidates), which tell the newcomers (dressed skaters
    who were not candidates) and a team's first game of a season. Refuses when a game of the
    season before was public only at or after the season's first as-of time (games)."""
    source = previous_season(season)
    played = minutes.filter(pl.col("season") == source)
    if played.is_empty():
        raise ValueError(f"no games with stints in {source} to project {season}'s minutes")
    mean, pull, total = {}, {}, {}
    per_team_game = played.group_by("game_id", "team", "role").agg(pl.col(STATES).sum())
    for role in SKATER_ROLES:
        own = played.filter(pl.col("role") == role)
        for state in STATES:
            mean[role, state] = float(own[state].mean())  # type: ignore[arg-type]
            total[role, state] = float(
                per_team_game.filter(pl.col("role") == role)[state].mean()  # type: ignore[arg-type]
            )
            pull[role, state] = _pull(own, state, role, source)
    candidates = rows.filter(pl.col("season") == source)
    openers = candidates.group_by("game_id", "team").agg(pl.col("opener").first())
    newcomers = played.join(
        candidates.select("game_id", "team", "player_id"),
        on=["game_id", "team", "player_id"],
        how="anti",
    ).join(openers, on=["game_id", "team"], how="inner")
    newcomer: dict[tuple[str, bool, str], float] = {}
    for role in SKATER_ROLES:
        own = newcomers.filter(pl.col("role") == role)
        other, first = own.filter(~pl.col("opener")), own.filter(pl.col("opener"))
        if other.is_empty():
            raise ValueError(f"no {role} newcomers in {source} to project {season}'s minutes")
        for state in STATES:
            newcomer[role, False, state] = float(other[state].mean())  # type: ignore[arg-type]
            # 2010-11's first games had no candidates: 2011-12's use the other average.
            newcomer[role, True, state] = (
                float(first[state].mean()) if first.height else newcomer[role, False, state]  # type: ignore[arg-type]
            )
    cutoff = played["observed_utc"].max()
    assert isinstance(cutoff, datetime)
    first = season_cutoff(games, season)
    if cutoff >= first:
        raise ValueError(
            f"{source}'s stints were public at {cutoff}, not before {season}'s first as-of {first}"
        )
    return SeasonConstants(season, source, mean, pull, total, newcomer, cutoff)


def _pull(own: pl.DataFrame, state: str, role: str, source: int) -> float:
    """The game-to-game variance of one player's minutes over the variance between players'
    averages, over the players with at least PULL_MIN_GAMES games."""
    players = (
        own.group_by("player_id")
        .agg(mean=pl.col(state).mean(), var=pl.col(state).var(), games=pl.len())
        .filter(pl.col("games") >= PULL_MIN_GAMES)
    )
    if players.height < 2:
        raise ValueError(f"too few {role} with {PULL_MIN_GAMES} games in {source} for the pull")
    within = float(players["var"].mean())  # type: ignore[arg-type]
    between = float(players["mean"].var()) - within / float(players["games"].mean())  # type: ignore[arg-type]
    if between <= 0:
        raise ValueError(f"{source} {role} {state}: players' averages don't differ beyond noise")
    return within / between


def history(minutes: pl.DataFrame) -> pl.DataFrame:
    """Each player's decayed sums for his team's line after each of his games for it, in the
    order they became public (game_utc): w, the sum of the weights, and sum_<state>, the weighted
    minutes, with weights 0.5^(k / HALF_LIFE) for the game k games back; last_<state> is the
    game's own minutes, the reference's guess."""
    ordered = minutes.sort("observed_utc", "game_date", "game_id")
    index = pl.int_range(pl.len()).over("line", "player_id").cast(pl.Float64)
    # sum_n = Σ_j x_j DECAY^(n - j), as DECAY^n Σ_j x_j DECAY^-j: a career of 2,000 games keeps
    # DECAY^-j under 1e61, well inside a float's range.
    grow, shrink = DECAY**-index, DECAY**index
    return ordered.select(
        "line",
        "player_id",
        game_utc="observed_utc",
        w=grow.cum_sum().over("line", "player_id") * shrink,
        **{
            f"sum_{state}": (pl.col(state) * grow).cum_sum().over("line", "player_id") * shrink
            for state in STATES
        },
        **{f"last_{state}": pl.col(state) for state in STATES},
    ).sort("game_utc")


def project(
    scored: pl.DataFrame,
    minutes: pl.DataFrame,
    constants: Mapping[int, SeasonConstants],
    games: pl.DataFrame,
    lines: Mapping[str, str] | None = None,
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """The candidates of scored (with p_available) with their minutes if they dress (min_<state>,
    scaled), expected minutes (exp_<state>), pp_unit, and the reference's last_<state>; and the
    replacement skaters per team-game and role: their expected count and minutes (exp_<state>,
    in all). Each season's constants come from constants. Every team-game of games in the scored
    seasons gets replacements: one without candidates, such as a new team's first game, is all
    replacements, as a team's first game of a season."""
    lines = team_lines() if lines is None else lines
    seasons = sorted(scored["season"].unique().to_list())
    missing = [season for season in seasons if season not in constants]
    if missing:
        raise ValueError(f"no ice-time constants for {missing}")
    per_role = _per_role(constants, seasons)
    rows = (
        scored.with_columns(line=pl.col("team").replace(dict(lines)))
        .sort("as_of_utc")
        .join_asof(
            history(minutes),
            left_on="as_of_utc",
            right_on="game_utc",
            by=["line", "player_id"],
            strategy="backward",
            allow_exact_matches=False,
            check_sortedness=False,  # both sides are sorted by time above
        )
        .join(per_role, on=["season", "role"], how="left")
        .with_columns(
            **{
                f"raw_{state}": pl.when(pl.col("w").is_null())
                .then(pl.col(f"mean_{state}"))
                .otherwise(
                    (pl.col(f"sum_{state}") + pl.col(f"pull_{state}") * pl.col(f"mean_{state}"))
                    / (pl.col("w") + pl.col(f"pull_{state}"))
                )
                for state in STATES
            }
        )
    )
    slots = pl.DataFrame(
        {"role": list(SKATER_SLOTS), "slots": list(SKATER_SLOTS.values())},
        schema={"role": pl.String, "slots": pl.Float64},
    )
    with_candidates = rows.select(
        "game_id", "season", "game_date", "team", "opener", "as_of_utc"
    ).unique(subset=["game_id", "team"])
    scheduled = pl.concat(
        [
            games.filter(pl.col("season").is_in(seasons)).select(
                "game_id",
                "season",
                "game_date",
                team=pl.col(side),
                opener=pl.lit(True),
                as_of_utc=as_of(pl.col("game_date"), pl.col("start_utc")),
            )
            for side in ("home", "away")
        ]
    )
    team_games = pl.concat(
        [
            with_candidates,
            scheduled.join(with_candidates, on=["game_id", "team"], how="anti").select(
                with_candidates.columns
            ),
        ],
        how="vertical_relaxed",
    )
    sums = rows.group_by("game_id", "team", "role").agg(
        dressing=pl.col("p_available").sum(),
        **{
            f"weighted_{state}": (pl.col("p_available") * pl.col(f"raw_{state}")).sum()
            for state in STATES
        },
    )
    newcomer = _newcomers(constants, seasons)
    replacements = (
        team_games.join(slots, how="cross")
        .join(sums, on=["game_id", "team", "role"], how="left")
        .join(per_role, on=["season", "role"], how="left")
        .join(newcomer, on=["season", "role", "opener"], how="left")
        .with_columns(
            pl.col("dressing").fill_null(0.0),
            *(pl.col(f"weighted_{state}").fill_null(0.0) for state in STATES),
        )
        .with_columns(count=(pl.col("slots") - pl.col("dressing")).clip(lower_bound=0.0))
        .with_columns(
            **{EXPECTED[state]: pl.col("count") * pl.col(f"new_{state}") for state in STATES}
        )
        .with_columns(
            **{
                f"scale_{state}": pl.when(
                    (pl.col(f"weighted_{state}") > 0)
                    & (pl.col(f"total_{state}") > pl.col(EXPECTED[state]))
                )
                .then(
                    (pl.col(f"total_{state}") - pl.col(EXPECTED[state]))
                    / pl.col(f"weighted_{state}")
                )
                .otherwise(0.0)
                for state in STATES
            }
        )
    )
    scales = replacements.select("game_id", "team", "role", *(f"scale_{s}" for s in STATES))
    rows = (
        rows.join(scales, on=["game_id", "team", "role"], how="left")
        .with_columns(
            **{f"min_{s}": pl.col(f"raw_{s}") * pl.col(f"scale_{s}") for s in STATES},
        )
        .with_columns(
            **{EXPECTED[s]: pl.col("p_available") * pl.col(f"min_{s}") for s in STATES},
        )
        .with_columns(
            pp_unit=pl.struct(pl.col(EXPECTED["pp"]).neg(), pl.col("player_id"))
            .rank("ordinal")
            .over("game_id", "team")
            <= PP_UNIT
        )
        .sort("game_id", "team", "player_id")
    )
    replacement_rows = replacements.select(
        "game_id",
        "season",
        "game_date",
        "team",
        "role",
        "count",
        *EXPECTED.values(),
        observed_utc="as_of_utc",
    ).sort("game_id", "team", "role")
    return rows, replacement_rows


def _per_role(constants: Mapping[int, SeasonConstants], seasons: list[int]) -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "season": season,
                "role": role,
                **{f"mean_{s}": constants[season].mean[role, s] for s in STATES},
                **{f"pull_{s}": constants[season].pull[role, s] for s in STATES},
                **{f"total_{s}": constants[season].total[role, s] for s in STATES},
            }
            for season in seasons
            for role in SKATER_ROLES
        ],
        schema_overrides={"season": pl.Int32},
    )


def _newcomers(constants: Mapping[int, SeasonConstants], seasons: list[int]) -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "season": season,
                "role": role,
                "opener": opener,
                **{f"new_{s}": constants[season].newcomer[role, opener, s] for s in STATES},
            }
            for season in seasons
            for role in SKATER_ROLES
            for opener in (False, True)
        ],
        schema_overrides={"season": pl.Int32},
    )


def input_problems(coverage: pl.DataFrame, minutes: pl.DataFrame, last: int) -> list[str]:
    """Why the lake's stints cannot give the minutes up to the season last: a game whose shift
    chart is complete (shift_coverage) with no stints, which would leave its season's averages,
    pulls and totals, and its players' histories, measured on part of the season."""
    complete = coverage.filter(pl.col("complete"), pl.col("season") <= last)
    missing = complete.join(minutes.select("game_id").unique(), on="game_id", how="anti")
    problems = []
    for (season,), frame in missing.sort("game_id").group_by("season", maintain_order=True):
        examples = ", ".join(map(str, frame["game_id"].head(3).to_list()))
        problems.append(
            f"{season}: {frame.height:,} games with a complete chart and no stints, e.g. {examples}"
        )
    return problems


def lake_minutes(lake: "Lake", boxscores: pl.DataFrame, last: int) -> pl.DataFrame:
    """player_minutes of every season up to last, read a season at a time: the stints of every
    season at once would not fit in memory."""
    seasons = sorted(s for s in boxscores["season"].unique().to_list() if s <= last)
    frames = [
        player_minutes(
            lake.read("stints", seasons=[season]),
            boxscores.filter(pl.col("season") == season),
        )
        for season in seasons
    ]
    return pl.concat(frames) if frames else player_minutes(lake.read("stints"), boxscores)


def with_minutes(
    skaters: pl.DataFrame, projected: pl.DataFrame, constants: Mapping[int, SeasonConstants]
) -> pl.DataFrame:
    """The skaters' lineups rows (projection.score) with their expected minutes and power-play
    unit from project. train_cutoff becomes the later of the lineup model's and the season's
    constants', so it covers everything the row was fitted on."""
    cutoffs = pl.DataFrame(
        {
            "season": list(constants),
            "minutes_cutoff": [c.cutoff for c in constants.values()],
        },
        schema={"season": pl.Int32, "minutes_cutoff": pl.Datetime("us", "UTC")},
    )
    keys = ["game_id", "team", "player_id"]
    return (
        skaters.join(projected.select(*keys, *EXPECTED.values(), "pp_unit"), on=keys, how="left")
        .join(cutoffs, on="season", how="left")
        .with_columns(
            train_cutoff=pl.when(pl.col("minutes_cutoff") > pl.col("train_cutoff"))
            .then(pl.col("minutes_cutoff"))
            .otherwise(pl.col("train_cutoff"))
        )
        .select(list(dtypes(Lineups)))
        .cast(dtypes(Lineups))  # type: ignore[arg-type]
    )


def replacement_table(replacements: pl.DataFrame, skaters: pl.DataFrame) -> pl.DataFrame:
    """The LineupReplacements rows: project's replacements with the train_cutoff and
    artifact_version of their season's skaters (with_minutes), the same for every team-game of a
    season, so a team-game without candidates gets them too."""
    stamps = skaters.group_by("season").agg(
        pl.col("train_cutoff").max(), pl.col("artifact_version").first()
    )
    table = replacements.join(stamps, on="season", how="inner").select(
        list(dtypes(LineupReplacements))
    )
    return LineupReplacements.validate(
        table.cast(dtypes(LineupReplacements)).sort("game_id", "team", "role")  # type: ignore[arg-type]
    )
