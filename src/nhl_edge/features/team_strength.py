"""Rolling team strength ΔS (#74, docs/plan.md §5, ADR 0011): the home team's expected goal margin
over the away team, from each team's earlier games only, decayed by games played and shrunk toward
the league.

Per team and game, from `shot_xg`, `shots` and `strength_time`:
- **5v5 with both nets manned:** xG for and against, and minutes;
- **power play** (more skaters, both nets manned, at most five): xG for and minutes;
- **penalty kill** (the mirror): xG against and minutes.

Shorthanded xG for, power-play xG against, 4v4, 3v3 and empty-net play are left out: they are
small, and B2's other terms absorb them.

A team's rate before a game sums its earlier games, a game k games back weighing 0.5 ** (k /
half_life), plus `prior_games` games at the league rate:

    rate = (sum of weighted xG + prior_games * league xG per game)
           / (sum of weighted minutes + prior_games * league minutes per game)

Its power-play and penalty-kill minutes per game shrink the same way. The league figures are
those of every team-game public before the prediction time, in the game's season and the one
before it.

ΔS is the home team's expected goals minus the away team's:
- **5v5:** the league's 5v5 minutes per game times the average of one team's xG for and the other's
  xG against per minute;
- **power play:** expected minutes, the average of one team's power-play minutes per game and the
  other's penalty-kill minutes per game, times the average of its power-play xG for and the other's
  penalty-kill xG against per minute.

A game's ΔS is computed as of 10:00 US Eastern on its date, E2's prediction time, or its start if
that is earlier, so E1 and E2 see the same history. Only team-games public before then count
(hard rule 1), which with ADR 0004 means every game up to the day before.
"""

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import polars as pl

from nhl_edge.backtest.seasons import TRAINING_SEASONS
from nhl_edge.features import xg
from nhl_edge.ingest.sbr import ET, OPEN_ASSUMED_AT_ET
from nhl_edge.lake.schemas import TeamStrength, dtypes

COMPONENT = "team-strength"
# The first season with xG, and so with team strength.
FIRST_SEASON = xg.FIRST_SEASON
# Tuned seasons: every training season after the first with xG, each predicted by a model fitted
# on the earlier ones (ADR 0011).
TUNING_SEASONS = tuple(season for season in TRAINING_SEASONS if season > FIRST_SEASON)

POWER_PLAY = ("5v4", "5v3", "4v3")
PENALTY_KILL = ("4v5", "3v5", "3v4")
# Each team-game's sums: (xG column, minutes column) per component, and the minutes alone.
RATES = {
    "xgf_5v5": "min_5v5",
    "xga_5v5": "min_5v5",
    "xgf_pp": "min_pp",
    "xga_pk": "min_pk",
}
PER_GAME = ("min_5v5", "min_pp", "min_pk")
SUMS = (*RATES, *PER_GAME)


@dataclass(frozen=True)
class Settings:
    """Team strength's two tuned settings (ADR 0011)."""

    half_life: float
    prior_games: float

    @property
    def label(self) -> str:
        return f"half-life {self.half_life:g}, prior {self.prior_games:g}"


def as_of(game_date: pl.Expr, start_utc: pl.Expr) -> pl.Expr:
    """10:00 US Eastern on the game date, or the start if earlier."""
    ten = game_date.dt.combine(OPEN_ASSUMED_AT_ET).dt.replace_time_zone(str(ET))
    return pl.min_horizontal(ten.dt.convert_time_zone("UTC"), start_utc)


