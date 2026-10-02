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
read. The weighted sums behind the fit are kept from day to day, so each refit costs one solve.
Goalies are not columns: they enter B3 only through its goalie conversion (plan §5).

A game's ratings read only stints public before its as-of time (team strength's: 10:00 US Eastern
on the game date, or an hour before the start if that is earlier), which with ADR 0004 means every
game up to the day before.

Ratings are per hour of ice time, in xG. Each `sd` is the ridge's posterior spread: the residual
variance per hour of the decayed fit times the diagonal of the inverse of the penalized weighted
sums. `hours` is the decayed ice time behind the rating.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
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


@dataclass(frozen=True)
class Settings:
    """RAPM's two tuned settings (ADR 0011, #103): the memory, as a half-life in league game
    days, and the pull toward the prior mean, in hours of ice time."""

    half_life_days: float
    pull_hours: float

    @property
    def label(self) -> str:
        return f"half-life {self.half_life_days:g} game days, pull {self.pull_hours:g} hours"


# Provisional until the tuning task (#103), as the owner chose on 2026-10-02 (ADR 0019): one
# season of league game days, and a pull worth 20 hours, about a regular forward's 5v5 season.
PROVISIONAL = Settings(half_life_days=180.0, pull_hours=20.0)
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

    def penalty(self, settings: Settings) -> NDArray[np.float64]:
        """Each column's pull in hours: none for the intercept, home, score, zone, situation and
        defensemen terms; BIAS_PULL_HOURS for season and arena; the setting's for players."""
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

    def matrix(self, rows: pl.DataFrame) -> sp.csr_matrix:
        """The rows' design matrix, its players added to the design."""
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
        for column, sign, place in (
            ("attackers", 1.0, self.attack),
            ("defenders", -1.0, self.defend),
        ):
            lengths = rows[column].list.len().to_numpy()
            row = np.repeat(np.arange(n, dtype=np.int64), lengths)
            ids = rows[column].explode(empty_as_null=False).to_numpy().astype(np.int64)
            k = self.positions(ids, add=True)
            parts.append((row, place(k), np.full(row.size, sign)))
        r, c, v = (np.concatenate(p) for p in zip(*parts, strict=True))
        return sp.csr_matrix((v, (r, c)), shape=(n, self.size))

    def _term(self, term: str) -> int:
        if term not in self.index:
            raise ValueError(f"{self.model} RAPM has no column for {term}")
        return self.index[term]


@dataclass(frozen=True)
class Solution:
    """One refit: every column's estimate, posterior sd (NaN where not asked or without data),
    decayed hours, and the residual sd per square-root hour (None before any data)."""

    beta: NDArray[np.float64]
    sd: NDArray[np.float64]
    hours: NDArray[np.float64]
    sigma: float | None


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

    def _rebase(self, day: float) -> None:
        assert self.t0 is not None
        factor = np.exp2(-(day - self.t0) / self.half_life)
        self.a *= factor
        self.b *= factor
        self.yy *= factor
        self.n *= factor
        self.t0 = day

    def solve(self, penalty: NDArray[np.float64], wanted: NDArray[np.int64]) -> Solution:
        """The ridge fit as of the latest day added, with the posterior sd of the wanted columns.
        A column without data keeps its prior, 0."""
        size = penalty.size
        self.grow(size)
        if self.t0 is None or self.latest is None:
            nothing = np.zeros(size)
            return Solution(nothing, np.full(size, np.nan), nothing, None)
        factor = np.exp2(-(self.latest - self.t0) / self.half_life)
        hours = np.diag(self.a)[:size] * factor
        active = np.flatnonzero(hours > 0)
        asked = np.zeros(size, dtype=bool)
        asked[wanted] = True
        # The asked columns last: the inverse's diagonal for them needs only the trailing block
        # of the Cholesky factor.
        order = np.concatenate([active[~asked[active]], active[asked[active]]])
        last = int(asked[active].sum())
        m = self.a[np.ix_(order, order)] * factor
        pull = penalty[order] + FLOOR
        m[np.diag_indices_from(m)] += pull
        rhs = self.b[order] * factor
        chol, lower = sl.cho_factor(m, lower=True, overwrite_a=True, check_finite=False)
        fitted = sl.cho_solve((chol, lower), rhs, check_finite=False)
        # y'Wy - 2 b'β + β'Aβ, with (A + P)β = b.
        rss = self.yy * factor - fitted @ rhs - pull @ (fitted * fitted)
        variance = max(rss, 0.0) / (self.n * factor)
        beta = np.zeros(size)
        beta[order] = fitted
        sd = np.full(size, np.nan)
        if last:
            tail = chol[-last:, -last:]
            inverse = sl.solve_triangular(tail, np.eye(last), lower=True, check_finite=False)
            sd[order[-last:]] = np.sqrt(variance * (inverse * inverse).sum(axis=0))
        return Solution(beta, sd, hours, float(np.sqrt(variance)))


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
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """player_ratings and rapm_terms for the wanted candidates (targets()). stint_seasons yields
    each season's stints in order, from FIRST_SEASON to the last wanted season at least: a
    season's ratings read every earlier season's."""
    if wanted.is_empty():
        return pl.DataFrame(schema=dtypes(PlayerRatings)), pl.DataFrame(schema=dtypes(RapmTerms))
    if wanted["as_of_utc"].is_null().any():
        raise ValueError("a candidate's game is not in games")
    last = int(wanted["season"].max())  # type: ignore[arg-type]
    seasons = sorted(s for s in games["season"].unique().to_list() if FIRST_SEASON <= s <= last)
    arenas = sorted(venues["arena_id"].unique().to_list())
    numbers = league_days(games)
    designs = {m: Design(m, seasons, arenas) for m in MODELS}
    normals = {m: Normal(settings.half_life_days) for m in MODELS}
    ratings: list[pl.DataFrame] = []
    terms: list[pl.DataFrame] = []
    known: datetime | None = None
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
        models = {}
        for m in MODELS:
            rows = model_rows(stints, games, roles, venues, m)
            days = np.array([numbers[d] for d in rows["game_date"].to_list()], dtype=np.float64)
            models[m] = _Model(designs[m], normals[m], rows, designs[m].matrix(rows), days)
        times = stints["observed_utc"].unique().sort()
        season_wanted = wanted.filter(pl.col("season") == season).sort("as_of_utc")
        for (count,), group in season_wanted.with_columns(
            count=times.search_sorted(season_wanted["as_of_utc"], side="left")
        ).group_by("count", maintain_order=True):
            if count:
                known = times[int(count) - 1]
            for model in models.values():
                model.add_until(known)
                rated, solution = _ratings(group, model, settings, known)
                ratings.append(rated)
                if known is not None and solution.sigma is not None:
                    terms.append(_terms(group, model, solution, known))
        for model in models.values():
            model.add_until(times[-1] if times.len() else None)
        if times.len():
            known = times[-1]
    missing = set(wanted["season"].unique().to_list()) - seen
    if missing:
        raise ValueError(f"no stints for {sorted(missing)}")
    return (
        _stamp(pl.concat(ratings), PlayerRatings, settings, version),
        _stamp(pl.concat(terms), RapmTerms, settings, version)
        if terms
        else pl.DataFrame(schema=dtypes(RapmTerms)),
    )


