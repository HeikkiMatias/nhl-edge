"""b3_split's fit (#259, ADR 0044): B3 with Δĝ split into its four parts (b3.Terms(split=True)),
fitted once on exactly the live B3's training games, those before the live fold start.

The artifact keeps the split fit beside B3's own fit on the same games, so the challenger can
refuse a day whose live B3 (its bundle's fit) is not the one the split was fitted beside. It is
committed under reports/live/ as b3-split-<yyyymmdd>-<shortsha>.json and pinned by its sha256 in
the registry (live/challengers.py); it is never refitted during the season.

folds() is ADR 0044's stability check: the split's weights at earlier seasons' fold starts, each
fitted on its earlier seasons only, with no season scored.
"""

from collections.abc import Mapping
from datetime import datetime
from typing import Any

import numpy as np

from nhl_edge.game import b2, b3
from nhl_edge.game.b3 import B3Model
from nhl_edge.live import blend_fit
from nhl_edge.live import bundle as lb

COMPONENT = "b3-split"
SPLIT = b3.Terms(split=True)


def artifact(
    tables: b3.Tables, live: blend_fit.LiveFit, version: str, now: datetime
) -> dict[str, Any]:
    """The split's fit and B3's own on the live season's training games, before its fold
    start, with the live fit they belong to."""
    season = blend_fit.LIVE_SEASON
    plain = b3.fit_fold(tables, season, live.fold_start)
    split = b3.fit_fold(tables, season, live.fold_start, terms=SPLIT)
    return {
        "version": version,
        "season": season,
        "live_fit": live.version,
        "fold_start": live.fold_start.isoformat(),
        "games": split.games,
        "train_cutoff": split.train_cutoff.isoformat(),
        "computed_utc": now.isoformat(),
        "b3": lb.fit_record(plain),
        "split": lb.fit_record(split),
    }


def model(record: Mapping[str, Any]) -> B3Model:
    """A fit record (lb.fit_record) as a B3Model, its inputs included."""
    return B3Model(
        season=record["season"],
        settings=b2.Settings(**record["settings"]),
        intercept=record["intercept"],
        weights=tuple(record["weights"]),
        means=tuple(record["means"]),
        scales=tuple(record["scales"]),
        games=record["games"],
        train_cutoff=datetime.fromisoformat(record["train_cutoff"]),
        inputs=tuple(record.get("inputs", b3.INPUTS)),
    )


def load(record: Mapping[str, Any]) -> tuple[B3Model, B3Model]:
    """The artifact's split fit and B3's own beside it."""
    split = model(record["split"])
    if split.inputs != SPLIT.inputs():
        raise ValueError(f"{record['version']} is not a split fit: its inputs are {split.inputs}")
    return split, model(record["b3"])


def differences(day: B3Model, plain: B3Model) -> list[str]:
    """Where the day's live B3 differs from the one the split was fitted beside: other games,
    cutoff, inputs or settings, or a coefficient beyond lb.TOLERANCE."""
    problems = [
        f"{name}: {getattr(day, name)} against {getattr(plain, name)}"
        for name in ("games", "train_cutoff", "inputs", "settings")
        if getattr(day, name) != getattr(plain, name)
    ]
    for name in ("intercept", "weights", "means", "scales"):
        a, b = np.atleast_1d(getattr(day, name)), np.atleast_1d(getattr(plain, name))
        if a.shape != b.shape or np.abs(a - b).max() > lb.TOLERANCE:
            problems.append(f"{name} differs")
    return problems


def folds(tables: b3.Tables, starts: Mapping[int, datetime]) -> dict[int, dict[str, Any]]:
    """ADR 0044's stability check: at each season's fold start, the split's weight on each part
    and B3's on Δĝ, both fitted on the earlier seasons only. Nothing is scored."""
    result = {}
    for season, start in sorted(starts.items()):
        plain = b3.fit_fold(tables, season, start)
        split = b3.fit_fold(tables, season, start, terms=SPLIT)
        parts = range(len(b3.SPLIT))
        result[season] = {
            "games": split.games,
            "split": {split.inputs[i]: split.weights[i] for i in parts},
            # Per expected goal: each weight over its input's scale, comparable with Δĝ's.
            "per_goal": {split.inputs[i]: split.weights[i] / split.scales[i] for i in parts},
            "delta_g_hat": plain.weights[b3.SKILL],
            "delta_g_hat_per_goal": plain.weights[b3.SKILL] / plain.scales[b3.SKILL],
        }
    return result
