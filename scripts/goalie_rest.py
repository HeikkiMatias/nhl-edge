"""#418 (#417): does a goalie's own rest add to his rating? Each definition was fixed on the issue
before any figure was read (the comment of 2026-11-11).

    uv run python scripts/goalie_rest.py

**A goalie's starts:** every game he started, for any team (the boxscore's starter flag in
actual_lineups). A start counts only when it is on an earlier date than tonight and its boxscore
was public before the as-of time.

**Step 1, the shot level** (a measurement on the goalie who played, not a forecast input):
- the unblocked shots shot_xg scores, faced by the defending team's starter;
- an unpenalized logistic regression of the goal on logit(xG), his log gamma (B3's conversion
  multiplier for the shooting team against him, ADR 0022, known before the start; 0 when he was
  no candidate), the defending team at home and the defending team's back-to-back (the base);
- the full model adds his rest: days since his last start, one-hot as 1, 3, 4 to 6 and 7 or more
  against 2 (no earlier start counts as 7 or more), and his starts in the 7 days before;
- each explore season is fitted on the shots from FIRST_FIT to the season before, public before
  its fold start, and the measure is the per-shot log loss, full less base.

**Step 2, the game level** (the yardstick's explore half at E2's prediction time, ADR 0053):
- each candidate's rest at the prediction time, from his starts as above: whether he started
  yesterday, his starts in the 7 days before and the days since his last start, capped at 7;
- each side's value is the average over its candidates by p_start, and each column is home less
  away: goalie_b2b_diff, starts7_diff and rest_days_diff;
- the baseline is B3's goalie part plus the two teams' back-to-back flags. goalie_workload adds
  starts7_diff and rest_days_diff, goalie_b2b adds goalie_b2b_diff, and each is paired against
  the baseline on the move skill.

**The decision:** Holm's step-down at ALPHA over the three tests. A test holds when it passes with
a negative estimate. goalie_workload is kept when it holds and its rested interval lies below 0;
goalie_b2b when it holds and step 1 holds, since it lives on back-to-backs only, where an opener
of the evening before may predate last night's game (ADR 0006). A kept candidate goes to the owner
as a challenger input, as development evidence only (the confirm half is spent, ADR 0053).

It reads the lake to the explore half's last season only, and writes
reports/yardstick/goalie-rest-<yyyymmdd>-<shortsha>.md. It writes nothing to the lake.
"""

import argparse
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
from sklearn.linear_model import LogisticRegression

from nhl_edge.backtest import metrics, reports
from nhl_edge.backtest import yardstick as ys
from nhl_edge.game import b2, b3
from nhl_edge.lake.tables import Lake, Reader

ALPHA = ys.ALPHA
FAMILY = ("shots", "goalie_workload", "goalie_b2b")
# The days before tonight's date whose starts count as recent, and the cap on days of rest.
WINDOW = 7
CAP = 7
# Days since the last start, as step 1's bands, with 2 days as the reference.
BANDS = ("1", "2", "3", "4-6", "7+")
REFERENCE = "2"
BAND_COLUMNS = {"1": "band_1", "3": "band_3", "4-6": "band_4_6", "7+": "band_7_plus"}
SHOT_BASE = ("logit_xg", "log_gamma", "defending_home", "team_b2b")
SHOT_REST = (*BAND_COLUMNS.values(), "starts7")
GAME_BASE = ("goalie_diff", "home_back_to_back", "away_back_to_back")
WORKLOAD = ("starts7_diff", "rest_days_diff")
B2B = ("goalie_b2b_diff",)
CANDIDATES: dict[str, tuple[str, ...]] = {
    "goalie base": GAME_BASE,
    "goalie_workload": (*GAME_BASE, *WORKLOAD),
    "goalie_b2b": (*GAME_BASE, *B2B),
    # Descriptive, never part of the decision: each new column alone, for the move's slope on it.
    **{name: (name,) for name in (*WORKLOAD, *B2B)},
}
# Each game-level family member's primary measure: its move skill less the baseline's.
PRIMARY = {
    "goalie_workload": ("goalie_workload", "goalie base"),
    "goalie_b2b": ("goalie_b2b", "goalie base"),
}
CALIBRATED = ("goalie base", "goalie_workload", "goalie_b2b")
# A shot's xG is kept this far from 0 and 1 before its log-odds are taken.
XG_EDGE = 1e-6


