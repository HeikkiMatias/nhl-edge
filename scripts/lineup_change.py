"""#370's fifth candidate, lineup_change, on the yardstick's explore half, by the rule fixed on the
issue before it was scored (the comment of 2026-11-04).

    uv run python scripts/lineup_change.py

**The feature,** per team T of game g at E2's prediction time t (a second after SBR's opener at its
assumed 10:00 ET), from the tables read at their data's time (ys.through):
- **tonight's lineup:** T's projected skaters for g (lineups, F and D, observed before t), each
  weighted by his expected 5v5 minutes;
- **the last game's lineup:** the skaters who dressed in T's latest earlier game of the season
  whose boxscore was public before t (actual_lineups, F and D), each weighted by his time on ice
  at all strengths. Only earlier boxscores are read (hard rule 9);
- **a skater's rating:** ev_off + ev_def, the means of his latest player_ratings row observed before
  t; for tonight's candidates that is tonight's own row, as B3 reads it. A skater without one
  counts 0, as B3 rates replacements;
- **a lineup's strength:** 5 times its skaters' weighted mean rating, the time-averaged sum of the
  on-ice 5v5 ratings, in B3's units;
- **T's change:** tonight's strength less the last game's; 0 for T's first game of the season.

lineup_change is the home team's change less the away team's.

**The decision:** its primary measure is its move skill against the opener's drift. It is kept when
it passes Holm's step-down at ALPHA over the family of five with a negative estimate, the four
others at their p-values in #389's record (EARLIER). Their decisions stand. B3's split plus
lineup_change, paired against the split, is descriptive only.

It reads the lake to the explore half's last season only, and writes
reports/yardstick/lineup-change-<yyyymmdd>-<shortsha>.md. It writes nothing to the lake.
"""

import argparse
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from nhl_edge.backtest import metrics, reports
from nhl_edge.backtest import yardstick as ys
from nhl_edge.game import b3
from nhl_edge.lake.tables import Lake

ALPHA = ys.ALPHA
NAME = "lineup_change"
FAMILY = ("travel", "density", "early", "goalie_presumed", NAME)
EARLIER = Path("reports/yardstick/targeted-features-20261104-ee21ec8.json")
SKATERS = ("F", "D")
ON_ICE = 5.0
RATED = ("ev_off", "ev_def")
CANDIDATES: dict[str, tuple[str, ...]] = {
    NAME: (NAME,),
    # Descriptive, never part of the decision: whether it adds beyond B3's split.
    "B3 split": b3.SPLIT,
    "B3 split + lineup_change": (*b3.SPLIT, NAME),
}
BEFORE = timedelta(microseconds=1)
UTC_TYPE = pl.Datetime("us", "UTC")


def targets(games: pl.DataFrame, moments: pl.DataFrame) -> pl.DataFrame:
    """Each game of moments for each of its teams: game_id, team, season, is_home and the
    prediction time t, as the latest instant before it (known_by), for as-of joins that must
    read rows observed strictly before t."""
    sides = pl.concat(
        [
            games.select("game_id", "season", team=side, is_home=pl.lit(side == "home"))
            for side in ("home", "away")
        ]
    )
    return sides.join(moments.select("game_id", "prediction_utc"), on="game_id").with_columns(
        known_by=pl.col("prediction_utc") - BEFORE
    )


def ratings(player_ratings: pl.DataFrame) -> pl.DataFrame:
    """Each player's rating rows: ev_off + ev_def at each game he was rated for, by when it was
    observed, sorted for an as-of join."""
    return (
        player_ratings.filter(pl.col("component").is_in(RATED))
        .group_by("game_id", "player_id", "observed_utc")
        .agg(rating=pl.col("mean").sum(), parts=pl.len())
        .filter(pl.col("parts") == len(RATED))
        .select("player_id", "observed_utc", "rating")
        .sort("observed_utc")
    )


