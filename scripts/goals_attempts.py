"""#420 (#417): do a team's 5v5 goals and shot attempts, rated as ΔS rates its 5v5 xG, add to the
5v5 xG part? Each definition was fixed on the issue before any figure was read (the comment of
2026-11-11).

    uv run python scripts/goals_attempts.py

**Each team-game's 5v5 counts,** beside ΔS's own team-games (team_strength.team_games: the same
games, 5v5 minutes with both nets manned, and public times):
- goals: the shots rows with event_type goal, at 5v5, with the shooting team's goalie in (ΔS's
  own_goalie_in), not at an empty net and not a penalty shot;
- attempts: the same filter on every shots row (shots on goal, misses and goals) and on the
  blocked_shots rows whose reason is null or "blocked" (a teammate's block and "other-block",
  from 2023-24 on, do not count);
- unlike xG, a shot without coordinates counts.

**Each rate** is built as ΔS's 5v5 part is: decayed sums over the team's line's earlier team-games
public before ΔS's as-of time, at ΔS's half-life of 80 games, plus `pull` games at the league's
rate over the season and the one before. Each column is the home team's expected 5v5 count less
the away team's: the league's 5v5 minutes per game times the average of one side's rate for and
the other's rate against. five_team_xg_diff is ΔS's own 5v5 part, the control.

**The pull** of each measure is chosen on 2012-13 alone (FIT_SEASON): the one on PULLS whose rate
before each team-game best forecasts that game's own rate, for and against, weighted by its
minutes, Σ (count - rate·minutes)² / minutes, ties going to more pull. The pull's own choice
reads no explore game, result or price, and its cutoff is checked to fall before the first explore
fold. The half-life is not out of sample: it is ΔS's, tuned on these seasons' results through
team_strength.TUNED_CUTOFF (ADR 0011), so the pulls are chosen at an in-sample half-life.

**The candidates** on the yardstick's explore half (ADR 0053): the 5v5 xG part (B3's five_diff)
alone, and with five_goals_diff, five_attempts_diff or both. Each is paired against the 5v5 xG part
on the move skill, game by game. **The decision:** Holm's step-down at ALPHA over the three; a test
holds, and its candidate is kept, when it passes with a negative estimate. A kept candidate goes to
the owner as a challenger input, as development evidence only (the confirm half is spent).

It reads the lake to the explore half's last season only, and writes
reports/yardstick/goals-attempts-<yyyymmdd>-<shortsha>.md. It writes nothing to the lake.
"""

import argparse
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from nhl_edge.backtest import metrics, reports
from nhl_edge.backtest import yardstick as ys
from nhl_edge.features import team_strength as ts
from nhl_edge.lake.tables import Lake, Reader

ALPHA = ys.ALPHA
HALF_LIFE = ts.TUNED.half_life
PULLS = (0.0, 10.0, 20.0, 40.0, 80.0, 160.0, 320.0)
FIT_SEASON = ys.FIRST_FIT
# Each measure's (for, against) sums per team-game, and its column.
MEASURES: dict[str, tuple[str, str]] = {
    "goals": ("gf", "ga"),
    "attempts": ("cf", "ca"),
    "team_xg": ("xgf_5v5", "xga_5v5"),
}
COLUMNS = {
    "goals": "five_goals_diff",
    "attempts": "five_attempts_diff",
    "team_xg": "five_team_xg_diff",
}
SUMS = ("gf", "ga", "cf", "ca", "xgf_5v5", "xga_5v5", "min_5v5")
# The blocked attempts that count: before 2023-24 the feed gives no reason.
BLOCKED_REASONS = ("blocked",)
BASE = ("five_diff",)
FAMILY = ("goals", "attempts", "both")
CANDIDATES: dict[str, tuple[str, ...]] = {
    "xg part": BASE,
    "goals": (*BASE, COLUMNS["goals"]),
    "attempts": (*BASE, COLUMNS["attempts"]),
    "both": (*BASE, COLUMNS["goals"], COLUMNS["attempts"]),
    # The control, never part of the decision: the team's own 5v5 xG rate, as ΔS rates it.
    "team xg": (*BASE, COLUMNS["team_xg"]),
    # Descriptive: each new column alone, for the move's slope on it.
    **{column: (column,) for column in COLUMNS.values()},
}
PAIRED = {name: (name, "xg part") for name in (*FAMILY, "team xg")}
CALIBRATED = ("xg part", *FAMILY, "team xg")
# The check of the code: at ΔS's settings the control is ΔS's 5v5 part to float noise.
AGREEMENT = 1e-9