def starts(lineups: pl.DataFrame) -> pl.DataFrame:
    """Every start in the boxscores: the goalie, its date and when its boxscore became public."""
    return lineups.filter(pl.col("starting_goalie")).select(
        goalie_id="player_id", start_date="game_date", start_utc="observed_utc"
    )


def rest(asks: pl.DataFrame, started: pl.DataFrame) -> pl.DataFrame:
    """asks (goalie_id, game_date, as_of_utc and any keys) with each goalie's rest, from his starts
    (starts) on earlier dates whose boxscores were public before as_of_utc: yesterday (1.0 when he
    started the day before), starts7 (his starts in the WINDOW days before) and days (since his
    last start, null when he has none)."""
    indexed = asks.with_row_index("ask")
    known = (
        indexed.select("ask", "goalie_id", "game_date", "as_of_utc")
        .join(started, on="goalie_id")
        .filter(
            pl.col("start_date") < pl.col("game_date"),
            pl.col("start_utc") < pl.col("as_of_utc"),
        )
        .with_columns(back=(pl.col("game_date") - pl.col("start_date")).dt.total_days())
        .group_by("ask")
        .agg(
            days=pl.col("back").min(),
            starts7=(pl.col("back") <= WINDOW).sum(),
            yesterday=(pl.col("back") == 1).any(),
        )
    )
    return (
        indexed.join(known, on="ask", how="left")
        .with_columns(
            starts7=pl.col("starts7").fill_null(0).cast(pl.Float64),
            yesterday=pl.col("yesterday").fill_null(False).cast(pl.Float64),
        )
        .drop("ask")
    )


def band(days: pl.Expr) -> pl.Expr:
    """Step 1's band of days since the last start; no earlier start counts as 7 or more."""
    return (
        pl.when(days == 1)
        .then(pl.lit("1"))
        .when(days == 2)
        .then(pl.lit("2"))
        .when(days == 3)
        .then(pl.lit("3"))
        .when(days <= 6)
        .then(pl.lit("4-6"))
        .otherwise(pl.lit("7+"))
    )


def played_yesterday(games: pl.DataFrame) -> pl.DataFrame:
    """Each team-game (game_id, team) with team_b2b: 1.0 when the team played the day before."""
    sides = pl.concat(
        [games.select("game_id", "game_date", team=pl.col(side)) for side in ("home", "away")]
    )
    days = (
        sides.select("team", next_day=pl.col("game_date") + pl.duration(days=1))
        .unique()
        .with_columns(team_b2b=pl.lit(1.0))
    )
    return sides.join(
        days, left_on=["team", "game_date"], right_on=["team", "next_day"], how="left"
    ).select("game_id", "team", team_b2b=pl.col("team_b2b").fill_null(0.0))


