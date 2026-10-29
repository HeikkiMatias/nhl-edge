"""The evidence for ADR 0050 (#125): a debutant's NHLe in his debut season, on the training
seasons only (ADR 0011).

    uv run python scripts/debut_nhle.py

It reads the lake's games, players, player_league_seasons, actual_lineups, lineups, stints and
player_ratings of 2010-11 to 2017-18 (`Lake.pull` brings them from R2), and fits RAPM's 5v5
model three times with the frozen settings (rapm.TUNED), as `nhl rapm --tune` does:

- **base:** rapm.rate's fit, each player's traits fixed at the season's start (ADR 0020). A
  debutant's lines are public only from his first boxscore (ADR 0016), so his prior has no NHLe
  all season. The report checks that it reproduces the lake's player_ratings.
- **prior** (option a1): from the first as-of time after a debutant's lines turn public, his
  prior mean reads his NHLe, with the season's trait effects and NHLe factors held fixed.
- **rows** (option a2): a1, and the season's rows carry each debutant's NHLe in the trait
  columns. Only later seasons' season-start fits read those columns: the daily fits hold them
  at 0.

Each daily fit also predicts the 5v5 rows of the games it rates, before they are read. The
report, reports/ratings/debut-nhle-<version>.md, compares the three on 2012-13 to 2017-18 with
weekly block bootstrap intervals (hard rule 7). It writes nothing to the lake.
"""

import argparse
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import polars as pl

from nhl_edge.backtest import metrics, reports, tuning
from nhl_edge.backtest.walk_forward import fold_start
from nhl_edge.lake.tables import Lake
from nhl_edge.lineup import goalie_start as gs
from nhl_edge.live.targets import rated
from nhl_edge.ratings import priors, rapm
from nhl_edge.reference import load_venues

BASE, PRIOR, ROWS = "base", "prior", "rows"
RUNS = (BASE, PRIOR, ROWS)
LABELS = {
    BASE: "base (today)",
    PRIOR: "a1, the prior",
    ROWS: "a2, the prior and the rows",
}
# Every line in the lake: a debutant's later traits, used only once his lines are public.
EVER = datetime(2100, 1, 1, tzinfo=UTC)
SEASONS = rapm.TUNING_SEASONS
FIRST_LINEUPS = 20102011
OUT = Path("reports/ratings")


@dataclass
class Run:
    """One fit's ratings, its out-of-sample 5v5 rows, each season's debutants whose traits
    change (late) and its ev_off trait effects."""

    ratings: list[pl.DataFrame] = field(default_factory=list)
    stints: list[pl.DataFrame] = field(default_factory=list)
    late: list[pl.DataFrame] = field(default_factory=list)
    effects: dict[int, dict[str, float]] = field(default_factory=dict)


def late_traits(
    players: pl.DataFrame,
    lines: pl.DataFrame,
    season: int,
    factors: pl.DataFrame,
    start: pl.DataFrame,
    cutoff: datetime,
) -> pl.DataFrame:
    """The players whose NHLe traits differ from their season-start ones (start) once every line
    in the lake is read, with the season's factors: their traits then, and public_utc, the latest
    observed_utc of the league seasons their NHLe reads (his first boxscore, ADR 0016), sorted
    by it. Only those public after cutoff, the season's first as-of time."""
    after = priors.traits(
        players, priors.nhle_inputs(priors.league_lines(lines, EVER), factors, season), season
    )
    changed = (
        after.join(start, on="player_id", suffix="_start")
        .filter(
            (pl.col("has_nhle") != pl.col("has_nhle_start"))
            | (pl.col("nhle") != pl.col("nhle_start"))
        )
        .select(after.columns)
    )
    recent = [season - k * 10_001 for k in range(1, priors.NHLE_SEASONS + 1)]
    public = (
        lines.filter(
            pl.col("league") != priors.NHL,
            pl.col("game_type") == priors.REGULAR_SEASON,
            pl.col("season").is_in(recent),
        )
        .group_by("player_id")
        .agg(public_utc=pl.col("observed_utc").max())
    )
    return (
        changed.join(public, on="player_id")
        .filter(pl.col("public_utc") >= cutoff)
        .sort("public_utc", "player_id")
    )


