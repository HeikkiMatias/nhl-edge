"""Lineup availability (#99, ADR 0017): before a game, the probability that each of a team's
skaters dresses, from earlier boxscores only (hard rule 9). B3 weights each skater's minutes by
it (docs/plan.md §5 and §10).

**Candidates:** the skaters who dressed for the team in its last WINDOW games public before the
prediction time, through its line of team codes (PHX, ARI, UTA). A player whose latest public
game was for another team drops out.

**Inputs per candidate:**
- whether he dressed for the team's last game, and his share of the window's games;
- team games since he last dressed (capped);
- an early exit: he dressed last game and played under half his average ice time in his other
  games of the window;
- his run of straight team games dressed up to the last one (capped);
- whether he was listed among the defense in his latest game for the team;
- the team's first game of a season, times "dressed last game" and times the share. The flag
  alone is the same for every candidate, so the shift below would cancel it.

**The model:** a logistic regression fitted per season on earlier seasons' candidates whose
boxscores were public before the season's first as-of time. Each team-game's probabilities are
then shifted by one common amount on the log-odds scale, so that they add up to the skaters
expected to dress from among them: DRESSED_SKATERS less the earlier seasons' average newcomers
(dressed skaters who were not candidates), one average for a team's first game of a season and
one for its other games. The window, caps and inputs are fixed by ADR 0017, not tuned.

Each game is rated as of the time team strength uses: 10:00 US Eastern on its date, or an hour
before its start if earlier. A game's own boxscore is read only to score the model. The goalies'
rows of the lineups table are copied from goalie_starts (ADR 0012).
"""

from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast

import numpy as np
import polars as pl
from numpy.typing import NDArray
from scipy.optimize import minimize
from scipy.special import expit

from nhl_edge.features.team_strength import as_of, team_lines
from nhl_edge.lake.schemas import DRESSED_SKATERS, Lineups, dtypes
from nhl_edge.lineup.goalie_start import _group_index, _starts, season_cutoff

COMPONENT = "lineup"
# The first season predicted: 2010-11, the first in the lake, has no earlier season to fit on.
FIRST_SEASON = 20112012
SKATER_ROLES = ("F", "D")
WINDOW = 10
SINCE_CAP = 10
STREAK_CAP = 10
# An early exit: under this share of his average ice time in his other games of the window.
EARLY_EXIT_SHARE = 0.5
# A light ridge that keeps the fit finite where an input never varies; it is not tuned, and it
# leaves the intercept alone.
L2 = 1e-3
FEATURES = (
    "dressed_last",
    "recent_share",
    "since_last",
    "early_exit",
    "streak",
    "defense",
    "opener_dressed_last",
    "opener_recent_share",
)
# The shift's search on the log-odds scale: wide enough for any total between 0 and the
# candidates, and enough halvings to reach float precision.
SHIFT_BOUND = 50.0
BISECTIONS = 64


@dataclass
class _LineState:
    """A team line's last WINDOW games, each as its season and its skaters' (role, time on ice),
    and each skater's run of straight games up to the last one."""

    window: deque[tuple[int, dict[int, tuple[str, int | None]]]] = field(
        default_factory=lambda: deque(maxlen=WINDOW)
    )
    streaks: dict[int, int] = field(default_factory=dict)

    def add(self, season: int, skaters: dict[int, tuple[str, int | None]]) -> None:
        self.streaks = {player: self.streaks.get(player, 0) + 1 for player in skaters}
        self.window.append((season, skaters))


def team_skater_games(lineups: pl.DataFrame) -> pl.DataFrame:
    """One row per team-game: the skaters who dressed, with their role and time on ice, and when
    the boxscore became public."""
    skaters = lineups.filter(pl.col("role").is_in(SKATER_ROLES)).sort("player_id")
    return skaters.group_by("game_id", "season", "game_date", "team").agg(
        players=pl.col("player_id"),
        roles=pl.col("role"),
        toi=pl.col("toi_s"),
        observed_utc=pl.col("observed_utc").max(),
    )