def rated(rows: pl.DataFrame, table: pl.DataFrame) -> pl.DataFrame:
    """rows (player_id, known_by, weight) with each player's latest rating observed by known_by,
    0 without one."""
    # Both sides are sorted on their keys; polars can't check that within by-groups.
    found = rows.sort("known_by").join_asof(
        table,
        left_on="known_by",
        right_on="observed_utc",
        by="player_id",
        strategy="backward",
        check_sortedness=False,
    )
    return found.with_columns(pl.col("rating").fill_null(0.0))


def strength(rows: pl.DataFrame) -> pl.DataFrame:
    """Each team-game's lineup strength: ON_ICE times its skaters' weighted mean rating, 0 when
    its weights sum to 0."""
    return rows.group_by("game_id", "team").agg(
        strength=pl.when(pl.col("weight").sum() > 0)
        .then(ON_ICE * (pl.col("weight") * pl.col("rating")).sum() / pl.col("weight").sum())
        .otherwise(0.0)
    )


def lineup_change(
    games: pl.DataFrame,
    lineups: pl.DataFrame,
    actual_lineups: pl.DataFrame,
    player_ratings: pl.DataFrame,
    moments: pl.DataFrame,
) -> pl.DataFrame:
    """Each game of moments with lineup_change (the module's rule), from rows observed before its
    prediction time only."""
    teams = targets(games, moments)
    table = ratings(player_ratings)
    tonight = (
        lineups.filter(pl.col("role").is_in(SKATERS))
        .join(teams, on=["game_id", "team"])
        .filter(pl.col("observed_utc") < pl.col("prediction_utc"))
        .select("game_id", "team", "player_id", "known_by", weight=pl.col("exp_5v5").fill_null(0.0))
    )
    # T's earlier games whose boxscore was public before t: the latest is its last game.
    played = (
        actual_lineups.select(
            last_id="game_id", team="team", season="season", last_utc="observed_utc"
        )
        .unique()
        .sort("last_utc")
    )
    last = (
        teams.sort("known_by")
        .join_asof(
            played,
            left_on="known_by",
            right_on="last_utc",
            by=["team", "season"],
            strategy="backward",
            check_sortedness=False,
        )
        .filter(pl.col("last_id").is_not_null(), pl.col("last_id") != pl.col("game_id"))
    )
    dressed = (
        actual_lineups.filter(pl.col("role").is_in(SKATERS))
        .select(last_id="game_id", team="team", player_id="player_id", toi_s="toi_s")
        .join(last.select("game_id", "team", "last_id", "known_by"), on=["last_id", "team"])
        .select("game_id", "team", "player_id", "known_by", weight=pl.col("toi_s").cast(pl.Float64))
    )
    now = strength(rated(tonight, table)).rename({"strength": "tonight"})
    before = strength(rated(dressed, table)).rename({"strength": "before"})
    change = (
        teams.select("game_id", "team", "is_home")
        .join(now, on=["game_id", "team"], how="left")
        .join(before, on=["game_id", "team"], how="left")
        .with_columns(
            change=pl.when(pl.col("before").is_null())
            .then(0.0)
            .otherwise(pl.col("tonight").fill_null(0.0) - pl.col("before"))
        )
    )
    home = change.filter(pl.col("is_home")).select("game_id", home="change")
    away = change.filter(~pl.col("is_home")).select("game_id", away="change")
    return (
        home.join(away, on="game_id")
        .select("game_id", lineup_change=pl.col("home") - pl.col("away"))
        .sort("game_id")
    )


def with_candidate(read: ys.Read) -> pl.DataFrame:
    """The yardstick's rows with lineup_change at each game's prediction time."""
    moments = read.frame.select("game_id", "prediction_utc")
    t3 = read.b3_tables
    found = lineup_change(read.games, t3.lineups, t3.actual_lineups, t3.player_ratings, moments)
    return read.frame.join(found, on="game_id").sort("game_id")


def earlier(path: Path = EARLIER) -> dict[str, float]:
    """The four other family members' p-values from #389's record."""
    record = json.loads(path.read_text())["decision"]["estimates"]
    found = {name: float(entry["p_value"]) for name, entry in record.items()}
    if set(found) != set(FAMILY) - {NAME}:
        raise ValueError(f"{path} holds {sorted(found)}, not the family's other four")
    return found


