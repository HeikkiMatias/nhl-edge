"""Point in time for scripts/late_season_stakes.py (#422, hard rule 1): a game's flags come only
from standings observed before its time (the yardstick's prediction time, or the projection's
for the lineups), so never from its own date's standings, and later standings change no earlier
game's flags."""

from datetime import date, datetime, timedelta

import goalie_rest_fixtures as gf
import late_season_fixtures as f
import polars as pl

ls = f.load()


def every_day_flags(read_games: pl.DataFrame, flag_from: date) -> pl.DataFrame:
    """SYNTHETIC flags of every team on every date of the games: clinched from flag_from on."""
    teams = sorted(set(read_games["home"]) | set(read_games["away"]))
    first, last = read_games["game_date"].min(), read_games["game_date"].max()
    assert isinstance(first, date) and isinstance(last, date)
    span = (last - first).days  # type: ignore[operator]
    days = [first - timedelta(days=1) + timedelta(days=i) for i in range(span + 2)]
    return f.flagged([(t, d, d >= flag_from, False) for t in teams for d in days])


def test_a_game_reads_only_standings_observed_before_its_prediction() -> None:
    read = gf.league_read()
    games = read.frame.filter(pl.col("prediction_utc").is_not_null())
    middle = games["game_date"].sort()[games.height // 2]
    assert isinstance(middle, date)
    frame, _ = ls.with_columns(read, every_day_flags(read.games, middle))
    known = frame.select("game_id", "game_date", "prediction_utc", "clinched_diff").join(
        read.games.select("game_id", "home", "away"), on="game_id"
    )
    # Every team is flagged from `middle`'s standings, observed the morning after it. A game on
    # `middle` itself, predicted that morning, can't read them: both sides read as unflagged.
    on_the_day = known.filter(pl.col("game_date") == middle)
    assert on_the_day.height and on_the_day["clinched_diff"].abs().max() == 0
    # A flag a game reads was observed before its prediction: with both sides flagged from the
    # day before on, the difference is 0 again, so a nonzero one would be a side read early.
    assert known["clinched_diff"].abs().max() == 0


def test_a_flag_observed_after_the_prediction_is_never_read() -> None:
    read = gf.league_read()
    games = read.frame.filter(pl.col("prediction_utc").is_not_null())
    middle = games["game_date"].sort()[games.height // 2]
    assert isinstance(middle, date)
    home = read.games.filter(pl.col("game_date") == middle)["home"][0]
    rows = every_day_flags(read.games, date(2100, 1, 1)).with_columns(
        clinched=(pl.col("team") == home) & (pl.col("standings_date") >= middle)
    )
    frame, _ = ls.with_columns(read, rows)
    flagged = frame.join(read.games.select("game_id", "home", "away"), on="game_id").filter(
        pl.col("clinched_diff") != 0
    )
    # The home team's first flagged standings are `middle`'s, observed the morning after.
    assert flagged.height and (flagged["game_date"] > middle).all()
    for row in flagged.iter_rows(named=True):
        assert row["prediction_utc"] > f.morning_after(middle)
    # Later standings change no earlier game's flags.
    cut = rows.filter(pl.col("observed_utc") <= f.morning_after(middle))
    early, _ = ls.with_columns(read, cut)
    before = frame.filter(pl.col("prediction_utc") <= f.morning_after(middle))
    assert before.select("game_id", "clinched_diff").equals(
        early.filter(pl.col("prediction_utc") <= f.morning_after(middle)).select(
            "game_id", "clinched_diff"
        )
    )


def test_the_lineup_rows_read_flags_known_at_the_projection() -> None:
    day = date(2014, 4, 1)
    game = 2014020100
    projected = datetime.fromisoformat("2014-04-01T14:00:00+00:00")
    lineups = f.lineups([(game, "BOS", 1, "F", 1.0, None, 15.0)], projected)
    actual = f.actual([(game, "BOS", 1, False)])
    games = f.games([(game, day, "BOS", "TOR", 3, 1, "REG")], 20132014)
    # The game's own date's standings say clinched; the day before's don't.
    flags = f.flagged([("BOS", day - timedelta(days=1), False, False), ("BOS", day, True, True)])
    row = ls.lineup_rows(lineups, actual, games, flags).row(0, named=True)
    assert not row["clinched"] and not row["eliminated"] and row["flagged"] == 0.0
    assert row["standings_date"] == day - timedelta(days=1)