def team_games(
    shots: pl.DataFrame, shot_xg: pl.DataFrame, strength_time: pl.DataFrame
) -> pl.DataFrame:
    """One row per team and game with xG: its 5v5, power-play and penalty-kill xG and minutes,
    and when the game's feeds became public. Games without xG (2010-11) are left out."""
    both_manned = ~pl.col("own_net_empty") & ~pl.col("opp_net_empty")

    def minutes(states: Sequence[str]) -> pl.Expr:
        mask = both_manned & pl.col("strength").is_in(list(states))
        return (pl.col("seconds").filter(mask).sum() / 60).cast(pl.Float64)

    time_on = strength_time.group_by("game_id", "team").agg(
        min_5v5=minutes(["5v5"]),
        min_pp=minutes(POWER_PLAY),
        min_pk=minutes(PENALTY_KILL),
        time_observed=pl.col("observed_utc").max(),
    )
    for_, against = pl.col("skaters_for"), pl.col("skaters_against")
    xg = shot_xg.join(
        shots.select("game_id", "event_id", "team", "skaters_for", "skaters_against"),
        on=["game_id", "event_id"],
    )
    by_team = xg.group_by("game_id", "team").agg(
        xg_5v5=pl.col("xg").filter((for_ == 5) & (against == 5)).sum(),
        xg_pp=pl.col("xg").filter((for_ > against) & (for_ <= 5)).sum(),
        shots_observed=pl.col("observed_utc").max(),
    )
    games = strength_time.select("game_id", "season", "game_date", "team").unique()
    pairs = games.join(games.rename({"team": "opponent"}), on=["game_id", "season", "game_date"])
    pairs = pairs.filter(pl.col("team") != pl.col("opponent"))
    own = by_team.rename({"xg_5v5": "xgf_5v5", "xg_pp": "xgf_pp"})
    opp = by_team.select(
        "game_id", opponent="team", xga_5v5="xg_5v5", xga_pk="xg_pp", opp_observed="shots_observed"
    )
    frame = (
        pairs.join(own, on=["game_id", "team"], how="inner")
        .join(opp, on=["game_id", "opponent"], how="inner")
        .join(time_on, on=["game_id", "team"], how="inner")
    )
    return frame.select(
        "game_id",
        "season",
        "game_date",
        "team",
        "opponent",
        *SUMS,
        observed_utc=pl.max_horizontal("shots_observed", "opp_observed", "time_observed"),
    ).sort("team", "game_date", "game_id")


def _decayed(values: np.ndarray, decay: float) -> np.ndarray:
    """Running sums with older rows decayed: out[j] = values[j] + decay * out[j - 1]."""
    out = np.empty_like(values)
    total = np.zeros(values.shape[1])
    for j, row in enumerate(values):
        total = row + decay * total
        out[j] = total
    return out


def team_states(history: pl.DataFrame, targets: pl.DataFrame, settings: Settings) -> pl.DataFrame:
    """Each target (team, as_of) row with the team's decayed sums over its team-games public
    before as_of, and the decayed game count."""
    decay = 0.5 ** (1 / settings.half_life)
    columns = [*SUMS, "games"]
    frames = []
    for (team,), rows in targets.group_by("team"):
        past = history.filter(pl.col("team") == team).sort("observed_utc", "game_date", "game_id")
        values = past.select(*SUMS, games=pl.lit(1.0)).to_numpy().astype(float)
        sums = _decayed(values, decay) if len(values) else np.zeros((0, len(columns)))
        # The team-games public before each target: a prefix of the history, by observed_utc.
        seen = np.searchsorted(
            past["observed_utc"].to_numpy(), rows["as_of_utc"].to_numpy(), side="left"
        )
        state = np.zeros((rows.height, len(columns)))
        has = seen > 0
        state[has] = sums[seen[has] - 1]
        frames.append(
            rows.with_columns(
                pl.Series(name, state[:, i]) for i, name in enumerate(columns)
            ).with_columns(history_games=pl.Series(seen, dtype=pl.Int32))
        )
    return pl.concat(frames)


def league_states(history: pl.DataFrame, targets: pl.DataFrame) -> pl.DataFrame:
    """For each distinct (season, as_of), the league's totals over every team-game public before
    as_of in that season and the one before."""
    out = []
    for (season,), rows in targets.select("season", "as_of_utc").unique().group_by("season"):
        past = history.filter(pl.col("season").is_in([season, season - 10001])).sort("observed_utc")
        totals = past.select(*SUMS, games=pl.lit(1.0)).to_numpy().astype(float).cumsum(axis=0)
        seen = np.searchsorted(
            past["observed_utc"].to_numpy(), rows["as_of_utc"].to_numpy(), side="left"
        )
        state = np.zeros((rows.height, len(SUMS) + 1))
        has = seen > 0
        state[has] = totals[seen[has] - 1]
        out.append(
            rows.with_columns(
                pl.Series(f"league_{name}", state[:, i]) for i, name in enumerate([*SUMS, "games"])
            )
        )
    return pl.concat(out)


