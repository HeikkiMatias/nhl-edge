"""Point in time for scripts/close_forecast_density_history.py (ADR 0056, hard rule 1): it reads
the lake only to the last training season, density is counted at each game's prediction time,
and each season's fits read earlier seasons only."""

import sys
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import polars as pl
import pytest
import yardstick_fixtures as yf
from close_forecast_fixtures import load_density, with_density

from nhl_edge.backtest import yardstick as ys

h2 = load_density()


def test_the_script_reads_the_lake_only_to_the_training_seasons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[int] = []

    def read(lake: Any, last: int) -> None:
        asked.append(last)
        raise RuntimeError("stop here")

    monkeypatch.setattr(h2.ys, "read", read)
    monkeypatch.setattr(h2, "Lake", lambda: None)
    monkeypatch.setattr(sys, "argv", ["close_forecast_density_history.py"])
    with pytest.raises(RuntimeError, match="stop here"):
        h2.main()
    assert asked == [max(ys.CONFIRM)]


def test_density_is_counted_at_each_games_prediction_time() -> None:
    day = date(2014, 1, 10)

    def game(gid: int, d: date, home: str, away: str, public: datetime) -> dict[str, Any]:
        start = datetime.combine(d, time(23), UTC)
        return {
            "game_id": gid,
            "season": 20132014,
            "game_date": d,
            "start_utc": start,
            "home": home,
            "away": away,
            "observed_utc": public,
        }

    predicted = datetime.combine(day, time(15), UTC)
    played = [
        game(1, day - timedelta(days=1), "BOS", "NYR", datetime.combine(day, time(10), UTC)),
        game(2, day, "BOS", "TOR", datetime.combine(day + timedelta(days=1), time(10), UTC)),
    ]
    later = game(3, day + timedelta(days=1), "TOR", "BOS", predicted - timedelta(hours=1))
    late = game(4, day - timedelta(days=1), "MTL", "TOR", predicted + timedelta(hours=1))

    def read(rows: list[dict[str, Any]]) -> Any:
        games = pl.DataFrame(rows).with_columns(
            pl.col("season").cast(pl.Int32),
            pl.col("start_utc", "observed_utc").cast(pl.Datetime("us", "UTC")),
        )
        frame = pl.DataFrame(
            {
                "game_id": [2],
                "prediction_utc": [predicted],
                "y": [0.0],
                "o": [0.0],
                "h_s": [0.0],
                **{p: [0.0] for p in h2.PARTS},
            }
        ).with_columns(pl.col("prediction_utc").cast(pl.Datetime("us", "UTC")))
        return type("Read", (), {"frame": frame, "games": games})()

    before = h2.with_density(read(played))
    # A later game, and a result public only after the prediction, are never counted.
    assert h2.with_density(read([*played, later, late])).equals(before)
    row = before.row(0, named=True)
    # BOS played yesterday at home, TOR hasn't: BOS is tired, TOR fresh.
    assert (row["dense3_diff"], row["mismatch"], row["away_trip"]) == (1.0, -1.0, 0.0)


def test_each_seasons_fits_never_read_a_later_season() -> None:
    frame = with_density(yf.moves((*yf.SEASONS, 20152016, 20162017)))
    later = pl.col("season") >= 20152016
    noisy = frame.with_columns(y=pl.when(later).then(pl.col("y") * 10).otherwise(pl.col("y")))
    seasons = (20132014, 20142015)
    first = h2.out_of_sample(frame, seasons)
    assert h2.out_of_sample(noisy, seasons).equals(first)
