"""#359's history evidence: B3's projected game total against SBR's totals, by the yardstick's
protocol (ADR 0053) and the rule fixed on the issue before any result was read (the comment of
2026-11-05).

    uv run python scripts/totals_history.py [--half explore|confirm]

**The projection,** at E2's prediction time (a second after SBR's opener at its assumed 10:00 ET),
from B3's tables read at their data's time (ys.through): each team's expected goals,
raw·κ·φ·gamma (game/b3.py), averaged over the goalie-start scenarios known then, never the starters:
- **total:** the home team's expected goals plus the away team's;
- **total split:** its four parts, each summed over both teams: 5v5 xG, power-play and
  shorthanded xG, finishing's goals beyond them raw·(κφ - 1), and the opposing goalies' beyond
  those raw·κφ·(gamma - 1). They sum to total.

**The market,** at each quote, the opener known before the prediction time and the close: the over
and under at one line, de-vigged multiplicatively (market/devig.py, hard rule 3) into p_over, and
its implied mean μ, the mean of a Poisson total T with P(T > line | no push) = p_over. Only whole
and half lines within LINES, the same on both sides.

**The measures,** each scored season fitted on the games from FIRST_FIT whose results were public
before its fold start:
- **the move:** y = μ_c - μ_o; the forecast a + b_o·μ_o + b·x against the opener's own drift
  a + b_o·μ_o, by least squares. Per game (y - ŷ)² - (y - ŷ₀)²: negative when x anticipates the
  move. The close is y's only;
- **the result beyond the close:** the over at the closing line, T the final score with the
  shootout winner's goal as books settle, pushes dropped; a logistic model on logit p_over_c, μ_c
  and x against one on logit p_over_c and μ_c. Per game the log-loss difference: negative when x
  knows something the close doesn't.

**The reading:** on the explore half, a measure supports a candidate when its interval over all
games lies below 0. A supported pair is written down in CLAIMS before a confirm run, which scores
it once, its tests one family under Holm's step-down at ALPHA (ADR 0048); a test holds when it
passes with a negative estimate. A confirm run is refused while CLAIMS is empty.

It reads the lake to its half's last season only and writes
reports/yardstick/totals-history[-confirm]-<yyyymmdd>-<shortsha>.md and .json. It writes nothing to
the lake.
"""

import argparse
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
from scipy.stats import poisson
from sklearn.linear_model import LogisticRegression

from nhl_edge.backtest import metrics, reports
from nhl_edge.backtest import yardstick as ys
from nhl_edge.game import b2, b3
from nhl_edge.lake.tables import Lake
from nhl_edge.market.devig import fair_probabilities

ALPHA = ys.ALPHA
FIRST_FIT = ys.FIRST_FIT
LINES = (3.0, 9.0)
PARTS = ("total_five", "total_special", "total_finishing", "total_goalie")
CANDIDATES: dict[str, tuple[str, ...]] = {"total": ("total",), "total split": PARTS}
MEASURES = ("move", "result")
# The claims written down from the explore half, (candidate, measure), before a confirm run.
CLAIMS: tuple[tuple[str, str], ...] = ()
# The bisection's bracket for a Poisson mean, in goals, and its steps: far below float precision.
BRACKET = (0.5, 20.0)
STEPS = 100
UTC_TYPE = pl.Datetime("us", "UTC")


def implied_mean(line: np.ndarray, p_over: np.ndarray) -> np.ndarray:
    """The mean μ of a Poisson total T with P(T > line | T != line) = p_over, by bisection: on a
    half line no total pushes, on a whole line T = line is a push and voided."""
    line = np.asarray(line, dtype=np.float64)
    p_over = np.asarray(p_over, dtype=np.float64)
    whole = line == np.floor(line)
    k = np.floor(line)
    low = np.full(line.shape, BRACKET[0])
    high = np.full(line.shape, BRACKET[1])
    for _ in range(STEPS):
        mid = (low + high) / 2
        push = np.where(whole, poisson.pmf(k, mid), 0.0)
        over = poisson.sf(k, mid) / (1 - push)
        high = np.where(over > p_over, mid, high)
        low = np.where(over > p_over, low, mid)
    return (low + high) / 2


