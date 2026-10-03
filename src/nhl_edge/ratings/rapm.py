"""RAPM per strength state (#101, ADR 0019, docs/plan.md §5): each skater's offense and defense
per hour at 5v5, and his power-play offense and penalty-kill defense, refit every game day from
the stints public before it.

**5v5 model.** Each kept 5v5 stint with both goalies in net gives two rows, one per team
attacking. The response is the attacking team's xG per hour in the stint, weighted by the stint's
hours times its decay:

    xG per hour = intercept + season + arena + home + score + zone
                  + Σ attacking skaters' offense - Σ defending skaters' defense

- season: the season's departure from the intercept, the league rate;
- arena: the arena's departure (ADR 0019): some arenas run high or low for both teams;
- home: the attacking team is at home, not at a neutral site;
- score: the attacking team's lead at the stint's start, capped at two either way, tied the base;
- zone: the faceoff that opens the stint, from the attacking team's side, a change on the fly the
  base.
These terms only remove bias from the ratings (plan §5): home ice lives in h_s.

**Power-play model.** Each kept stint at 5v4, 5v3 or 4v3 gives one row, the team on the power
play attacking: its power-play offense against the other's penalty-kill defense, with the same
terms but the arena, plus the situation (5v4 the base) and the defensemen on the power play.
That term carries a defenseman's average on the power play, about half of whose 5v4 time has one
defenseman and half two; the stored power-play rating of a defenseman adds it back. At 5v5, where
99.3% of the time is three forwards and two defensemen, and on the penalty kill, where 99.7% of the
5v4 time has two defensemen, a forward's and a defenseman's averages cannot be told apart, so
there every skater is pulled toward the average skater.

**Fitting.** Ridge regression: every player's rating is pulled toward 0, the average skater of
his role, by `pull_hours` hours of ice time (the position average until the priors task, #102),
and the season and arena terms by BIAS_PULL_HOURS, which only keeps the fit solvable. A stint's
decay is 0.5 ** (k / half_life_days), k the league game days from its date to the latest date
read, whether or not a model kept a row from that date. The weighted sums behind the fit are
kept from day to day, so each refit costs one solve. Goalies are not columns: they enter B3 only
through its goalie conversion (plan §5).

A game's ratings read only stints public before its as-of time (team strength's: 10:00 US Eastern
on the game date, or an hour before the start if that is earlier), which with ADR 0004 means every
game up to the day before.

Ratings are per hour of ice time, in xG. Each `sd` is the ridge's posterior spread: the residual
variance per hour of the decayed fit times the diagonal of the inverse of the penalized weighted
sums. A defenseman's power-play sd adds the defensemen term's variance and its covariance with
his own column. `hours` is the decayed ice time behind the rating.
"""

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, cast

import numpy as np
import polars as pl
import scipy.linalg as sl
import scipy.sparse as sp
from numpy.typing import NDArray

from nhl_edge.features import team_strength as ts
from nhl_edge.features import xg
from nhl_edge.lake.schemas import PlayerRatings, RapmTerms, dtypes
from nhl_edge.lineup import goalie_start as gs
from nhl_edge.ratings import priors

COMPONENT = "rapm"
# The first season with xG, and so with ratings.
FIRST_SEASON = xg.FIRST_SEASON
SECONDS_PER_HOUR = 3600.0

EV, PP = "ev", "pp"
MODELS = (EV, PP)
# Each model's two player components: the attacking skaters', then the defending skaters'.
COMPONENTS = {EV: ("ev_off", "ev_def"), PP: ("pp", "pk")}
EV_STRENGTH = "5v5"
# Power-play strengths from the home side; the away team's power plays are their mirror.
PP_STRENGTHS = ts.POWER_PLAY
BASE_SITUATION = "5v4"
SCORE_CAP = 2
SCORES = (-2, -1, 1, 2)
ZONES = ("O", "N", "D")
FLIPPED_ZONE = {"O": "D", "N": "N", "D": "O"}
SKATER_ROLES = ("F", "D")
DEFENSE = "D"
INTERCEPT = "intercept"
SIGMA = "sigma"
# The power play's defensemen term: a defenseman's average there, against a forward's.
DEFENSEMEN = "defensemen"
# The prefix of the priors' trait columns.
PRIOR = "prior"


@dataclass(frozen=True)
class Settings:
    """RAPM's three tuned settings (ADR 0011, #103): the memory, as a half-life in league game
    days; the pull toward the prior mean, in hours of ice time; and the aging weight, the share
    of the age curve's expected change (#102, ADR 0020) that shifts a player's past evidence at
    each season's start."""

    half_life_days: float
    pull_hours: float
    aging: float = 0.0

    @property
    def label(self) -> str:
        return (
            f"half-life {self.half_life_days:g} game days, pull {self.pull_hours:g} hours, "
            f"aging {self.aging:g}"
        )


