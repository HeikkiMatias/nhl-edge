"""B3, the player layer (#106, ADR 0023, docs/plan.md §5):

    P(home win) = sigmoid(β0 + h_s + β1·Δĝ + β·ΔR + β·empty seats)

for the full game, overtime and shootout included (hard rule 2). Δĝ, the home team's expected
goals less the away team's from the projected lineups, takes the place of B2's ΔS and ΔG; h_s,
ΔR and the empty-seat share are B2's (ADR 0013).

**A team's expected goals,** team A against team B, from each game's rows:
- 5v5: (T / 60)·(μ5 + Σ_A w·o - Σ_B w·d), T the game's 5v5 minutes (the projected skater-minutes
  at 5v5 over five, averaged over the teams), μ5 RAPM's 5v5 rate for the season (the intercept
  plus the season term of the game date's fit), o and d each candidate's 5v5 offense and defense,
  and w = 5·exp_5v5 over the team's total exp_5v5; replacement skaters count in the minutes,
  rated 0;
- power play: (P_A / 60)·(μPP + Σ_A u·pp - Σ_B v·pk), P_A A's expected power-play minutes,
  u = 5·exp_pp over A's total and v = 4·exp_pk over B's total (5v4, the base);
- shorthanded: A's expected shorthanded xG;
- goals: their sum times A's goal multiplier against the opposing goalie, κ·φ_A·gamma
  (goal_multipliers), κ·φ_A when the goalie was not a candidate.

**The fit,** per fold, as B2's: a logistic regression with B2's frozen L2 penalty (ADR 0011) on
the standardized inputs, trained on games from 2011-12 whose results and boxscores were public
before the fold starts, with the projected skaters and the goalies who started them (ADR 0023).

**A prediction** averages the probability over every pair of candidate starters, weighted by
their start probabilities, as B2's. A game's own lineup and starters are never read (hard rule
9).
"""

from dataclasses import dataclass, replace
from datetime import datetime
from typing import cast

import numpy as np
import polars as pl
from numpy.typing import NDArray
from scipy.optimize import minimize
from scipy.special import expit

from nhl_edge import reference
from nhl_edge.features import team_strength as ts
from nhl_edge.game import b2
from nhl_edge.lineup.goalie_start import team_goalie_games
from nhl_edge.ratings import rapm

COMPONENT = "b3"
FIRST_SEASON = b2.FIRST_SEASON
INPUTS = ("delta_g_hat", *b2.SCHEDULE_INPUTS)
SKILL = INPUTS.index("delta_g_hat")
# B2's frozen L2 strength (ADR 0023).
TUNED = b2.TUNED
# The latest tuning cutoff behind B3's inputs: RAPM's memory and B2's settings (ADR 0011).
TUNED_CUTOFF = max(rapm.TRAIN_CUTOFF, b2.TUNED_CUTOFF)
# Skaters on the ice: at 5v5, on the power play and killing a 5v4 penalty.
ON_ICE = {"5v5": 5.0, "pp": 5.0, "pk": 4.0}
STATES = tuple(ON_ICE)
UTC = pl.Datetime("us", "UTC")
# A game counts toward, and gets, its arena's home edge only when at least half full (ADR 0035).
HALF_EMPTY = 0.5


@dataclass(frozen=True)
class Terms:
    """B3's optional terms for policy v2 (#225). The default adds none: it is the B3 that every
    policy v1 decision, replay and backtest reads, untouched."""

    arena: bool = False  # each arena's home edge beyond h_s (#223, ADR 0035)

    def label(self) -> str:
        """The model's name in a backtest: B3, or B3 with its terms, such as B3+arena."""
        return "+".join(["B3", *(name for name, on in vars(self).items() if on)])


V1 = Terms()