def counts(shots: pl.DataFrame, blocked: pl.DataFrame) -> pl.DataFrame:
    """Each shooting team's 5v5 goals and attempts per game: at 5v5 with its own goalie in, not
    at an empty net, no penalty shot, and blocked attempts by reason (BLOCKED_REASONS)."""

    def five(frame: pl.DataFrame) -> pl.DataFrame:
        return frame.filter(
            (pl.col("skaters_for") == 5)
            & (pl.col("skaters_against") == 5)
            & ts.own_goalie_in()
            & ~pl.col("is_empty_net").fill_null(False)
        )

    unblocked = five(shots.filter(~pl.col("is_penalty_shot"))).select(
        "game_id", "team", goal=pl.col("event_type") == "goal"
    )
    blocks = five(
        blocked.filter(pl.col("reason").is_null() | pl.col("reason").is_in(BLOCKED_REASONS))
    ).select("game_id", "team", goal=pl.lit(False))
    return (
        pl.concat([unblocked, blocks])
        .group_by("game_id", "team")
        .agg(goals=pl.col("goal").sum().cast(pl.Float64), attempts=pl.len().cast(pl.Float64))
    )


def feeds_public(shots: pl.DataFrame, blocked: pl.DataFrame) -> pl.DataFrame:
    """When each game's play-by-play rows (shots and blocked shots) were all public."""
    return (
        pl.concat(
            [shots.select("game_id", "observed_utc"), blocked.select("game_id", "observed_utc")]
        )
        .group_by("game_id")
        .agg(feed_utc=pl.col("observed_utc").max())
    )


def team_games(
    shots: pl.DataFrame,
    blocked: pl.DataFrame,
    shot_xg: pl.DataFrame,
    strength_time: pl.DataFrame,
) -> pl.DataFrame:
    """ΔS's team-games (team_strength.team_games) with each side's 5v5 goals and attempts for
    and against beside its 5v5 xG and minutes, 0 where it had none. A team-game is public when
    every feed it reads is: ΔS's time, or the play-by-play's when later."""
    found = counts(shots, blocked)
    own = found.select("game_id", "team", gf="goals", cf="attempts")
    opp = found.select("game_id", opponent="team", ga="goals", ca="attempts")
    return (
        ts.team_games(shots, shot_xg, strength_time)
        .join(own, on=["game_id", "team"], how="left")
        .join(opp, on=["game_id", "opponent"], how="left")
        .join(feeds_public(shots, blocked), on="game_id", how="left")
        .with_columns(
            pl.col("gf", "ga", "cf", "ca").fill_null(0.0),
            observed_utc=pl.max_horizontal("observed_utc", "feed_utc"),
        )
        .select("game_id", "season", "game_date", "team", "opponent", *SUMS, "observed_utc")
        .sort("team", "game_date", "game_id")
    )


def team_sums(
    history: pl.DataFrame,
    targets: pl.DataFrame,
    half_life: float = HALF_LIFE,
    lines: Mapping[str, str] | None = None,
) -> pl.DataFrame:
    """Each target (team, as_of_utc) with its line's decayed SUMS and games over the team-games
    public before as_of_utc, as team_strength.team_states sums ΔS's."""
    lines = ts.team_lines() if lines is None else lines
    decay = 0.5 ** (1 / half_life)
    columns = [*SUMS, "games"]
    history = history.with_columns(line=pl.col("team").replace(dict(lines)))
    targets = targets.with_columns(line=pl.col("team").replace(dict(lines)))
    frames = []
    for (line,), rows in targets.group_by("line"):
        past = history.filter(pl.col("line") == line).sort(
            "observed_utc", "game_date", "game_id", "team"
        )
        values = past.select(*SUMS, games=pl.lit(1.0)).to_numpy().astype(float)
        sums = ts._decayed(values, decay) if len(values) else np.zeros((0, len(columns)))
        seen = np.searchsorted(
            past["observed_utc"].to_numpy(), rows["as_of_utc"].to_numpy(), side="left"
        )
        state = np.zeros((rows.height, len(columns)))
        has = seen > 0
        state[has] = sums[seen[has] - 1]
        frames.append(
            rows.with_columns(pl.Series(name, state[:, i]) for i, name in enumerate(columns))
        )
    return pl.concat(frames).drop("line")


