"""The uncertainty score u (docs/plan.md §5, #139, ADR 0026): how much doubt hangs over a game's
projected lineups at its prediction time. The market blend (#140) lets the model's weight depend
on it, and the policy (#141) raises its hurdle and lowers its stake.

u has three parts, each per game from rows known before its prediction time (observed_utc):
- goalie doubt: 1 minus the likeliest candidate starter's p_start (goalie_starts), averaged over
  the two teams. A team without candidates is fully in doubt (1). Live, a confirmed starter makes
  it 0.
- availability doubt: the sum of p·(1-p) over a team's skater candidates (lineups.p_available),
  the expected number of lineup surprises, averaged over the two teams.
- rookie minutes: the share of both teams' projected 5v5 minutes (exp_5v5) that goes to skaters
  with fewer than ROOKIE_GAMES earlier NHL regular-season games, or to replacement slots
  (lineup_replacements). A skater's earlier games are his NHL lines of earlier seasons
  (player_league_seasons, public at each season's end) plus this season's boxscores
  (actual_lineups), each counted once known.

u is the average of the three parts, each standardized on the fold's training games (Scale). The
weights are equal, and nothing is tuned.
"""

from dataclasses import dataclass
from datetime import datetime

import numpy as np
import polars as pl

# A skater with fewer earlier NHL regular-season games than one full season is a rookie.
ROOKIE_GAMES = 82
NHL = "NHL"
REGULAR_SEASON = 2
PARTS = ("goalie_doubt", "availability_doubt", "rookie_share")
SKATERS = ("F", "D")
UTC = pl.Datetime("us", "UTC")


@dataclass(frozen=True)
class Tables:
    """The lake tables u reads."""

    games: pl.DataFrame
    goalie_starts: pl.DataFrame
    lineups: pl.DataFrame
    lineup_replacements: pl.DataFrame
    actual_lineups: pl.DataFrame
    player_league_seasons: pl.DataFrame


def _sides(games: pl.DataFrame, moments: pl.DataFrame) -> pl.DataFrame:
    """Each game of moments twice, once per team, with its season and prediction time."""
    timed = moments.select("game_id", "prediction_utc").join(
        games.select("game_id", "season", "home", "away"), on="game_id"
    )
    return pl.concat(
        [
            timed.select("game_id", "season", "prediction_utc", team=pl.col(side))
            for side in ("home", "away")
        ]
    )


def _known(rows: pl.DataFrame, sides: pl.DataFrame) -> pl.DataFrame:
    """rows (game_id, team, observed_utc, ...) of the teams in sides, known before each game's
    prediction time."""
    return rows.join(sides, on=["game_id", "team"]).filter(
        pl.col("observed_utc") < pl.col("prediction_utc")
    )


def earlier_games(skaters: pl.DataFrame, tables: Tables) -> pl.DataFrame:
    """Each skater row (game_id, season, player_id, prediction_utc) with his earlier NHL
    regular-season games known before the prediction time (earlier_games), and the latest row
    that count read (games_utc)."""
    keys = skaters.select(
        "game_id", pl.col("season").cast(pl.Int32), "player_id", "prediction_utc"
    ).unique()
    lines = tables.player_league_seasons.filter(
        pl.col("league") == NHL, pl.col("game_type") == REGULAR_SEASON
    ).select(
        "player_id",
        line_season="season",
        played=pl.col("games_played").fill_null(0),
        line_utc="observed_utc",
    )
    career = (
        keys.join(lines, on="player_id")
        .filter(
            pl.col("line_season") < pl.col("season"),
            pl.col("line_utc") < pl.col("prediction_utc"),
        )
        .group_by("game_id", "player_id")
        .agg(before=pl.col("played").cast(pl.Int64).sum(), lines_utc=pl.col("line_utc").max())
    )
    # This season's boxscores: a running count per player, read up to the prediction time.
    box = (
        tables.actual_lineups.filter(pl.col("role").is_in(SKATERS))
        .select("player_id", pl.col("season").cast(pl.Int32), "game_id", box_utc="observed_utc")
        .unique(["player_id", "game_id"])
        .sort("player_id", "season", "box_utc")
        .with_columns(dressed=pl.int_range(1, pl.len() + 1).over("player_id", "season"))
        .select("player_id", "season", "box_utc", "dressed")
    )
    this_season = (
        keys.sort("prediction_utc")
        .join_asof(
            box.sort("box_utc"),
            left_on="prediction_utc",
            right_on="box_utc",
            by=["player_id", "season"],
            strategy="backward",
            # A boxscore public only at the prediction time is not read.
            allow_exact_matches=False,
            # Both sides are sorted by time over all players, so within each player too.
            check_sortedness=False,
        )
        .select("game_id", "player_id", "dressed", "box_utc")
    )
    return (
        keys.join(career, on=["game_id", "player_id"], how="left")
        .join(this_season, on=["game_id", "player_id"], how="left")
        .select(
            "game_id",
            "player_id",
            earlier_games=pl.col("before").fill_null(0) + pl.col("dressed").fill_null(0),
            games_utc=pl.max_horizontal("lines_utc", "box_utc"),
        )
    )


