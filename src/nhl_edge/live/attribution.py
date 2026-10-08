"""Attribution of the live bets (#193, docs/plans/phase-5.md task 7): each paper bet's move from
Pinnacle's fair price at the decision, split into the market's part and each B3 input's departure
from its usual level at that price, as history's bets are split (backtest/e3.attribution, #154).

**The usual levels** are each input part's least-squares line on logit p_mkt over the live
blend's training games: E1's rows of 2018-19 to 2022-23, the live fit's own (live/blend_fit.py),
each game's B3 parts from its own fold's B3 fit at its prediction time. They read inputs and
prices only, never a result. They are fixed once, before any live bet is attributed, and stored
beside the live fit (reports/live/attribution-levels-<version>.json) with their own version and
train_cutoff. 2022-23's rows are read as the live fit reads them: predicted, never scored (the
owner's ruling of 2026-10-05).

**A live bet's parts** come from the day's run bundle: B3's fit and the rows the decision read,
at its input cutoff (live/bundle.py). logit p_mkt is the decision's B0 and u its score on the
live scale, both from the ledger, and the weights are the live fit's BLEND. An edge explained by
one odd input is a suspected bug (the daily-slate skill). Nothing in the policy reads them.
"""

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from nhl_edge.backtest import blend as blend_backtest
from nhl_edge.backtest import e3
from nhl_edge.betting.selection import POLICY_VERSION
from nhl_edge.game import b2, b3, uncertainty
from nhl_edge.live import blend_fit
from nhl_edge.live import bundle as lb
from nhl_edge.live import predict as lp

COMPONENT = "attribution-levels"
PREFIX = f"{COMPONENT}-"