def league_sums(history: pl.DataFrame, targets: pl.DataFrame) -> pl.DataFrame:
    """Each distinct (season, as_of_utc) with the league's SUMS and games over every team-game
    public before as_of_utc in that season and the one before (team_strength.league_states)."""
    out = []
    for (season,), rows in targets.select("season", "as_of_utc").unique().group_by("season"):
        past = history.filter(pl.col("season").is_in([season, season - 10001])).sort(
            "observed_utc", "game_id", "team"
        )
        totals = past.select(*SUMS, games=pl.lit(1.0)).to_numpy().astype(float).cumsum(axis=0)
        seen = np.searchsorted(
            past["observed_utc"].to_numpy(), rows["as_of_utc"].to_numpy(), side="left"
        )
        state = np.zeros((rows.height, len(SUMS) + 1))
        has = seen > 0
        state[has] = totals[seen[has] - 1]
        out.append(
            rows.with_columns(
                pl.Series(f"league_{name}", state[:, i]) for i, name in enumerate([*SUMS, "games"])
            )
        )
    return pl.concat(out)


def per_minute(column: str, pull: float, suffix: str = "") -> pl.Expr:
    """A side's rate per 5v5 minute: its decayed count plus pull games at the league's rate, over
    its decayed minutes plus pull games at the league's minutes (team_strength.side_rates). The
    away side's columns carry suffix; the league's are shared."""
    games = pl.col("league_games")
    return (pl.col(f"{column}{suffix}") + pull * pl.col(f"league_{column}") / games) / (
        pl.col(f"min_5v5{suffix}") + pull * pl.col("league_min_5v5") / games
    )


def states(
    games: pl.DataFrame,
    history: pl.DataFrame,
    half_life: float = HALF_LIFE,
    lines: Mapping[str, str] | None = None,
) -> pl.DataFrame:
    """Each game (game_id, season, game_date, start_utc, home, away) with ΔS's as-of time and
    each side's sums and the league's, the away side's suffixed _away: everything a rate needs
    at any pull."""
    targets = games.select(
        "game_id",
        "season",
        "home",
        "away",
        as_of_utc=ts.as_of(pl.col("game_date"), pl.col("start_utc")),
    )
    sides = pl.concat(
        [
            targets.select("game_id", "season", "as_of_utc", team=pl.col(side), side=pl.lit(side))
            for side in ("home", "away")
        ]
    )
    summed = team_sums(history, sides, half_life, lines).join(
        league_sums(history, sides), on=["season", "as_of_utc"]
    )
    home = summed.filter(pl.col("side") == "home").drop("side", "team", "season", "as_of_utc")
    away = summed.filter(pl.col("side") == "away").select(
        "game_id", *(pl.col(c).alias(f"{c}_away") for c in (*SUMS, "games"))
    )
    return targets.join(home, on="game_id").join(away, on="game_id")


def difference(measure: str, pull: float) -> pl.Expr:
    """The measure's column: the home side's expected 5v5 count less the away side's, each the
    league's 5v5 minutes per game times the average of its rate for and the other's against, 0
    where there is nothing to rate on (ΔS's 5v5 part, team_strength.strength)."""
    for_, against = MEASURES[measure]
    league_minutes = pl.col("league_min_5v5") / pl.col("league_games")
    home = league_minutes * (per_minute(for_, pull) + per_minute(against, pull, "_away")) / 2
    away = league_minutes * (per_minute(for_, pull, "_away") + per_minute(against, pull)) / 2
    return home.fill_nan(0.0).fill_null(0.0) - away.fill_nan(0.0).fill_null(0.0)