def quotes(sbr_odds: pl.DataFrame) -> pl.DataFrame:
    """Each game's totals quotes (game_id, quote, line, p_over, mu, assumed_available_utc): the
    over and under at one line within LINES, whole or half, de-vigged multiplicatively."""
    rows = sbr_odds.filter(pl.col("market") == "totals")
    wide = rows.group_by("game_id", "quote").agg(
        line=pl.col("line").filter(pl.col("side") == "over").first(),
        under_line=pl.col("line").filter(pl.col("side") == "under").first(),
        over=pl.col("price_decimal").filter(pl.col("side") == "over").first(),
        under=pl.col("price_decimal").filter(pl.col("side") == "under").first(),
        assumed_available_utc=pl.col("assumed_available_utc").max(),
        sides=pl.len(),
    )
    kept = wide.filter(
        pl.col("sides") == 2,
        pl.col("line") == pl.col("under_line"),
        (pl.col("line") * 2) == (pl.col("line") * 2).round(0),
        pl.col("line").is_between(*LINES),
        pl.col("over").is_not_null() & pl.col("under").is_not_null(),
    ).sort("game_id", "quote")
    if kept.is_empty():
        return kept.select("game_id", "quote", "line", "assumed_available_utc").with_columns(
            p_over=pl.lit(None, pl.Float64), mu=pl.lit(None, pl.Float64)
        )
    p_over = fair_probabilities(kept.select("over", "under").to_numpy())[:, 0]
    return kept.select(
        "game_id",
        "quote",
        "line",
        "assumed_available_utc",
        p_over=pl.Series(p_over),
        mu=pl.Series(implied_mean(kept["line"].to_numpy(), p_over)),
    )


def market(sbr_odds: pl.DataFrame, moments: pl.DataFrame) -> pl.DataFrame:
    """Each game of moments with its totals opener, if assumed available before its prediction
    time, and its close: line_o, p_o, mu_o, line_c, p_c, mu_c, q_c (logit p_c) and y = mu_c -
    mu_o. The close is a target and the result model's input only."""
    found = quotes(sbr_odds)
    opens = (
        found.filter(pl.col("quote") == "open")
        .join(moments, on="game_id")
        .filter(pl.col("assumed_available_utc") < pl.col("prediction_utc"))
        .select("game_id", line_o="line", p_o="p_over", mu_o="mu")
    )
    closes = found.filter(pl.col("quote") == "close").select(
        "game_id", line_c="line", p_c="p_over", mu_c="mu"
    )
    return (
        opens.join(closes, on="game_id")
        .with_columns(
            q_c=(pl.col("p_c") / (1 - pl.col("p_c"))).log(), y=pl.col("mu_c") - pl.col("mu_o")
        )
        .sort("game_id")
    )


def pairs(split: pl.DataFrame, pool: pl.DataFrame, gammas: pl.DataFrame) -> pl.DataFrame:
    """Every pair of candidate starters for each game of split, as b3.scenarios builds them: its
    weight and each side's goals, raw·κφ·gamma against the opposing candidate (gamma 1 for a goalie
    without one, and one average goalie at weight 1 for a team without candidates)."""
    frame = split.select(
        "game_id", "home", "away", "home_raw", "home_base", "away_raw", "away_base"
    )
    for side in ("home", "away"):
        picks = pool.select(
            "game_id",
            pl.col("team").alias(side),
            pl.col("goalie_id").alias(f"{side}_goalie"),
            pl.col("p_start").alias(f"{side}_p"),
        )
        frame = frame.join(picks, on=["game_id", side], how="left").with_columns(
            pl.col(f"{side}_p").fill_null(1.0)
        )
    frame = b3.with_gamma(frame, gammas, "home", "away_goalie")
    frame = b3.with_gamma(frame, gammas, "away", "home_goalie")
    home = pl.col("home_raw") * pl.col("home_base")
    away = pl.col("away_raw") * pl.col("away_base")
    return frame.select(
        "game_id",
        weight=pl.col("home_p") * pl.col("away_p"),
        home_goals=home * pl.col("home_gamma"),
        away_goals=away * pl.col("away_gamma"),
        total_goalie=home * (pl.col("home_gamma") - 1) + away * (pl.col("away_gamma") - 1),
    )


