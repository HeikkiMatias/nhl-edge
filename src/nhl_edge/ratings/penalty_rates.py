"""Penalty rates and expected power plays (#104, ADR 0021, docs/plan.md §5): each skater's
penalties taken and drawn per hour, and each team's expected power plays, power-play minutes and
shorthanded xG from both projected lineups.

**Counted penalties.** Penalties of 2, 4 or 5 minutes: minors, double minors, bench minors,
majors and match penalties. At each moment of a game (period and second) and each length, a
team's n penalties face the other team's m; each of the n weighs (n - min(n, m)) / n, so an
offset penalty, which gives no power play, counts 0. A penalty is taken by its committer and
drawn by its drawer when that player is a skater in the game's boxscore; penalties without one
count only in the league's level.

**A skater's rates.** From his earlier games with stints on any team, public before team
strength's as-of time, each weighing 0.5 ** (d / half-life), d league game days back from the
latest date read, with RAPM's frozen half-life (#103). Exposure is his minutes at 5v5, on the
power play and on the penalty kill, the states projected minutes cover (ADR 0018). His rate is
r = (C + c·rho) / (H + c): C his weighted count, H his weighted hours, rho his role's rate over all
skaters of the role with the same weights, and c the pull in hours, measured per role and
component on the season before (season_pulls). A candidate without games is at rho.

**Expected power plays.** A lineup's index is Σ e·r / Σ e·rho over its projected skaters, e
the expected minutes (exp_5v5 + exp_pp + exp_pk, with the probability of dressing in) and the
replacement skaters at rho. Team A's expected power plays against team B are
L * (I_taken(B) + I_drawn(A)) / 2, L the league's unoffset penalties per team-game in the games
public before the as-of time, in the game's season and the one before (team strength's league
window). Its power-play minutes are those times length, the league's power-play minutes per unoffset
penalty; its penalty-kill minutes are B's power-play minutes; and its shorthanded xG is s times
them, s the league's xG per penalty-kill minute for the team short-handed, its goalie in.

Nothing is tuned: the memory is RAPM's frozen one and the pulls, rates and league figures are
measured on earlier data.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

import numpy as np
import polars as pl

from nhl_edge.features import team_strength as ts
from nhl_edge.lake.schemas import ExpectedPowerPlays, PenaltyRates, dtypes
from nhl_edge.lineup.goalie_start import season_cutoff
from nhl_edge.lineup.minutes import STATES
from nhl_edge.lineup.projection import SKATER_ROLES
from nhl_edge.ratings import rapm

COMPONENT = "power-plays"
TAKEN, DRAWN = "pen_taken", "pen_drawn"
COMPONENTS = (TAKEN, DRAWN)
# The column each component counts in a player-game row.
COUNTS = {TAKEN: "taken", DRAWN: "drawn"}
# Penalty lengths that can leave a team short-handed: misconducts (10) and penalty shots (0)
# cannot.
COUNTED_MINUTES = (2, 4, 5)
HALF_LIFE_DAYS = rapm.TUNED.half_life_days
# Players with fewer games with stints in the season before don't enter the pull.
PULL_MIN_GAMES = 20
# The memory was tuned on the training seasons (#103, ADR 0011).
TRAIN_CUTOFF = rapm.TRAIN_CUTOFF
MOMENT = ("game_id", "period", "seconds", "duration_min")


def unoffset(penalties: pl.DataFrame) -> pl.DataFrame:
    """The counted penalties (Penalties rows) with weight, the share of each that leaves its team
    short-handed rather than offset by the other team's of the same length at the same moment."""
    counted = penalties.filter(pl.col("duration_min").is_in(COUNTED_MINUTES))
    sides = counted.group_by(*MOMENT, "is_home").agg(n=pl.len())
    other = sides.select(*MOMENT, is_home=~pl.col("is_home"), m=pl.col("n"))
    weights = sides.join(other, on=[*MOMENT, "is_home"], how="left").select(
        *MOMENT,
        "is_home",
        weight=(pl.col("n") - pl.min_horizontal("n", pl.col("m").fill_null(0))) / pl.col("n"),
    )
    return counted.join(weights, on=[*MOMENT, "is_home"]).select(
        "game_id",
        "season",
        "game_date",
        "team",
        "is_home",
        "committed_by",
        "drawn_by",
        pl.col("weight").cast(pl.Float64),
        "observed_utc",
    )


