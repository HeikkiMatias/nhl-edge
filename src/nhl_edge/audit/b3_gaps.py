"""Hard rule 8's screen of B3's gaps (#107, gate 2): the games where B3's probability differs from
B1's by more than 8 points, screened for bug signatures before a manual review. It never reads a
game's result: only B3's inputs and fit, the projection, and, to check the projection against
what happened, the game's own boxscore (who dressed and who started), which no model reads.

For each gap game, as B3 saw it at the start (E1):
- B3's log-odds in parts: β0, the home term h_s, Δĝ's term at the goalie mixture's expected Δĝ
  and each schedule input's term, beside the market's log-odds; the input with the largest term
  is the gap's driver;
- each team's expected goals at 5v5, on the power play and shorthanded, and its multiplier κ·φ;
- flags, each a bug signature:
  - no_candidates: a team without candidate skaters, which plays replacements only;
  - average_goalie: a team without candidate goalies, counted as average;
  - out_of_range: an input (Δĝ of any goalie pair, or a schedule input) outside the range of the
    fold's training games;
  - jump: a team's own 5v5 strength (its on-ice offense plus defense, in goals a game) moved by
    more than JUMP since its previous game of the season;
  - projection_miss: MISSED or more of a team's dressed skaters were not projected to play;
  - starter_surprise: a starter whose start probability was under UNSURE, or not a candidate.

The manual review covers every flagged game, every gap above LARGE, and a seeded sample of SAMPLE
more, stratified by season (#107).
"""

from collections.abc import Mapping
from datetime import datetime
from typing import Any

import numpy as np
import polars as pl

from nhl_edge.backtest.subsets import IN_LINEUP
from nhl_edge.features import team_strength as ts
from nhl_edge.game import b2, b3
from nhl_edge.lineup.goalie_start import team_goalie_games

JUMP = 0.08
MISSED = 4
UNSURE = 0.1
LARGE = 0.20
SAMPLE = 40
SEED = 20261003
FLAGS = (
    "no_candidates",
    "average_goalie",
    "out_of_range",
    "jump",
    "projection_miss",
    "starter_surprise",
)
TERMS = tuple(f"term_{name}" for name in b3.INPUTS)


def _logit(p: pl.Expr) -> pl.Expr:
    return (p / (1 - p)).log()


def explained(model: b3.B3Model, usable: pl.DataFrame, pairs: pl.DataFrame) -> pl.DataFrame:
    """Per game of usable, the goalie mixture's expected Δĝ with its range over the pairs, and
    B3's log-odds terms there: β0 (intercept), h_s (offset) and each input's weight times its
    standardized value (term_<input>)."""
    expected = pairs.group_by("game_id").agg(
        delta_g_hat=(pl.col("delta_g_hat") * pl.col("weight")).sum() / pl.col("weight").sum(),
        delta_low=pl.col("delta_g_hat").min(),
        delta_high=pl.col("delta_g_hat").max(),
    )
    others = [name for name in b3.INPUTS if name != "delta_g_hat"]
    frame = usable.select("game_id", "offset", *others).join(expected, on="game_id")
    return frame.select(
        "game_id",
        "delta_g_hat",
        "delta_low",
        "delta_high",
        *others,
        intercept=pl.lit(model.intercept),
        offset="offset",
        **{
            f"term_{name}": (pl.col(name) - mean) / scale * weight
            for name, mean, scale, weight in zip(
                b3.INPUTS, model.means, model.scales, model.weights, strict=True
            )
        },
    )


