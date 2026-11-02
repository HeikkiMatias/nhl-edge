"""Hand-built morning rows and B3 parts for the close forecaster's tests (#369, ADR 0054): three
games on Wednesday 2026-11-04, their morning run at 07:08 EST."""

from datetime import UTC, date, datetime, timedelta

import polars as pl

from nhl_edge.live import morning as lm

DAY = date(2026, 11, 4)  # a Wednesday, after the end of daylight time on 11-01
RUN = datetime(2026, 11, 4, 12, 8, tzinfo=UTC)  # 07:08 EST
SNAPSHOT = RUN - timedelta(minutes=2)
PUBLISHED = RUN + timedelta(minutes=9)
START = datetime(2026, 11, 5, 0, 0, tzinfo=UTC)


def morning(day: date = DAY, statuses: tuple[str, ...] = ("predicted",) * 3) -> pl.DataFrame:
    """The day's morning rows, as the morning view stores them, for three games."""
    n = len(statuses)
    shift = timedelta(days=(day - DAY).days)
    return pl.DataFrame(
        {
            "game_date": [day] * n,
            "season": [20262027] * n,
            "game_id": [2026020100 + i for i in range(n)],
            "start_utc": [START + shift] * n,
            "home": ["BOS", "TOR", "MTL"][:n],
            "away": ["NYR", "OTT", "DET"][:n],
            "prediction_utc": [RUN + shift] * n,
            "published_utc": [RUN + shift + timedelta(minutes=3)] * n,
            "status": list(statuses),
            "event_id": [f"e{i}{day:%d}" for i in range(n)],
            "morning_snapshot_utc": [SNAPSHOT + shift] * n,
            "home_price": [1.80, 2.10, 1.95][:n],
            "away_price": [2.10, 1.80, 1.95][:n],
            "p_b0": [0.55, 0.45, 0.5][:n],
            "p_b3": [0.6, 0.4, 0.5][:n],
            "p_blend": [0.56, 0.44, 0.5][:n],
            "u_sd": [0.0] * n,
            "morning_version": [lm.VERSION] * n,
        }
    ).with_columns(
        pl.col("season").cast(pl.Int32),
        pl.col("start_utc", "prediction_utc", "published_utc", "morning_snapshot_utc").cast(
            pl.Datetime("us", "UTC")
        ),
    )


def found(ids: list[int] | None = None) -> pl.DataFrame:
    ids = ids if ids is not None else [2026020100, 2026020101, 2026020102]
    return pl.DataFrame(
        {
            "game_id": ids,
            "five_diff": [0.2, -0.1, 0.0][: len(ids)],
            "special_diff": [0.05, 0.0, 0.0][: len(ids)],
            "finishing_diff": [0.0, 0.1, 0.0][: len(ids)],
            "goalie_diff": [0.1, -0.05, 0.0][: len(ids)],
        }
    )