def strength(games: pl.DataFrame, history: pl.DataFrame, settings: Settings) -> pl.DataFrame:
    """ΔS and its parts for every game in games, from the team-games in history (team_games)."""
    targets = games.select(
        "game_id",
        "season",
        "game_date",
        "home",
        "away",
        as_of_utc=as_of(pl.col("game_date"), pl.col("start_utc")),
    )
    sides = pl.concat(
        [
            targets.select("game_id", "season", "as_of_utc", team=pl.col(side), side=pl.lit(side))
            for side in ("home", "away")
        ]
    )
    teams = team_states(history, sides, settings)
    league = league_states(history, sides)
    rows = teams.join(league, on=["season", "as_of_utc"])
    m = settings.prior_games
    games_ = pl.col("league_games")
    rates = {}
    for total, minutes in RATES.items():
        league_xg = pl.col(f"league_{total}") / games_
        league_min = pl.col(f"league_{minutes}") / games_
        rates[f"{total}_per_min"] = (pl.col(total) + m * league_xg) / (
            pl.col(minutes) + m * league_min
        )
    for minutes in PER_GAME:
        league_min = pl.col(f"league_{minutes}") / games_
        rates[f"{minutes}_per_game"] = (pl.col(minutes) + m * league_min) / (pl.col("games") + m)
    rated = rows.with_columns(
        league_5v5_per_game=pl.col("league_min_5v5") / games_, **rates
    ).select(
        "game_id",
        "side",
        "history_games",
        "league_5v5_per_game",
        *rates,
    )
    home = rated.filter(pl.col("side") == "home").drop("side")
    away = rated.filter(pl.col("side") == "away").drop("side", "league_5v5_per_game")
    both = targets.join(home, on="game_id").join(away, on="game_id", suffix="_away")

    def expected(attack: str, defend: str) -> tuple[pl.Expr, pl.Expr]:
        a = "" if attack == "home" else "_away"
        d = "" if defend == "home" else "_away"
        five = (
            pl.col("league_5v5_per_game")
            * (pl.col(f"xgf_5v5_per_min{a}") + pl.col(f"xga_5v5_per_min{d}"))
            / 2
        )
        pp_minutes = (pl.col(f"min_pp_per_game{a}") + pl.col(f"min_pk_per_game{d}")) / 2
        pp = pp_minutes * (pl.col(f"xgf_pp_per_min{a}") + pl.col(f"xga_pk_per_min{d}")) / 2
        return five, pp

    home_five, home_pp = expected("home", "away")
    away_five, away_pp = expected("away", "home")
    # Before any team-game with xG is public (2011-12's first night), there is nothing to rate:
    # ΔS is 0.
    deltas = {
        "delta_s": (home_five + home_pp) - (away_five + away_pp),
        "delta_5v5": home_five - away_five,
        "delta_special_teams": home_pp - away_pp,
    }
    return both.select(
        "game_id",
        "season",
        "game_date",
        "home",
        "away",
        *(expr.fill_nan(0.0).fill_null(0.0).alias(name) for name, expr in deltas.items()),
        home_history=pl.col("history_games"),
        away_history=pl.col("history_games_away"),
        as_of_utc="as_of_utc",
    ).sort("game_id")


# The tuning grid and the order that breaks ties toward the steadier setting (ADR 0011).
GRID = tuple(Settings(h, m) for h in (10, 20, 40, 80) for m in (0, 10, 20, 40))


def steadiness(settings: Settings) -> tuple[float, float]:
    """Longer memory first, then more pull toward the league."""
    return settings.half_life, settings.prior_games


# Frozen by run team-strength-20261001-9dc689a on #74's PR (ADR 0011): the leader, on the
# grid's steadiest corner. The owner kept the grid as fixed.
TUNED = Settings(half_life=80, prior_games=40)


def rows(
    games: pl.DataFrame, history: pl.DataFrame, settings: Settings, artifact_version: str
) -> pl.DataFrame:
    """The games' TeamStrength rows."""
    frame = strength(games, history, settings).with_columns(
        half_life=pl.lit(float(settings.half_life)),
        prior_games=pl.lit(float(settings.prior_games)),
        artifact_version=pl.lit(artifact_version),
        observed_utc=pl.col("as_of_utc"),
    )
    columns = dtypes(TeamStrength)
    return TeamStrength.validate(frame.select(list(columns)).cast(columns))  # type: ignore[arg-type]


def input_problems(
    games: pl.DataFrame, shot_xg: pl.DataFrame, strength_time: pl.DataFrame, last: int
) -> list[str]:
    """Why the lake cannot rate games up to the season last: a game of 2011-12 on without xG or
    without strength time would drop out of its teams' histories unnoticed."""
    needed = games.filter(pl.col("season").is_between(FIRST_SEASON, last))
    problems = []
    for label, table in (("xG", shot_xg), ("strength time", strength_time)):
        missing = needed.join(table.select("game_id").unique(), on="game_id", how="anti")
        for (season,), frame in missing.sort("game_id").group_by("season", maintain_order=True):
            examples = ", ".join(str(g) for g in frame["game_id"].head(3).to_list())
            problems.append(f"{season}: {frame.height:,} games without {label}, e.g. {examples}")
    return sorted(problems)