# Provisional until the tuning task (#103), as the owner chose on 2026-10-02 (ADR 0019): one
# season of league game days, and a pull worth 20 hours, about a regular forward's 5v5 season.
PROVISIONAL = Settings(half_life_days=180.0, pull_hours=20.0)
# The tuning's candidates (#103, ADR 0011), fixed before the first run as the owner chose on
# 2026-10-03: a pull of 10, 20, 40 or 80 hours, a memory of half, one or two seasons of league
# game days, and an aging weight of 0, 0.5 or 1. Tuned on 5v5; the power play reuses them.
GRID = tuple(
    Settings(half_life_days=half_life, pull_hours=pull, aging=aging)
    for pull in (10.0, 20.0, 40.0, 80.0)
    for half_life in (90.0, 180.0, 360.0)
    for aging in (0.0, 0.5, 1.0)
)
# Every training season after the first with xG, each predicted by a model fitted on the earlier
# ones (ADR 0011).
TUNING_SEASONS = ts.TUNING_SEASONS


def steadiness(settings: Settings) -> tuple[float, float, float]:
    """The order among tied candidates, steadiest last: more pull, then a longer memory, then
    fuller aging."""
    return (settings.pull_hours, settings.half_life_days, settings.aging)


# Frozen by the tuning run rapm-20261003-3545379 (#103, ADR 0011): the rule's pull and memory,
# 80 hours and two seasons of league game days, without aging, as the owner chose on 2026-10-03.
# No candidate with full aging tied the leader. The power-play model reuses them.
TUNED = Settings(half_life_days=360.0, pull_hours=80.0, aging=0.0)


def expected_difference(
    ratings: pl.DataFrame, lineups: pl.DataFrame, games: pl.DataFrame
) -> pl.DataFrame:
    """The tuning's feature per game (#103): the home team's projected 5v5 expected goals less
    the away team's, from each candidate's 5v5 offense plus defense (xG per hour) times his
    expected 5v5 minutes (ADR 0018). Replacement skaters count as the reference skater, 0, and
    the league rate cancels between the two teams."""
    net = (
        ratings.filter(pl.col("component").is_in(["ev_off", "ev_def"]))
        .group_by("game_id", "player_id")
        .agg(net=pl.col("mean").sum())
    )
    teams = (
        lineups.filter(pl.col("role").is_in(SKATER_ROLES))
        .select("game_id", "team", "player_id", "exp_5v5")
        .join(net, on=["game_id", "player_id"], how="left")
        .group_by("game_id", "team")
        .agg(xg=(pl.col("exp_5v5") / 60 * pl.col("net").fill_null(0.0)).sum())
    )
    sides = games.select("game_id", "home", "away")
    return (
        sides.join(teams.rename({"team": "home", "xg": "home_xg"}), on=["game_id", "home"])
        .join(teams.rename({"team": "away", "xg": "away_xg"}), on=["game_id", "away"])
        .select("game_id", x=pl.col("home_xg") - pl.col("away_xg"))
        .sort("game_id")
    )


# The season and arena terms' pull: only enough to keep the fit solvable.
BIAS_PULL_HOURS = 1.0
# Added to every diagonal entry, so a column without a pull never makes the fit singular.
FLOOR = 1e-9
# The weighted sums are rebased before their growth factor passes 2 ** REBASE.
REBASE = 512.0
# The specification's figures read 2011-12 to 2017-18 (ADR 0019), as team strength's tuning did.
TRAIN_CUTOFF = ts.TUNED_CUTOFF

ROW_COLUMNS = (
    "game_id",
    "season",
    "game_date",
    "observed_utc",
    "hours",
    "y",
    "attackers",
    "defenders",
    "home",
    "score",
    "zone",
    "arena_id",
    "situation",
    "attack_d",
)


def _mirror(strength: str) -> str:
    home, away = strength.split("v")
    return f"{away}v{home}"


def model_rows(
    stints: pl.DataFrame,
    games: pl.DataFrame,
    roles: pl.DataFrame,
    venues: pl.DataFrame,
    model: str,
) -> pl.DataFrame:
    """The model's rows from kept stints with xG and both goalies in net, sorted by when they
    became public: one per team attacking at 5v5, one per team on the power play. roles is
    actual_lineups (game_id, player_id, role) and venues maps each venue to its arena_id."""
    if model not in MODELS:
        raise ValueError(f"unknown RAPM model {model}")
    kept = (
        stints.filter(
            pl.col("drop_reason").is_null(),
            pl.col("home_xg").is_not_null() & pl.col("away_xg").is_not_null(),
            pl.col("home_goalie").is_not_null() & pl.col("away_goalie").is_not_null(),
        )
        .join(games.select("game_id", "neutral_site", "venue"), on="game_id", how="left")
        .join(venues.select("venue", "arena_id"), on="venue", how="left")
        .with_columns(at_home=~pl.col("neutral_site").fill_null(False))
    )
    keep = ("game_id", "season", "game_date", "stint_id", "seconds", "observed_utc", "arena_id")
    flipped = pl.col("zone_start").replace_strict(FLIPPED_ZONE, default=None)
    against = -pl.col("score_state")

    def side(frame: pl.DataFrame, home: bool, situation: pl.Expr) -> pl.DataFrame:
        own, other = ("home", "away") if home else ("away", "home")
        return frame.select(
            *keep,
            side=pl.lit(0 if home else 1, pl.Int8),
            attackers=f"{own}_skaters",
            defenders=f"{other}_skaters",
            xg=f"{own}_xg",
            home=pl.col("at_home") if home else pl.lit(False),
            score=pl.col("score_state") if home else against,
            zone=pl.col("zone_start") if home else flipped,
            situation=situation,
        )

    if model == EV:
        even = kept.filter(pl.col("strength") == EV_STRENGTH)
        situation = pl.lit(EV_STRENGTH)
        sides = [side(even, True, situation), side(even, False, situation)]
    else:
        mirrors = {_mirror(s): s for s in PP_STRENGTHS}
        home_pp = kept.filter(pl.col("strength").is_in(PP_STRENGTHS))
        away_pp = kept.filter(pl.col("strength").is_in(list(mirrors)))
        sides = [
            side(home_pp, True, pl.col("strength")),
            side(away_pp, False, pl.col("strength").replace_strict(mirrors)),
        ]
    rows = (
        pl.concat(sides)
        .with_columns(
            hours=pl.col("seconds").cast(pl.Float64) / SECONDS_PER_HOUR,
            score=pl.col("score").cast(pl.Int8).clip(-SCORE_CAP, SCORE_CAP),
        )
        .with_columns(y=pl.col("xg") / pl.col("hours"))
        .sort("observed_utc", "game_date", "game_id", "stint_id", "side")
    )
    if model == PP:
        rows = rows.with_columns(attack_d=_defensemen(rows, "attackers", roles))
    else:
        rows = rows.with_columns(attack_d=pl.lit(0))
    return rows.with_columns(pl.col("attack_d").cast(pl.Int8)).select(ROW_COLUMNS)