def training_history(
    tables: b2.Tables,
    b3_tables: b3.Tables,
    u_tables: uncertainty.Tables,
    priced: pl.DataFrame,
    starts: dict[int, datetime],
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """The live fit's training rows, E1's rows of each of its seasons rebuilt as nhl live
    blend-fit rebuilds them (blend_fit.season_predictions and the blend's rows); and its games
    (season, game_id, prediction_utc, logit_mkt), with logit p_mkt at each game's prediction
    time."""
    frames, parts = [], []
    for season in blend_fit.TRAINING_SEASONS:
        predicted, doubts = blend_fit.season_predictions(
            tables, b3_tables, u_tables, priced, season, starts[season]
        )
        frames.append(predicted)
        parts.append(doubts)
    experiment = blend_fit.EXPERIMENT.value
    rows = blend_backtest.rows(pl.concat(frames), {experiment: pl.concat(parts)}, tables.games)
    return rows, e3.market_history(rows, experiment)


def identity_problems(
    rows: pl.DataFrame, history: pl.DataFrame, live_record: dict[str, Any]
) -> list[str]:
    """Where the rebuilt training rows are not the live fit's own: their count per season, and
    the live fit refitted from them, which must give back its recorded blends and u scale
    (blend_fit.differences). Equal counts alone would let a changed game through."""
    problems = count_problems(history, live_record)
    if problems:
        return problems
    start = datetime.fromisoformat(live_record["fold_start"])
    fold = blend_fit.fit(rows, list(blend_fit.TRAINING_SEASONS), start, blend_fit.LIVE_SEASON)
    return blend_fit.differences(blend_fit.describe(fold), live_record["fits"])


def games_digest(training: pl.DataFrame) -> str:
    """The sha256 of the training games, season:game_id sorted, one per line: which games the
    levels stand on, to check them against later."""
    keys = sorted(f"{s}:{g}" for s, g in training.select("season", "game_id").iter_rows())
    return lb.sha256("\n".join(keys).encode())


def levels(
    b3_tables: b3.Tables, history: pl.DataFrame, starts: dict[int, datetime]
) -> tuple[pl.DataFrame, list[datetime]]:
    """B3's parts for every training game, each season's from its own fold fit, with logit_mkt;
    and the fits' train_cutoffs."""
    frames, cutoffs = [], []
    for season in blend_fit.TRAINING_SEASONS:
        parts, cutoff = season_parts(b3_tables, history, season, starts[season])
        frames.append(parts)
        cutoffs.append(cutoff)
    return pl.concat(frames), cutoffs


def season_parts(
    b3_tables: b3.Tables, history: pl.DataFrame, season: int, start: datetime
) -> tuple[pl.DataFrame, datetime]:
    """B3's parts for the season's training games (history: season, game_id, prediction_utc,
    logit_mkt) from the season's own fold fit, cut off before start, with logit_mkt beside them;
    and the fit's train_cutoff."""
    rows = history.filter(pl.col("season") == season)
    _, model = b3.predictions(b3_tables, rows.select("game_id", "prediction_utc"), season, start)
    parts = e3.b3_parts(rows, b3_tables, model, season).join(
        rows.select("season", "game_id", "prediction_utc", "logit_mkt"), on="game_id"
    )
    return parts, model.train_cutoff


def artifact(
    version: str,
    live_version: str,
    live_start: datetime,
    training: pl.DataFrame,
    cutoffs: list[datetime],
    fitted_utc: datetime,
) -> dict[str, Any]:
    """The usual levels' record: each input part's line on logit p_mkt over the training games
    (e3.usual), refused unless every row and fit behind it was known before the live fold
    starts."""
    if training.is_empty():
        raise ValueError("no training games to set the usual levels on")
    last_row = training["prediction_utc"].max()
    assert isinstance(last_row, datetime)
    train_cutoff = max([*cutoffs, last_row])
    if train_cutoff >= live_start:
        raise ValueError(f"the usual levels read a row known at {train_cutoff}, after {live_start}")
    per_season = training.group_by("season").len().sort("season")
    return {
        "version": version,
        "fitted_utc": fitted_utc.isoformat(),
        "live_fit": live_version,
        "policy": POLICY_VERSION,
        "season": blend_fit.LIVE_SEASON,
        "experiment": blend_fit.EXPERIMENT.value,
        "train_cutoff": train_cutoff.isoformat(),
        "training": {
            "seasons": list(blend_fit.TRAINING_SEASONS),
            "games": training.height,
            "per_season": {str(s): n for s, n in per_season.iter_rows()},
            "games_sha256": games_digest(training),
        },
        "lines": {
            part: {"intercept": alpha, "slope": beta}
            for part, (alpha, beta) in e3.usual(training).items()
        },
        "note": "Fixed once for the live fit's bets (#193): each B3 input part's least-squares "
        "line on logit p_mkt over the live fit's training games, inputs and prices only. "
        "2022-23's rows were predicted, never scored (the owner's ruling, 2026-10-05).",
    }


def count_problems(history: pl.DataFrame, live_record: dict[str, Any]) -> list[str]:
    """Where the rebuilt training games differ, per season, from the live fit's record of its
    own: the usual levels must stand on exactly its games."""
    rebuilt = {str(s): n for s, n in history.group_by("season").len().iter_rows()}
    recorded = live_record["training"]["per_season"]
    return [
        f"{season}: {rebuilt.get(season, 0):,} games rebuilt, {recorded.get(season, 0):,} in "
        f"{live_record['version']}"
        for season in sorted(set(rebuilt) | set(recorded))
        if rebuilt.get(season, 0) != recorded.get(season, 0)
    ]


def path_for(live_version: str, where: Path = blend_fit.REPORTS) -> Path | None:
    """The committed usual levels of the live fit, or None. Refuses more than one: they are
    fixed once."""
    found = []
    for path in sorted(where.glob(f"{PREFIX}*.json")):
        if json.loads(path.read_text()).get("live_fit") == live_version:
            found.append(path)
    if len(found) > 1:
        raise ValueError(f"more than one set of usual levels for {live_version}: {found}")
    return found[0] if found else None


def load(record: dict[str, Any], live_version: str) -> dict[str, tuple[float, float]]:
    """The usual levels of each of e3.INPUTS, refused unless they are the live fit's."""
    if record.get("live_fit") != live_version or record.get("policy") != POLICY_VERSION:
        raise ValueError(
            f"{record.get('version')} holds the usual levels of {record.get('live_fit')}, not "
            f"{live_version}"
        )
    lines = record["lines"]
    return {part: (lines[part]["intercept"], lines[part]["slope"]) for part in e3.INPUTS}


def bet_parts(
    ledger: pl.DataFrame,
    parts: pl.DataFrame,
    lines: dict[str, tuple[float, float]],
    live: blend_fit.LiveFit,
) -> pl.DataFrame:
    """Each of the ledger's bets (game_id, away, home, side, price, ev) with its parts
    (part_<name> for each of e3.PARTS, in log-odds toward its side) and its driver. parts holds
    B3's parts at the decision (e3.b3_parts: game_id, intercept and each input)."""
    bets = ledger.filter(pl.col("status") == lp.PREDICTED, pl.col("bet").fill_null(False))
    p, q = bets["p_b0"].to_numpy(), bets["p_blend"].to_numpy()
    rows = bets.select(
        "game_id",
        "away",
        "home",
        "side",
        "price",
        "ev",
        "u",
        logit_mkt=pl.Series(np.log(p / (1 - p)), dtype=pl.Float64),
        move=pl.Series(np.log(q / (1 - q)) - np.log(p / (1 - p)), dtype=pl.Float64),
    ).join(parts, on="game_id", how="left")
    attributed = e3.with_driver(e3.decompose(rows, lines, live.blends["BLEND"].weights))
    # B3's parts are read at the goalie mixture's expected effect, as on history, while its
    # logged probability mixes each starter's: what the parts leave of the logged move is shown
    # apart (rest), never a driver.
    sign = pl.when(pl.col("side") == "home").then(1.0).otherwise(-1.0)
    total = pl.sum_horizontal(*(pl.col(f"part_{p}") for p in e3.PARTS))
    found = pl.all_horizontal(*(pl.col(f"part_{p}").is_not_null() for p in e3.PARTS))
    return attributed.with_columns(
        rest=pl.when(found).then(sign * pl.col("move") - total).otherwise(None)
    ).drop("move")


def markdown(attributed: pl.DataFrame) -> str:
    """The bets' parts for the daily slate, in log-odds toward each bet's side."""
    if attributed.is_empty():
        return "No bets to attribute.\n"
    names = list(e3.PARTS)
    lines = [
        "| Game | Bet | Driver | " + " | ".join(n.replace("_", " ") for n in names) + " | Rest |",
        "| --- | --- | --- | " + " | ".join("---:" for _ in names) + " | ---: |",
    ]
    for row in attributed.sort("game_id").iter_rows(named=True):
        cells = [row[f"part_{n}"] for n in names] + [row["rest"]]
        values = " | ".join("" if x is None else f"{x:+.3f}" for x in cells)
        lines.append(
            f"| {row['away']} at {row['home']} | {row['side']} at {row['price']:.2f} | "
            f"{row['driver'] or 'no B3 parts'} | {values} |"
        )
    return "\n".join(lines) + "\n"


def slate_section(
    store: lb.Store | None,
    day: date,
    ledger: pl.DataFrame,
    reports: Path = blend_fit.REPORTS,
    fit: Path | None = None,
) -> str:
    """The daily slate's attribution of the day's bets, from its run bundle, the live fit the day
    names (under reports, or fit for a dry run's own) and that fit's usual levels under reports;
    or why there is none. The rest is what the parts leave of the logged move."""
    header = "Attribution of each bet, in log-odds toward its side (#193):"
    if store is None:
        return f"{header} it needs the day's run bundle, so pass --r2 or --from.\n"
    try:
        saved = lb.read(store, day)
        path = fit or reports / saved.manifest["live_fit"]
        body = path.read_bytes()
        if lb.sha256(body) != saved.manifest["live_fit_sha256"]:
            raise ValueError(f"{path} is not the live fit the day was decided with")
        live = blend_fit.load(json.loads(body))
        found = path_for(live.version, reports)
        if found is None:
            return (
                f"{header} no usual levels for {live.version} yet: "
                "nhl live attribution-levels --write fixes them.\n"
            )
        lines = load(json.loads(found.read_text()), live.version)
    except (OSError, ValueError) as exc:
        return f"{header} {exc}.\n"
    parts = lb.b3_parts(saved)
    if parts is None:
        return f"{header} the day's models were never read.\n"
    return f"{header}\n\n" + markdown(bet_parts(ledger, parts, lines, live))