def team_flags(tables: b3.Tables, games: pl.Series, goalies: pl.DataFrame) -> pl.DataFrame:
    """Per team-game of games: its expected goals in parts and multiplier, how far its own
    strength moved since its previous game (jump), and its lineup flags. goalies are the
    candidate goalies B3 read for each game (game_id, team, goalie_id, p_start), those known
    before its prediction. Reads the game's own boxscore only for who dressed and who
    started."""
    wanted = pl.col("game_id").is_in(games.implode())
    lines = ts.team_lines()
    base, _ = b3.multipliers(tables)
    goals = (
        b3.team_goals(tables)
        .join(base.select("game_id", "team", "base"), on=["game_id", "team"])
        .join(tables.games.select("game_id", "start_utc"), on="game_id")
        .with_columns(goals=pl.col("raw") * pl.col("base"), line=pl.col("team").replace(lines))
        .sort("line", "start_utc", "game_id")
        # The team's own strength, so a change of opponent is no jump; within the season, so the
        # summer's roster changes are none either.
        .with_columns(
            jump=(pl.col("strength_5v5") - pl.col("strength_5v5").shift(1))
            .abs()
            .over("line", "season")
        )
        .filter(wanted)
        .select("game_id", "team", "xg_5v5", "xg_pp", "xg_sh", "base", "goals", "jump")
    )
    projected = tables.lineups.filter(
        wanted, pl.col("role").is_in(["F", "D"]), pl.col("p_available") >= IN_LINEUP
    ).select("game_id", "team", "player_id")
    dressed = tables.actual_lineups.filter(wanted, pl.col("role").is_in(["F", "D"]))
    missed = (
        dressed.join(projected, on=["game_id", "team", "player_id"], how="anti")
        .group_by("game_id", "team")
        .agg(missed=pl.len())
    )
    candidates = (
        tables.lineups.filter(wanted, pl.col("role").is_in(["F", "D"]))
        .select("game_id", "team")
        .unique()
        .with_columns(has_candidates=pl.lit(True))
    )
    candidate_goalies = (
        goalies.filter(wanted)
        .select("game_id", "team")
        .unique()
        .with_columns(has_goalies=pl.lit(True))
    )
    starters = (
        team_goalie_games(tables.actual_lineups.filter(wanted))
        .select("game_id", "team", goalie_id="starter")
        .join(
            goalies.select("game_id", "team", "goalie_id", "p_start"),
            on=["game_id", "team", "goalie_id"],
            how="left",
        )
        .select("game_id", "team", starter_p=pl.col("p_start").fill_null(0.0))
    )
    keys = ["game_id", "team"]
    return (
        goals.join(missed, on=keys, how="left")
        .join(candidates, on=keys, how="left")
        .join(candidate_goalies, on=keys, how="left")
        .join(starters, on=keys, how="left")
        .with_columns(
            pl.col("missed").fill_null(0),
            no_candidates=pl.col("has_candidates").is_null(),
            average_goalie=pl.col("has_goalies").is_null(),
            jump_flag=pl.col("jump").fill_null(0.0) > JUMP,
            projection_miss=pl.col("missed").fill_null(0) >= MISSED,
            starter_surprise=pl.col("starter_p") < UNSURE,
        )
        .drop("has_candidates", "has_goalies")
    )


