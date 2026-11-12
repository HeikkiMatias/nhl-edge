"""#421 (#417): a richer xG, stage one. Does it change what the ratings forecast? Each definition
was fixed on the issue before any figure was read (the comment of 2026-11-12). It writes nothing
to the lake.

    uv run python scripts/richer_xg.py

**Step 1, the fit.** M0 is ADR 0010's design (xg.design), refitted by xg.fit's rule: each season
on every earlier season's scorable shots public before its fold start. M1 to M4 each add a group
of terms to the one before:
- M1, off-wing: the shooter shoots right and the shot's turned y > 0, or left and y < 0
  (players.shoots, a fact fixed before a player's debut);
- M2, score state: the shooting team leads, or trails, from every goal earlier in the game by
  game time and then play order;
- M3, the previous event: log(1 + d), log(1 + t) and log(1 + d / max(t, 1)), d the feet from the
  play logged just before the shot (the one its prev_ columns describe) and t prev_seconds, and an
  indicator for no previous play or no coordinates, the three then 0. The previous play's
  coordinates come from the cached play-by-play (RawStore, each shot's raw_key), turned by the
  shooting team's attack direction as the shot's are;
- M4, on an ADR 0010 rebound, the change of signed angle atan2(y, 89 - x) from the previous play,
  in degrees over 90.
M4 is the richer xG. Step 1's measures are on the explore seasons' shots: the paired per-shot log
loss of each model less M0's and less the one before, M0's and M4's calibration by season, and
AUC.

**Step 2, flurries** (on today's xG, the lake's shot_xg): a scored shot that is an ADR 0010
rebound of the same team's scored shot continues that shot's chain, and its xG is discounted by
the product of (1 - xG) over the chain's earlier shots.

**Step 3, downstream** on the yardstick's explore half (ADR 0053): B2's parts that read xG, rebuilt
in memory at their frozen settings: ΔS's 5v5 part (team_strength.strength's delta_5v5) and the
goalie effect ΔG (goalie.effects, averaged over the start scenarios known at the prediction as the
yardstick's delta_g is). The candidates, each alone and paired against today's part alone on the
move skill: richer 5v5 (from M4), flurry 5v5 (from the flurry xG) and richer goalie (ΔG from M4).
**The decision:** Holm's step-down at ALPHA over the three; a test holds when it passes with a
negative estimate. One or more holds: an ADR for a new xG version goes to the owner. None: xG stays
parked, the figures noted on #250.

It reads the lake, and the cached play-by-play, to the explore half's last season only, and writes
reports/yardstick/richer-xg-<yyyymmdd>-<shortsha>.md.
"""

import argparse
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score

from nhl_edge.backtest import metrics, reports
from nhl_edge.backtest import yardstick as ys
from nhl_edge.backtest.walk_forward import fold_start
from nhl_edge.features import goalie as ge
from nhl_edge.features import team_strength as ts
from nhl_edge.features import xg
from nhl_edge.game import b2
from nhl_edge.ingest.feeds import check_game_id
from nhl_edge.ingest.shots import attack_directions, previous_plays, unblocked_attempts
from nhl_edge.lake.raw import RawStore
from nhl_edge.lake.tables import Lake, Reader

ALPHA = ys.ALPHA
# Each model's added terms: each model is the one before it plus its own.
ADDED: dict[str, tuple[str, ...]] = {
    "M0": (),
    "M1": ("off_wing",),
    "M2": ("leads", "trails"),
    "M3": ("log_prev_distance", "log_prev_seconds", "log_prev_speed", "no_prev_coordinates"),
    "M4": ("angle_change",),
}
LEVELS = tuple(ADDED)
RICHER = "M4"
# The seasons scored: every one with xG to the explore half's last.
SCORED = tuple(range(xg.FIRST_SEASON, max(ys.EXPLORE) + 1, 10001))
# The probability gap within which the refitted M0 counts as the lake's shot_xg.
AGREEMENT = 1e-6
# Step 3's columns, each alone in a candidate.
PARTS = {
    "today 5v5": "five_today",
    "richer 5v5": "five_richer",
    "flurry 5v5": "five_flurry",
    "today goalie": "goalie_today",
    "richer goalie": "goalie_richer",
}
CANDIDATES: dict[str, tuple[str, ...]] = {name: (column,) for name, column in PARTS.items()}
FAMILY = ("richer 5v5", "flurry 5v5", "richer goalie")
PAIRED = {
    "richer 5v5": ("richer 5v5", "today 5v5"),
    "flurry 5v5": ("flurry 5v5", "today 5v5"),
    "richer goalie": ("richer goalie", "today goalie"),
}