def projection(
    split: pl.DataFrame, pool: pl.DataFrame, gammas: pl.DataFrame, moments: pl.DataFrame
) -> pl.DataFrame:
    """Each game of moments whose inputs were known before its prediction time, with total and
    its parts (PARTS), the goalie terms averaged over the start scenarios known then. split is
    b3.split_inputs of b3.game_inputs, pool the goalie candidates with p_start and observed_utc,
    and gammas b3.multipliers' second table."""
    usable, ready = b2.known_before(split, pool, moments, "observed_utc")
    weight = pl.col("weight")
    goalies = (
        pairs(usable, ready, gammas)
        .group_by("game_id")
        .agg(
            *(
                (pl.col(c) * weight).sum().truediv(weight.sum()).alias(c)
                for c in ("home_goals", "away_goals", "total_goalie")
            )
        )
    )
    return (
        usable.select(
            "game_id",
            total_five=pl.col("home_five") + pl.col("away_five"),
            total_special=pl.col("home_special") + pl.col("away_special"),
            total_finishing=pl.col("home_raw") * (pl.col("home_base") - 1)
            + pl.col("away_raw") * (pl.col("away_base") - 1),
        )
        .join(goalies, on="game_id")
        .select(
            "game_id",
            total=pl.col("home_goals") + pl.col("away_goals"),
            home_goals="home_goals",
            away_goals="away_goals",
            **{c: c for c in PARTS},
        )
        .sort("game_id")
    )


def expected_goals(tables: b3.Tables, moments: pl.DataFrame) -> pl.DataFrame:
    """projection from B3's tables, as ys.features reads them."""
    split = b3.split_inputs(b3.game_inputs(tables))
    pool = tables.goalie_starts.select("game_id", "team", "goalie_id", "p_start", "observed_utc")
    _, gammas = b3.multipliers(tables)
    return projection(split, pool, gammas, moments)


def results(games: pl.DataFrame) -> pl.DataFrame:
    """Each game's final total, the shootout winner's goal counted as the scores count it, and
    when it became public."""
    return games.select(
        "game_id",
        goals=(pl.col("home_score") + pl.col("away_score")).cast(pl.Int32),
        result_utc="observed_utc",
    )


def rows(
    base: pl.DataFrame, totals: pl.DataFrame, found: pl.DataFrame, games: pl.DataFrame
) -> pl.DataFrame:
    """The scored games: the yardstick's (base: game_id, season, game_date, start_utc,
    prediction_utc, rested) with their totals market, projection and result, and the over at the
    closing line (null on a push)."""
    goals = pl.col("goals").cast(pl.Float64)
    return (
        base.select("game_id", "season", "game_date", "start_utc", "prediction_utc", "rested")
        .join(totals, on="game_id")
        .join(found, on="game_id")
        .join(results(games), on="game_id")
        .with_columns(
            over=pl.when(goals > pl.col("line_c"))
            .then(1)
            .when(goals < pl.col("line_c"))
            .then(0)
            .otherwise(None)
            .cast(pl.Int8)
        )
        .sort("game_id")
    )


def _least_squares(train: pl.DataFrame, test: pl.DataFrame, columns: Sequence[str]) -> np.ndarray:
    def design(frame: pl.DataFrame) -> np.ndarray:
        return np.column_stack([np.ones(frame.height), frame.select(columns).to_numpy()])

    beta, *_ = np.linalg.lstsq(design(train), train["y"].to_numpy(), rcond=None)
    return design(test) @ beta


def _logistic(train: pl.DataFrame, test: pl.DataFrame, columns: Sequence[str]) -> np.ndarray:
    # Unpenalized, as the yardstick's result model: C is the inverse of the L2 strength.
    model = LogisticRegression(C=np.inf, max_iter=1000)
    model.fit(train.select(columns).to_numpy(), train["over"].to_numpy())
    return model.predict_proba(test.select(columns).to_numpy())[:, 1]


