"""Goalie effect ΔG (#75, docs/plan.md §5, ADR 0011): the goals a team's goalie is expected to
save above an average one, from earlier games only.

**A goalie's effect per shot** is his goals saved above expected per unblocked shot (xG against
minus goals against, over the shots `shot_xg` scores: no empty nets, no penalty shots), decayed by
his own games and shrunk toward zero:

    effect = sum of weighted (xG - goals) / (sum of weighted shots + prior_shots)

where a game k of his games back weighs 0.5 ** (k / half_life). His history follows him from team
to team. A goalie with no earlier game has an effect of 0.

**Shots he is expected to face** in a game: the average of the opponent's unblocked shots per
game and his own team's unblocked shots allowed per game, each decayed and shrunk toward the
league like team strength, with its frozen settings (ADR 0011), so this part has nothing of its
own to tune.

**ΔG** for a pair of goalies is the home goalie's effect times the shots he is expected to face,
minus the away goalie's. B2 mixes over the goalie-start model's pairs (#76); the tuning scores the
expected ΔG under those probabilities.

A game is rated as of the time team strength uses: 10:00 US Eastern on its date, or an hour before
its start if that is earlier, from goalie-games and team-games public before then (hard rule 1).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

import numpy as np
import polars as pl

from nhl_edge.features import team_strength as ts
from nhl_edge.features import xg
from nhl_edge.lake.schemas import GoalieEffects, dtypes

COMPONENT = "goalie-effect"
# The first season with xG, and so with goalie effects.
FIRST_SEASON = xg.FIRST_SEASON
# Tuned on the same seasons as team strength (ADR 0011).
TUNING_SEASONS = ts.TUNING_SEASONS


@dataclass(frozen=True)
class Settings:
    """The goalie effect's two tuned settings (ADR 0011)."""

    half_life: float  # in the goalie's own games
    prior_shots: float

    @property
    def label(self) -> str:
        return f"half-life {self.half_life:g} games, prior {self.prior_shots:g} shots"


def goalie_games(shots: pl.DataFrame, shot_xg: pl.DataFrame) -> pl.DataFrame:
    """One row per goalie and game: the unblocked shots with xG he faced, their xG minus the goals
    he allowed (gsax), and when the game's feeds became public."""
    faced = shot_xg.join(
        shots.select("game_id", "event_id", "goalie_id", "is_goal"), on=["game_id", "event_id"]
    ).filter(pl.col("goalie_id").is_not_null())
    return (
        faced.group_by("goalie_id", "game_id", "season", "game_date")
        .agg(
            shots=pl.len().cast(pl.Float64),
            gsax=pl.col("xg").sum() - pl.col("is_goal").cast(pl.Float64).sum(),
            observed_utc=pl.col("observed_utc").max(),
        )
        .sort("goalie_id", "observed_utc", "game_date", "game_id")
    )


def team_shot_games(
    games: pl.DataFrame, shots: pl.DataFrame, shot_xg: pl.DataFrame
) -> pl.DataFrame:
    """One row per team and game with xG: the unblocked shots with xG it took and allowed, and
    when the game's feeds became public. Games without xG (2010-11) are left out."""
    by_team = shot_xg.join(shots.select("game_id", "event_id", "team"), on=["game_id", "event_id"])
    counts = by_team.group_by("game_id", "team").agg(n=pl.len().cast(pl.Float64))
    public = shot_xg.group_by("game_id").agg(observed_utc=pl.col("observed_utc").max())
    sides = pl.concat(
        [
            games.select(
                "game_id", "season", "game_date", team=pl.col(side), opponent=pl.col(other)
            )
            for side, other in (("home", "away"), ("away", "home"))
        ]
    ).join(public, on="game_id")
    return (
        sides.join(counts.rename({"n": "shots_for"}), on=["game_id", "team"], how="left")
        .join(
            counts.rename({"team": "opponent", "n": "shots_against"}),
            on=["game_id", "opponent"],
            how="left",
        )
        .with_columns(pl.col("shots_for", "shots_against").fill_null(0.0))
        .select(
            "game_id", "season", "game_date", "team", "shots_for", "shots_against", "observed_utc"
        )
        .sort("team", "observed_utc", "game_date", "game_id")
    )


