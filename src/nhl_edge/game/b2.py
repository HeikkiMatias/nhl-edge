"""B2, the team and goalie baseline (#78, ADR 0013, docs/plan.md §5):

    P(home win) = sigmoid(β0 + h_s + β1·ΔS + β2·ΔG + β3·ΔR)

for the full game, overtime and shootout included (hard rule 2).

**Inputs per game,** from the feature tables, each row usable once its `observed_utc` has passed:
- ΔS from `team_strength`;
- ΔG, the home goalie's `goals_saved` minus the away goalie's (`goalie_effects`);
- h_s from `schedule_terms`, a fixed term (an offset), 0 at a neutral site;
- ΔR from `schedule_terms`: each team's back-to-back flag, the rest difference, the travel
  difference per 1,000 km and each team's time-zone change in absolute hours;
- the empty-seat share, 1 minus `capacity_share`, at a non-neutral game.

**The fit:** a logistic regression with an L2 penalty on the inputs' weights, not on β0, over the
inputs standardized on the training games. It trains on earlier games from 2011-12 whose results
were public before the fold starts, with the goalies who started them (ADR 0013): a starter with
no `goalie_effects` row counts as average.

**A prediction** averages the probability over every pair of candidate starters, weighted by the
goalie-start probabilities (`goalie_starts`). A game's own starters are never read (hard rule 9).
A team with no candidates counts its goalie as average.

The L2 strength is tuned on the training seasons (ADR 0011).
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import cast

import numpy as np
import polars as pl
from numpy.typing import NDArray
from scipy.optimize import minimize
from scipy.special import expit

from nhl_edge.features import team_strength as ts
from nhl_edge.lineup.goalie_start import team_goalie_games

COMPONENT = "b2"
# The first season with every input.
FIRST_SEASON = 20112012
TUNING_SEASONS = ts.TUNING_SEASONS
INPUTS = (
    "delta_s",
    "delta_g",
    "home_back_to_back",
    "away_back_to_back",
    "rest_diff",
    "travel_diff",
    "home_tz",
    "away_tz",
    "empty_seats",
)
GOALIE = INPUTS.index("delta_g")
KM = 1000.0


@dataclass(frozen=True)
class Settings:
    """B2's one tuned setting (ADR 0011): the L2 penalty on the standardized inputs' weights."""

    l2: float

    @property
    def label(self) -> str:
        return f"L2 {self.l2:g}"


@dataclass(frozen=True)
class Tables:
    """The lake tables B2 reads."""

    games: pl.DataFrame
    team_strength: pl.DataFrame
    schedule_terms: pl.DataFrame
    goalie_starts: pl.DataFrame
    goalie_effects: pl.DataFrame
    actual_lineups: pl.DataFrame


def game_inputs(tables: Tables) -> pl.DataFrame:
    """One row per game with ΔS, the schedule inputs and the h_s offset, its as-of time and when
    its rows became known (observed_utc, the later of the two tables')."""
    strength = tables.team_strength.select(
        "game_id", "delta_s", s_as_of="as_of_utc", s_observed="observed_utc"
    )
    terms = tables.schedule_terms
    neutral = pl.col("neutral_site")
    return terms.join(strength, on="game_id").select(
        "game_id",
        "season",
        "game_date",
        "home",
        "away",
        "delta_s",
        home_back_to_back=pl.col("home_back_to_back").cast(pl.Float64),
        away_back_to_back=pl.col("away_back_to_back").cast(pl.Float64),
        rest_diff=(pl.col("home_rest_days") - pl.col("away_rest_days")).cast(pl.Float64),
        travel_diff=(pl.col("home_travel_km") - pl.col("away_travel_km")) / KM,
        home_tz=pl.col("home_tz_shift").abs(),
        away_tz=pl.col("away_tz_shift").abs(),
        empty_seats=pl.when(neutral).then(0.0).otherwise(1 - pl.col("capacity_share")),
        offset=pl.when(neutral).then(0.0).otherwise(pl.col("h_s")),
        as_of_utc=pl.max_horizontal("as_of_utc", "s_as_of"),
        observed_utc=pl.max_horizontal("observed_utc", "s_observed"),
    )


def candidates(tables: Tables) -> pl.DataFrame:
    """Each team-game's candidate starters with their start probability and expected goals
    saved, and when both became known."""
    effects = tables.goalie_effects.select(
        "game_id",
        "team",
        "goalie_id",
        "goals_saved",
        e_as_of="as_of_utc",
        e_observed="observed_utc",
    )
    return tables.goalie_starts.join(effects, on=["game_id", "team", "goalie_id"]).select(
        "game_id",
        "team",
        "goalie_id",
        "p_start",
        "goals_saved",
        as_of_utc=pl.max_horizontal("observed_utc", "e_as_of"),
        observed_utc=pl.max_horizontal("observed_utc", "e_observed"),
    )


def starters_delta(tables: Tables) -> pl.DataFrame:
    """Each game's ΔG with the goalies who started it, for training only, and when its
    boxscore became public. A starter without a goalie_effects row counts as average."""
    starters = team_goalie_games(tables.actual_lineups).select(
        "game_id", "team", goalie_id="starter", lineup_utc="observed_utc"
    )
    saved = starters.join(
        tables.goalie_effects.select("game_id", "team", "goalie_id", "goals_saved"),
        on=["game_id", "team", "goalie_id"],
        how="left",
    ).with_columns(pl.col("goals_saved").fill_null(0.0))
    sides = tables.games.select("game_id", "home", "away")
    home = saved.select("game_id", home="team", home_saved="goals_saved", home_utc="lineup_utc")
    away = saved.select("game_id", away="team", away_saved="goals_saved", away_utc="lineup_utc")
    return (
        sides.join(home, on=["game_id", "home"])
        .join(away, on=["game_id", "away"])
        .select(
            "game_id",
            delta_g=pl.col("home_saved") - pl.col("away_saved"),
            lineup_utc=pl.max_horizontal("home_utc", "away_utc"),
        )
    )


def scenarios(games: pl.DataFrame, candidates: pl.DataFrame) -> pl.DataFrame:
    """Every pair of candidate starters for each game (game_id, home, away): its weight, the
    product of their start probabilities, and its ΔG. A team without candidates gets one average
    goalie."""
    sides = []
    for side in ("home", "away"):
        teams = games.select("game_id", team=pl.col(side))
        rows = teams.join(
            candidates.select("game_id", "team", "p_start", "goals_saved"),
            on=["game_id", "team"],
            how="left",
        )
        sides.append(
            rows.with_columns(
                pl.col("p_start").fill_null(1.0), pl.col("goals_saved").fill_null(0.0)
            ).select("game_id", **{f"{side}_p": "p_start", f"{side}_saved": "goals_saved"})
        )
    return (
        sides[0]
        .join(sides[1], on="game_id")
        .select(
            "game_id",
            weight=pl.col("home_p") * pl.col("away_p"),
            delta_g=pl.col("home_saved") - pl.col("away_saved"),
        )
        .sort("game_id")
    )


def objective(
    x: NDArray[np.float64], y: NDArray[np.float64], offset: NDArray[np.float64], l2: float
) -> Callable[[NDArray[np.float64]], tuple[float, NDArray[np.float64]]]:
    """The penalized log loss summed over games, and its gradient, in (β0, weights)."""

    def loss(beta: NDArray[np.float64]) -> tuple[float, NDArray[np.float64]]:
        eta = beta[0] + offset + x @ beta[1:]
        p = expit(eta)
        nll = float(np.sum(np.logaddexp(0, eta) - y * eta))
        residual = p - y
        gradient = np.concatenate([[residual.sum()], x.T @ residual + l2 * beta[1:]])
        return nll + 0.5 * l2 * float(beta[1:] @ beta[1:]), gradient

    return loss


@dataclass(frozen=True)
class B2Model:
    """A fitted B2: its intercept and weights on the standardized inputs, the standardization,
    and the games it read."""

    season: int
    settings: Settings
    intercept: float
    weights: tuple[float, ...]
    means: tuple[float, ...]
    scales: tuple[float, ...]
    games: int
    train_cutoff: datetime

    def predict(self, inputs: pl.DataFrame, pairs: pl.DataFrame) -> pl.DataFrame:
        """Each game in inputs (game_id, INPUTS but delta_g, offset) with p_home, averaged over
        its goalie pairs (scenarios())."""
        means, scales, weights = (np.asarray(v) for v in (self.means, self.scales, self.weights))
        others = [name for name in INPUTS if name != "delta_g"]
        index = [INPUTS.index(name) for name in others]
        z = (inputs.select(others).to_numpy() - means[index]) / scales[index]
        base = self.intercept + inputs["offset"].to_numpy() + z @ weights[index]
        frame = inputs.select("game_id").with_columns(base=pl.Series(base))
        joined = pairs.join(frame, on="game_id")
        goalie = (joined["delta_g"].to_numpy() - means[GOALIE]) / scales[GOALIE]
        p = expit(joined["base"].to_numpy() + weights[GOALIE] * goalie)
        mixed = (
            joined.with_columns(p=pl.Series(p) * pl.col("weight"))
            .group_by("game_id")
            .agg(p_home=pl.col("p").sum() / pl.col("weight").sum())
        )
        return inputs.select("game_id").join(mixed, on="game_id")


def fit(train: pl.DataFrame, settings: Settings, season: int) -> B2Model:
    """B2 fitted on train: one row per game with INPUTS (delta_g from its starters), offset,
    home_win and result_utc."""
    if train.height == 0 or train["home_win"].n_unique() < 2:
        raise ValueError(f"no earlier games to fit B2 on for {season}")
    raw = train.select(INPUTS).to_numpy().astype(float)
    means = raw.mean(axis=0)
    scales = raw.std(axis=0)
    scales[scales == 0] = 1.0
    x = (raw - means) / scales
    y = train["home_win"].cast(pl.Float64).to_numpy()
    loss = objective(x, y, train["offset"].to_numpy().astype(float), settings.l2)
    result = minimize(loss, np.zeros(len(INPUTS) + 1), jac=True, method="L-BFGS-B")
    if not result.success:
        raise ValueError(f"B2's fit for {season} did not converge: {result.message}")
    beta = cast(NDArray[np.float64], result.x)
    cutoff = train["result_utc"].max()
    assert isinstance(cutoff, datetime)
    return B2Model(
        season=season,
        settings=settings,
        intercept=float(beta[0]),
        weights=tuple(float(b) for b in beta[1:]),
        means=tuple(float(m) for m in means),
        scales=tuple(float(s) for s in scales),
        games=train.height,
        train_cutoff=cutoff,
    )


def training_games(
    tables: Tables, inputs: pl.DataFrame, season: int, start: datetime, known: str
) -> pl.DataFrame:
    """The games B2 for season may train on: from FIRST_SEASON, of earlier seasons, with results
    and boxscores public before start, and inputs known before it by the column known
    (observed_utc in the backtest; as_of_utc while tuning, where the tuned seasons are in-sample
    by design, ADR 0011)."""
    results = tables.games.select(
        "game_id",
        home_win=(pl.col("home_score") > pl.col("away_score")).cast(pl.Int8),
        result_utc="observed_utc",
    )
    return (
        inputs.drop("delta_g", strict=False)
        .filter(pl.col("season").is_between(FIRST_SEASON, season - 1), pl.col(known) < start)
        .join(results, on="game_id")
        .join(starters_delta(tables), on="game_id")
        .filter(pl.col("result_utc") < start, pl.col("lineup_utc") < start)
        .sort("game_id")
    )


def known_before(
    inputs: pl.DataFrame, pool: pl.DataFrame, moments: pl.DataFrame, known: str
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """The games of moments (game_id, prediction_utc) whose inputs were known before their
    prediction time by the column known, and their candidates known by then."""
    usable = inputs.join(moments, on="game_id").filter(pl.col(known) < pl.col("prediction_utc"))
    teams = pl.concat(
        [usable.select("game_id", "prediction_utc", team=pl.col(side)) for side in ("home", "away")]
    )
    ready = pool.join(teams, on=["game_id", "team"]).filter(
        pl.col(known) < pl.col("prediction_utc")
    )
    return usable, ready


def predictions(
    tables: Tables,
    moments: pl.DataFrame,
    season: int,
    start: datetime,
    settings: Settings,
    known: str = "observed_utc",
) -> tuple[pl.DataFrame, B2Model]:
    """B2's p_home for the season's games in moments (game_id, prediction_utc), from a fit on the
    games before start, with its train_cutoff, and the fit."""
    inputs = game_inputs(tables)
    model = fit(training_games(tables, inputs, season, start, known), settings, season)
    usable, ready = known_before(
        inputs.filter(pl.col("season") == season), candidates(tables), moments, known
    )
    pairs = scenarios(usable.select("game_id", "home", "away"), ready)
    predicted = model.predict(usable, pairs).with_columns(
        train_cutoff=pl.lit(model.train_cutoff, dtype=pl.Datetime("us", "UTC"))
    )
    return predicted, model


# The tuning grid for the L2 penalty and the order that breaks ties toward the steadier, stronger
# penalty (ADR 0011).
GRID = tuple(Settings(l2) for l2 in (0.1, 1, 10, 100, 1000))


def steadiness(settings: Settings) -> float:
    """A stronger penalty first."""
    return settings.l2


def tuning_scores(tables: Tables, settings: Settings, seasons: Sequence[int]) -> pl.DataFrame:
    """Each game of the seasons with B2's log loss, fitted on the earlier seasons' games whose
    results were public before the season's first game and predicted at its as-of time, with the
    tuned seasons' features in-sample by design (ADR 0011)."""
    from nhl_edge.backtest.metrics import log_loss
    from nhl_edge.backtest.walk_forward import fold_start, outcomes

    calendar = tables.games.select("season", "start_utc")
    inputs = game_inputs(tables)
    out = []
    for season in sorted(seasons):
        start = fold_start(calendar, season)
        moments = inputs.filter(pl.col("season") == season).select(
            "game_id", prediction_utc=pl.col("as_of_utc") + pl.duration(microseconds=1)
        )
        predicted, _ = predictions(tables, moments, season, start, settings, known="as_of_utc")
        out.append(
            predicted.join(outcomes(tables.games), on="game_id")
            .join(inputs.select("game_id", "season", "game_date"), on="game_id")
            .select(
                "game_id",
                "season",
                "game_date",
                log_loss=log_loss(pl.col("p_home"), pl.col("home_win")),
            )
        )
    return pl.concat(out).sort("game_id")


# Frozen by run b2-20261001-fe11def on #78 (ADR 0011): the leader, and the steadiest of its
# ties. An L2 of 1,000 did not tie.
TUNED = Settings(l2=100)
# The last result that run reads: 2017-18's final night, as for the features.
TUNED_CUTOFF = ts.TUNED_CUTOFF


TABLES = ("team_strength", "schedule_terms", "goalie_starts", "goalie_effects")


def input_problems(tables: Tables, last: int, expected: Mapping[int, int]) -> list[str]:
    """Why the lake cannot run B2 up to the season last: a season short of its games, or a game
    of FIRST_SEASON on missing from a feature table, would drop out of the fits unnoticed."""
    needed = tables.games.filter(pl.col("season").is_between(FIRST_SEASON, last))
    problems = []
    for season in sorted(s for s in expected if FIRST_SEASON <= s <= last):
        count = needed.filter(pl.col("season") == season).height
        if count != expected[season]:
            problems.append(f"{season}: {count:,} of {expected[season]:,} games")
    for name in TABLES:
        table = getattr(tables, name)
        missing = needed.join(table.select("game_id").unique(), on="game_id", how="anti")
        for (season,), frame in missing.sort("game_id").group_by("season", maintain_order=True):
            examples = ", ".join(str(g) for g in frame["game_id"].head(3).to_list())
            problems.append(f"{season}: {frame.height:,} games without {name}, e.g. {examples}")
    return sorted(problems)
