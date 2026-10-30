"""The market-relative yardstick (#361, ADR 0053): a feature is scored on the training seasons by
what it knows that the market doesn't yet, rather than by the noisy result alone (ADR 0011).

Two targets, in the home side's log-odds, from SBR's opener and close de-vigged under B1_METHOD
(hard rule 3); the close is a target only, never an input:
- **The move:** y = c - o, from the opener o to the close c. Each scored season fits the forecast
  y ~ a + b_o·o + b·x and the baseline y ~ a + b_o·o (the opener's own drift) by least squares on
  the earlier seasons. A game's move skill is (y - ŷ)² - (y - ŷ₀)²: negative when the feature
  anticipates the move.
- **The result beyond the close:** the log loss of a logistic model of the home team's full-game
  win (hard rule 2) on c and x, less that of c alone, each fitted the same way: gate 3's question.

**Where.** The training seasons (ADR 0011): every fit reads the games from FIRST_FIT whose results
were public before the scored season's first game. The explore half (EXPLORE) is for looking;
a claim from it is written down, then scored once on the confirm half (CONFIRM). A run reads no
season after its half's last. Every figure is also given on the rested games, where neither team
played the day before: SBR's opener may be from the evening before (ADR 0006), before the results
that the 10:00 ET features know.

**The features** are B2's and B3's inputs at E2's prediction time, a second after the opener's
assumed 10:00 ET (backtest/market.py), with the goalie part over the start scenarios known then
(ADR 0012), never the starters. Their frozen settings were tuned on these seasons' results, so
the tuned tables' rows carry the tuning cutoff as their observed time (ADR 0011). As B2's own
tuning does (b2.tuning_scores), the yardstick reads each row at the time its data was known
(untuned): in-sample in its settings by design. The confirmation is live (ADR 0038).
"""

import io
import json
from collections.abc import Mapping, Sequence
from dataclasses import fields, replace
from datetime import datetime
from hashlib import sha256
from typing import Any

import numpy as np
import polars as pl
from sklearn.linear_model import LogisticRegression

from nhl_edge.backtest import walk_forward as wf
from nhl_edge.backtest.market import Experiment, market_prices
from nhl_edge.backtest.metrics import bootstrap, log_loss, slope
from nhl_edge.backtest.seasons import TRAINING_SEASONS
from nhl_edge.features.team_strength import TUNED_CUTOFF
from nhl_edge.game import b2, b3
from nhl_edge.live import bundle as lb

# B3's first season with features: fitted on, never scored.
FIRST_FIT = 20112012
EXPLORE = (20122013, 20132014, 20142015)
CONFIRM = (20152016, 20162017, 20172018)
HALVES = {"explore": EXPLORE, "confirm": CONFIRM}
# B2's inputs, B3's expected goal difference and its four parts (ADR 0044), which sum to it.
B2_INPUTS = ("delta_s", "delta_g", "h_s")
FEATURES = (*B2_INPUTS, "delta_g_hat", *b3.SPLIT)
CANDIDATES: dict[str, tuple[str, ...]] = {
    **{name: (name,) for name in FEATURES},
    "B2 inputs": B2_INPUTS,
    "B3 split": b3.SPLIT,
    # Δĝ is left out: it is the split's sum.
    "all": (*B2_INPUTS, *b3.SPLIT),
}
# The first season a feature has its inputs, when later than FIRST_FIT: every 2011-12 shooter
# finishes at φ = 1, with no earlier season of xG to measure on (ADR 0022). As ADR 0011 scores a
# season only once an earlier one has the component's inputs, a candidate's fits start at its
# features' first such season and its scoring a season later.
FIRST_INPUTS = {"finishing_diff": 20122013}
PREFIX = "bundles/yardstick"
UTC = pl.Datetime("us", "UTC")


def seasons_of(half: str) -> tuple[int, ...]:
    """The half's scored seasons: training seasons only (ADR 0011)."""
    seasons = HALVES[half]
    assert set(seasons) <= set(TRAINING_SEASONS)
    return seasons


