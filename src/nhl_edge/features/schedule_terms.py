"""Schedule terms ΔR and the season home term h_s (#77, docs/plan.md §5, ADR 0011), before each
game from earlier games only.

**Per team:**
- **rest:** days since its last game this season, capped at REST_CAP (a season's first game takes
  the cap), and whether that was yesterday (a back-to-back);
- **travel:** great-circle km from the arena of its last game this season to tonight's, or from
  its home arena (`home_arenas.csv`) for a season's first game;
- **time-zone change:** tonight's arena's UTC offset minus the previous arena's, both at tonight's
  start, in hours: positive when the team went east.

**Per game:** the neutral-site flag, and the share of the arena's seats open to spectators
(`reference.capacity_share`, as announced by the as-of time).

**h_s,** the season's home edge, as log-odds: the home team's full-game win rate (overtime and
shootout included, hard rule 2) in the season's non-neutral games public before the as-of time,
pulled toward the rate of the three seasons before it with a weight worth `prior_games` games.
That weight is tuned on the training seasons and frozen (ADR 0011).

A game is rated as of the time team strength uses: 10:00 US Eastern on its date, or an hour before
its start if that is earlier. A team's last game counts only once its result is public (ADR 0003),
as `schedule_known_at` reads the schedule (ADR 0005): the schedule holds only games that went on
to be played, so a game not yet played could reveal a postponement. Tonight's own row is read for
its venue and flags, public a day before it starts.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import polars as pl

from nhl_edge.backtest.seasons import TRAINING_SEASONS
from nhl_edge.features import team_strength as ts
from nhl_edge.ingest.games import result_public
from nhl_edge.lake.schemas import ScheduleTerms, dtypes
from nhl_edge.reference import Reference, capacity_share

COMPONENT = "schedule-terms"
# The first season with an earlier one to pull the home edge toward.
FIRST_SEASON = 20112012
TUNING_SEASONS = tuple(season for season in TRAINING_SEASONS if season > FIRST_SEASON)
REST_CAP = 4
PRIOR_SEASONS = 3
EARTH_RADIUS_KM = 6371.0


@dataclass(frozen=True)
class Settings:
    """The home edge's one tuned setting (ADR 0011)."""

    prior_games: float

    @property
    def label(self) -> str:
        return f"prior {self.prior_games:g} games"


def _arenas(ref: Reference) -> pl.DataFrame:
    return ref.arenas.select("arena_id", "latitude", "longitude", "tz")


def game_arenas(schedule: pl.DataFrame, ref: Reference) -> pl.DataFrame:
    """Each scheduled game's arena: its venue's building, with coordinates and time zone."""
    return (
        schedule.select("game_id", "venue")
        .join(ref.venues, on="venue", how="left")
        .join(_arenas(ref), on="arena_id", how="left")
        .drop("venue")
    )


def home_arena(ref: Reference) -> pl.DataFrame:
    """Each team's primary home arena by season, as (team, first_season, last_season, arena)."""
    return (
        ref.home_arenas.filter("primary")
        .join(_arenas(ref), on="arena_id")
        .select("team", "first_season", "last_season", "arena_id", "latitude", "longitude", "tz")
    )


def team_rows(schedule: pl.DataFrame, ref: Reference) -> pl.DataFrame:
    """Each game twice, once per team: the team, its side, the game's arena, the as-of time and
    when its result became public."""
    arenas = game_arenas(schedule, ref)
    base = schedule.select(
        "game_id",
        "season",
        "game_date",
        "start_utc",
        as_of_utc=ts.as_of(pl.col("game_date"), pl.col("start_utc")),
        result_utc=result_public(pl.col("game_date")),
    ).join(arenas, on="game_id")
    return pl.concat(
        [
            base.join(schedule.select("game_id", team=pl.col(side)), on="game_id").with_columns(
                side=pl.lit(side)
            )
            for side in ("home", "away")
        ]
    ).sort("team", "game_date", "game_id")


def _offset_hours(tz: str, moment: datetime) -> float:
    offset = moment.astimezone(ZoneInfo(tz)).utcoffset()
    assert offset is not None
    return offset.total_seconds() / 3600


