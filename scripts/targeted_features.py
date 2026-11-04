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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from nhl_edge.backtest import metrics, reports
from nhl_edge.backtest import yardstick as ys
from nhl_edge.game import b2, b3
from nhl_edge.lake.tables import Lake

EARLY = 20
ALPHA = ys.ALPHA
FAMILY = ("travel", "density", "early", "goalie_presumed", "lineup_change")
TRAVEL = ("travel_diff", "tz_diff")
DENSITY = ("dense3_diff", "dense6_diff", "mismatch", "home_return", "away_trip")
CANDIDATES: dict[str, tuple[str, ...]] = {
    "travel": TRAVEL,
    "density": DENSITY,
    "B3 split": b3.SPLIT,
    "early": (*b3.SPLIT, "five_diff_early"),
    "goalie_presumed": ("goalie_presumed",),
    "B3 split + goalie_presumed": (*b3.SPLIT, "goalie_presumed"),
}
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


def team_games(games: pl.DataFrame) -> pl.DataFrame:
    """Each team's games of each season in order: date, whether at home, its rest in days (None
    for its first), its games over the 3 and 6 days before and its road games in a row before,
    each from its earlier dates only."""
    sides = pl.concat(
        [
            games.select(
                "game_id",
                "season",
                "game_date",
                "start_utc",
                team=side,
                home=pl.lit(side == "home"),
            )
            for side in ("home", "away")
        ]
    ).sort("season", "team", "start_utc")
    by = ["season", "team"]
    sides = sides.with_columns(n=pl.int_range(pl.len()).over(by))
    earlier = sides.select(*by, other_date="game_date", other_n="n")
    window = (
        sides.join(earlier, on=by)
        .filter(pl.col("other_date") < pl.col("game_date"))
        .group_by("game_id", "team")
        .agg(
            dense3=(pl.col("other_date") >= pl.col("game_date") - timedelta(days=3)).sum(),
            dense6=(pl.col("other_date") >= pl.col("game_date") - timedelta(days=6)).sum(),
        )
    )
    last_home = pl.when(pl.col("home")).then(pl.col("n")).shift(1).forward_fill().over(by)
    return (
        sides.with_columns(
            rest=(pl.col("game_date") - pl.col("game_date").shift(1).over(by)).dt.total_days(),
            road=pl.col("n") - last_home.fill_null(-1) - 1,
        )
        .join(window, on=["game_id", "team"], how="left")
        .with_columns(pl.col("dense3", "dense6").fill_null(0).cast(pl.Int32))
    )


def density(games: pl.DataFrame) -> pl.DataFrame:
    """Each game's density columns (DENSITY) from every played game of the schedule before its
    date: the schedule is public a day before (ADR 0005), and a game played on an earlier date is
    over by the prediction time."""
    per = team_games(games)
    out = games.select("game_id", "home", "away")
    for side in ("home", "away"):
        part = per.select(
            "game_id",
            pl.col("team").alias(side),
            *(pl.col(c).alias(f"{side}_{c}") for c in ("rest", "dense3", "dense6", "road")),
        )
        out = out.join(part, on=["game_id", side])
    tired = {side: pl.col(f"{side}_rest") == 1 for side in ("home", "away")}
    fresh = {side: pl.col(f"{side}_rest").fill_null(99) >= 2 for side in ("home", "away")}
    return out.select(
        "game_id",
        dense3_diff=(pl.col("home_dense3") - pl.col("away_dense3")).cast(pl.Float64),
        dense6_diff=(pl.col("home_dense6") - pl.col("away_dense6")).cast(pl.Float64),
        mismatch=(
            (tired["away"].fill_null(False) & fresh["home"]).cast(pl.Float64)
            - (tired["home"].fill_null(False) & fresh["away"]).cast(pl.Float64)
        ),
        home_return=pl.col("home_road").cast(pl.Float64),
        away_trip=pl.col("away_road").cast(pl.Float64),
    )


def early(frame: pl.DataFrame, games: pl.DataFrame) -> pl.DataFrame:
    """Each game of frame with five_diff_early: five_diff when both teams are within their first
    EARLY games of the season, by start, else 0."""
    per = team_games(games).select("game_id", "team", number=pl.col("n") + 1)
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
        .join(density(read.games), on="game_id")
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
    return {"family": list(FAMILY), "estimates": estimates, "lineup_change": "not yet scored"}


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
    lines.append(
        f"**Kept:** {', '.join(kept)}: proposed to the owner as challenger inputs, as development "
        "evidence only."
        if kept
        else "**Kept:** none of the four scored. lineup_change follows in its own PR."
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