def terms(level: str) -> tuple[str, ...]:
    """The level's added terms and every earlier level's."""
    upto = LEVELS[: LEVELS.index(level) + 1]
    return tuple(term for name in upto for term in ADDED[name])


def previous_coordinates(body: bytes, game_id: int) -> list[dict[str, Any]]:
    """Each unblocked attempt of a game's play-by-play with the play logged just before it in its
    period (ingest.shots.previous_plays, the play its prev_ columns describe): that play's event id
    and its coordinates turned by the shooting team's attack direction, as the shot's are, or null
    without coordinates or a direction."""
    data = json.loads(body)
    check_game_id(data, game_id, "play-by-play")
    shots = unblocked_attempts(data["plays"])
    directions = attack_directions(shots)
    previous = previous_plays(data["plays"])
    rows = []
    for play in shots:
        found = previous.get(play["eventId"])
        prev_id = prev_x = prev_y = None
        if found is not None:
            before, _ = found
            prev_id = before["eventId"]
            details = before.get("details") or {}
            x, y = details.get("xCoord"), details.get("yCoord")
            key = (play["periodDescriptor"]["number"], play["details"]["eventOwnerTeamId"])
            direction = directions.get(key)
            if x is not None and y is not None and direction:
                prev_x, prev_y = x * direction, y * direction
        rows.append(
            {
                "game_id": game_id,
                "event_id": play["eventId"],
                "prev_event_id": prev_id,
                "prev_x": prev_x,
                "prev_y": prev_y,
            }
        )
    return rows


PREVIOUS_SCHEMA = {
    "game_id": pl.Int64,
    "event_id": pl.Int64,
    "prev_event_id": pl.Int64,
    "prev_x": pl.Float64,
    "prev_y": pl.Float64,
}


def previous_frame(shots: pl.DataFrame, get: Callable[[str], bytes]) -> pl.DataFrame:
    """The previous play of every shot, from each game's cached play-by-play: the very response
    its shots were parsed from (raw_key)."""
    rows: list[dict[str, Any]] = []
    for game_id, raw_key in shots.select("game_id", "raw_key").unique().sort("game_id").iter_rows():
        rows += previous_coordinates(get(raw_key), game_id)
    return pl.DataFrame(rows, schema=PREVIOUS_SCHEMA)


def score_state(shots: pl.DataFrame) -> pl.DataFrame:
    """Each shot with whether the shooting team leads or trails, from every goal earlier in the
    game (any strength, penalty shots and empty nets included). Earlier is by game time and then
    play order, as ingest.shots.previous_plays orders plays: old feeds log some plays out of
    order, so play order alone can put a goal ahead of the shot taken just before it."""
    goal = pl.col("is_goal").cast(pl.Int32)
    home = (goal * pl.col("is_home").cast(pl.Int32)).alias("home_goal")
    away = (goal * (~pl.col("is_home")).cast(pl.Int32)).alias("away_goal")
    ordered = shots.select(
        "game_id", "event_id", "seconds", "sort_order", "is_home", home, away
    ).sort("game_id", "seconds", "sort_order")
    counted = ordered.with_columns(
        home_before=pl.col("home_goal").cum_sum().over("game_id") - pl.col("home_goal"),
        away_before=pl.col("away_goal").cum_sum().over("game_id") - pl.col("away_goal"),
    )
    own = pl.when(pl.col("is_home")).then("home_before").otherwise("away_before")
    other = pl.when(pl.col("is_home")).then("away_before").otherwise("home_before")
    return counted.select(
        "game_id",
        "event_id",
        leads=(own > other).cast(pl.Float64),
        trails=(own < other).cast(pl.Float64),
    )


