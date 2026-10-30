"""The evidence for ADR 0051 (#134): the rating B3 should give its replacement skaters, on the
training seasons only (ADR 0011).

    uv run python scripts/replacement_level.py

B3 counts each team-game's replacement skaters (`lineup_replacements`, ADR 0018) in its 5v5
minutes and rates them 0, RAPM's reference skater (ADR 0023). This script scores B3's 5v5 xG
difference with the replacements rated at each level below, per role and season, each from
earlier seasons only:

- **zero:** today's.
- **fringe** (#134's option 2): the season-end 5v5 rating, offense plus defense, of the skaters
  who dressed 20 games or fewer in an earlier season, weighted by their games.
- **prior** (option 3): the season prior of the earlier seasons' newcomers, the skaters who
  dressed without being candidates (ADR 0017).
- **newcomer:** those newcomers' rating as known before their game (their latest earlier rating,
  or their season prior without one), one level for a team's first game of a season and one for
  its other games.
- **fitted:** the levels, F and D, that best fit the realized 5v5 xG difference of the earlier
  seasons' games. A check on what the data ask for, not an option.

It reads the lake's games, actual_lineups, lineups, lineup_replacements, player_ratings and
stints of 2011-12 to 2017-18 (`Lake.pull` brings them from R2), and writes
reports/lineups/replacement-level-<version>.md. It writes nothing to the lake.
"""

import argparse
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import polars as pl
from sklearn.linear_model import LogisticRegression

from nhl_edge.backtest import metrics, reports
from nhl_edge.backtest.walk_forward import fold_start, outcomes
from nhl_edge.lake.tables import Lake
from nhl_edge.ratings import rapm

SEASONS = tuple(range(20112012, 20182019, 10_001))
SCORED = rapm.TUNING_SEASONS
ROLES = ("F", "D")
# A fringe skater dressed this many games or fewer in his season.
FRINGE_GAMES = 20
ZERO, FRINGE, PRIOR, NEWCOMER, FITTED = "zero", "fringe", "prior", "newcomer", "fitted"
LEVELS = (FRINGE, PRIOR, NEWCOMER, FITTED)
# B3's skaters on the ice at 5v5.
ON_ICE = 5.0
OUT = Path("reports/lineups")


def five_ratings(ratings: pl.DataFrame) -> pl.DataFrame:
    """Each player_ratings row's 5v5 offense plus defense (level) and prior, one row per
    candidate-game: what B3's 5v5 strength sums (ADR 0023)."""
    wide = ratings.filter(pl.col("component").is_in(["ev_off", "ev_def"])).pivot(
        on="component",
        index=["game_id", "season", "player_id", "as_of_utc"],
        values=["mean", "prior"],
    )
    return wide.select(
        "game_id",
        "season",
        "player_id",
        "as_of_utc",
        level=pl.col("mean_ev_off") + pl.col("mean_ev_def"),
        prior=pl.col("prior_ev_off") + pl.col("prior_ev_def"),
    )


def team_minutes(lineups: pl.DataFrame, rated: pl.DataFrame, spare: pl.DataFrame) -> pl.DataFrame:
    """Per team-game: the candidates' Σ exp_5v5·level (cand_sum), the replacements' 5v5 minutes
    per role (spare_F, spare_D) and every skater's (all_min), as B3's team_goals sums them."""
    skaters = lineups.filter(pl.col("role").is_in(ROLES)).join(
        rated.select("game_id", "player_id", "level"), on=["game_id", "player_id"], how="left"
    )
    cand = skaters.group_by("game_id", "team").agg(
        cand_min=pl.col("exp_5v5").fill_null(0.0).sum(),
        cand_sum=(pl.col("exp_5v5").fill_null(0.0) * pl.col("level").fill_null(0.0)).sum(),
    )
    minutes = spare.group_by("game_id", "team").agg(
        **{f"spare_{r}": pl.col("exp_5v5").filter(pl.col("role") == r).sum() for r in ROLES}
    )
    parts = ("cand_min", "cand_sum", *(f"spare_{r}" for r in ROLES))
    return (
        cand.join(minutes, on=["game_id", "team"], how="full", coalesce=True)
        .with_columns(pl.col(*parts).fill_null(0.0))
        .with_columns(all_min=pl.sum_horizontal("cand_min", *(f"spare_{r}" for r in ROLES)))
    )