def player_games(
    minutes: pl.DataFrame, weighted: pl.DataFrame, games: pl.DataFrame
) -> pl.DataFrame:
    """One row per skater and game with stints (minutes, lineup.minutes.player_minutes): his
    role, hours at 5v5, on the power play and on the penalty kill, his weighted penalties taken
    and drawn (unoffset), the game's league day, and when both the stints and the penalties were
    public."""
    numbers = rapm.league_days(games)
    taken = (
        weighted.filter(pl.col("committed_by").is_not_null())
        .group_by("game_id", "team", player_id="committed_by")
        .agg(taken=pl.col("weight").sum())
    )
    drawn = (
        weighted.filter(pl.col("drawn_by").is_not_null())
        .group_by("game_id", pl.col("team").alias("penalized"), player_id="drawn_by")
        .agg(drawn=pl.col("weight").sum())
    )
    published = weighted.group_by("game_id").agg(penalties_utc=pl.col("observed_utc").max())
    rows = (
        minutes.join(taken, on=["game_id", "team", "player_id"], how="left")
        .join(drawn, on=["game_id", "player_id"], how="left")
        # A drawer is a skater of the other team.
        .with_columns(drawn=pl.when(pl.col("penalized") != pl.col("team")).then(pl.col("drawn")))
        .group_by("game_id", "player_id")
        .agg(
            pl.col("season", "game_date", "team", "role", *STATES, "observed_utc").first(),
            pl.col("taken").first(),
            pl.col("drawn").sum(),
        )
        .join(published, on="game_id", how="left")
    )
    return rows.select(
        "game_id",
        "season",
        "game_date",
        "team",
        "player_id",
        "role",
        hours=pl.sum_horizontal(*STATES) / 60,
        taken=pl.col("taken").fill_null(0.0),
        drawn=pl.col("drawn").fill_null(0.0),
        day=pl.col("game_date").replace_strict(numbers, return_dtype=pl.Float64),
        observed_utc=pl.max_horizontal("observed_utc", "penalties_utc"),
    ).sort("observed_utc", "game_date", "game_id", "player_id")


@dataclass(frozen=True)
class SeasonPulls:
    """A season's pulls in hours, per role and component, measured on the season before
    (source); cutoff is when the last of its games was public."""

    season: int
    source: int
    pull: dict[tuple[str, str], float]
    cutoff: datetime


def season_pulls(rows: pl.DataFrame, season: int, games: pl.DataFrame) -> SeasonPulls:
    """The season's pulls from the season before's player_games. Over its skaters of a role with
    PULL_MIN_GAMES games or more, μ = ΣC / ΣH and v = (Σ H·(C/H - μ)² - n·μ) / ΣH, the spread
    of true rates beyond Poisson noise; the pull is μ / v, or infinite when v is 0 or less, which
    puts every player at his role's rate. Refuses when a game of the season before was public
    only at or after the season's first as-of time."""
    source = season - 10_001
    played = rows.filter(pl.col("season") == source)
    if played.is_empty():
        raise ValueError(f"no games with stints in {source} to measure {season}'s pulls")
    # Running sums in a fixed order rather than a group sum, which threads may add up in any
    # order: a pull, a small difference of large sums, must not move in its last digits from
    # one run to the next or with rows it never reads.
    group = ["role", "player_id"]
    players = (
        played.sort(*group, "observed_utc", "game_id")
        .select(
            *group,
            pl.col("hours", "taken", "drawn").cum_sum().over(group),
            games=pl.int_range(1, pl.len() + 1).over(group),
        )
        .group_by(group, maintain_order=True)
        .last()
        .filter(pl.col("games") >= PULL_MIN_GAMES, pl.col("hours") > 0)
    )
    pull = {}
    for role in SKATER_ROLES:
        own = players.filter(pl.col("role") == role)
        if own.height < 2:
            raise ValueError(f"too few {role} with {PULL_MIN_GAMES} games in {source}")
        hours = own["hours"].to_numpy()
        for component, column in COUNTS.items():
            counts = own[column].to_numpy()
            mu = counts.sum() / hours.sum()
            spread = (np.sum(hours * (counts / hours - mu) ** 2) - len(hours) * mu) / hours.sum()
            pull[role, component] = float(mu / spread) if spread > 0 and mu > 0 else np.inf
    cutoff = played["observed_utc"].max()
    assert isinstance(cutoff, datetime)
    first = season_cutoff(games, season)
    if cutoff >= first:
        raise ValueError(
            f"{source}'s games were public at {cutoff}, not before {season}'s first as-of {first}"
        )
    return SeasonPulls(season, source, pull, cutoff)