def pull_losses(
    history: pl.DataFrame,
    games: pl.DataFrame,
    measure: str,
    season: int = FIT_SEASON,
    half_life: float = HALF_LIFE,
    lines: Mapping[str, str] | None = None,
) -> dict[float, float]:
    """Each pull's loss on the season's team-games: the rate before each game against that game's
    own rate, for and against, weighted by its 5v5 minutes, Σ (count - rate·minutes)² / minutes.
    Only team-games with minutes and with history count, the same at every pull."""
    for_, against = MEASURES[measure]
    played = history.filter(pl.col("season") == season, pl.col("min_5v5") > 0).join(
        games.select("game_id", "game_date", "start_utc"), on=["game_id", "game_date"]
    )
    targets = played.select(
        "game_id",
        "season",
        "team",
        own_for=pl.col(for_),
        own_against=pl.col(against),
        own_minutes=pl.col("min_5v5"),
        as_of_utc=ts.as_of(pl.col("game_date"), pl.col("start_utc")),
    )
    summed = team_sums(history, targets, half_life, lines).join(
        league_sums(history, targets), on=["season", "as_of_utc"]
    )
    # A fixed order, so the sum is the same to its last digit however the lines were grouped.
    summed = summed.filter(pl.col("games") > 0).sort("game_id", "team")
    losses = {}
    minutes = pl.col("own_minutes")
    for pull in PULLS:
        loss = summed.select(
            (
                (pl.col("own_for") - per_minute(for_, pull) * minutes) ** 2 / minutes
                + (pl.col("own_against") - per_minute(against, pull) * minutes) ** 2 / minutes
            ).sum()
        ).item()
        losses[pull] = float(loss)
    return losses


def pull_cutoff(history: pl.DataFrame, season: int = FIT_SEASON) -> datetime:
    """The last team-game the pull's choice reads: every one to the season's end (its targets,
    and the history and league before them)."""
    last = history.filter(pl.col("season") <= season)["observed_utc"].max()
    if not isinstance(last, datetime):
        raise ValueError(f"no team-games to {season}")
    return last


def choose(losses: Mapping[float, float]) -> float:
    """The pull with the lowest loss, ties going to more pull (team_strength.steadiness)."""
    best = min(losses.values())
    return max(pull for pull, loss in losses.items() if loss == best)


def columns_for(
    games: pl.DataFrame,
    history: pl.DataFrame,
    pulls: Mapping[str, float],
    half_life: float = HALF_LIFE,
    lines: Mapping[str, str] | None = None,
) -> pl.DataFrame:
    """Each game's three columns at each measure's pull, with ΔS's as-of time."""
    state = states(games, history, half_life, lines)
    return state.select(
        "game_id",
        "as_of_utc",
        *(difference(m, pulls[m]).alias(COLUMNS[m]) for m in MEASURES),
    ).sort("game_id")


def check(
    games: pl.DataFrame, history: pl.DataFrame, team_strength: pl.DataFrame
) -> dict[str, Any]:
    """The control at ΔS's settings against the lake's ΔS 5v5 part, game by game."""
    mine = columns_for(games, history, {m: ts.TUNED.prior_games for m in MEASURES})
    both = mine.join(team_strength.select("game_id", "delta_5v5"), on="game_id")
    gap = (both[COLUMNS["team_xg"]] - both["delta_5v5"]).abs()
    largest = gap.max()
    worst = float(largest) if isinstance(largest, float) else 0.0
    return {
        "games": both.height,
        "agree": int((gap <= AGREEMENT).sum()),
        "largest_gap": worst,
        "settings": ts.TUNED.label,
    }


def with_rates(read: ys.Read, history: pl.DataFrame, pulls: Mapping[str, float]) -> pl.DataFrame:
    """The yardstick's rows with the three columns. Refused if a game's as-of time is not before
    its prediction time."""
    games = read.games.select("game_id", "season", "game_date", "start_utc", "home", "away").join(
        read.frame.select("game_id"), on="game_id", how="semi"
    )
    found = columns_for(games, history, pulls)
    frame = read.frame.join(found, on="game_id")
    late = frame.filter(pl.col("as_of_utc") >= pl.col("prediction_utc")).height
    if late:
        raise ValueError(f"{late} games rated at or after their prediction time")
    return frame.drop("as_of_utc").sort("game_id")


