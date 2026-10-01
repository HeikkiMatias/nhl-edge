from datetime import UTC, date, datetime, time, timedelta

import numpy as np
import polars as pl
import pytest

from nhl_edge.backtest import tuning

SEASONS = (20112012, 20122013, 20132014)


def games(seed: int = 5) -> pl.DataFrame:
    """Three seasons of games whose home win follows a known signal x."""
    rng = np.random.default_rng(seed)
    rows = []
    for season in SEASONS:
        first = date(season // 10000, 10, 5)
        for n in range(600):
            day = first + timedelta(days=n // 4)
            x = float(rng.normal())
            home_win = rng.random() < 1 / (1 + np.exp(-(0.2 + 0.8 * x)))
            rows.append(
                {
                    "game_id": season * 1000 + n,
                    "season": season,
                    "game_date": day,
                    "start_utc": datetime.combine(day, time(23), UTC),
                    "home_score": 3 if home_win else 1,
                    "away_score": 1 if home_win else 3,
                    "observed_utc": datetime.combine(day + timedelta(days=1), time(10), UTC),
                    "x": x,
                }
            )
    return pl.DataFrame(rows).with_columns(pl.col("season").cast(pl.Int32))


GAMES = games()


def test_each_season_is_predicted_by_a_model_of_earlier_seasons() -> None:
    signal = tuning.scored_games(GAMES.select("game_id", "x"), GAMES, SEASONS[1:])
    noise = tuning.scored_games(
        GAMES.select("game_id", x=pl.Series(np.random.default_rng(1).normal(size=GAMES.height))),
        GAMES,
        SEASONS[1:],
    )
    assert signal["season"].unique().sort().to_list() == list(SEASONS[1:])
    assert signal["log_loss"].mean() < noise["log_loss"].mean() - 0.03  # type: ignore[operator]


def test_the_first_season_has_nothing_to_fit_on() -> None:
    with pytest.raises(ValueError, match="no earlier games"):
        tuning.scored_games(GAMES.select("game_id", "x"), GAMES, SEASONS[:1])


def test_a_later_seasons_results_never_reach_an_earlier_fit() -> None:
    scored = tuning.scored_games(GAMES.select("game_id", "x"), GAMES, [20122013])
    flipped = GAMES.with_columns(
        home_score=pl.when(pl.col("season") == 20132014).then(0).otherwise(pl.col("home_score"))
    )
    again = tuning.scored_games(flipped.select("game_id", "x"), flipped, [20122013])
    assert scored.equals(again)


def candidate(
    label: str, offset: float, noise: float, settings: tuple[int, int]
) -> tuning.Candidate:
    base = tuning.scored_games(GAMES.select("game_id", "x"), GAMES, SEASONS[1:])
    # Noise with a mean of exactly zero, so the pooled gap to the base is the offset.
    jitter = np.random.default_rng(len(label)).normal(0, noise, base.height)
    losses = base["log_loss"] + offset + (jitter - jitter.mean())
    return tuning.Candidate(settings, label, base.with_columns(log_loss=losses))


def test_the_steadiest_of_the_leader_and_its_ties_is_chosen() -> None:
    leader = candidate("leader", 0.0, 0.0, (10, 0))
    tie = candidate("tie", 0.0002, 0.03, (40, 10))
    worse = candidate("worse", 0.05, 0.0, (80, 40))
    choice = tuning.choose([leader, tie, worse], steadier=lambda s: s)
    assert choice.chosen == (40, 10)
    rows = {row["label"]: row for row in choice.rows}
    assert rows["leader"]["leader"] and rows["tie"]["ties"] and not rows["worse"]["ties"]
    assert [row["label"] for row in choice.rows] == ["leader", "tie", "worse"]
    text = tuning.markdown(choice, "test", "v1", SEASONS[1:])
    assert "| tie (chosen) |" in text and "| leader (leader) |" in text


def test_without_ties_the_leader_is_chosen() -> None:
    leader = candidate("leader", 0.0, 0.0, (10, 0))
    worse = candidate("worse", 0.05, 0.0, (80, 40))
    assert tuning.choose([leader, worse], steadier=lambda s: s).chosen == (10, 0)