def _grow(day: pl.Expr) -> pl.Expr:
    """A row's weight times 2 ** (latest day / half-life): sums of these, scaled back by the
    latest day read, are the decayed sums. Fifteen seasons of league days keep it under 2 ** 8."""
    return pl.lit(2.0).pow(day / HALF_LIFE_DAYS)


def _batches(rows: pl.DataFrame) -> pl.DataFrame:
    """Per publication time, the latest league day read up to then."""
    return (
        rows.group_by("observed_utc")
        .agg(pl.col("day").max())
        .sort("observed_utc")
        .select("observed_utc", reference=pl.col("day").cum_max(), known_utc="observed_utc")
    )


def _player_history(rows: pl.DataFrame) -> pl.DataFrame:
    """Each player's grown sums after each of his games, in publication order."""
    grow = _grow(pl.col("day"))
    by = pl.col("player_id")
    return rows.select(
        "player_id",
        "observed_utc",
        g_hours=(pl.col("hours") * grow).cum_sum().over(by),
        **{f"g_{c}": (pl.col(COUNTS[c]) * grow).cum_sum().over(by) for c in COMPONENTS},
    )


def _role_history(rows: pl.DataFrame) -> pl.DataFrame:
    """Each role's grown sums over all its skaters after each publication time."""
    grow = _grow(pl.col("day"))
    per_batch = (
        rows.group_by("role", "observed_utc")
        .agg(
            r_hours=(pl.col("hours") * grow).sum(),
            **{f"r_{c}": (pl.col(COUNTS[c]) * grow).sum() for c in COMPONENTS},
        )
        .sort("observed_utc")
    )
    sums = ["r_hours", *(f"r_{c}" for c in COMPONENTS)]
    return per_batch.select(
        "role", "observed_utc", *(pl.col(s).cum_sum().over("role") for s in sums)
    )


def _before(
    left: pl.DataFrame, right: pl.DataFrame, by: str | Sequence[str] | None = None
) -> pl.DataFrame:
    """left with right's latest row public strictly before each as_of_utc."""
    return left.sort("as_of_utc").join_asof(
        right.sort("observed_utc"),
        left_on="as_of_utc",
        right_on="observed_utc",
        by=by,
        strategy="backward",
        allow_exact_matches=False,
        check_sortedness=False,
    )


def role_rates(rows: pl.DataFrame, times: pl.DataFrame) -> pl.DataFrame:
    """Each role's rate per hour of each component (rho_<component>) at each as_of_utc of times,
    over the player_games public before it; null before any."""
    targets = (
        times.select("as_of_utc")
        .unique()
        .join(pl.DataFrame({"role": list(SKATER_ROLES)}), how="cross")
    )
    joined = _before(targets, _role_history(rows), by="role")
    return joined.select(
        "as_of_utc",
        "role",
        **{f"rho_{c}": pl.col(f"r_{c}") / pl.col("r_hours") for c in COMPONENTS},
    )


