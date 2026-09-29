"""Reference files: team codes, arenas, venue names, home arenas, coach tenures and attendance
limits.

Hand-compiled CSVs next to this module, from the sources docs/data-sources.md lists. They cover
the lake's seasons, 2010-11 on, and `nhl audit reference` checks them against every game in
`games`: each game's teams, venue, home arena and coaches must map to exactly one row on its date.

Team codes, arenas, venues and home arenas are known seasons ahead, so they carry no observed_utc.
A feature learns a game's venue from `schedule`, public a day before the game (ADR 0005). A
coach's stint is different: its end is future information while it runs, so features read
tenures through coaches_known_at. An attendance limit counts from the day after it was announced,
so features read capacity shares through capacity_share.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandera.polars as pa
import polars as pl

from nhl_edge.ingest.games import EXPECTED_GAMES, result_public
from nhl_edge.lake.schemas import (
    Arenas,
    AttendanceLimits,
    CoachTenures,
    HomeArenas,
    Teams,
    Venues,
    dtypes,
)

REFERENCE_DIR = Path(__file__).parent
# An open-ended last_season, for range joins.
OPEN_SEASON = 99_999_999
# What coaches_known_at returns.
KNOWN_COACH_COLUMNS = ("team", "first_game", "last_game", "coach")


def _load(name: str, schema: type[pa.DataFrameModel]) -> pl.DataFrame:
    return schema.validate(pl.read_csv(REFERENCE_DIR / f"{name}.csv", schema=dtypes(schema)))


def load_teams() -> pl.DataFrame:
    return _load("teams", Teams)


def load_arenas() -> pl.DataFrame:
    return _load("arenas", Arenas)


def load_venues() -> pl.DataFrame:
    return _load("venues", Venues)


def load_home_arenas() -> pl.DataFrame:
    return _load("home_arenas", HomeArenas)


def load_coaches() -> pl.DataFrame:
    return _load("coaches", CoachTenures)


def load_attendance_limits() -> pl.DataFrame:
    return _load("attendance_limits", AttendanceLimits)


@dataclass(frozen=True)
class Reference:
    teams: pl.DataFrame
    arenas: pl.DataFrame
    venues: pl.DataFrame
    home_arenas: pl.DataFrame
    coaches: pl.DataFrame
    attendance_limits: pl.DataFrame

    @classmethod
    def load(cls) -> "Reference":
        return cls(
            load_teams(),
            load_arenas(),
            load_venues(),
            load_home_arenas(),
            load_coaches(),
            load_attendance_limits(),
        )


def lineage(teams: pl.DataFrame) -> dict[str, str]:
    """Each code mapped to the first code of its line of predecessors, so the codes of one team
    share a value: ATL and WPG map to ATL, and PHX, ARI and UTA to PHX."""
    predecessor = dict(zip(teams["team"], teams["predecessor"], strict=True))

    def first(team: str) -> str:
        seen = {team}
        while predecessor.get(team) is not None:
            team = predecessor[team]
            if team in seen:
                raise ValueError(f"teams.csv: predecessors of {team} form a cycle")
            seen.add(team)
        return team

    return {team: first(team) for team in predecessor}


def coaches_known_at(
    coaches: pl.DataFrame, teams: pl.DataFrame, prediction_utc: datetime
) -> pl.DataFrame:
    """The coach tenures a prediction at prediction_utc may use.

    Who coached a game shows in its feeds, public at 10:00 UTC the morning after (ADR 0003 and
    0004). So a stint counts as known from the morning after its first game, and its last_game
    only once the team's next stint is known; until then last_game is null, as for a stint still
    running. A new coach therefore counts from the morning after his first game, which is
    conservative: most changes are announced a day or more ahead. The note stays out: it was
    written with hindsight, such as how many games of a shared bench the NHL credits to whom.
    """
    line = pl.col("team").replace_strict(lineage(teams))
    next_first = pl.col("first_game").shift(-1).over(line, order_by="first_game")
    successor_known = (result_public(next_first) < prediction_utc).fill_null(False)
    return (
        coaches.with_columns(last_game=pl.when(successor_known).then("last_game"))
        .filter(result_public(pl.col("first_game")) < prediction_utc)
        .select(KNOWN_COACH_COLUMNS)
        .sort("team", "first_game")
    )


def _public_by(announced: pl.Expr, prediction_utc: datetime) -> pl.Expr:
    """Whether a source dated announced was public by prediction_utc: from 10:00 UTC the next
    day, the rule ADR 0003 sets for results. A null date means known all along."""
    return announced.is_null() | (result_public(announced) < prediction_utc)


def capacity_share(
    games: pl.DataFrame, prediction_utc: datetime, ref: Reference | None = None
) -> pl.DataFrame:
    """game_id and the share of its arena's seats open to spectators, as known at prediction_utc.

    games needs game_id, game_date and venue (Schedule's columns). A game takes the latest limit
    at its arena that started by its date and was announced by the prediction (10:00 UTC the day
    after its source). That limit applies while it runs, and after its last day too while what
    ended it is not yet known: the next limit's announcement, or the lift's (ended_announced). So
    a game-day prediction reads Nashville's first home game with fans as empty, since the change
    was reported the next day, and Montreal's game closed on the day itself as full. A game with
    no known limit takes 1.
    """
    ref = ref or Reference.load()
    limits = (
        ref.attendance_limits.sort("arena_id", "first_date")
        .with_columns(
            known=_public_by(pl.col("announced"), prediction_utc),
            lift_known=_public_by(pl.col("ended_announced"), prediction_utc),
            replaced=pl.col("first_date").shift(-1).over("arena_id")
            == pl.col("last_date") + pl.duration(days=1),
        )
        .with_columns(successor_known=pl.col("known").shift(-1).over("arena_id").fill_null(False))
    )
    latest = (
        games.select("game_id", "game_date", "venue")
        .join(ref.venues, on="venue")
        .join(limits.filter("known"), on="arena_id")
        .filter(pl.col("first_date") <= pl.col("game_date"))
        .sort("first_date")
        .group_by("game_id")
        .last()
    )
    ended_unknown = pl.when(pl.col("replaced").fill_null(False)).then(~pl.col("successor_known"))
    applies = (pl.col("game_date") <= pl.col("last_date")) | ended_unknown.otherwise(
        ~pl.col("lift_known")
    )
    return (
        games.select("game_id")
        .join(latest.filter(applies).select("game_id", "capacity_share"), on="game_id", how="left")
        .with_columns(pl.col("capacity_share").fill_null(1.0))
    )


def team_games(games: pl.DataFrame) -> pl.DataFrame:
    """One row per team per game: game_id, season, game_date, team and is_home."""
    columns = ["game_id", "season", "game_date"]
    return pl.concat(
        [
            games.select(*columns, team="home", is_home=pl.lit(True)),
            games.select(*columns, team="away", is_home=pl.lit(False)),
        ]
    )


def finished_seasons(games: pl.DataFrame) -> list[int]:
    """The seasons games holds in full: as many games as the season had (EXPECTED_GAMES)."""
    counts = games.group_by("season").len().sort("season")
    return [season for season, count in counts.iter_rows() if EXPECTED_GAMES.get(season) == count]


def check_files(ref: Reference) -> list[str]:
    """Problems within the reference files themselves, one line each: rows that point at missing
    rows, a code change that leaves a gap, and coach stints of one team that overlap."""
    return list(_file_problems(ref))


def check_games(games: pl.DataFrame, ref: Reference | None = None) -> list[str]:
    """Problems mapping games (columns of Games or Schedule) to the reference files, one line
    each, after those in the files themselves. Every game's two codes must be in use in its
    season, its venue must name an arena, a non-neutral game must be at one of the home team's
    home arenas that season, and one coach stint must cover each team on each game date."""
    ref = ref or Reference.load()
    return [
        *check_files(ref),
        *_team_code_problems(games, ref),
        *_venue_problems(games, ref),
        *_home_arena_problems(games, ref),
        *_coach_problems(games, ref),
        *_attendance_problems(games, ref),
    ]


def _missing(frame: pl.DataFrame, column: str, known: pl.Series) -> list[str]:
    return sorted(set(frame[column].drop_nulls()) - set(known))


def _file_problems(ref: Reference) -> Iterator[str]:
    arena_ids, codes = ref.arenas["arena_id"], ref.teams["team"]
    for arena in _missing(ref.venues, "arena_id", arena_ids):
        yield f"venues.csv: arena {arena} is not in arenas.csv"
    for arena in sorted(set(arena_ids) - set(ref.venues["arena_id"])):
        yield f"arenas.csv: no venue name maps to {arena}"
    for arena in _missing(ref.home_arenas, "arena_id", arena_ids):
        yield f"home_arenas.csv: arena {arena} is not in arenas.csv"
    for name, frame, column in (
        ("home_arenas", ref.home_arenas, "team"),
        ("coaches", ref.coaches, "team"),
        ("teams", ref.teams, "predecessor"),
    ):
        for team in _missing(frame, column, codes):
            yield f"{name}.csv: team {team} is not in teams.csv"
    seasons = dict(zip(codes, ref.teams["last_season"], strict=True))
    for team, first, predecessor in (
        ref.teams.filter(pl.col("predecessor").is_not_null())
        .select("team", "first_season", "predecessor")
        .iter_rows()
    ):
        last = seasons.get(predecessor)
        if last is None or last + 10_001 != first:
            yield f"teams.csv: {team} starts in {first}, not the season after {predecessor} ends"
    for arena in _missing(ref.attendance_limits, "arena_id", arena_ids):
        yield f"attendance_limits.csv: arena {arena} is not in arenas.csv"
    overlapping = (
        ref.attendance_limits.sort("arena_id", "first_date")
        .with_columns(next_first=pl.col("first_date").shift(-1).over("arena_id"))
        .filter(pl.col("last_date") >= pl.col("next_first"))
    )
    for arena, first, next_first in overlapping.select(
        "arena_id", "first_date", "next_first"
    ).iter_rows():
        yield (
            f"attendance_limits.csv: the {arena} limit from {first} overlaps the one from "
            f"{next_first}"
        )
    try:
        line = lineage(ref.teams)
    except ValueError as exc:
        yield str(exc)
        return
    stints = ref.coaches.filter(pl.col("team").is_in(list(line))).with_columns(
        line=pl.col("team").replace_strict(line)
    )
    overlaps = stints.sort("line", "first_game").with_columns(
        next_first=pl.col("first_game").shift(-1).over("line"),
        next_team=pl.col("team").shift(-1).over("line"),
    )
    for team, first, last, next_team, next_first in (
        overlaps.filter(
            pl.col("next_first").is_not_null()
            & (pl.col("last_game").is_null() | (pl.col("last_game") >= pl.col("next_first")))
        )
        .select("team", "first_game", "last_game", "next_team", "next_first")
        .iter_rows()
    ):
        yield (
            f"coaches.csv: the {team} stint from {first} (to {last or 'now'}) overlaps the "
            f"{next_team} stint from {next_first}"
        )


def _examples(game_ids: pl.Series) -> str:
    return f"{game_ids.len():,} games, e.g. {game_ids.min()}"


def _team_code_problems(games: pl.DataFrame, ref: Reference) -> Iterator[str]:
    played = team_games(games).join(ref.teams, on="team", how="left")
    outside = played.filter(
        pl.col("first_season").is_null()
        | (pl.col("season") < pl.col("first_season"))
        | (pl.col("season") > pl.col("last_season").fill_null(OPEN_SEASON))
    )
    for (team, season), rows in sorted(outside.group_by("team", "season")):
        yield (
            f"{team} plays in {season} ({_examples(rows['game_id'])}), outside its seasons in "
            "teams.csv"
        )
    # In a finished season, every code in use must have played.
    for season in finished_seasons(games):
        active = ref.teams.filter(
            (pl.col("first_season") <= season)
            & (pl.col("last_season").fill_null(OPEN_SEASON) >= season)
        )["team"]
        for team in sorted(set(active) - set(played.filter(pl.col("season") == season)["team"])):
            yield f"{team} is in use in {season} by teams.csv but plays no game"


def _venue_problems(games: pl.DataFrame, ref: Reference) -> Iterator[str]:
    unknown = games.join(ref.venues, on="venue", how="anti")
    for (venue,), rows in sorted(unknown.group_by("venue")):
        yield f"venue {venue!r} ({_examples(rows['game_id'])}) is not in venues.csv"


def _home_arenas_by_season(played: pl.DataFrame, ref: Reference) -> pl.DataFrame:
    """Each team-season in played joined with the team's home arenas that season."""
    return (
        played.select("team", "season")
        .unique()
        .join(ref.home_arenas, on="team", how="left")
        .filter(
            (pl.col("season") >= pl.col("first_season"))
            & (pl.col("season") <= pl.col("last_season").fill_null(OPEN_SEASON))
        )
    )


def _home_arena_problems(games: pl.DataFrame, ref: Reference) -> Iterator[str]:
    played = team_games(games)
    home = _home_arenas_by_season(played, ref)
    primaries = (
        played.select("team", "season")
        .unique()
        .join(
            home.group_by("team", "season").agg(pl.col("primary").sum()),
            on=["team", "season"],
            how="left",
        )
        .filter(pl.col("primary").fill_null(0) != 1)
    )
    for team, season, count in primaries.sort("team", "season").iter_rows():
        yield f"{team} has {count or 0} primary home arenas in {season} in home_arenas.csv"
    # The primary arena is where the team's first home game of the season is played. Only a
    # finished season is sure to hold that game: in part of one, the first game present may not be.
    openers = (
        games.filter(~pl.col("neutral_site") & pl.col("season").is_in(finished_seasons(games)))
        .sort("game_date", "game_id")
        .group_by("home", "season")
        .first()
        .join(ref.venues, on="venue")
        .join(
            home.filter("primary").select("team", "season", primary_arena="arena_id"),
            left_on=["home", "season"],
            right_on=["team", "season"],
        )
        .filter(pl.col("arena_id") != pl.col("primary_arena"))
    )
    for team, season, venue, game_id in (
        openers.sort("home", "season").select("home", "season", "venue", "game_id").iter_rows()
    ):
        yield (
            f"{team}'s first home game in {season} ({game_id}) is at {venue}, not its primary "
            "home arena in home_arenas.csv"
        )
    located = games.join(ref.venues, on="venue").join(
        home.select("team", "season", "arena_id", at_home=pl.lit(True)),
        left_on=["home", "season", "arena_id"],
        right_on=["team", "season", "arena_id"],
        how="left",
    )
    away_from_home = located.filter(~pl.col("neutral_site") & pl.col("at_home").is_null())
    for (team, season, venue), rows in sorted(away_from_home.group_by("home", "season", "venue")):
        yield (
            f"{team} hosts at {venue} in {season} ({_examples(rows['game_id'])}), not one of its "
            "home arenas in home_arenas.csv"
        )
    neutral_at_home = located.filter(pl.col("neutral_site") & pl.col("at_home").is_not_null())
    for (team, venue), rows in sorted(neutral_at_home.group_by("home", "venue")):
        yield f"neutral-site games at {team}'s home arena {venue} ({_examples(rows['game_id'])})"


def _coach_problems(games: pl.DataFrame, ref: Reference) -> Iterator[str]:
    line = lineage(ref.teams)
    played = team_games(games).filter(pl.col("team").is_in(list(line)))
    played = played.with_columns(line=pl.col("team").replace_strict(line))
    stints = ref.coaches.filter(pl.col("team").is_in(list(line))).with_columns(
        line=pl.col("team").replace_strict(line)
    )
    covering = played.join(stints.select("line", "first_game", "last_game"), on="line").filter(
        (pl.col("game_date") >= pl.col("first_game"))
        & (pl.col("last_game").is_null() | (pl.col("game_date") <= pl.col("last_game")))
    )
    counts = played.join(
        covering.group_by("game_id", "team").len(), on=["game_id", "team"], how="left"
    ).filter(pl.col("len").fill_null(0) != 1)
    for (team, stints_covering), rows in sorted(
        counts.with_columns(pl.col("len").fill_null(0)).group_by("team", "len")
    ):
        dates = rows["game_date"]
        yield (
            f"{team}: {stints_covering} coach stints cover {rows.height:,} of its games "
            f"({dates.min()} to {dates.max()})"
        )
    # Between a team's first and last game in games, its stints start and end on its games.
    game_days = played.select("line", day="game_date").unique()
    spans = game_days.group_by("line").agg(
        first_day=pl.col("day").min(), last_day=pl.col("day").max()
    )
    for column in ("first_game", "last_game"):
        ends = stints.join(spans, on="line").filter(
            pl.col(column).is_between(pl.col("first_day"), pl.col("last_day"))
        )
        off = ends.join(game_days, left_on=["line", column], right_on=["line", "day"], how="anti")
        for team, day in off.select("team", column).sort("team", column).iter_rows():
            yield f"coaches.csv: {team} {column} {day} is not a game of that team"


def _attendance_problems(games: pl.DataFrame, ref: Reference) -> Iterator[str]:
    limits = ref.attendance_limits
    in_force = (
        games.join(ref.venues, on="venue")
        .join(limits, on="arena_id")
        .filter(pl.col("game_date").is_between(pl.col("first_date"), pl.col("last_date")))
    )
    # A season flagged as limited has a limit at every game.
    if "limited_attendance" in games.columns:
        unlimited = games.filter("limited_attendance").join(in_force, on="game_id", how="anti")
        for (season,), rows in sorted(unlimited.group_by("season")):
            yield (
                f"{season} is limited_attendance, but attendance_limits.csv has no limit for "
                f"{_examples(rows['game_id'])}"
            )
    # A limit with no announcement date was in force from its season's first day.
    starts = games.group_by("season").agg(season_first=pl.col("game_date").min())
    late = (
        limits.filter(pl.col("announced").is_null())
        .join(starts, left_on=_season_of(pl.col("first_date")), right_on="season")
        .filter(pl.col("first_date") > pl.col("season_first"))
    )
    for arena, first in late.select("arena_id", "first_date").sort("arena_id").iter_rows():
        yield (
            f"attendance_limits.csv: the {arena} limit from {first} has no announcement date "
            "but starts after its season's first day"
        )
    # A limit with no next one the day after was lifted. When a game at its arena follows in the
    # same season, the lift's announcement decides that game's share, so it must be recorded.
    lifted = (
        limits.sort("arena_id", "first_date")
        .with_columns(next_first=pl.col("first_date").shift(-1).over("arena_id"))
        .filter(
            pl.col("ended_announced").is_null()
            & (
                pl.col("next_first").is_null()
                | (pl.col("next_first") > pl.col("last_date") + pl.duration(days=1))
            )
        )
    )
    after = (
        games.join(ref.venues, on="venue")
        .join(lifted, on="arena_id")
        .filter(
            (pl.col("game_date") > pl.col("last_date"))
            & (_season_of(pl.col("game_date")) == _season_of(pl.col("last_date")))
        )
    )
    for arena, first, last in (
        after.select("arena_id", "first_date", "last_date").unique().sort("arena_id").iter_rows()
    ):
        yield (
            f"attendance_limits.csv: the {arena} limit from {first} ends on {last}, before a "
            "game there that season, without the lift's announcement date"
        )


def _season_of(day: pl.Expr) -> pl.Expr:
    """20202021 for a date from July 2020 to June 2021: the season it falls in."""
    year = day.dt.year()
    first = pl.when(day.dt.month() >= 7).then(year).otherwise(year - 1)
    return (first * 10_001 + 1).cast(pl.Int32)