def with_late(start: pl.DataFrame, late: pl.DataFrame) -> pl.DataFrame:
    """The season-start traits with the late players' replaced by their later ones."""
    if late.is_empty():
        return start
    later = late.drop("public_utc")
    return pl.concat([start.join(later, on="player_id", how="anti"), later]).sort("player_id")


def rate(
    stint_seasons: Iterable[pl.DataFrame],
    games: pl.DataFrame,
    roles: pl.DataFrame,
    venues: pl.DataFrame,
    wanted: pl.DataFrame,
    players: pl.DataFrame,
    lines: pl.DataFrame,
    kind: str,
) -> Run:
    """rapm.rate's 5v5 fit without spreads, with the run's handling of debutants' NHLe."""
    settings = rapm.TUNED
    assert not settings.aging, "the copy leaves out the aging shift"
    last = int(wanted["season"].max())  # type: ignore[arg-type]
    seasons = sorted(
        s for s in games["season"].unique().to_list() if rapm.FIRST_SEASON <= s <= last
    )
    numbers = rapm.league_days(games)
    design = rapm.Design(rapm.EV, seasons, sorted(venues["arena_id"].unique().to_list()))
    normal = rapm.Normal(settings.half_life_days)
    run = Run()
    known: datetime | None = None
    reference: float | None = None
    for season, stints in rapm._by_season(stint_seasons, wanted, games):
        if season > last:
            continue
        cutoff = gs.season_cutoff(games, season)
        start, factors = rapm._season_traits(players, lines, season, cutoff)
        assert start is not None
        late = late_traits(players, lines, season, factors, start, cutoff)
        rows = rapm.model_rows(stints, games, roles, venues, rapm.EV)
        days = np.array([numbers[d] for d in rows["game_date"].to_list()], dtype=np.float64)
        x = design.matrix(rows, with_late(start, late) if kind == ROWS else start)
        model = rapm._Model(design, normal, rows, x, days)
        if known is not None and known >= cutoff:
            raise ValueError(f"stints public at {known}, after {season} starts at {cutoff}")
        effects = rapm._season_effects(model, settings, reference)
        start_means = priors.prior_means(start, effects)
        means = start_means
        model.effects = effects
        model.prior = rapm._prior_vector(design, means)
        run.effects[season] = dict(effects.get("ev_off", {}))
        run.late.append(
            late.select("player_id", "public_utc")
            .with_columns(season=pl.lit(season, pl.Int32))
            .join(start_means.select("player_id", prior_start="ev_off"), on="player_id")
            .join(
                priors.prior_means(late.drop("public_utc"), effects).select(
                    "player_id", prior_late="ev_off"
                ),
                on="player_id",
            )
        )
        indexed = rows.with_row_index("row").group_by("game_id").agg(pl.col("row"))
        rows_of = dict(zip(indexed["game_id"].to_list(), indexed["row"].to_list(), strict=True))
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
        public = late["public_utc"]
        swapped = 0
        predicted: list[tuple[np.ndarray, np.ndarray]] = []
        for (count,), group in season_wanted.with_columns(
            count=times.search_sorted(season_wanted["as_of_utc"], side="left")
        ).group_by("count", maintain_order=True):
            if count:
                known = times[int(count) - 1]
                reference = float(batches["day"][int(count) - 1])
            if kind != BASE:
                # Lines public strictly before the group's earliest as-of time.
                first = group["as_of_utc"].min()
                assert isinstance(first, datetime)
                n = int(public.search_sorted(first, side="left"))
                if n > swapped:
                    means = priors.prior_means(with_late(start, late.head(n)), effects)
                    model.prior = rapm._prior_vector(design, means)
                    swapped = n
            model.add_until(known)
            ratings, solution = rapm._ratings(
                group, model, settings, known, reference, means, spread=False
            )
            run.ratings.append(ratings)
            found = [rows_of[g] for g in group["game_id"].unique().to_list() if g in rows_of]
            if found and solution.sigma is not None:
                at = np.sort(np.concatenate([np.asarray(f, dtype=np.int64) for f in found]))
                beta = solution.beta.copy()
                columns = np.arange(len(design.terms), design.size)
                # A player without data sits at his prior mean, as in his rating.
                empty = columns[solution.hours[columns] <= 0]
                beta[empty] = model.prior[empty]
                predicted.append((at, x[at] @ beta))
        model.add_until(times[-1] if times.len() else None)
        if times.len():
            known, reference = times[-1], float(batches["day"][-1])
        if predicted:
            run.stints.append(_out_of_sample(rows, predicted, late, season_wanted, season))
        print(f"  {kind} {season}", flush=True)
    return run