def _defensemen(rows: pl.DataFrame, column: str, roles: pl.DataFrame) -> pl.Series:
    """How many of each row's skaters in column played defense in the game, by its boxscore."""
    defense = roles.filter(pl.col("role") == DEFENSE).select(
        "game_id", player=pl.col("player_id"), d=pl.lit(1, pl.Int32)
    )
    counts = (
        rows.with_row_index("row")
        .select("row", "game_id", player=pl.col(column))
        .explode("player", empty_as_null=False)
        .join(defense, on=["game_id", "player"], how="left")
        .group_by("row")
        .agg(pl.col("d").sum())
    )
    return (
        pl.DataFrame({"row": pl.arange(0, rows.height, eager=True).cast(pl.UInt32)})
        .join(counts, on="row", how="left")
        .sort("row")["d"]
        .fill_null(0)
    )


class Design:
    """A model's columns: its bias terms, then an attacking and a defending column per player, in
    the order players first appear. Players are added as their rows are built."""

    def __init__(self, model: str, seasons: Sequence[int], arenas: Sequence[str]) -> None:
        self.model = model
        terms = [INTERCEPT, "home"]
        terms += [f"score:{s:+d}" for s in SCORES]
        terms += [f"zone:{z}" for z in ZONES]
        terms += [f"season:{s}" for s in seasons]
        if model == EV:
            terms += [f"arena:{a}" for a in arenas]
        else:
            terms += [f"situation:{s}" for s in PP_STRENGTHS if s != BASE_SITUATION]
            terms.append(DEFENSEMEN)
        # The priors' trait columns (#102, ADR 0020): fitted only at a season's start, and fixed
        # at 0 in the daily fits, where the players' prior means carry them.
        self.components = COMPONENTS[model]
        self.trait_columns: dict[str, NDArray[np.int64]] = {}
        for component in self.components:
            names = priors.TRAITS[component]
            self.trait_columns[component] = np.arange(len(terms), len(terms) + len(names))
            terms += [f"{PRIOR}:{component}:{t}" for t in names]
        self.terms = tuple(terms)
        self.index = {term: i for i, term in enumerate(terms)}
        self.players: dict[int, int] = {}

    @property
    def size(self) -> int:
        return len(self.terms) + 2 * len(self.players)

    def attack(self, k: NDArray[np.int64]) -> NDArray[np.int64]:
        return len(self.terms) + 2 * k

    def defend(self, k: NDArray[np.int64]) -> NDArray[np.int64]:
        return len(self.terms) + 2 * k + 1

    def traits(self) -> NDArray[np.bool_]:
        """Which columns are the priors' trait columns."""
        mask = np.zeros(self.size, dtype=bool)
        for columns in self.trait_columns.values():
            mask[columns] = True
        return mask

    def penalty(self, settings: Settings) -> NDArray[np.float64]:
        """Each column's pull in hours: none for the intercept, home, score, zone, situation,
        defensemen and trait terms; BIAS_PULL_HOURS for season and arena; the setting's for
        players."""
        light = [
            BIAS_PULL_HOURS if t.startswith(("season:", "arena:")) else 0.0 for t in self.terms
        ]
        return np.concatenate([light, np.full(2 * len(self.players), settings.pull_hours)])

    def positions(self, ids: NDArray[np.int64], add: bool) -> NDArray[np.int64]:
        """Each player's position among the design's players, -1 for one it lacks; with add,
        the players it lacks join it first, in id order."""
        unique = np.unique(ids)
        if add:
            for player in unique.tolist():
                self.players.setdefault(int(player), len(self.players))
        lookup = np.array([self.players.get(int(p), -1) for p in unique], dtype=np.int64)
        return lookup[np.searchsorted(unique, ids)] if ids.size else ids.astype(np.int64)

    def matrix(self, rows: pl.DataFrame, traits: pl.DataFrame | None = None) -> sp.csr_matrix:
        """The rows' design matrix, its players added to the design. traits (priors.traits() of
        the rows' season) fill the trait columns: the attacking skaters' summed for the attacking
        component, the defending skaters' summed and negated for the defending one; a player
        without traits counts as the reference skater, 0."""
        n = rows.height
        parts: list[tuple[NDArray[np.int64], NDArray[np.int64], NDArray[np.float64]]] = []

        def put(mask: NDArray[np.bool_], column: int, values: NDArray[Any] | None = None) -> None:
            where = np.flatnonzero(mask)
            data = np.ones(where.size) if values is None else values[where].astype(np.float64)
            parts.append((where, np.full(where.size, column, dtype=np.int64), data))

        put(np.ones(n, dtype=bool), self.index[INTERCEPT])
        put(rows["home"].to_numpy(), self.index["home"])
        score = rows["score"].to_numpy()
        for s in SCORES:
            put(score == s, self.index[f"score:{s:+d}"])
        for z in ZONES:
            put((rows["zone"] == z).fill_null(False).to_numpy(), self.index[f"zone:{z}"])
        season = rows["season"].to_numpy()
        for s in np.unique(season).tolist():
            put(season == s, self._term(f"season:{s}"))
        if self.model == EV:
            arena = rows["arena_id"]
            for a in arena.drop_nulls().unique().sort().to_list():
                put((arena == a).fill_null(False).to_numpy(), self._term(f"arena:{a}"))
        else:
            situation = rows["situation"].to_numpy()
            for s in PP_STRENGTHS:
                if s != BASE_SITUATION:
                    put(situation == s, self.index[f"situation:{s}"])
            attack_d = rows["attack_d"].to_numpy()
            put(attack_d > 0, self.index[DEFENSEMEN], attack_d)
        lookup = _trait_lookup(traits)
        for column, sign, place, component in (
            ("attackers", 1.0, self.attack, self.components[0]),
            ("defenders", -1.0, self.defend, self.components[1]),
        ):
            lengths = rows[column].list.len().to_numpy()
            row = np.repeat(np.arange(n, dtype=np.int64), lengths)
            ids = rows[column].explode(empty_as_null=False).to_numpy().astype(np.int64)
            k = self.positions(ids, add=True)
            parts.append((row, place(k), np.full(row.size, sign)))
            if lookup is not None:
                names = priors.TRAITS[component]
                values = lookup(ids, names)
                sums = np.zeros((n, len(names)))
                np.add.at(sums, row, values)
                for j, trait_column in enumerate(self.trait_columns[component]):
                    put(sums[:, j] != 0, int(trait_column), sign * sums[:, j])
        r, c, v = (np.concatenate(p) for p in zip(*parts, strict=True))
        return sp.csr_matrix((v, (r, c)), shape=(n, self.size))

    def _term(self, term: str) -> int:
        if term not in self.index:
            raise ValueError(f"{self.model} RAPM has no column for {term}")
        return self.index[term]