def first_inputs(columns: Sequence[str]) -> int:
    """The first season all the columns have their inputs (FIRST_INPUTS): a candidate's fits
    start there and its scoring a season later."""
    return max(FIRST_INPUTS.get(name, FIRST_FIT) for name in columns)


def untuned(frame: pl.DataFrame) -> pl.DataFrame:
    """A feature table's rows observed when their data was known (as_of_utc, or known_utc when
    later), not from the tuning cutoff of their frozen settings. Refused unless that cutoff is
    the only difference: each row is observed at its data's time, or at TUNED_CUTOFF when that is
    later. A table without as_of_utc is observed at its data's time already."""
    if "as_of_utc" not in frame.columns:
        return frame
    data = (
        pl.max_horizontal("as_of_utc", "known_utc")
        if "known_utc" in frame.columns
        else pl.col("as_of_utc")
    )
    lifted = (pl.col("observed_utc") == pl.lit(TUNED_CUTOFF, dtype=UTC)) & (data < TUNED_CUTOFF)
    wrong = frame.filter(~((pl.col("observed_utc") == data) | lifted))
    if wrong.height:
        raise ValueError(
            f"{wrong.height} rows observed neither at their data's time nor the cutoff"
        )
    return frame.with_columns(observed_utc=data)


def through[T: (b2.Tables, b3.Tables)](tables: T, last: int) -> T:
    """The tables cut to the seasons to last, each read at its data's time (untuned)."""
    cut = {}
    for item in fields(tables):
        frame = getattr(tables, item.name)
        if frame is None:
            continue
        if "season" in frame.columns:
            frame = frame.filter(pl.col("season") <= last)
        cut[item.name] = untuned(frame)
    return replace(tables, **cut)


def _expected(pairs: pl.DataFrame, *columns: str) -> pl.DataFrame:
    """Each game's columns averaged over its goalie pairs by their weights."""
    weight = pl.col("weight")
    return pairs.group_by("game_id").agg(
        *((pl.col(c) * weight).sum().truediv(weight.sum()).alias(c) for c in columns)
    )


def features(b2_tables: b2.Tables, b3_tables: b3.Tables, moments: pl.DataFrame) -> pl.DataFrame:
    """Each game of moments (game_id, prediction_utc) whose B2 and B3 inputs were known before
    its prediction time, with FEATURES and the rest flags: B2's ΔS, h_s and ΔG, and B3's Δĝ and
    its parts, each goalie term averaged over the start scenarios known then (b2.known_before),
    as B2 and B3 predict. The tables are read as through() gives them."""
    inputs = b2.game_inputs(b2_tables)
    usable, ready = b2.known_before(inputs, b2.candidates(b2_tables), moments, "observed_utc")
    delta_g = _expected(b2.scenarios(usable.select("game_id", "home", "away"), ready), "delta_g")
    split = b3.split_inputs(b3.game_inputs(b3_tables))
    pool = b3_tables.goalie_starts.select("game_id", "team", "goalie_id", "p_start", "observed_utc")
    usable3, ready3 = b2.known_before(split, pool, moments, "observed_utc")
    _, gammas = b3.multipliers(b3_tables)
    goalies = _expected(b3.scenarios(usable3, ready3, gammas), "delta_g_hat", "goalie_diff")
    return (
        usable.select(
            "game_id",
            "delta_s",
            "home_back_to_back",
            "away_back_to_back",
            h_s="offset",
        )
        .join(delta_g, on="game_id")
        .join(
            usable3.select("game_id", "five_diff", "special_diff", "finishing_diff"), on="game_id"
        )
        .join(goalies, on="game_id")
        .select("game_id", *FEATURES, "home_back_to_back", "away_back_to_back")
        .sort("game_id")
    )


def _logit(p: pl.Expr) -> pl.Expr:
    return (p / (1 - p)).log()


