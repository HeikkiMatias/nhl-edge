"""One pandera schema per table. Every write validates against its schema."""

from datetime import timedelta
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


DECIDED_IN = ("REG", "OT", "SO")
# No result counts as public sooner after its scheduled start (ADR 0003).
MIN_RESULT_LAG = timedelta(hours=6)
TRI_CODE = r"^[A-Z]{3}$"


class Games(pa.DataFrameModel):
    """One final regular-season game (NHL gameState OFF).

    Scores are full-game: a shootout adds one goal for its winner, so home_score > away_score
    settles the moneyline, OT and shootout included. decided_in is the period type that ended the
    game. limited_attendance marks the 2020-21 season, played without fans or with capped crowds.

    observed_utc is when the result (home_score, away_score, decided_in) counts as public:
    10:00 UTC the morning after game_date, a conservative bound because the API has no end time
    (ADR 0003). The schema also requires it to be at least six hours after start_utc.
    The schedule columns (teams, start, venue, neutral_site) were public long before the game,
    but share the row's observed_utc until schedule and results are split (#24).
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
        # 2023020001: season start year, game type 02 (regular season), game number
        game_id = pl.col("game_id")
        return data.lazyframe.select(
            (game_id // 1_000_000 == pl.col("season") // 10_000)
            & ((game_id // 10_000) % 100 == 2)
            & (pl.col("season") % 10_000 == pl.col("season") // 10_000 + 1)
        )

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


class Shots(pa.DataFrameModel):
    """One unblocked shot attempt (shot on goal, missed shot or goal) in play-by-play, periods 1
    to 4. Shootout attempts are not shots and are left out; penalty shots are kept and flagged.

    seconds is elapsed game time, (period - 1) * 1200 plus the period clock. team is the shooting
    team. x and y are rink feet, turned so the shooting team attacks the net at x = +89: the API
    gives raw rink coordinates, and the attack direction per team and period is inferred from the
    offensive-zone shots. Strength is the shooting team's view of situationCode: skaters_for and
    skaters_against (6 means that team's goalie is pulled), and is_empty_net when the defending
    goalie is off the ice. They are null for the few shots whose situationCode is missing.

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
    observed_utc: UtcDatetime
    raw_key: pl.String

    class Config(pa.DataFrameModel.Config):
        strict = True
        ordered = True
        unique: str | list[str] | None = ["game_id", "event_id"]  # noqa: RUF012 (pandera config)

    @pa.dataframe_check
    def goal_flag_matches_event(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(pl.col("is_goal") == (pl.col("event_type") == "goal"))

    @pa.dataframe_check
    def seconds_inside_the_period(cls, data: pa.PolarsData) -> pl.LazyFrame:
        seconds, period = pl.col("seconds"), pl.col("period")
        return data.lazyframe.select(
            (seconds >= period_start_s(period)) & (seconds <= period_end_s(period))
        )


class Shifts(pa.DataFrameModel):
    """One player shift from the shift chart (type 517 rows), periods 1 to 4.

    start_s and end_s are elapsed game seconds, like Shots.seconds. A player is on the ice for an
    event at second t when start_s < t <= end_s. team comes from the shift's teamId, mapped
    through the game's two teams. The parser drops zero-length placeholders, rows of teams not in
    the game, shootout rows and malformed rows, and ShiftCoverage counts them.

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
    ice. complete means no bad rows and every dressed player's shifts add up.

    Strength: at every unblocked shot except penalty shots (shots_checked), the skaters and
    goalies on the ice from the shifts are compared with situationCode. skater_mismatches and
    goalie_mismatches count the shots where they differ. RAPM drops stints that contradict it.

    observed_utc is 10:00 UTC the morning after game_date, as for Shots; raw_key is the shift
    chart's.
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
