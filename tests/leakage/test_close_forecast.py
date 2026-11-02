"""Point in time for the close forecaster (#369, ADR 0054, hard rule 1): the week's size reads only
earlier weeks' forecasts, mornings and closes public before the week's fold start; B3's parts read
only the morning bundle's rows known at its input cutoff; the frozen weights were fitted on the
training seasons only; and no forecast reads its own game's close."""

import inspect
import json
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta

import polars as pl
import pytest
from forecast_fixtures import PUBLISHED, found, morning

from nhl_edge.backtest.seasons import SEASON_ROLES, SeasonRole
from nhl_edge.game import b3
from nhl_edge.ingest.odds import ET
from nhl_edge.live import close_forecast as cf
from nhl_edge.live import settle as ls
from nhl_edge.market import closing

WEEK = date(2026, 11, 9)
UTC_TYPE = pl.Datetime("us", "UTC")


def kept(day: date, lam: float = 1.0) -> pl.DataFrame:
    """A day's stored forecasts, published as the morning's run plus 9 minutes."""
    shift = timedelta(days=(day - date(2026, 11, 4)).days)
    return cf.forecast(
        morning(day),
        found(),
        cf.weights(),
        cf.Fit(
            cf.week_of(day),
            cf.fold_start(cf.week_of(day)),
            lam,
            None,
            pl.DataFrame(schema=cf.ROW_SCHEMA),
        ),
        PUBLISHED + shift,
        "v",
    )


def closes_after_the_morning(asked: list[datetime]):  # type: ignore[no-untyped-def]
    """ls.closes as a fake: every game's proxy an hour before its start, recording the clock it
    was asked at and refusing odds observed at or after it."""

    def closes(odds: pl.DataFrame, now: datetime) -> pl.DataFrame:
        asked.append(now)
        latest = odds["snapshot_utc"].max()
        assert latest is None or (isinstance(latest, datetime) and latest < now)
        return odds.select(
            "event_id",
            close_status=pl.lit(closing.PROXY),
            close_snapshot_utc="snapshot_utc",
            close_home="home",
            close_away="away",
        )

    return closes


def odds_for(rows: pl.DataFrame, moved: float = 0.02) -> pl.DataFrame:
    """A closing pair for each morning row, an hour before its start."""
    return rows.select(
        "event_id",
        snapshot_utc=pl.col("start_utc") - timedelta(hours=1),
        home=1 / (1 / pl.col("home_price") + moved),
        away=pl.col("away_price"),
    )


def test_the_weeks_size_reads_only_earlier_weeks_public_before_its_fold_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[datetime] = []
    monkeypatch.setattr(ls, "closes", closes_after_the_morning(asked))
    days = [date(2026, 11, 4), date(2026, 11, 8), date(2026, 11, 9), date(2026, 11, 10)]
    forecasts = pl.concat([kept(day) for day in days])
    mornings = pl.concat([morning(day) for day in days])
    odds = odds_for(mornings)
    rows = cf.training_rows(forecasts, mornings, odds, WEEK)
    # Only the earlier week's games, and the fold start is the clock the closes are read at.
    assert set(rows["game_date"].to_list()) == {date(2026, 11, 4), date(2026, 11, 8)}
    assert asked == [cf.fold_start(WEEK)]
    # A forecast, or a morning, published after the fold start is not read.
    late = forecasts.with_columns(
        published_utc=pl.when(pl.col("game_date") == date(2026, 11, 8))
        .then(pl.lit(cf.fold_start(WEEK)).cast(UTC_TYPE))
        .otherwise(pl.col("published_utc"))
    )
    assert set(cf.training_rows(late, mornings, odds, WEEK)["game_date"].to_list()) == {
        date(2026, 11, 4)
    }
    # A close observed after the fold start is dropped before the closes are read.
    after = odds.with_columns(snapshot_utc=pl.lit(cf.fold_start(WEEK)).cast(UTC_TYPE))
    assert cf.training_rows(forecasts, mornings, after, WEEK).is_empty()


def test_a_close_before_the_morning_snapshot_is_no_move(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ls, "closes", closes_after_the_morning([]))
    day = date(2026, 11, 4)
    early = odds_for(morning(day)).with_columns(
        snapshot_utc=pl.col("snapshot_utc") - timedelta(days=1)
    )
    assert cf.training_rows(kept(day), morning(day), early, WEEK).is_empty()


def test_the_parts_read_only_the_bundles_rows_known_at_its_cutoff() -> None:
    from b3_fixtures import day_bundle, league

    tables = league()
    bundle = day_bundle(tables)
    start = bundle.inputs["slate"]["start_utc"].min()
    assert isinstance(start, datetime)
    plain = b3.fit_fold(tables, 20182019, start)
    bundle = replace(bundle, b3=plain)
    before = cf.parts(bundle)
    # A starter named after the cutoff, for every slate game's home team.
    starts = bundle.inputs["goalie_starts"]
    slate = bundle.inputs["slate"].select("game_id", team="home")
    named = (
        starts.join(slate, on=["game_id", "team"])
        .group_by("game_id", "team")
        .first()
        .with_columns(
            goalie_id=pl.lit(8_999_999).cast(starts.schema["goalie_id"]),
            p_start=pl.lit(1.0).cast(starts.schema["p_start"]),
            observed_utc=pl.lit(start + timedelta(hours=1)).cast(starts.schema["observed_utc"]),
        )
        .select(starts.columns)
    )
    later = replace(bundle, inputs={**bundle.inputs, "goalie_starts": pl.concat([starts, named])})
    assert cf.parts(later).equals(before)
    # Named before the cutoff, the same starter does change the goalie part: the check has power.
    earlier = named.with_columns(
        observed_utc=pl.lit(start - timedelta(hours=1)).cast(starts.schema["observed_utc"])
    )
    sooner = replace(
        bundle, inputs={**bundle.inputs, "goalie_starts": pl.concat([starts, earlier])}
    )
    assert not cf.parts(sooner)["goalie_diff"].equals(before["goalie_diff"])


def test_the_weights_were_fitted_on_the_training_seasons_only() -> None:
    w = cf.weights()
    record = json.loads(cf.WEIGHTS.read_text())
    training = {s for s, role in SEASON_ROLES.items() if role == SeasonRole.TRAINING}
    assert set(record["seasons"]) <= training
    assert w.train_cutoff < datetime(2018, 4, 10, tzinfo=UTC)


def test_no_forecast_reads_its_own_games_close() -> None:
    # The forecast takes the morning's rows, B3's parts, the weights and the week's kept fit:
    # no odds, no close and no result.
    assert list(inspect.signature(cf.forecast).parameters) == [
        "morning",
        "found",
        "w",
        "fit",
        "published",
        "code_version",
    ]
    # The week's fold start precedes every morning of the week (the window opens at 07:00 ET).
    for offset in range(7):
        day = WEEK + timedelta(days=offset)
        opens = datetime.combine(day, time(7), ET)
        assert cf.fold_start(cf.week_of(day)) <= opens.astimezone(UTC)