def openers(games: pl.DataFrame) -> pl.DataFrame:
    """Each team's first game of each season: season, team, open_utc."""
    sides = pl.concat(
        [games.select("season", team=side, start=pl.col("start_utc")) for side in ("home", "away")]
    )
    return sides.group_by("season", "team").agg(open_utc=pl.col("start").min())


def game_rows(games: pl.DataFrame, teams: pl.DataFrame) -> pl.DataFrame:
    """One row per game with both teams' minutes (h_ and a_ prefixes), whether each side plays
    its first game of the season, the game's 5v5 hours, and x0, B3's 5v5 xG difference with the
    replacements rated 0."""
    columns = ("cand_sum", *(f"spare_{r}" for r in ROLES), "all_min")
    first = openers(games)
    rows = games.select("game_id", "season", "game_date", "start_utc", "home", "away")
    for p, side in (("h", "home"), ("a", "away")):
        rows = rows.join(
            teams.select(
                "game_id",
                pl.col("team").alias(side),
                *(pl.col(c).alias(f"{p}_{c}") for c in columns),
            ),
            on=["game_id", side],
        ).join(
            first.select(
                "season", pl.col("team").alias(side), pl.col("open_utc").alias(f"{p}_open")
            ),
            on=["season", side],
        )
    hours = (pl.col("h_all_min") + pl.col("a_all_min")) / 2 / ON_ICE / 60
    return rows.with_columns(
        h_opener=pl.col("start_utc") == pl.col("h_open"),
        a_opener=pl.col("start_utc") == pl.col("a_open"),
        hours=hours,
        x0=hours * (on_ice("h", "cand_sum") - on_ice("a", "cand_sum")),
        spare_share=pl.max_horizontal(
            *(
                pl.sum_horizontal(*(pl.col(f"{p}_spare_{r}") for r in ROLES))
                / pl.col(f"{p}_all_min")
                for p in ("h", "a")
            )
        ),
    ).with_columns(opener=pl.col("h_opener") | pl.col("a_opener"))


def on_ice(side: str, column: str) -> pl.Expr:
    """The side's on-ice sum per hour from column: ON_ICE·column over its 5v5 skater-minutes."""
    total = pl.col(f"{side}_all_min")
    return pl.when(total > 0).then(ON_ICE * pl.col(f"{side}_{column}") / total).otherwise(0.0)


def rated_at(rows: pl.DataFrame, levels: Mapping[str, pl.Expr]) -> pl.Expr:
    """x0 with each side's replacements of role r rated at levels[f"{side}_{r}"]."""
    parts = [
        sign * pl.col("hours") * on_ice(p, f"spare_{r}") * levels[f"{p}_{r}"]
        for p, sign in (("h", 1.0), ("a", -1.0))
        for r in ROLES
    ]
    return pl.col("x0") + pl.sum_horizontal(parts)


def player_seasons(dressed: pl.DataFrame, rated: pl.DataFrame) -> pl.DataFrame:
    """Each skater's season: his games dressed, his usual role, and his season-end level, his
    latest rating of the season."""
    games = dressed.group_by("season", "player_id").agg(
        games=pl.len(), role=pl.col("role").mode().sort().first()
    )
    end = (
        rated.sort("as_of_utc")
        .group_by("season", "player_id")
        .agg(end_level=pl.col("level").last())
    )
    return games.join(end, on=["season", "player_id"], how="left")


