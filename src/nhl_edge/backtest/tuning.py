"""The tuning protocol (ADR 0011): a component's settings are chosen on the training seasons by how
well its feature predicts later games' winners, then frozen.

Each candidate's feature is scored season by season. A logistic model of the home team's
full-game win (overtime and shootout included, hard rule 2) on an intercept and the feature is
fitted on the earlier seasons' games whose results were public before the season's first game,
then predicts the season's games. The candidate with the lowest pooled log loss leads. Every
candidate whose paired difference against it has a weekly block bootstrap interval holding zero
ties with it (hard rule 7), and the steadiest of those wins, by the component's own order.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import polars as pl
from sklearn.linear_model import LogisticRegression

from nhl_edge.backtest.metrics import bootstrap, log_loss
from nhl_edge.backtest.walk_forward import fold_start, outcomes


def scored_games(
    feature: pl.DataFrame, games: pl.DataFrame, seasons: Sequence[int]
) -> pl.DataFrame:
    """Each game of the seasons with the log loss of the win model fitted on earlier seasons.
    feature holds game_id and x for the scored seasons and the earlier ones the fits read."""
    calendar = games.select("season", "start_utc")
    frame = (
        feature.select("game_id", "x")
        .join(games.select("game_id", "season", "game_date"), on="game_id")
        .join(outcomes(games), on="game_id")
    )
    out = []
    for season in sorted(seasons):
        start = fold_start(calendar, season)
        train = frame.filter(pl.col("season") < season, pl.col("result_utc") < start)
        if train["home_win"].n_unique() < 2:
            raise ValueError(f"no earlier games with the feature to fit on before {season}")
        # An unpenalized fit: C is the inverse of the L2 strength.
        model = LogisticRegression(C=np.inf, max_iter=1000)
        model.fit(train.select("x").to_numpy(), train["home_win"].to_numpy())
        test = frame.filter(pl.col("season") == season)
        p = model.predict_proba(test.select("x").to_numpy())[:, 1]
        out.append(
            test.with_columns(p_home=pl.Series(p)).select(
                "game_id",
                "season",
                "game_date",
                log_loss=log_loss(pl.col("p_home"), pl.col("home_win")),
            )
        )
    return pl.concat(out).sort("game_id")


@dataclass(frozen=True)
class Candidate[T]:
    settings: T
    label: str
    games: pl.DataFrame


@dataclass(frozen=True)
class Choice[T]:
    chosen: T
    rows: list[dict[str, Any]]


def choose[T](candidates: Sequence[Candidate[T]], steadier: Callable[[T], Any]) -> Choice[T]:
    """The leader by pooled log loss, its ties, and the steadiest among them: the one with the
    largest steadier(settings)."""
    pooled = {c.label: bootstrap(c.games, "log_loss") for c in candidates}
    leader = min(candidates, key=lambda c: pooled[c.label].mean)
    rows = []
    for candidate in candidates:
        paired = candidate.games.join(
            leader.games.select("game_id", leader_loss="log_loss"), on="game_id"
        ).with_columns(difference=pl.col("log_loss") - pl.col("leader_loss"))
        gap = bootstrap(paired, "difference")
        per_season = {
            int(season): float(rows_["log_loss"].mean())  # type: ignore[arg-type]
            for (season,), rows_ in candidate.games.sort("season").group_by(
                "season", maintain_order=True
            )
        }
        rows.append(
            {
                "candidate": candidate,
                "label": candidate.label,
                "pooled": pooled[candidate.label],
                "per_season": per_season,
                "difference": gap,
                "ties": candidate is leader or gap.low <= 0 <= gap.high,
            }
        )
    tied = [row["candidate"] for row in rows if row["ties"]]
    chosen = max(tied, key=lambda c: steadier(c.settings))
    for row in rows:
        row["leader"] = row["candidate"] is leader
        row["chosen"] = row["candidate"] is chosen
    rows.sort(key=lambda row: row["pooled"].mean)
    return Choice(chosen.settings, rows)


def markdown(choice: Choice[Any], component: str, version: str, seasons: Sequence[int]) -> str:
    """The tuning run's log (ADR 0011): every candidate, best first."""
    shown = sorted(seasons)
    lines = [
        f"# Tuning: {component}, {version}",
        "",
        f"Log loss of the home team's full-game win on {shown[0]} to {shown[-1]}, each season",
        "predicted by a model fitted on the earlier seasons (ADR 0011). Intervals are 95% weekly",
        "block bootstrap. A candidate whose difference from the leader holds 0 ties with it, and",
        "the steadiest of the ties is chosen.",
        "",
        "| Candidate | Log loss | Minus the leader | Ties | "
        + " | ".join(str(s) for s in shown)
        + " |",
        "| --- | --- | --- | --- | " + " | ".join("---:" for _ in shown) + " |",
    ]
    for row in choice.rows:
        pooled, gap = row["pooled"], row["difference"]
        mark = " (chosen)" if row["chosen"] else " (leader)" if row["leader"] else ""
        seasons_ = " | ".join(f"{row['per_season'].get(s, float('nan')):.4f}" for s in shown)
        lines.append(
            f"| {row['label']}{mark} | {pooled.mean:.4f} [{pooled.low:.4f}, {pooled.high:.4f}] "
            f"| {gap.mean:+.4f} [{gap.low:+.4f}, {gap.high:+.4f}] "
            f"| {'yes' if row['ties'] else 'no'} | {seasons_} |"
        )
    return "\n".join(lines) + "\n"