def rates(
    candidates: pl.DataFrame, rows: pl.DataFrame, pulls: Mapping[int, SeasonPulls]
) -> pl.DataFrame:
    """Each candidate's rates (one row per component, PenaltyRates' columns but the stamps):
    candidates holds game_id, season, game_date, team, player_id, role and as_of_utc; rows is
    player_games; pulls the scored seasons' SeasonPulls."""
    missing = sorted(set(candidates["season"].unique().to_list()) - set(pulls))
    if missing:
        raise ValueError(f"no pulls for {missing}")
    table = pl.DataFrame(
        [
            {"season": s, "role": r, "component": c, "pull_hours": p.pull[r, c]}
            for s, p in pulls.items()
            for r in SKATER_ROLES
            for c in COMPONENTS
        ],
        schema={
            "season": pl.Int32,
            "role": pl.String,
            "component": pl.String,
            "pull_hours": pl.Float64,
        },
    )
    joined = _before(candidates, _player_history(rows), by="player_id")
    joined = _before(joined.drop("observed_utc"), _batches(rows))
    joined = joined.drop("observed_utc").join(
        role_rates(rows, candidates), on=["as_of_utc", "role"], how="left"
    )
    scale = pl.lit(2.0).pow(-pl.col("reference") / HALF_LIFE_DAYS)
    long = pl.concat(
        [
            joined.select(
                "game_id",
                "season",
                "game_date",
                "team",
                "player_id",
                "role",
                "as_of_utc",
                "known_utc",
                component=pl.lit(c),
                count=(pl.col(f"g_{c}") * scale).fill_null(0.0),
                hours=(pl.col("g_hours") * scale).fill_null(0.0),
                prior=pl.col(f"rho_{c}"),
            )
            for c in COMPONENTS
        ]
    ).join(table, on=["season", "role", "component"], how="left")
    if long["prior"].is_null().any():
        raise ValueError("a candidate's game has no earlier penalties or stints to rate from")
    finite = pl.col("pull_hours").is_finite()
    pulled = pl.col("count") + pl.col("pull_hours") * pl.col("prior")
    return long.select(
        "game_id",
        "season",
        "game_date",
        "team",
        "player_id",
        "role",
        "component",
        mean=pl.when(finite)
        .then(pulled / (pl.col("hours") + pl.col("pull_hours")))
        .otherwise(pl.col("prior")),
        prior="prior",
        sd=pl.when(finite)
        .then(pulled.sqrt() / (pl.col("hours") + pl.col("pull_hours")))
        .otherwise(0.0),
        hours="hours",
        known_utc="known_utc",
        half_life_days=pl.lit(HALF_LIFE_DAYS),
        pull_hours="pull_hours",
        as_of_utc="as_of_utc",
    ).sort("game_id", "player_id", "component")


def team_games(
    strength_time: pl.DataFrame,
    weighted: pl.DataFrame,
    shots: pl.DataFrame,
    shot_xg: pl.DataFrame,
) -> pl.DataFrame:
    """One row per team and game (strength_time): its power plays (the other team's unoffset
    penalties), its power-play and penalty-kill minutes with both nets manned, as team strength
    counts them, its shorthanded xG with its own goalie in (null in a game without xG), and when
    all of these were public."""
    both_manned = ~pl.col("own_net_empty") & ~pl.col("opp_net_empty")

    def minutes(states: Sequence[str]) -> pl.Expr:
        mask = both_manned & pl.col("strength").is_in(list(states))
        return (pl.col("seconds").filter(mask).sum() / 60).cast(pl.Float64)

    base = strength_time.group_by("game_id", "season", "game_date", "team").agg(
        pp_minutes=minutes(ts.POWER_PLAY),
        pk_minutes=minutes(ts.PENALTY_KILL),
        time_utc=pl.col("observed_utc").max(),
    )
    pairs = base.join(base.select("game_id", opponent="team"), on="game_id", how="inner").filter(
        pl.col("team") != pl.col("opponent")
    )
    against = weighted.group_by("game_id", opponent="team").agg(
        opportunities=pl.col("weight").sum(), penalties_utc=pl.col("observed_utc").max()
    )
    for_, against_ = pl.col("skaters_for"), pl.col("skaters_against")
    shorthanded = (
        shot_xg.join(
            shots.select(
                "game_id",
                "event_id",
                "team",
                "is_home",
                "skaters_for",
                "skaters_against",
                "situation_code",
            ),
            on=["game_id", "event_id"],
        )
        .with_columns(goalie_in=ts.own_goalie_in())
        .group_by("game_id", "team")
        .agg(
            sh_xg=pl.col("xg")
            .filter((for_ < against_) & (against_ <= 5) & pl.col("goalie_in"))
            .sum(),
            shots_utc=pl.col("observed_utc").max(),
        )
    )
    with_xg = shot_xg.select("game_id").unique().with_columns(has_xg=pl.lit(True))
    frame = (
        pairs.join(against, on=["game_id", "opponent"], how="left")
        .join(shorthanded, on=["game_id", "team"], how="left")
        .join(with_xg, on="game_id", how="left")
        .with_columns(
            pl.col("opportunities").fill_null(0.0),
            has_xg=pl.col("has_xg").fill_null(False),
        )
        .with_columns(
            sh_xg=pl.when(pl.col("has_xg")).then(pl.col("sh_xg").fill_null(0.0)),
        )
    )
    return frame.select(
        "game_id",
        "season",
        "game_date",
        "team",
        "opponent",
        "opportunities",
        "pp_minutes",
        "pk_minutes",
        "sh_xg",
        "has_xg",
        observed_utc=pl.max_horizontal("time_utc", "penalties_utc", "shots_utc"),
    ).sort("observed_utc", "game_id", "team")


