"""Simple expected goals (#73, ADR 0010): the probability that an unblocked shot becomes a goal,
from where and how it was taken. Team strength (#74) and the goalie effect (#75) build on it.

The model is a logistic regression on:
- distance and angle to the net, each as a cubic spline with fixed knots;
- shot type, with the rare ones grouped as "other";
- rebound: the play before the shot is the shooting team's own attempt, 3 seconds or less before,
  with a term per season, since rebounds convert less every season;
- rush: the play before the shot is in the neutral or the shooting team's defensive zone, 4
  seconds or less before;
- the strength state from the shooting team's side;
- the season, so scoring drift between seasons is absorbed.

Penalty shots, shots at an empty net and shots without coordinates are left out, when fitting and
when scoring. Every constant here is fixed by ADR 0010 from common public definitions, not tuned
on results. The rebound term per season was chosen among four variants on the training seasons
only, after the first calibration report (ADR 0010).

One model scores each season. It is fitted on every earlier season's shots public before the
season's first game (hard rule 1), and a season it has not seen takes the latest training season's
level. So every scored shot's xG comes from a model that never saw it.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime

import numpy as np
import polars as pl
from numpy.typing import NDArray
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import SplineTransformer

from nhl_edge.backtest.walk_forward import fold_start
from nhl_edge.lake.schemas import ShotXg, dtypes

COMPONENT = "xg"
# The first season scored: 2010-11, the first in the lake, has no earlier season to train on.
FIRST_SEASON = 20112012
# Shots are turned so the shooting team attacks the net at x = +89 (Shots).
NET_X = 89
REBOUND_S = 3
RUSH_S = 4
ATTEMPTS = ("shot-on-goal", "missed-shot", "blocked-shot")
RUSH_ZONES = ("N", "D")
# Shot types with their own term; the rest and unknown ones are "other". Wrist is the reference.
SHOT_TYPES = ("wrist", "snap", "slap", "backhand", "tip-in", "deflected", "wrap-around")
SHOT_KINDS = (*SHOT_TYPES, "other")
# Strength states from the shooting team's side; even 5v5 is the reference.
STATES = ("5v5", "4v4", "3v3", "advantage", "shorthanded", "unknown")
# Spline knots in feet and degrees. The outer knots bound the data: a shot is at most about 190
# feet from the net, and an angle past 90 degrees is from behind the goal line.
DISTANCE_KNOTS = (0.0, 10.0, 20.0, 30.0, 45.0, 60.0, 100.0, 200.0)
ANGLE_KNOTS = (0.0, 20.0, 40.0, 60.0, 80.0, 100.0, 180.0)
SPLINE_DEGREE = 3
# The L2 penalty's inverse strength. At hundreds of thousands of shots it is negligible; it only
# keeps a rare category's coefficient finite.
C = 1.0
MAX_ITER = 1000

OUTPUT_KEYS = ("game_id", "season", "game_date", "event_id")


def model_frame(shots: pl.DataFrame) -> pl.DataFrame:
    """The shots the model fits and scores, with its inputs: every unblocked shot with
    coordinates, but penalty shots and shots at an empty net."""
    x, y = pl.col("x").cast(pl.Float64), pl.col("y").cast(pl.Float64)
    for_, against = pl.col("skaters_for"), pl.col("skaters_against")
    return shots.filter(
        ~pl.col("is_penalty_shot"),
        ~pl.col("is_empty_net"),
        pl.col("x").is_not_null(),
        pl.col("y").is_not_null(),
    ).with_columns(
        distance=((NET_X - x) ** 2 + y**2).sqrt(),
        angle=pl.arctan2(y.abs(), NET_X - x).degrees(),
        shot_kind=pl.when(pl.col("shot_type").is_in(SHOT_TYPES))
        .then(pl.col("shot_type"))
        .otherwise(pl.lit("other")),
        rebound=(
            pl.col("prev_event_type").is_in(ATTEMPTS)
            & pl.col("prev_by_shooting_team")
            & (pl.col("prev_seconds") <= REBOUND_S)
        ).fill_null(False),
        rush=(pl.col("prev_zone").is_in(RUSH_ZONES) & (pl.col("prev_seconds") <= RUSH_S)).fill_null(
            False
        ),
        state=pl.when(for_.is_null() | against.is_null())
        .then(pl.lit("unknown"))
        .when(for_ > against)
        .then(pl.lit("advantage"))
        .when(for_ < against)
        .then(pl.lit("shorthanded"))
        .when(for_ == 5)
        .then(pl.lit("5v5"))
        .when(for_ == 4)
        .then(pl.lit("4v4"))
        .when(for_ == 3)
        .then(pl.lit("3v3"))
        .otherwise(pl.lit("unknown")),
    )


def _spline(knots: tuple[float, ...]) -> SplineTransformer:
    grid = np.asarray(knots).reshape(-1, 1)
    spline = SplineTransformer(
        knots=grid,  # type: ignore[arg-type]  # the stubs allow only "uniform" or "quantile"
        degree=SPLINE_DEGREE,
        extrapolation="constant",
        include_bias=False,
    )
    return spline.fit(grid)


def _one_hot(values: pl.Series, levels: tuple[object, ...]) -> NDArray[np.float64]:
    """Indicator columns for every level but the first, the reference."""
    unknown = set(values.unique().to_list()) - set(levels)
    if unknown:
        raise ValueError(f"{values.name} has values outside {levels}: {sorted(unknown)}")
    return np.column_stack([(values == level).to_numpy() for level in levels[1:]]).astype(float)


def design(frame: pl.DataFrame, seasons: tuple[int, ...]) -> NDArray[np.float64]:
    """The model's input columns for a model_frame, with seasons the fitted model knows. A later
    season takes the last known season's level, and its rebound effect."""
    season = frame["season"].clip(upper_bound=seasons[-1])
    blocks = [
        _spline(DISTANCE_KNOTS).transform(frame["distance"].to_numpy().reshape(-1, 1)),
        _spline(ANGLE_KNOTS).transform(frame["angle"].to_numpy().reshape(-1, 1)),
        _one_hot(frame["shot_kind"], SHOT_KINDS),
        _one_hot(frame["state"], STATES),
        frame.select("rebound", "rush").to_numpy().astype(float),
    ]
    if len(seasons) > 1:
        by_season = _one_hot(season, seasons)
        # The rebound effect moves by season, as the level does (ADR 0010).
        blocks += [by_season, by_season * frame["rebound"].to_numpy()[:, None]]
    return np.hstack(blocks)