def _states(
    history: pl.DataFrame,
    targets: pl.DataFrame,
    key: str,
    columns: tuple[str, ...],
    half_life: float,
) -> pl.DataFrame:
    """Each target row (key, as_of_utc) with the decayed sums of columns over the history rows of
    its key public before as_of_utc, and how many rows that was (history_rows)."""
    decay = 0.5 ** (1 / half_life)
    frames = []
    for (value,), rows in targets.group_by(key):
        # A full sort key, so the same rows are always summed in the same order (CI on #86).
        past = history.filter(pl.col(key) == value).sort("observed_utc", "game_date", "game_id")
        sums = (
            ts._decayed(past.select(columns).to_numpy().astype(float), decay)
            if past.height
            else np.zeros((0, len(columns)))
        )
        seen = np.searchsorted(
            past["observed_utc"].to_numpy(), rows["as_of_utc"].to_numpy(), side="left"
        )
        state = np.zeros((rows.height, len(columns)))
        has = seen > 0
        state[has] = sums[seen[has] - 1]
        frames.append(
            rows.with_columns(
                pl.Series(name, state[:, i]) for i, name in enumerate(columns)
            ).with_columns(history_rows=pl.Series(seen, dtype=pl.Int32))
        )
    if not frames:
        return targets.with_columns(
            *(pl.lit(0.0).alias(name) for name in columns),
            history_rows=pl.lit(0, dtype=pl.Int32),
        )
    return pl.concat(frames)


def expected_shots(
    targets: pl.DataFrame, history: pl.DataFrame, lines: Mapping[str, str] | None = None
) -> pl.DataFrame:
    """Each target (game_id, season, team, opponent, as_of_utc) with the unblocked shots the team's
    goalie is expected to face: the average of the opponent's shots per game and the team's shots
    allowed per game, each shrunk toward the league's shots per team-game in the season and the
    one before, with team strength's frozen settings."""
    lines = ts.team_lines() if lines is None else lines
    settings = ts.TUNED
    columns = ("shots_for", "shots_against", "games")
    history = history.with_columns(line=pl.col("team").replace(dict(lines)), games=pl.lit(1.0))
    with_lines = targets.with_columns(
        line=pl.col("team").replace(dict(lines)),
        opponent_line=pl.col("opponent").replace(dict(lines)),
    )
    own = _states(history, with_lines, "line", columns, settings.half_life)
    opp = _states(
        history,
        with_lines.select("game_id", "team", "as_of_utc", line="opponent_line"),
        "line",
        columns,
        settings.half_life,
    ).select("game_id", "team", **{f"opp_{name}": name for name in columns})
    league = _league_shots(history, targets)
    m = settings.prior_games
    allowed = (pl.col("shots_against") + m * pl.col("league")) / (pl.col("games") + m)
    taken = (pl.col("opp_shots_for") + m * pl.col("league")) / (pl.col("opp_games") + m)
    return (
        own.join(opp, on=["game_id", "team"])
        .join(league, on=["season", "as_of_utc"])
        .with_columns(expected_shots=((allowed + taken) / 2).fill_nan(0.0).fill_null(0.0))
        .select(*targets.columns, "expected_shots")
    )


def _league_shots(history: pl.DataFrame, targets: pl.DataFrame) -> pl.DataFrame:
    """For each distinct (season, as_of_utc), the league's unblocked shots per team-game over the
    team-games public before then in that season and the one before (null before any)."""
    out = []
    for (season,), rows in targets.select("season", "as_of_utc").unique().group_by("season"):
        past = history.filter(pl.col("season").is_in([season, season - 10001])).sort(
            "observed_utc", "game_id", "team"
        )
        shots = past["shots_for"].to_numpy().astype(float).cumsum()
        seen = np.searchsorted(
            past["observed_utc"].to_numpy(), rows["as_of_utc"].to_numpy(), side="left"
        )
        per_game = np.full(rows.height, np.nan)
        has = seen > 0
        per_game[has] = shots[seen[has] - 1] / seen[has]
        out.append(rows.with_columns(league=pl.Series(per_game)))
    return pl.concat(out)


def effects(
    candidates: pl.DataFrame,
    games: pl.DataFrame,
    goalies: pl.DataFrame,
    team_shots: pl.DataFrame,
    settings: Settings,
    lines: Mapping[str, str] | None = None,
) -> pl.DataFrame:
    """Each candidate (game_id, team, goalie_id) with his effect per shot, his goalie-games read,
    the shots his team is expected to allow, and the goals he is expected to save over the game
    (goals_saved). goalies is goalie_games(), team_shots team_shot_games()."""
    targets = candidates.select("game_id", "team", "goalie_id").join(
        games.select(
            "game_id",
            "season",
            "game_date",
            "home",
            "away",
            as_of_utc=ts.as_of(pl.col("game_date"), pl.col("start_utc")),
        ),
        on="game_id",
    )
    targets = targets.with_columns(
        opponent=pl.when(pl.col("team") == pl.col("home"))
        .then(pl.col("away"))
        .otherwise(pl.col("home"))
    ).drop("home", "away")
    own = _states(goalies, targets, "goalie_id", ("shots", "gsax"), settings.half_life)
    denominator = pl.col("shots") + settings.prior_shots
    rated = own.with_columns(
        effect=pl.when(denominator > 0).then(pl.col("gsax") / denominator).otherwise(0.0),
        goalie_games=pl.col("history_rows"),
    )
    teams = targets.select("game_id", "season", "team", "opponent", "as_of_utc").unique()
    shots = expected_shots(teams, team_shots, lines).select("game_id", "team", "expected_shots")
    return (
        rated.join(shots, on=["game_id", "team"])
        .with_columns(goals_saved=pl.col("effect") * pl.col("expected_shots"))
        .select(
            "game_id",
            "season",
            "game_date",
            "team",
            "goalie_id",
            "effect",
            "goalie_games",
            "expected_shots",
            "goals_saved",
            "as_of_utc",
        )
        .sort("game_id", "team", "goalie_id")
    )


