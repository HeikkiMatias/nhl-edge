"""Point-in-time rules for the SBR archive (ADR 0006). SBR gives no time for its prices, so every
price is observed at its game's start and known_at never shows one before puck drop. E2 alone
reads assumed_available_utc, through assumed_available_at: the opener from 10:00 US Eastern on the
game date (or once the schedule is public, if later), the close only at the start, and neither once
the game has started."""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest

from nhl_edge.ingest.sbr import assumed_available_at, match_season, open_assumed_utc, parse_season
from nhl_edge.lake.schemas import Games, SbrOdds, Schedule, dtypes
from nhl_edge.lake.tables import known_at

PAGE = (
    Path(__file__).parents[1] / "unit" / "fixtures" / "sbr" / "nhl-odds-2021-22.html"
).read_bytes()
TBL_PIT = datetime(2021, 10, 12, 23, tzinfo=UTC)  # 19:00 EDT
# A European start, 14:00 EDT: before the evening, but after 10:00 ET.
SJS_ARI = datetime(2021, 10, 13, 18, tzinfo=UTC)
MORNING = datetime(2021, 10, 12, 14, 0, 1, tzinfo=UTC)  # 10:00:01 EDT on TBL_PIT's date


def table(
    sjs_ari_start: datetime = SJS_ARI, sjs_ari_public: datetime | None = None
) -> pl.DataFrame:
    sched = pl.DataFrame(
        [
            (2021020001, 20212022, date(2021, 10, 12), TBL_PIT, "TBL", "PIT"),
            (2021020010, 20212022, date(2021, 10, 13), sjs_ari_start, "SJS", "ARI"),
        ],
        schema=["game_id", "season", "game_date", "start_utc", "home", "away"],
        orient="row",
    ).with_columns(
        venue=pl.lit("x"),
        neutral_site=pl.lit(False),
        limited_attendance=pl.lit(False),
        observed_utc=pl.col("start_utc") - timedelta(days=1),
        raw_key=pl.lit("k"),
    )
    if sjs_ari_public is not None:
        sched = sched.with_columns(
            observed_utc=pl.when(pl.col("game_id") == 2021020010)
            .then(pl.lit(sjs_ari_public))
            .otherwise(pl.col("observed_utc"))
        )
    sched = sched.cast(dtypes(Schedule)).select(list(dtypes(Schedule)))  # type: ignore[arg-type]
    frame, _ = match_season(
        parse_season(PAGE, 20212022), sched, pl.DataFrame(schema=dtypes(Games)), "k"
    )
    return SbrOdds.validate(frame)


def test_every_price_is_observed_at_its_game_start() -> None:
    frame = table()
    assert (frame["observed_utc"] == frame["start_utc"]).all()


def test_known_at_shows_no_price_before_its_game_starts() -> None:
    frame = table()
    assert known_at(frame, MORNING).is_empty()
    assert known_at(frame, TBL_PIT).is_empty()
    after = known_at(frame, TBL_PIT + timedelta(seconds=1))
    assert set(after["game_id"]) == {2021020001}
    assert set(after["quote"]) == {"open", "close"}


def test_e2_sees_only_the_opener_on_game_day_morning() -> None:
    usable = assumed_available_at(table(), MORNING)
    assert set(usable["quote"]) == {"open"}
    assert set(usable["game_id"]) == {2021020001}
    assert assumed_available_at(table(), MORNING - timedelta(seconds=2)).is_empty()


def test_e2_never_sees_a_close_before_the_start() -> None:
    frame = table()
    closes = assumed_available_at(frame, TBL_PIT).filter(pl.col("quote") == "close")
    assert 2021020001 not in closes["game_id"].to_list()


def test_e2_sees_no_price_once_the_game_has_started() -> None:
    # After puck drop neither the opener nor the close can still be bet.
    # 11:00 EDT the next day: TBL_PIT is over, SJS_ARI's opener is up and its start is ahead.
    usable = assumed_available_at(table(), datetime(2021, 10, 13, 15, tzinfo=UTC))
    assert set(usable["game_id"]) == {2021020010}
    assert set(usable["quote"]) == {"open"}


def test_the_opener_waits_for_a_schedule_made_public_after_ten_eastern() -> None:
    # Like the Lake Tahoe game re-timed on its game day (SCHEDULE_PUBLIC_OVERRIDES): the row
    # carries the new start, so E2 sees it only once the new time is public.
    public = datetime(2021, 10, 13, 16, tzinfo=UTC)  # 12:00 EDT, after 10:00 ET
    frame = table(sjs_ari_public=public)
    opens = frame.filter((pl.col("game_id") == 2021020010) & (pl.col("quote") == "open"))
    assert (opens["assumed_available_utc"] == public).all()
    assert 2021020010 not in assumed_available_at(frame, public)["game_id"].to_list()


def test_the_assumed_opener_is_never_later_than_an_early_start() -> None:
    early = datetime(2021, 10, 13, 9, tzinfo=UTC)  # 05:00 EDT, before 10:00 ET
    frame = table(early)
    opens = frame.filter((pl.col("game_id") == 2021020010) & (pl.col("quote") == "open"))
    assert (opens["assumed_available_utc"] == early).all()


@pytest.mark.parametrize(
    ("game_date", "expected"),
    [
        (date(2021, 10, 12), datetime(2021, 10, 12, 14, tzinfo=UTC)),  # EDT
        (date(2022, 1, 12), datetime(2022, 1, 12, 15, tzinfo=UTC)),  # EST
    ],
)
def test_the_opener_is_assumed_at_ten_eastern(game_date: date, expected: datetime) -> None:
    late = expected + timedelta(hours=12)
    assert open_assumed_utc(game_date, late, expected - timedelta(days=1)) == expected


def test_the_schema_rejects_an_observed_time_before_the_start() -> None:
    frame = table().with_columns(observed_utc=pl.col("assumed_available_utc"))
    with pytest.raises(Exception, match="observed_at_start"):
        SbrOdds.validate(frame)


def test_the_schema_rejects_an_assumed_time_after_the_start() -> None:
    frame = table().with_columns(assumed_available_utc=pl.col("start_utc") + timedelta(minutes=1))
    with pytest.raises(Exception, match="assumed_no_later_than_start"):
        SbrOdds.validate(frame)
