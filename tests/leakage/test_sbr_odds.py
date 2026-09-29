"""Point-in-time rules for the SBR archive: every price is observed no later than its game's start,
the close exactly at the start, and the opener at 10:00 US Eastern on the game date (or the start
when that is earlier). A prediction therefore sees a game's opener only from game-day morning and
its close never before puck drop."""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest

from nhl_edge.ingest.sbr import match_season, open_observed_utc, parse_season
from nhl_edge.lake.schemas import Games, SbrOdds, Schedule, dtypes
from nhl_edge.lake.tables import known_at

PAGE = (
    Path(__file__).parents[1] / "unit" / "fixtures" / "sbr" / "nhl-odds-2021-22.html"
).read_bytes()
TBL_PIT = datetime(2021, 10, 12, 23, tzinfo=UTC)  # 19:00 EDT
# A European start, 14:00 EDT: before the evening, but after 10:00 ET.
SJS_ARI = datetime(2021, 10, 13, 18, tzinfo=UTC)


def table(sjs_ari_start: datetime = SJS_ARI) -> pl.DataFrame:
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
    sched = sched.cast(dtypes(Schedule)).select(list(dtypes(Schedule)))  # type: ignore[arg-type]
    frame, _ = match_season(
        parse_season(PAGE, 20212022), sched, pl.DataFrame(schema=dtypes(Games)), "k"
    )
    return SbrOdds.validate(frame)


def test_no_price_is_observed_after_its_game_starts() -> None:
    frame = table()
    assert (frame["observed_utc"] <= frame["start_utc"]).all()


def test_the_close_is_not_known_before_the_start() -> None:
    frame = table()
    closes = known_at(frame, TBL_PIT).filter(pl.col("quote") == "close")
    assert 2021020001 not in closes["game_id"].to_list()
    after = known_at(frame, TBL_PIT + timedelta(seconds=1)).filter(pl.col("quote") == "close")
    assert 2021020001 in after["game_id"].to_list()


def test_on_game_day_morning_only_the_opener_is_known() -> None:
    morning = datetime(2021, 10, 12, 14, 0, 1, tzinfo=UTC)  # 10:00:01 EDT
    usable = known_at(table(), morning)
    assert set(usable["quote"]) == {"open"}
    assert set(usable["game_id"]) == {2021020001}
    assert known_at(table(), morning - timedelta(seconds=2)).is_empty()


def test_the_opener_is_never_later_than_an_early_start() -> None:
    early = datetime(2021, 10, 13, 9, tzinfo=UTC)  # 05:00 EDT, before 10:00 ET
    frame = table(early)
    opens = frame.filter((pl.col("game_id") == 2021020010) & (pl.col("quote") == "open"))
    assert (opens["observed_utc"] == early).all()


@pytest.mark.parametrize(
    ("game_date", "expected"),
    [
        (date(2021, 10, 12), datetime(2021, 10, 12, 14, tzinfo=UTC)),  # EDT
        (date(2022, 1, 12), datetime(2022, 1, 12, 15, tzinfo=UTC)),  # EST
    ],
)
def test_the_opener_is_public_at_ten_eastern(game_date: date, expected: datetime) -> None:
    late = expected + timedelta(hours=12)
    assert open_observed_utc(game_date, late) == expected


def test_the_schema_rejects_a_price_observed_after_the_start() -> None:
    frame = table().with_columns(observed_utc=pl.col("start_utc") + timedelta(minutes=1))
    with pytest.raises(Exception, match="observed_no_later_than_start"):
        SbrOdds.validate(frame)