def newcomers(
    dressed: pl.DataFrame, lineups: pl.DataFrame, rated: pl.DataFrame, games: pl.DataFrame
) -> pl.DataFrame:
    """The skaters who dressed without being candidates (ADR 0017): season, game_id, team,
    player_id, role, opener (his team's first game of the season), his season prior (prior), and
    his level as known before the game (known: his latest rating with as_of_utc before the
    game's start, else his season prior, else 0), and whether he had that earlier rating
    (rated)."""
    starts = games.select("game_id", "start_utc")
    first = openers(games)
    new = (
        dressed.join(
            lineups.select("game_id", "player_id"), on=["game_id", "player_id"], how="anti"
        )
        .join(starts, on="game_id")
        .join(first, on=["season", "team"])
        .with_columns(opener=pl.col("start_utc") == pl.col("open_utc"))
        .sort("start_utc")
    )
    earlier = rated.select("player_id", known_utc="as_of_utc", earlier="level").sort("known_utc")
    # A rating as of the game's start counts only from before it.
    found = new.with_columns(before=pl.col("start_utc") - pl.duration(microseconds=1)).join_asof(
        earlier, left_on="before", right_on="known_utc", by="player_id", strategy="backward"
    )
    priors = rated.group_by("season", "player_id").agg(prior=pl.col("prior").first())
    return (
        found.join(priors, on=["season", "player_id"], how="left")
        .with_columns(known=pl.coalesce("earlier", "prior", pl.lit(0.0)))
        .with_columns(rated=pl.col("earlier").is_not_null())
        .select(
            "season", "game_id", "team", "player_id", "role", "opener", "prior", "known", "rated"
        )
    )


def season_levels(
    season: int, seasons: pl.DataFrame, new: pl.DataFrame
) -> dict[str, dict[str, float]]:
    """The season's level per option and role, from the seasons before it only: fringe and prior
    per role, and newcomer per role and context (in-season or opener)."""
    fringe = seasons.filter(
        pl.col("season") < season,
        pl.col("games") <= FRINGE_GAMES,
        pl.col("end_level").is_not_null(),
    )
    past = new.filter(pl.col("season") < season)
    out: dict[str, dict[str, float]] = {
        FRINGE: {},
        PRIOR: {},
        f"{NEWCOMER}_in": {},
        f"{NEWCOMER}_open": {},
    }
    for role in ROLES:
        f = fringe.filter(pl.col("role") == role)
        out[FRINGE][role] = _mean(f["end_level"], f["games"])
        n = past.filter(pl.col("role") == role)
        out[PRIOR][role] = _mean(n["prior"].drop_nulls())
        out[f"{NEWCOMER}_in"][role] = _mean(n.filter(~pl.col("opener"))["known"])
        out[f"{NEWCOMER}_open"][role] = _mean(n.filter(pl.col("opener"))["known"])
    return out


def _mean(values: pl.Series, weights: pl.Series | None = None) -> float:
    """The (weighted) mean, 0 for none: no earlier season means no level beyond the reference."""
    if values.is_empty():
        return 0.0
    if weights is None:
        return float(values.mean())  # type: ignore[arg-type]
    return float((values * weights).sum()) / float(weights.sum())


def level_exprs(kind: str, levels: Mapping[str, Mapping[str, float]]) -> dict[str, pl.Expr]:
    """Each side and role's level for the option: the season's constant, or for newcomer the
    opener level on a side's first game of the season."""
    out = {}
    for p in ("h", "a"):
        for r in ROLES:
            if kind == NEWCOMER:
                out[f"{p}_{r}"] = (
                    pl.when(pl.col(f"{p}_opener"))
                    .then(pl.lit(levels[f"{NEWCOMER}_open"][r]))
                    .otherwise(pl.lit(levels[f"{NEWCOMER}_in"][r]))
                )
            elif kind == ZERO:
                out[f"{p}_{r}"] = pl.lit(0.0)
            else:
                out[f"{p}_{r}"] = pl.lit(levels[kind][r])
    return out