def parts(tables: Tables, moments: pl.DataFrame) -> pl.DataFrame:
    """u's three parts for each game of moments (game_id, prediction_utc), from rows known before
    its prediction time, with when the latest row read became known (observed_utc) and the latest
    train_cutoff of the fitted tables read (goalie_starts, lineups, lineup_replacements), which a
    caller checks against its fold start. A game whose lineups were not known by then has no
    row."""
    sides = _sides(tables.games, moments)
    goalies = (
        _known(
            tables.goalie_starts.select(
                "game_id", "team", "p_start", "train_cutoff", "observed_utc"
            ),
            sides,
        )
        .group_by("game_id", "team")
        .agg(
            top=pl.col("p_start").max(),
            goalies_utc=pl.col("observed_utc").max(),
            goalies_cutoff=pl.col("train_cutoff").max(),
        )
    )
    skaters = _known(
        tables.lineups.filter(pl.col("role").is_in(SKATERS)).select(
            "game_id",
            "team",
            "player_id",
            "p_available",
            "exp_5v5",
            "train_cutoff",
            "observed_utc",
        ),
        sides,
    )
    counted = skaters.join(earlier_games(skaters, tables), on=["game_id", "player_id"], how="left")
    rookie = pl.col("earlier_games").fill_null(0) < ROOKIE_GAMES
    minutes = pl.col("exp_5v5").fill_null(0.0)
    p = pl.col("p_available").fill_null(0.0)
    teams = counted.group_by("game_id", "team").agg(
        surprises=(p * (1 - p)).sum(),
        minutes=minutes.sum(),
        rookie_minutes=(minutes * rookie).sum(),
        lineups_utc=pl.col("observed_utc").max(),
        lineups_cutoff=pl.col("train_cutoff").max(),
        games_utc=pl.col("games_utc").max(),
    )
    spare = (
        _known(
            tables.lineup_replacements.select(
                "game_id", "team", "exp_5v5", "train_cutoff", "observed_utc"
            ),
            sides,
        )
        .group_by("game_id", "team")
        .agg(
            spare=pl.col("exp_5v5").sum(),
            spare_utc=pl.col("observed_utc").max(),
            spare_cutoff=pl.col("train_cutoff").max(),
        )
    )
    per_team = (
        sides.join(teams, on=["game_id", "team"], how="left")
        .join(spare, on=["game_id", "team"], how="left")
        .join(goalies, on=["game_id", "team"], how="left")
        .with_columns(
            pl.col("surprises", "minutes", "rookie_minutes", "spare").fill_null(0.0),
            known=pl.max_horizontal("lineups_utc", "spare_utc", "goalies_utc", "games_utc"),
            cutoff=pl.max_horizontal("lineups_cutoff", "spare_cutoff", "goalies_cutoff"),
            has_lineup=pl.col("lineups_utc").is_not_null() | pl.col("spare_utc").is_not_null(),
        )
    )
    return (
        per_team.group_by("game_id")
        .agg(
            goalie_doubt=(1 - pl.col("top").fill_null(0.0)).mean(),
            availability_doubt=pl.col("surprises").mean(),
            unknown=(pl.col("rookie_minutes") + pl.col("spare")).sum(),
            total=(pl.col("minutes") + pl.col("spare")).sum(),
            teams=pl.col("has_lineup").sum(),
            observed_utc=pl.col("known").max(),
            train_cutoff=pl.col("cutoff").max(),
        )
        # Both teams need a projected lineup known by the prediction time.
        .filter(pl.col("teams") == 2, pl.col("total") > 0)
        .select(
            "game_id",
            "goalie_doubt",
            "availability_doubt",
            rookie_share=pl.col("unknown") / pl.col("total"),
            train_cutoff=pl.col("train_cutoff").cast(UTC),
            observed_utc=pl.col("observed_utc").cast(UTC),
        )
        .sort("game_id")
    )


@dataclass(frozen=True)
class Scale:
    """Each part's mean and standard deviation over a fold's training games, so u is in the
    same units on the games the fold scores."""

    means: tuple[float, ...]
    sds: tuple[float, ...]
    games: int
    train_cutoff: datetime

    def score(self, frame: pl.DataFrame) -> pl.Series:
        """u for each row of frame (PARTS): the average of the standardized parts."""
        x = frame.select(PARTS).to_numpy()
        z = (x - np.asarray(self.means)) / np.asarray(self.sds)
        return pl.Series("u", z.mean(axis=1), dtype=pl.Float64)


def fit_scale(training: pl.DataFrame) -> Scale:
    """The Scale of training's rows (PARTS, observed_utc, train_cutoff). Its train_cutoff is the
    latest time any row behind them became known, or any fitted table they read was cut off, so
    a fold can refuse a Scale it could not have had. A part that never varies gets a spread of
    1, so it adds nothing to u."""
    if training.is_empty():
        raise ValueError("no training games to standardize u on")
    cutoff = training.select(pl.max_horizontal("observed_utc", "train_cutoff").max()).item()
    assert isinstance(cutoff, datetime)
    x = training.select(PARTS).to_numpy()
    sds = x.std(axis=0)
    return Scale(
        means=tuple(float(m) for m in x.mean(axis=0)),
        sds=tuple(float(s) if s > 0 else 1.0 for s in sds),
        games=training.height,
        train_cutoff=cutoff,
    )


def spread(frame: pl.DataFrame) -> dict[str, dict[str, float | int]]:
    """Each part's spread over frame's games (PARTS): count, mean, standard deviation and the
    10th, 50th and 90th percentiles. Inputs only, for the report."""
    out: dict[str, dict[str, float | int]] = {}
    for part in PARTS:
        values = frame[part]
        out[part] = {
            "games": values.len(),
            "mean": float(values.mean()),  # type: ignore[arg-type]
            "sd": float(values.std()),  # type: ignore[arg-type]
            "p10": float(values.quantile(0.1)),  # type: ignore[arg-type]
            "p50": float(values.quantile(0.5)),  # type: ignore[arg-type]
            "p90": float(values.quantile(0.9)),  # type: ignore[arg-type]
        }
    return out