def _out_of_sample(
    rows: pl.DataFrame,
    predicted: list[tuple[np.ndarray, np.ndarray]],
    late: pl.DataFrame,
    season_wanted: pl.DataFrame,
    season: int,
) -> pl.DataFrame:
    """The predicted rows with their prediction, and whether a debutant whose lines were public
    before the game's as-of time was on the ice (the same in every run)."""
    at = np.concatenate([a for a, _ in predicted])
    frame = rows.with_row_index("row")[at].with_columns(
        yhat=pl.Series(np.concatenate([y for _, y in predicted]))
    )
    as_of = season_wanted.group_by("game_id").agg(as_of_utc=pl.col("as_of_utc").min())
    on_ice = (
        frame.select("row", "game_id", player_id=pl.concat_list("attackers", "defenders"))
        .explode("player_id")
        .join(late.select("player_id", "public_utc"), on="player_id")
        .join(as_of, on="game_id")
        .filter(pl.col("public_utc") < pl.col("as_of_utc"))
        .select("row")
        .unique()
    )
    return frame.select(
        "row",
        "game_id",
        "game_date",
        "hours",
        "y",
        "yhat",
        season=pl.lit(season, pl.Int32),
        debutant_on=pl.col("row").is_in(on_ice["row"].implode()),
    )


def estimate(frame: pl.DataFrame, value: str, scale: float = 1.0, digits: int = 5) -> str:
    e = metrics.bootstrap(frame, value)
    f = f"{{:+.{digits}f}}"
    return f"{f.format(e.mean * scale)} [{f.format(e.low * scale)}, {f.format(e.high * scale)}]"


def ratio(frame: pl.DataFrame, num: str, den: str) -> str:
    """Σnum / Σden over the frame's games, with a weekly block bootstrap interval."""
    rng = np.random.default_rng(metrics.SEED)
    weekly = metrics._weeks(frame, pl.col(num).sum().alias("n"), pl.col(den).sum().alias("d"))
    n, d = np.zeros(metrics.DRAWS), np.zeros(metrics.DRAWS)
    for season, picks in metrics._picks(weekly, metrics.DRAWS, rng):
        n += season["n"].to_numpy()[picks].sum(axis=1)
        d += season["d"].to_numpy()[picks].sum(axis=1)
    value = float(frame[num].sum()) / float(frame[den].sum())
    low, high = np.percentile(n / d, [2.5, 97.5])
    return f"{value:.2f} [{low:.2f}, {high:.2f}]"


def xg_errors(feature: pl.DataFrame, realized: pl.DataFrame, games: pl.DataFrame) -> pl.DataFrame:
    """Each scored game's squared error of its realized 5v5 xG difference, predicted by a + b·x
    fitted on the earlier seasons' games before the fold start, as tuning.scored_games fits its
    win model."""
    frame = feature.join(realized, on="game_id").join(
        games.select("game_id", "season", "game_date", "start_utc"), on="game_id"
    )
    calendar = games.select("season", "start_utc")
    parts = []
    for season in SEASONS:
        train = frame.filter(
            pl.col("season") < season, pl.col("start_utc") < fold_start(calendar, season)
        )
        b, a = np.polyfit(train["x"].to_numpy(), train["r"].to_numpy(), 1)
        parts.append(
            frame.filter(pl.col("season") == season).select(
                "game_id", "season", "game_date", se=(pl.col("r") - a - b * pl.col("x")) ** 2
            )
        )
    return pl.concat(parts)


