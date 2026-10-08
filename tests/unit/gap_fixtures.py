"""A fold's blend fit and ten E2 gaps whose p_blend follows from it, with their market and u's
parts, for the blend gap check's tests (#156)."""

from datetime import UTC, datetime
from typing import Any

import polars as pl

from nhl_edge.game import uncertainty
from nhl_edge.market import blend


def blend_fixture() -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame, dict[Any, Any]]:
    """Ten E2 gaps whose p_blend follows from a fold fit, with their market and u's parts."""
    fit = blend.Blend(
        blend.Kind.MODEL,
        (-0.07, 0.49, 0.71, 0.22),
        (0.1,) * 4,
        3213,
        datetime(2021, 5, 20, tzinfo=UTC),
    )
    scale = uncertainty.Scale(
        (0.3, 1.6, 0.17), (0.09, 0.48, 0.06), 3213, datetime(2021, 5, 20, tzinfo=UTC), 0.67
    )
    ids = list(range(1, 11))
    p_mkt = pl.DataFrame(
        {"experiment": ["E2"] * 10, "game_id": ids, "p_mkt": [0.40 + 0.02 * i for i in range(10)]}
    )
    parts = pl.DataFrame(
        {
            "experiment": ["E2"] * 10,
            "game_id": ids,
            "goalie_doubt": [0.25 + 0.01 * i for i in range(10)],
            "availability_doubt": [1.5] * 10,
            "rookie_share": [0.2] * 10,
        }
    )
    p_b3 = [0.55 + 0.01 * i for i in range(10)]
    p_blend = fit.predict(p_mkt["p_mkt"].to_numpy(), p_b3, scale.score(parts).to_numpy())
    gaps = pl.DataFrame(
        {
            "experiment": ["E2"] * 10,
            "season": [20212022] * 10,
            "game_id": ids,
            "p_b3": p_b3,
            "p_blend": p_blend.round(4),
        }
    )
    return gaps, p_mkt, parts, {("E2", 20212022): (fit, scale)}
