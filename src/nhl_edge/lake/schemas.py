"""One pandera schema per table. Every write validates against its schema."""

from datetime import date, datetime, timedelta
from typing import Annotated

import pandera.polars as pa
import polars as pl

UtcDatetime = Annotated[pl.Datetime, "us", "UTC"]


def dtypes(model: type[pa.DataFrameModel]) -> dict[str, pl.DataType]:
    """Column name to Polars dtype, in schema order, for building frames that match a model."""
    return {name: column.dtype.type for name, column in model.to_schema().columns.items()}


ODDS_SIDES = {
    "h2h": ("home", "away"),
    "h2h_3_way": ("home", "draw", "away"),
    "spreads": ("home", "away"),
    "totals": ("over", "under"),
}
ODDS_MARKETS = tuple(ODDS_SIDES)
ODDS_KEY = ("snapshot_utc", "event_id", "book", "market", "side")
# The book's last_update comes from the Odds API clock, snapshot_utc from ours.
CLOCK_SKEW = timedelta(seconds=60)


class OddsSnapshots(pa.DataFrameModel):
    """One quote: snapshot, Odds API event, book, market and side.

    snapshot_utc is when the quote was observed, so it is the time point-in-time filters use.
    last_update_utc is when the book last changed the market, to spot stale quotes. h2h is the
    two-way moneyline, settled on the full game including OT and the shootout. h2h_3_way is the
    regulation line (home, draw, away over 60 minutes) and must never be compared with h2h.
    """

    snapshot_utc: UtcDatetime
    last_update_utc: UtcDatetime
    event_id: pl.String = pa.Field(str_length={"min_value": 1})
    commence_time_utc: UtcDatetime
    home: pl.String = pa.Field(str_matches=r"^[A-Z]{3}$")
    away: pl.String = pa.Field(str_matches=r"^[A-Z]{3}$")
    book: pl.String = pa.Field(str_length={"min_value": 1})
    market: pl.String = pa.Field(isin=ODDS_MARKETS)
    side: pl.String = pa.Field(isin=sorted({s for sides in ODDS_SIDES.values() for s in sides}))
    line: pl.Float64 = pa.Field(nullable=True)
    price_decimal: pl.Float64 = pa.Field(gt=1)
    is_closing_proxy: pl.Boolean
    slot: pl.String
    raw_key: pl.String

    class Config(pa.DataFrameModel.Config):
        strict = True
        ordered = True
        unique: str | list[str] | None = list(ODDS_KEY)  # noqa: RUF012 (pandera config)

    @pa.dataframe_check
    def line_null_only_for_h2h_markets(cls, data: pa.PolarsData) -> pl.LazyFrame:
        no_line = pl.col("market").is_in(["h2h", "h2h_3_way"])
        return data.lazyframe.select(no_line == pl.col("line").is_null())

    @pa.dataframe_check
    def side_fits_market(cls, data: pa.PolarsData) -> pl.LazyFrame:
        fits = pl.lit(False)
        for market, sides in ODDS_SIDES.items():
            fits = fits | ((pl.col("market") == market) & pl.col("side").is_in(sides))
        return data.lazyframe.select(fits)

    @pa.dataframe_check
    def last_update_not_after_snapshot(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(
            pl.col("last_update_utc") <= pl.col("snapshot_utc") + CLOCK_SKEW
        )

    @pa.dataframe_check
    def home_is_not_away(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(pl.col("home") != pl.col("away"))


class LakeOddsSnapshots(OddsSnapshots):
    """The lake's history of every stored odds snapshot (Supabase keeps only a 36-hour window),
    replayed from the raw responses by `nhl odds replay`.

    Each quote keeps its OddsSnapshots columns and checks: snapshot_utc is when it was observed,
    and h2h (two-way, full game) and h2h_3_way (regulation) stay apart. snapshot_date is the UTC
    date of snapshot_utc, which partitions the table as the raw responses are laid out. Only
    pre-game quotes are kept: a game under way has live prices. game_id and game_type (1 preseason,
    2 regular season, 3 playoffs) come from the NHL schedule listing that matches the event's teams
    and start (other NHL types, such as 4 for the All-Star game, are kept as they are); they are
    null for an event no listing matches. They are keys, not observed facts:
    anything joined through game_id still goes through its own point-in-time selector, and a
    closing proxy keys on the event and its commence time, since quotes priced before a
    postponement carry the rescheduled game's id.
    """

    snapshot_date: pl.Date
    game_id: pl.Int64 = pa.Field(nullable=True)
    game_type: pl.Int8 = pa.Field(ge=1, nullable=True)

    @pa.dataframe_check
    def snapshot_date_is_the_utc_date(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(pl.col("snapshot_date") == pl.col("snapshot_utc").dt.date())

    @pa.dataframe_check
    def pre_game_quotes_only(cls, data: pa.PolarsData) -> pl.LazyFrame:
        # A game under way has live prices that move with the score.
        return data.lazyframe.select(pl.col("commence_time_utc") > pl.col("snapshot_utc"))

    @pa.dataframe_check
    def matched_events_have_a_game_type(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(pl.col("game_id").is_null() == pl.col("game_type").is_null())


SBR_MARKETS = ("h2h", "spreads", "totals")
SBR_QUOTES = ("open", "close")
SBR_KEY = ("game_id", "market", "side", "quote")


class SbrOdds(pa.DataFrameModel):
    """One SBR archive price (#7): the opening or closing line of one side of one market, for an
    NHL regular-season game of 2010-11 to 2022-23.

    h2h is the moneyline, settled on the full game including OT and the shootout, like the Odds
    API's h2h. spreads is the closing puck line, from 2014-15, and line is the side's handicap.
    totals has the line for over and under. price_american is as SBR prints it and price_decimal
    its conversion. home and away come from the NHL schedule, not from SBR's rows.

    SBR gives no time for its prices (ADR 0006). observed_utc is start_utc for every price, the
    only time each was surely public, so known_at shows no SBR price before its game starts.
    assumed_available_utc is E2's assumption of when the price could be bet: the close at
    start_utc, the open at 10:00 US Eastern on game_date or the start when that is earlier. Only
    E2 reads it, through sbr.assumed_available_at, never as observed_utc.
    """

    game_id: pl.Int64
    season: pl.Int32
    game_date: pl.Date
    start_utc: UtcDatetime
    home: pl.String = pa.Field(str_matches=r"^[A-Z]{3}$")
    away: pl.String = pa.Field(str_matches=r"^[A-Z]{3}$")
    market: pl.String = pa.Field(isin=SBR_MARKETS)
    side: pl.String = pa.Field(isin=["home", "away", "over", "under"])
    line: pl.Float64 = pa.Field(nullable=True)
    quote: pl.String = pa.Field(isin=SBR_QUOTES)
    price_american: pl.Int32
    price_decimal: pl.Float64 = pa.Field(gt=1)
    observed_utc: UtcDatetime
    assumed_available_utc: UtcDatetime
    raw_key: pl.String

    class Config(pa.DataFrameModel.Config):
        strict = True
        ordered = True
        unique: str | list[str] | None = list(SBR_KEY)  # noqa: RUF012 (pandera config)

    @pa.dataframe_check
    def regular_season_id_of_its_season(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(regular_season_id_of_its_season())

    @pa.dataframe_check
    def home_is_not_away(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(pl.col("home") != pl.col("away"))

    @pa.dataframe_check
    def line_null_only_for_h2h(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select((pl.col("market") == "h2h") == pl.col("line").is_null())

    @pa.dataframe_check
    def side_fits_market(cls, data: pa.PolarsData) -> pl.LazyFrame:
        team = pl.col("side").is_in(["home", "away"])
        return data.lazyframe.select((pl.col("market") == "totals") != team)

    @pa.dataframe_check
    def american_price_is_valid(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(pl.col("price_american").abs() >= 100)

    @pa.dataframe_check
    def observed_at_start(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(pl.col("observed_utc") == pl.col("start_utc"))

    @pa.dataframe_check
    def assumed_no_later_than_start(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(pl.col("assumed_available_utc") <= pl.col("start_utc"))

    @pa.dataframe_check
    def close_assumed_at_start(cls, data: pa.PolarsData) -> pl.LazyFrame:
        close = pl.col("quote") == "close"
        assumed = pl.col("assumed_available_utc")
        return data.lazyframe.select(~close | (assumed == pl.col("start_utc")))


SBR_SUSPECT_FLAGS = ("big_move", "extreme_open", "swapped", "below_100")


class SbrSuspectOpeners(pa.DataFrameModel):
    """One SBR game whose opening moneyline is likely wrong (#56), with the evidence: each team's
    American price at the open and at the close, the multiplicative de-vigged home probability at
    each, the move between them, the gap between the close and the opener with its sides swapped,
    and the home team's closing puck line (null before 2014-15). Each flag is one criterion of
    ingest/sbr_suspect.py, and a row meets at least one. bad_close is not a criterion: it marks a
    game whose close, not its opener, is the likely error, since the opener and the closing puck
    line agree against the close (#64). The close columns are null when SBR has no close; p_open
    is null exactly when the opener sums below 100%.

    The flags read the close, public only at the start (ADR 0006), so observed_utc is start_utc:
    the list is hindsight, for E2's report, never a model input. raw_key is the SBR page.
    """

    game_id: pl.Int64
    season: pl.Int32
    game_date: pl.Date
    start_utc: UtcDatetime
    home: pl.String = pa.Field(str_matches=r"^[A-Z]{3}$")
    away: pl.String = pa.Field(str_matches=r"^[A-Z]{3}$")
    open_home: pl.Int32
    open_away: pl.Int32
    close_home: pl.Int32 = pa.Field(nullable=True)
    close_away: pl.Int32 = pa.Field(nullable=True)
    p_open: pl.Float64 = pa.Field(gt=0, lt=1, nullable=True)
    p_close: pl.Float64 = pa.Field(gt=0, lt=1, nullable=True)
    move: pl.Float64 = pa.Field(ge=0, le=1, nullable=True)
    unswapped_gap: pl.Float64 = pa.Field(ge=0, le=1, nullable=True)
    close_home_line: pl.Float64 = pa.Field(nullable=True)
    big_move: pl.Boolean
    extreme_open: pl.Boolean
    swapped: pl.Boolean
    below_100: pl.Boolean
    bad_close: pl.Boolean
    observed_utc: UtcDatetime
    raw_key: pl.String

    class Config(pa.DataFrameModel.Config):
        strict = True
        ordered = True
        unique: str | list[str] | None = "game_id"

    @pa.dataframe_check
    def regular_season_id_of_its_season(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(regular_season_id_of_its_season())

    @pa.dataframe_check
    def home_is_not_away(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(pl.col("home") != pl.col("away"))

    @pa.dataframe_check
    def american_prices_are_valid(cls, data: pa.PolarsData) -> pl.LazyFrame:
        prices = pl.col("open_home", "open_away", "close_home", "close_away")
        return data.lazyframe.select((prices.abs() >= 100).fill_null(True))

    @pa.dataframe_check
    def meets_a_criterion(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(pl.any_horizontal(*SBR_SUSPECT_FLAGS))

    @pa.dataframe_check
    def below_100_has_no_opening_probability(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(pl.col("below_100") == pl.col("p_open").is_null())

    @pa.dataframe_check
    def bad_close_has_its_evidence(cls, data: pa.PolarsData) -> pl.LazyFrame:
        evidence = pl.col("p_open", "p_close", "close_home_line").is_not_null()
        return data.lazyframe.select(~pl.col("bad_close") | pl.all_horizontal(evidence))

    @pa.dataframe_check
    def observed_at_start(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(pl.col("observed_utc") == pl.col("start_utc"))


DECIDED_IN = ("REG", "OT", "SO")
# No result counts as public sooner after its scheduled start (ADR 0003).
MIN_RESULT_LAG = timedelta(hours=6)
TRI_CODE = r"^[A-Z]{3}$"
# A game's schedule counts as public this long before its start (ADR 0005).
SCHEDULE_LEAD = timedelta(hours=24)


def regular_season_id_of_its_season() -> pl.Expr:
    """2023020001: the season's start year, game type 02 (regular season), then the game number."""
    game_id = pl.col("game_id")
    return (
        (game_id // 1_000_000 == pl.col("season") // 10_000)
        & ((game_id // 10_000) % 100 == 2)
        & (pl.col("season") % 10_000 == pl.col("season") // 10_000 + 1)
    )


class Games(pa.DataFrameModel):
    """One final regular-season game (NHL gameState OFF).

    Scores are full-game: a shootout adds one goal for its winner, so home_score > away_score
    settles the moneyline, OT and shootout included. decided_in is the period type that ended the
    game. limited_attendance marks the 2020-21 season, played without fans or with capped crowds.

    observed_utc is when the result (home_score, away_score, decided_in) counts as public:
    10:00 UTC the morning after game_date, a conservative bound because the API has no end time
    (ADR 0003). The schema also requires it to be at least six hours after start_utc. The teams,
    start and venue ride along so a result reads on its own, but they share the result's
    observed_utc. A feature that needs the pre-game facts of the game it predicts reads Schedule,
    which is public a day before the start and has no result columns.
    """

    game_id: pl.Int64
    season: pl.Int32
    game_date: pl.Date
    start_utc: UtcDatetime
    home: pl.String = pa.Field(str_matches=TRI_CODE)
    away: pl.String = pa.Field(str_matches=TRI_CODE)
    venue: pl.String
    home_score: pl.Int16 = pa.Field(ge=0)
    away_score: pl.Int16 = pa.Field(ge=0)
    decided_in: pl.String = pa.Field(isin=DECIDED_IN)
    neutral_site: pl.Boolean
    limited_attendance: pl.Boolean
    observed_utc: UtcDatetime
    raw_key: pl.String

    class Config(pa.DataFrameModel.Config):
        strict = True
        ordered = True
        unique: str | list[str] | None = "game_id"

    @pa.dataframe_check
    def regular_season_id_of_its_season(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(regular_season_id_of_its_season())

    @pa.dataframe_check
    def home_is_not_away(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(pl.col("home") != pl.col("away"))

    @pa.dataframe_check
    def no_ties(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(pl.col("home_score") != pl.col("away_score"))

    @pa.dataframe_check
    def extra_time_wins_by_one(cls, data: pa.PolarsData) -> pl.LazyFrame:
        margin = (pl.col("home_score").cast(pl.Int32) - pl.col("away_score")).abs()
        return data.lazyframe.select((pl.col("decided_in") == "REG") | (margin == 1))

    @pa.dataframe_check
    def observed_six_hours_after_start(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(pl.col("observed_utc") >= pl.col("start_utc") + MIN_RESULT_LAG)


class Schedule(pa.DataFrameModel):
    """The pre-game facts of one final regular-season game: when and where it was played, and by
    whom. It has no result columns, so reading it can never reveal a score.

    observed_utc is when the schedule counts as public: 24 hours before start_utc (ADR 0005). The
    NHL publishes each season's schedule in the summer and gives postponed games new dates days or
    weeks ahead, so a day is conservative; a game re-timed at shorter notice gets a later time
    (games.SCHEDULE_PUBLIC_OVERRIDES). Rest, travel and home-ice features read this table through
    games.schedule_known_at: games whose result is public, plus the games being predicted. Not
    other games, because the table holds only games that were played, and a missing or delayed
    game would reveal how it turned out. Results come from Games, public the morning after.
    """

    game_id: pl.Int64
    season: pl.Int32
    game_date: pl.Date
    start_utc: UtcDatetime
    home: pl.String = pa.Field(str_matches=TRI_CODE)
    away: pl.String = pa.Field(str_matches=TRI_CODE)
    venue: pl.String
    neutral_site: pl.Boolean
    limited_attendance: pl.Boolean
    observed_utc: UtcDatetime
    raw_key: pl.String

    class Config(pa.DataFrameModel.Config):
        strict = True
        ordered = True
        unique: str | list[str] | None = "game_id"

    @pa.dataframe_check
    def regular_season_id_of_its_season(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(regular_season_id_of_its_season())

    @pa.dataframe_check
    def home_is_not_away(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(pl.col("home") != pl.col("away"))

    @pa.dataframe_check
    def public_no_sooner_than_a_day_before_start(cls, data: pa.PolarsData) -> pl.LazyFrame:
        # An earlier observed_utc would leak the schedule; a later one (an override for a game
        # re-dated at short notice) is allowed, as long as it is before the start.
        observed, start = pl.col("observed_utc"), pl.col("start_utc")
        return data.lazyframe.select((observed >= start - SCHEDULE_LEAD) & (observed < start))


class Players(pa.DataFrameModel):
    """One NHL player, from the player landing page.

    Only facts fixed before a player's NHL debut belong here, which is why the table has no
    observed_utc: fetched_utc records provenance. Anything that changes over a career (position,
    team, stats, injuries) goes in a table with observed_utc. The landing page's position is the
    one listed today, and so is the boxscore's position code: the role a player had in a game (F,
    D or G) comes from the boxscore group he is listed in (ActualLineups.role).
    """

    player_id: pl.Int64
    name: pl.String = pa.Field(str_length={"min_value": 1})
    birth_date: pl.Date
    shoots: pl.String = pa.Field(isin=("L", "R"), nullable=True)
    draft_year: pl.Int16 = pa.Field(nullable=True)
    draft_overall: pl.Int16 = pa.Field(ge=1, nullable=True)
    fetched_utc: UtcDatetime
    raw_key: pl.String

    class Config(pa.DataFrameModel.Config):
        strict = True
        ordered = True
        unique: str | list[str] | None = "player_id"

    @pa.dataframe_check
    def drafted_or_not(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(
            pl.col("draft_year").is_null() == pl.col("draft_overall").is_null()
        )


# Regular-season periods: three of 20 minutes, then one 5-minute overtime. The shootout (period 5)
# is not play and has no rows in the per-game tables.
PERIOD_S = 1200
OT_PERIOD = 4
OT_S = 300


def period_start_s(period: pl.Expr) -> pl.Expr:
    """Elapsed game seconds at the start of a period."""
    return (period.cast(pl.Int32) - 1) * PERIOD_S


def period_end_s(period: pl.Expr) -> pl.Expr:
    """Elapsed game seconds at the end of a period."""
    return period_start_s(period) + pl.when(period == OT_PERIOD).then(OT_S).otherwise(PERIOD_S)


SHOT_EVENTS = ("shot-on-goal", "missed-shot", "goal")
ZONES = ("O", "N", "D")
ROLES = ("F", "D", "G")
# Where a shot's skater counts come from (ADR 0009): the shift chart when the game's chart is
# complete and puts 3 to 6 skaters of each team on the ice, otherwise the situationCode.
STRENGTH_SOURCES = ("situation_code", "chart")


class Shots(pa.DataFrameModel):
    """One unblocked shot attempt (shot on goal, missed shot or goal) in play-by-play, periods 1
    to 4. Shootout attempts are not shots and are left out; penalty shots are kept and flagged.

    seconds is elapsed game time, (period - 1) * 1200 plus the period clock. team is the shooting
    team. x and y are rink feet, turned so the shooting team attacks the net at x = +89: the API
    gives raw rink coordinates, and the attack direction per team and period is inferred from the
    offensive-zone shots. Strength is the shooting team's view of the skaters on the ice:
    skaters_for and skaters_against (6 means that team's goalie is pulled), and strength such as
    5v4. strength_source says where the counts come from (ADR 0009): the shift chart when the
    game's chart is complete, otherwise situationCode, which drifts in some 2019-21 games (#28).
    is_empty_net, when the defending goalie is off the ice, always comes from situationCode. The
    counts are null for the few shots whose situationCode is missing.

    The prev_ columns describe the play logged just before the shot in the same period, for the
    xG model's rebound and rush flags (#73, ADR 0010): its type, the seconds from it to the shot,
    whether it is logged under the shooting team (a blocked shot is logged under the team that
    took it), and its zone from the shooting team's side. They are null for the period's first
    play; the team is null for plays without one, such as stoppages, and the zone for plays
    without coordinates or zoneCode.

    observed_utc is when the game's play-by-play counts as public: 10:00 UTC the morning after
    game_date, the rule ADR 0003 sets for results. A backfilled feed includes post-game
    corrections live did not have: shooter_id on a goal is the corrected scorer, and a shot record
    may have been added, removed or fixed (ADR 0004).
    """

    game_id: pl.Int64
    season: pl.Int32
    game_date: pl.Date
    event_id: pl.Int32
    sort_order: pl.Int32
    period: pl.Int8 = pa.Field(ge=1, le=OT_PERIOD)
    seconds: pl.Int32 = pa.Field(ge=0)
    team: pl.String = pa.Field(str_matches=TRI_CODE)
    is_home: pl.Boolean
    event_type: pl.String = pa.Field(isin=SHOT_EVENTS)
    is_goal: pl.Boolean
    shooter_id: pl.Int64
    goalie_id: pl.Int64 = pa.Field(nullable=True)
    shot_type: pl.String = pa.Field(nullable=True)
    zone: pl.String = pa.Field(isin=ZONES, nullable=True)
    x: pl.Int16 = pa.Field(ge=-100, le=100, nullable=True)
    y: pl.Int16 = pa.Field(ge=-43, le=43, nullable=True)
    skaters_for: pl.Int8 = pa.Field(ge=0, le=6, nullable=True)
    skaters_against: pl.Int8 = pa.Field(ge=0, le=6, nullable=True)
    strength: pl.String = pa.Field(str_matches=r"^[0-6]v[0-6]$", nullable=True)
    is_empty_net: pl.Boolean
    is_penalty_shot: pl.Boolean
    situation_code: pl.String = pa.Field(nullable=True)
    strength_source: pl.String = pa.Field(isin=STRENGTH_SOURCES, nullable=True)
    prev_event_type: pl.String = pa.Field(nullable=True)
    prev_seconds: pl.Int32 = pa.Field(ge=0, nullable=True)
    prev_by_shooting_team: pl.Boolean = pa.Field(nullable=True)
    prev_zone: pl.String = pa.Field(isin=ZONES, nullable=True)
    observed_utc: UtcDatetime
    raw_key: pl.String

    class Config(pa.DataFrameModel.Config):
        strict = True
        ordered = True
        unique: str | list[str] | None = ["game_id", "event_id"]  # noqa: RUF012 (pandera config)

    @pa.dataframe_check
    def strength_has_a_source(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(
            pl.col("strength").is_null() == pl.col("strength_source").is_null()
        )

    @pa.dataframe_check
    def goal_flag_matches_event(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(pl.col("is_goal") == (pl.col("event_type") == "goal"))

    @pa.dataframe_check
    def seconds_inside_the_period(cls, data: pa.PolarsData) -> pl.LazyFrame:
        seconds, period = pl.col("seconds"), pl.col("period")
        return data.lazyframe.select(
            (seconds >= period_start_s(period)) & (seconds <= period_end_s(period))
        )


class StrengthTime(pa.DataFrameModel):
    """The seconds one team spent at one strength state in a game (#72): strength in the team's
    own view (5v4 is its power play), and whether its own or the opponent's net was empty. The
    two teams of a game mirror each other. Skater counts follow ADR 0009, as in shots:
    strength_source says whether they came from the complete shift chart or situationCode.
    game_seconds is the game's length, overtime included and shootout left out, which each
    team's seconds should add up to. Rows count as public at 10:00 UTC the morning after the game
    (ADR 0004)."""

    game_id: pl.Int64
    season: pl.Int32
    game_date: pl.Date
    team: pl.String = pa.Field(str_matches=TRI_CODE)
    is_home: pl.Boolean
    strength: pl.String = pa.Field(str_matches=r"^[0-6]v[0-6]$")
    own_net_empty: pl.Boolean
    opp_net_empty: pl.Boolean
    strength_source: pl.String = pa.Field(isin=STRENGTH_SOURCES)
    seconds: pl.Int32 = pa.Field(gt=0)
    game_seconds: pl.Int32 = pa.Field(ge=0)
    observed_utc: UtcDatetime
    raw_key: pl.String

    class Config(pa.DataFrameModel.Config):
        strict = True
        ordered = True
        unique: str | list[str] | None = [  # noqa: RUF012 (pandera config)
            "game_id",
            "team",
            "strength",
            "own_net_empty",
            "opp_net_empty",
            "strength_source",
        ]

    @pa.dataframe_check
    def regular_season_id_of_its_season(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(regular_season_id_of_its_season())


# Penalty types in play-by-play (#96): minor (a double minor is a minor of 4 minutes), major,
# misconduct, game misconduct, match, bench minor, and a penalty shot, which puts no one in the box.
PENALTY_TYPES = ("MIN", "MAJ", "MIS", "GAM", "MAT", "BEN", "PS")


class Penalties(pa.DataFrameModel):
    """One penalty in a game's play-by-play (#96), periods 1 to 4. A penalty logged in the
    shootout is not play and is left out: 2 in 2010-11 to 2021-22.

    seconds is elapsed game time, as in Shots. team is the penalized team, the play's
    eventOwnerTeamId. committed_by is the player penalized, drawn_by the player fouled and
    served_by the player who sat in the box for someone else, such as a bench minor or a goalie's
    penalty; each is null where the feed names no one. type_code is in PENALTY_TYPES and
    duration_min is the feed's minutes in the box: a misconduct's 10 minutes do not leave the team
    short-handed, and a penalty shot's 0 is not a power play.

    observed_utc is 10:00 UTC the morning after game_date, as for Shots (ADR 0004).
    """

    game_id: pl.Int64
    season: pl.Int32
    game_date: pl.Date
    event_id: pl.Int32
    sort_order: pl.Int32
    period: pl.Int8 = pa.Field(ge=1, le=OT_PERIOD)
    seconds: pl.Int32 = pa.Field(ge=0)
    team: pl.String = pa.Field(str_matches=TRI_CODE)
    is_home: pl.Boolean
    committed_by: pl.Int64 = pa.Field(nullable=True)
    drawn_by: pl.Int64 = pa.Field(nullable=True)
    served_by: pl.Int64 = pa.Field(nullable=True)
    type_code: pl.String = pa.Field(isin=PENALTY_TYPES)
    desc_key: pl.String
    duration_min: pl.Int8 = pa.Field(ge=0)
    observed_utc: UtcDatetime
    raw_key: pl.String

    class Config(pa.DataFrameModel.Config):
        strict = True
        ordered = True
        unique: str | list[str] | None = ["game_id", "event_id"]  # noqa: RUF012 (pandera config)

    @pa.dataframe_check
    def regular_season_id_of_its_season(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(regular_season_id_of_its_season())

    @pa.dataframe_check
    def seconds_inside_the_period(cls, data: pa.PolarsData) -> pl.LazyFrame:
        seconds, period = pl.col("seconds"), pl.col("period")
        return data.lazyframe.select(
            (seconds >= period_start_s(period)) & (seconds <= period_end_s(period))
        )


class Faceoffs(pa.DataFrameModel):
    """One faceoff in a game's play-by-play (#96), periods 1 to 4.

    seconds is elapsed game time, as in Shots. winning_team is the play's eventOwnerTeamId, and
    home_won says whether it is the home team. zone is where the faceoff was, from the home
    team's side: the feed's zoneCode is from the winner's side, so it flips O and D when the away
    team won. Checked on every game of 2019-20 to 2021-22, where plays say which end the home team
    defends: of 150,175 faceoffs away from center ice, the coordinates agree with the zone for all
    but 101. All but one of those fill two whole games (2019020249, 2019020256) and a period of
    2020020175, where the side or the coordinates are flipped throughout.

    observed_utc is 10:00 UTC the morning after game_date, as for Shots (ADR 0004).
    """

    game_id: pl.Int64
    season: pl.Int32
    game_date: pl.Date
    event_id: pl.Int32
    sort_order: pl.Int32
    period: pl.Int8 = pa.Field(ge=1, le=OT_PERIOD)
    seconds: pl.Int32 = pa.Field(ge=0)
    winning_team: pl.String = pa.Field(str_matches=TRI_CODE)
    home_won: pl.Boolean
    winner_id: pl.Int64
    loser_id: pl.Int64
    zone: pl.String = pa.Field(isin=ZONES)
    observed_utc: UtcDatetime
    raw_key: pl.String

    class Config(pa.DataFrameModel.Config):
        strict = True
        ordered = True
        unique: str | list[str] | None = ["game_id", "event_id"]  # noqa: RUF012 (pandera config)

    @pa.dataframe_check
    def regular_season_id_of_its_season(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(regular_season_id_of_its_season())

    @pa.dataframe_check
    def seconds_inside_the_period(cls, data: pa.PolarsData) -> pl.LazyFrame:
        seconds, period = pl.col("seconds"), pl.col("period")
        return data.lazyframe.select(
            (seconds >= period_start_s(period)) & (seconds <= period_end_s(period))
        )


class ShotXg(pa.DataFrameModel):
    """A shot's expected goals (#73, ADR 0010): the probability that the unblocked shot became a
    goal, from the xG model of its season, which was fitted only on shots public before the
    season's first game. train_cutoff is the last training shot's observed_utc, and
    artifact_version names the fit. Penalty shots, shots at an empty net and shots without
    coordinates get no xG. observed_utc is the shot's own: the model predates every shot it
    scores."""

    game_id: pl.Int64
    season: pl.Int32
    game_date: pl.Date
    event_id: pl.Int32
    xg: pl.Float64 = pa.Field(gt=0, lt=1)
    train_cutoff: UtcDatetime
    artifact_version: pl.String = pa.Field(str_matches=r"^xg-\d{8}-")
    observed_utc: UtcDatetime

    class Config(pa.DataFrameModel.Config):
        strict = True
        ordered = True
        unique: str | list[str] | None = ["game_id", "event_id"]  # noqa: RUF012 (pandera config)

    @pa.dataframe_check
    def model_predates_the_shot(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(pl.col("train_cutoff") < pl.col("observed_utc"))


class TeamStrength(pa.DataFrameModel):
    """One game's rolling team strength ΔS (#74, ADR 0011): the home team's expected goal margin
    over the away team, from each team's team-games public before as_of_utc: 10:00 US Eastern on
    the game date, or an hour before the start if that is earlier. delta_5v5 and
    delta_special_teams are its parts. home_history and away_history count the team-games each
    rating read (0 before a team's first game with xG). half_life and prior_games are the
    settings it was computed with, and artifact_version names the run.

    train_cutoff is the last result the tuning run that chose the settings read (ADR 0011).
    observed_utc, when the rating could be known, is the later of as_of_utc and train_cutoff: a
    rating of 2011-12 to 2017-18, the seasons the settings were tuned on, is unavailable before
    the cutoff, so no fold starting before it can read one (Codex on #86)."""

    game_id: pl.Int64
    season: pl.Int32
    game_date: pl.Date
    home: pl.String = pa.Field(str_matches=TRI_CODE)
    away: pl.String = pa.Field(str_matches=TRI_CODE)
    delta_s: pl.Float64
    delta_5v5: pl.Float64
    delta_special_teams: pl.Float64
    home_history: pl.Int32 = pa.Field(ge=0)
    away_history: pl.Int32 = pa.Field(ge=0)
    half_life: pl.Float64 = pa.Field(gt=0)
    prior_games: pl.Float64 = pa.Field(ge=0)
    train_cutoff: UtcDatetime
    artifact_version: pl.String = pa.Field(str_matches=r"^team-strength-\d{8}-")
    as_of_utc: UtcDatetime
    observed_utc: UtcDatetime

    class Config(pa.DataFrameModel.Config):
        strict = True
        ordered = True
        unique: str | list[str] | None = ["game_id"]  # noqa: RUF012 (pandera config)

    @pa.dataframe_check
    def known_no_earlier_than_its_cutoffs(cls, data: pa.PolarsData) -> pl.LazyFrame:
        observed = pl.col("observed_utc")
        return data.lazyframe.select(
            (observed >= pl.col("as_of_utc")) & (observed >= pl.col("train_cutoff"))
        )

    @pa.dataframe_check
    def parts_add_up(cls, data: pa.PolarsData) -> pl.LazyFrame:
        parts = pl.col("delta_5v5") + pl.col("delta_special_teams")
        return data.lazyframe.select((pl.col("delta_s") - parts).abs() < 1e-9)


class ScheduleTerms(pa.DataFrameModel):
    """One game's schedule terms and season home edge (#77, ADR 0011), as of as_of_utc: 10:00 US
    Eastern on its date, or an hour before the start if that is earlier.

    Per side: rest_days since the team's last game this season (capped at 4; a season's first
    game takes the cap), back_to_back, travel_km from the arena of that game (or the team's home
    arena) to tonight's, and tz_shift, the change in UTC offset in hours (positive going east).
    capacity_share is the share of seats open to spectators as announced by then.

    home_win_rate is the season's home win rate in its non-neutral games public by then
    (season_games of them), pulled toward the three seasons before with a weight worth
    prior_games games, and h_s its log-odds. train_cutoff is the last result the tuning run that
    chose prior_games read, and observed_utc the later of the two (ADR 0011)."""

    game_id: pl.Int64
    season: pl.Int32
    game_date: pl.Date
    home: pl.String = pa.Field(str_matches=TRI_CODE)
    away: pl.String = pa.Field(str_matches=TRI_CODE)
    neutral_site: pl.Boolean
    capacity_share: pl.Float64 = pa.Field(ge=0, le=1)
    home_rest_days: pl.Int8 = pa.Field(ge=0, le=4)
    away_rest_days: pl.Int8 = pa.Field(ge=0, le=4)
    home_back_to_back: pl.Boolean
    away_back_to_back: pl.Boolean
    home_travel_km: pl.Float64 = pa.Field(ge=0)
    away_travel_km: pl.Float64 = pa.Field(ge=0)
    home_tz_shift: pl.Float64 = pa.Field(ge=-12, le=12)
    away_tz_shift: pl.Float64 = pa.Field(ge=-12, le=12)
    home_win_rate: pl.Float64 = pa.Field(gt=0, lt=1)
    h_s: pl.Float64
    season_games: pl.Int32 = pa.Field(ge=0)
    prior_games: pl.Float64 = pa.Field(gt=0)
    train_cutoff: UtcDatetime
    artifact_version: pl.String = pa.Field(str_matches=r"^schedule-terms-\d{8}-")
    as_of_utc: UtcDatetime
    observed_utc: UtcDatetime

    class Config(pa.DataFrameModel.Config):
        strict = True
        ordered = True
        unique: str | list[str] | None = ["game_id"]  # noqa: RUF012 (pandera config)

    @pa.dataframe_check
    def known_no_earlier_than_its_cutoffs(cls, data: pa.PolarsData) -> pl.LazyFrame:
        observed = pl.col("observed_utc")
        return data.lazyframe.select(
            (observed >= pl.col("as_of_utc")) & (observed >= pl.col("train_cutoff"))
        )


class GoalieEffects(pa.DataFrameModel):
    """A candidate goalie's effect before a team-game (#75, ADR 0011), one row per goalie_starts
    candidate. effect is his goals saved above expected per unblocked shot, decayed by his own
    games (half_life) and shrunk toward zero (prior_shots); goalie_games counts the games of his
    it read. expected_shots is the unblocked shots his team is expected to allow, and goals_saved
    their product. B2 takes the home goalie's goals_saved minus the away goalie's as ΔG.

    as_of_utc is the game's as-of time, as for team strength. train_cutoff is the last result the
    tuning run that chose the settings read, and observed_utc the later of the two: an effect of
    2011-12 to 2017-18, the seasons the settings were tuned on, is unavailable before the cutoff
    (ADR 0011)."""

    game_id: pl.Int64
    season: pl.Int32
    game_date: pl.Date
    team: pl.String = pa.Field(str_matches=TRI_CODE)
    goalie_id: pl.Int64
    effect: pl.Float64
    goalie_games: pl.Int32 = pa.Field(ge=0)
    expected_shots: pl.Float64 = pa.Field(ge=0)
    goals_saved: pl.Float64
    half_life: pl.Float64 = pa.Field(gt=0)
    prior_shots: pl.Float64 = pa.Field(ge=0)
    train_cutoff: UtcDatetime
    artifact_version: pl.String = pa.Field(str_matches=r"^goalie-effect-\d{8}-")
    as_of_utc: UtcDatetime
    observed_utc: UtcDatetime

    class Config(pa.DataFrameModel.Config):
        strict = True
        ordered = True
        unique: str | list[str] | None = ["game_id", "team", "goalie_id"]  # noqa: RUF012 (pandera config)

    @pa.dataframe_check
    def known_no_earlier_than_its_cutoffs(cls, data: pa.PolarsData) -> pl.LazyFrame:
        observed = pl.col("observed_utc")
        return data.lazyframe.select(
            (observed >= pl.col("as_of_utc")) & (observed >= pl.col("train_cutoff"))
        )

    @pa.dataframe_check
    def goals_saved_is_effect_times_shots(cls, data: pa.PolarsData) -> pl.LazyFrame:
        product = pl.col("effect") * pl.col("expected_shots")
        return data.lazyframe.select((pl.col("goals_saved") - product).abs() < 1e-9)


class GoalieStarts(pa.DataFrameModel):
    """The probability that a goalie starts a team's game (#76, ADR 0012), one row per candidate:
    each goalie who dressed for the team in its last ten games public before observed_utc. A
    team-game's probabilities add up to 1, and a team with no earlier game has no rows.

    observed_utc is the game's as-of time: 10:00 US Eastern on the game date, or an hour before
    the start if that is earlier, as for team strength. The model was fitted on earlier seasons'
    starters public before the season's first as-of time; train_cutoff is the last of them, and
    artifact_version names the run."""

    game_id: pl.Int64
    season: pl.Int32
    game_date: pl.Date
    team: pl.String = pa.Field(str_matches=TRI_CODE)
    goalie_id: pl.Int64
    p_start: pl.Float64 = pa.Field(gt=0, le=1)
    train_cutoff: UtcDatetime
    artifact_version: pl.String = pa.Field(str_matches=r"^goalie-start-\d{8}-")
    observed_utc: UtcDatetime

    class Config(pa.DataFrameModel.Config):
        strict = True
        ordered = True
        unique: str | list[str] | None = ["game_id", "team", "goalie_id"]  # noqa: RUF012 (pandera config)

    @pa.dataframe_check
    def model_predates_the_game(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(pl.col("train_cutoff") < pl.col("observed_utc"))

    @pa.dataframe_check
    def each_team_game_adds_up_to_one(cls, data: pa.PolarsData) -> pl.LazyFrame:
        total = pl.col("p_start").sum().over("game_id", "team")
        return data.lazyframe.select((total - 1).abs() < 1e-9)


class Shifts(pa.DataFrameModel):
    """One player shift from the shift chart (type 517 rows), periods 1 to 4.

    start_s and end_s are elapsed game seconds, like Shots.seconds. A player is on the ice for an
    event at second t when start_s < t <= end_s. team comes from the shift's teamId, mapped
    through the game's two teams. The parser drops zero-length placeholders, rows of teams not in
    the game, shootout rows and malformed rows, and ShiftCoverage counts them.

    A game whose chart has no shifts takes them from the NHL's time-on-ice reports when both are
    in the raw cache (#68, ingest/toi_reports.py): the 57 games of 2024-25 with an empty chart.
    There a player is found by his sweater number in the game's boxscore, and raw_key is the
    report's (nhl/toi-home/ or nhl/toi-visitor/).

    observed_utc is 10:00 UTC the morning after game_date, as for Shots.
    """

    game_id: pl.Int64
    season: pl.Int32
    game_date: pl.Date
    team: pl.String = pa.Field(str_matches=TRI_CODE)
    player_id: pl.Int64
    period: pl.Int8 = pa.Field(ge=1, le=OT_PERIOD)
    shift_number: pl.Int16 = pa.Field(ge=1)
    start_s: pl.Int32 = pa.Field(ge=0)
    end_s: pl.Int32
    observed_utc: UtcDatetime
    raw_key: pl.String

    class Config(pa.DataFrameModel.Config):
        strict = True
        ordered = True
        unique: str | list[str] | None = [  # noqa: RUF012 (pandera config)
            "game_id",
            "player_id",
            "period",
            "shift_number",
        ]

    @pa.dataframe_check
    def ends_after_it_starts(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(pl.col("end_s") > pl.col("start_s"))

    @pa.dataframe_check
    def inside_its_period(cls, data: pa.PolarsData) -> pl.LazyFrame:
        period = pl.col("period")
        return data.lazyframe.select(
            (pl.col("start_s") >= period_start_s(period))
            & (pl.col("end_s") <= period_end_s(period))
        )


class ActualLineups(pa.DataFrameModel):
    """One player dressed for a game, from the boxscore's playerByGameStats.

    role is the group the boxscore lists him in: F (forwards), D (defense) or G (goalies). That
    is how he was used in this game. The boxscore's position code is left out, because it is the
    position listed today (Brent Burns is listed D in the forwards group all through 2013-14).
    starting_goalie is the boxscore's starter flag, one per team. toi_s is time on ice in seconds.

    These are the only source for backtest lineups (hard rule 9), and never a game's own:
    observed_utc is 10:00 UTC the morning after game_date, as for Shots. Scoring stats (goals,
    assists, points, plus-minus, decision) stay out. A backfilled toi_s can include a post-game
    correction live did not have (ADR 0004).
    """

    game_id: pl.Int64
    season: pl.Int32
    game_date: pl.Date
    team: pl.String = pa.Field(str_matches=TRI_CODE)
    is_home: pl.Boolean
    player_id: pl.Int64
    role: pl.String = pa.Field(isin=ROLES)
    sweater_number: pl.Int16 = pa.Field(ge=0, le=99, nullable=True)
    starting_goalie: pl.Boolean
    toi_s: pl.Int32 = pa.Field(ge=0, nullable=True)
    observed_utc: UtcDatetime
    raw_key: pl.String

    class Config(pa.DataFrameModel.Config):
        strict = True
        ordered = True
        unique: str | list[str] | None = ["game_id", "player_id"]  # noqa: RUF012 (pandera config)

    @pa.dataframe_check
    def starters_are_goalies(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(~pl.col("starting_goalie") | (pl.col("role") == "G"))

    @pa.dataframe_check
    def one_starting_goalie_per_team(cls, data: pa.PolarsData) -> pl.LazyFrame:
        starters = pl.col("starting_goalie").cast(pl.Int32).sum().over("game_id", "team")
        return data.lazyframe.select(starters == 1)


# A player's summed shift time may differ from his boxscore time on ice by this much before the
# game's shift chart counts as incomplete.
TOI_TOLERANCE_S = 60


class ShiftCoverage(pa.DataFrameModel):
    """How far one game's shift chart can be trusted (docs/plan.md section 9).

    Shift rows: shift_rows kept in Shifts; dropped_rows are zero-length placeholders and shootout
    rows, which carry no play; foreign_rows belong to teams not in the game; bad_rows are
    malformed. Players: players_without_shifts dressed with time on ice but have no shift, and
    players_toi_off have summed shifts more than TOI_TOLERANCE_S away from their boxscore time on
    ice, or no boxscore time on ice to check against. complete means no bad rows and every dressed
    player's shifts add up.

    Strength: at every unblocked shot except penalty shots (shots_checked), the skaters and
    goalies on the ice from the shifts are compared with situationCode. skater_mismatches and
    goalie_mismatches count the shots where they differ. Stints keep the chart's counts and leave
    out only impossible ones (ADR 0015).

    observed_utc is 10:00 UTC the morning after game_date, as for Shots. raw_key is the shift
    chart's, or, for a game whose shifts come from its time-on-ice reports (#68), the home team's
    report's: one key per row, though the visitors' report gave half the shifts. The row's counts
    and checks then describe the reports' shifts, by the same rules.
    """

    game_id: pl.Int64
    season: pl.Int32
    game_date: pl.Date
    shift_rows: pl.Int32 = pa.Field(ge=0)
    dropped_rows: pl.Int32 = pa.Field(ge=0)
    foreign_rows: pl.Int32 = pa.Field(ge=0)
    bad_rows: pl.Int32 = pa.Field(ge=0)
    players_dressed: pl.Int16 = pa.Field(ge=0)
    players_without_shifts: pl.Int16 = pa.Field(ge=0)
    players_toi_off: pl.Int16 = pa.Field(ge=0)
    shots_checked: pl.Int32 = pa.Field(ge=0)
    skater_mismatches: pl.Int32 = pa.Field(ge=0)
    goalie_mismatches: pl.Int32 = pa.Field(ge=0)
    complete: pl.Boolean
    observed_utc: UtcDatetime
    raw_key: pl.String

    class Config(pa.DataFrameModel.Config):
        strict = True
        ordered = True
        unique: str | list[str] | None = "game_id"

    @pa.dataframe_check
    def complete_means_no_gaps(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(
            pl.col("complete")
            == (
                (pl.col("bad_rows") == 0)
                & (pl.col("players_without_shifts") == 0)
                & (pl.col("players_toi_off") == 0)
            )
        )

    @pa.dataframe_check
    def mismatches_within_shots(cls, data: pa.PolarsData) -> pl.LazyFrame:
        checked = pl.col("shots_checked")
        return data.lazyframe.select(
            (pl.col("skater_mismatches") <= checked) & (pl.col("goalie_mismatches") <= checked)
        )


# Why a stint is left out of RAPM (ADR 0015): a team with fewer than 3 or more than 6 skaters on
# the ice by the shift chart, or with two goalies.
STINT_DROPS = ("skaters", "goalies")
# The skaters of one team a stint may have, its goalie in or pulled (as CHART_SKATERS).
STINT_SKATERS = (3, 6)


class Stints(pa.DataFrameModel):
    """One stint (#97, ADR 0015): a stretch of a period in which the players on the ice, by the
    game's shift chart, and the score do not change. Only games whose chart is complete
    (ShiftCoverage) have stints. RAPM's rows (docs/plan.md sections 4 and 5).

    start_s and end_s are elapsed game seconds, and the stint holds every moment t with
    start_s < t <= end_s, as a shift does. stint_id numbers a game's stints in time order.
    home_skaters and away_skaters are the skaters' player ids, sorted; a player not in the
    boxscore's goalies group counts as a skater. home_goalie and away_goalie are each team's one
    goalie on the ice, null when its net is empty or it has two. strength is the home team's
    skaters, then the away team's, as 5v4: a pulled goalie's team has 6.

    score_state is the home team's goals minus the away team's at the stint's start, every goal of
    periods 1 to 4 counted. zone_start is the zone of a faceoff at the stint's start from the home
    team's side (Faceoffs), null when the stint starts with a change on the fly. home_xg,
    away_xg, home_goals and away_goals count each team's unblocked shots in the stint, penalty
    shots left out: xG from shot_xg, which gives none to shots at an empty net or without
    coordinates, and goals from shots. The xG columns, xg_version and xg_train_cutoff are null in
    a season without xG, such as 2010-11. A season's xG comes from one model, fitted before its
    first game (hard rule 1); features/stints.py refuses anything else, and a shot that model
    scores without a shot_xg row.

    drop_reason says why RAPM leaves the stint out (STINT_DROPS, ADR 0015), null for a stint it
    keeps. observed_utc is 10:00 UTC the morning after game_date, with the game's feeds (ADR 0004).
    """

    game_id: pl.Int64
    season: pl.Int32
    game_date: pl.Date
    stint_id: pl.Int16 = pa.Field(ge=1)
    period: pl.Int8 = pa.Field(ge=1, le=OT_PERIOD)
    start_s: pl.Int32 = pa.Field(ge=0)
    end_s: pl.Int32
    seconds: pl.Int32 = pa.Field(gt=0)
    home_skaters: Annotated[pl.List, pl.Int64()]
    away_skaters: Annotated[pl.List, pl.Int64()]
    home_goalie: pl.Int64 = pa.Field(nullable=True)
    away_goalie: pl.Int64 = pa.Field(nullable=True)
    strength: pl.String = pa.Field(str_matches=r"^\d+v\d+$")
    score_state: pl.Int8
    zone_start: pl.String = pa.Field(isin=ZONES, nullable=True)
    home_xg: pl.Float64 = pa.Field(ge=0, nullable=True)
    away_xg: pl.Float64 = pa.Field(ge=0, nullable=True)
    home_goals: pl.Int8 = pa.Field(ge=0)
    away_goals: pl.Int8 = pa.Field(ge=0)
    drop_reason: pl.String = pa.Field(isin=STINT_DROPS, nullable=True)
    xg_version: pl.String = pa.Field(str_matches=r"^xg-\d{8}-", nullable=True)
    xg_train_cutoff: UtcDatetime = pa.Field(nullable=True)
    observed_utc: UtcDatetime

    class Config(pa.DataFrameModel.Config):
        strict = True
        ordered = True
        unique: str | list[str] | None = ["game_id", "stint_id"]  # noqa: RUF012 (pandera config)

    @pa.dataframe_check
    def regular_season_id_of_its_season(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(regular_season_id_of_its_season())

    @pa.dataframe_check
    def inside_its_period(cls, data: pa.PolarsData) -> pl.LazyFrame:
        period = pl.col("period")
        return data.lazyframe.select(
            (pl.col("start_s") >= period_start_s(period))
            & (pl.col("end_s") <= period_end_s(period))
            & (pl.col("seconds") == pl.col("end_s") - pl.col("start_s"))
        )

    @pa.dataframe_check
    def strength_counts_the_skaters(cls, data: pa.PolarsData) -> pl.LazyFrame:
        counts = pl.format(
            "{}v{}", pl.col("home_skaters").list.len(), pl.col("away_skaters").list.len()
        )
        return data.lazyframe.select(pl.col("strength") == counts)

    @pa.dataframe_check
    def dropped_only_when_impossible(cls, data: pa.PolarsData) -> pl.LazyFrame:
        low, high = STINT_SKATERS
        possible = pl.col("home_skaters").list.len().is_between(low, high) & pl.col(
            "away_skaters"
        ).list.len().is_between(low, high)
        reason = pl.col("drop_reason")
        return data.lazyframe.select(
            pl.when(~possible)
            .then(reason == "skaters")
            .otherwise(reason.is_null() | (reason == "goalies"))
        )

    @pa.dataframe_check
    def xg_model_predates_the_game(cls, data: pa.PolarsData) -> pl.LazyFrame:
        cutoff = pl.col("xg_train_cutoff")
        versioned = pl.col("xg_version").is_not_null()
        return data.lazyframe.select(
            (versioned == cutoff.is_not_null())
            & (versioned == pl.col("home_xg").is_not_null())
            & (versioned == pl.col("away_xg").is_not_null())
            & (cutoff.is_null() | (cutoff < pl.col("observed_utc")))
        )


PREGAME_GOALIES_KEY = ("game_id", "team", "observed_utc")


class PregameGoalies(pa.DataFrameModel):
    """One team of one game at one pre-game poll (#42): what the gamecenter boxscore said about its
    goalies at observed_utc, the fetch time, which is when that state was public.

    goalies_listed counts the goalies in playerByGameStats (0 while the boxscore has none),
    starters_flagged those with the starter flag, and starter_id is the flagged goalie when
    exactly one is. A null starter_id is a finding, not a gap: nothing confirmed yet. Only
    responses fetched before start_utc are kept. Actual starters come from actual_lineups.
    """

    season: pl.Int32
    game_date: pl.Date
    game_id: pl.Int64
    team: pl.String = pa.Field(str_matches=TRI_CODE)
    is_home: pl.Boolean
    start_utc: UtcDatetime
    observed_utc: UtcDatetime
    game_state: pl.String
    goalies_listed: pl.Int16 = pa.Field(ge=0)
    starters_flagged: pl.Int16 = pa.Field(ge=0)
    starter_id: pl.Int64 = pa.Field(nullable=True)
    raw_key: pl.String

    class Config(pa.DataFrameModel.Config):
        strict = True
        ordered = True
        unique: str | list[str] | None = list(PREGAME_GOALIES_KEY)  # noqa: RUF012 (pandera config)

    @pa.dataframe_check
    def observed_before_start(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(pl.col("observed_utc") < pl.col("start_utc"))

    @pa.dataframe_check
    def starter_only_when_one_is_flagged(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(
            pl.col("starter_id").is_not_null() == (pl.col("starters_flagged") == 1)
        )

    @pa.dataframe_check
    def flagged_within_listed(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(pl.col("starters_flagged") <= pl.col("goalies_listed"))


DAILYFACEOFF_GOALIES_KEY = ("start_utc", "team", "observed_utc")


class DailyFaceoffGoalies(pa.DataFrameModel):
    """One team of one game in a Daily Faceoff starting-goalies page fetched before the start
    (#48): the goalie it expects to start and the status of that report.

    observed_utc is the fetch time, which is when a prediction may use the row: a status can
    change, so reported_utc (Daily Faceoff's own time for the report, null without one) only
    shows how early it was published. status is Daily Faceoff's label, such as Likely or
    Confirmed, and null before any report. Goalies are kept by name and Daily Faceoff id.
    There is no NHL game_id; start_utc and team identify the game.
    """

    season: pl.Int32
    game_date: pl.Date
    start_utc: UtcDatetime
    team: pl.String = pa.Field(str_matches=TRI_CODE)
    is_home: pl.Boolean
    goalie_name: pl.String = pa.Field(nullable=True)
    dfo_goalie_id: pl.Int64 = pa.Field(nullable=True)
    status: pl.String = pa.Field(nullable=True)
    reported_utc: UtcDatetime = pa.Field(nullable=True)
    source_url: pl.String = pa.Field(nullable=True)
    observed_utc: UtcDatetime
    raw_key: pl.String

    class Config(pa.DataFrameModel.Config):
        strict = True
        ordered = True
        unique: str | list[str] | None = list(DAILYFACEOFF_GOALIES_KEY)  # noqa: RUF012 (pandera config)

    @pa.dataframe_check
    def observed_before_start(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(pl.col("observed_utc") < pl.col("start_utc"))

    @pa.dataframe_check
    def reported_not_after_observed(cls, data: pa.PolarsData) -> pl.LazyFrame:
        reported = pl.col("reported_utc")
        return data.lazyframe.select(
            reported.is_null() | (reported <= pl.col("observed_utc") + CLOCK_SKEW)
        )


# Reference files (src/nhl_edge/reference/): hand-compiled CSVs, not lake tables. They cover the
# lake's seasons, 2010-11 on.
ARENA_ID = r"^[a-z0-9_]+$"


def valid_season(season: pl.Expr) -> pl.Expr:
    """20232024: consecutive years. Null passes, for an open-ended last_season."""
    return (season % 10_000 == season // 10_000 + 1).fill_null(True)


def is_time_zone(tz: str) -> bool:
    """A zone Polars can convert to, which is how features will use it."""
    try:
        pl.Series([datetime(2000, 1, 1)]).dt.replace_time_zone(tz)
    except pl.exceptions.ComputeError:
        return False
    return True


class Teams(pa.DataFrameModel):
    """One NHL API team code (triCode) used from 2010-11 on, with the seasons it was used.

    first_season is 20102011 for a code already in use then. last_season is null while the code
    is in use. predecessor is the code the team continues under a new name or city: ATL became
    WPG in 2011-12, PHX became ARI in 2014-15, and ARI became UTA in 2024-25, when the Coyotes'
    players and staff moved to Utah. The NHL counts Utah as a new franchise (franchise_id 40), so
    predecessor, not franchise_id, links codes that are one team. Known seasons ahead, so no
    observed_utc.
    """

    team: pl.String = pa.Field(str_matches=TRI_CODE)
    franchise_id: pl.Int16 = pa.Field(ge=1)
    name: pl.String = pa.Field(str_length={"min_value": 1})
    first_season: pl.Int32 = pa.Field(ge=20102011)
    last_season: pl.Int32 = pa.Field(nullable=True)
    predecessor: pl.String = pa.Field(str_matches=TRI_CODE, nullable=True)

    class Config(pa.DataFrameModel.Config):
        strict = True
        ordered = True
        unique: str | list[str] | None = "team"

    @pa.dataframe_check
    def seasons_are_seasons(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(
            valid_season(pl.col("first_season")) & valid_season(pl.col("last_season"))
        )

    @pa.dataframe_check
    def last_not_before_first(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(
            (pl.col("last_season") >= pl.col("first_season")).fill_null(True)
        )


class Arenas(pa.DataFrameModel):
    """One building an NHL regular-season game was played in from 2010-11 on, home arena or
    neutral site (European games, outdoor games, Lake Tahoe).

    arena_id is the slug of name, the latest name the NHL API gave the building; Venues maps every
    earlier name to it. latitude and longitude are WGS84 degrees and tz an IANA zone, for travel
    and time-zone features. source is the Wikipedia article the coordinates come from. Buildings
    don't move, so no observed_utc.
    """

    arena_id: pl.String = pa.Field(str_matches=ARENA_ID)
    name: pl.String = pa.Field(str_length={"min_value": 1})
    city: pl.String = pa.Field(str_length={"min_value": 1})
    country: pl.String = pa.Field(str_matches=r"^[A-Z]{3}$")
    latitude: pl.Float64 = pa.Field(ge=-90, le=90)
    longitude: pl.Float64 = pa.Field(ge=-180, le=180)
    tz: pl.String = pa.Field(str_matches=r"^(America|Europe)/[A-Za-z_/]+$")
    source: pl.String = pa.Field(str_startswith="https://")

    class Config(pa.DataFrameModel.Config):
        strict = True
        ordered = True
        unique: str | list[str] | None = "arena_id"

    @pa.dataframe_check
    def tz_is_a_zone(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(pl.col("tz").map_elements(is_time_zone, pl.Boolean))

    @pa.dataframe_check
    def names_are_unique(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(pl.col("name").is_unique())


class Venues(pa.DataFrameModel):
    """Every venue name the NHL API gives a regular-season game (Schedule.venue), mapped to its
    building. Renames of one building map to one arena_id: Pepsi Center and Ball Arena are both
    ball_arena."""

    venue: pl.String = pa.Field(str_length={"min_value": 1})
    arena_id: pl.String = pa.Field(str_matches=ARENA_ID)

    class Config(pa.DataFrameModel.Config):
        strict = True
        ordered = True
        unique: str | list[str] | None = "venue"


class HomeArenas(pa.DataFrameModel):
    """The arena a team played its home games in, by season, from 2010-11 on.

    A team has one primary home arena per season, its base for travel: the arena of its first
    home game that season, which the schedule makes public before the season starts. NYI split its
    home games between Barclays Center and the Nassau Coliseum in 2018-19 and 2019-20, so those
    seasons list both: Barclays is primary in 2018-19 and the Coliseum in 2019-20. last_season is
    null while in use.
    """

    team: pl.String = pa.Field(str_matches=TRI_CODE)
    arena_id: pl.String = pa.Field(str_matches=ARENA_ID)
    first_season: pl.Int32 = pa.Field(ge=20102011)
    last_season: pl.Int32 = pa.Field(nullable=True)
    primary: pl.Boolean

    class Config(pa.DataFrameModel.Config):
        strict = True
        ordered = True
        unique: str | list[str] | None = ["team", "arena_id", "first_season"]  # noqa: RUF012

    @pa.dataframe_check
    def seasons_are_seasons(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(
            valid_season(pl.col("first_season")) & valid_season(pl.col("last_season"))
        )

    @pa.dataframe_check
    def last_not_before_first(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(
            (pl.col("last_season") >= pl.col("first_season")).fill_null(True)
        )


class CoachTenures(pa.DataFrameModel):
    """One head coach's stint with a team, from his first to his last regular-season game.

    team is the code at the first game. A stint runs on through a change of code (Tippett from PHX
    to ARI, Tourigny from ARI to UTA), which Teams.predecessor links. last_game is null for a
    current coach. A coach with two stints with a team has two rows. coach is null only for games
    the NHL credits to no coach, which note explains, as it does interim and shared benches.

    A stint's end is future information while it runs: features read tenures through
    reference.coaches_known_at, never this table directly.
    """

    team: pl.String = pa.Field(str_matches=TRI_CODE)
    first_game: pl.Date
    last_game: pl.Date = pa.Field(nullable=True)
    coach: pl.String = pa.Field(nullable=True)
    note: pl.String = pa.Field(nullable=True)

    class Config(pa.DataFrameModel.Config):
        strict = True
        ordered = True
        unique: str | list[str] | None = ["team", "first_game"]  # noqa: RUF012

    @pa.dataframe_check
    def last_not_before_first(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select((pl.col("last_game") >= pl.col("first_game")).fill_null(True))

    @pa.dataframe_check
    def uncredited_games_are_explained(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(pl.col("coach").is_not_null() | pl.col("note").is_not_null())


# Seasons whose first day came with spectator limits already in force, known before the season.
LIMITED_SEASON_STARTS = (date(2021, 1, 13),)


class AttendanceLimits(pa.DataFrameModel):
    """A limit on spectators at one arena over a date range, from first_date to last_date.

    capacity_share is the share of the arena's seats that could be filled: 0 means no spectators.
    A head-count limit is divided by the arena's seating capacity, as limit shows. A game at an
    arena and date no row covers had no limit.

    announced is the date of the source reporting the limit (source). It is null only for a limit
    in force from the first day of a season that started under limits (LIMITED_SEASON_STARTS), known
    before the season. A limit ends either when the next one starts the day after, or when it is
    lifted: ended_announced is then the date of the source reporting the lift (ended_source). It is
    null when a limit ended with its season or event. Features read shares through
    reference.capacity_share, which uses a limit, and a lift, only once announced.
    """

    arena_id: pl.String = pa.Field(str_matches=ARENA_ID)
    first_date: pl.Date
    last_date: pl.Date
    capacity_share: pl.Float64 = pa.Field(ge=0, lt=1)
    limit: pl.String = pa.Field(str_length={"min_value": 1})
    announced: pl.Date = pa.Field(nullable=True)
    source: pl.String = pa.Field(str_startswith="https://")
    ended_announced: pl.Date = pa.Field(nullable=True)
    ended_source: pl.String = pa.Field(str_startswith="https://", nullable=True)

    class Config(pa.DataFrameModel.Config):
        strict = True
        ordered = True
        unique: str | list[str] | None = ["arena_id", "first_date"]  # noqa: RUF012

    @pa.dataframe_check
    def last_not_before_first(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(pl.col("last_date") >= pl.col("first_date"))

    @pa.dataframe_check
    def announced_while_in_force_or_before(cls, data: pa.PolarsData) -> pl.LazyFrame:
        # A limit reported only after it ended would never apply to a game.
        return data.lazyframe.select((pl.col("announced") <= pl.col("last_date")).fill_null(True))

    @pa.dataframe_check
    def unannounced_only_from_a_limited_season_start(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(
            pl.col("announced").is_not_null()
            | pl.col("first_date").is_in(list(LIMITED_SEASON_STARTS))
        )

    @pa.dataframe_check
    def a_lift_has_its_source(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(
            pl.col("ended_announced").is_null() == pl.col("ended_source").is_null()
        )