def _trait_lookup(
    traits: pl.DataFrame | None,
) -> Callable[[NDArray[np.int64], Sequence[str]], NDArray[np.float64]] | None:
    """A function giving each player id's traits (0 for an id without them), or None."""
    if traits is None or traits.is_empty():
        return None
    order = np.argsort(traits["player_id"].to_numpy())
    known = traits["player_id"].to_numpy()[order]

    def lookup(ids: NDArray[np.int64], names: Sequence[str]) -> NDArray[np.float64]:
        table = traits.select(names).to_numpy()[order]
        at = np.clip(np.searchsorted(known, ids), 0, known.size - 1)
        found = known[at] == ids
        return np.where(found[:, None], table[at], 0.0)

    return lookup


@dataclass(frozen=True)
class Solution:
    """One refit: every column's estimate, posterior sd (NaN where not asked or without data),
    decayed hours, and the residual sd per square-root hour (None before any data). shifted_sd
    is each asked column's sd once the shift column is added to it, covariance included."""

    beta: NDArray[np.float64]
    sd: NDArray[np.float64]
    hours: NDArray[np.float64]
    sigma: float | None
    shifted_sd: NDArray[np.float64]


class Normal:
    """The decayed weighted sums of a model's rows: X'WX, X'Wy, y'Wy and the decayed row count.
    A row of league game day t enters with weight hours * 2 ** ((t - t0) / half_life), so older
    rows need no rescaling as days are added; a refit divides by the growth of the latest day."""

    def __init__(self, half_life_days: float) -> None:
        self.half_life = half_life_days
        self.a = np.zeros((0, 0))
        self.b = np.zeros(0)
        self.yy = 0.0
        self.n = 0.0
        self.t0: float | None = None
        self.latest: float | None = None

    def grow(self, size: int) -> None:
        old = self.b.size
        if size > old:
            a = np.zeros((size, size))
            a[:old, :old] = self.a
            self.a, self.b = a, np.concatenate([self.b, np.zeros(size - old)])

    def add(
        self,
        x: sp.csr_matrix,
        hours: NDArray[np.float64],
        y: NDArray[np.float64],
        days: NDArray[np.float64],
    ) -> None:
        if not hours.size:
            return
        width = cast(tuple[int, int], x.shape)[1]
        self.grow(width)
        if self.t0 is None:
            self.t0 = float(days.min())
        if (days.max() - self.t0) / self.half_life > REBASE:
            self._rebase(float(days.max()))
        growth = np.exp2((days - self.t0) / self.half_life)
        w = hours * growth
        xw = sp.diags(w) @ x
        product = (x.T @ xw).tocoo()
        product.sum_duplicates()
        self.a[product.row, product.col] += product.data
        self.b[:width] += x.T @ (w * y)
        self.yy += float(w @ (y * y))
        self.n += float(growth.sum())
        self.latest = float(days.max()) if self.latest is None else max(self.latest, days.max())

    def age(self, shift: NDArray[np.float64]) -> None:
        """Move every column's evidence by shift, as if each row's response had been y + X·shift:
        X'Wy gains X'WX·shift, and y'Wy gains 2·shift'X'Wy + shift'X'WX·shift."""
        size = shift.size
        self.grow(size)
        moved = self.a[:size, :size] @ shift
        self.yy += float(2 * shift @ self.b[:size] + shift @ moved)
        self.b[:size] += moved

    def _rebase(self, day: float) -> None:
        assert self.t0 is not None
        factor = np.exp2(-(day - self.t0) / self.half_life)
        self.a *= factor
        self.b *= factor
        self.yy *= factor
        self.n *= factor
        self.t0 = day

    def solve(
        self,
        penalty: NDArray[np.float64],
        wanted: NDArray[np.int64],
        reference: float | None = None,
        shift: int | None = None,
        prior: NDArray[np.float64] | None = None,
        exclude: NDArray[np.bool_] | None = None,
    ) -> Solution:
        """The ridge fit with the decay counted to the league day reference (by default the
        latest day added), the posterior sd of the wanted columns, and with shift, their sd once
        that column is added to each. Each column is pulled toward its prior mean (0 without
        prior); the excluded columns are held at 0. A column without data, or excluded, gets 0
        in beta."""
        size = penalty.size
        self.grow(size)
        nan = np.full(size, np.nan)
        if self.t0 is None or self.latest is None:
            nothing = np.zeros(size)
            return Solution(nothing, nan, nothing, None, nan.copy())
        reference = self.latest if reference is None else reference
        if reference < self.latest:
            raise ValueError("the decay's reference day is before the latest day added")
        factor = np.exp2(-(reference - self.t0) / self.half_life)
        hours = np.diag(self.a)[:size] * factor
        usable = hours > 0
        if exclude is not None:
            usable &= ~exclude[:size]
        active = np.flatnonzero(usable)
        asked = np.zeros(size, dtype=bool)
        asked[wanted] = True
        if shift is not None:
            asked[shift] = True
        # The asked columns last: the inverse's diagonal for them needs only the trailing block
        # of the Cholesky factor.
        order = np.concatenate([active[~asked[active]], active[asked[active]]])
        last = int(asked[active].sum())
        m = self.a[np.ix_(order, order)] * factor
        pull = penalty[order] + FLOOR
        m[np.diag_indices_from(m)] += pull
        data = self.b[order] * factor
        means = np.zeros(order.size) if prior is None else prior[order]
        chol, lower = sl.cho_factor(m, lower=True, overwrite_a=True, check_finite=False)
        fitted = sl.cho_solve((chol, lower), data + pull * means, check_finite=False)
        # y'Wy - 2 b'β + β'Aβ, with (A + P)β = b + Pμ.
        rss = self.yy * factor - fitted @ data + fitted @ (pull * (means - fitted))
        variance = max(rss, 0.0) / (self.n * factor)
        beta = np.zeros(size)
        beta[order] = fitted
        sd, shifted_sd = nan.copy(), nan.copy()
        if last:
            # With the asked columns last, the inverse's block for them is Z'Z, Z the inverse of
            # the factor's trailing block: a column's variance is its column of Z squared, and
            # the variance of a sum of columns is their sum's.
            tail = chol[-last:, -last:]
            z = sl.solve_triangular(tail, np.eye(last), lower=True, check_finite=False)
            columns = order[-last:]
            sd[columns] = np.sqrt(variance * (z * z).sum(axis=0))
            if shift is not None and asked[shift] and hours[shift] > 0:
                (k,) = np.flatnonzero(columns == shift)
                summed = z + z[:, [k]]
                shifted_sd[columns] = np.sqrt(variance * (summed * summed).sum(axis=0))
        return Solution(beta, sd, hours, float(np.sqrt(variance)), shifted_sd)


