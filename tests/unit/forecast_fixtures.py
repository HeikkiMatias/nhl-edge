"""Hand-built morning rows and B3 parts for the close forecaster's tests (#369, ADR 0054): three
games on Wednesday 2026-11-04, their morning run at 07:08 EST. For version 2 (ADR 0056), a morning
bundle's slate and the season's earlier games."""

from datetime import UTC, date, datetime, timedelta

import polars as pl

from nhl_edge.features.density import DENSITY
from nhl_edge.live import bundle as lb
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


# Version 2 (ADR 0056): the morning bundle's slate and the season's earlier games, for density.
CUTOFF = RUN  # the morning bundle's input cutoff


def slate_bundle(cutoff: datetime = CUTOFF) -> lb.Bundle:
    """A morning bundle with only what density reads: its slate and its input cutoff."""
    rows = morning().select("game_id", "season", "game_date", "start_utc", "home", "away")
    return lb.Bundle({"input_cutoff": cutoff.isoformat()}, {"slate": rows}, None, None)


def played(rows: list[tuple[int, date, str, str]], public: datetime | None = None) -> pl.DataFrame:
    """SYNTHETIC played games of 2026-27: (game_id, date, home, away), each starting at 00:00
    UTC after its date, its result public at 10:00 UTC the morning after (ADR 0003), or at
    public."""
    return pl.DataFrame(
        {
            "game_id": [g for g, _, _, _ in rows],
            "season": [20262027] * len(rows),
            "game_date": [d for _, d, _, _ in rows],
            "start_utc": [
                datetime.combine(d + timedelta(days=1), datetime.min.time(), UTC)
                for _, d, _, _ in rows
            ],
            "home": [h for _, _, h, _ in rows],
            "away": [a for _, _, _, a in rows],
            "observed_utc": [
                public
                or datetime.combine(d + timedelta(days=1), datetime.min.time(), UTC)
                + timedelta(hours=10)
                for _, d, _, _ in rows
            ],
        }
    ).with_columns(
        pl.col("season").cast(pl.Int32),
        pl.col("start_utc", "observed_utc").cast(pl.Datetime("us", "UTC")),
    )


# BOS played at PHI last night; NYR at home two nights ago; TOR on a two-game road trip.
EARLIER = [
    (2026020090, date(2026, 11, 3), "PHI", "BOS"),
    (2026020085, date(2026, 11, 2), "NYR", "CAR"),
    (2026020080, date(2026, 10, 31), "BUF", "TOR"),
    (2026020086, date(2026, 11, 2), "CBJ", "TOR"),
]


def with_density(
    found_rows: pl.DataFrame, values: dict[str, list[float]] | None = None
) -> pl.DataFrame:
    """found() with version 2's five density columns, all 0 unless given."""
    n = found_rows.height
    columns = {name: [0.0] * n for name in DENSITY} | (values or {})
    return found_rows.with_columns(
        **{k: pl.Series(v, dtype=pl.Float64) for k, v in columns.items()}
    )