def richer_frame(
    shots: pl.DataFrame, previous: pl.DataFrame, players: pl.DataFrame
) -> pl.DataFrame:
    """The scorable shots (xg.model_frame) with every model's terms, sorted by game and event."""
    hands = players.select(shooter_id="player_id", shoots="shoots")
    frame = (
        xg.model_frame(shots)
        .join(score_state(shots), on=["game_id", "event_id"], how="left")
        .join(previous, on=["game_id", "event_id"], how="left")
        .join(hands, on="shooter_id", how="left")
        .sort("game_id", "event_id")
    )
    x, y = pl.col("x").cast(pl.Float64), pl.col("y").cast(pl.Float64)
    px, py = pl.col("prev_x"), pl.col("prev_y")
    distance = ((x - px) ** 2 + (y - py) ** 2).sqrt()
    seconds = pl.col("prev_seconds").cast(pl.Float64)
    has = distance.is_not_null() & seconds.is_not_null()
    signed = pl.arctan2(y, xg.NET_X - x).degrees()
    prev_signed = pl.arctan2(py, xg.NET_X - px).degrees()
    return frame.with_columns(
        off_wing=(((pl.col("shoots") == "R") & (y > 0)) | ((pl.col("shoots") == "L") & (y < 0)))
        .fill_null(False)
        .cast(pl.Float64),
        log_prev_distance=pl.when(has).then(distance.log1p()).otherwise(0.0),
        log_prev_seconds=pl.when(has).then(seconds.log1p()).otherwise(0.0),
        log_prev_speed=pl.when(has)
        .then((distance / pl.max_horizontal(seconds, pl.lit(1.0))).log1p())
        .otherwise(0.0),
        no_prev_coordinates=(~has).cast(pl.Float64),
        angle_change=pl.when(pl.col("rebound") & px.is_not_null())
        .then((signed - prev_signed).abs() / 90)
        .otherwise(0.0),
    )


def design(frame: pl.DataFrame, seasons: tuple[int, ...], level: str) -> np.ndarray:
    """ADR 0010's columns (xg.design) and the level's added terms."""
    base = xg.design(frame, seasons)
    added = terms(level)
    if not added:
        return base
    return np.hstack([base, frame.select(added).to_numpy().astype(float)])


@dataclass(frozen=True)
class Fit:
    """A level's model for a season, as xg.fit fits ADR 0010's."""

    level: str
    season: int
    seasons: tuple[int, ...]
    model: LogisticRegression
    shots: int
    train_cutoff: datetime

    def predict(self, frame: pl.DataFrame) -> np.ndarray:
        return self.model.predict_proba(design(frame, self.seasons, self.level))[:, 1]


def fit(frame: pl.DataFrame, season: int, start: datetime, level: str) -> Fit:
    """The level's model for season, on the earlier seasons' shots public before start (the
    season's fold start), by xg.fit's rule and settings."""
    train = frame.filter(pl.col("season") < season, pl.col("observed_utc") < start)
    seasons = tuple(sorted(train["season"].unique().to_list()))
    model = LogisticRegression(C=xg.C, max_iter=xg.MAX_ITER)
    model.fit(design(train, seasons, level), train["is_goal"].to_numpy())
    cutoff = train["observed_utc"].max()
    if not isinstance(cutoff, datetime):
        raise ValueError(f"no shots to fit {level} for {season} on")
    return Fit(level, season, seasons, model, train.height, cutoff)


def levels_of(season: int) -> tuple[str, ...]:
    """Every level on the explore seasons (step 1); M0 and the richer xG on the rest (step 3)."""
    return LEVELS if season in ys.EXPLORE else ("M0", RICHER)