def paired_frame(per_game: pl.DataFrame, a: str, b: str, measure: str = "move") -> pl.DataFrame:
    """a's measure less b's, game by game on the games both scored (difference)."""
    left = per_game.filter(pl.col("candidate") == a)
    right = per_game.filter(pl.col("candidate") == b).select("game_id", other=measure)
    return left.join(right, on="game_id").with_columns(difference=pl.col(measure) - pl.col("other"))


def decide(per_game: pl.DataFrame) -> dict[str, Any]:
    """Each paired estimate (the family's three and the control's), its p-value and the figures
    shown beside it; Holm's step-down at ALPHA over the family; whether each test holds (passes
    with a negative estimate) and so whether its candidate is kept."""
    estimates: dict[str, dict[str, Any]] = {}
    for name, (a, b) in PAIRED.items():
        both = paired_frame(per_game, a, b)
        estimates[name] = {
            "primary": metrics.bootstrap(both, "difference").to_dict(),
            "p_value": metrics.p_value(both, "difference"),
            "rested": metrics.bootstrap(both.filter(pl.col("rested")), "difference").to_dict(),
            "back_to_back": metrics.bootstrap(
                both.filter(~pl.col("rested")), "difference"
            ).to_dict(),
            "per_season": {
                str(season): metrics.bootstrap(part, "difference").to_dict()
                for (season,), part in both.sort("season").group_by("season", maintain_order=True)
            },
            "result": metrics.bootstrap(
                paired_frame(per_game, a, b, "result"), "difference"
            ).to_dict(),
        }
    steps = metrics.holm({n: estimates[n]["p_value"] for n in FAMILY}, ALPHA)
    for name in FAMILY:
        entry = estimates[name]
        entry["threshold"] = steps[name]["threshold"]
        entry["holds"] = steps[name]["passes"] and entry["primary"]["mean"] < 0
        entry["kept"] = entry["holds"]
    return {"family": list(FAMILY), "estimates": estimates}


def calibration(frame: pl.DataFrame) -> dict[str, Any]:
    """Shown beside: the calibration slope of the result model's probabilities (the close and the
    candidate's columns) for the baseline, the family and the control, and of the close alone."""
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


def correlations(frame: pl.DataFrame) -> dict[str, dict[str, float]]:
    """Descriptive: the correlation of the 5v5 xG part and the three columns on the explore
    half's games."""
    names = (*BASE, *COLUMNS.values())
    games = frame.filter(pl.col("season").is_in(ys.EXPLORE)).select(names).to_numpy()
    matrix = np.corrcoef(games, rowvar=False)
    return {a: {b: float(matrix[i, j]) for j, b in enumerate(names)} for i, a in enumerate(names)}


def _thousandths(estimate: Mapping[str, Any] | None) -> str:
    if estimate is None:
        return "none"
    return (
        f"{estimate['mean'] * 1000:+.3f} [{estimate['low'] * 1000:+.3f}, "
        f"{estimate['high'] * 1000:+.3f}]"
    )


def _plain(estimate: Mapping[str, Any] | None) -> str:
    if estimate is None:
        return "none"
    mean = estimate.get("mean", estimate.get("value"))
    return f"{mean:+.3f} [{estimate['low']:+.3f}, {estimate['high']:+.3f}]"


