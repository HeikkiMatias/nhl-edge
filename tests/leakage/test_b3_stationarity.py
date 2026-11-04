"""Point in time for scripts/b3_stationarity.py (#378 step 1, hard rule 1): it reads the lake only
to 2017-18, scores only training seasons, and each season's fit reads only that season's games."""

from typing import Any

import polars as pl
import pytest
from b3_stationarity_fixtures import games, load

from nhl_edge.backtest import yardstick as ys
from nhl_edge.backtest.seasons import SEASON_ROLES, SeasonRole

st = load()


def test_it_scores_only_the_training_seasons_to_2017_18() -> None:
    assert st.LAST == max(ys.CONFIRM) == 20172018
    assert all(SEASON_ROLES[s] is SeasonRole.TRAINING for s in st.SEASONS)


def test_a_later_season_in_the_rows_changes_nothing() -> None:
    base = games({s: (0.45, 0.05) for s in st.SEASONS})
    later = games({20182019: (5.0, 2.0)}, seed=9)
    assert st.run(base, draws=50) == st.run(pl.concat([base, later]), draws=50)


def test_a_seasons_fit_never_reads_another_season() -> None:
    rows = games({s: (0.45, 0.05) for s in st.SEASONS})
    flipped = rows.with_columns(
        home_win=pl.when(pl.col("season") != 20142015)
        .then(1 - pl.col("home_win"))
        .otherwise(pl.col("home_win"))
    )
    one = rows.filter(pl.col("season") == 20142015)
    same = flipped.filter(pl.col("season") == 20142015)
    assert st.fit(one, 20142015) == st.fit(same, 20142015)
    assert (st.bootstrap(one, 20142015, 20) == st.bootstrap(same, 20142015, 20)).all()


def test_the_lake_is_cut_to_the_last_season(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[int] = []

    def through(tables: Any, last: int) -> None:
        asked.append(last)
        raise RuntimeError("stop here")

    class Lake:
        def read(self, name: str) -> pl.DataFrame:
            return pl.DataFrame({"season": [20172018, 20182019]})

    monkeypatch.setattr(st.ys, "through", through)
    with pytest.raises(RuntimeError, match="stop here"):
        st.read_rows(Lake())
    assert asked == [20172018]
