"""#370 (#361 step 4): targeted features on the yardstick's explore half, each fixed on the issue
before it was scored (the comment of 2026-11-04).

    uv run python scripts/targeted_features.py

**Where:** 2013-14 and 2014-15, each season fitted on the earlier ones from 2012-13 (ADR 0053), at
E2's prediction time: a second after SBR's opener at its assumed 10:00 ET. Every input is known by
then, and every difference is home less away. The primary measure is the move skill against the
opener's drift; beside it, the result beyond the close, and both on the rested games.

**The family,** five candidates (FAMILY):
1. **travel:** B2's own schedule inputs at the prediction time, `travel_diff` (per 1,000 km) and
   `tz_diff` (|home_tz| - |away_tz|).
2. **density:** from the schedule, counting the season's games played on earlier dates only:
   `dense3_diff` and `dense6_diff` (each team's games over the 3 and 6 days before tonight),
   `mismatch` (+1 when the away team is on a back-to-back and the home team rested 2 days or
   more, -1 the other way), `home_return` (the home team's road games in a row just before
   tonight) and `away_trip` (the away team's road games in a row before tonight).
3. **early:** `five_diff_early`, B3's 5v5 part times a flag for both teams within their first
   EARLY games, scored as B3's split plus it, paired against the split alone.
4. **goalie_presumed:** the goalie part if each team's likeliest starter at the prediction time
   starts, less the goalie part over the start scenarios (the mixture B3 predicts with); alone,
   and as the split plus it, paired against the split. The hypothesis: a negative coefficient.
5. **lineup_change:** a later PR on #370. Until it is scored it enters Holm's step-down at p = 1,
   which keeps every threshold where a family of five puts it.

**The decision:** a candidate is kept when its primary measure (the paired one for early and
goalie_presumed) passes Holm's step-down at ALPHA over the family with a negative estimate; travel
and density also need their rested interval below 0. A kept candidate goes to the owner as a
challenger input, as development evidence only (the confirm half is spent, ADR 0053).

It reads the lake to the explore half's last season only, and writes
reports/yardstick/targeted-features-<yyyymmdd>-<shortsha>.md. It writes nothing to the lake.
"""

import argparse
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl

from nhl_edge.backtest import metrics, reports
from nhl_edge.backtest import yardstick as ys
from nhl_edge.features.density import DENSITY, density, team_games
from nhl_edge.game import b2, b3
from nhl_edge.lake.tables import Lake

EARLY = 20
ALPHA = ys.ALPHA
FAMILY = ("travel", "density", "early", "goalie_presumed", "lineup_change")
TRAVEL = ("travel_diff", "tz_diff")
CANDIDATES: dict[str, tuple[str, ...]] = {
    "travel": TRAVEL,
    "density": DENSITY,
    "B3 split": b3.SPLIT,
    "early": (*b3.SPLIT, "five_diff_early"),
    "goalie_presumed": ("goalie_presumed",),
    "B3 split + goalie_presumed": (*b3.SPLIT, "goalie_presumed"),
    # Descriptive, added after the decision, never part of it: whether a kept candidate adds
    # beyond B3's split, which already carries the goalie-start mixture (#370's report).
    "B3 split + density": (*b3.SPLIT, *DENSITY),
}
DESCRIPTIVE = {"density beyond B3's split": ("B3 split + density", "B3 split")}
# Each family member's primary measure: a candidate's own move skill, or a pair's difference.
PRIMARY: dict[str, tuple[str, str | None]] = {
    "travel": ("travel", None),
    "density": ("density", None),
    "early": ("early", "B3 split"),
    "goalie_presumed": ("B3 split + goalie_presumed", "B3 split"),
}
SCHEDULE_FACTS = ("travel", "density")


def travel(b2_tables: b2.Tables, moments: pl.DataFrame) -> pl.DataFrame:
    """B2's travel and time-zone inputs of each game of moments, known before its prediction."""
    usable, _ = b2.known_before(
        b2.game_inputs(b2_tables), b2.candidates(b2_tables), moments, "observed_utc"
    )
    return usable.select(
        "game_id", "travel_diff", tz_diff=pl.col("home_tz").abs() - pl.col("away_tz").abs()
    )