def candidates(
    games: pl.DataFrame, lineups: pl.DataFrame, lines: Mapping[str, str] | None = None
) -> pl.DataFrame:
    """One row per game, team and candidate skater, with his inputs as of the game's prediction
    time (as_of_utc) and whether it is the team's first game of a season (opener). dressed says
    whether he dressed, from the game's own boxscore, label_utc when that became public and
    skaters_dressed how many skaters dressed; all three are null without a boxscore. They are
    labels for fitting and scoring, never inputs."""
    lines = team_lines() if lines is None else lines
    history = team_skater_games(lineups).with_columns(line=pl.col("team").replace(dict(lines)))
    targets = (
        pl.concat(
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
        )
        .with_columns(line=pl.col("team").replace(dict(lines)))
        .sort("as_of_utc", "game_id", "team")
    )
    past = (
        history.sort("observed_utc", "game_date", "game_id", "team")
        .select("season", "line", "players", "roles", "toi", "observed_utc")
        .rows()
    )
    states: dict[str, _LineState] = {}
    # Each player's line in his latest public game, for the drop-out rule.
    latest: dict[int, str] = {}
    rows: list[dict[str, Any]] = []
    seen = 0
    for target in targets.iter_rows(named=True):
        # Only team-games public strictly before the as-of time (hard rule 9).
        while seen < len(past) and past[seen][5] < target["as_of_utc"]:
            season, line, players, roles, toi, _ = past[seen]
            skaters = {p: (r, t) for p, r, t in zip(players, roles, toi, strict=True)}
            states.setdefault(line, _LineState()).add(season, skaters)
            latest.update(dict.fromkeys(players, line))
            seen += 1
        state = states.get(target["line"])
        if state is not None:
            rows.extend(_candidate_rows(target, state, latest))
    frame = pl.DataFrame(rows, schema=_CANDIDATE_SCHEMA)
    team_labels = history.select(
        "game_id", "team", label_utc="observed_utc", skaters_dressed=pl.col("players").list.len()
    )
    dressed = (
        lineups.filter(pl.col("role").is_in(SKATER_ROLES))
        .select("game_id", "team", "player_id")
        .with_columns(dressed=pl.lit(True))
    )
    return (
        frame.join(team_labels, on=["game_id", "team"], how="left")
        .join(dressed, on=["game_id", "team", "player_id"], how="left")
        .with_columns(
            dressed=pl.when(pl.col("label_utc").is_null())
            .then(None)
            .otherwise(pl.col("dressed").fill_null(False))
        )
        .sort("game_id", "team", "player_id")
    )


_CANDIDATE_SCHEMA = {
    "game_id": pl.Int64,
    "season": pl.Int32,
    "game_date": pl.Date,
    "team": pl.String,
    "player_id": pl.Int64,
    "role": pl.String,
    **{name: pl.Float64 for name in FEATURES},
    "opener": pl.Boolean,
    "as_of_utc": pl.Datetime("us", "UTC"),
}


def _early_exit(last: int | None, others: Iterable[int | None]) -> bool:
    """Under EARLY_EXIT_SHARE of his average ice time in his other games; not without them."""
    times = [toi for toi in others if toi is not None]
    if last is None or not times:
        return False
    return last < EARLY_EXIT_SHARE * sum(times) / len(times)


def _candidate_rows(
    target: dict[str, Any], state: _LineState, latest: Mapping[int, str]
) -> list[dict[str, Any]]:
    if not state.window:
        return []
    window = state.window
    last_season, last = window[-1]
    opener = target["season"] > last_season
    players = sorted(
        {p for _, skaters in window for p in skaters if latest.get(p) == target["line"]}
    )
    out = []
    for player in players:
        dressed = [i for i, (_, skaters) in enumerate(window) if player in skaters]
        role = window[dressed[-1]][1][player][0]
        dressed_last = player in last
        share = len(dressed) / len(window)
        since = len(window) - 1 - dressed[-1]
        early = dressed_last and _early_exit(
            last[player][1], (window[i][1][player][1] for i in dressed[:-1])
        )
        streak = state.streaks.get(player, 0)
        out.append(
            {
                "game_id": target["game_id"],
                "season": target["season"],
                "game_date": target["game_date"],
                "team": target["team"],
                "player_id": player,
                "role": role,
                "dressed_last": float(dressed_last),
                "recent_share": share,
                "since_last": min(since, SINCE_CAP) / SINCE_CAP,
                "early_exit": float(early),
                "streak": min(streak, STREAK_CAP) / STREAK_CAP,
                "defense": float(role == "D"),
                "opener_dressed_last": float(opener and dressed_last),
                "opener_recent_share": share if opener else 0.0,
                "opener": opener,
                "as_of_utc": target["as_of_utc"],
            }
        )
    return out


def _design(frame: pl.DataFrame) -> NDArray[np.float64]:
    """The inputs with a leading column of ones for the intercept."""
    return np.column_stack([np.ones(frame.height), frame.select(FEATURES).to_numpy()])