def market(sbr_odds: pl.DataFrame, games: pl.DataFrame) -> pl.DataFrame:
    """Each game from FIRST_FIT with its opener at E2's prediction time and its close, de-vigged
    under B1_METHOD: o and c, with the openers E2 refuses dropped by its fold's bounds (ADR 0007),
    season by season."""
    calendar = games.select("season", "start_utc")
    opens = market_prices(sbr_odds, Experiment.E2)
    closes = wf.b0(market_prices(sbr_odds, Experiment.E1), wf.B1_METHOD)
    kept = []
    # Earlier seasons' closes bound FIRST_FIT's openers; their own games are never read.
    for season in sorted(s for s in set(opens["season"].to_list()) if s >= FIRST_FIT):
        rows = opens.filter(pl.col("season") == season)
        low, high = wf.bounds(closes, wf.fold_start(calendar, season))
        kept.append(rows.filter(~wf.implausible(rows, low, high)))
    open_p = wf.b0(pl.concat(kept), wf.B1_METHOD)
    return (
        open_p.select(
            "game_id", "season", "game_date", "start_utc", "prediction_utc", p_open="p_home"
        )
        .join(closes.select("game_id", p_close="p_home"), on="game_id")
        .with_columns(o=_logit(pl.col("p_open")), c=_logit(pl.col("p_close")))
        .with_columns(y=pl.col("c") - pl.col("o"))
        .sort("game_id")
    )


def rows(
    market_rows: pl.DataFrame, feature_rows: pl.DataFrame, games: pl.DataFrame
) -> pl.DataFrame:
    """The yardstick's games: the market's, with their features, their result on the full game
    (OT and shootout included, hard rule 2) and when it was public, and whether neither team
    played the day before (rested)."""
    return (
        market_rows.join(feature_rows, on="game_id")
        .join(wf.outcomes(games), on="game_id")
        # B2's flags are 1.0 for a team that played the day before.
        .with_columns(
            rested=(pl.col("home_back_to_back") == 0) & (pl.col("away_back_to_back") == 0)
        )
        .drop("home_back_to_back", "away_back_to_back")
        .sort("game_id")
    )


def _least_squares(train: pl.DataFrame, test: pl.DataFrame, columns: Sequence[str]) -> np.ndarray:
    def design(frame: pl.DataFrame) -> np.ndarray:
        return np.column_stack([np.ones(frame.height), frame.select(columns).to_numpy()])

    beta, *_ = np.linalg.lstsq(design(train), train["y"].to_numpy(), rcond=None)
    return design(test) @ beta


def _logistic(train: pl.DataFrame, test: pl.DataFrame, columns: Sequence[str]) -> np.ndarray:
    # An unpenalized fit, as ADR 0011's win model: C is the inverse of the L2 strength.
    model = LogisticRegression(C=np.inf, max_iter=1000)
    model.fit(train.select(columns).to_numpy(), train["home_win"].to_numpy())
    return model.predict_proba(test.select(columns).to_numpy())[:, 1]


def scored(frame: pl.DataFrame, columns: Sequence[str], seasons: Sequence[int]) -> pl.DataFrame:
    """Each game of the seasons after the columns' first_inputs with its move skill (move) and
    its result beyond the close (result), each season's fits on the games from first_inputs to
    the season before whose results were public before its first game (wf.fold_start over
    frame's games). The close is only y's
    and the result model's: the move's forecast reads o and the features alone."""
    calendar = frame.select("season", "start_utc")
    first = first_inputs(columns)
    out = []
    for season in sorted(s for s in seasons if s > first):
        start = wf.fold_start(calendar, season)
        train = frame.filter(
            pl.col("season").is_between(first, season - 1), pl.col("result_utc") < start
        )
        if train.height < 2:
            raise ValueError(f"no earlier games to fit on before {season}")
        test = frame.filter(pl.col("season") == season)
        y_hat = _least_squares(train, test, ["o", *columns])
        y_base = _least_squares(train, test, ["o"])
        p = _logistic(train, test, ["c", *columns])
        p_base = _logistic(train, test, ["c"])
        out.append(
            test.select("game_id", "season", "game_date", "rested", "y", *columns).with_columns(
                y_hat=pl.Series(y_hat),
                y_base=pl.Series(y_base),
                p=pl.Series(p),
                p_base=pl.Series(p_base),
                home_win=test["home_win"],
            )
        )
    games = pl.concat(out)
    return games.with_columns(
        move=(pl.col("y") - pl.col("y_hat")) ** 2 - (pl.col("y") - pl.col("y_base")) ** 2,
        result=log_loss(pl.col("p"), pl.col("home_win"))
        - log_loss(pl.col("p_base"), pl.col("home_win")),
    ).sort("game_id")