def early(frame: pl.DataFrame, games: pl.DataFrame) -> pl.DataFrame:
    """Each game of frame (game_id, prediction_utc, five_diff) with five_diff_early: five_diff
    when both teams are within their first EARLY games of the season, counted from the games
    whose results were public before its prediction time, else 0."""
    per = team_games(games, frame.select("game_id", "prediction_utc"))
    flags = games.select("game_id", "home", "away")
    for side in ("home", "away"):
        flags = flags.join(
            per.select("game_id", pl.col("team").alias(side), pl.col("number").alias(f"{side}_n")),
            on=["game_id", side],
        )
    both = (pl.col("home_n") <= EARLY) & (pl.col("away_n") <= EARLY)
    return frame.join(flags.select("game_id", early=both), on="game_id").select(
        "game_id", five_diff_early=pl.when(pl.col("early")).then(pl.col("five_diff")).otherwise(0.0)
    )


def presumed(pairs: pl.DataFrame) -> pl.DataFrame:
    """Each game's goalie part if the likeliest pair starts (the pairs of greatest weight,
    averaged when tied) less the part over every pair by its weight (the mixture)."""
    w = pl.col("weight")
    return (
        pairs.group_by("game_id")
        .agg(
            top=pl.col("goalie_diff").filter(w == w.max()).mean(),
            mixture=(pl.col("goalie_diff") * w).sum() / w.sum(),
        )
        .select("game_id", goalie_presumed=pl.col("top") - pl.col("mixture"))
        .sort("game_id")
    )


def goalie_pairs(b3_tables: b3.Tables, moments: pl.DataFrame) -> pl.DataFrame:
    """Every start scenario of each game of moments, known before its prediction, as the
    yardstick averages its goalie part (ys.features)."""
    split = b3.split_inputs(b3.game_inputs(b3_tables))
    pool = b3_tables.goalie_starts.select("game_id", "team", "goalie_id", "p_start", "observed_utc")
    usable, ready = b2.known_before(split, pool, moments, "observed_utc")
    _, gammas = b3.multipliers(b3_tables)
    return b3.scenarios(usable, ready, gammas)


def with_candidates(read: ys.Read) -> pl.DataFrame:
    """The yardstick's rows with every candidate's columns."""
    frame = read.frame
    moments = frame.select("game_id", "prediction_utc")
    return (
        frame.join(travel(read.b2_tables, moments), on="game_id")
        .join(density(read.games, moments), on="game_id")
        .join(early(frame, read.games), on="game_id")
        .join(presumed(goalie_pairs(read.b3_tables, moments)), on="game_id")
        .sort("game_id")
    )


def paired_frame(
    per_game: pl.DataFrame, a: str, b: str, rested: bool = False, measure: str = "move"
) -> pl.DataFrame:
    """a's measure less b's, game by game on the games both scored (difference)."""
    if rested:
        per_game = per_game.filter(pl.col("rested"))
    left = per_game.filter(pl.col("candidate") == a)
    right = per_game.filter(pl.col("candidate") == b).select("game_id", other=measure)
    return left.join(right, on="game_id").with_columns(difference=pl.col(measure) - pl.col("other"))


def decide(per_game: pl.DataFrame, found: Mapping[str, Any]) -> dict[str, Any]:
    """Each family member's primary estimate, its p-value, Holm's step-down over the family of
    five (lineup_change at p = 1 until scored) and whether it is kept."""
    estimates: dict[str, dict[str, Any]] = {}
    for name, (a, b) in PRIMARY.items():
        if b is None:
            primary, p = found[a]["move"], found[a]["move_p"]
            rested = found[a]["move_rested"]
        else:
            both = paired_frame(per_game, a, b)
            primary = metrics.bootstrap(both, "difference").to_dict()
            p = metrics.p_value(both, "difference")
            rested = metrics.bootstrap(paired_frame(per_game, a, b, True), "difference").to_dict()
        estimates[name] = {"primary": primary, "p_value": p, "rested": rested}
    steps = metrics.holm({**{n: e["p_value"] for n, e in estimates.items()}, "lineup_change": 1.0})
    for name, entry in estimates.items():
        passes = steps[name]["passes"] and entry["primary"]["mean"] < 0
        if name in SCHEDULE_FACTS:
            passes = passes and entry["rested"]["high"] < 0
        entry |= {"threshold": steps[name]["threshold"], "kept": passes}
    described: dict[str, Any] = {}
    for name, (a, b) in DESCRIPTIVE.items():
        parts = {
            "all": paired_frame(per_game, a, b),
            "rested": paired_frame(per_game, a, b, True),
            "back_to_back": paired_frame(per_game.filter(~pl.col("rested")), a, b),
        }
        if "result" in per_game.columns:
            parts["result"] = paired_frame(per_game, a, b, measure="result")
        if all(frame.height for frame in parts.values()):
            described[name] = {
                k: metrics.bootstrap(frame, "difference").to_dict() for k, frame in parts.items()
            }
    return {
        "family": list(FAMILY),
        "estimates": estimates,
        "lineup_change": "not yet scored",
        "descriptive": described,
    }