@dataclass(frozen=True)
class Tables:
    """The lake tables B3 reads."""

    games: pl.DataFrame
    schedule_terms: pl.DataFrame
    goalie_starts: pl.DataFrame
    actual_lineups: pl.DataFrame
    lineups: pl.DataFrame
    lineup_replacements: pl.DataFrame
    player_ratings: pl.DataFrame
    rapm_terms: pl.DataFrame
    expected_power_plays: pl.DataFrame
    goal_multipliers: pl.DataFrame
    # The pre-game schedule (ADR 0005), read only by Terms(arena=True) for each game's venue.
    schedule: pl.DataFrame | None = None


def league_rates(rapm_terms: pl.DataFrame) -> pl.DataFrame:
    """Per game date, RAPM's 5v5 and power-play rates for the season (mu_5v5, mu_pp): each
    model's intercept plus its season term, from the date's fit, and when it became known."""
    season = pl.concat_str(pl.lit("season:"), pl.col("season").cast(pl.String))
    wanted = rapm_terms.filter(
        pl.col("model").is_in(["ev", "pp"]),
        (pl.col("term") == "intercept") | (pl.col("term") == season),
    )
    # One fit per date and model: a second would be summed into the rate.
    repeated = wanted.group_by("game_date", "model", "term").len().filter(pl.col("len") > 1)
    if repeated.height:
        dates = ", ".join(str(d) for d in repeated["game_date"].unique().sort().head(3))
        raise ValueError(f"rapm_terms has more than one fit on a date, e.g. {dates}")
    return wanted.group_by("game_date").agg(
        mu_5v5=pl.col("value").filter(pl.col("model") == "ev").sum(),
        mu_pp=pl.col("value").filter(pl.col("model") == "pp").sum(),
        rates_utc=pl.col("observed_utc").max(),
    )


