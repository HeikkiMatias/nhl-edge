"""Point in time for the close forecaster (#369, ADR 0054, hard rule 1): the week's size reads only
earlier weeks' forecasts, mornings and closes public before the week's fold start; B3's parts read
only the morning bundle's rows known at its input cutoff; the frozen weights were fitted on the
training seasons only; and no forecast reads its own game's close."""

import inspect
import json
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta

import forecast_fixtures as fx
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
        "spec",
    ]
    # The week's fold start precedes every morning of the week (the window opens at 07:00 ET).
    for offset in range(7):
        day = WEEK + timedelta(days=offset)
        opens = datetime.combine(day, time(7), ET)
        assert cf.fold_start(cf.week_of(day)) <= opens.astimezone(UTC)


# Version 2 (ADR 0056)


def kept_2(day: date) -> pl.DataFrame:
    """A day's stored version 2 forecasts, at both sizes 1."""
    shift = timedelta(days=(day - date(2026, 11, 4)).days)
    week = cf.week_of(day)
    fit = cf.Fit(
        week,
        cf.fold_start(week),
        1.0,
        None,
        pl.DataFrame(schema=cf.ROW_SCHEMA_2),
        1.0,
        cf.VERSION_2,
    )
    found_2 = fx.with_density(found(), {"mismatch": [1.0, -1.0, 0.0]})
    return cf.forecast(morning(day), found_2, cf.weights(cf.V2), fit, PUBLISHED + shift, "v", cf.V2)


def test_density_reads_only_results_public_before_the_morning_cutoff() -> None:
    before = cf.density_inputs(fx.slate_bundle(), fx.played(fx.EARLIER))
    # Last night's NYR game, its result public only after the cutoff: never counted.
    late = fx.played(
        [(2026020091, date(2026, 11, 3), "NYR", "WSH")], public=fx.CUTOFF + timedelta(minutes=1)
    )
    # Tonight's games and a later one, whatever their result's time: never counted.
    tonight = fx.played(
        [
            (g, fx.DAY, h, a)
            for g, h, a in fx.morning().select("game_id", "home", "away").iter_rows()
        ]
    )
    later = fx.played([(2026020120, date(2026, 11, 6), "BOS", "NYR")], public=fx.CUTOFF)
    every = pl.concat([fx.played(fx.EARLIER), late, tonight, later])
    assert cf.density_inputs(fx.slate_bundle(), every).equals(before)
    # The same NYR game public before the cutoff does count: the check has power.
    sooner = fx.played(
        [(2026020091, date(2026, 11, 3), "NYR", "WSH")], public=fx.CUTOFF - timedelta(minutes=1)
    )
    moved = cf.density_inputs(fx.slate_bundle(), pl.concat([fx.played(fx.EARLIER), sooner]))
    assert not moved.equals(before)
    # Another season's games are never read.
    other = fx.played(fx.EARLIER).with_columns(season=pl.lit(20252026, dtype=pl.Int32))
    alone = cf.density_inputs(fx.slate_bundle(), other)
    assert all((alone[c] == 0).all() for c in fx.DENSITY)


def test_version_2s_sizes_read_only_its_own_earlier_weeks_public_before_its_fold_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[datetime] = []
    monkeypatch.setattr(ls, "closes", closes_after_the_morning(asked))
    days = [date(2026, 11, 4), date(2026, 11, 8), date(2026, 11, 9), date(2026, 11, 10)]
    second = pl.concat([kept_2(day) for day in days])
    first = pl.concat([kept(day) for day in days])
    mornings = pl.concat([morning(day) for day in days])
    odds = odds_for(mornings)
    rows = cf.training_rows(pl.concat([first, second]), mornings, odds, WEEK, cf.V2)
    assert set(rows["game_date"].to_list()) == {date(2026, 11, 4), date(2026, 11, 8)}
    assert asked == [cf.fold_start(WEEK)]
    assert rows.columns == list(cf.ROW_SCHEMA_2)
    # Version 1's forecasts are never version 2's rows, nor version 2's version 1's.
    assert cf.training_rows(first, mornings, odds, WEEK, cf.V2).is_empty()
    assert cf.training_rows(second, mornings, odds, WEEK).is_empty()
    # A forecast published after the fold start is not read.
    late = second.with_columns(
        published_utc=pl.when(pl.col("game_date") == date(2026, 11, 8))
        .then(pl.lit(cf.fold_start(WEEK)).cast(UTC_TYPE))
        .otherwise(pl.col("published_utc"))
    )
    found_days = cf.training_rows(late, mornings, odds, WEEK, cf.V2)["game_date"].to_list()
    assert set(found_days) == {date(2026, 11, 4)}


def test_version_2s_weights_were_fitted_on_the_training_seasons_only() -> None:
    w = cf.weights(cf.V2)
    record = json.loads(cf.WEIGHTS_2.read_text())
    training = {s for s, role in SEASON_ROLES.items() if role == SeasonRole.TRAINING}
    assert set(record["seasons"]) <= training
    assert w.train_cutoff < datetime(2018, 4, 10, tzinfo=UTC)
    assert record["density"] == list(fx.DENSITY)


def test_version_2s_inputs_read_no_close_and_no_result() -> None:
    # Beside the morning bundle, version 2 reads the season's games, filtered to the results
    # public before the cutoff (test_density_reads_only_results_public_before_the_morning_cutoff);
    # its forecast reads no odds, close or result.
    assert list(inspect.signature(cf.density_inputs).parameters) == ["bundle", "games"]
    assert list(inspect.signature(cf.inputs).parameters) == ["bundle", "spec", "games"]