def _ratings(
    group: pl.DataFrame, model: _Model, settings: Settings, known: datetime | None
) -> tuple[pl.DataFrame, Solution]:
    """The group's candidates' two components of the model, from one refit."""
    design = model.design
    k = design.positions(group["player_id"].to_numpy().astype(np.int64), add=False)
    has = k >= 0
    attack = np.where(has, design.attack(np.maximum(k, 0)), -1)
    defend = np.where(has, design.defend(np.maximum(k, 0)), -1)
    wanted = np.concatenate([attack[has], defend[has]])
    solution = model.normal.solve(design.penalty(settings), wanted)
    prior_sd = np.nan if solution.sigma is None else solution.sigma / np.sqrt(settings.pull_hours)
    # A defenseman's power-play rating adds the defensemen term, his role's average there.
    defense = (group["role"] == DEFENSE).to_numpy()
    shifts = (
        (defense * solution.beta[design.index[DEFENSEMEN]], 0.0)
        if design.model == PP
        else (0.0, 0.0)
    )
    frames = []
    for name, columns, shift in zip(
        COMPONENTS[design.model], (attack, defend), shifts, strict=True
    ):
        rated = has & (solution.hours[columns] > 0)
        mean = np.where(rated, solution.beta[columns], 0.0) + shift
        sd = np.where(rated, solution.sd[columns], prior_sd)
        hours = np.where(rated, solution.hours[columns], 0.0)
        frames.append(
            group.select(
                "game_id", "season", "game_date", "team", "player_id", "role", "as_of_utc"
            ).with_columns(
                component=pl.lit(name),
                mean=pl.Series(mean),
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
    rows = [
        {
            "term": term,
            "value": float(solution.beta[i]),
            "hours": None if term == DEFENSEMEN else float(solution.hours[i]),
        }
        for i, term in enumerate(design.terms)
        if solution.hours[i] > 0
    ]
    rows.append({"term": SIGMA, "value": solution.sigma, "hours": None})
    fit = pl.DataFrame(rows, schema={"term": pl.String, "value": pl.Float64, "hours": pl.Float64})
    days = group.group_by("season", "game_date").agg(as_of_utc=pl.col("as_of_utc").min())
    return days.join(fit, how="cross").with_columns(
        model=pl.lit(design.model), known_utc=pl.lit(known, dtype=pl.Datetime("us", "UTC"))
    )


def _stamp(frame: pl.DataFrame, schema: Any, settings: Settings, version: str) -> pl.DataFrame:
    cutoff = pl.lit(TRAIN_CUTOFF, dtype=pl.Datetime("us", "UTC"))
    stamped = frame.with_columns(
        half_life_days=pl.lit(settings.half_life_days),
        pull_hours=pl.lit(settings.pull_hours),
        train_cutoff=cutoff,
        artifact_version=pl.lit(version),
        observed_utc=pl.max_horizontal(pl.col("as_of_utc"), cutoff),
    )
    columns = dtypes(schema)
    return schema.validate(stamped.select(list(columns)).cast(columns))  # type: ignore[arg-type]