def team_goals(tables: Tables) -> pl.DataFrame:
    """One row per team-game with projected skaters: the team's expected 5v5, power-play and
    shorthanded xG before its goal multiplier (raw), its own 5v5 strength (strength_5v5), and
    when the rows behind it became known (observed_utc)."""
    ratings = tables.player_ratings.pivot(
        on="component", index=["game_id", "player_id"], values="mean"
    )
    rated_utc = tables.player_ratings.group_by("game_id").agg(
        ratings_utc=pl.col("observed_utc").max()
    )
    skaters = tables.lineups.filter(pl.col("role").is_in(["F", "D"])).join(
        ratings, on=["game_id", "player_id"], how="left"
    )
    minutes = {s: pl.col(f"exp_{s}").fill_null(0.0) for s in STATES}
    rating = {c: pl.col(c).fill_null(0.0) for c in ("ev_off", "ev_def", "pp", "pk")}
    candidates = skaters.group_by("game_id", "team").agg(
        **{f"total_{s}": minutes[s].sum() for s in STATES},
        off=(minutes["5v5"] * rating["ev_off"]).sum(),
        defense=(minutes["5v5"] * rating["ev_def"]).sum(),
        power=(minutes["pp"] * rating["pp"]).sum(),
        kill=(minutes["pk"] * rating["pk"]).sum(),
        lineups_utc=pl.col("observed_utc").max(),
    )
    spare = tables.lineup_replacements.group_by("game_id", "team").agg(
        **{f"spare_{s}": pl.col(f"exp_{s}").sum() for s in STATES},
        spare_utc=pl.col("observed_utc").max(),
    )
    # A team without candidates, such as an expansion team's first game, has replacements only.
    sums = [*(f"total_{s}" for s in STATES), "off", "defense", "power", "kill"]
    teams = (
        candidates.join(spare, on=["game_id", "team"], how="full", coalesce=True)
        .with_columns(
            *(pl.col(f"spare_{s}").fill_null(0.0) for s in STATES), pl.col(*sums).fill_null(0.0)
        )
        .with_columns(**{f"all_{s}": pl.col(f"total_{s}") + pl.col(f"spare_{s}") for s in STATES})
        .select(
            "game_id",
            "team",
            minutes_5v5=pl.col("all_5v5") / ON_ICE["5v5"],
            # Each side's on-ice sums: minutes-weighted ratings over its skaters' share of the ice.
            off=_on_ice(pl.col("off"), pl.col("all_5v5"), ON_ICE["5v5"]),
            defense=_on_ice(pl.col("defense"), pl.col("all_5v5"), ON_ICE["5v5"]),
            power=_on_ice(pl.col("power"), pl.col("all_pp"), ON_ICE["pp"]),
            kill=_on_ice(pl.col("kill"), pl.col("all_pk"), ON_ICE["pk"]),
            team_utc=b2_later(pl.col("lineups_utc"), pl.col("spare_utc")),
        )
    )
    power_plays = tables.expected_power_plays.select(
        "game_id",
        "team",
        "opponent",
        "is_home",
        "season",
        "game_date",
        "pp_minutes",
        sh_xg=pl.col("sh_xg").fill_null(0.0),
        power_plays_utc="observed_utc",
    )
    opposing = teams.select(
        "game_id",
        opponent="team",
        opp_minutes_5v5="minutes_5v5",
        opp_defense="defense",
        opp_kill="kill",
        opp_utc="team_utc",
    )
    frame = (
        power_plays.join(teams, on=["game_id", "team"], how="inner")
        .join(opposing, on=["game_id", "opponent"], how="inner")
        .join(league_rates(tables.rapm_terms), on="game_date", how="inner")
        .join(rated_utc, on="game_id", how="left")
    )
    five = (
        (pl.col("minutes_5v5") + pl.col("opp_minutes_5v5"))
        / 2
        / 60
        * (pl.col("mu_5v5") + pl.col("off") - pl.col("opp_defense"))
    )
    power = pl.col("pp_minutes") / 60 * (pl.col("mu_pp") + pl.col("power") - pl.col("opp_kill"))
    known = pl.col("power_plays_utc")
    for column in ("team_utc", "opp_utc", "rates_utc", "ratings_utc"):
        known = b2_later(known, pl.col(column))
    return frame.select(
        "game_id",
        "season",
        "game_date",
        "team",
        "opponent",
        "is_home",
        xg_5v5=five,
        xg_pp=power,
        xg_sh="sh_xg",
        raw=five + power + pl.col("sh_xg"),
        # The team's own 5v5 strength, its on-ice offense plus defense over its 5v5 minutes:
        # what its lineup adds to the goal difference, whoever the opponent.
        strength_5v5=pl.col("minutes_5v5") / 60 * (pl.col("off") + pl.col("defense")),
        observed_utc=known,
    ).sort("game_id", "team")


def _on_ice(weighted: pl.Expr, total: pl.Expr, skaters: float) -> pl.Expr:
    """Σ minutes·rating over the team's total minutes in the state, times the skaters on the
    ice: the time-averaged sum of the on-ice ratings, replacements rated 0."""
    return pl.when(total > 0).then(weighted * skaters / total).otherwise(0.0)


def b2_later(a: pl.Expr, b: pl.Expr) -> pl.Expr:
    """The later of two times, either of which may be null."""
    return pl.when(b.is_null() | (a >= b)).then(a).otherwise(b)


def multipliers(tables: Tables) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Each attacking team-game's base multiplier, κ·φ (league finishing taken as 1 before any
    is public), and each opposing candidate goalie's gamma, with when they became known."""
    rows = tables.goal_multipliers
    base = rows.group_by("game_id", "team").agg(
        base=(pl.col("league_finishing").fill_null(1.0) * pl.col("phi")).first(),
        multipliers_utc=pl.col("observed_utc").max(),
    )
    gammas = rows.filter(pl.col("goalie_id").is_not_null()).select(
        "game_id", "team", "goalie_id", "gamma"
    )
    return base, gammas


