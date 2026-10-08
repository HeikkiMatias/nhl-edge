"""One pandera schema per table. Every write validates against its schema."""

from datetime import UTC, date, datetime, timedelta
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
    closing proxy keys on the event, its start the commence time of its latest snapshot, since
    quotes priced before a postponement carry the rescheduled game's id. is_closing_proxy marks
    both sides of each started game's closing proxy per book, h2h only (#21, ADR 0033,
    market/closing.py).
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


class Slate(pa.DataFrameModel):
    """One regular-season game scheduled for a game date, as the NHL schedule listed it when it
    was fetched: the targets a live prediction rates (docs/plans/phase-5.md, task 1, #162).

    observed_utc is the actual fetch time of the schedule response, not a convention: the slate
    is what this installation knew at that moment. Only games whose schedule state is OK are
    listed, so a postponed or cancelled game has no row. A slate never holds a result; the game's
    state when fetched (FUT before the start) is kept for the decision's checks.
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
    game_state: pl.String
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


class PaperLedger(pa.DataFrameModel):
    """One slate game's paper decision (nhl predict, #164; ADRs 0028, 0030 and 0033): its
    prediction and bet, or why it has none (status).

    Every row carries the decision instant (prediction_utc, fixed when the run started; one per
    date), when the decision was published (published_utc: the actual clock as the ledger was
    built and written, minutes later once the models were read; #170), the policy and the
    versions of the live fit, the feature build and the code. Every model input was known before
    the decision snapshot, when the price bet was observed. A predicted row adds
    Pinnacle's prices at the decision snapshot with their last update, the best other EU book's
    beside them, B0 to B3, u's parts, u and u_sd, and the blend and its twins. A picked row adds
    the side, its price and expected return, the hurdle, the guard's move, and the stake: its
    share of the day's bankroll times the bankroll. Bets settle on the full game, OT and shootout
    included. Each date is written once to R2 (ledger/live/<date>.parquet), and every prediction
    is decided and published before its game's start.
    """

    game_date: pl.Date
    season: pl.Int32
    game_id: pl.Int64
    start_utc: UtcDatetime
    home: pl.String = pa.Field(str_matches=TRI_CODE)
    away: pl.String = pa.Field(str_matches=TRI_CODE)
    prediction_utc: UtcDatetime
    published_utc: UtcDatetime
    status: pl.String
    event_id: pl.String = pa.Field(nullable=True)
    decision_snapshot_utc: UtcDatetime = pa.Field(nullable=True)
    home_price: pl.Float64 = pa.Field(gt=1, nullable=True)
    away_price: pl.Float64 = pa.Field(gt=1, nullable=True)
    last_update_utc: UtcDatetime = pa.Field(nullable=True)
    best_home_price: pl.Float64 = pa.Field(gt=1, nullable=True)
    best_home_book: pl.String = pa.Field(nullable=True)
    best_home_update_utc: UtcDatetime = pa.Field(nullable=True)
    best_away_price: pl.Float64 = pa.Field(gt=1, nullable=True)
    best_away_book: pl.String = pa.Field(nullable=True)
    best_away_update_utc: UtcDatetime = pa.Field(nullable=True)
    p_b0: pl.Float64 = pa.Field(gt=0, lt=1, nullable=True)
    p_b1: pl.Float64 = pa.Field(gt=0, lt=1, nullable=True)
    p_b2: pl.Float64 = pa.Field(gt=0, lt=1, nullable=True)
    p_b3: pl.Float64 = pa.Field(gt=0, lt=1, nullable=True)
    goalie_doubt: pl.Float64 = pa.Field(nullable=True)
    availability_doubt: pl.Float64 = pa.Field(nullable=True)
    rookie_share: pl.Float64 = pa.Field(nullable=True)
    u: pl.Float64 = pa.Field(nullable=True)
    u_sd: pl.Float64 = pa.Field(nullable=True)
    p_blend: pl.Float64 = pa.Field(gt=0, lt=1, nullable=True)
    p_blend_b2: pl.Float64 = pa.Field(gt=0, lt=1, nullable=True)
    p_blend_market: pl.Float64 = pa.Field(gt=0, lt=1, nullable=True)
    side: pl.String = pa.Field(isin=["home", "away"], nullable=True)
    price: pl.Float64 = pa.Field(gt=1, nullable=True)
    p_side: pl.Float64 = pa.Field(nullable=True)
    ev: pl.Float64 = pa.Field(nullable=True)
    hurdle: pl.Float64 = pa.Field(nullable=True)
    picked: pl.Boolean = pa.Field(nullable=True)
    p_morning: pl.Float64 = pa.Field(nullable=True)
    morning_utc: UtcDatetime = pa.Field(nullable=True)
    p_decision: pl.Float64 = pa.Field(nullable=True)
    guard_decision_utc: UtcDatetime = pa.Field(nullable=True)
    moved_against: pl.Float64 = pa.Field(nullable=True)
    guarded: pl.Boolean = pa.Field(nullable=True)
    bet: pl.Boolean = pa.Field(nullable=True)
    fraction: pl.Float64 = pa.Field(ge=0, le=0.015, nullable=True)
    bankroll: pl.Float64 = pa.Field(nullable=True)
    stake: pl.Float64 = pa.Field(ge=0, nullable=True)
    policy_version: pl.String
    blend_version: pl.String = pa.Field(str_matches=r"^blend-live-\d{8}-")
    feature_build: pl.String = pa.Field(nullable=True)
    code_version: pl.String
    b2_train_cutoff: UtcDatetime = pa.Field(nullable=True)
    b3_train_cutoff: UtcDatetime = pa.Field(nullable=True)

    class Config(pa.DataFrameModel.Config):
        strict = True
        ordered = True
        # A game postponed after its decision is decided again on its new date.
        unique: str | list[str] | None = ["game_date", "game_id"]  # noqa: RUF012 (pandera config)

    @pa.dataframe_check
    def decided_and_published_before_the_start(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(
            (pl.col("status") != "predicted")
            | (
                (pl.col("prediction_utc") < pl.col("start_utc"))
                & (pl.col("published_utc") < pl.col("start_utc"))
            )
        )

    @pa.dataframe_check
    def published_after_the_decision(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(pl.col("published_utc") >= pl.col("prediction_utc"))

    @pa.dataframe_check
    def a_bet_only_on_a_prediction(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(
            ~pl.col("bet").fill_null(False) | (pl.col("status") == "predicted")
        )

    @pa.dataframe_check
    def one_decision_a_day(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(
            (pl.col("prediction_utc").n_unique().over("game_date") == 1)
            & (pl.col("published_utc").n_unique().over("game_date") == 1)
        )


class PaperSettlements(pa.DataFrameModel):
    """One settled paper bet (nhl live settle, #165): a bet of the paper ledger whose game is
    final, with its result on the full game (OT and shootout included, hard rule 2), its profit,
    and its closing line value against Pinnacle's closing proxy (ADR 0033, market/closing.py).

    won is whether the bet's side won the full game (home_win from games' scores, which count the
    shootout winner's goal), and profit is stake·(price - 1) when it did, -stake when it didn't. A
    game played more than 12 hours from the ledger's start is a postponed game: its bet is void
    (status "void"), with no profit and no CLV. close_status is "proxy" when the game is eligible
    and Pinnacle has a closing proxy taken after the bet's decision snapshot, or why it counts
    without one (no pre-game snapshot, which holds even for an incidental proxy, stale, missing).
    Only a proxy gives close_home and close_away, p_close (the bet's side, de-vigged
    multiplicatively), clv = price·p_close - 1, and fair_move (p_close over the decision's
    de-vigged probability of the side, less 1). The ledger's own columns travel along:
    the price taken, the decision's Pinnacle pair (home_price, away_price) and its snapshot.
    settled_utc and code_version name the run; the table is rebuilt whole each night from the
    ledgers in R2, the games and the odds.
    """

    game_date: pl.Date
    season: pl.Int32
    game_id: pl.Int64
    event_id: pl.String
    start_utc: UtcDatetime
    home: pl.String = pa.Field(str_matches=TRI_CODE)
    away: pl.String = pa.Field(str_matches=TRI_CODE)
    decision_snapshot_utc: UtcDatetime
    side: pl.String = pa.Field(isin=["home", "away"])
    price: pl.Float64 = pa.Field(gt=1)
    home_price: pl.Float64 = pa.Field(gt=1)
    away_price: pl.Float64 = pa.Field(gt=1)
    stake: pl.Float64 = pa.Field(gt=0)
    status: pl.String = pa.Field(isin=["settled", "void"])
    home_win: pl.Int8 = pa.Field(isin=[0, 1], nullable=True)
    won: pl.Boolean = pa.Field(nullable=True)
    profit: pl.Float64
    result_utc: UtcDatetime
    close_status: pl.String = pa.Field(isin=["proxy", "no pre-game snapshot", "stale", "missing"])
    close_snapshot_utc: UtcDatetime = pa.Field(nullable=True)
    close_home: pl.Float64 = pa.Field(gt=1, nullable=True)
    close_away: pl.Float64 = pa.Field(gt=1, nullable=True)
    p_close: pl.Float64 = pa.Field(gt=0, lt=1, nullable=True)
    clv: pl.Float64 = pa.Field(nullable=True)
    fair_move: pl.Float64 = pa.Field(nullable=True)
    policy_version: pl.String
    blend_version: pl.String
    settled_utc: UtcDatetime
    code_version: pl.String

    class Config(pa.DataFrameModel.Config):
        strict = True
        ordered = True
        unique: str | list[str] | None = ["game_date", "game_id"]  # noqa: RUF012 (pandera config)

    @pa.dataframe_check
    def a_close_only_from_a_proxy_after_the_decision(cls, data: pa.PolarsData) -> pl.LazyFrame:
        proxy = (pl.col("close_status") == "proxy") & (pl.col("status") == "settled")
        valued = pl.col("clv").is_not_null()
        after = pl.col("close_snapshot_utc") > pl.col("decision_snapshot_utc")
        return data.lazyframe.select((proxy == valued) & (~valued | after.fill_null(False)))

    @pa.dataframe_check
    def the_profit_follows_the_result(cls, data: pa.PolarsData) -> pl.LazyFrame:
        expected = (
            pl.when(pl.col("status") == "void")
            .then(0.0)
            .when(pl.col("won"))
            .then(pl.col("stake") * (pl.col("price") - 1))
            .otherwise(-pl.col("stake"))
        )
        return data.lazyframe.select((pl.col("profit") - expected).abs() < 1e-9)


class FeatureBuilds(pa.DataFrameModel):
    """One table a live feature build (nhl live features, #162) wrote for a game date's slate: the
    record a prediction checks before it reads the slate's feature rows (#170).

    A build deletes its date's record before it changes any table, and writes it once every step
    is done, so a record means the whole build finished. The table "slate" is the slate itself:
    its rows are every slate game, and its games those fetched before their as-of time, the only
    ones rated. Each other row gives a feature table's rows of the date under one
    artifact_version, and how many of the slate's games they cover (games; null for a table
    without game_id). A table can hold another component's rows, as lineups holds goalie_starts'
    goalies, but never two versions of one component: a build that left them is refused.
    build_id and code_version name the build and the commit it ran. slate_raw_key and
    slate_fetched_utc name the schedule response, and started_utc and finished_utc are the
    build's actual clock times, never a convention.
    """

    game_date: pl.Date
    season: pl.Int32
    table: pl.String
    artifact_version: pl.String
    rows: pl.Int64 = pa.Field(ge=0)
    games: pl.Int64 = pa.Field(ge=0, nullable=True)
    slate_games: pl.Int64 = pa.Field(ge=0)
    build_id: pl.String = pa.Field(str_matches=r"^live-features-\d{8}-")
    code_version: pl.String
    slate_raw_key: pl.String
    slate_fetched_utc: UtcDatetime
    started_utc: UtcDatetime
    finished_utc: UtcDatetime

    class Config(pa.DataFrameModel.Config):
        strict = True
        ordered = True
        unique: str | list[str] | None = [  # noqa: RUF012 (pandera config)
            "game_date",
            "table",
            "artifact_version",
        ]

    @pa.dataframe_check
    def finished_after_it_started(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(pl.col("finished_utc") >= pl.col("started_utc"))

    @pa.dataframe_check
    def covers_no_more_games_than_the_slate(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(
            pl.col("games").is_null() | (pl.col("games") <= pl.col("slate_games"))
        )


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


PLAYER_LEAGUE_SEASONS_KEY = ("player_id", "season", "league_abbrev", "game_type")
# The game types a season line is kept for: regular season (2) and playoffs (3).
SEASON_LINE_GAME_TYPES = (2, 3)
# A season's lines count as public on this day (month, day), at 00:00 UTC, after the season:
# nearly every league's season and the NHL playoffs are over by then (#98). The exceptions below
# come later.
SEASON_LINES_PUBLIC = (7, 1)
# Leagues whose season, as the NHL labels it, can end after July 1. Their lines count as public on
# October 1 instead, before any NHL season but 2026-27 starts, and none of them matters to an NHL
# player's prior then.
LATE_LEAGUES = (
    # The Australian league, played from April to September in a calendar year the label does not
    # give.
    "AIHL",
    "AUSTRALIA",
    # The World Cup of Hockey, played in August and September (2004's labelled 2003-04).
    "WCUP",
    # The Brick Invitational, a tournament for 10-year-olds played in early July and labelled with
    # the season before.
    "BRICK INVITATIONAL",
    # The Olympic qualification: the August 2025 final round is labelled 2024-25, while the August
    # 2021 one is labelled 2021-22.
    "OGQ",
    # JPL-Pro, a summer pro-am league whose label is not known.
    "JPL-PRO",
    # Small events whose dates or labels are not known, as a precaution: exhibitions, camps, an
    # early Olympic qualification and youth tournaments whose players' ages fit either a spring
    # event or a summer one labelled with the season before.
    "EXHIB.",
    "IIHF DEV. CAMP",
    "OLY-Q",
    "OGC-16",
    "QGC-16",
    "WCCC-16",
    "WSI U12",
    "WSI U13",
    "WSI U14",
    "WSI U15",
    "WSI U16",
)
LATE_LINES_PUBLIC = (10, 1)
# Seasons whose NHL playoffs ended after July 1: the 2020 bubble (Cup Final on September 28) and
# 2020-21 (July 7). None of their lines counts as public before the day given.
LATE_SEASONS = {
    20192020: datetime(2020, 10, 1, tzinfo=UTC),
    20202021: datetime(2021, 7, 9, tzinfo=UTC),
}
# One league's season that ended after July 1, by (season, league): the 2022 World Juniors,
# stopped in December 2021 and replayed in August 2022 under the 2021-22 label.
LATE_LEAGUE_SEASONS = {(20212022, "WJC-20"): datetime(2022, 10, 1, tzinfo=UTC)}


def season_lines_public_utc(season: pl.Expr, league: pl.Expr) -> pl.Expr:
    """When the lines of a season given as 20152016 count as public: July 1 of its second year,
    October 1 for a late league (LATE_LEAGUES), and never before the end of a late season
    (LATE_SEASONS) or a late league-season (LATE_LEAGUE_SEASONS)."""
    year = season % 10_000
    by_league = (
        pl.when(league.is_in(LATE_LEAGUES))
        .then(pl.datetime(year, LATE_LINES_PUBLIC[0], LATE_LINES_PUBLIC[1], time_zone="UTC"))
        .otherwise(
            pl.datetime(year, SEASON_LINES_PUBLIC[0], SEASON_LINES_PUBLIC[1], time_zone="UTC")
        )
    )
    season_end = season.replace_strict(
        LATE_SEASONS, default=None, return_dtype=pl.Datetime("us", "UTC")
    )
    league_season_end = [
        pl.when((season == late_season) & (league == late_league)).then(pl.lit(end))
        for (late_season, late_league), end in LATE_LEAGUE_SEASONS.items()
    ]
    return pl.max_horizontal(by_league, season_end, *league_season_end)


class PlayerLeagueSeasons(pa.DataFrameModel):
    """A player's season in one league and game type (#98), from the seasonTotals array of his
    landing page, the NHL's career stats in every league: the input of the NHLe offensive priors
    (#102).

    A player with several teams in a league-season has one line per team; they are summed, and
    teams counts them. league_abbrev is the page's leagueAbbrev as it is and part of the key.
    league is the same name trimmed and in upper case, with the known variants of one league
    mapped to one name (Sweden to SHL, Swiss and NLA to NL; LEAGUE_VARIANTS in
    ingest/player_seasons.py), so one league can sit under two keys in a player's season.
    game_type is 2 (regular season) or 3 (playoffs). games_played, goals and assists are null
    when a line summed lacks them, as most goalie lines lack goals and assists. age_at_season is
    the player's age in whole years on September 15 of the season's first year, the NHL draft
    cutoff, from players.birth_date.

    A season's lines are public on July 1 (00:00 UTC) after it; October 1 for the leagues that
    can end later or whose dates are not known (LATE_LEAGUES), such as the Australian league, the
    World Cup of Hockey and the Olympic qualification; and never before the end of the 2020 and
    2021 NHL playoffs, which ran past July 1 (LATE_SEASONS), or of the 2022 World Juniors, replayed
    in August 2022 (LATE_LEAGUE_SEASONS). The pages were fetched in 2026, so their fetch time would
    hide all history from the backtest. A line whose season was not yet public when the page was
    fetched is a partial season and is not kept. A page fetched long after a season may carry
    later corrections to its goals and assists, which ADR 0016 accepts.

    The table holds only players who reached the NHL, so that a player has rows is hindsight
    before his first NHL game. first_boxscore_utc is when his first boxscore in the lake became
    public (actual_lineups), and observed_utc is the later of it and his season's public date. So
    known_at shows a player's lines only once he has played, and a player without a boxscore has
    no rows. The lake starts in 2010-11, so a player who debuted earlier counts from his first
    game in it.
    """

    player_id: pl.Int64
    season: pl.Int32
    league_abbrev: pl.String = pa.Field(str_length={"min_value": 1})
    league: pl.String = pa.Field(str_length={"min_value": 1})
    game_type: pl.Int8 = pa.Field(isin=SEASON_LINE_GAME_TYPES)
    teams: pl.Int16 = pa.Field(ge=1)
    games_played: pl.Int16 = pa.Field(ge=0, nullable=True)
    goals: pl.Int16 = pa.Field(ge=0, nullable=True)
    assists: pl.Int16 = pa.Field(ge=0, nullable=True)
    age_at_season: pl.Int16 = pa.Field(ge=0, nullable=True)
    first_boxscore_utc: UtcDatetime
    observed_utc: UtcDatetime
    raw_key: pl.String

    class Config(pa.DataFrameModel.Config):
        strict = True
        ordered = True
        unique: str | list[str] | None = list(PLAYER_LEAGUE_SEASONS_KEY)  # noqa: RUF012 (pandera config)

    @pa.dataframe_check
    def season_spans_two_years(cls, data: pa.PolarsData) -> pl.LazyFrame:
        season = pl.col("season")
        return data.lazyframe.select(season % 10_000 == season // 10_000 + 1)

    @pa.dataframe_check
    def league_trimmed_upper_case(cls, data: pa.PolarsData) -> pl.LazyFrame:
        league = pl.col("league")
        return data.lazyframe.select(league == league.str.strip_chars().str.to_uppercase())

    @pa.dataframe_check
    def observed_when_the_season_is_over_and_he_has_played(
        cls, data: pa.PolarsData
    ) -> pl.LazyFrame:
        public = season_lines_public_utc(pl.col("season"), pl.col("league"))
        return data.lazyframe.select(
            pl.col("observed_utc") == pl.max_horizontal(public, pl.col("first_boxscore_utc"))
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


# The skaters who dress for a team-game: 18 in all but 20 of 18,711 team-games of 2010-11 to
# 2017-18 (ADR 0017).
DRESSED_SKATERS = 18
# The skaters a team dresses by role, and its first power-play unit (#100, ADR 0018).
SKATER_SLOTS = {"F": 12, "D": 6}
PP_UNIT = 5


class Lineups(pa.DataFrameModel):
    """A team's projected lineup before a game (#99, ADR 0017), one row per candidate: each player
    who dressed for the team in its last ten games public before observed_utc, through its line
    of team codes, and has not dressed for another team since. A team with no earlier game has
    no rows.

    A skater (role F or D, as listed in his latest game for the team) has p_available, the
    probability that he dresses. A team-game's skaters add up to the 18 who dress less the
    newcomers expected among them, so to at most 18. Their model was fitted on earlier seasons'
    boxscores public before the season's first as-of time; train_cutoff is the last of them, and
    artifact_version names the run. A goalie (role G) has p_start, copied with its train_cutoff
    and artifact_version from goalie_starts (ADR 0012), and a team-game's goalies add up to 1.

    A skater also has his expected minutes at 5v5, on the power play and on the penalty kill
    (exp_5v5, exp_pp, exp_pk; #100, ADR 0018): his probability of dressing times his decayed,
    pulled minutes, scaled so that a team-game's candidates of a role and its replacement skaters
    (LineupReplacements) add up to the league's minutes of the role the season before. pp_unit
    marks the five with the most expected power-play minutes. Goalies have none of these.
    train_cutoff covers both the availability model and the season-before figures of the minutes.

    observed_utc is the game's as-of time: 10:00 US Eastern on the game date, or an hour before
    the start if that is earlier, as for team strength."""

    game_id: pl.Int64
    season: pl.Int32
    game_date: pl.Date
    team: pl.String = pa.Field(str_matches=TRI_CODE)
    player_id: pl.Int64
    role: pl.String = pa.Field(isin=ROLES)
    p_available: pl.Float64 = pa.Field(gt=0, le=1, nullable=True)
    p_start: pl.Float64 = pa.Field(gt=0, le=1, nullable=True)
    exp_5v5: pl.Float64 = pa.Field(ge=0, nullable=True)
    exp_pp: pl.Float64 = pa.Field(ge=0, nullable=True)
    exp_pk: pl.Float64 = pa.Field(ge=0, nullable=True)
    pp_unit: pl.Boolean = pa.Field(nullable=True)
    train_cutoff: UtcDatetime
    artifact_version: pl.String = pa.Field(str_matches=r"^(lineup|goalie-start)-\d{8}-")
    observed_utc: UtcDatetime

    class Config(pa.DataFrameModel.Config):
        strict = True
        ordered = True
        unique: str | list[str] | None = ["game_id", "team", "player_id"]  # noqa: RUF012 (pandera config)

    @pa.dataframe_check
    def skaters_dress_and_goalies_start(cls, data: pa.PolarsData) -> pl.LazyFrame:
        goalie = pl.col("role") == "G"
        return data.lazyframe.select(
            (goalie == pl.col("p_available").is_null())
            & (goalie == pl.col("p_start").is_not_null())
            & (goalie == pl.col("artifact_version").str.starts_with("goalie-start-"))
        )

    @pa.dataframe_check
    def only_skaters_have_minutes(cls, data: pa.PolarsData) -> pl.LazyFrame:
        goalie = pl.col("role") == "G"
        columns = ("exp_5v5", "exp_pp", "exp_pk", "pp_unit")
        return data.lazyframe.select(
            pl.all_horizontal(goalie == pl.col(column).is_null() for column in columns)
        )

    @pa.dataframe_check
    def a_power_play_unit_of_at_most_five(cls, data: pa.PolarsData) -> pl.LazyFrame:
        unit = pl.col("pp_unit").fill_null(False).cast(pl.Int32).sum().over("game_id", "team")
        return data.lazyframe.select(unit <= PP_UNIT)

    @pa.dataframe_check
    def model_predates_the_game(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(pl.col("train_cutoff") < pl.col("observed_utc"))

    @pa.dataframe_check
    def one_as_of_time_per_team_game(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(pl.col("observed_utc").n_unique().over("game_id", "team") == 1)

    @pa.dataframe_check
    def skaters_add_up_to_at_most_those_who_dress(cls, data: pa.PolarsData) -> pl.LazyFrame:
        total = pl.col("p_available").sum().over("game_id", "team")
        return data.lazyframe.select(total <= DRESSED_SKATERS + 1e-9)

    @pa.dataframe_check
    def goalies_add_up_to_one(cls, data: pa.PolarsData) -> pl.LazyFrame:
        total = pl.col("p_start").sum().over("game_id", "team")
        return data.lazyframe.select((pl.col("role") != "G") | ((total - 1).abs() < 1e-9))


class LineupReplacements(pa.DataFrameModel):
    """A team's replacement skaters before a game (#100, ADR 0018), one row per role: the slots
    (SKATER_SLOTS) its candidates in Lineups leave short of, expected count, and their expected
    minutes in all at 5v5, on the power play and on the penalty kill. Each gets a newcomer's
    average minutes of the season before, one average for a team's first game of a season and one
    for its other games. A team-game's candidates of a role and its replacements add up to the
    league's skater-minutes of the role the season before.

    train_cutoff, artifact_version and observed_utc are those of the team-game's skaters in
    Lineups."""

    game_id: pl.Int64
    season: pl.Int32
    game_date: pl.Date
    team: pl.String = pa.Field(str_matches=TRI_CODE)
    role: pl.String = pa.Field(isin=list(SKATER_SLOTS))
    count: pl.Float64 = pa.Field(ge=0)
    exp_5v5: pl.Float64 = pa.Field(ge=0)
    exp_pp: pl.Float64 = pa.Field(ge=0)
    exp_pk: pl.Float64 = pa.Field(ge=0)
    train_cutoff: UtcDatetime
    artifact_version: pl.String = pa.Field(str_matches=r"^lineup-\d{8}-")
    observed_utc: UtcDatetime

    class Config(pa.DataFrameModel.Config):
        strict = True
        ordered = True
        unique: str | list[str] | None = ["game_id", "team", "role"]  # noqa: RUF012 (pandera config)

    @pa.dataframe_check
    def model_predates_the_game(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(pl.col("train_cutoff") < pl.col("observed_utc"))

    @pa.dataframe_check
    def at_most_the_role_s_slots(cls, data: pa.PolarsData) -> pl.LazyFrame:
        slots = pl.col("role").replace_strict(SKATER_SLOTS, return_dtype=pl.Float64)
        return data.lazyframe.select(pl.col("count") <= slots)


# RAPM's player components (#101, ADR 0019): 5v5 offense and defense, power-play offense and
# penalty-kill defense. Finishing and penalties (plan §4) come with their tasks.
RATING_COMPONENTS = ("ev_off", "ev_def", "pp", "pk")
RAPM_MODELS = ("ev", "pp")


class PlayerRatings(pa.DataFrameModel):
    """A candidate skater's RAPM ratings before a game (#101, ADR 0019), one row per component:
    his effect on xG per hour of ice time, offense raising his team's and defense lowering the
    other's (plan §5), from the stints public before as_of_utc, team strength's as-of time.
    known_utc is the latest stint time read, null before the first stints with xG. The candidates
    are those of Lineups.

    mean is relative to the average skater at 5v5 and on the penalty kill. On the power play it is
    relative to the average forward, a defenseman's adding his role's average there. sd is the
    ridge's posterior spread, the prior's for a player without data, and null before any data; a
    defenseman's power-play sd includes the role term's variance and covariance with his own.
    hours is the decayed ice time behind the rating, 0 without data. prior is the mean the rating
    is pulled toward (#102, ADR 0020): the player's traits times the season's effects, on the same
    footing as mean; a player without data is at it. half_life_days, pull_hours and aging are
    the settings (aging, the share of the age curve that moves past evidence, #103).

    train_cutoff is the last result the specification's figures read, as for team strength; and
    observed_utc, when the rating could be known, is the later of as_of_utc and train_cutoff."""

    game_id: pl.Int64
    season: pl.Int32
    game_date: pl.Date
    team: pl.String = pa.Field(str_matches=TRI_CODE)
    player_id: pl.Int64
    role: pl.String = pa.Field(isin=["F", "D"])
    component: pl.String = pa.Field(isin=list(RATING_COMPONENTS))
    mean: pl.Float64
    prior: pl.Float64
    sd: pl.Float64 = pa.Field(ge=0, nullable=True)
    hours: pl.Float64 = pa.Field(ge=0)
    known_utc: UtcDatetime = pa.Field(nullable=True)
    half_life_days: pl.Float64 = pa.Field(gt=0)
    pull_hours: pl.Float64 = pa.Field(gt=0)
    aging: pl.Float64 = pa.Field(ge=0)
    as_of_utc: UtcDatetime
    train_cutoff: UtcDatetime
    artifact_version: pl.String = pa.Field(str_matches=r"^rapm-\d{8}-")
    observed_utc: UtcDatetime

    class Config(pa.DataFrameModel.Config):
        strict = True
        ordered = True
        unique: str | list[str] | None = ["game_id", "player_id", "component"]  # noqa: RUF012 (pandera config)

    @pa.dataframe_check
    def regular_season_id_of_its_season(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(regular_season_id_of_its_season())

    @pa.dataframe_check
    def reads_only_stints_public_before_the_as_of_time(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(
            pl.col("known_utc").is_null() | (pl.col("known_utc") < pl.col("as_of_utc"))
        )

    @pa.dataframe_check
    def observed_at_the_as_of_time_or_the_cutoff(cls, data: pa.PolarsData) -> pl.LazyFrame:
        later = pl.max_horizontal("as_of_utc", "train_cutoff")
        return data.lazyframe.select(pl.col("observed_utc") == later)


class RapmTerms(pa.DataFrameModel):
    """The terms of each RAPM fit behind PlayerRatings (#101, ADR 0019), once per game date and
    fit: the intercept, the season, arena, home, score and zone terms that only remove bias, the
    power play's situation and defensemen terms, the priors' trait effects in force
    (prior:<component>:<trait>, fitted at the season's start, ADR 0020), and sigma, the residual
    sd per square-root hour.
    B3 reads its xG rates from the intercept and season terms, since ratings are relative to a
    skater rated 0. hours is the decayed ice time of the rows with the term, null for the
    defensemen term, a count, the trait effects and sigma.

    known_utc is the latest stint time the fit read, and as_of_utc the earliest as-of time of the
    date's games it serves; train_cutoff and observed_utc as in PlayerRatings."""

    season: pl.Int32
    game_date: pl.Date
    model: pl.String = pa.Field(isin=list(RAPM_MODELS))
    term: pl.String
    value: pl.Float64
    hours: pl.Float64 = pa.Field(ge=0, nullable=True)
    known_utc: UtcDatetime
    half_life_days: pl.Float64 = pa.Field(gt=0)
    pull_hours: pl.Float64 = pa.Field(gt=0)
    aging: pl.Float64 = pa.Field(ge=0)
    as_of_utc: UtcDatetime
    train_cutoff: UtcDatetime
    artifact_version: pl.String = pa.Field(str_matches=r"^rapm-\d{8}-")
    observed_utc: UtcDatetime

    class Config(pa.DataFrameModel.Config):
        strict = True
        ordered = True
        unique: str | list[str] | None = ["game_date", "known_utc", "model", "term"]  # noqa: RUF012 (pandera config)

    @pa.dataframe_check
    def read_before_the_as_of_time(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(pl.col("known_utc") < pl.col("as_of_utc"))

    @pa.dataframe_check
    def observed_at_the_as_of_time_or_the_cutoff(cls, data: pa.PolarsData) -> pl.LazyFrame:
        later = pl.max_horizontal("as_of_utc", "train_cutoff")
        return data.lazyframe.select(pl.col("observed_utc") == later)


# Penalty rates' components (#104, ADR 0021): penalties taken and drawn per hour.
PENALTY_COMPONENTS = ("pen_taken", "pen_drawn")


class PenaltyRates(pa.DataFrameModel):
    """A candidate skater's penalty rates before a game (#104, ADR 0021), one row per component:
    his unoffset penalties taken (pen_taken) or drawn (pen_drawn) per hour at 5v5, on the power
    play and on the penalty kill, from his earlier games with stints public before as_of_utc,
    team strength's as-of time. The candidates are those of Lineups, as in PlayerRatings.

    mean is pulled toward prior, his role's rate over all skaters of the role with the same
    weights, by pull_hours hours, measured per role and component on the season before
    (infinite when players' rates did not differ beyond noise, which puts everyone at prior). sd
    is the gamma posterior's spread, 0 at an infinite pull. hours is his decayed exposure, 0
    without games, and half_life_days RAPM's frozen memory (#103). known_utc is the latest
    player-game time read.

    train_cutoff is the later of RAPM's tuning cutoff, since the memory was tuned (ADR 0011), and
    the season's pulls' cutoff; observed_utc is the later of as_of_utc and train_cutoff."""

    game_id: pl.Int64
    season: pl.Int32
    game_date: pl.Date
    team: pl.String = pa.Field(str_matches=TRI_CODE)
    player_id: pl.Int64
    role: pl.String = pa.Field(isin=["F", "D"])
    component: pl.String = pa.Field(isin=list(PENALTY_COMPONENTS))
    mean: pl.Float64 = pa.Field(ge=0)
    prior: pl.Float64 = pa.Field(ge=0)
    sd: pl.Float64 = pa.Field(ge=0)
    hours: pl.Float64 = pa.Field(ge=0)
    known_utc: UtcDatetime
    half_life_days: pl.Float64 = pa.Field(gt=0)
    pull_hours: pl.Float64 = pa.Field(gt=0)
    as_of_utc: UtcDatetime
    train_cutoff: UtcDatetime
    artifact_version: pl.String = pa.Field(str_matches=r"^power-plays-\d{8}-")
    observed_utc: UtcDatetime

    class Config(pa.DataFrameModel.Config):
        strict = True
        ordered = True
        unique: str | list[str] | None = ["game_id", "player_id", "component"]  # noqa: RUF012 (pandera config)

    @pa.dataframe_check
    def regular_season_id_of_its_season(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(regular_season_id_of_its_season())

    @pa.dataframe_check
    def reads_only_games_public_before_the_as_of_time(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(pl.col("known_utc") < pl.col("as_of_utc"))

    @pa.dataframe_check
    def observed_at_the_as_of_time_or_the_cutoff(cls, data: pa.PolarsData) -> pl.LazyFrame:
        later = pl.max_horizontal("as_of_utc", "train_cutoff")
        return data.lazyframe.select(pl.col("observed_utc") == later)


class ExpectedPowerPlays(pa.DataFrameModel):
    """One team's expected power plays in a game (#104, ADR 0021), as of team strength's as-of
    time. taken_index is the opponent's projected lineup's penalties taken over a league-average
    lineup's for the same minutes, and drawn_index the team's own penalties drawn, likewise (1 is
    average). opportunities is league_opportunities (the league's unoffset penalties per
    team-game, the games public before as_of_utc in the season and the one before) times the
    average of the two indexes; pp_minutes is opportunities times pp_length, the league's
    power-play minutes per unoffset penalty; pk_minutes is the opponent's pp_minutes; and sh_xg
    is pk_minutes times sh_xg_per_pk_minute, the league's shorthanded xG per penalty-kill minute,
    null before any game with xG is public. known_utc is the latest player-game or team-game
    read.

    train_cutoff is the latest of RAPM's tuning cutoff, the season's pulls' and the train_cutoff
    of the game's lineups and replacements, whose expected minutes it reads; observed_utc is the
    later of as_of_utc and train_cutoff."""

    game_id: pl.Int64
    season: pl.Int32
    game_date: pl.Date
    team: pl.String = pa.Field(str_matches=TRI_CODE)
    opponent: pl.String = pa.Field(str_matches=TRI_CODE)
    is_home: pl.Boolean
    taken_index: pl.Float64 = pa.Field(gt=0)
    drawn_index: pl.Float64 = pa.Field(gt=0)
    opportunities: pl.Float64 = pa.Field(gt=0)
    pp_minutes: pl.Float64 = pa.Field(gt=0)
    pk_minutes: pl.Float64 = pa.Field(gt=0)
    sh_xg: pl.Float64 = pa.Field(ge=0, nullable=True)
    league_opportunities: pl.Float64 = pa.Field(gt=0)
    pp_length: pl.Float64 = pa.Field(gt=0)
    sh_xg_per_pk_minute: pl.Float64 = pa.Field(ge=0, nullable=True)
    known_utc: UtcDatetime
    as_of_utc: UtcDatetime
    train_cutoff: UtcDatetime
    artifact_version: pl.String = pa.Field(str_matches=r"^power-plays-\d{8}-")
    observed_utc: UtcDatetime

    class Config(pa.DataFrameModel.Config):
        strict = True
        ordered = True
        unique: str | list[str] | None = ["game_id", "team"]  # noqa: RUF012 (pandera config)

    @pa.dataframe_check
    def regular_season_id_of_its_season(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(regular_season_id_of_its_season())

    @pa.dataframe_check
    def reads_only_games_public_before_the_as_of_time(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(pl.col("known_utc") < pl.col("as_of_utc"))

    @pa.dataframe_check
    def observed_at_the_as_of_time_or_the_cutoff(cls, data: pa.PolarsData) -> pl.LazyFrame:
        later = pl.max_horizontal("as_of_utc", "train_cutoff")
        return data.lazyframe.select(pl.col("observed_utc") == later)


class Finishing(pa.DataFrameModel):
    """A candidate skater's finishing before a game (#105, ADR 0022): phi, his goals over the
    expected goals of his unblocked shots with xG, the league's finishing being 1, pulled toward
    1 by finishing_pull expected goals; phi_sd the gamma posterior's spread, 0 at an infinite
    pull (no earlier season to measure it on, or no spread beyond noise). goals and
    expected_goals are his decayed sums (xG times the league's finishing, league_finishing), and
    hours his decayed ice time at 5v5, on the power play and on the penalty kill. xg_rate is his
    xG per hour pulled toward his role's (xg_rate_prior) by rate_pull hours, null before any xG
    is public, and share his share of his team's expected xG by his expected minutes, which
    weighs his phi in the team's. half_life_days is RAPM's frozen memory (#103); known_utc the
    latest skater-game read, null before any.

    train_cutoff is the latest of RAPM's and the goalie effect's tuning cutoff (ADR 0011), the
    season's pulls' and the game's lineups', whose expected minutes the share reads; observed_utc
    the later of as_of_utc and train_cutoff."""

    game_id: pl.Int64
    season: pl.Int32
    game_date: pl.Date
    team: pl.String = pa.Field(str_matches=TRI_CODE)
    player_id: pl.Int64
    role: pl.String = pa.Field(isin=["F", "D"])
    phi: pl.Float64 = pa.Field(gt=0)
    phi_sd: pl.Float64 = pa.Field(ge=0)
    goals: pl.Float64 = pa.Field(ge=0)
    expected_goals: pl.Float64 = pa.Field(ge=0)
    xg_rate: pl.Float64 = pa.Field(ge=0, nullable=True)
    xg_rate_prior: pl.Float64 = pa.Field(ge=0, nullable=True)
    hours: pl.Float64 = pa.Field(ge=0)
    share: pl.Float64 = pa.Field(ge=0, le=1)
    finishing_pull: pl.Float64 = pa.Field(gt=0)
    rate_pull: pl.Float64 = pa.Field(gt=0)
    league_finishing: pl.Float64 = pa.Field(gt=0, nullable=True)
    known_utc: UtcDatetime = pa.Field(nullable=True)
    half_life_days: pl.Float64 = pa.Field(gt=0)
    as_of_utc: UtcDatetime
    train_cutoff: UtcDatetime
    artifact_version: pl.String = pa.Field(str_matches=r"^finishing-\d{8}-")
    observed_utc: UtcDatetime

    class Config(pa.DataFrameModel.Config):
        strict = True
        ordered = True
        unique: str | list[str] | None = ["game_id", "player_id"]  # noqa: RUF012 (pandera config)

    @pa.dataframe_check
    def regular_season_id_of_its_season(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(regular_season_id_of_its_season())

    @pa.dataframe_check
    def reads_only_games_public_before_the_as_of_time(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(
            pl.col("known_utc").is_null() | (pl.col("known_utc") < pl.col("as_of_utc"))
        )

    @pa.dataframe_check
    def observed_at_the_as_of_time_or_the_cutoff(cls, data: pa.PolarsData) -> pl.LazyFrame:
        later = pl.max_horizontal("as_of_utc", "train_cutoff")
        return data.lazyframe.select(pl.col("observed_utc") == later)


class GoalMultipliers(pa.DataFrameModel):
    """A team's goal multipliers in a game against one candidate goalie of the opponent (#105,
    ADR 0022): phi, its projected shooters' finishing weighted by their shares of its expected
    xG (Finishing), gamma, the goalie's conversion, 1 less his goals saved above expected per
    unblocked shot (goalie_effects) over the league's xG per such shot (xg_per_shot, null before
    any is public, when gamma is 1), and multiplier, their product times league_finishing, the
    league's goals over xG (1 before any is public), B3's factor on the team's xG with that
    goalie in net. An opponent without candidate goalies (a new team's first game) gets one row
    with goalie_id null and gamma 1, as B2 takes a missing goalie as average. known_utc is the
    latest game read.

    train_cutoff is the latest of Finishing's, the game's lineups' and the goalie effect's;
    observed_utc the later of as_of_utc and train_cutoff."""

    game_id: pl.Int64
    season: pl.Int32
    game_date: pl.Date
    team: pl.String = pa.Field(str_matches=TRI_CODE)
    opponent: pl.String = pa.Field(str_matches=TRI_CODE)
    goalie_id: pl.Int64 = pa.Field(nullable=True)
    phi: pl.Float64 = pa.Field(gt=0)
    gamma: pl.Float64 = pa.Field(gt=0)
    multiplier: pl.Float64 = pa.Field(gt=0)
    xg_per_shot: pl.Float64 = pa.Field(gt=0, nullable=True)
    league_finishing: pl.Float64 = pa.Field(gt=0, nullable=True)
    known_utc: UtcDatetime = pa.Field(nullable=True)
    as_of_utc: UtcDatetime
    train_cutoff: UtcDatetime
    artifact_version: pl.String = pa.Field(str_matches=r"^finishing-\d{8}-")
    observed_utc: UtcDatetime

    class Config(pa.DataFrameModel.Config):
        strict = True
        ordered = True
        unique: str | list[str] | None = ["game_id", "team", "goalie_id"]  # noqa: RUF012 (pandera config)

    @pa.dataframe_check
    def regular_season_id_of_its_season(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(regular_season_id_of_its_season())

    @pa.dataframe_check
    def reads_only_games_public_before_the_as_of_time(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(
            pl.col("known_utc").is_null() | (pl.col("known_utc") < pl.col("as_of_utc"))
        )

    @pa.dataframe_check
    def observed_at_the_as_of_time_or_the_cutoff(cls, data: pa.PolarsData) -> pl.LazyFrame:
        later = pl.max_horizontal("as_of_utc", "train_cutoff")
        return data.lazyframe.select(pl.col("observed_utc") == later)


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


DAILYFACEOFF_LINES_KEY = ("game_date", "team", "observed_utc", "dfo_player_id", "group")


class DailyFaceoffLines(pa.DataFrameModel):
    """One player in one group of a Daily Faceoff team line-combinations page (#121), fetched
    with the slot goalie polls for a team playing on game_date: the projected lines (F1-F4,
    D1-D3, G), the power-play and penalty-kill units, and the injured players (group "ir"), with
    Daily Faceoff's injury status ("out", "dtd"; null when none) and game-time-decision flag. A
    player appears once per group he is in.

    observed_utc is the fetch time, which is when a prediction may use the row: the lines and
    statuses change, so lines_updated_utc (when Daily Faceoff last updated the page's lines, with
    lines_source naming its source) and news_utc (the player's latest news item) only show how
    old the information was. Players are kept by name and Daily Faceoff id; matching them to NHL
    player ids is left to the measurement (#121), which has the players. Logged for live games
    only: no source dates past statuses, so nothing here enters a backtest (hard rule 1).
    """

    season: pl.Int32
    game_date: pl.Date
    team: pl.String = pa.Field(str_matches=TRI_CODE)
    lines_updated_utc: UtcDatetime = pa.Field(nullable=True)
    lines_source: pl.String = pa.Field(nullable=True)
    dfo_player_id: pl.Int64
    player_name: pl.String
    jersey: pl.Int16 = pa.Field(nullable=True)
    position: pl.String = pa.Field(nullable=True)
    group: pl.String
    category: pl.String = pa.Field(nullable=True)
    injury_status: pl.String = pa.Field(nullable=True)
    game_time_decision: pl.Boolean
    news_utc: UtcDatetime = pa.Field(nullable=True)
    observed_utc: UtcDatetime
    raw_key: pl.String

    class Config(pa.DataFrameModel.Config):
        strict = True
        ordered = True
        unique: str | list[str] | None = list(DAILYFACEOFF_LINES_KEY)  # noqa: RUF012 (pandera config)

    @pa.dataframe_check
    def updated_not_after_observed(cls, data: pa.PolarsData) -> pl.LazyFrame:
        observed = pl.col("observed_utc") + CLOCK_SKEW
        return data.lazyframe.select(
            (pl.col("lines_updated_utc").is_null() | (pl.col("lines_updated_utc") <= observed))
            & (pl.col("news_utc").is_null() | (pl.col("news_utc") <= observed))
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