def markdown(found: Mapping[str, Any], version: str) -> str:
    decision = found["decision"]["estimates"]
    seasons = [str(s) for s in ys.EXPLORE]
    lines = [
        f"# Goals and shot attempts beside xG ({version})",
        "",
        "#420 (#417), on the yardstick's explore half (2013-14 and 2014-15, fitted from 2012-13),",
        "each definition fixed on the issue before any figure was read (the comment of",
        "2026-11-11). Intervals are 95% weekly block bootstrap. Not fresh: #361, #366, #370 and",
        "#418 read these seasons, and five_diff's own move skill is known, so every p-value is",
        "development evidence only.",
        "",
        "**In-sample settings:** the 5v5 xG part's settings (and the yardstick's other features')",
        f"were tuned on these seasons, through {found['tuning_cutoff']}, and are read at their",
        "data's time under the training seasons' exception (ADR 0053, ADR 0011). So is the new",
        f"columns' half-life, ΔS's {HALF_LIFE:g} games: their pulls were chosen on 2012-13 alone",
        f"(through {found['pulls']['train_cutoff']}, before the first explore fold), but at that",
        "in-sample half-life. None of these figures is out of sample.",
        "",
        "## The pulls, chosen on 2012-13",
        "",
        f"Half-life {HALF_LIFE:g} games (ΔS's). Loss: Σ (count - rate·minutes)² / minutes over",
        "2012-13's team-games, for and against; the lowest wins, ties to more pull.",
        "",
        "| Measure | " + " | ".join(f"pull {p:g}" for p in PULLS) + " | Chosen |",
        "|---|" + "---|" * (len(PULLS) + 1),
    ]
    for measure, losses in found["pulls"]["losses"].items():
        best = min(losses.values())
        cells = [
            f"**{losses[str(p)]:,.1f}**" if losses[str(p)] == best else f"{losses[str(p)]:,.1f}"
            for p in PULLS
        ]
        lines.append(
            f"| {measure} | " + " | ".join(cells) + f" | {found['pulls']['chosen'][measure]:g} |"
        )
    agreement = found["check"]
    lines += [
        "",
        f"**The check of the code:** at ΔS's settings ({agreement['settings']}), the control",
        f"equals the lake's ΔS 5v5 part on {agreement['agree']:,} of {agreement['games']:,}",
        f"explore-half games (largest gap {agreement['largest_gap']:.2e}).",
        "",
        "## The candidates",
        "",
        "Move skill and result beyond the close in thousandths: negative means the candidate",
        "anticipates SBR's move beyond the opener's drift, or adds to the close.",
        "",
        "| Candidate | Games | Move skill | On the rested games | Result beyond the close |",
        "|---|---|---|---|---|",
    ]
    for name, figures in found["figures"].items():
        if name in COLUMNS.values():
            continue
        lines.append(
            f"| {name} | {figures['games']:,} | {ys._cell(figures['move'], 1000)} | "
            f"{ys._cell(figures['move_rested'], 1000)} | "
            f"{ys._cell(figures['result'], 1000)} |"
        )
    lines += [
        "",
        "**Shown beside, never part of the rule:** the slope of the move on each new column",
        "alone, the calibration slope of the result model's probabilities, and (descriptive)",
        "the correlations of the columns on the explore half's games.",
        "",
        "| Column | Slope of the move on it |",
        "|---|---|",
    ]
    for column in COLUMNS.values():
        lines.append(f"| {column} | {_plain(found['figures'][column].get('slope'))} |")
    lines += ["", "| Result model | Calibration slope |", "|---|---|"]
    for name, value in found["calibration"].items():
        lines.append(f"| {name} | {_plain(value)} |")
    names = list(found["correlations"])
    lines += ["", "| Correlation | " + " | ".join(names) + " |", "|---|" + "---|" * len(names)]
    for a in names:
        row = found["correlations"][a]
        lines.append(f"| {a} | " + " | ".join(f"{row[b]:.2f}" for b in names) + " |")
    lines += [
        "",
        "## The decision",
        "",
        f"Holm's step-down at {ALPHA} over the three paired tests against the 5v5 xG part. A test",
        "holds, and its candidate is kept, when it passes with a negative estimate.",
        "",
        "| Test | Paired move skill (thousandths) | p | Holm threshold | Holds | Kept |",
        "|---|---|---|---|---|---|",
    ]
    for name in FAMILY:
        entry = decision[name]
        lines.append(
            f"| {name} | {_thousandths(entry['primary'])} | {entry['p_value']:.4f} | "
            f"{entry['threshold']:.4f} | {'yes' if entry['holds'] else 'no'} | "
            f"{'yes' if entry['kept'] else 'no'} |"
        )
    kept = [n for n in FAMILY if decision[n]["kept"]]
    lines += [
        "",
        "**Kept:** "
        + (
            f"{', '.join(kept)}: proposed to the owner as challenger inputs, as development "
            "evidence only. On the rested games, where no rate knows a game the opener may not: "
            + "; ".join(f"{n} {_thousandths(decision[n]['rested'])}" for n in kept)
            + "."
            if kept
            else "none."
        ),
        "",
        "**Shown beside** (paired against the 5v5 xG part, thousandths). On a back-to-back the",
        "rates know last night's game, which an opener of the evening before may not (ADR 0006).",
        "The control, team xg, is outside the family.",
        "",
        "| Pairing | Rested | Back-to-back | "
        + " | ".join(seasons)
        + " | Result beyond the close |",
        "|---|---|---|" + "---|" * len(seasons) + "---|",
    ]
    for name in PAIRED:
        entry = decision[name]
        by = entry["per_season"]
        lines.append(
            f"| {name} | {_thousandths(entry['rested'])} | {_thousandths(entry['back_to_back'])} "
            "| "
            + " | ".join(_thousandths(by.get(s)) for s in seasons)
            + f" | {_thousandths(entry['result'])} |"
        )
    control = decision["team xg"]
    lines += [
        "",
        f"The control's paired move skill: {_thousandths(control['primary'])} (p "
        f"{control['p_value']:.4f}, outside the family).",
        "",
        "## Limits",
        "",
        "- **Redundancy:** the measures move together (the correlations above), so any gain is",
        "  small.",
        "- **Luck:** a goals rate can chase it, which is what its pull is for.",
        "- **Overlap:** a goals rate overlaps B3's finishing multipliers (ADR 0022).",
        "- **The market:** sharp bettors use such mixes, so the close likely holds them.",
        "",
    ]
    return "\n".join(lines)