def game_inputs(tables: Tables) -> pl.DataFrame:
    """One row per game with both teams' raw expected goals (home_raw, away_raw), the schedule
    inputs, the h_s offset, its as-of time and when its rows became known."""
    raw = team_goals(tables)
    base, _ = multipliers(tables)
    sides = raw.join(base, on=["game_id", "team"], how="inner")
    home = sides.filter(pl.col("is_home")).select(
        "game_id",
        home_raw="raw",
        home_base="base",
        home_utc="observed_utc",
        home_m="multipliers_utc",
    )
    away = sides.filter(~pl.col("is_home")).select(
        "game_id",
        away_raw="raw",
        away_base="base",
        away_utc="observed_utc",
        away_m="multipliers_utc",
    )
    schedule = b2.schedule_inputs(tables.schedule_terms)
    known = pl.col("observed_utc")
    for column in ("home_utc", "away_utc", "home_m", "away_m"):
        known = b2_later(known, pl.col(column))
    return (
        schedule.join(home, on="game_id")
        .join(away, on="game_id")
        .with_columns(observed_utc=known)
        .drop("home_utc", "away_utc", "home_m", "away_m")
        .sort("game_id")
    )


def with_gamma(frame: pl.DataFrame, gammas: pl.DataFrame, side: str, goalie: str) -> pl.DataFrame:
    """frame with <side>_gamma, the gamma of the side's attack against the opposing goalie in
    the column goalie (1 when he has none)."""
    keyed = gammas.select("game_id", side_team="team", **{goalie: "goalie_id"}, gamma="gamma")
    return (
        frame.join(keyed.rename({"side_team": side}), on=["game_id", side, goalie], how="left")
        .rename({"gamma": f"{side}_gamma"})
        .with_columns(pl.col(f"{side}_gamma").fill_null(1.0))
    )


def delta() -> pl.Expr:
    """Δĝ: the home team's goals less the away team's, each with its gamma column."""
    home = pl.col("home_raw") * pl.col("home_base") * pl.col("home_gamma")
    away = pl.col("away_raw") * pl.col("away_base") * pl.col("away_gamma")
    return home - away


def starters_delta(tables: Tables, inputs: pl.DataFrame) -> pl.DataFrame:
    """Each game's Δĝ with the goalies who started it, for training only, and when its boxscore
    became public (lineup_utc). A starter who was not a candidate faces each side at gamma 1."""
    starters = team_goalie_games(tables.actual_lineups).select(
        "game_id", "team", goalie_id="starter", lineup_utc="observed_utc"
    )
    _, gammas = multipliers(tables)
    pairs = (
        inputs.select("game_id", "home", "away", "home_raw", "home_base", "away_raw", "away_base")
        .join(
            starters.select("game_id", away="team", away_goalie="goalie_id", away_utc="lineup_utc"),
            on=["game_id", "away"],
        )
        .join(
            starters.select("game_id", home="team", home_goalie="goalie_id", home_utc="lineup_utc"),
            on=["game_id", "home"],
        )
    )
    # The home attack faces the away starter, and the away attack the home starter.
    pairs = with_gamma(pairs, gammas, "home", "away_goalie")
    pairs = with_gamma(pairs, gammas, "away", "home_goalie")
    return pairs.select(
        "game_id",
        delta_g_hat=delta(),
        lineup_utc=b2_later(pl.col("home_utc"), pl.col("away_utc")),
    )


def scenarios(usable: pl.DataFrame, candidates: pl.DataFrame, gammas: pl.DataFrame) -> pl.DataFrame:
    """Every pair of candidate starters for each game of usable (game_id, home, away and the
    raw goals and bases): its weight, the product of their start probabilities, and its Δĝ. A
    team without candidates gets one average goalie (gamma 1, weight 1)."""
    frame = usable.select(
        "game_id", "home", "away", "home_raw", "home_base", "away_raw", "away_base"
    )
    for side in ("home", "away"):
        picks = candidates.select(
            "game_id", side=pl.col("team"), goalie=pl.col("goalie_id"), p=pl.col("p_start")
        ).rename({"side": side, "goalie": f"{side}_goalie", "p": f"{side}_p"})
        frame = frame.join(picks, on=["game_id", side], how="left").with_columns(
            pl.col(f"{side}_p").fill_null(1.0)
        )
    frame = with_gamma(frame, gammas, "home", "away_goalie")
    frame = with_gamma(frame, gammas, "away", "home_goalie")
    return frame.select(
        "game_id",
        weight=pl.col("home_p") * pl.col("away_p"),
        delta_g_hat=delta(),
    ).sort("game_id")