def league_days(games: pl.DataFrame) -> dict[date, int]:
    """Each league game date's number, counting only dates with regular-season games, so the
    summer does not decay a rating."""
    dates = sorted(games["game_date"].unique().to_list())
    return {day: i for i, day in enumerate(dates)}


def targets(lineups: pl.DataFrame, games: pl.DataFrame, seasons: Iterable[int]) -> pl.DataFrame:
    """The candidate skaters of each game of the seasons (lineups, ADR 0017) with the game's
    as-of time."""
    wanted = pl.Series(list(seasons), dtype=pl.Int32).implode()
    return (
        lineups.filter(pl.col("role").is_in(SKATER_ROLES), pl.col("season").is_in(wanted))
        .select("game_id", "season", "game_date", "team", "player_id", "role")
        .join(
            games.select("game_id", as_of_utc=ts.as_of(pl.col("game_date"), pl.col("start_utc"))),
            on="game_id",
            how="left",
        )
        .sort("as_of_utc", "game_id", "team", "player_id")
    )


@dataclass
class _Model:
    design: Design
    normal: Normal
    rows: pl.DataFrame
    x: sp.csr_matrix
    days: NDArray[np.float64]
    # The season's prior means per column, and its trait effects per component (#102).
    prior: NDArray[np.float64] | None = None
    effects: Mapping[str, Mapping[str, float]] = field(default_factory=dict)
    done: int = 0

    def add_until(self, known: datetime | None) -> None:
        """Add the season's rows public at or before known, the latest stint time read."""
        if known is None:
            return
        end = self.rows["observed_utc"].search_sorted(known, side="right")
        if end > self.done:
            part = slice(self.done, end)
            self.normal.add(
                self.x[part],
                self.rows["hours"].to_numpy()[part],
                self.rows["y"].to_numpy()[part],
                self.days[part],
            )
            self.done = end