def screen(tables: b3.Tables, gaps: pl.DataFrame, starts: Mapping[int, datetime]) -> pl.DataFrame:
    """Every gap game of gaps (one experiment's gaps_b3.csv rows: season, game_id, game_date,
    home, away, p_b3, p_b1, gap) with B3's terms, both teams' parts and the flags, B3 refit for
    each season as the backtest's E1 fold starting at starts[season] and predicting at each
    game's start. Raises if the refit leaves out a gap game or its probability differs from p_b3:
    the screen would then not explain the backtest's gaps."""
    # The gaps file's own columns only: nothing else it might carry reaches the report.
    gaps = gaps.select("season", "game_id", "game_date", "home", "away", "p_b3", "p_b1", "gap")
    frames = []
    for season, start in sorted(starts.items()):
        season_gaps = gaps.filter(pl.col("season") == season)
        if season_gaps.is_empty():
            continue
        moments = tables.games.join(season_gaps.select("game_id"), on="game_id").select(
            "game_id", prediction_utc="start_utc"
        )
        predicted, model = b3.predictions(tables, moments, season, start)
        through = b3.through(tables, season)
        inputs = b3.game_inputs(through)
        train = b3.training_games(through, inputs, season, start, "observed_utc")
        pool = through.goalie_starts.select(
            "game_id", "team", "goalie_id", "p_start", "observed_utc"
        )
        usable, ready = b2.known_before(
            inputs.filter(pl.col("season") == season), pool, moments, "observed_utc"
        )
        _, gammas = b3.multipliers(through)
        terms = explained(model, usable, b3.scenarios(usable, ready, gammas))
        # The fold's training range of each input.
        outside = pl.lit(False)
        for name in b3.INPUTS:
            low, high = train[name].min(), train[name].max()
            if name == "delta_g_hat":
                outside |= (pl.col("delta_low") < low) | (pl.col("delta_high") > high)
            else:
                outside |= (pl.col(name) < low) | (pl.col(name) > high)
        frame = (
            season_gaps.join(predicted.select("game_id", "p_home"), on="game_id")
            .join(terms, on="game_id")
            .with_columns(out_of_range=outside)
        )
        unexplained = season_gaps.join(frame.select("game_id"), on="game_id", how="anti")
        if unexplained.height:
            examples = ", ".join(str(g) for g in unexplained["game_id"].sort().head(3))
            raise ValueError(
                f"B3 refit for {season} does not predict {unexplained.height} gap games, e.g. "
                f"{examples}: rerun nhl backtest"
            )
        drift = frame.select((pl.col("p_home") - pl.col("p_b3")).abs().max()).item()
        if drift is None or drift > 1e-3:
            raise ValueError(
                f"B3 refit for {season} differs from the gaps file by {drift}: rerun nhl backtest"
            )
        teams = team_flags(through, frame["game_id"], ready)
        for side in ("home", "away"):
            renamed = teams.rename({c: f"{side}_{c}" for c in teams.columns if c != "game_id"})
            frame = frame.join(
                renamed, left_on=["game_id", side], right_on=["game_id", f"{side}_team"]
            )
        frames.append(frame)
    if not frames:
        return pl.DataFrame()
    screened = pl.concat(frames, how="diagonal_relaxed")
    flags = {
        flag: pl.col(f"home_{column}") | pl.col(f"away_{column}")
        for flag, column in (
            ("no_candidates", "no_candidates"),
            ("average_goalie", "average_goalie"),
            ("jump", "jump_flag"),
            ("projection_miss", "projection_miss"),
            ("starter_surprise", "starter_surprise"),
        )
    }
    others = pl.concat_list([pl.col(t).abs() for t in TERMS])
    return (
        screened.with_columns(**flags)
        .with_columns(
            flagged=pl.any_horizontal(*FLAGS),
            logit_b3=_logit(pl.col("p_b3")),
            logit_b1=_logit(pl.col("p_b1")),
            driver=pl.lit(list(b3.INPUTS)).list.get(others.list.arg_max()),
            timid=(pl.col("p_b3") - 0.5).abs() < (pl.col("p_b1") - 0.5).abs(),
            other_favourite=(pl.col("p_b3") - 0.5) * (pl.col("p_b1") - 0.5) < 0,
        )
        .sort("season", "game_date", "game_id")
    )