def objective(
    x: NDArray[np.float64], y: NDArray[np.float64]
) -> Callable[[NDArray[np.float64]], tuple[float, NDArray[np.float64]]]:
    """The logistic regression's penalized negative log-likelihood and its gradient: rows x, whose
    first column is the intercept's, and outcomes y. The ridge leaves the intercept alone."""
    ridge = np.r_[0.0, np.ones(x.shape[1] - 1)]

    def loss(beta: NDArray[np.float64]) -> tuple[float, NDArray[np.float64]]:
        eta = x @ beta
        nll = float(np.sum(np.logaddexp(0.0, eta) - y * eta))
        gradient = x.T @ (expit(eta) - y)
        return nll + L2 * float(ridge @ beta**2), gradient + 2 * L2 * ridge * beta

    return loss


def shift_to_total(
    logits: NDArray[np.float64], groups: NDArray[np.int64], totals: NDArray[np.float64]
) -> NDArray[np.float64]:
    """The logistic of logit + c, with one c per group so that the group adds up to its total. A
    group with no more rows than its total gets 1 throughout. Groups are consecutive and numbered
    from 0."""
    if logits.size == 0:
        return np.empty(0)
    starts = _starts(groups)
    low = np.full(len(starts), -SHIFT_BOUND)
    high = np.full(len(starts), SHIFT_BOUND)
    for _ in range(BISECTIONS):
        middle = (low + high) / 2
        above = np.add.reduceat(expit(logits + middle[groups]), starts) > totals
        high = np.where(above, middle, high)
        low = np.where(above, low, middle)
    shifted = expit(logits + ((low + high) / 2)[groups])
    sizes = np.diff(np.r_[starts, len(logits)])
    return np.where((sizes <= totals)[groups], 1.0, shifted)


def team_game_newcomers(rows: pl.DataFrame) -> pl.DataFrame:
    """One row per team-game with a boxscore among the candidate rows: whether it was the team's
    first game of a season, and its newcomers, the dressed skaters who were not candidates."""
    return (
        rows.filter(pl.col("dressed").is_not_null())
        .group_by("game_id", "team")
        .agg(
            season=pl.col("season").first(),
            game_date=pl.col("game_date").first(),
            opener=pl.col("opener").first(),
            newcomers=pl.col("skaters_dressed").first() - pl.col("dressed").sum(),
        )
        .sort("game_id", "team")
    )


@dataclass(frozen=True)
class AvailabilityModel:
    """A fitted logistic regression and the newcomers expected per team-game, the season it
    predicts and the seasons it was fitted on."""

    season: int
    seasons: tuple[int, ...]
    # The intercept, then FEATURES.
    coefficients: tuple[float, ...]
    # Expected newcomers per team-game: in a team's other games, and in its first of a season.
    newcomers: tuple[float, float]
    team_games: int
    train_cutoff: datetime
    artifact_version: str

    def logits(self, frame: pl.DataFrame) -> NDArray[np.float64]:
        return _design(frame) @ np.asarray(self.coefficients)

    def predict(self, frame: pl.DataFrame) -> NDArray[np.float64]:
        """Each candidate row's probability of dressing, shifted so each team-game adds up to
        the skaters expected from among its candidates; frame is sorted by game and team."""
        if frame.is_empty():
            return np.empty(0)
        groups = _group_index(frame)
        opener = frame["opener"].to_numpy()[_starts(groups)]
        totals = DRESSED_SKATERS - np.where(opener, self.newcomers[1], self.newcomers[0])
        return shift_to_total(self.logits(frame), groups, totals)


def fit(
    rows: pl.DataFrame, games: pl.DataFrame, season: int, artifact_version: str
) -> AvailabilityModel:
    """The model for season, fitted on earlier seasons' candidates whose team-game boxscore was
    public before the season's first as-of time, and the newcomers expected from the same
    team-games. Without an earlier first game of a season (2011-12's fit), first games take the
    other games' average."""
    cutoff = season_cutoff(games, season)
    train = rows.filter(
        pl.col("season") < season,
        pl.col("dressed").is_not_null(),
        pl.col("label_utc") < cutoff,
    )
    if train.is_empty():
        raise ValueError(f"no earlier team-games to fit the lineup model for {season}")
    loss = objective(_design(train), train["dressed"].cast(pl.Float64).to_numpy())
    result = minimize(loss, np.zeros(len(FEATURES) + 1), jac=True, method="L-BFGS-B")
    if not result.success:
        raise ValueError(f"lineup fit for {season} did not converge: {result.message}")
    team_games = team_game_newcomers(train)
    other = team_games.filter(~pl.col("opener"))["newcomers"].mean()
    first = team_games.filter(pl.col("opener"))["newcomers"].mean()
    if other is None:
        raise ValueError(f"no earlier team-game past a season's first to fit {season} on")
    last = train["label_utc"].max()
    assert isinstance(last, datetime)
    return AvailabilityModel(
        season=season,
        seasons=tuple(sorted(train["season"].unique().to_list())),
        coefficients=tuple(float(b) for b in cast(NDArray[np.float64], result.x)),
        newcomers=(
            float(cast(float, other)),
            float(cast(float, other if first is None else first)),
        ),
        team_games=team_games.height,
        train_cutoff=last,
        artifact_version=artifact_version,
    )