@dataclass(frozen=True)
class B3Model:
    """A fitted B3: its intercept and weights on the standardized inputs, the standardization,
    the games it read, and with Terms(arena=True) each arena's home edge (ADR 0035)."""

    season: int
    settings: b2.Settings
    intercept: float
    weights: tuple[float, ...]
    means: tuple[float, ...]
    scales: tuple[float, ...]
    games: int
    train_cutoff: datetime
    arenas: tuple[tuple[str, float], ...] = ()

    def predict(self, inputs: pl.DataFrame, pairs: pl.DataFrame) -> pl.DataFrame:
        """Each game in inputs (game_id, the schedule inputs, offset, and arena_id when the fit
        has arenas) with p_home, averaged over its goalie pairs (scenarios())."""
        means, scales, weights = (np.asarray(v) for v in (self.means, self.scales, self.weights))
        others = [name for name in INPUTS if name != "delta_g_hat"]
        index = [INPUTS.index(name) for name in others]
        z = (inputs.select(others).to_numpy() - means[index]) / scales[index]
        base = self.intercept + inputs["offset"].to_numpy() + z @ weights[index]
        if self.arenas:
            base = base + arena_offsets(inputs, self.arenas)
        frame = inputs.select("game_id").with_columns(base=pl.Series(base))
        joined = pairs.join(frame, on="game_id")
        skill = (joined["delta_g_hat"].to_numpy() - means[SKILL]) / scales[SKILL]
        p = expit(joined["base"].to_numpy() + weights[SKILL] * skill)
        mixed = (
            joined.with_columns(p=pl.Series(p) * pl.col("weight"))
            .group_by("game_id")
            .agg(p_home=pl.col("p").sum() / pl.col("weight").sum())
        )
        return inputs.select("game_id").join(mixed, on="game_id")


def fit(train: pl.DataFrame, settings: b2.Settings, season: int) -> B3Model:
    """B3 fitted on train: one row per game with INPUTS (delta_g_hat from its starters), offset,
    home_win and known_utc. train_cutoff is the latest known_utc."""
    if train.height == 0 or train["home_win"].n_unique() < 2:
        raise ValueError(f"no earlier games to fit B3 on for {season}")
    raw = train.select(INPUTS).to_numpy().astype(float)
    means = raw.mean(axis=0)
    scales = raw.std(axis=0)
    scales[scales == 0] = 1.0
    x = (raw - means) / scales
    y = train["home_win"].cast(pl.Float64).to_numpy()
    loss = b2.objective(x, y, train["offset"].to_numpy().astype(float), settings.l2)
    result = minimize(loss, np.zeros(len(INPUTS) + 1), jac=True, method="L-BFGS-B")
    if not result.success:
        raise ValueError(f"B3's fit for {season} did not converge: {result.message}")
    beta = cast(NDArray[np.float64], result.x)
    cutoff = train["known_utc"].max()
    assert isinstance(cutoff, datetime)
    return B3Model(
        season=season,
        settings=settings,
        intercept=float(beta[0]),
        weights=tuple(float(b) for b in beta[1:]),
        means=tuple(float(m) for m in means),
        scales=tuple(float(s) for s in scales),
        games=train.height,
        train_cutoff=cutoff,
    )