def _km(lat1: np.ndarray, lon1: np.ndarray, lat2: np.ndarray, lon2: np.ndarray) -> np.ndarray:
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dp, dl = p2 - p1, np.radians(lon2 - lon1)
    a = np.sin(dp / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


def travel(rows: pl.DataFrame, targets: pl.DataFrame, ref: Reference) -> pl.DataFrame:
    """Each target (game_id, team) with rest_days, back_to_back, travel_km and tz_shift, from the
    team's last game this season whose result was public before the target's as-of time."""
    want = targets.select("game_id", "team").join(rows, on=["game_id", "team"])
    previous = rows.select(
        "team",
        "season",
        prev_date="game_date",
        prev_result_utc="result_utc",
        prev_lat="latitude",
        prev_lon="longitude",
        prev_tz="tz",
    ).sort("prev_result_utc")
    # The team's latest game this season public before the as-of time. Both sides are sorted on
    # their keys, so every team-season group is too, which Polars cannot check with by groups.
    joined = (
        want.sort("as_of_utc")
        .join_asof(
            previous,
            left_on="as_of_utc",
            right_on="prev_result_utc",
            by=["team", "season"],
            strategy="backward",
            check_sortedness=False,
            allow_exact_matches=False,
        )
        .join(
            home_arena(ref),
            on="team",
            how="left",
        )
        .filter(
            pl.col("first_season") <= pl.col("season"),
            pl.col("last_season").is_null() | (pl.col("last_season") >= pl.col("season")),
        )
    )
    first = pl.col("prev_date").is_null()
    origin = joined.with_columns(
        from_lat=pl.when(first).then("latitude_right").otherwise("prev_lat"),
        from_lon=pl.when(first).then("longitude_right").otherwise("prev_lon"),
        from_tz=pl.when(first).then("tz_right").otherwise("prev_tz"),
        rest_days=pl.when(first)
        .then(REST_CAP)
        .otherwise(
            pl.min_horizontal((pl.col("game_date") - pl.col("prev_date")).dt.total_days(), REST_CAP)
        )
        .cast(pl.Int8),
    )
    km = _km(
        origin["from_lat"].to_numpy(),
        origin["from_lon"].to_numpy(),
        origin["latitude"].to_numpy(),
        origin["longitude"].to_numpy(),
    )
    shifts = [
        _offset_hours(tz, start) - _offset_hours(from_tz, start)
        for tz, from_tz, start in origin.select("tz", "from_tz", "start_utc").iter_rows()
    ]
    return origin.select(
        "game_id",
        "team",
        "rest_days",
        back_to_back=pl.col("rest_days") == 1,
        travel_km=pl.Series(km),
        tz_shift=pl.Series(shifts, dtype=pl.Float64),
    )


def home_edge(targets: pl.DataFrame, games: pl.DataFrame, settings: Settings) -> pl.DataFrame:
    """Each target (game_id, season, as_of_utc) with h_s, the shrunk home win rate it came from,
    and the season's games it read (season_games)."""
    results = games.filter(~pl.col("neutral_site")).select(
        "season",
        "observed_utc",
        home_win=(pl.col("home_score") > pl.col("away_score")).cast(pl.Float64),
    )
    by_season = results.group_by("season").agg(wins=pl.col("home_win").sum(), games=pl.len())
    out = []
    for (season,), rows in targets.group_by("season"):
        earlier = by_season.filter(
            pl.col("season").is_between(season - PRIOR_SEASONS * 10001, season - 1)
        )
        prior_games = float(earlier["games"].sum())
        prior_rate = float(earlier["wins"].sum()) / prior_games if prior_games else np.nan
        past = results.filter(pl.col("season") == season).sort("observed_utc")
        wins = np.concatenate([[0.0], past["home_win"].to_numpy().cumsum()])
        seen = np.searchsorted(
            past["observed_utc"].to_numpy(), rows["as_of_utc"].to_numpy(), side="left"
        )
        m = settings.prior_games
        rate = (wins[seen] + m * prior_rate) / (seen + m)
        out.append(
            rows.with_columns(
                home_win_rate=pl.Series(rate),
                season_games=pl.Series(seen, dtype=pl.Int32),
            )
        )
    frame = pl.concat(out)
    return frame.with_columns(h_s=(pl.col("home_win_rate") / (1 - pl.col("home_win_rate"))).log())


def capacity(schedule: pl.DataFrame, targets: pl.DataFrame, ref: Reference) -> pl.DataFrame:
    """Each target's open-seat share as announced by its as-of time."""
    out = []
    for (moment,), rows in targets.group_by("as_of_utc"):
        assert isinstance(moment, datetime)
        tonight = schedule.filter(pl.col("game_id").is_in(rows["game_id"].implode()))
        share = capacity_share(tonight, moment, rows["game_id"].to_list(), ref)
        out.append(share.filter(pl.col("game_id").is_in(rows["game_id"].implode())))
    return pl.concat(out)


def terms(
    schedule: pl.DataFrame,
    games: pl.DataFrame,
    settings: Settings,
    seasons: list[int],
    ref: Reference | None = None,
) -> pl.DataFrame:
    """Every scheduled game of the seasons with its schedule terms and home edge. schedule and
    games hold the seasons and the PRIOR_SEASONS before them."""
    ref = ref or Reference.load()
    rows = team_rows(schedule, ref)
    targets = schedule.filter(pl.col("season").is_in(seasons)).select(
        "game_id",
        "season",
        "game_date",
        "home",
        "away",
        "neutral_site",
        as_of_utc=ts.as_of(pl.col("game_date"), pl.col("start_utc")),
    )
    sides = pl.concat([targets.select("game_id", team=pl.col(side)) for side in ("home", "away")])
    moved = travel(rows, sides, ref)
    home = moved.rename({c: f"home_{c}" for c in moved.columns if c not in ("game_id", "team")})
    away = moved.rename({c: f"away_{c}" for c in moved.columns if c not in ("game_id", "team")})
    edge = home_edge(targets.select("game_id", "season", "as_of_utc"), games, settings)
    seats = capacity(schedule, targets.select("game_id", "as_of_utc"), ref)
    return (
        targets.join(home.rename({"team": "home"}), on=["game_id", "home"], how="left")
        .join(away.rename({"team": "away"}), on=["game_id", "away"], how="left")
        .join(edge.drop("season", "as_of_utc"), on="game_id")
        .join(seats, on="game_id")
        .sort("game_id")
    )


# The tuning grid for the home edge's pull toward the three seasons before, in games, and the
# order that breaks ties toward the steadier, stronger pull (ADR 0011). A season has about 1,300
# games.
GRID = tuple(Settings(m) for m in (50, 100, 200, 400, 800, 1600))


def steadiness(settings: Settings) -> float:
    """More pull toward the earlier seasons."""
    return settings.prior_games


def tuning_feature(rated: pl.DataFrame) -> pl.DataFrame:
    """The home edge as the tuning's feature (game_id, x): 0 at a neutral site."""
    return rated.select(
        "game_id", x=pl.when(pl.col("neutral_site")).then(0.0).otherwise(pl.col("h_s"))
    )


# Frozen by run schedule-terms-20261001-89564c5 on #77 (ADR 0011). All six pulls tied, and the
# leader was also the steadiest, on the grid's edge. The owner chose to freeze it and keep the
# grid.
TUNED = Settings(prior_games=1600)
# The last result that run reads: 2017-18's final night, as for team strength.
TUNED_CUTOFF = ts.TUNED_CUTOFF


def rows(
    rated: pl.DataFrame,
    settings: Settings,
    artifact_version: str,
    train_cutoff: datetime = TUNED_CUTOFF,
) -> pl.DataFrame:
    """The games' ScheduleTerms rows, each with the cutoff of the tuning run that chose the
    setting: a row is observed at its as-of time, or at the cutoff if that is later."""
    frame = rated.with_columns(
        prior_games=pl.lit(float(settings.prior_games)),
        train_cutoff=pl.lit(train_cutoff),
        artifact_version=pl.lit(artifact_version),
        observed_utc=pl.max_horizontal("as_of_utc", pl.lit(train_cutoff)),
    )
    columns = dtypes(ScheduleTerms)
    return ScheduleTerms.validate(frame.select(list(columns)).cast(columns))  # type: ignore[arg-type]


def input_problems(
    schedule: pl.DataFrame,
    games: pl.DataFrame,
    last: int,
    expected: Mapping[int, int],
    ref: Reference | None = None,
) -> list[str]:
    """Why the lake cannot rate games up to the season last: a season short of its games, a game
    whose venue has no arena, or a team-season without a primary home arena."""
    ref = ref or Reference.load()
    problems = []
    for season in sorted(s for s in expected if s <= last):
        for label, table in (("games", games), ("schedule", schedule)):
            count = table.filter(pl.col("season") == season).height
            if count != expected[season]:
                problems.append(f"{season}: {count:,} of {expected[season]:,} in {label}")
    # The seasons rated: the earlier ones feed only the home edge's prior.
    needed = schedule.filter(pl.col("season").is_between(FIRST_SEASON, last))
    no_arena = game_arenas(needed, ref).filter(pl.col("latitude").is_null())
    if no_arena.height:
        examples = ", ".join(str(g) for g in no_arena["game_id"].head(3).to_list())
        problems.append(f"{no_arena.height:,} games without an arena, e.g. {examples}")
    team_seasons = pl.concat(
        [needed.select("season", team=pl.col(side)) for side in ("home", "away")]
    ).unique()
    homes = team_seasons.join(home_arena(ref), on="team", how="left").filter(
        pl.col("first_season") <= pl.col("season"),
        pl.col("last_season").is_null() | (pl.col("last_season") >= pl.col("season")),
    )
    missing = team_seasons.join(homes, on=["team", "season"], how="anti")
    for season, team in missing.sort("season", "team").select("season", "team").rows():
        problems.append(f"{season}: {team} has no primary home arena")
    return sorted(problems)