def predictions(frame: pl.DataFrame, calendar: pl.DataFrame) -> tuple[pl.DataFrame, list[Fit]]:
    """Each scored season's shots with each of its levels' probability (p_<level>, null where a
    level is not fitted) and the cutoff of its latest fit."""
    out, fits = [], []
    for season in SCORED:
        start = fold_start(calendar, season)
        test = frame.filter(pl.col("season") == season)
        columns = {}
        cutoff = None
        for level in LEVELS:
            if level not in levels_of(season):
                columns[f"p_{level}"] = pl.Series([None] * test.height, dtype=pl.Float64)
                continue
            model = fit(frame, season, start, level)
            fits.append(model)
            columns[f"p_{level}"] = pl.Series(model.predict(test), dtype=pl.Float64)
            cutoff = model.train_cutoff
        out.append(
            test.select(
                "game_id",
                "season",
                "game_date",
                "event_id",
                "team",
                "observed_utc",
                goal=pl.col("is_goal").cast(pl.Float64),
            ).with_columns(train_cutoff=pl.lit(cutoff, dtype=ys.UTC), **columns)
        )
    return pl.concat(out).sort("game_id", "event_id"), fits


def log_loss(p: str) -> pl.Expr:
    return metrics.log_loss(pl.col(p), pl.col("goal"))


def step1(scored: pl.DataFrame) -> dict[str, Any]:
    """Step 1's measures on the explore seasons' shots: each level's paired per-shot log loss less
    M0's and less the level before, M0's and M4's calibration by season, and AUC."""
    shots = scored.filter(pl.col("season").is_in(ys.EXPLORE))
    losses = shots.with_columns(**{f"loss_{lv}": log_loss(f"p_{lv}") for lv in LEVELS})
    out: dict[str, Any] = {"shots": shots.height, "goals": int(shots["goal"].sum()), "levels": {}}
    for i, level in enumerate(LEVELS):
        entry: dict[str, Any] = {
            "terms": list(ADDED[level]),
            "auc": float(roc_auc_score(shots["goal"].to_numpy(), shots[f"p_{level}"].to_numpy())),
            "log_loss": float(losses[f"loss_{level}"].mean()),  # type: ignore[arg-type]
        }
        if i:
            for label, other in (("vs_m0", "M0"), ("vs_before", LEVELS[i - 1])):
                paired = losses.with_columns(d=pl.col(f"loss_{level}") - pl.col(f"loss_{other}"))
                entry[label] = metrics.bootstrap(paired, "d").to_dict()
                entry[f"{label}_p"] = metrics.p_value(paired, "d")
        out["levels"][level] = entry
    out["calibration"] = {
        level: {
            str(season): {
                name: value.to_dict()
                for name, value in metrics.calibration(part, f"p_{level}", "goal").items()
            }
            for (season,), part in shots.sort("season").group_by("season", maintain_order=True)
        }
        for level in ("M0", RICHER)
    }
    return out


def flurry(today: pl.DataFrame, frame: pl.DataFrame) -> pl.DataFrame:
    """Today's xG with each shot discounted by its chain (step 2): a scored shot that is an ADR
    0010 rebound of the same team's scored shot continues that shot's chain, and its xG is
    multiplied by (1 - xG) of each earlier shot of the chain."""
    rows = frame.select("game_id", "event_id", "team", "rebound", "prev_event_id").join(
        today.select("game_id", "event_id", "xg"), on=["game_id", "event_id"]
    )
    known = {(r["game_id"], r["event_id"]): r for r in rows.iter_rows(named=True)}
    factors: dict[tuple[int, int], float] = {}

    def factor(key: tuple[int, int]) -> float:
        if key in factors:
            return factors[key]
        row = known[key]
        before = (key[0], row["prev_event_id"])
        linked = row["rebound"] and before in known and known[before]["team"] == row["team"]
        value = factor(before) * (1 - known[before]["xg"]) if linked else 1.0
        factors[key] = value
        return value

    discount = pl.DataFrame(
        [{"game_id": k[0], "event_id": k[1], "factor": factor(k)} for k in known],
        schema={"game_id": pl.Int64, "event_id": pl.Int64, "factor": pl.Float64},
    )
    out = today.join(discount, on=["game_id", "event_id"], how="left").with_columns(
        xg=pl.col("xg") * pl.col("factor").fill_null(1.0)
    )
    if "artifact_version" in out.columns:
        out = out.with_columns(artifact_version=pl.lit("richer-xg-flurry"))
    return out.drop("factor")