def expected_delta(rated: pl.DataFrame, starts: pl.DataFrame, games: pl.DataFrame) -> pl.DataFrame:
    """Each game's ΔG expected under the goalie-start probabilities (game_id, x): the home team's
    probability-weighted goals saved minus the away team's. A team with no candidates adds 0."""
    weighted = (
        rated.join(
            starts.select("game_id", "team", "goalie_id", "p_start"),
            on=["game_id", "team", "goalie_id"],
        )
        .group_by("game_id", "team")
        .agg(saved=(pl.col("p_start") * pl.col("goals_saved")).sum())
    )
    home = weighted.rename({"team": "home", "saved": "home_saved"})
    away = weighted.rename({"team": "away", "saved": "away_saved"})
    return (
        games.select("game_id", "home", "away")
        .join(home, on=["game_id", "home"], how="left")
        .join(away, on=["game_id", "away"], how="left")
        .select(
            "game_id", x=pl.col("home_saved").fill_null(0.0) - pl.col("away_saved").fill_null(0.0)
        )
        .sort("game_id")
    )


# The tuning grid and the order that breaks ties toward the steadier setting (ADR 0011): memory in
# the goalie's own games, and a pull toward zero worth so many unblocked shots. A starter faces
# about 2,500 a season.
GRID = tuple(Settings(h, m) for h in (20, 40, 80, 160) for m in (500, 1000, 2000, 4000))


def steadiness(settings: Settings) -> tuple[float, float]:
    """Longer memory first, then more pull toward zero."""
    return settings.half_life, settings.prior_shots


# Not tuned yet: set from the tuning run on #75 (ADR 0011).
TUNED = Settings(half_life=80, prior_shots=2000)
# The last result that run read: 2017-18's final night, as for team strength. An effect observed
# before it used settings chosen with its own season's results (in-sample, ADR 0011).
TUNED_CUTOFF = ts.TUNED_CUTOFF


def rows(
    rated: pl.DataFrame,
    settings: Settings,
    artifact_version: str,
    train_cutoff: datetime = TUNED_CUTOFF,
) -> pl.DataFrame:
    """The candidates' GoalieEffects rows, each with the cutoff of the tuning run that chose the
    settings: an effect is observed at its as-of time, or at the cutoff if that is later."""
    frame = rated.with_columns(
        half_life=pl.lit(float(settings.half_life)),
        prior_shots=pl.lit(float(settings.prior_shots)),
        train_cutoff=pl.lit(train_cutoff),
        artifact_version=pl.lit(artifact_version),
        observed_utc=pl.max_horizontal("as_of_utc", pl.lit(train_cutoff)),
    )
    columns = dtypes(GoalieEffects)
    return GoalieEffects.validate(frame.select(list(columns)).cast(columns))  # type: ignore[arg-type]


def input_problems(
    games: pl.DataFrame,
    shot_xg: pl.DataFrame,
    starts: pl.DataFrame,
    last: int,
    expected: Mapping[int, int],
) -> list[str]:
    """Why the lake cannot rate goalies up to the season last: a season short of its games
    (expected), or a game of 2011-12 on without xG or without goalie-start probabilities, would
    drop out of the histories or the tuning unnoticed."""
    needed = games.filter(pl.col("season").is_between(FIRST_SEASON, last))
    problems = []
    for season in sorted(s for s in expected if FIRST_SEASON <= s <= last):
        count = needed.filter(pl.col("season") == season).height
        if count != expected[season]:
            problems.append(f"{season}: {count:,} of {expected[season]:,} games")
    for label, table in (("xG", shot_xg), ("goalie starts", starts)):
        missing = needed.join(table.select("game_id").unique(), on="game_id", how="anti")
        for (season,), frame in missing.sort("game_id").group_by("season", maintain_order=True):
            examples = ", ".join(str(g) for g in frame["game_id"].head(3).to_list())
            problems.append(f"{season}: {frame.height:,} games without {label}, e.g. {examples}")
    return sorted(problems)