def fitted(model: B3Model, frame: pl.DataFrame) -> NDArray[np.float64]:
    """The model's chance for each row of frame (INPUTS, offset), as fitted: Δĝ from the row's
    own starters, with no goalie mixing."""
    means, scales, weights = (np.asarray(v) for v in (model.means, model.scales, model.weights))
    z = (frame.select(INPUTS).to_numpy().astype(float) - means) / scales
    return cast(
        NDArray[np.float64], expit(model.intercept + frame["offset"].to_numpy() + z @ weights)
    )


def counted() -> pl.Expr:
    """The games an arena's home edge reads and applies to (ADR 0035): at a known arena, not a
    neutral site (arena_id is null there), and at least half full."""
    return pl.col("arena_id").is_not_null() & (pl.col("empty_seats") <= HALF_EMPTY)


def arena_shifts(train: pl.DataFrame, model: B3Model) -> tuple[tuple[str, float], ...]:
    """Each arena's home edge beyond h_s, in log-odds (#223, ADR 0035), from the fold's training
    games (train, with arena_id and home_win) and the model fitted on them: per arena, g = Σ(win -
    p) and h = Σ p(1 - p) over its counted games; τ², how much arenas truly differ, is max(0, (Σ
    g²/h - k) / Σ h) over its k arenas; and the shift g·τ² / (τ²·h + 1) pulls each raw edge g/h
    toward 0 by τ² / (τ² + 1/h). Nothing is tuned: τ² is the fold's own."""
    games = train.with_columns(p=pl.Series(fitted(model, train))).filter(counted())
    per = games.group_by("arena_id").agg(
        g=(pl.col("home_win") - pl.col("p")).sum(), h=(pl.col("p") * (1 - pl.col("p"))).sum()
    )
    if per.is_empty():
        return ()
    g, h = per["g"].to_numpy(), per["h"].to_numpy()
    tau2 = max(0.0, float(((g**2 / h).sum() - per.height) / h.sum()))
    shifts = g * tau2 / (tau2 * h + 1)
    return tuple(sorted(zip(per["arena_id"].to_list(), (float(x) for x in shifts), strict=True)))


def with_arena(tables: Tables, inputs: pl.DataFrame) -> pl.DataFrame:
    """inputs with each game's arena_id (ADR 0035): its venue's arena in the pre-game schedule
    (ADR 0005), null at a neutral site, and its observed_utc the later of its own and the schedule
    row's. A game without a schedule row is refused."""
    if tables.schedule is None:
        raise ValueError("Terms(arena=True) needs the schedule in B3's tables")
    # A game without its schedule row would leave the arena model fitting and scoring on fewer
    # games than B3, and bias the paired comparison: refuse instead (Codex on #226).
    missing = inputs.join(tables.schedule.select("game_id"), on="game_id", how="anti")
    if missing.height:
        raise ValueError(
            f"{missing.height} games lack a schedule row for the arena term, such as "
            f"{missing['game_id'][0]}: ingest their schedule first"
        )
    arenas = (
        tables.schedule.select("game_id", "venue", "neutral_site", a_utc="observed_utc")
        .join(reference.load_venues(), on="venue", how="left")
        .select(
            "game_id", "a_utc", arena_id=pl.when(~pl.col("neutral_site")).then(pl.col("arena_id"))
        )
    )
    return (
        inputs.join(arenas, on="game_id")
        .with_columns(observed_utc=b2_later(pl.col("observed_utc"), pl.col("a_utc")))
        .drop("a_utc")
    )


def arena_offsets(
    inputs: pl.DataFrame, arenas: tuple[tuple[str, float], ...]
) -> NDArray[np.float64]:
    """Each row's arena shift (ADR 0035): its arena's for a counted game, else 0."""
    table = pl.DataFrame(
        {"arena_id": [a for a, _ in arenas], "shift": [x for _, x in arenas]},
        schema={"arena_id": pl.String, "shift": pl.Float64},
    )
    rows = inputs.select("arena_id", "empty_seats").with_row_index("row")
    joined = rows.join(table, on="arena_id", how="left").sort("row")
    shift = pl.when(counted()).then(pl.col("shift")).otherwise(0.0).fill_null(0.0)
    return joined.select(shift=shift)["shift"].to_numpy()