def as_shot_xg(scored: pl.DataFrame, today: pl.DataFrame, level: str) -> pl.DataFrame:
    """A level's probabilities as shot_xg rows: today's rows with xg, its fit's cutoff and the
    version replaced."""
    found = scored.select("game_id", "event_id", new=f"p_{level}", cutoff="train_cutoff")
    out = today.join(found, on=["game_id", "event_id"], how="left")
    if out["new"].null_count():
        raise ValueError(f"{out['new'].null_count()} of today's xG shots without {level}")
    return out.with_columns(
        xg=pl.col("new"),
        train_cutoff=pl.col("cutoff"),
        artifact_version=pl.lit(f"richer-xg-{level}"),
    ).drop("new", "cutoff")


def five_part(
    games: pl.DataFrame, shots: pl.DataFrame, strength_time: pl.DataFrame, shot_xg: pl.DataFrame
) -> pl.DataFrame:
    """ΔS's 5v5 part of each game from the given xG, at team strength's frozen settings."""
    history = ts.team_games(shots, shot_xg, strength_time)
    return ts.strength(games, history, ts.TUNED).select("game_id", "delta_5v5", "as_of_utc")


def goalie_part(
    read: ys.Read, games: pl.DataFrame, shots: pl.DataFrame, shot_xg: pl.DataFrame
) -> pl.DataFrame:
    """The goalie effect ΔG of each game from the given xG, at its frozen settings, averaged over
    the start scenarios known at the prediction as the yardstick's delta_g is (ys.features)."""
    candidates = read.b2_tables.goalie_starts.join(
        games.select("game_id", "game_date"), on=["game_id", "game_date"], how="semi"
    )
    goalies = ge.goalie_games(shots, shot_xg)
    team_shots = ge.team_shot_games(read.games, shots, shot_xg)
    rated = ge.effects(candidates, games, goalies, team_shots, ge.TUNED)
    version = reports.version(ge.COMPONENT, datetime.now(UTC))
    effects = ys.untuned(ge.rows(rated, ge.TUNED, version))
    tables = replace(read.b2_tables, goalie_effects=effects)
    moments = read.frame.select("game_id", "prediction_utc")
    inputs = b2.game_inputs(tables)
    usable, ready = b2.known_before(inputs, b2.candidates(tables), moments, "observed_utc")
    pairs = b2.scenarios(usable.select("game_id", "home", "away"), ready)
    return ys._expected(pairs, "delta_g")


def gap(a: pl.Series, b: pl.Series) -> tuple[int, float]:
    """How many of two aligned columns agree to AGREEMENT, and their largest gap."""
    diff = (a - b).abs()
    largest = diff.max()
    return int((diff <= AGREEMENT).sum()), float(largest) if isinstance(largest, float) else 0.0


def downstream(
    read: ys.Read,
    shots: pl.DataFrame,
    strength_time: pl.DataFrame,
    variants: Mapping[str, pl.DataFrame],
) -> tuple[pl.DataFrame, dict[str, Any]]:
    """The yardstick's rows with each part from each xG variant (today, richer, flurry), and the
    checks of the code: today's parts against the lake's ΔS 5v5 part and the yardstick's ΔG."""
    games = read.games.join(read.frame.select("game_id"), on="game_id", how="semi")
    frame = read.frame
    for name in ("today", "richer", "flurry"):
        five = five_part(games, shots, strength_time, variants[name])
        frame = frame.join(
            five.select("game_id", **{f"five_{name}": "delta_5v5", f"as_of_{name}": "as_of_utc"}),
            on="game_id",
        )
    for name in ("today", "richer"):
        part = goalie_part(read, games, shots, variants[name])
        frame = frame.join(part.select("game_id", **{f"goalie_{name}": "delta_g"}), on="game_id")
    late = frame.filter(pl.col("as_of_today") >= pl.col("prediction_utc")).height
    if late:
        raise ValueError(f"{late} games rated at or after their prediction time")
    explore = frame.filter(pl.col("season").is_in(ys.EXPLORE))
    lake = explore.join(
        read.b2_tables.team_strength.select("game_id", "game_date", lake_five="delta_5v5"),
        on=["game_id", "game_date"],
    )
    five_agree, five_gap = gap(lake["five_today"], lake["lake_five"])
    goalie_agree, goalie_gap = gap(explore["goalie_today"], explore["delta_g"])
    checks = {
        "games": explore.height,
        "five_agree": five_agree,
        "five_games": lake.height,
        "five_gap": five_gap,
        "goalie_agree": goalie_agree,
        "goalie_gap": goalie_gap,
    }
    return frame.drop("as_of_today", "as_of_richer", "as_of_flurry").sort("game_id"), checks