LEAGUE_SUMS = ("opportunities", "pp_minutes", "xg_pk_minutes", "sh_xg", "team_games")


def league_figures(history: pl.DataFrame, times: pl.DataFrame) -> pl.DataFrame:
    """For each (season, as_of_utc) of times, over the team_games public before as_of_utc in
    that season and the one before: L, unoffset penalties per team-game (league_opportunities);
    length, power-play minutes per unoffset penalty (pp_length); and s, shorthanded xG per
    penalty-kill minute in the games with xG (sh_xg_per_pk_minute, null without any)."""
    rows = history.select(
        "season",
        "observed_utc",
        "opportunities",
        "pp_minutes",
        xg_pk_minutes=pl.when(pl.col("has_xg")).then(pl.col("pk_minutes")).otherwise(0.0),
        sh_xg=pl.col("sh_xg").fill_null(0.0),
        team_games=pl.lit(1.0),
    )
    out = []
    for (season,), targets in times.select("season", "as_of_utc").unique().group_by("season"):
        past = rows.filter(pl.col("season").is_in([season, season - 10_001])).sort("observed_utc")
        totals = past.select(LEAGUE_SUMS).to_numpy().astype(float).cumsum(axis=0)
        seen = np.searchsorted(
            past["observed_utc"].to_numpy(), targets["as_of_utc"].to_numpy(), side="left"
        )
        state = np.full((targets.height, len(LEAGUE_SUMS)), np.nan)
        has = seen > 0
        state[has] = totals[seen[has] - 1]
        out.append(
            targets.with_columns(pl.Series(name, state[:, i]) for i, name in enumerate(LEAGUE_SUMS))
        )
    sums = pl.concat(out)
    return sums.select(
        "season",
        "as_of_utc",
        league_opportunities=pl.col("opportunities") / pl.col("team_games"),
        pp_length=pl.col("pp_minutes") / pl.col("opportunities"),
        sh_xg_per_pk_minute=pl.when(pl.col("xg_pk_minutes") > 0).then(
            pl.col("sh_xg") / pl.col("xg_pk_minutes")
        ),
    ).with_columns(pl.col(pl.Float64).fill_nan(None))