def shot_rows(
    shots: pl.DataFrame, shot_xg: pl.DataFrame, read: ys.Read
) -> tuple[pl.DataFrame, dict[str, int]]:
    """Step 1's shots: each unblocked shot shot_xg scores whose goalie was the defending team's
    starter, from FIRST_FIT to the last season of read, with its goal, its xG, the base terms
    and his rest, each known before the game's start; and the counts left out on the way."""
    last = int(read.games["season"].max())  # type: ignore[arg-type]
    seasons = pl.col("season").is_between(ys.FIRST_FIT, last)
    games = read.games.filter(seasons).select("game_id", "game_date", "start_utc", "home", "away")
    lineups = read.b2_tables.actual_lineups
    starters = lineups.filter(pl.col("starting_goalie")).select(
        "game_id", defending="team", goalie_id="player_id"
    )
    scored = shots.filter(seasons).join(
        shot_xg.filter(seasons).select("game_id", "event_id", "xg", xg_cutoff="train_cutoff"),
        on=["game_id", "event_id"],
    )
    frame = (
        scored.drop("game_date")
        .join(games, on="game_id")
        .with_columns(
            defending=pl.when(pl.col("is_home")).then(pl.col("away")).otherwise(pl.col("home")),
            defending_home=(~pl.col("is_home")).cast(pl.Float64),
        )
        .join(starters, on=["game_id", "defending", "goalie_id"])
    )
    fitted_late = frame.filter(pl.col("xg_cutoff") >= pl.col("start_utc")).height
    frame = frame.filter(pl.col("xg_cutoff") < pl.col("start_utc"))
    gammas = read.b3_tables.goal_multipliers.filter(pl.col("goalie_id").is_not_null()).select(
        "game_id", "team", "goalie_id", "gamma", gamma_utc="observed_utc"
    )
    asks = frame.select("game_id", "goalie_id", "game_date", as_of_utc="start_utc").unique()
    rested = rest(asks, starts(lineups)).select("game_id", "goalie_id", "days", "starts7")
    frame = (
        frame.join(gammas, on=["game_id", "team", "goalie_id"], how="left")
        .join(rested, on=["game_id", "goalie_id"])
        .join(
            played_yesterday(games), left_on=["game_id", "defending"], right_on=["game_id", "team"]
        )
    )
    known = pl.col("gamma").is_not_null() & (pl.col("gamma_utc") < pl.col("start_utc"))
    xg = pl.col("xg").clip(XG_EDGE, 1 - XG_EDGE)
    out = frame.with_columns(
        goal=pl.col("is_goal").cast(pl.Float64),
        logit_xg=(xg / (1 - xg)).log(),
        log_gamma=pl.when(known).then(pl.col("gamma").log()).otherwise(0.0),
        band=band(pl.col("days")),
    ).with_columns(
        *(
            (pl.col("band") == label).cast(pl.Float64).alias(column)
            for label, column in BAND_COLUMNS.items()
        )
    )
    counts = {
        "xg_fitted_at_or_after_the_start": fitted_late,
        "starters_without_a_known_gamma": out.filter(~known)
        .select("game_id", "goalie_id")
        .unique()
        .height,
    }
    return (
        out.select(
            "game_id",
            "season",
            "game_date",
            "goalie_id",
            "observed_utc",
            "goal",
            "xg",
            "band",
            *SHOT_BASE,
            *SHOT_REST,
        ).sort("game_id", "goalie_id"),
        counts,
    )


def _fit(train: pl.DataFrame, test: pl.DataFrame, columns: Sequence[str]) -> np.ndarray:
    # An unpenalized fit, as the yardstick's result model: C is the inverse of the L2 strength.
    model = LogisticRegression(C=np.inf, max_iter=1000)
    model.fit(train.select(columns).to_numpy(), train["goal"].to_numpy())
    return model.predict_proba(test.select(columns).to_numpy())[:, 1]


def shot_scores(shots: pl.DataFrame, frame: pl.DataFrame) -> pl.DataFrame:
    """Each explore season's shots with the base and full models' probabilities and the per-shot
    log loss, full less base (difference): each season fitted on the shots from FIRST_FIT to the
    season before whose results were public before its fold start (ys.fold_start on the
    yardstick's rows, frame), and the fit's train_cutoff, the last result it read."""
    out = []
    for season in ys.seasons_of("explore"):
        start = ys.fold_start(frame, season)
        train = shots.filter(
            pl.col("season").is_between(ys.FIRST_FIT, season - 1), pl.col("observed_utc") < start
        )
        if train.is_empty():
            raise ValueError(f"no earlier shots to fit on before {season}")
        test = shots.filter(pl.col("season") == season)
        out.append(
            test.with_columns(
                p_base=pl.Series(_fit(train, test, SHOT_BASE)),
                p_full=pl.Series(_fit(train, test, (*SHOT_BASE, *SHOT_REST))),
                train_cutoff=pl.lit(train["observed_utc"].max(), dtype=ys.UTC),
            )
        )
    return pl.concat(out).with_columns(
        difference=metrics.log_loss(pl.col("p_full"), pl.col("goal"))
        - metrics.log_loss(pl.col("p_base"), pl.col("goal"))
    )


def by_band(scored: pl.DataFrame) -> dict[str, Any]:
    """Descriptive: each band's starter-games and shots, and its goals less xG per 100 shots."""
    out: dict[str, Any] = {}
    for label in BANDS:
        part = scored.filter(pl.col("band") == label).with_columns(
            above=(pl.col("goal") - pl.col("xg")) * 100
        )
        out[label] = {
            "starter_games": part.select("game_id", "goalie_id").unique().height,
            "shots": part.height,
            "goals_less_xg_per_100": metrics.bootstrap(part, "above").to_dict()
            if part.height
            else None,
        }
    return out


