"""Goalie-start model (#76, ADR 0012): before a game, the probability that each of a team's
goalies starts it, from earlier boxscores only (hard rule 9). B2 mixes over these probabilities
when the starter is unconfirmed (docs/plan.md §5).

**Candidates:** the goalies who dressed for the team in its last WINDOW games public before the
prediction time, through its line of team codes (PHX, ARI, UTA).

**Inputs per candidate:**
- his share of the team's starts in those games, and in its games this season;
- whether he started the team's last game, and his run of straight starts (capped);
- whether the team played yesterday with him its starter then (the team playing yesterday alone
  is the same for every candidate, so it cannot move the choice among them);
- days since his last start for the team (capped);
- whether he dressed for the team's last game.

**The model:** a conditional logit, one choice among a team's candidates per game, so each game's
probabilities add up to 1. It is fitted per season on earlier seasons' team-games whose starters
were public before the season's first as-of time. The windows and caps are fixed by ADR 0012, not
tuned.

Each game is rated as of the time team strength uses: 10:00 US Eastern on its date, or an hour
before its start if earlier. A game's own boxscore is read only to score the model.
"""

from collections import Counter, deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, cast

import numpy as np
import polars as pl
from numpy.typing import NDArray
from scipy.optimize import minimize

from nhl_edge.features.team_strength import as_of, team_lines
from nhl_edge.lake.schemas import GoalieStarts, dtypes

COMPONENT = "goalie-start"
# The first season predicted: 2010-11, the first in the lake, has no earlier season to fit on.
FIRST_SEASON = 20112012
WINDOW = 10
STREAK_CAP = 10
REST_CAP_DAYS = 14
# A light ridge that keeps the fit finite where an input never varies; it is not tuned.
L2 = 1e-3
FEATURES = (
    "recent_share",
    "season_share",
    "started_last",
    "streak",
    "back_to_back_started",
    "rest",
    "dressed_last",
)


@dataclass
class _TeamState:
    """A team line's history up to some point: its last WINDOW games and running counts."""

    window: deque[tuple[date, int, frozenset[int]]]
    season: int | None = None
    season_games: int = 0
    season_starts: Counter[int] | None = None
    streak_goalie: int | None = None
    streak: int = 0
    last_start: dict[int, date] | None = None

    def add(self, season: int, game_date: date, starter: int, dressed: frozenset[int]) -> None:
        if season != self.season:
            self.season, self.season_games, self.season_starts = season, 0, Counter()
        assert self.season_starts is not None and self.last_start is not None
        self.season_games += 1
        self.season_starts[starter] += 1
        if starter == self.streak_goalie:
            self.streak += 1
        else:
            self.streak_goalie, self.streak = starter, 1
        self.last_start[starter] = game_date
        self.window.append((game_date, starter, dressed))


def team_goalie_games(lineups: pl.DataFrame) -> pl.DataFrame:
    """One row per team-game: the goalies who dressed, the starter (null if none was flagged),
    and when the boxscore became public."""
    goalies = lineups.filter(pl.col("role") == "G")
    return goalies.group_by("game_id", "season", "game_date", "team").agg(
        dressed=pl.col("player_id").sort(),
        starter=pl.col("player_id").filter(pl.col("starting_goalie")).first(),
        observed_utc=pl.col("observed_utc").max(),
    )


def candidates(
    games: pl.DataFrame, lineups: pl.DataFrame, lines: Mapping[str, str] | None = None
) -> pl.DataFrame:
    """One row per game, team and candidate goalie, with his inputs as of the game's prediction
    time (as_of_utc). started says whether he started, from the game's own boxscore, and
    starter_utc when that became public; both are null without a boxscore. They are labels for
    fitting and scoring, never inputs."""
    lines = team_lines() if lines is None else lines
    history = team_goalie_games(lineups).with_columns(line=pl.col("team").replace(dict(lines)))
    labels = history.select("game_id", "team", "starter", starter_utc="observed_utc")
    targets = pl.concat(
        [
            games.select(
                "game_id",
                "season",
                "game_date",
                team=pl.col(side),
                as_of_utc=as_of(pl.col("game_date"), pl.col("start_utc")),
            )
            for side in ("home", "away")
        ]
    ).with_columns(line=pl.col("team").replace(dict(lines)))
    rows: list[dict[str, Any]] = []
    for (line,), wanted in targets.group_by("line"):
        past = (
            history.filter(pl.col("line") == line, pl.col("starter").is_not_null())
            .sort("observed_utc", "game_date", "game_id", "team")
            .select("season", "game_date", "starter", "dressed", "observed_utc")
            .rows()
        )
        state = _TeamState(window=deque(maxlen=WINDOW), last_start={})
        seen = 0
        for target in wanted.sort("as_of_utc", "game_id").iter_rows(named=True):
            # Only team-games public strictly before the as-of time (hard rule 9).
            while seen < len(past) and past[seen][4] < target["as_of_utc"]:
                season, game_date, starter, dressed, _ = past[seen]
                state.add(season, game_date, starter, frozenset(dressed))
                seen += 1
            rows.extend(_candidate_rows(target, state))
    frame = pl.DataFrame(rows, schema=_CANDIDATE_SCHEMA)
    return (
        frame.join(labels, on=["game_id", "team"], how="left")
        .with_columns(
            started=pl.when(pl.col("starter").is_null())
            .then(None)
            .otherwise(pl.col("goalie_id") == pl.col("starter"))
        )
        .drop("starter")
        .sort("game_id", "team", "goalie_id")
    )