def expected(
    rated: pl.DataFrame,
    candidates: pl.DataFrame,
    replacements: pl.DataFrame,
    roles: pl.DataFrame,
    league: pl.DataFrame,
    games: pl.DataFrame,
) -> pl.DataFrame:
    """Each team-game's expected power plays (ExpectedPowerPlays' columns but the stamps).
    rated is rates(); candidates the lineups rows of the skaters with their expected minutes;
    replacements the lineup_replacements rows; roles is role_rates() at the games' as-of times;
    league is league_figures(). Every team-game of games in rated's seasons gets a row: one
    without candidates, such as a new team's first game, is all replacements."""
    seasons = rated["season"].unique().implode()
    scheduled = games.filter(pl.col("season").is_in(seasons)).select(
        "game_id",
        "season",
        "game_date",
        "home",
        "away",
        as_of_utc=ts.as_of(pl.col("game_date"), pl.col("start_utc")),
    )
    sides = pl.concat(
        [
            scheduled.select(
                "game_id",
                "season",
                "game_date",
                "as_of_utc",
                team=pl.col(side),
                opponent=pl.col(other),
                is_home=pl.lit(side == "home"),
            )
            for side, other in (("home", "away"), ("away", "home"))
        ]
    )
    minutes = candidates.select(
        "game_id",
        "team",
        "player_id",
        e=pl.sum_horizontal(*(pl.col(f"exp_{s}").fill_null(0.0) for s in ("5v5", "pp", "pk"))),
    )
    weighted = (
        rated.join(minutes, on=["game_id", "team", "player_id"], how="left")
        .with_columns(pl.col("e").fill_null(0.0))
        .group_by("game_id", "team", "component")
        .agg(num=(pl.col("e") * pl.col("mean")).sum(), den=(pl.col("e") * pl.col("prior")).sum())
    )
    spare = (
        replacements.select(
            "game_id",
            "team",
            "role",
            e=pl.sum_horizontal(*(pl.col(f"exp_{s}") for s in ("5v5", "pp", "pk"))),
        )
        .join(sides.select("game_id", "team", "as_of_utc"), on=["game_id", "team"], how="inner")
        .join(roles, on=["as_of_utc", "role"], how="left")
    )
    spare = (
        pl.concat(
            [
                spare.select(
                    "game_id", "team", component=pl.lit(c), spare=pl.col("e") * pl.col(f"rho_{c}")
                )
                for c in COMPONENTS
            ]
        )
        .group_by("game_id", "team", "component")
        .agg(pl.col("spare").sum())
    )
    index = (
        sides.select("game_id", "team")
        .join(pl.DataFrame({"component": list(COMPONENTS)}), how="cross")
        .join(weighted, on=["game_id", "team", "component"], how="left")
        .join(spare, on=["game_id", "team", "component"], how="left")
        .with_columns(pl.col("num", "den", "spare").fill_null(0.0))
        .with_columns(
            index=pl.when(pl.col("den") + pl.col("spare") > 0)
            .then((pl.col("num") + pl.col("spare")) / (pl.col("den") + pl.col("spare")))
            .otherwise(1.0)
        )
        .pivot(on="component", index=["game_id", "team"], values="index")
    )
    taken = index.select("game_id", opponent="team", taken_index=TAKEN)
    drawn = index.select("game_id", "team", drawn_index=DRAWN)
    frame = (
        sides.join(drawn, on=["game_id", "team"], how="left")
        .join(taken, on=["game_id", "opponent"], how="left")
        .join(league, on=["season", "as_of_utc"], how="left")
        .with_columns(
            opportunities=pl.col("league_opportunities")
            * (pl.col("taken_index") + pl.col("drawn_index"))
            / 2
        )
        .with_columns(pp_minutes=pl.col("opportunities") * pl.col("pp_length"))
    )
    killing = frame.select("game_id", opponent="team", pk_minutes="pp_minutes")
    return (
        frame.join(killing, on=["game_id", "opponent"], how="left")
        .with_columns(sh_xg=pl.col("sh_xg_per_pk_minute") * pl.col("pk_minutes"))
        .select(
            "game_id",
            "season",
            "game_date",
            "team",
            "opponent",
            "is_home",
            "taken_index",
            "drawn_index",
            "opportunities",
            "pp_minutes",
            "pk_minutes",
            "sh_xg",
            "league_opportunities",
            "pp_length",
            "sh_xg_per_pk_minute",
            "as_of_utc",
        )
        .sort("game_id", "team")
    )


def stamp(
    frame: pl.DataFrame,
    schema: type[PenaltyRates] | type[ExpectedPowerPlays],
    pulls: Mapping[int, SeasonPulls],
    version: str,
) -> pl.DataFrame:
    """The rows with train_cutoff, the later of the memory's tuning cutoff and the season's pulls'
    cutoff, artifact_version, and observed_utc, the later of as_of_utc and train_cutoff."""
    cutoffs = pl.DataFrame(
        {
            "season": list(pulls),
            "train_cutoff": [max(TRAIN_CUTOFF, p.cutoff) for p in pulls.values()],
        },
        schema={"season": pl.Int32, "train_cutoff": pl.Datetime("us", "UTC")},
    )
    stamped = frame.join(cutoffs, on="season", how="left").with_columns(
        artifact_version=pl.lit(version),
        observed_utc=pl.when(pl.col("as_of_utc") > pl.col("train_cutoff"))
        .then(pl.col("as_of_utc"))
        .otherwise(pl.col("train_cutoff")),
    )
    columns = dtypes(schema)
    return schema.validate(stamped.select(list(columns)).cast(columns))  # type: ignore[arg-type]