def rest_columns(read: ys.Read) -> tuple[pl.DataFrame, int]:
    """Each yardstick game's rest columns with the two back-to-back flags, all known before its
    prediction time: goalie_b2b_diff, starts7_diff and rest_days_diff, each side's candidates
    (goalie_starts, as the yardstick averages its goalie part) averaged by p_start, home less
    away; and the games left out because a side had no candidate known then."""
    moments = read.frame.select("game_id", "prediction_utc")
    usable, _ = b2.known_before(
        b2.game_inputs(read.b2_tables), b2.candidates(read.b2_tables), moments, "observed_utc"
    )
    flags = usable.select("game_id", "home", "away", "home_back_to_back", "away_back_to_back")
    pool = read.b3_tables.goalie_starts.select(
        "game_id", "team", "goalie_id", "p_start", "observed_utc"
    )
    split = b3.split_inputs(b3.game_inputs(read.b3_tables))
    _, ready = b2.known_before(split, pool, moments, "observed_utc")
    asks = ready.join(read.games.select("game_id", "game_date"), on="game_id").select(
        "game_id", "team", "goalie_id", "p_start", "game_date", as_of_utc="prediction_utc"
    )
    found = rest(asks, starts(read.b2_tables.actual_lineups)).with_columns(
        rest_days=pl.col("days").fill_null(CAP).clip(upper_bound=CAP).cast(pl.Float64)
    )
    weight = pl.col("p_start")
    sides = found.group_by("game_id", "team").agg(
        *(
            ((pl.col(c) * weight).sum() / weight.sum()).alias(c)
            for c in ("yesterday", "starts7", "rest_days")
        )
    )
    out = flags
    for side in ("home", "away"):
        out = out.join(
            sides.rename({"team": side, **{c: f"{side}_{c}" for c in sides.columns[2:]}}),
            on=["game_id", side],
        )
    columns = out.select(
        "game_id",
        "home_back_to_back",
        "away_back_to_back",
        goalie_b2b_diff=pl.col("home_yesterday") - pl.col("away_yesterday"),
        starts7_diff=pl.col("home_starts7") - pl.col("away_starts7"),
        rest_days_diff=pl.col("home_rest_days") - pl.col("away_rest_days"),
    ).sort("game_id")
    return columns, flags.height - columns.height


def with_candidates(read: ys.Read) -> tuple[pl.DataFrame, int]:
    """The yardstick's rows with the rest columns and the back-to-back flags, and the games left
    out for want of a candidate."""
    columns, missing = rest_columns(read)
    return read.frame.join(columns, on="game_id").sort("game_id"), missing


def paired_frame(per_game: pl.DataFrame, a: str, b: str, measure: str = "move") -> pl.DataFrame:
    """a's measure less b's, game by game on the games both scored (difference)."""
    left = per_game.filter(pl.col("candidate") == a)
    right = per_game.filter(pl.col("candidate") == b).select("game_id", other=measure)
    return left.join(right, on="game_id").with_columns(difference=pl.col(measure) - pl.col("other"))


def calibration(frame: pl.DataFrame) -> dict[str, Any]:
    """Shown beside: the calibration slope of the result model's probabilities (the close and
    the candidate's columns) for each family candidate and its baseline, and of the close alone,
    on the explore half's games."""
    seasons = ys.seasons_of("explore")
    frame = frame.filter(pl.col("season").is_between(ys.FIRST_FIT, max(seasons)))
    out: dict[str, Any] = {}
    for name in CALIBRATED:
        games = ys.scored(frame, CANDIDATES[name], seasons)
        out[name] = metrics.calibration(games, "p", "home_win")["slope"].to_dict()
        if "the close alone" not in out:
            close = metrics.calibration(games, "p_base", "home_win")["slope"]
            out["the close alone"] = close.to_dict()
    return out


