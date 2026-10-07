"""Point-in-time rules for the live blend (ADR 0030, #163, hard rule 6): the season's one fit reads
only rows public before the live fold starts, its B1 only closes whose results were, and 2022-23's
rows come from fits cut off before 2022-23's own fold start."""

from datetime import UTC, date, datetime, timedelta

import polars as pl
import pytest
from blend_fixtures import rows

from nhl_edge.live import blend_fit as bf

SEASONS = [20182019, 20192020, 20202021, 20212022, 20222023]
ROWS = rows(SEASONS, games=300)
LIVE_START = datetime(2026, 9, 29, 23, tzinfo=UTC)


def test_every_row_the_live_fit_reads_was_known_before_the_live_fold() -> None:
    fold = bf.fit(ROWS, SEASONS, LIVE_START, bf.LIVE_SEASON)
    known = fold.train.select(
        pl.max_horizontal(
            "result_utc",
            "prediction_utc",
            "p_b2_cutoff",
            "p_b3_cutoff",
            "parts_utc",
            "parts_cutoff",
        ).max()
    ).item()
    assert known < LIVE_START
    assert fold.train.height == ROWS.height
    assert max(b.train_cutoff for b in fold.blends.values()) < LIVE_START
    assert fold.scale.train_cutoff < LIVE_START


def test_a_row_known_after_the_live_fold_start_is_refused() -> None:
    late = ROWS.with_columns(
        p_b3_cutoff=pl.when(pl.col("game_id") == ROWS["game_id"][0])
        .then(pl.lit(LIVE_START))
        .otherwise(pl.col("p_b3_cutoff"))
    )
    with pytest.raises(ValueError, match="after its fold starts"):
        bf.fit(late, SEASONS, LIVE_START, bf.LIVE_SEASON)


def test_the_reproduction_reads_only_the_folds_before_2022_23() -> None:
    first = ROWS.filter(pl.col("season") == 20222023)["prediction_utc"].min()
    assert isinstance(first, datetime)
    fold = bf.fit(ROWS, SEASONS[:4], first, 20222023)
    assert set(fold.train["season"].to_list()) == set(SEASONS[:4])
    # 2022-23's own rows are after its fold start: asked to learn from them, the fit refuses.
    with pytest.raises(ValueError):
        bf.fit(ROWS.filter(pl.col("season") == 20222023), [20222023], first, 20222023)


def market_rows(days: list[date], season: int) -> pl.DataFrame:
    """Two closes a day, a home win and a home loss at each price, so B1 has something to fit."""
    stamps = [datetime(d.year, d.month, d.day, 23, tzinfo=UTC) for d in days for _ in range(4)]
    return pl.DataFrame(
        {
            "season": [season] * len(stamps),
            "p_home": [0.55, 0.55, 0.4, 0.4] * len(days),
            "home_win": [1, 0, 1, 0] * len(days),
            "result_utc": [s + timedelta(hours=11) for s in stamps],
        },
        schema_overrides={"season": pl.Int32, "home_win": pl.Int8},
    )


def test_b1_reads_only_earlier_seasons_closes_public_before_the_fold() -> None:
    earlier = market_rows([date(2022, 10, 10), date(2022, 10, 11)], 20222023)
    # A live-season close, and one whose result came after the fold start, are never read.
    live = market_rows([date(2026, 10, 1)], 20262027)
    late = market_rows([date(2026, 9, 29)], 20252026)
    b1 = bf.b1_fit(pl.concat([earlier, live, late]), LIVE_START)
    assert b1.games == earlier.height
    assert b1.train_cutoff == earlier["result_utc"].max()
    assert b1.train_cutoff < LIVE_START