def score(
    lineups: pl.DataFrame,
    games: pl.DataFrame,
    seasons: Iterable[int],
    artifact_version: str,
    lines: Mapping[str, str] | None = None,
) -> tuple[pl.DataFrame, pl.DataFrame, list[AvailabilityModel]]:
    """The skaters' rows of the lineups table for every team-game of the seasons, the candidate
    rows they came from with p_available (for the report), and the model fitted for each season.
    lineups and games hold the seasons and every earlier one."""
    wanted = sorted(set(seasons))
    early = [season for season in wanted if season < FIRST_SEASON]
    if early:
        raise ValueError(f"{early} have no earlier season to fit the lineup model on")
    if not wanted:
        raise ValueError("no seasons to score")
    rows = candidates(games.filter(pl.col("season") <= wanted[-1]), lineups, lines)
    frames, scored, models = [], [], []
    for season in wanted:
        model = fit(rows, games, season, artifact_version)
        test = rows.filter(pl.col("season") == season).sort("game_id", "team", "player_id")
        test = test.with_columns(p_available=pl.Series(model.predict(test), dtype=pl.Float64))
        scored.append(test)
        frames.append(
            test.select(
                "game_id",
                "season",
                "game_date",
                "team",
                "player_id",
                "role",
                "p_available",
                p_start=pl.lit(None, dtype=pl.Float64),
                train_cutoff=pl.lit(model.train_cutoff, dtype=pl.Datetime("us", "UTC")),
                artifact_version=pl.lit(artifact_version),
                observed_utc="as_of_utc",
            )
        )
        models.append(model)
    return pl.concat(frames), pl.concat(scored), models


def with_goalies(skaters: pl.DataFrame, goalie_starts: pl.DataFrame) -> pl.DataFrame:
    """The lineups table: the skaters' rows and, for the same team-games, each candidate goalie's
    p_start copied from goalie_starts with its own train_cutoff and artifact_version. Refuses
    when a team-game with skaters has no goalie row."""
    team_games = skaters.select("game_id", "team").unique()
    missing = team_games.join(goalie_starts, on=["game_id", "team"], how="anti")
    if missing.height:
        examples = ", ".join(f"{g} {t}" for g, t in missing.sort("game_id", "team").head(3).rows())
        raise ValueError(
            f"{missing.height:,} team-games have no goalie-start probabilities, e.g. {examples}"
        )
    goalies = goalie_starts.join(team_games, on=["game_id", "team"], how="semi").select(
        "game_id",
        "season",
        "game_date",
        "team",
        player_id="goalie_id",
        role=pl.lit("G"),
        p_available=pl.lit(None, dtype=pl.Float64),
        p_start="p_start",
        train_cutoff="train_cutoff",
        artifact_version="artifact_version",
        observed_utc="observed_utc",
    )
    table = pl.concat([skaters, goalies.cast(dtypes(Lineups))], how="vertical_relaxed")  # type: ignore[arg-type]
    table = table.cast(dtypes(Lineups)).sort("game_id", "team", "player_id")  # type: ignore[arg-type]
    return Lineups.validate(table)


def input_problems(
    games: pl.DataFrame, lineups: pl.DataFrame, last: int, expected: Mapping[int, int]
) -> list[str]:
    """Why the lake cannot score lineups up to the season last: a season short of its games
    (expected), or a team-game without skaters in its boxscore, would drop out of the teams'
    histories unnoticed."""
    needed = games.filter(pl.col("season") <= last)
    problems = []
    for season in sorted(s for s in expected if s <= last):
        count = needed.filter(pl.col("season") == season).height
        if count != expected[season]:
            problems.append(f"{season}: {count:,} of {expected[season]:,} games")
    dressed = team_skater_games(lineups).select("game_id", "team")
    team_games = pl.concat(
        [needed.select("game_id", "season", team=pl.col(side)) for side in ("home", "away")]
    )
    missing = team_games.join(dressed, on=["game_id", "team"], how="anti")
    for (season,), frame in missing.sort("game_id").group_by("season", maintain_order=True):
        examples = ", ".join(f"{g} {t}" for g, t in frame.select("game_id", "team").head(3).rows())
        problems.append(f"{season}: {frame.height:,} team-games without skaters, e.g. {examples}")
    return sorted(problems)