def report(runs: dict[str, Run], lake: Lake, games: pl.DataFrame, version: str) -> str:
    ratings = {k: pl.concat(r.ratings) for k, r in runs.items()}
    stints = {
        k: pl.concat(r.stints).filter(pl.col("season").is_in(SEASONS)) for k, r in runs.items()
    }
    late = pl.concat(runs[PRIOR].late)
    seasons = sorted(ratings[BASE]["season"].unique().to_list())
    lineups = lake.read("lineups", seasons=seasons)
    out = [
        f"# Debutants' NHLe in their debut season, {version}",
        "",
        "The evidence for ADR 0050 (#125), from `scripts/debut_nhle.py`: RAPM's 5v5 model fitted "
        "three times with the frozen settings (`rapm.TUNED`) on the training seasons, scored on "
        "2012-13 to 2017-18 (ADR 0011). Intervals are 95% weekly block bootstrap (hard rule 7).",
        "",
        f"- **{LABELS[BASE]}:** a debutant's traits are fixed at the season's start, before his "
        "lines are public (ADR 0016), so his prior has no NHLe all season (ADR 0020).",
        f"- **{LABELS[PRIOR]}:** from the first as-of time after his lines are public, his prior "
        "mean reads his NHLe, with the season's trait effects and NHLe factors held fixed.",
        f"- **{LABELS[ROWS]}:** a1, and his rows of the season carry his NHLe in the trait "
        "columns, which only later seasons' season-start fits read.",
        "",
    ]
    stored = lake.read("player_ratings", seasons=seasons).filter(
        pl.col("component").is_in(["ev_off", "ev_def"])
    )
    check = ratings[BASE].join(
        stored.select("game_id", "player_id", "component", stored="mean"),
        on=["game_id", "player_id", "component"],
    )
    gap = float((check["mean"] - check["stored"]).abs().max())  # type: ignore[arg-type]
    out += [
        f"The base fit reproduces the lake's player_ratings ({stored['artifact_version'][0]}): "
        f"{check.height:,} of {ratings[BASE].height:,} 5v5 ratings, at most {gap:.0e} apart.",
        "",
        "## Who changes",
        "",
    ]
    lineups_all = pl.concat(
        [
            lake.read("actual_lineups", seasons=[s]).select("season", "player_id", "role")
            for s in range(FIRST_LINEUPS, max(SEASONS) + 1, 10_001)
        ]
    )
    first = (
        lineups_all.filter(pl.col("role").is_in(rapm.SKATER_ROLES))
        .group_by("player_id")
        .agg(season=pl.col("season").min())
    )
    debuts = first.group_by("season").agg(debutants=pl.len())
    ends = ratings[BASE].group_by("season").agg(end=pl.col("as_of_utc").max())
    # A goalie debutant's lines turn public too, but goalies are no RAPM column.
    changed = (
        late.join(ends, on="season")
        .filter(pl.col("public_utc") < pl.col("end"))
        .join(first, on=["player_id", "season"], how="semi")
    )
    pair = (
        ratings[BASE]
        .join(
            ratings[PRIOR].select("game_id", "player_id", "component", other="prior"),
            on=["game_id", "player_id", "component"],
        )
        .filter(pl.col("component") == "ev_off", pl.col("other") != pl.col("prior"))
    )
    minutes = lineups.select("game_id", "player_id", "exp_5v5")
    share = (
        ratings[BASE]
        .filter(pl.col("component") == "ev_off")
        .join(minutes, on=["game_id", "player_id"], how="left")
        .join(
            pair.select("game_id", "player_id", changed=pl.lit(True)),
            on=["game_id", "player_id"],
            how="left",
        )
        .with_columns(pl.col("changed").fill_null(False))
        .group_by("season")
        .agg(
            games=pl.col("changed").sum(),
            share=(pl.col("exp_5v5") * pl.col("changed")).sum() / pl.col("exp_5v5").sum(),
        )
    )
    table = (
        changed.with_columns(d=pl.col("prior_late") - pl.col("prior_start"))
        .group_by("season")
        .agg(
            players=pl.len(),
            d=pl.col("d").mean(),
            lo=pl.col("d").quantile(0.1),
            hi=pl.col("d").quantile(0.9),
        )
        .join(debuts, on="season")
        .join(share, on="season")
        .filter(pl.col("season").is_in(SEASONS))
        .sort("season")
    )
    out += [
        "Each season's debutants (skaters whose first boxscore in the lake is that season's), "
        "those of them whose NHLe turns public in the season, their candidate games after it, the "
        "share of the season's projected 5v5 minutes those games hold, and the change in their "
        "5v5 offense prior (xG per hour) under a1.",
        "",
        "| Season | Debutants | With an NHLe | Candidate games changed | Their share of projected "
        "5v5 minutes | Prior change: mean [10%, 90%] |",
        "| --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for r in table.iter_rows(named=True):
        out.append(
            f"| {r['season']} | {r['debutants']} | {r['players']} | {r['games']:,} "
            f"| {r['share']:.1%} | {r['d']:+.3f} [{r['lo']:+.3f}, {r['hi']:+.3f}] |"
        )
    out += [
        "",
        "Their prior falls on average: today's prior treats a debutant like the reference skater "
        "without a recent minor-league season, and the NHLe effects (below) put most debutants "
        "below it.",
        "",
        "## The trait effects",
        "",
        "Each season's 5v5 offense effects of the NHLe traits, from the season-start fit. a1 "
        "keeps base's; a2 learns them with debut seasons coded too.",
        "",
        "| Season | nhle, base and a1 | has_nhle, base and a1 | nhle, a2 | has_nhle, a2 |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for season in SEASONS:
        a, b = runs[PRIOR].effects[season], runs[ROWS].effects[season]
        out.append(
            f"| {season} | {a['nhle']:+.3f} | {a['has_nhle']:+.3f} | {b['nhle']:+.3f} "
            f"| {b['has_nhle']:+.3f} |"
        )

    features = {k: rapm.expected_difference(v, lineups, games) for k, v in ratings.items()}
    losses = {k: tuning.scored_games(v, games, SEASONS) for k, v in features.items()}
    realized = pl.concat(
        [
            lake.read("stints", seasons=[s])
            .filter(pl.col("strength") == rapm.EV_STRENGTH, pl.col("drop_reason").is_null())
            .group_by("game_id")
            .agg(r=(pl.col("home_xg") - pl.col("away_xg")).sum())
            for s in seasons
        ]
    )
    errors = {k: xg_errors(v, realized, games) for k, v in features.items()}
    base_rows = stints[BASE]
    hours = float(base_rows["hours"].sum())
    with_debutant = float(base_rows.filter("debutant_on")["hours"].sum())
    per_hour = float((base_rows["hours"] * (base_rows["y"] - base_rows["yhat"]) ** 2).sum()) / hours
    out += [
        "",
        "## Predictions",
        "",
        "Each measure is the run's minus base's, negative when the run predicts better:",
        "- **Win log loss:** the home team's full-game win, from each game's projected 5v5 xG "
        "difference through a win model fitted on the earlier seasons (the tuning's measure, "
        "ADR 0011).",
        "- **5v5 xG difference:** the squared error of the game's realized 5v5 xG difference, "
        "predicted by a line fitted on the earlier seasons.",
        "- **5v5 stints:** each game's own 5v5 rows, predicted by the fit that rates the game, "
        "before they are read: RAPM's own loss, its hours-weighted squared error, per hour. The "
        "second line keeps the rows with a debutant on the ice whose lines were public before "
        "the game.",
        "- **Slope:** the regression of base's stint residual on the run's change of prediction, "
        "hours-weighted, through 0: 1 when the change moves the predictions as far as the data "
        "says, 0 when it carries no information.",
        "",
        f"Base: win log loss {estimate(losses[BASE], 'log_loss')}, 5v5 xG difference squared "
        f"error {estimate(errors[BASE], 'se', digits=4)}, stints' squared error per hour "
        f"{per_hour:.2f} ({hours:,.0f} hours, {with_debutant:,.0f} of them with a debutant on).",
        "",
        "| Run | Win log loss | 5v5 xG difference | 5v5 stints | 5v5 stints, debutant on "
        "| Slope | Slope, debutant on |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for kind, other in ((PRIOR, BASE), (ROWS, BASE), (ROWS, PRIOR)):
        loss = losses[other].join(losses[kind].select("game_id", run="log_loss"), on="game_id")
        loss = loss.with_columns(d=pl.col("run") - pl.col("log_loss"))
        error = errors[other].join(errors[kind].select("game_id", run="se"), on="game_id")
        error = error.with_columns(d=pl.col("run") - pl.col("se"))
        errors_, slopes = [], []
        joined = stints[other].join(
            stints[kind].select("season", "row", run="yhat"), on=["season", "row"]
        )
        assert joined.height == stints[other].height
        for rows in (joined, joined.filter("debutant_on")):
            per_game = rows.group_by("game_id", "season", "game_date").agg(
                hours=pl.col("hours").sum(),
                d=(
                    pl.col("hours")
                    * ((pl.col("y") - pl.col("run")) ** 2 - (pl.col("y") - pl.col("yhat")) ** 2)
                ).sum(),
                num=(
                    pl.col("hours")
                    * (pl.col("run") - pl.col("yhat"))
                    * (pl.col("y") - pl.col("yhat"))
                ).sum(),
                den=(pl.col("hours") * (pl.col("run") - pl.col("yhat")) ** 2).sum(),
            )
            mean_hours = float(per_game["hours"].mean())  # type: ignore[arg-type]
            errors_.append(estimate(per_game, "d", 1 / mean_hours, 4))
            slopes.append(ratio(per_game, "num", "den"))
        cells = [estimate(loss, "d"), estimate(error, "d", digits=4), *errors_, *slopes]
        name = f"{LABELS[kind]} minus {'base' if other == BASE else 'a1'}"
        out.append(f"| {name} | {' | '.join(cells)} |")
    out += [
        "",
        "The training seasons are in-sample for the frozen settings (ADR 0011), which were tuned "
        "with base's priors.",
        "",
    ]
    return "\n".join(out)


def main() -> None:
    parser = argparse.ArgumentParser(description="The evidence for ADR 0050 (#125).")
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()
    lake = Lake.from_env(mirror=False)
    games = lake.read("games")
    seasons = [
        s
        for s in sorted(games["season"].unique().to_list())
        if rapm.FIRST_SEASON <= s <= max(SEASONS)
    ]
    wanted = rapm.targets(rated(lake.read("lineups", seasons=seasons), games), games, seasons)
    roles = lake.read("actual_lineups").select("game_id", "player_id", "role")
    stints = {s: lake.read("stints", seasons=[s]) for s in seasons}
    players, lines = lake.read("players"), lake.read("player_league_seasons")
    venues = load_venues()

    def stint_seasons() -> Iterator[pl.DataFrame]:
        for season in seasons:
            yield stints[season]

    version = reports.version("debut-nhle", datetime.now(UTC))
    runs = {}
    for kind in RUNS:
        began = time.time()
        runs[kind] = rate(stint_seasons(), games, roles, venues, wanted, players, lines, kind)
        print(f"{kind}: {time.time() - began:.0f}s", flush=True)
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / f"{version}.md"
    path.write_text(report(runs, lake, games, version))
    print(path)


if __name__ == "__main__":
    main()