def paired_frame(per_game: pl.DataFrame, a: str, b: str, measure: str = "move") -> pl.DataFrame:
    """a's measure less b's, game by game on the games both scored (difference)."""
    left = per_game.filter(pl.col("candidate") == a)
    right = per_game.filter(pl.col("candidate") == b).select("game_id", other=measure)
    return left.join(right, on="game_id").with_columns(difference=pl.col(measure) - pl.col("other"))


def decide(per_game: pl.DataFrame) -> dict[str, Any]:
    """Each paired estimate, its p-value and the figures shown beside it; Holm's step-down at ALPHA
    over the family; and whether each test holds (passes with a negative estimate)."""
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
    return {"family": list(FAMILY), "estimates": estimates}


def calibration(frame: pl.DataFrame) -> dict[str, Any]:
    """Shown beside: the calibration slope of the result model's probabilities (the close and the
    part) for each candidate, and of the close alone."""
    seasons = ys.seasons_of("explore")
    frame = frame.filter(pl.col("season").is_between(ys.FIRST_FIT, max(seasons)))
    out: dict[str, Any] = {}
    for name, columns in CANDIDATES.items():
        games = ys.scored(frame, columns, seasons)
        out[name] = metrics.calibration(games, "p", "home_win")["slope"].to_dict()
        if "the close alone" not in out:
            close = metrics.calibration(games, "p_base", "home_win")["slope"]
            out["the close alone"] = close.to_dict()
    return out


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
    one, checks, decision = found["step1"], found["checks"], found["decision"]["estimates"]
    counts = found["counts"]
    seasons = [str(s) for s in ys.EXPLORE]
    lines = [
        f"# A richer xG, stage one ({version})",
        "",
        "#421 (#417), each definition fixed on the issue before any figure was read (the comment",
        "of 2026-11-12). Intervals are 95% weekly block bootstrap. Not fresh: #361, #366, #370,",
        "#418 and #420 read the explore half, so every p-value is development evidence only.",
        "",
        "**In-sample settings:** the downstream parts' half-lives and pulls were tuned with",
        f"today's xG on these seasons, through {found['tuning_cutoff']} (ADR 0011), which favours",
        "today's xG. The xG fits themselves read only earlier seasons, each before its fold",
        "start.",
        "",
        "## Step 1: the fit",
        "",
        f"{one['shots']:,} scorable shots and {one['goals']:,} goals in the explore seasons. "
        f"Refitted M0 equals the lake's shot_xg on {checks['m0_agree']:,} of "
        f"{checks['m0_shots']:,} shots of {SCORED[0]} to {SCORED[-1]} (largest gap "
        f"{checks['m0_gap']:.1e}).",
        "",
        f"Shots without the shooter's hand: {counts['no_hand']:,}; without the previous play's "
        f"coordinates: {counts['no_prev']:,}; rebounds: {counts['rebounds']:,}.",
        "",
        "Per-shot log loss in thousandths: negative means the model fits better.",
        "",
        "| Model | Adds | Log loss | Less M0 | p | Less the one before | p | AUC |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for level, entry in one["levels"].items():
        adds = ", ".join(entry["terms"]) or "ADR 0010"
        less_m0 = _thousandths(entry.get("vs_m0"), 4) if "vs_m0" in entry else ""
        less_before = _thousandths(entry.get("vs_before"), 4) if "vs_before" in entry else ""
        p0 = f"{entry['vs_m0_p']:.4f}" if "vs_m0_p" in entry else ""
        p1 = f"{entry['vs_before_p']:.4f}" if "vs_before_p" in entry else ""
        lines.append(
            f"| {level} | {adds} | {entry['log_loss'] * 1000:.2f} | {less_m0} | {p0} | "
            f"{less_before} | {p1} | {entry['auc']:.4f} |"
        )
    lines += [
        "",
        "Calibration by season (1 and 0 when calibrated):",
        "",
        "| Model | Season | Slope | Intercept |",
        "|---|---|---|---|",
    ]
    for level, by in one["calibration"].items():
        for season, values in by.items():
            lines.append(
                f"| {level} | {season} | {_plain(values['slope'])} | "
                f"{_plain(values['intercept'])} |"
            )
    lines += [
        "",
        "## Step 2: flurries",
        "",
        f"{counts['chained']:,} explore-season shots continue a chain; their mean discount is "
        f"{counts['mean_discount']:.3f}. Today's xG over the explore seasons is "
        f"{counts['xg_today']:,.1f}, the flurry xG {counts['xg_flurry']:,.1f}.",
        "",
        "## Step 3: downstream, on the yardstick",
        "",
        f"**The checks of the code:** rebuilt from the lake's shot_xg, the 5v5 part equals the "
        f"lake's ΔS 5v5 part on {checks['five_agree']:,} of {checks['five_games']:,} explore games "
        f"(largest gap {checks['five_gap']:.1e}), and ΔG the yardstick's delta_g on "
        f"{checks['goalie_agree']:,} of {checks['games']:,} (largest gap "
        f"{checks['goalie_gap']:.1e}).",
        "",
        "Move skill and result beyond the close in thousandths: negative means the part",
        "anticipates SBR's move beyond the opener's drift, or adds to the close.",
        "",
        "| Candidate | Games | Move skill | On the rested games | Result beyond the close |",
        "|---|---|---|---|---|",
    ]
    for name, figures in found["figures"].items():
        lines.append(
            f"| {name} | {figures['games']:,} | {ys._cell(figures['move'], 1000)} | "
            f"{ys._cell(figures['move_rested'], 1000)} | "
            f"{ys._cell(figures['result'], 1000)} |"
        )
    lines += ["", "| Result model | Calibration slope |", "|---|---|"]
    for name, value in found["calibration"].items():
        lines.append(f"| {name} | {_plain(value)} |")
    lines += [
        "",
        "## The decision",
        "",
        f"Holm's step-down at {ALPHA} over the three paired tests, each part from the new xG less",
        "the same part from today's. A test holds when it passes with a negative estimate.",
        "",
        "| Test | Paired move skill (thousandths) | p | Holm threshold | Holds |",
        "|---|---|---|---|---|",
    ]
    for name in FAMILY:
        entry = decision[name]
        lines.append(
            f"| {name} | {_thousandths(entry['primary'])} | {entry['p_value']:.4f} | "
            f"{entry['threshold']:.4f} | {'yes' if entry['holds'] else 'no'} |"
        )
    held = [n for n in FAMILY if decision[n]["holds"]]
    lines += [
        "",
        "**Holds:** "
        + (
            f"{', '.join(held)}: an ADR for a new xG version goes to the owner, with the rebuild "
            "as its own issue."
            if held
            else "none. xG stays parked, with these figures noted on #250."
        ),
        "",
        "**Shown beside** (paired, thousandths). On a back-to-back the parts know last night's",
        "game, which an opener of the evening before may not (ADR 0006).",
        "",
        "| Pairing | Rested | Back-to-back | "
        + " | ".join(seasons)
        + " | Result beyond the close |",
        "|---|---|---|" + "---|" * len(seasons) + "---|",
    ]
    for name in FAMILY:
        entry = decision[name]
        by = entry["per_season"]
        lines.append(
            f"| {name} | {_thousandths(entry['rested'])} | {_thousandths(entry['back_to_back'])} "
            "| "
            + " | ".join(_thousandths(by.get(s)) for s in seasons)
            + f" | {_thousandths(entry['result'])} |"
        )
    lines += [
        "",
        "## Limits",
        "",
        "- **Fit versus forecasts:** a better fit per shot often barely moves game forecasts.",
        "- **Frozen settings:** the downstream half-lives and pulls were tuned with today's xG,",
        "  which favours it.",
        "- **Rink bias:** finer location terms can amplify it.",
        "- **Stage one:** B3's parts read xG through RAPM and the finishing multipliers, which",
        "  only the full rebuild would test.",
        "",
    ]
    return "\n".join(lines)


def counts_of(frame: pl.DataFrame, today: pl.DataFrame, flurried: pl.DataFrame) -> dict[str, Any]:
    explore = pl.col("season").is_in(ys.EXPLORE)
    shots = frame.filter(explore)
    both = (
        today.filter(explore)
        .select("game_id", "event_id", "xg")
        .join(flurried.select("game_id", "event_id", flurry="xg"), on=["game_id", "event_id"])
    )
    chained = both.filter(pl.col("flurry") < pl.col("xg"))
    ratio = (chained["flurry"] / chained["xg"]).mean()
    return {
        "no_hand": int(shots["shoots"].null_count()),
        "no_prev": int(shots["no_prev_coordinates"].sum()),
        "rebounds": int(shots["rebound"].sum()),
        "chained": chained.height,
        "mean_discount": float(ratio) if isinstance(ratio, float) else 1.0,
        "xg_today": float(both["xg"].sum()),
        "xg_flurry": float(both["flurry"].sum()),
    }


def run(lake: Reader, get: Callable[[str], bytes]) -> dict[str, Any]:
    """Steps 1 to 3 on the lake, and the cached play-by-play, to the explore half's last season."""
    last = max(ys.EXPLORE)
    read = ys.read(lake, last)

    def upto(table: str) -> pl.DataFrame:
        return lake.read(table).filter(pl.col("season") <= last)

    shots, today, strength_time = upto("shots"), upto("shot_xg"), upto("strength_time")
    frame = richer_frame(shots, previous_frame(shots, get), lake.read("players"))
    scored, fits = predictions(frame, read.games.select("season", "start_utc"))
    for model in fits:
        if model.train_cutoff >= fold_start(read.games.select("season", "start_utc"), model.season):
            raise ValueError(f"{model.level} for {model.season} read a shot at its fold start")
    lake_xg = today.filter(pl.col("season").is_in(SCORED))
    m0 = lake_xg.join(scored.select("game_id", "event_id", "p_M0"), on=["game_id", "event_id"])
    m0_agree, m0_gap = gap(m0["xg"], m0["p_M0"])
    flurried = flurry(today, frame)
    variants = {"today": today, "richer": as_shot_xg(scored, today, RICHER), "flurry": flurried}
    yardstick, checks = downstream(read, shots, strength_time, variants)
    per_game, figures = ys.run(yardstick, "explore", CANDIDATES)
    return {
        "step1": step1(scored),
        "counts": counts_of(frame, today, flurried),
        "checks": checks | {"m0_agree": m0_agree, "m0_shots": m0.height, "m0_gap": m0_gap},
        "fits": [
            {
                "level": f.level,
                "season": f.season,
                "shots": f.shots,
                "train_cutoff": str(f.train_cutoff),
            }
            for f in fits
        ],
        "tuning_cutoff": str(ts.TUNED_CUTOFF),
        "figures": figures,
        "calibration": calibration(yardstick),
        "decision": decide(per_game),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="A richer xG, stage one (#421).")
    parser.add_argument("--out", type=Path, default=Path("reports/yardstick"))
    args = parser.parse_args()
    found = run(Lake(), RawStore().get)
    version = reports.version("richer-xg", datetime.now(UTC))
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / f"{version}.json").write_text(
        json.dumps(found, indent=2, sort_keys=True, default=str)
    )
    path = args.out / f"{version}.md"
    path.write_text(markdown(found, version))
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