@dataclass(frozen=True)
class XgModel:
    """A fitted xG model and the season it scores."""

    season: int
    seasons: tuple[int, ...]
    model: LogisticRegression
    shots: int
    goals: int
    train_cutoff: datetime
    artifact_version: str

    def predict(self, frame: pl.DataFrame) -> NDArray[np.float64]:
        """Each model_frame row's probability of a goal."""
        if frame.is_empty():
            return np.empty(0)
        return self.model.predict_proba(design(frame, self.seasons))[:, 1]


def fit(shots: pl.DataFrame, season: int, start: datetime, artifact_version: str) -> XgModel:
    """The model that scores season, fitted on the shots of earlier seasons public before start,
    the season's first game."""
    train = model_frame(shots.filter(pl.col("season") < season, pl.col("observed_utc") < start))
    goals = int(train["is_goal"].sum())
    if goals == 0 or goals == train.height:
        raise ValueError(f"the xG model for {season} needs goals and saves before {start}")
    seasons = tuple(sorted(train["season"].unique().to_list()))
    model = LogisticRegression(C=C, max_iter=MAX_ITER)
    model.fit(design(train, seasons), train["is_goal"].to_numpy())
    cutoff = train["observed_utc"].max()
    assert isinstance(cutoff, datetime)
    return XgModel(season, seasons, model, train.height, goals, cutoff, artifact_version)


def input_problems(
    shots: pl.DataFrame,
    games: pl.DataFrame,
    seasons: Iterable[int],
    expected: Mapping[int, int],
) -> list[str]:
    """Why the lake cannot fit and score the seasons, if it cannot. Every season from the lake's
    first to the last one scored is read: a fit on a season short of its games, or with games
    missing their shots, would be biased with nothing to show for it (Codex on #73).
    - A season that expected lists must have all its games.
    - Every game must have shots.
    - Every game's shots must carry the play before them: a game whose shots all lack it was
      written before #73 and needs a replay. A period's first play can have none, so one such shot
      is no problem.
    """
    last = max(seasons)
    needed = games.filter(pl.col("season") <= last)
    problems = []
    for season in sorted(s for s in expected if s <= last):
        count = needed.filter(pl.col("season") == season).height
        if count != expected[season]:
            problems.append(f"{season}: {count:,} of {expected[season]:,} games")
    per_game = shots.group_by("game_id").agg(replayed=pl.col("prev_event_type").is_not_null().any())
    joined = needed.select("game_id", "season").join(per_game, on="game_id", how="left")
    for label, rows in (
        ("games without shots", joined.filter(pl.col("replayed").is_null())),
        ("games whose shots lack the play before them", joined.filter(~pl.col("replayed"))),
    ):
        for (season,), frame in rows.sort("game_id").group_by("season", maintain_order=True):
            examples = ", ".join(str(g) for g in frame["game_id"].head(3).to_list())
            problems.append(f"{season}: {frame.height:,} {label}, e.g. {examples}")
    return sorted(problems)


def score(
    shots: pl.DataFrame, games: pl.DataFrame, seasons: Iterable[int], artifact_version: str
) -> tuple[pl.DataFrame, list[XgModel]]:
    """Every scored shot of the seasons, as ShotXg rows, and the model that scored each season.
    shots holds the seasons and every earlier one; games gives each season's first start."""
    seasons = sorted(set(seasons))
    early = [season for season in seasons if season < FIRST_SEASON]
    if early:
        raise ValueError(f"{early} have no earlier season to fit an xG model on")
    calendar = games.select("season", "start_utc")
    frames = [pl.DataFrame(schema=dtypes(ShotXg))]
    models = []
    for season in seasons:
        model = fit(shots, season, fold_start(calendar, season), artifact_version)
        scored = model_frame(shots.filter(pl.col("season") == season))
        frames.append(
            scored.select(
                *OUTPUT_KEYS,
                xg=pl.Series(model.predict(scored), dtype=pl.Float64),
                train_cutoff=pl.lit(model.train_cutoff),
                artifact_version=pl.lit(artifact_version),
                observed_utc="observed_utc",
            ).cast(dtypes(ShotXg))  # type: ignore[arg-type]
        )
        models.append(model)
    return ShotXg.validate(pl.concat(frames).sort("game_id", "event_id")), models