_CANDIDATE_SCHEMA = {
    "game_id": pl.Int64,
    "season": pl.Int32,
    "game_date": pl.Date,
    "team": pl.String,
    "goalie_id": pl.Int64,
    **{name: pl.Float64 for name in FEATURES},
    "as_of_utc": pl.Datetime("us", "UTC"),
}


def _candidate_rows(target: dict[str, Any], state: _TeamState) -> list[dict[str, Any]]:
    if not state.window:
        return []
    assert state.last_start is not None and state.season_starts is not None
    last_date, last_starter, last_dressed = state.window[-1]
    goalies = sorted(set().union(*(dressed for _, _, dressed in state.window)))
    window_starts = Counter(starter for _, starter, _ in state.window)
    same_season = state.season == target["season"]
    back_to_back = (target["game_date"] - last_date).days == 1
    out = []
    for goalie in goalies:
        last = state.last_start.get(goalie)
        rest_days = REST_CAP_DAYS if last is None else (target["game_date"] - last).days
        started_last = goalie == last_starter
        season_share = state.season_starts[goalie] / state.season_games if same_season else 0.0
        out.append(
            {
                "game_id": target["game_id"],
                "season": target["season"],
                "game_date": target["game_date"],
                "team": target["team"],
                "goalie_id": goalie,
                "recent_share": window_starts[goalie] / len(state.window),
                "season_share": season_share,
                "started_last": float(started_last),
                "streak": min(state.streak, STREAK_CAP) / STREAK_CAP if started_last else 0.0,
                "back_to_back_started": float(back_to_back and started_last),
                "rest": min(rest_days, REST_CAP_DAYS) / REST_CAP_DAYS,
                "dressed_last": float(goalie in last_dressed),
                "as_of_utc": target["as_of_utc"],
            }
        )
    return out


def _softmax_by_group(
    scores: NDArray[np.float64], groups: NDArray[np.int64]
) -> NDArray[np.float64]:
    starts = _starts(groups)
    shifted = scores - np.maximum.reduceat(scores, starts)[groups]
    weights = np.exp(shifted)
    return weights / np.add.reduceat(weights, starts)[groups]


def _starts(groups: NDArray[np.int64]) -> NDArray[np.int64]:
    """Where each group begins; groups are consecutive and numbered from 0."""
    return np.flatnonzero(np.r_[True, groups[1:] != groups[:-1]])


def _group_index(frame: pl.DataFrame) -> NDArray[np.int64]:
    """Each row's team-game, numbered from 0; frame is sorted by game and team."""
    new = pl.col("game_id").ne_missing(pl.col("game_id").shift()) | pl.col("team").ne_missing(
        pl.col("team").shift()
    )
    groups = frame.select(group=new.cast(pl.Int64).cum_sum() - 1)["group"]
    return groups.to_numpy().astype(np.int64)


def objective(
    x: NDArray[np.float64], y: NDArray[np.float64], groups: NDArray[np.int64]
) -> Callable[[NDArray[np.float64]], tuple[float, NDArray[np.float64]]]:
    """The conditional logit's penalized negative log-likelihood and its gradient: rows x with
    one-hot outcomes y, in consecutive groups numbered from 0."""
    starts = _starts(groups)

    def loss(beta: NDArray[np.float64]) -> tuple[float, NDArray[np.float64]]:
        p = _softmax_by_group(x @ beta, groups)
        nll = -float(np.sum(y * np.log(np.clip(p, 1e-300, None))))
        expected = np.add.reduceat(p[:, None] * x, starts)[groups]
        gradient = -(y[:, None] * (x - expected)).sum(axis=0)
        return nll + L2 * float(beta @ beta), gradient + 2 * L2 * beta

    return loss


def season_cutoff(games: pl.DataFrame, season: int) -> datetime:
    """The season's first as-of time: its model reads only boxscores public before it, so it
    predates every rating it gives, and the backtest's fold start. The lineup model (ADR 0017)
    starts its seasons here too."""
    first = games.filter(pl.col("season") == season).select(
        as_of(pl.col("game_date"), pl.col("start_utc")).min()
    )
    moment = first.item() if first.height else None
    if not isinstance(moment, datetime):
        raise ValueError(f"no games of {season} to start its model")
    return moment