def figures(games: pl.DataFrame, columns: Sequence[str]) -> dict[str, Any]:
    """A candidate's figures over its scored games, each with its weekly block bootstrap interval
    (hard rule 7): the move skill, pooled, per season and on the rested games, the result beyond
    the close, and for a single feature the slope of the move on it."""
    rested = games.filter(pl.col("rested"))
    out: dict[str, Any] = {
        "games": games.height,
        "rested_games": rested.height,
        "move": bootstrap(games, "move").to_dict(),
        "move_rested": bootstrap(rested, "move").to_dict(),
        "move_per_season": {
            str(season): bootstrap(part, "move").to_dict()
            for (season,), part in games.sort("season").group_by("season", maintain_order=True)
        },
        "result": bootstrap(games, "result").to_dict(),
        "result_rested": bootstrap(rested, "result").to_dict(),
        # The baseline's squared error, to scale the move skill by.
        "baseline_error": float(((games["y"] - games["y_base"]) ** 2).mean()),  # type: ignore[arg-type]
    }
    if len(columns) == 1:
        # A feature with no spread over the games, but float noise, has no slope to estimate.
        spread = games[columns[0]].std()
        varies = isinstance(spread, float) and spread > 1e-12
        out["slope"] = slope(games, columns[0], "y").to_dict() if varies else None
    return out


def run(
    frame: pl.DataFrame, half: str, candidates: Mapping[str, Sequence[str]] = CANDIDATES
) -> tuple[pl.DataFrame, dict[str, Any]]:
    """Every candidate scored on the half's seasons: their per-game rows (candidate, game_id,
    season, game_date, rested, move, result) and their figures. It reads no game of a season
    after the half's last."""
    seasons = seasons_of(half)
    frame = frame.filter(pl.col("season").is_between(FIRST_FIT, max(seasons)))
    per_game, found = [], {}
    for name, columns in candidates.items():
        games = scored(frame, columns, seasons)
        found[name] = figures(games, columns)
        per_game.append(
            games.select(
                pl.lit(name).alias("candidate"),
                "game_id",
                "season",
                "game_date",
                "rested",
                "move",
                "result",
            )
        )
    return pl.concat(per_game), found


def paired(per_game: pl.DataFrame, a: str, b: str, measure: str = "move") -> dict[str, Any]:
    """Candidate a's measure less b's, game by game on the games both scored, with its weekly
    block bootstrap interval: negative favours a."""
    left = per_game.filter(pl.col("candidate") == a)
    right = per_game.filter(pl.col("candidate") == b).select("game_id", other=measure)
    both = left.join(right, on="game_id").with_columns(difference=pl.col(measure) - pl.col("other"))
    return bootstrap(both, "difference").to_dict()


def _cell(estimate: Mapping[str, Any], scale: float = 1.0) -> str:
    return (
        f"{estimate['mean'] * scale:+.3f} [{estimate['low'] * scale:+.3f}, "
        f"{estimate['high'] * scale:+.3f}]"
    )