def run(lake: Reader) -> dict[str, Any]:
    """The pulls on 2012-13, the columns, the candidates on the explore half and the decision,
    on the lake to the explore half's last season."""
    last = max(ys.EXPLORE)
    read = ys.read(lake, last)

    def upto(table: str) -> pl.DataFrame:
        return lake.read(table).filter(pl.col("season") <= last)

    history = team_games(
        upto("shots"), upto("blocked_shots"), upto("shot_xg"), upto("strength_time")
    )
    if history["season"].max() != last:
        raise ValueError(f"the team-games end before {last}")
    cutoff = pull_cutoff(history)
    first_fold = ys.fold_start(read.frame, min(ys.EXPLORE))
    if cutoff >= first_fold:
        raise ValueError(f"the pulls read to {cutoff}, not before the first explore fold")
    losses = {m: pull_losses(history, read.games, m) for m in MEASURES}
    pulls = {m: choose(found) for m, found in losses.items()}
    frame = with_rates(read, history, pulls)
    explore_games = read.games.join(
        read.frame.filter(pl.col("season").is_in(ys.EXPLORE)).select("game_id"), on="game_id"
    )
    agreement = check(
        explore_games,
        history,
        read.b2_tables.team_strength.filter(pl.col("season").is_in(ys.EXPLORE)),
    )
    per_game, figures = ys.run(frame, "explore", CANDIDATES)
    return {
        "pulls": {
            "losses": {m: {str(p): v for p, v in found.items()} for m, found in losses.items()},
            "chosen": pulls,
            "train_cutoff": str(cutoff),
            "first_explore_fold": str(first_fold),
        },
        "check": agreement,
        "tuning_cutoff": str(ts.TUNED_CUTOFF),
        "figures": figures,
        "calibration": calibration(frame),
        "correlations": correlations(frame),
        "decision": decide(per_game),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Goals and shot attempts beside xG (#420).")
    parser.add_argument("--out", type=Path, default=Path("reports/yardstick"))
    args = parser.parse_args()
    found = run(Lake())
    version = reports.version("goals-attempts", datetime.now(UTC))
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / f"{version}.json").write_text(
        json.dumps(found, indent=2, sort_keys=True, default=str)
    )
    path = args.out / f"{version}.md"
    path.write_text(markdown(found, version))
    print(f"wrote {path}")