def fitted_levels(train: pl.DataFrame) -> dict[str, float]:
    """The F and D levels that best fit train's realized 5v5 xG difference r: least squares of
    r on x0 and each role's replacement term, each term's coefficient over x0's."""
    rows = train.filter(pl.col("r").is_not_null())
    zero = {f"{p}_{r}": pl.lit(0.0) for p in ("h", "a") for r in ROLES}
    terms = [
        rated_at(rows, zero | {f"h_{r}": pl.lit(1.0), f"a_{r}": pl.lit(1.0)}) - pl.col("x0")
        for r in ROLES
    ]
    x = rows.select(pl.lit(1.0), "x0", *(t.alias(r) for t, r in zip(terms, ROLES, strict=True)))
    c = np.linalg.lstsq(x.to_numpy(), rows["r"].to_numpy(), rcond=None)[0]
    return {r: float(c[2 + k] / c[1]) for k, r in enumerate(ROLES)}


def score(
    rows: pl.DataFrame,
    calendar: pl.DataFrame,
    by_season: Mapping[int, Mapping[str, Mapping[str, float]]],
    seasons: Sequence[int] = SCORED,
) -> pl.DataFrame:
    """Each scored game's prediction under each option, the home win's probability (p_) and the
    5v5 xG difference's (rhat_), with its win log loss (ll_) and squared error (se_): the win
    model and the line fitted on the earlier seasons' games before the fold start, as
    tuning.scored_games fits."""
    out = []
    for season in seasons:
        start = fold_start(calendar, season)
        train = rows.filter(pl.col("season") < season, pl.col("result_utc") < start)
        test = rows.filter(pl.col("season") == season)
        fit = fitted_levels(train)
        options = {k: level_exprs(k, by_season[season]) for k in (ZERO, FRINGE, PRIOR, NEWCOMER)}
        options[FITTED] = {f"{p}_{r}": pl.lit(fit[r]) for p in ("h", "a") for r in ROLES}
        scored = test.select("game_id", "season", "game_date", "spare_share", "opener")
        for kind, levels in options.items():
            # Each fold's train rows take the option's levels for their own seasons.
            tx = _with_own_levels(train, kind, by_season, levels).to_numpy()
            ex = test.select(rated_at(test, levels)).to_series().to_numpy()
            model = LogisticRegression(C=np.inf, max_iter=1000)
            model.fit(tx.reshape(-1, 1), train["home_win"].to_numpy())
            p = model.predict_proba(ex.reshape(-1, 1))[:, 1]
            y = test["home_win"].to_numpy()
            known = train["r"].is_not_null().to_numpy()
            b, a = np.polyfit(tx[known], train["r"].to_numpy()[known], 1)
            scored = scored.with_columns(
                **{
                    f"p_{kind}": pl.Series(p),
                    f"rhat_{kind}": pl.Series(a + b * ex),
                    f"ll_{kind}": pl.Series(-(y * np.log(p) + (1 - y) * np.log(1 - p))),
                    f"se_{kind}": (test["r"] - a - b * pl.Series(ex)) ** 2,
                }
            )
        out.append(scored)
    return pl.concat(out)


def _with_own_levels(
    train: pl.DataFrame,
    kind: str,
    by_season: Mapping[int, Mapping[str, Mapping[str, float]]],
    fitted: Mapping[str, pl.Expr],
) -> pl.Series:
    """train's x under the option, each season at its own levels (the fitted option at the
    fold's)."""
    if kind == FITTED:
        return train.select(rated_at(train, fitted)).to_series()
    parts = []
    for (season,), rows in train.group_by("season", maintain_order=True):
        levels = by_season.get(int(season))  # type: ignore[arg-type]
        exprs = level_exprs(ZERO if levels is None else kind, levels or {})
        parts.append(rows.select("game_id", x=rated_at(rows, exprs)))
    x = pl.concat(parts)
    return train.select("game_id").join(x, on="game_id", how="left", maintain_order="left")["x"]


def by_seasons(frame: pl.DataFrame, value: str) -> metrics.Interval:
    """The mean with whole seasons resampled: for the openers, which fall in one week a season,
    so weekly blocks would not vary (as #120's openers)."""
    seasons = frame["season"].unique().sort().to_list()
    per = {s: frame.filter(pl.col("season") == s)[value].to_numpy() for s in seasons}
    rng = np.random.default_rng(metrics.SEED)
    means = np.array(
        [
            np.concatenate([per[s] for s in rng.choice(seasons, len(seasons))]).mean()
            for _ in range(metrics.DRAWS)
        ]
    )
    return metrics.interval(float(frame[value].mean()), means)  # type: ignore[arg-type]