def rate(
    stint_seasons: Iterable[pl.DataFrame],
    games: pl.DataFrame,
    roles: pl.DataFrame,
    venues: pl.DataFrame,
    wanted: pl.DataFrame,
    settings: Settings,
    version: str,
    players: pl.DataFrame | None = None,
    league_seasons: pl.DataFrame | None = None,
    kinds: Sequence[str] = MODELS,
    spread: bool = True,
) -> tuple[pl.DataFrame, pl.DataFrame, list[priors.PriorFit]]:
    """player_ratings and rapm_terms for the wanted candidates (targets()), and each season's
    priors (#102, ADR 0020). stint_seasons yields each season's stints in order, from
    FIRST_SEASON to the last wanted season at least: a season's ratings read every earlier
    season's. Without players every prior mean is 0; without league_seasons nobody has an
    NHLe. The tuning (#103) fits only the 5v5 model (kinds) and skips the posterior spreads
    (spread), leaving a rated player's sd null."""
    if wanted.is_empty():
        return (
            pl.DataFrame(schema=dtypes(PlayerRatings)),
            pl.DataFrame(schema=dtypes(RapmTerms)),
            [],
        )
    if wanted["as_of_utc"].is_null().any():
        raise ValueError("a candidate's game is not in games")
    last = int(wanted["season"].max())  # type: ignore[arg-type]
    seasons = sorted(s for s in games["season"].unique().to_list() if FIRST_SEASON <= s <= last)
    arenas = sorted(venues["arena_id"].unique().to_list())
    numbers = league_days(games)
    designs = {m: Design(m, seasons, arenas) for m in kinds}
    normals = {m: Normal(settings.half_life_days) for m in kinds}
    ratings: list[pl.DataFrame] = []
    terms: list[pl.DataFrame] = []
    fits: list[priors.PriorFit] = []
    season_ends: list[pl.DataFrame] = []
    known: datetime | None = None
    # The decay counts to the latest league game day read, whether or not a model kept a row
    # from it.
    reference: float | None = None
    seen: set[int] = set()
    for stints in stint_seasons:
        if stints.is_empty():
            continue
        (season,) = stints["season"].unique().to_list()
        if season < FIRST_SEASON or season > last:
            continue
        if seen and season <= max(seen):
            raise ValueError(f"stints of {season} after those of {max(seen)}")
        seen.add(season)
        cutoff = gs.season_cutoff(games, season)
        season_traits, factors = _season_traits(players, league_seasons, season, cutoff)
        models = {}
        for m in kinds:
            rows = model_rows(stints, games, roles, venues, m)
            days = np.array([numbers[d] for d in rows["game_date"].to_list()], dtype=np.float64)
            x = designs[m].matrix(rows, season_traits)
            models[m] = _Model(designs[m], normals[m], rows, x, days)
        # The season's priors, from the stints, lines and ratings public before it starts.
        if known is not None and known >= cutoff:
            raise ValueError(f"stints public at {known}, after {season} starts at {cutoff}")
        curves = priors.age_curves(_concat(season_ends), season)
        if settings.aging and curves and season_traits is not None:
            for model in models.values():
                model.normal.age(_aging_shift(model.design, season_traits, curves, settings.aging))
        effects: dict[str, dict[str, float]] = {}
        for model in models.values():
            effects |= _season_effects(model, settings, reference)
        means = None if season_traits is None else priors.prior_means(season_traits, effects)
        for model in models.values():
            model.effects = effects
            model.prior = _prior_vector(model.design, means)
        fits.append(priors.PriorFit(season, cutoff, factors, effects, curves))
        batches = (
            stints.group_by("observed_utc")
            .agg(pl.col("game_date").max())
            .sort("observed_utc")
            .with_columns(
                day=pl.col("game_date").replace_strict(numbers, return_dtype=pl.Float64).cum_max()
            )
        )
        times = batches["observed_utc"]
        season_wanted = wanted.filter(pl.col("season") == season).sort("as_of_utc")
        for (count,), group in season_wanted.with_columns(
            count=times.search_sorted(season_wanted["as_of_utc"], side="left")
        ).group_by("count", maintain_order=True):
            if count:
                known = times[int(count) - 1]
                reference = float(batches["day"][int(count) - 1])
            for model in models.values():
                model.add_until(known)
                rated, solution = _ratings(group, model, settings, known, reference, means, spread)
                ratings.append(rated)
                if known is not None and solution.sigma is not None:
                    terms.append(_terms(group, model, solution, known))
        for model in models.values():
            model.add_until(times[-1] if times.len() else None)
        if times.len():
            known, reference = times[-1], float(batches["day"][-1])
        season_ends.append(_season_end(models, settings, reference, season, season_traits))
    missing = set(wanted["season"].unique().to_list()) - seen
    if missing:
        raise ValueError(f"no stints for {sorted(missing)}")
    return (
        _stamp(pl.concat(ratings), PlayerRatings, settings, version),
        _stamp(pl.concat(terms), RapmTerms, settings, version)
        if terms
        else pl.DataFrame(schema=dtypes(RapmTerms)),
        fits,
    )