def decide(
    per_game: pl.DataFrame, found: Mapping[str, Any], others: Mapping[str, float]
) -> dict[str, Any]:
    """lineup_change's primary estimate and p-value, Holm's step-down over the family of five
    with the others at their recorded p-values, whether it is kept, and the descriptive pairing
    with B3's split."""
    primary, p = found[NAME]["move"], found[NAME]["move_p"]
    steps = metrics.holm({**others, NAME: p}, ALPHA)
    kept = steps[NAME]["passes"] and primary["mean"] < 0
    both = ys.paired(per_game, "B3 split + lineup_change", "B3 split")
    return {
        "family": list(FAMILY),
        "primary": primary,
        "p_value": p,
        "rested": found[NAME]["move_rested"],
        "result": found[NAME]["result"],
        "slope": found[NAME].get("slope"),
        "holm": steps,
        "kept": kept,
        "descriptive": {"lineup_change beyond B3's split": both},
    }


def _cell(estimate: Mapping[str, Any] | None, scale: float = 1000.0) -> str:
    if not estimate or ("mean" not in estimate and "value" not in estimate):
        return "none"
    m = estimate.get("mean", estimate.get("value"))
    assert m is not None
    return f"{m * scale:+.3f} [{estimate['low'] * scale:+.3f}, {estimate['high'] * scale:+.3f}]"


def markdown(found: Mapping[str, Any], decision: Mapping[str, Any], version: str) -> str:
    own = found[NAME]
    lines = [
        f"# lineup_change on the yardstick: {version}",
        "",
        "#370's fifth candidate, by the rule fixed on the issue before it was scored. The",
        "explore half (2013-14 and 2014-15), each season fitted on the earlier ones from 2012-13",
        "(ADR 0053), at E2's prediction time. Move skill is in thousandths of squared log-odds",
        "against the opener's drift: negative means it anticipates the move. Intervals are 95%",
        "weekly block bootstrap. The features' settings were tuned on these seasons (ADR 0011):",
        "development evidence only.",
        "",
        "| Measure | Games | Value |",
        "| --- | --- | --- |",
        f"| Move skill (primary) | {own['games']:,} | {_cell(own['move'])} |",
        f"| The same, rested games | {own['rested_games']:,} | {_cell(own['move_rested'])} |",
        f"| Result beyond the close | {own['games']:,} | {_cell(own['result'])} |",
        f"| Slope of the move on it | {own['games']:,} | {_cell(own.get('slope'), 1.0)} |",
        "",
        "**Holm's step-down over the family of five** (the other four at #389's p-values):",
        "",
        "| Candidate | p | Threshold | Passes |",
        "| --- | --- | --- | --- |",
    ]
    for name, step in sorted(decision["holm"].items(), key=lambda kv: kv[1]["rank"]):
        lines.append(
            f"| {name} | {step['p_value']:.3f} | {step['threshold']:.4f} "
            f"| {'yes' if step['passes'] else 'no'} |"
        )
    verdict = (
        "**lineup_change is kept:** it passes the step-down with a negative estimate."
        if decision["kept"]
        else "**lineup_change is not kept:** it doesn't pass the step-down with a negative "
        "estimate. The four earlier decisions stand: density is kept, travel, early and "
        "goalie_presumed are not."
    )
    beyond = decision["descriptive"]["lineup_change beyond B3's split"]
    lines += [
        "",
        verdict,
        "",
        "**Descriptive, never part of the decision:** B3's split plus lineup_change less the",
        f"split, {_cell(beyond)} thousandths.",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="lineup_change on the yardstick (#370).")
    parser.add_argument("--out", type=Path, default=Path("reports/yardstick"))
    args = parser.parse_args()
    others = earlier()
    read = ys.read(Lake(), max(ys.EXPLORE))
    per_game, found = ys.run(with_candidate(read), "explore", CANDIDATES)
    decision = decide(per_game, found, others)
    version = reports.version("lineup-change", datetime.now(UTC))
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / f"{version}.json").write_text(
        json.dumps({"figures": found, "decision": decision}, indent=2, sort_keys=True, default=str)
    )
    path = args.out / f"{version}.md"
    path.write_text(markdown(found, decision, version))
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