def shown(interval: metrics.Interval, digits: int) -> str:
    f = f"{{:+.{digits}f}}"
    return f"{f.format(interval.value)} [{f.format(interval.low)}, {f.format(interval.high)}]"


def report(
    scored: pl.DataFrame,
    by_season: Mapping[int, Mapping[str, Mapping[str, float]]],
    share: pl.DataFrame,
    new: pl.DataFrame,
    version: str,
) -> str:
    out = [
        f"# Replacement skaters' rating, {version}",
        "",
        "The evidence for ADR 0051 (#134), from `scripts/replacement_level.py`: B3's 5v5 xG "
        "difference with its replacement skaters rated at each level, scored on 2012-13 to "
        "2017-18 (ADR 0011). Each level comes from earlier seasons only.",
        "",
        "## How many minutes",
        "",
        "The replacements' share of each season's projected 5v5 skater-minutes, and of the "
        "minutes in a team's first game of the season.",
        "",
        "| Season | Every game | First games |",
        "| --- | ---: | ---: |",
    ]
    for r in share.sort("season").iter_rows(named=True):
        out.append(f"| {r['season']} | {r['all']:.1%} | {r['first']:.1%} |")
    out += [
        "",
        "## Who the newcomers are",
        "",
        "The skaters who dressed without being candidates (ADR 0017), whom the replacement rows "
        "stand for, and the share of them with an NHL rating before their game: most had played "
        "in the NHL before.",
        "",
        "| Season | In-season newcomers | Rated before | Newcomers in first games | Rated before |",
        "| --- | ---: | ---: | ---: | ---: |",
        *(
            f"| {r['season']} | {r['n_in']:,} | {r['rated_in']:.0%} | {r['n_open']:,} "
            f"| {r['rated_open']:.0%} |"
            for r in new.filter(pl.col("season").is_in(scored["season"].unique().implode()))
            .group_by("season")
            .agg(
                n_in=(~pl.col("opener")).sum(),
                rated_in=pl.col("rated").filter(~pl.col("opener")).mean(),
                n_open=pl.col("opener").sum(),
                rated_open=pl.col("rated").filter("opener").mean(),
            )
            .sort("season")
            .iter_rows(named=True)
        ),
        "",
        "## The levels",
        "",
        "Each season's level per role, 5v5 offense plus defense in xG per hour; 0 is RAPM's "
        "reference skater. The newcomer level has one value for a team's first game of a "
        "season (opener) and one for its other games (in-season).",
        "",
        "| Season | Fringe F | Fringe D | Prior F | Prior D | Newcomer F, in-season | Newcomer D, "
        "in-season | Newcomer F, opener | Newcomer D, opener |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for season in sorted(scored["season"].unique().to_list()):
        v = by_season[season]
        cells = [
            v[FRINGE]["F"],
            v[FRINGE]["D"],
            v[PRIOR]["F"],
            v[PRIOR]["D"],
            v[f"{NEWCOMER}_in"]["F"],
            v[f"{NEWCOMER}_in"]["D"],
            v[f"{NEWCOMER}_open"]["F"],
            v[f"{NEWCOMER}_open"]["D"],
        ]
        out.append(f"| {season} | " + " | ".join(f"{c:+.3f}" for c in cells) + " |")
    subsets = (
        ("Every game", scored, False),
        ("In-season games", scored.filter(~pl.col("opener")), False),
        (
            "In-season, replacements 3% or more of a side's minutes",
            scored.filter(~pl.col("opener"), pl.col("spare_share") >= 0.03),
            False,
        ),
        ("Either team's first game of the season", scored.filter(pl.col("opener")), True),
    )
    out += [
        "",
        "## Predictions",
        "",
        "Each option's minus zero's, negative when it predicts better: the home team's "
        "full-game win log loss through a win model on the 5v5 xG difference, and the squared "
        "error of the game's realized 5v5 xG difference through a line, both fitted on the "
        "earlier seasons' games before the fold starts. Intervals are 95% weekly block "
        "bootstrap, except the first games', which resample whole seasons, since each season's "
        "fall in one week.",
        "",
    ]
    for name, frame, seasons_only in subsets:
        out += [
            f"**{name}** ({frame.height:,} games)",
            "",
            "| Option | Win log loss | 5v5 xG difference |",
            "| --- | --- | --- |",
        ]
        for kind in LEVELS:
            rows = frame.with_columns(
                dll=pl.col(f"ll_{kind}") - pl.col(f"ll_{ZERO}"),
                dse=pl.col(f"se_{kind}") - pl.col(f"se_{ZERO}"),
            )
            with_r = rows.filter(pl.col("dse").is_not_null())
            if seasons_only:
                cells = [by_seasons(rows, "dll"), by_seasons(with_r, "dse")]
            else:
                cells = [
                    _interval(metrics.bootstrap(rows, "dll")),
                    _interval(metrics.bootstrap(with_r, "dse")),
                ]
            out.append(f"| {kind} | {shown(cells[0], 5)} | {shown(cells[1], 5)} |")
        out.append("")
    out += [
        "Only the 5v5 part of Δĝ is scored. B3 rates the replacements 0 on the power play and "
        "the penalty kill too, where they hold similar shares of the minutes.",
        "",
    ]
    return "\n".join(out)


def _interval(e: metrics.Estimate) -> metrics.Interval:
    return metrics.Interval(value=e.mean, low=e.low, high=e.high)


def shares(rows: pl.DataFrame) -> pl.DataFrame:
    """The replacements' share of the projected 5v5 skater-minutes per season, overall and in
    teams' first games."""
    sides = pl.concat(
        [
            rows.select(
                "season",
                first=pl.col(f"{p}_opener"),
                spare=pl.sum_horizontal(*(pl.col(f"{p}_spare_{r}") for r in ROLES)),
                total=pl.col(f"{p}_all_min"),
            )
            for p in ("h", "a")
        ]
    )
    return sides.group_by("season").agg(
        all=pl.col("spare").sum() / pl.col("total").sum(),
        first=pl.col("spare").filter("first").sum() / pl.col("total").filter("first").sum(),
    )


def main(seasons: Sequence[int] = SEASONS) -> None:
    parser = argparse.ArgumentParser(description="The evidence for ADR 0051 (#134).")
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()
    lake = Lake.from_env(mirror=False)
    games = lake.read("games").filter(pl.col("season").is_in(seasons))
    rated = five_ratings(lake.read("player_ratings", seasons=seasons))
    lineups = lake.read("lineups", seasons=seasons)
    dressed = pl.concat([lake.read("actual_lineups", seasons=[s]) for s in seasons]).filter(
        pl.col("role").is_in(ROLES)
    )
    teams = team_minutes(lineups, rated, lake.read("lineup_replacements", seasons=seasons))
    realized = pl.concat(
        [
            lake.read("stints", seasons=[s])
            .filter(pl.col("strength") == rapm.EV_STRENGTH, pl.col("drop_reason").is_null())
            .group_by("game_id")
            .agg(r=(pl.col("home_xg") - pl.col("away_xg")).sum())
            for s in seasons
        ]
    )
    rows = (
        game_rows(games, teams)
        .join(realized, on="game_id", how="left")
        .join(outcomes(games), on="game_id")
    )
    ps = player_seasons(dressed, rated)
    new = newcomers(dressed, lineups, rated, games)
    by_season = {s: season_levels(s, ps, new) for s in seasons}
    scored = score(rows, games.select("season", "start_utc"), by_season)
    version = reports.version("replacement-level", datetime.now(UTC))
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / f"{version}.md"
    path.write_text(
        report(scored, by_season, shares(rows.filter(pl.col("season").is_in(SCORED))), new, version)
    )
    print(path)


if __name__ == "__main__":
    main()