def scored(frame: pl.DataFrame, columns: Sequence[str], seasons: Sequence[int]) -> pl.DataFrame:
    """Each game of the seasons with its move skill (move) and, off a push, its result beyond the
    close (result), each season fitted on the games from FIRST_FIT to the season before whose
    results were public before its fold start (ys.fold_start). Each row carries its fit's
    train_cutoff, the last result it read."""
    out = []
    for season in sorted(s for s in seasons if s > FIRST_FIT):
        start = ys.fold_start(frame, season)
        train = frame.filter(
            pl.col("season").is_between(FIRST_FIT, season - 1), pl.col("result_utc") < start
        )
        decided = train.filter(pl.col("over").is_not_null())
        if train.height < 2 or decided["over"].n_unique() < 2:
            raise ValueError(f"no earlier games to fit on before {season}")
        test = frame.filter(pl.col("season") == season)
        market_terms = ["q_c", "mu_c"]
        p = _logistic(decided, test, [*market_terms, *columns])
        p_base = _logistic(decided, test, market_terms)
        out.append(
            test.select("game_id", "season", "game_date", "rested", "y", "over").with_columns(
                y_hat=pl.Series(_least_squares(train, test, ["mu_o", *columns])),
                y_base=pl.Series(_least_squares(train, test, ["mu_o"])),
                p=pl.Series(p),
                p_base=pl.Series(p_base),
                train_cutoff=pl.lit(train["result_utc"].max(), dtype=UTC_TYPE),
            )
        )
    games = pl.concat(out)
    over = pl.col("over")
    return games.with_columns(
        move=(pl.col("y") - pl.col("y_hat")) ** 2 - (pl.col("y") - pl.col("y_base")) ** 2,
        result=pl.when(over.is_not_null()).then(
            metrics.log_loss(pl.col("p"), over) - metrics.log_loss(pl.col("p_base"), over)
        ),
    ).sort("game_id")


def _estimate(frame: pl.DataFrame, value: str) -> dict[str, Any]:
    part = frame.filter(pl.col(value).is_not_null())
    return {**metrics.bootstrap(part, value).to_dict(), "p_value": metrics.p_value(part, value)}


def figures(games: pl.DataFrame) -> dict[str, Any]:
    """A candidate's figures, each with its weekly block bootstrap interval (hard rule 7): both
    measures over all games, on the rested games and per season."""
    rested = games.filter(pl.col("rested"))
    out: dict[str, Any] = {"games": games.height, "rested_games": rested.height}
    for measure in MEASURES:
        out[measure] = _estimate(games, measure)
        out[f"{measure}_rested"] = _estimate(rested, measure)
        out[f"{measure}_per_season"] = {
            str(season): _estimate(part, measure)
            for (season,), part in games.sort("season").group_by("season", maintain_order=True)
        }
    out["baseline_error"] = float(((games["y"] - games["y_base"]) ** 2).mean())  # type: ignore[arg-type]
    out["pushes"] = games.filter(pl.col("over").is_null()).height
    return out


def described(frame: pl.DataFrame) -> dict[str, Any]:
    """Descriptive figures over the scored games, never tested: the projection's and the
    market's means, and the projection's correlation with the opener, the close and the result."""

    def corr(a: str, b: str) -> float:
        return float(frame.select(pl.corr(a, b)).item())

    return {
        "games": frame.height,
        "mean_total": float(frame["total"].mean()),  # type: ignore[arg-type]
        "mean_mu_o": float(frame["mu_o"].mean()),  # type: ignore[arg-type]
        "mean_mu_c": float(frame["mu_c"].mean()),  # type: ignore[arg-type]
        "mean_goals": float(frame["goals"].mean()),  # type: ignore[arg-type]
        "moved_line": frame.filter(pl.col("line_o") != pl.col("line_c")).height,
        "corr_total_mu_o": corr("total", "mu_o"),
        "corr_total_mu_c": corr("total", "mu_c"),
        "corr_total_goals": corr("total", "goals"),
        "corr_mu_c_goals": corr("mu_c", "goals"),
    }