def review_set(screened: pl.DataFrame) -> pl.DataFrame:
    """screened with review: "flagged" for a flagged game, "large" for a gap above LARGE, and
    "sample" for a seeded sample of SAMPLE of the rest, split evenly over the seasons."""
    marked = screened.with_columns(
        review=pl.when(pl.col("flagged"))
        .then(pl.lit("flagged"))
        .when(pl.col("gap").abs() > LARGE)
        .then(pl.lit("large"))
        .otherwise(pl.lit(""))
    )
    rng = np.random.default_rng(SEED)
    seasons = sorted(marked["season"].unique().to_list())
    sampled: list[int] = []
    for season in seasons:
        rest = marked.filter(pl.col("season") == season, pl.col("review") == "")["game_id"]
        take = min(SAMPLE // len(seasons), rest.len())
        sampled += [int(g) for g in rng.choice(rest.sort().to_numpy(), size=take, replace=False)]
    return marked.with_columns(
        review=pl.when(pl.col("game_id").is_in(sampled))
        .then(pl.lit("sample"))
        .otherwise(pl.col("review"))
    )


def summary(marked: pl.DataFrame) -> dict[str, Any]:
    """Counts behind the review: per flag, per review group, and the gaps' shape."""
    return {
        "games": marked.height,
        "flags": {flag: int(marked[flag].sum()) for flag in FLAGS},
        "flagged": int(marked["flagged"].sum()),
        "review": dict(
            marked.filter(pl.col("review") != "").group_by("review").len().sort("review").rows()
        ),
        "timid": float(marked["timid"].mean()),  # type: ignore[arg-type]
        "other_favourite": float(marked["other_favourite"].mean()),  # type: ignore[arg-type]
        "drivers": dict(marked.group_by("driver").len().sort("len", descending=True).rows()),
    }


def every_gap(screened: pl.DataFrame) -> pl.DataFrame:
    """screened with every game marked for review ("all"): the blend's gaps are few enough to
    review each (#144)."""
    return screened.with_columns(review=pl.lit("all"))


def blend_gaps(rows: pl.DataFrame) -> pl.DataFrame:
    """The blend's gaps (gaps_blend.csv: experiment, season, game_id, game_date, home, away,
    p_blend, p_b1, gap), one row per game, the experiment with the larger gap kept."""
    return (
        rows.sort(pl.col("gap").abs(), descending=True)
        .unique("game_id", keep="first")
        .sort("season", "game_date", "game_id")
    )


def markdown(marked: pl.DataFrame, source: str, version: str, model: str = "B3") -> str:
    """The screen's report: what was screened, the flag counts, and one row per game to review,
    with a blank column for the reviewer's note. For the blend's gaps (model "blend"), Gap is
    the blend's gap against B1, and B3 and its terms are B3's own at the start."""
    facts = summary(marked)
    lines = [
        f"# {model} gap screen ({version})",
        "",
        f"Hard rule 8's screen (#107) of {facts['games']:,} gaps above 8 points against B1 in "
        f"`{source}`. B3 is refit as in the backtest's E1 folds. No result was read.",
        "",
        "## Shape of the gaps",
        "",
        f"- B3 is closer to 50% than the market in {facts['timid']:.0%} of them, and picks the "
        f"other favourite in {facts['other_favourite']:.0%}.",
        "- The input with the largest term in B3's log-odds: "
        + ", ".join(f"{name} {count}" for name, count in facts["drivers"].items())
        + ".",
        "",
        "## Flags",
        "",
        "| Flag | Games |",
        "| --- | --- |",
        *(f"| {flag} | {count} |" for flag, count in facts["flags"].items()),
        f"| any | {facts['flagged']} |",
        "",
        "## Games to review",
        "",
        "Every flagged game, every gap above "
        f"{LARGE:.0%}, and a seeded sample (seed {SEED}) of {SAMPLE} more, split by season. "
        "Each team's goals are its expected goals times κ·φ; missed counts its dressed skaters "
        "that were not projected; starter is the starter's start probability.",
        "",
        "| Review | Game | Date | Home | Away | B3 | B1 | Gap | Δĝ | Driver | Home goals | "
        "Away goals | Missed | Starter | Flags | Note |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | "
        "--- | --- |",
    ]
    order = {"flagged": 0, "large": 1, "sample": 2, "all": 3}
    shown = (
        marked.filter(pl.col("review") != "")
        .with_columns(order=pl.col("review").replace_strict(order, return_dtype=pl.Int8))
        .sort("order", "season", "game_date", "game_id")
    )
    for row in shown.iter_rows(named=True):
        flags = ", ".join(flag for flag in FLAGS if row[flag])
        lines.append(
            f"| {row['review']} | {row['game_id']} | {row['game_date']} | {row['home']} | "
            f"{row['away']} | {row['p_b3']:.3f} | {row['p_b1']:.3f} | {row['gap']:+.3f} | "
            f"{row['delta_g_hat']:+.3f} | {row['driver']} | {row['home_goals']:.2f} | "
            f"{row['away_goals']:.2f} | {row['home_missed']}/{row['away_missed']} | "
            f"{row['home_starter_p']:.2f}/{row['away_starter_p']:.2f} | {flags} | |"
        )
    return "\n".join(lines) + "\n"
