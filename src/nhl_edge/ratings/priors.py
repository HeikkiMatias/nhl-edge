"""Priors for RAPM (#102, ADR 0020, docs/plan.md §5): the rating each skater is pulled toward
before his own stints say otherwise, from his age, draft slot and minor-league scoring, and the age
curve RAPM's aging will use (#103).

**Traits,** per player and season, centred on a reference skater: a 27-year-old, drafted 60th,
with no recent minor-league season.
- age: his age on October 1 of the season's first year, less 27, and its square;
- slot: log(pick / 60) for a drafted player, 0 for an undrafted one, with an undrafted flag;
- nhle: his NHLe points per game less 0.3, and a flag that he has one (offense and the power play
  only). It is his non-NHL league seasons of the two seasons before, regular season, at least
  MIN_GAMES games each, translated by the league's NHLe factor and weighted by games.

**NHLe factors,** per season: for each move from a league season straight into the next NHL
season, both at least MIN_GAMES games, the NHL points per game over the league's, as a ratio of
sums over the moves whose NHL season is one of the NHLE_WINDOW seasons before. A league with
fewer than POOL_MOVES moves joins one pooled factor. A season's lines are read only once public
(`player_league_seasons.observed_utc`, never before the player's first boxscore), so a future
debutant never shapes a factor. A league listed under two abbreviations in one season keeps the
abbreviation with more games.

**The trait effects** are fitted inside RAPM once before each season (rapm.py): each trait enters
the season-start fit as a column, the attacking skaters' traits for offense and the defending
skaters' for defense, beside the players' own columns. A player's prior mean for the season is his
traits times those effects. Defense and the penalty kill use age and draft slot but no NHLe.

**The age curve,** per season and component: each player's change in rating from his last game of
one season to his last game of the next, for players with at least AGE_HOURS hours behind both
(AGE_PP_HOURS on the power play and the penalty kill), regressed on his age at the second season
(less 27) and its square, weighted by the harmonic mean of the two seasons' hours. Only pairs of
seasons that ended before the season starts count.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime

import numpy as np
import polars as pl

from nhl_edge.lake.tables import known_at

NHL = "NHL"
REGULAR_SEASON = 2
# A league season counts with at least this many games: no tournament reaches it.
MIN_GAMES = 15
NHLE_WINDOW = 10
POOL_MOVES = 30
POOLED = "pooled"
# A player's NHLe reads his league seasons of the two seasons before.
NHLE_SEASONS = 2
REFERENCE_AGE = 27.0
REFERENCE_PICK = 60.0
REFERENCE_PPG = 0.3
AGE_HOURS = 10.0
AGE_PP_HOURS = 2.0

OFFENSE_TRAITS = ("age", "age2", "slot", "undrafted", "nhle", "has_nhle")
DEFENSE_TRAITS = ("age", "age2", "slot", "undrafted")
# Each RAPM component's traits.
TRAITS: Mapping[str, tuple[str, ...]] = {
    "ev_off": OFFENSE_TRAITS,
    "ev_def": DEFENSE_TRAITS,
    "pp": OFFENSE_TRAITS,
    "pk": DEFENSE_TRAITS,
}
ALL_TRAITS = OFFENSE_TRAITS


def season_start(season: int) -> date:
    """October 1 of the season's first year, the day ages are taken on."""
    return date(season // 10_000, 10, 1)


def league_lines(lines: pl.DataFrame, cutoff: datetime) -> pl.DataFrame:
    """Each player's regular-season games and points per season and league, from the lines of
    player_league_seasons public before cutoff. A team-by-team split is summed; one league under
    two abbreviations keeps the one with more games."""
    public = known_at(lines, cutoff).filter(pl.col("game_type") == REGULAR_SEASON)
    by_abbrev = public.group_by("player_id", "season", "league", "league_abbrev").agg(
        games=pl.col("games_played").cast(pl.Int64).sum(),
        points=(pl.col("goals").cast(pl.Int64) + pl.col("assists").cast(pl.Int64)).sum(),
    )
    return (
        by_abbrev.sort("games", "league_abbrev", descending=[True, False])
        .unique(["player_id", "season", "league"], keep="first", maintain_order=True)
        .select("player_id", "season", "league", "games", "points")
        .sort("player_id", "season", "league")
    )


def _previous(season: int, back: int = 1) -> int:
    return season - back * 10_001


def nhle_factors(lines: pl.DataFrame, season: int) -> pl.DataFrame:
    """The NHLe factor of each league with POOL_MOVES moves or more, and a pooled one for the
    rest, from league_lines() public before the season: league, moves, factor."""
    first = _previous(season, NHLE_WINDOW)
    nhl = lines.filter(pl.col("league") == NHL, pl.col("games") >= MIN_GAMES).select(
        "player_id", nhl_season="season", nhl_games="games", nhl_points="points"
    )
    moves = (
        lines.filter(pl.col("league") != NHL, pl.col("games") >= MIN_GAMES)
        .with_columns(nhl_season=pl.col("season") + 10_001)
        .join(nhl, on=["player_id", "nhl_season"])
        .filter(pl.col("nhl_season").is_between(first, _previous(season)))
    )
    counts = moves.group_by("league").agg(moves=pl.len())
    pooled = moves.join(counts, on="league").with_columns(
        league=pl.when(pl.col("moves") >= POOL_MOVES)
        .then(pl.col("league"))
        .otherwise(pl.lit(POOLED))
    )
    factors = pooled.group_by("league").agg(
        moves=pl.len(),
        factor=(pl.col("nhl_points").sum() / pl.col("nhl_games").sum())
        / (pl.col("points").sum() / pl.col("games").sum()),
    )
    return factors.filter(pl.col("factor").is_finite()).sort(
        "moves", "league", descending=[True, False]
    )


def nhle_inputs(lines: pl.DataFrame, factors: pl.DataFrame, season: int) -> pl.DataFrame:
    """Each player's NHLe points per game for the season: his non-NHL league seasons of the
    NHLE_SEASONS seasons before, at least MIN_GAMES games each, translated by their league's
    factor (the pooled one for a league without its own) and weighted by games."""
    if factors.is_empty():
        return pl.DataFrame(schema={"player_id": pl.Int64, "nhle_ppg": pl.Float64})
    own = dict(zip(factors["league"], factors["factor"], strict=True))
    pooled = own.get(POOLED)
    recent = [_previous(season, k) for k in range(1, NHLE_SEASONS + 1)]
    rows = lines.filter(
        pl.col("league") != NHL,
        pl.col("games") >= MIN_GAMES,
        pl.col("season").is_in(recent),
    ).with_columns(
        factor=pl.col("league").replace_strict(own, default=pooled, return_dtype=pl.Float64)
    )
    return (
        rows.filter(pl.col("factor").is_not_null())
        .group_by("player_id")
        .agg(nhle_ppg=(pl.col("points") * pl.col("factor")).sum() / pl.col("games").sum())
        .sort("player_id")
    )


def traits(players: pl.DataFrame, nhle: pl.DataFrame, season: int) -> pl.DataFrame:
    """Every player's centred traits for the season (ALL_TRAITS), from players (birth_date,
    draft_overall) and nhle_inputs()."""
    start = season_start(season)
    age = (pl.lit(start) - pl.col("birth_date")).dt.total_days() / 365.25 - REFERENCE_AGE
    pick = pl.col("draft_overall").cast(pl.Float64)
    has = pl.col("nhle_ppg").is_not_null()
    return (
        players.select("player_id", "birth_date", "draft_overall")
        .unique("player_id", keep="first")
        .join(nhle, on="player_id", how="left")
        .select(
            "player_id",
            age=age.fill_null(0.0),
            age2=age.fill_null(0.0) ** 2,
            slot=(pick / REFERENCE_PICK).log().fill_null(0.0),
            undrafted=pick.is_null().cast(pl.Float64),
            nhle=pl.when(has).then(pl.col("nhle_ppg") - REFERENCE_PPG).otherwise(0.0),
            has_nhle=has.cast(pl.Float64),
        )
        .sort("player_id")
    )


@dataclass(frozen=True)
class AgeCurve:
    """A component's expected change in rating from one season to the next by age: b0 + b1·a +
    b2·a², a his age at the second season less 27, from `pairs` pairs of seasons."""

    component: str
    coefficients: tuple[float, float, float]
    pairs: int

    def change(self, age: float) -> float:
        a = age - REFERENCE_AGE
        b0, b1, b2 = self.coefficients
        return b0 + b1 * a + b2 * a * a


def age_curves(season_ends: pl.DataFrame, season: int) -> dict[str, AgeCurve]:
    """The age curve of each component for the season, from season_ends (season, player_id,
    component, mean, hours, age: each player's rating at his last game of a season, age less
    27) of the seasons that ended before it. A component with fewer than four pairs gets none."""
    earlier = season_ends.filter(pl.col("season") < season)
    later = earlier.with_columns(season=pl.col("season") - 10_001)
    pairs = earlier.join(later, on=["season", "player_id", "component"], suffix="_next")
    out = {}
    for component in TRAITS:
        least = AGE_PP_HOURS if component in ("pp", "pk") else AGE_HOURS
        rows = pairs.filter(
            pl.col("component") == component,
            pl.col("hours") >= least,
            pl.col("hours_next") >= least,
        )
        if rows.height < 4:
            continue
        a = rows["age_next"].to_numpy()
        x = np.column_stack([np.ones_like(a), a, a * a])
        h, h_next = rows["hours"].to_numpy(), rows["hours_next"].to_numpy()
        w = 2 / (1 / h + 1 / h_next)
        dy = rows["mean_next"].to_numpy() - rows["mean"].to_numpy()
        b = np.linalg.lstsq(x * np.sqrt(w)[:, None], dy * np.sqrt(w), rcond=None)[0]
        out[component] = AgeCurve(component, (float(b[0]), float(b[1]), float(b[2])), rows.height)
    return out


@dataclass(frozen=True)
class PriorFit:
    """A season's priors: its NHLe factors, each component's trait effects (from the
    season-start RAPM fit), and its age curves, all from data public before cutoff."""

    season: int
    cutoff: datetime
    factors: pl.DataFrame
    effects: Mapping[str, Mapping[str, float]] = field(default_factory=dict)
    curves: Mapping[str, AgeCurve] = field(default_factory=dict)


def prior_means(
    season_traits: pl.DataFrame, effects: Mapping[str, Mapping[str, float]]
) -> pl.DataFrame:
    """Each player's prior mean per component: his traits times the component's effects, 0
    without effects (before any data)."""
    exprs = {
        component: pl.sum_horizontal(
            [pl.col(t) * effects.get(component, {}).get(t, 0.0) for t in names]
        )
        for component, names in TRAITS.items()
    }
    return season_traits.select("player_id", **exprs)


def leagues_shown(factors: pl.DataFrame, limit: int = 8) -> Sequence[dict[str, object]]:
    """The leagues with the most moves, then the pooled factor, for the report."""
    own = factors.filter(pl.col("league") != POOLED).head(limit)
    rest = factors.filter(pl.col("league") == POOLED)
    return [*own.iter_rows(named=True), *rest.iter_rows(named=True)]