def run(frame: pl.DataFrame, half: str) -> tuple[pl.DataFrame, dict[str, Any]]:
    """Every candidate scored on the half's seasons: per-game rows and figures. It reads no game
    of a season after the half's last."""
    seasons = ys.seasons_of(half)
    frame = frame.filter(pl.col("season").is_between(FIRST_FIT, max(seasons)))
    per_game, found = [], {}
    for name, columns in CANDIDATES.items():
        games = scored(frame, columns, seasons)
        found[name] = figures(games)
        per_game.append(
            games.select(pl.lit(name).alias("candidate"), "game_id", *MEASURES, "train_cutoff")
        )
    scored_games = frame.filter(pl.col("season").is_in(seasons))
    found["described"] = described(scored_games)
    return pl.concat(per_game), found


def supported(found: Mapping[str, Any]) -> list[tuple[str, str]]:
    """The explore half's supported pairs: a measure's interval over all games below 0."""
    return [
        (name, measure)
        for name in CANDIDATES
        for measure in MEASURES
        if found[name][measure]["high"] < 0
    ]


def verdict(
    found: Mapping[str, Any], claims: Sequence[tuple[str, str]] = CLAIMS
) -> list[dict[str, Any]]:
    """A confirm run's verdict on the claims: Holm's step-down at ALPHA over their two-sided
    p-values (ADR 0048); a test holds when it passes and its estimate is negative."""
    if not claims:
        raise ValueError("no claim written down: a confirm run needs CLAIMS")
    steps = metrics.holm({f"{c}: {m}": found[c][m]["p_value"] for c, m in claims}, ALPHA)
    out = []
    for candidate, measure in claims:
        estimate, step = found[candidate][measure], steps[f"{candidate}: {measure}"]
        out.append(
            {
                "test": f"{candidate}: {measure}",
                "mean": estimate["mean"],
                "low": estimate["low"],
                "high": estimate["high"],
                "p_value": step["p_value"],
                "threshold": step["threshold"],
                "passes": step["passes"],
                "holds": step["passes"] and estimate["mean"] < 0,
            }
        )
    return out


def _cell(estimate: Mapping[str, Any], scale: float = 1000.0) -> str:
    return (
        f"{estimate['mean'] * scale:+.2f} [{estimate['low'] * scale:+.2f}, "
        f"{estimate['high'] * scale:+.2f}]"
    )


