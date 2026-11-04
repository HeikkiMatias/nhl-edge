"""Point in time for scripts/ot_winner.py (#379 step 1, hard rule 1): each scored season's fits
read only earlier seasons' ties, and the seasons read are open ones only."""

from datetime import date
from typing import Any

import numpy as np
import polars as pl
import pytest
from ot_winner_fixtures import league, load

from nhl_edge.backtest.seasons import SEASON_ROLES, SeasonRole

ow = load()


def test_a_later_season_never_changes_an_earlier_seasons_predictions() -> None:
    tied, inputs, games, rates = league(0.5)
    base, _, fits = ow.predictions(tied, inputs, games, rates)
    flipped = tied.with_columns(
        home_won=pl.when(pl.col("season") >= 20182019)
        .then(1 - pl.col("home_won"))
        .otherwise(pl.col("home_won"))
    )
    after, _, fits_after = ow.predictions(flipped, inputs, games, rates)
    early = pl.col("season") <= 20172018
    cols = ["game_id", "q0", "q1", "q2", "q3"]
    assert (
        after.filter(early)
        .select(cols)
        .sort("game_id")
        .equals(base.filter(early).select(cols).sort("game_id"))
    )
    assert fits["20172018"] == fits_after["20172018"]


def test_only_open_seasons_are_read_and_q3_only_on_training_seasons() -> None:
    open_roles = {SeasonRole.TRAINING, SeasonRole.TRAINING_FLAGGED, SeasonRole.DEVELOPMENT}
    seasons = [s for s in SEASON_ROLES if ow.FIRST <= s <= ow.LAST]
    assert all(SEASON_ROLES[s] in open_roles for s in seasons)
    assert ow.LAST == 20212022 and SEASON_ROLES[ow.LAST + 10001] is SeasonRole.MARKET_VALIDATION
    assert all(SEASON_ROLES[s] is SeasonRole.TRAINING for s in ow.Q3_SCORED)
    assert all(SEASON_ROLES[s] is not SeasonRole.TRAINING_FLAGGED for s in ow.SCORED)


def test_the_draw_rate_for_a_shift_reads_only_earlier_seasons() -> None:
    tied, inputs, games, rates = league(0.5)
    later = dict(rates) | {20212022: 0.9}
    _, _, fits = ow.predictions(tied, inputs, games, rates)
    _, _, fits_later = ow.predictions(tied, inputs, games, later)
    assert fits["20212022"]["draw_rate"] == fits_later["20212022"]["draw_rate"]


@pytest.mark.parametrize("season", [20162017, 20172018, 20182019, 20212022])
def test_a_scored_seasons_own_ties_never_enter_its_fit(season: int) -> None:
    tied, inputs, games, rates = league(0.5)
    base, _, fits = ow.predictions(tied, inputs, games, rates)
    own = tied.filter(pl.col("season") == season)["game_id"].implode()
    flipped = tied.with_columns(
        home_won=pl.when(pl.col("game_id").is_in(own))
        .then(1 - pl.col("home_won"))
        .otherwise(pl.col("home_won"))
    )
    moved = inputs.with_columns(
        o=pl.when(pl.col("game_id").is_in(own)).then(pl.col("o") + 1).otherwise(pl.col("o")),
        delta_g_hat=pl.when(pl.col("game_id").is_in(own))
        .then(pl.col("delta_g_hat") - 1)
        .otherwise(pl.col("delta_g_hat")),
    )
    _, _, fits_after = ow.predictions(flipped, moved, games, rates)
    assert fits_after[str(season)] == fits[str(season)]
    q1 = base.filter(pl.col("season") == season)["q1"].unique().to_list()
    after, _, _ = ow.predictions(flipped, moved, games, rates)
    assert after.filter(pl.col("season") == season)["q1"].unique().to_list() == q1


def test_a_shootouts_extra_goal_never_makes_a_regulation_winner() -> None:
    games = pl.DataFrame(
        {
            "game_id": [1],
            "season": [20162017],
            "game_date": [date(2016, 11, 1)],
            "home_score": [3],
            "away_score": [2],
            "decided_in": ["SO"],
        }
    ).with_columns(pl.col("season").cast(pl.Int32))
    shots = pl.DataFrame(
        {
            "game_id": [1, 1, 1, 1],
            "is_home": [True, True, False, False],
            "is_goal": [True] * 4,
            "period": [1, 3, 2, 3],
        }
    )
    tied, disagree = ow.ties(games, shots)
    assert tied["game_id"].to_list() == [1] and tied["home_won"].to_list() == [1]
    assert disagree == 0


def test_read_never_returns_a_season_after_2021_22(monkeypatch: pytest.MonkeyPatch) -> None:
    held_out = 20222023
    asked: dict[str, Any] = {}

    def market(sbr: pl.DataFrame, games: pl.DataFrame) -> pl.DataFrame:
        asked["market"] = (sbr["season"].max(), games["season"].max())
        return games.select("game_id", o=pl.lit(0.1))

    class Read:
        frame = pl.DataFrame({"game_id": [1], "delta_g_hat": [0.2]})

    def read(lake: Any, last: int) -> Read:
        asked["read"] = last
        return Read()

    monkeypatch.setattr(ow.ys, "market", market)
    monkeypatch.setattr(ow.ys, "read", read)
    seasons = [20162017, held_out]

    class Lake:
        def read(self, name: str) -> pl.DataFrame:
            base = {"season": seasons, "game_id": [1, 2]}
            if name == "games":
                return pl.DataFrame(
                    base
                    | {
                        "game_date": [date(2016, 11, 1), date(2022, 11, 1)],
                        "home_score": [3, 3],
                        "away_score": [2, 2],
                        "decided_in": ["SO", "SO"],
                    }
                ).with_columns(pl.col("season").cast(pl.Int32))
            if name == "shots":
                return pl.DataFrame(
                    base | {"is_goal": [True, True], "period": [1, 1], "is_home": [True, True]}
                ).with_columns(pl.col("season").cast(pl.Int32))
            return pl.DataFrame(base).with_columns(pl.col("season").cast(pl.Int32))

    tied, inputs, games, rates, _ = ow.read(Lake())
    assert asked["read"] == 20172018
    assert max(asked["market"]) <= ow.LAST
    assert games["season"].max() <= ow.LAST and held_out not in rates
    assert 2 not in tied["game_id"].to_list() and 2 not in inputs["game_id"].to_list()


def test_q3_never_decides_even_when_it_carries_the_signal() -> None:
    # Hard rule 1 (Codex on #390): q3's input settings were tuned after its folds' starts, so
    # it is described only. Here Δĝ carries the whole effect and the opener is noise.
    tied, inputs, games, rates = league(1.5)
    noise = inputs.with_columns(
        delta_g_hat=pl.col("o") / 1.4,
        o=pl.Series(np.random.default_rng(9).normal(0.2, 0.4, inputs.height)),
    )
    found = ow.run(tied, noise, games, rates)
    assert set(found["holm"]) == {"q1", "q2"} and set(found["gate_holm"]) == {"q2"}
    assert set(found["gate_by"]) == {"q2"} and not found["gate"]
    # q3's figures are still there, beside the decision.
    assert found["against_q1"]["q3"]["log_loss"]["high"] < 0
    assert "described only" in ow.markdown(found, 0, "ot-winner-20261104-abc1234")