def markdown(found: Mapping[str, Any], decision: Mapping[str, Any], version: str) -> str:
    def cell(e: Mapping[str, Any]) -> str:
        return ys._cell(e, 1000)

    lines = [
        f"# Targeted features on the yardstick ({version})",
        "",
        "#370, on the explore half (2013-14 and 2014-15, fitted from 2012-13), each candidate",
        "fixed on the issue before it was scored. Move skill in thousandths: negative means the",
        "candidate anticipates SBR's move beyond the opener's drift. Intervals are 95% weekly",
        "block bootstrap. The features' settings were tuned on these seasons (ADR 0011).",
        "",
        "Not fresh on these games: #361's diagnostics already read the early-season subset's move",
        "skill and, descriptively, where the goalie starter surprised (reports/yardstick/",
        "yardstick-diagnostics-20261101-c7bfa52.md), so early's p-value is development evidence",
        "twice over. Only travel and density must hold on the rested games by the rule; the",
        "others' rested intervals are given beside, since a start probability can read last",
        "night's game, which an opener of the evening before may not have known (ADR 0006).",
        "",
        "| Candidate | Games | Move skill | On the rested games | Result beyond the close |",
        "|---|---|---|---|---|",
    ]
    for name, figures in found.items():
        lines.append(
            f"| {name} | {figures['games']:,} | {cell(figures['move'])} | "
            f"{cell(figures['move_rested'])} | {cell(figures['result'])} |"
        )
    lines += [
        "",
        "**The family's decision** (Holm's step-down at 0.05 over five, lineup_change at p = 1",
        "until scored; early and goalie_presumed are paired against B3's split):",
        "",
        "| Candidate | Primary estimate | On the rested games | p | Holm threshold | Kept |",
        "|---|---|---|---|---|---|",
    ]
    for name, entry in decision["estimates"].items():
        lines.append(
            f"| {name} | {cell(entry['primary'])} | {cell(entry['rested'])} | "
            f"{entry['p_value']:.4f} | {entry['threshold']:.4f} | "
            f"{'yes' if entry['kept'] else 'no'} |"
        )
    lines += ["| lineup_change | not yet scored | | | | |", ""]
    kept = [n for n, e in decision["estimates"].items() if e["kept"]]
    notes = [
        f"{n} (rested interval {'below 0' if e['rested']['high'] < 0 else 'includes 0'})"
        for n, e in decision["estimates"].items()
        if e["kept"]
    ]
    lines.append(
        f"**Kept:** {', '.join(notes)}: proposed to the owner as challenger inputs, as "
        "development evidence only."
        if kept
        else "**Kept:** none of the four scored. lineup_change follows in its own PR."
    )
    lines += [
        "",
        "**Descriptive, after the decision and not part of it:** a kept candidate on top of B3's",
        "split, which already carries the goalie-start mixture, paired against the split (move",
        "skill and result beyond the close, thousandths):",
        "",
        "| Comparison | All games | Rested | Back-to-back | Result beyond the close |",
        "|---|---|---|---|---|",
    ]
    for name, entry in decision.get("descriptive", {}).items():
        lines.append(
            f"| {name} | {cell(entry['all'])} | {cell(entry['rested'])} | "
            f"{cell(entry['back_to_back'])} | {cell(entry['result'])} |"
        )
    return "\n".join([*lines, ""])


def main() -> None:
    parser = argparse.ArgumentParser(description="Targeted features on the yardstick (#370).")
    parser.add_argument("--out", type=Path, default=Path("reports/yardstick"))
    args = parser.parse_args()
    read = ys.read(Lake(), max(ys.EXPLORE))
    per_game, found = ys.run(with_candidates(read), "explore", CANDIDATES)
    decision = decide(per_game, found)
    version = reports.version("targeted-features", datetime.now(UTC))
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / f"{version}.json").write_text(
        json.dumps({"figures": found, "decision": decision}, indent=2, sort_keys=True, default=str)
    )
    path = args.out / f"{version}.md"
    path.write_text(markdown(found, decision, version))
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