@dataclass(frozen=True)
class GoalieStartModel:
    """A fitted conditional logit, the season it predicts and the seasons it was fitted on."""

    season: int
    seasons: tuple[int, ...]
    coefficients: tuple[float, ...]
    team_games: int
    train_cutoff: datetime
    artifact_version: str

    def predict(self, frame: pl.DataFrame) -> NDArray[np.float64]:
        """Each candidate row's start probability; frame is sorted by game and team."""
        if frame.is_empty():
            return np.empty(0)
        x = frame.select(FEATURES).to_numpy()
        return _softmax_by_group(x @ np.asarray(self.coefficients), _group_index(frame))


def fit(
    rows: pl.DataFrame, games: pl.DataFrame, season: int, artifact_version: str
) -> GoalieStartModel:
    """The model for season, fitted on earlier seasons' team-games whose starter was public before
    the season's first as-of time and was among the candidates."""
    cutoff = season_cutoff(games, season)
    train = rows.filter(
        pl.col("season") < season,
        pl.col("started").is_not_null(),
        pl.col("starter_utc") < cutoff,
    )
    # A starter outside the candidates (a call-up, a trade) leaves nothing to choose among.
    hit = train.group_by("game_id", "team").agg(hit=pl.col("started").any())
    train = (
        train.join(hit.filter("hit"), on=["game_id", "team"])
        .drop("hit")
        .sort("game_id", "team", "goalie_id")
    )
    if train.is_empty():
        raise ValueError(f"no earlier team-games to fit the goalie-start model for {season}")
    groups = _group_index(train)
    loss = objective(
        train.select(FEATURES).to_numpy(), train["started"].cast(pl.Float64).to_numpy(), groups
    )
    result = minimize(loss, np.zeros(len(FEATURES)), jac=True, method="L-BFGS-B")
    if not result.success:
        raise ValueError(f"goalie-start fit for {season} did not converge: {result.message}")
    last = train["starter_utc"].max()
    assert isinstance(last, datetime)
    return GoalieStartModel(
        season=season,
        seasons=tuple(sorted(train["season"].unique().to_list())),
        coefficients=tuple(float(b) for b in cast(NDArray[np.float64], result.x)),
        team_games=len(_starts(groups)),
        train_cutoff=last,
        artifact_version=artifact_version,
    )


def score(
    lineups: pl.DataFrame,
    games: pl.DataFrame,
    seasons: Iterable[int],
    artifact_version: str,
    lines: Mapping[str, str] | None = None,
) -> tuple[pl.DataFrame, pl.DataFrame, list[GoalieStartModel]]:
    """Start probabilities for every team-game of the seasons as GoalieStarts rows, the candidate
    rows they came from with p_start (for the report), and the model fitted for each season.
    lineups and games hold the seasons and every earlier one."""
    wanted = sorted(set(seasons))
    early = [season for season in wanted if season < FIRST_SEASON]
    if early:
        raise ValueError(f"{early} have no earlier season to fit the goalie-start model on")
    if not wanted:
        raise ValueError("no seasons to score")
    rows = candidates(games.filter(pl.col("season") <= wanted[-1]), lineups, lines)
    frames, scored, models = [], [], []
    for season in wanted:
        model = fit(rows, games, season, artifact_version)
        test = rows.filter(pl.col("season") == season).sort("game_id", "team", "goalie_id")
        test = test.with_columns(p_start=pl.Series(model.predict(test), dtype=pl.Float64))
        scored.append(test)
        frames.append(
            test.select(
                "game_id",
                "season",
                "game_date",
                "team",
                "goalie_id",
                "p_start",
                train_cutoff=pl.lit(model.train_cutoff, dtype=pl.Datetime("us", "UTC")),
                artifact_version=pl.lit(artifact_version),
                observed_utc="as_of_utc",
            )
        )
        models.append(model)
    table = pl.concat(frames).cast(dtypes(GoalieStarts)).sort("game_id", "team", "goalie_id")  # type: ignore[arg-type]
    return GoalieStarts.validate(table), pl.concat(scored), models


def input_problems(
    games: pl.DataFrame, lineups: pl.DataFrame, last: int, expected: Mapping[int, int]
) -> list[str]:
    """Why the lake cannot score goalie starts up to the season last: a season short of its
    games (expected), or a team-game without a flagged starter, would drop out of the teams'
    histories unnoticed."""
    needed = games.filter(pl.col("season") <= last)
    problems = []
    for season in sorted(s for s in expected if s <= last):
        count = needed.filter(pl.col("season") == season).height
        if count != expected[season]:
            problems.append(f"{season}: {count:,} of {expected[season]:,} games")
    flagged = team_goalie_games(lineups).filter(pl.col("starter").is_not_null())
    team_games = pl.concat(
        [needed.select("game_id", "season", team=pl.col(side)) for side in ("home", "away")]
    )
    missing = team_games.join(flagged, on=["game_id", "team"], how="anti")
    for (season,), frame in missing.sort("game_id").group_by("season", maintain_order=True):
        examples = ", ".join(f"{g} {t}" for g, t in frame.select("game_id", "team").head(3).rows())
        problems.append(f"{season}: {frame.height:,} team-games without a starter, e.g. {examples}")
    return sorted(problems)
