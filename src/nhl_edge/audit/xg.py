"""The xG model's calibration report (#73, ADR 0010).

Per season: the shots scored, goals against expected goals per 100 shots with a weekly block
bootstrap interval (hard rule 7), and how well xG ranks goals above saves and misses (AUC). A
held-out season shows only how many shots were scored. The development seasons count as held out
here: phase 2 keeps them unseen until gate 1.

Pooled over the training seasons scored: the same by distance band, strength state, shot type, and
for rebounds and rushes. These are the tables a change to the model would rest on, so they leave
out the development seasons, which phase 2 keeps for gate 1 (TRAINING_SEASONS). The model fits come
last, one per season, with their train_cutoff, and their training shots and goals only when every
training season is shown: otherwise one fit's totals less the previous one's would give a held-out
season's goals.
"""

from collections.abc import Collection, Sequence
from typing import Any

import polars as pl
from sklearn.metrics import roc_auc_score

from nhl_edge.backtest.metrics import bootstrap
from nhl_edge.features.xg import OUTPUT_KEYS, XgModel, model_frame

PER = 100
CALIBRATION_HEADER = "Goals | xG | Goals per 100 | xG per 100 | Goals minus xG per 100"
DISTANCE_BANDS = (10, 20, 30, 45, 60)
DISTANCE_LABELS = ("0-10 ft", "10-20 ft", "20-30 ft", "30-45 ft", "45-60 ft", "60+ ft")
GROUPS = {
    "Distance": "distance_band",
    "Strength state": "state",
    "Shot type": "shot_kind",
    "Rebound": "rebound",
    "Rush": "rush",
}


def scored_shots(shot_xg: pl.DataFrame, shots: pl.DataFrame) -> pl.DataFrame:
    """Each scored shot with its outcome, its inputs and residual: goals minus xG per 100 shots."""
    inputs = model_frame(shots).select(
        *OUTPUT_KEYS, "is_goal", "distance", "state", "shot_kind", "rebound", "rush"
    )
    return shot_xg.join(inputs, on=list(OUTPUT_KEYS), how="inner").with_columns(
        residual=(pl.col("is_goal").cast(pl.Float64) - pl.col("xg")) * PER,
        distance_band=pl.col("distance").cut(list(DISTANCE_BANDS), labels=list(DISTANCE_LABELS)),
    )


def _figures(rows: pl.DataFrame) -> dict[str, Any]:
    estimate = bootstrap(rows, "residual")
    return {
        "goals": int(rows["is_goal"].sum()),
        "xg": float(rows["xg"].sum()),
        "goals_per_100": float(rows["is_goal"].mean()) * PER,  # type: ignore[arg-type]
        "xg_per_100": float(rows["xg"].mean()) * PER,  # type: ignore[arg-type]
        "difference": estimate.mean,
        "low": estimate.low,
        "high": estimate.high,
    }


def season_report(scored: pl.DataFrame, shown: Collection[int]) -> list[dict[str, Any]]:
    """One row per season: shots scored, and for a shown season its calibration and AUC."""
    rows = []
    for (season,), frame in scored.sort("season").group_by("season", maintain_order=True):
        row: dict[str, Any] = {"season": season, "shots": frame.height}
        if season in shown:
            auc = roc_auc_score(frame["is_goal"].to_numpy(), frame["xg"].to_numpy())
            row |= _figures(frame) | {"auc": float(auc)}
        rows.append(row)
    return rows


def group_report(scored: pl.DataFrame, column: str) -> list[dict[str, Any]]:
    """One row per value of column, over the shots given: shots and calibration."""
    frame = scored.with_columns(pl.col(column).cast(pl.String))
    return [
        {"group": group, "shots": rows.height, **_figures(rows)}
        for (group,), rows in frame.sort(column).group_by(column, maintain_order=True)
    ]


def fits(models: Sequence[XgModel], shown: Collection[int]) -> list[dict[str, Any]]:
    """Each fit, with its training shots and goals only when every training season is shown."""
    rows = []
    for model in models:
        whole = set(model.seasons) <= set(shown)
        rows.append(
            {
                "season": model.season,
                "trained_on": f"{model.seasons[0]} to {model.seasons[-1]}",
                "shots": model.shots if whole else None,
                "goals": model.goals if whole else None,
                "train_cutoff": model.train_cutoff.isoformat(),
            }
        )
    return rows


def _calibration_cells(row: dict[str, Any]) -> str:
    return (
        f"{row['goals']:,} | {row['xg']:,.1f} | {row['goals_per_100']:.2f} | "
        f"{row['xg_per_100']:.2f} | {row['difference']:+.2f} "
        f"[{row['low']:+.2f}, {row['high']:+.2f}]"
    )


def markdown_report(
    scored: pl.DataFrame,
    models: Sequence[XgModel],
    shown: Collection[int],
    design_seasons: Collection[int],
    version: str,
) -> str:
    """The report: figures per season for the shown seasons, the group tables over
    design_seasons, and the fits."""
    design_rows = scored.filter(pl.col("season").is_in(list(design_seasons)))
    pooled = sorted(design_rows["season"].unique().to_list())
    lines = [
        f"# xG calibration: {version}",
        "",
        "Goals against expected goals per 100 shots, with 95% weekly block bootstrap intervals.",
        "A difference whose interval holds 0 is calibrated. Held-out seasons, the development",
        "seasons among them until gate 1, show only how many shots were scored. The group tables",
        "pool the training seasons only. The rebound term per season was chosen on 2012-13 to",
        "2017-18, so their figures are in-sample for that choice (ADR 0010).",
        "",
        "## Per season",
        "",
        f"| Season | Shots | {CALIBRATION_HEADER} | AUC |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in season_report(scored, shown):
        if "auc" in row:
            cells = f"{_calibration_cells(row)} | {row['auc']:.3f}"
        else:
            cells = "held out | | | | | "
        lines.append(f"| {row['season']} | {row['shots']:,} | {cells} |")
    if not design_rows.is_empty():
        seasons = f"{pooled[0]} to {pooled[-1]}" if len(pooled) > 1 else str(pooled[0])
        for title, column in GROUPS.items():
            lines += [
                "",
                f"## {title}, training seasons {seasons}",
                "",
                f"| Group | Shots | {CALIBRATION_HEADER} |",
                "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
            ]
            for row in group_report(design_rows, column):
                lines.append(f"| {row['group']} | {row['shots']:,} | {_calibration_cells(row)} |")
    lines += [
        "",
        "## Fits",
        "",
        "| Season scored | Trained on | Shots | Goals | train_cutoff |",
        "| --- | --- | ---: | ---: | --- |",
    ]
    for row in fits(models, shown):
        totals = (
            f"{row['shots']:,} | {row['goals']:,}" if row["shots"] is not None else "held out | "
        )
        lines.append(
            f"| {row['season']} | {row['trained_on']} | {totals} | {row['train_cutoff']} |"
        )
    return "\n".join(lines) + "\n"