def training_games(
    tables: Tables, inputs: pl.DataFrame, season: int, start: datetime, known: str
) -> pl.DataFrame:
    """The games B3 for season may train on: from FIRST_SEASON, of earlier seasons, with results
    and boxscores public before start, and inputs known before it by the column known."""
    results = tables.games.select(
        "game_id",
        home_win=(pl.col("home_score") > pl.col("away_score")).cast(pl.Int8),
        result_utc="observed_utc",
    )
    usable = inputs.filter(
        pl.col("season").is_between(FIRST_SEASON, season - 1), pl.col(known) < start
    )
    return (
        usable.join(results, on="game_id")
        .join(starters_delta(tables, usable), on="game_id")
        .filter(pl.col("result_utc") < start, pl.col("lineup_utc") < start)
        .with_columns(
            known_utc=b2_later(b2_later(pl.col("result_utc"), pl.col("lineup_utc")), pl.col(known))
        )
        .sort("game_id")
    )


CUTOFF_TABLES = (
    "schedule_terms",
    "goalie_starts",
    "lineups",
    "lineup_replacements",
    "player_ratings",
    "rapm_terms",
    "expected_power_plays",
    "goal_multipliers",
)


def through(tables: Tables, season: int) -> Tables:
    """tables with the rows of seasons up to season only: all that the season's fold reads."""
    kept = {
        name: frame.filter(pl.col("season") <= season)
        for name, frame in vars(tables).items()
        if frame is not None and "season" in frame.columns
    }
    return replace(tables, **kept)


def tuning_cutoff(tables: Tables, season: int) -> datetime:
    """The latest tuning cutoff behind B3 for the season's fold: RAPM's and B2's, and the latest
    train_cutoff of each table's rows of the seasons the fold reads, up to its own. Tables refit
    each season (expected power plays, finishing, the projection) carry later seasons' cutoffs
    that this fold never reads."""
    cutoffs = [TUNED_CUTOFF]
    for name in CUTOFF_TABLES:
        table = getattr(tables, name)
        if "train_cutoff" not in table.columns:
            continue
        if "season" in table.columns:
            table = table.filter(pl.col("season") <= season)
        latest = table["train_cutoff"].max()
        if latest is not None:
            assert isinstance(latest, datetime)
            cutoffs.append(latest)
    return max(cutoffs)


def predictions(
    tables: Tables,
    moments: pl.DataFrame,
    season: int,
    start: datetime,
    settings: b2.Settings = TUNED,
    terms: Terms = V1,
) -> tuple[pl.DataFrame, B3Model]:
    """B3's p_home for the season's games in moments (game_id, prediction_utc), from a fit on the
    games before start, with its train_cutoff, and the fit. Only rows of seasons up to the
    season are read. A fold starting before the tuning cutoff is refused: its inputs were tuned on
    its own season's results (ADR 0011). Every row is read only once known (observed_utc), and
    train_cutoff covers the fit and every table's cutoff."""
    tables = through(tables, season)
    cutoff = tuning_cutoff(tables, season)
    if start <= cutoff:
        raise ValueError(
            f"{season}'s fold starts at {start:%Y-%m-%d}, before the tuning cutoff "
            f"{cutoff:%Y-%m-%d}: B3's inputs are in-sample (ADR 0011)"
        )
    inputs = game_inputs(tables)
    if terms.arena:
        inputs = with_arena(tables, inputs)
    train = training_games(tables, inputs, season, start, "observed_utc")
    model = fit(train, settings, season)
    if terms.arena:
        model = replace(model, arenas=arena_shifts(train, model))
    model = replace(model, train_cutoff=max(model.train_cutoff, cutoff))
    pool = tables.goalie_starts.select("game_id", "team", "goalie_id", "p_start", "observed_utc")
    usable, ready = b2.known_before(
        inputs.filter(pl.col("season") == season), pool, moments, "observed_utc"
    )
    _, gammas = multipliers(tables)
    pairs = scenarios(usable, ready, gammas)
    predicted = model.predict(usable, pairs).with_columns(
        train_cutoff=pl.lit(model.train_cutoff, dtype=UTC)
    )
    return predicted, model