def decide(per_game: pl.DataFrame, shots: pl.DataFrame) -> dict[str, Any]:
    """The family's three estimates, their p-values, Holm's step-down at ALPHA, whether each test
    holds (passes with a negative estimate) and whether each game-level candidate is kept."""
    estimates: dict[str, dict[str, Any]] = {
        "shots": {
            "primary": metrics.bootstrap(shots, "difference").to_dict(),
            "p_value": metrics.p_value(shots, "difference"),
        }
    }
    for name, (a, b) in PRIMARY.items():
        both = paired_frame(per_game, a, b)
        estimates[name] = {
            "primary": metrics.bootstrap(both, "difference").to_dict(),
            "p_value": metrics.p_value(both, "difference"),
            "rested": metrics.bootstrap(both.filter(pl.col("rested")), "difference").to_dict(),
            "back_to_back": metrics.bootstrap(
                both.filter(~pl.col("rested")), "difference"
            ).to_dict(),
            "result": metrics.bootstrap(
                paired_frame(per_game, a, b, "result"), "difference"
            ).to_dict(),
        }
    steps = metrics.holm({n: e["p_value"] for n, e in estimates.items()}, ALPHA)
    for name, entry in estimates.items():
        entry |= {
            "threshold": steps[name]["threshold"],
            "holds": steps[name]["passes"] and entry["primary"]["mean"] < 0,
        }
    workload, back = estimates["goalie_workload"], estimates["goalie_b2b"]
    workload["kept"] = workload["holds"] and workload["rested"]["high"] < 0
    back["kept"] = back["holds"] and estimates["shots"]["holds"]
    return {"family": list(FAMILY), "estimates": estimates}


def _thousandths(estimate: Mapping[str, Any] | None, digits: int = 3) -> str:
    if estimate is None:
        return "none"
    return (
        f"{estimate['mean'] * 1000:+.{digits}f} [{estimate['low'] * 1000:+.{digits}f}, "
        f"{estimate['high'] * 1000:+.{digits}f}]"
    )


def _plain(estimate: Mapping[str, Any] | None) -> str:
    if estimate is None:
        return "none"
    mean = estimate.get("mean", estimate.get("value"))
    return f"{mean:+.3f} [{estimate['low']:+.3f}, {estimate['high']:+.3f}]"