def markdown(found: Mapping[str, Any], half: str, version: str) -> str:
    """The run's report: every candidate's figures, with intervals."""
    seasons = seasons_of(half)
    lines = [
        f"# Yardstick: {half}, {version}",
        "",
        f"The market-relative yardstick (ADR 0053) on {seasons[0]} to {seasons[-1]}, each season",
        "fitted on the earlier ones from 2011-12, or from a feature's first season with its",
        "inputs (2012-13 for finishing, ADR 0022), and scored only after it. Intervals are 95%",
        "weekly block bootstrap.",
        "",
        "- **Move skill** (in thousandths): the squared error of the forecast of SBR's move from",
        "  the opener to the close, in log-odds, less that of the opener's own drift. Negative",
        "  means the feature anticipates the move.",
        "- **Rested:** the games where neither team played the day before, which a stale opener",
        "  can't flatter (ADR 0006).",
        "- **Beyond the close** (in thousandths): the log loss of the close and the feature, less",
        "  the close alone. Negative means the feature adds to the closing price.",
        "- The features' frozen settings were tuned on these seasons' results (ADR 0011): they",
        "  are in-sample in their settings by design.",
        "",
        "| Candidate | Games (rested) | Move skill | Rested | Beyond the close | Slope of the "
        "move |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for name, row in found.items():
        trend = row.get("slope")
        slope_cell = (
            f"{trend['value']:+.3f} [{trend['low']:+.3f}, {trend['high']:+.3f}]" if trend else ""
        )
        lines.append(
            f"| {name} | {row['games']:,} ({row['rested_games']:,}) | {_cell(row['move'], 1000)} "
            f"| {_cell(row['move_rested'], 1000)} | {_cell(row['result'], 1000)} | {slope_cell} |"
        )
    first = next(iter(found.values()))
    lines += [
        "",
        f"The baseline's mean squared error, on {first['games']:,} games, is "
        f"{first['baseline_error'] * 1000:.3f} thousandths.",
        "",
        "## Move skill per season (in thousandths)",
        "",
        "| Candidate | " + " | ".join(str(s) for s in seasons) + " |",
        "| --- | " + " | ".join("---" for _ in seasons) + " |",
    ]
    for name, row in found.items():
        by = row["move_per_season"]
        cells = [_cell(by[str(s)], 1000) if str(s) in by else "not scored" for s in seasons]
        lines.append(f"| {name} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def run_key(half: str, version: str, claim: str | None, now: datetime) -> str:
    """A run's place in the store: a confirm run is named by its claim alone, so it is kept once;
    an explore run by its version and time."""
    if half == "confirm":
        return f"{PREFIX}/confirm-{claim}"
    return f"{PREFIX}/{half}-{version}-{now:%Y%m%dT%H%M%SZ}"


def keep(
    store: lb.Store,
    key: str,
    per_game: pl.DataFrame,
    found: Mapping[str, Any],
    report: str,
    identity: Mapping[str, Any],
    now: datetime,
) -> None:
    """The run's rows, figures and report, each written once, then its manifest, the last: a
    second run under the same key, such as a confirm run of the same claim, is refused."""
    bodies = {
        "rows.parquet": lb.parquet(per_game),
        "figures.json": json.dumps(found, sort_keys=True).encode(),
        "report.md": report.encode(),
    }
    manifest_key = f"{key}/manifest.json"
    try:
        store.get(manifest_key)
    except Exception:
        pass
    else:
        raise ValueError(f"{key} is kept already: a run is kept once")
    for name, body in bodies.items():
        lb.put_once(store, f"{key}/{name}", body)
    manifest = dict(identity) | {
        "kept_utc": now.isoformat(),
        "files": {name: sha256(body).hexdigest() for name, body in bodies.items()},
    }
    lb.put_once(store, manifest_key, json.dumps(manifest, sort_keys=True).encode())


def kept_rows(store: lb.Store, key: str) -> pl.DataFrame:
    return pl.read_parquet(io.BytesIO(store.get(f"{key}/rows.parquet")))