TABLES = (
    "schedule_terms",
    "goalie_starts",
    "lineups",
    "lineup_replacements",
    "player_ratings",
    "rapm_terms",
    "expected_power_plays",
    "goal_multipliers",
)


# The tables with rows for each team of a game: every team, or every team with earlier games
# (a candidate needs one), and their components each candidate is rated on.
EVERY_TEAM = ("lineup_replacements", "expected_power_plays", "goal_multipliers")
TEAMS_WITH_HISTORY = ("lineups", "goalie_starts")
COMPONENTS = ("ev_off", "ev_def", "pp", "pk")


def input_problems(tables: Tables, last: int) -> list[str]:
    """Why the lake cannot run B3 up to the season last: a game of FIRST_SEASON on missing from
    one of its tables, or from a team's rows of a per-team table, would drop out of the fits or
    lose a side unnoticed, as would a candidate without his ratings. A team's first game, without
    earlier games, has no candidates, and the games of RAPM's first day have no league rate, by
    construction."""
    needed = tables.games.filter(pl.col("season").is_between(FIRST_SEASON, last))
    # RAPM's first fit needs stints public before it, and a day's stints are public only the
    # morning after: the games of its first season's first day have no league rate, and no B3
    # inputs. Every later date has a fit.
    opening = tables.games.filter(pl.col("season") == rapm.FIRST_SEASON)["game_date"].min()
    lines = ts.team_lines()

    def sides(games: pl.DataFrame) -> pl.DataFrame:
        return pl.concat(
            [
                games.select("game_id", "season", "start_utc", team=pl.col(s))
                for s in ("home", "away")
            ]
        ).with_columns(line=pl.col("team").replace(lines))

    team_games = sides(needed)
    # A line's first game: nobody has dressed for it before (an expansion team's first game).
    firsts = (
        sides(tables.games)
        .sort("start_utc", "game_id")
        .group_by("line")
        .agg(pl.col("game_id").first())
    )
    with_history = team_games.join(firsts, on=["line", "game_id"], how="anti")
    problems = []

    def report(missing: pl.DataFrame, what: str) -> None:
        games = missing.select("game_id", "season").unique().sort("game_id")
        for (season,), frame in games.group_by("season", maintain_order=True):
            examples = ", ".join(str(g) for g in frame["game_id"].head(3).to_list())
            problems.append(f"{season}: {frame.height:,} games without {what}, e.g. {examples}")

    for name in TABLES:
        table = getattr(tables, name)
        if name == "rapm_terms":
            wanted = needed if opening is None else needed.filter(pl.col("game_date") != opening)
            keys = ["game_date"]
        elif name in EVERY_TEAM:
            wanted, keys = team_games, ["game_id", "team"]
        elif name in TEAMS_WITH_HISTORY:
            wanted, keys = with_history, ["game_id", "team"]
        else:
            wanted, keys = needed, ["game_id"]
        report(wanted.join(table.select(keys).unique(), on=keys, how="anti"), name)
    # Every skater candidate is rated on each component.
    rated = tables.player_ratings.group_by("game_id", "player_id").agg(
        pl.col("component").is_in(list(COMPONENTS)).sum().alias("components")
    )
    unrated = (
        tables.lineups.filter(pl.col("role").is_in(["F", "D"]))
        .join(needed.select("game_id", "season"), on="game_id")
        .join(rated, on=["game_id", "player_id"], how="left")
        .filter(pl.col("components").fill_null(0) < len(COMPONENTS))
    )
    report(unrated, "every candidate's player_ratings")
    return sorted(problems)