def markdown(found: Mapping[str, Any], version: str) -> str:
    step1, decision = found["shots"], found["decision"]["estimates"]
    lines = [
        f"# Goalie rest and workload ({version})",
        "",
        "#418 (#417), on the yardstick's explore half (2013-14 and 2014-15, fitted from 2012-13),",
        "each definition fixed on the issue before any figure was read (the comment of",
        "2026-11-11). Intervals are 95% weekly block bootstrap. Not fresh: #361, #366 and #370",
        "read these seasons, so every p-value is development evidence only.",
        "",
        "## Step 1: the shot level",
        "",
        f"{step1['shots']:,} unblocked shots, faced by the starters of {step1['starter_games']:,}",
        "team-games. Per-shot log loss of the full model (with his rest) less the base, in",
        "thousandths:",
        f"**{_thousandths(decision['shots']['primary'], 4)}**. Negative favours the rest terms.",
        "",
        "Left out on the way: "
        + ", ".join(f"{k.replace('_', ' ')} {v:,}" for k, v in step1["counts"].items())
        + ".",
        "",
        "**Descriptive, not part of the rule:** goals less xG per 100 unblocked shots by the days",
        "since the starter's last start (positive means more goals than xG expected):",
        "",
        "| Days since his last start | Starter-games | Shots | Goals less xG per 100 shots |",
        "|---|---|---|---|",
    ]
    for label, entry in step1["bands"].items():
        lines.append(
            f"| {label} | {entry['starter_games']:,} | {entry['shots']:,} | "
            f"{_plain(entry['goals_less_xg_per_100'])} |"
        )
    lines += [
        "",
        "## Step 2: the game level",
        "",
        "Games left out because a side had no candidate known at the prediction: "
        f"{found['missing_games']:,}.",
        "",
        "Rested games where a candidate started yesterday, for the team he had moved to (a",
        "start counts for any team, and goalie_starts can keep a traded goalie as a candidate of",
        f"his old team): {found['stale_rested']:,} of the explore half's.",
        "",
        "Move skill and result beyond the close in thousandths: negative means the candidate",
        "anticipates SBR's move beyond the opener's drift.",
        "",
        "| Candidate | Games | Move skill | On the rested games | Result beyond the close |",
        "|---|---|---|---|---|",
    ]
    for name, figures in found["figures"].items():
        lines.append(
            f"| {name} | {figures['games']:,} | {ys._cell(figures['move'], 1000)} | "
            f"{ys._cell(figures['move_rested'], 1000)} | {ys._cell(figures['result'], 1000)} |"
        )
    lines += [
        "",
        "**Shown beside, never part of the rule:** the slope of the move on each new column",
        "alone, and the calibration slope of the result model's probabilities.",
        "",
        "| Column | Slope of the move on it |",
        "|---|---|",
    ]
    for name in (*WORKLOAD, *B2B):
        lines.append(f"| {name} | {_plain(found['figures'][name].get('slope'))} |")
    lines += ["", "| Result model | Calibration slope |", "|---|---|"]
    for name, slope in found["calibration"].items():
        lines.append(f"| {name} | {_plain(slope)} |")
    lines += [
        "",
        "## The decision",
        "",
        f"Holm's step-down at {ALPHA} over the three tests. A test holds when it passes with a",
        "negative estimate. goalie_workload is kept when it holds and its rested interval lies",
        "below 0; goalie_b2b when it holds and step 1 holds.",
        "",
        "| Test | Estimate (thousandths) | Rested | Back-to-back | p | Holm threshold | Holds | "
        "Kept |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for name, entry in decision.items():
        digits = 4 if name == "shots" else 3
        kept = "not an input" if "kept" not in entry else ("yes" if entry["kept"] else "no")
        lines.append(
            f"| {name} | {_thousandths(entry['primary'], digits)} | "
            f"{_thousandths(entry.get('rested'))} | {_thousandths(entry.get('back_to_back'))} | "
            f"{entry['p_value']:.4f} | {entry['threshold']:.4f} | "
            f"{'yes' if entry['holds'] else 'no'} | {kept} |"
        )
    kept = [n for n, e in decision.items() if e.get("kept")]
    lines += [
        "",
        "**Kept:** "
        + (
            f"{', '.join(kept)}: proposed to the owner as challenger inputs, as development "
            "evidence only."
            if kept
            else "none."
        ),
        "",
        "**The paired result beyond the close** (descriptive, thousandths): "
        + "; ".join(f"{n} {_thousandths(decision[n]['result'])}" for n in PRIMARY)
        + ".",
        "",
    ]
    return "\n".join(lines)


def run(lake: Reader) -> dict[str, Any]:
    """Both steps on the lake to the explore half's last season, and the family's decision."""
    last = max(ys.EXPLORE)
    read = ys.read(lake, last)
    shots, counts = shot_rows(
        lake.read("shots").filter(pl.col("season") <= last),
        lake.read("shot_xg").filter(pl.col("season") <= last),
        read,
    )
    scored = shot_scores(shots, read.frame)
    frame, missing = with_candidates(read)
    explore = frame.filter(pl.col("season").is_in(ys.EXPLORE), pl.col("rested"))
    stale = explore.filter(pl.col("goalie_b2b_diff") != 0).height
    per_game, figures = ys.run(frame, "explore", CANDIDATES)
    decision = decide(per_game, scored)
    return {
        "shots": {
            "shots": scored.height,
            "starter_games": scored.select("game_id", "goalie_id").unique().height,
            "counts": counts,
            "bands": by_band(scored),
            "train_cutoffs": sorted(str(c) for c in scored["train_cutoff"].unique()),
        },
        "missing_games": missing,
        "stale_rested": stale,
        "figures": figures,
        "calibration": calibration(frame),
        "decision": decision,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Goalie rest and workload (#418).")
    parser.add_argument("--out", type=Path, default=Path("reports/yardstick"))
    args = parser.parse_args()
    found = run(Lake())
    version = reports.version("goalie-rest", datetime.now(UTC))
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / f"{version}.json").write_text(
        json.dumps(found, indent=2, sort_keys=True, default=str)
    )
    path = args.out / f"{version}.md"
    path.write_text(markdown(found, version))
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