def _concat(frames: Sequence[pl.DataFrame]) -> pl.DataFrame:
    if frames:
        return pl.concat(frames)
    return pl.DataFrame(
        schema={
            "season": pl.Int32,
            "player_id": pl.Int64,
            "component": pl.String,
            "mean": pl.Float64,
            "hours": pl.Float64,
            "age": pl.Float64,
        }
    )


def _season_traits(
    players: pl.DataFrame | None,
    league_seasons: pl.DataFrame | None,
    season: int,
    cutoff: datetime,
) -> tuple[pl.DataFrame | None, pl.DataFrame]:
    """Every player's traits for the season and its NHLe factors, from the lines public before
    cutoff, the season's first as-of time."""
    factors = pl.DataFrame(schema={"league": pl.String, "moves": pl.UInt32, "factor": pl.Float64})
    nhle = pl.DataFrame(schema={"player_id": pl.Int64, "nhle_ppg": pl.Float64})
    if league_seasons is not None:
        lines = priors.league_lines(league_seasons, cutoff)
        factors = priors.nhle_factors(lines, season)
        nhle = priors.nhle_inputs(lines, factors, season)
    if players is None:
        return None, factors
    return priors.traits(players, nhle, season), factors


def _season_effects(
    model: _Model, settings: Settings, reference: float | None
) -> dict[str, dict[str, float]]:
    """The trait effects of the model's components, from one fit of the stints read so far with
    the trait columns in: the season-start fit. None before any data."""
    design = model.design
    solution = model.normal.solve(design.penalty(settings), np.empty(0, np.int64), reference)
    if solution.sigma is None:
        return {}
    return {
        component: {
            name: float(solution.beta[column])
            for name, column in zip(
                priors.TRAITS[component], design.trait_columns[component], strict=True
            )
        }
        for component in design.components
    }


def _aging_shift(
    design: Design,
    season_traits: pl.DataFrame,
    curves: Mapping[str, priors.AgeCurve],
    weight: float,
) -> NDArray[np.float64]:
    """Each player column's aging shift for the season: the weight times his component's age
    curve at his age that season; 0 for a component without a curve. A player without a birth
    date counts as 27, as in his prior (ADR 0020)."""
    shift = np.zeros(design.size)
    if not design.players:
        return shift
    ids = np.fromiter(design.players.keys(), dtype=np.int64)
    k = np.fromiter(design.players.values(), dtype=np.int64)
    lookup = _trait_lookup(season_traits)
    assert lookup is not None
    ages = lookup(ids, ["age"])[:, 0] + priors.REFERENCE_AGE
    for component, columns in zip(
        design.components, (design.attack(k), design.defend(k)), strict=True
    ):
        curve = curves.get(component)
        if curve is not None:
            shift[columns] = weight * np.array([curve.change(a) for a in ages])
    return shift


def _prior_vector(design: Design, means: pl.DataFrame | None) -> NDArray[np.float64]:
    """Each column's prior mean: a player's attacking and defending columns get his prior means
    for the model's two components; every other column 0."""
    vector = np.zeros(design.size)
    if means is None or not design.players:
        return vector
    ids = np.fromiter(design.players.keys(), dtype=np.int64)
    k = np.fromiter(design.players.values(), dtype=np.int64)
    lookup = _trait_lookup(means)
    assert lookup is not None
    values = lookup(ids, list(design.components))
    vector[design.attack(k)] = values[:, 0]
    vector[design.defend(k)] = values[:, 1]
    return vector


def _season_end(
    models: Mapping[str, _Model],
    settings: Settings,
    reference: float | None,
    season: int,
    season_traits: pl.DataFrame | None,
) -> pl.DataFrame:
    """Each player of the season's rows, his ratings and hours after its last game, with his age
    less 27: what the age curves read."""
    frames = []
    for model in models.values():
        design = model.design
        players = np.unique(
            np.concatenate(
                [
                    model.rows[column].explode(empty_as_null=False).to_numpy().astype(np.int64)
                    for column in ("attackers", "defenders")
                ]
            )
        )
        if not players.size:
            continue
        solution = model.normal.solve(
            design.penalty(settings),
            np.empty(0, np.int64),
            reference,
            prior=model.prior,
            exclude=design.traits(),
        )
        if solution.sigma is None:
            continue
        k = design.positions(players, add=False)
        for component, columns in zip(
            design.components, (design.attack(k), design.defend(k)), strict=True
        ):
            frames.append(
                pl.DataFrame(
                    {
                        "season": pl.Series([season] * players.size, dtype=pl.Int32),
                        "player_id": players,
                        "component": [component] * players.size,
                        "mean": solution.beta[columns],
                        "hours": solution.hours[columns],
                    }
                )
            )
    if not frames:
        return _concat([])
    ages = (
        season_traits.select("player_id", age=pl.col("age"))
        if season_traits is not None
        else pl.DataFrame(schema={"player_id": pl.Int64, "age": pl.Float64})
    )
    return pl.concat(frames).join(ages, on="player_id", how="left").fill_null(0.0)