def markdown(
    found: Mapping[str, Any], half: str, version: str, tests: Sequence[Mapping[str, Any]] = ()
) -> str:
    """The report: each candidate's measures, in thousandths, with their intervals."""
    seasons = ys.seasons_of(half)
    d = found["described"]
    lines = [
        f"# #359: B3's projected total against SBR's totals, {half} half ({version})",
        "",
        f"Scored seasons {', '.join(str(s) for s in seasons)}, each fitted on the games from "
        f"{FIRST_FIT} whose results were public before its fold start (ADR 0053). By the rule on "
        "#359 of 2026-11-05. Figures are means in thousandths with weekly block bootstrap 95% "
        "intervals; negative favours the projection.",
        "",
        "- **move:** the squared-error skill on y = μ_c - μ_o, the implied mean total's move "
        "from the opener to the close, against the opener's own drift (goals²).",
        "- **result:** the log-loss difference of the over at the closing line, pushes dropped, "
        "beyond logit p_over_c and μ_c.",
        "",
        "| Candidate | Measure | All games | p | Rested games | p |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for name in CANDIDATES:
        for measure in MEASURES:
            whole, rested = found[name][measure], found[name][f"{measure}_rested"]
            lines.append(
                f"| {name} | {measure} | {_cell(whole)} | {whole['p_value']:.4f} "
                f"| {_cell(rested)} | {rested['p_value']:.4f} |"
            )
    lines += ["", "## Per season", "", "| Candidate | Measure | Season | Estimate | Games |"]
    lines.append("| --- | --- | --- | --- | --- |")
    for name in CANDIDATES:
        for measure in MEASURES:
            for season, estimate in found[name][f"{measure}_per_season"].items():
                lines.append(
                    f"| {name} | {measure} | {season} | {_cell(estimate)} | {estimate['games']} |"
                )
    first = found[next(iter(CANDIDATES))]
    lines += [
        "",
        "## Described, not tested",
        "",
        f"- {d['games']} games, {first['rested_games']} rested; {first['pushes']} pushes at the "
        f"closing line; the line moved from the opener to the close on {d['moved_line']}.",
        f"- Mean projected total {d['mean_total']:.3f}; mean implied total {d['mean_mu_o']:.3f} "
        f"at the opener and {d['mean_mu_c']:.3f} at the close; mean final total "
        f"{d['mean_goals']:.3f}.",
        f"- The projection's correlation with μ_o {d['corr_total_mu_o']:.3f}, with μ_c "
        f"{d['corr_total_mu_c']:.3f}, with the final total {d['corr_total_goals']:.3f}; μ_c's "
        f"with the final total {d['corr_mu_c_goals']:.3f}.",
        f"- The opener's drift leaves a squared error of {first['baseline_error']:.5f} goals² "
        "on the move.",
    ]
    if half == "explore":
        found_pairs = supported(found)
        lines += [
            "",
            "## Supported on the explore half",
            "",
            (
                "None: no measure's interval over all games lies below 0, so no claim and no "
                "confirm run (the rule on #359)."
                if not found_pairs
                else "These pairs' intervals lie below 0; each is a claim for the confirm half: "
                + ", ".join(f"{c}: {m}" for c, m in found_pairs)
                + "."
            ),
        ]
    if tests:
        lines += [
            "",
            "## The claim on the confirm half",
            "",
            f"One family, Holm's step-down at {ALPHA} (ADR 0048). A test holds when it passes "
            "and its estimate is negative.",
            "",
            "| Test | Estimate (thousandths) | p-value | Holm threshold | Holds |",
            "| --- | --- | --- | --- | --- |",
        ]
        for test in tests:
            lines.append(
                f"| {test['test']} | {_cell(test)} | {test['p_value']:.4f} "
                f"| {test['threshold']:.4f} | {'yes' if test['holds'] else 'no'} |"
            )
    lines += [
        "",
        "Caveats: B3's settings were tuned on these seasons' moneyline results (ADR 0011), so "
        "this is no clean holdout; SBR's opener may be from the evening before (ADR 0006), so the "
        "rested games are shown beside all.",
    ]
    return "\n".join(lines) + "\n"


def frame_of(read: ys.Read, sbr_odds: pl.DataFrame) -> pl.DataFrame:
    """The scored games from the yardstick's read and SBR's prices."""
    moments = read.frame.select("game_id", "prediction_utc")
    return rows(
        read.frame,
        market(sbr_odds, moments),
        expected_goals(read.b3_tables, moments),
        read.games,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="B3's projected total against SBR's (#359).")
    parser.add_argument("--half", choices=sorted(ys.HALVES), default="explore")
    parser.add_argument("--out", type=Path, default=Path("reports/yardstick"))
    args = parser.parse_args()
    if args.half == "confirm" and not CLAIMS:
        raise SystemExit("no claim written down: a confirm run needs CLAIMS")
    last = max(ys.seasons_of(args.half))
    lake = Lake()
    read = ys.read(lake, last)
    sbr_odds = lake.read("sbr_odds").filter(pl.col("season") <= last)
    _, found = run(frame_of(read, sbr_odds), args.half)
    tests = verdict(found) if args.half == "confirm" else []
    name = "totals-history" if args.half == "explore" else "totals-history-confirm"
    version = reports.version(name, datetime.now(UTC))
    args.out.mkdir(parents=True, exist_ok=True)
    payload = {"figures": found, "claims": [list(c) for c in CLAIMS], "verdict": tests}
    (args.out / f"{version}.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str)
    )
    path = args.out / f"{version}.md"
    path.write_text(markdown(found, args.half, version, tests))
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