def _ratings(
    group: pl.DataFrame,
    model: _Model,
    settings: Settings,
    known: datetime | None,
    reference: float | None,
    means: pl.DataFrame | None,
    spread: bool = True,
) -> tuple[pl.DataFrame, Solution]:
    """The group's candidates' two components of the model, from one refit pulled toward the
    season's prior means; with spread, their posterior sd too."""
    design = model.design
    ids = group["player_id"].to_numpy().astype(np.int64)
    k = design.positions(ids, add=False)
    has = k >= 0
    attack = np.where(has, design.attack(np.maximum(k, 0)), -1)
    defend = np.where(has, design.defend(np.maximum(k, 0)), -1)
    wanted = np.concatenate([attack[has], defend[has]]) if spread else np.empty(0, np.int64)
    shift = design.index[DEFENSEMEN] if design.model == PP else None
    solution = model.normal.solve(
        design.penalty(settings),
        wanted,
        reference,
        shift if spread else None,
        prior=model.prior,
        exclude=design.traits(),
    )
    lookup = _trait_lookup(means)
    priors_of = np.zeros((ids.size, 2)) if lookup is None else lookup(ids, list(design.components))
    prior_sd = np.nan if solution.sigma is None else solution.sigma / np.sqrt(settings.pull_hours)
    # A defenseman's power-play rating adds the defensemen term, his role's average there, and
    # its sd the term's variance and covariance with his own.
    defense = (group["role"] == DEFENSE).to_numpy()
    frames = []
    for j, (name, columns, shifted) in enumerate(
        zip(COMPONENTS[design.model], (attack, defend), (shift is not None, False), strict=True)
    ):
        rated = has & (solution.hours[columns] > 0)
        prior = priors_of[:, j]
        mean = np.where(rated, solution.beta[columns], prior)
        sd = np.where(rated, solution.sd[columns], prior_sd)
        if shifted and shift is not None and solution.hours[shift] > 0:
            own = defense & rated
            mean = mean + defense * solution.beta[shift]
            prior = prior + defense * solution.beta[shift]
            if spread:
                sd = np.where(own, solution.shifted_sd[columns], sd)
                sd = np.where(defense & ~rated, np.hypot(prior_sd, solution.sd[shift]), sd)
        hours = np.where(rated, solution.hours[columns], 0.0)
        frames.append(
            group.select(
                "game_id", "season", "game_date", "team", "player_id", "role", "as_of_utc"
            ).with_columns(
                component=pl.lit(name),
                mean=pl.Series(mean),
                prior=pl.Series(prior),
                sd=pl.Series(sd).fill_nan(None),
                hours=pl.Series(hours),
                known_utc=pl.lit(known, dtype=pl.Datetime("us", "UTC")),
            )
        )
    return pl.concat(frames), solution


def _terms(group: pl.DataFrame, model: _Model, solution: Solution, known: datetime) -> pl.DataFrame:
    """The fit's bias terms and residual sd, once per game date in the group."""
    assert solution.sigma is not None
    design = model.design
    traits = design.traits()
    rows = [
        {
            "term": term,
            "value": float(solution.beta[i]),
            "hours": None if term == DEFENSEMEN else float(solution.hours[i]),
        }
        for i, term in enumerate(design.terms)
        if solution.hours[i] > 0 and not traits[i]
    ]
    # The trait effects in force: the season-start fit's, held fixed in the daily fits.
    rows += [
        {"term": f"{PRIOR}:{component}:{name}", "value": value, "hours": None}
        for component in design.components
        for name, value in model.effects.get(component, {}).items()
    ]
    rows.append({"term": SIGMA, "value": solution.sigma, "hours": None})
    fit = pl.DataFrame(rows, schema={"term": pl.String, "value": pl.Float64, "hours": pl.Float64})
    days = group.group_by("season", "game_date").agg(as_of_utc=pl.col("as_of_utc").min())
    return days.join(fit, how="cross").with_columns(
        model=pl.lit(design.model), known_utc=pl.lit(known, dtype=pl.Datetime("us", "UTC"))
    )


def _stamp(frame: pl.DataFrame, schema: Any, settings: Settings, version: str) -> pl.DataFrame:
    cutoff = pl.lit(TRAIN_CUTOFF, dtype=pl.Datetime("us", "UTC"))
    # when/then rather than max_horizontal: a frame from a one-row cross join can hold as_of_utc
    # as a scalar column, which max_horizontal with a literal fails to broadcast.
    later = pl.when(pl.col("as_of_utc") > cutoff).then(pl.col("as_of_utc")).otherwise(cutoff)
    stamped = frame.with_columns(
        half_life_days=pl.lit(settings.half_life_days),
        pull_hours=pl.lit(settings.pull_hours),
        aging=pl.lit(settings.aging),
        train_cutoff=cutoff,
        artifact_version=pl.lit(version),
        observed_utc=later,
    )
    columns = dtypes(schema)
    return schema.validate(stamped.select(list(columns)).cast(columns))  # type: ignore[arg-type]
