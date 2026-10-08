"""Player league seasons (#98): each player's season lines in every league, from the seasonTotals
array of the player landing pages in the raw cache, for the NHLe offensive priors (#102).

nhl ingest fetches a player's landing page once, when he first shows up on a roster or in a
boxscore, and reuses it from then on (NhlApi.player_landing). So this reads the newest cached page
of every player in players and makes no request; a season played after a page's fetch needs that
page fetched again. Once a season, after its lines are public, `nhl player-seasons --refresh`
does that for every player with a boxscore in it (refresh, #117).

Point in time: a season's lines count as public on July 1 (00:00 UTC) after it, when nearly every
league's season and the NHL playoffs are over (lake/schemas.py, SEASON_LINES_PUBLIC). The
exceptions come later: the Australian league (April to September), the World Cup of Hockey (August
and September), the Brick Invitational (July), the Olympic qualification and a few events whose
dates are not known on October 1 (LATE_LEAGUES); every line of 2019-20 and 2020-21, whose NHL
playoffs ran past July 1, from their end (LATE_SEASONS); and the 2021-22 World Juniors, replayed
in August 2022, from October 1, 2022 (LATE_LEAGUE_SEASONS). The pages' fetch time would hide all
history from the backtest, since the backfill fetched them in 2026. A line whose season had not
reached that day when its page was fetched is a partial season and is dropped. A page fetched long
after a season may carry later corrections to its goals and assists, which ADR 0016 accepts.

Every player here reached the NHL, so that he has rows is hindsight before his first NHL game.
His rows count as public only once his first boxscore in the lake has (first_boxscore_utc, from
actual_lineups), and a player without a boxscore has none until he plays.
"""

import json
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import polars as pl

from nhl_edge.ingest.nhl_api import NhlApi, NotFoundError, fetched_after, parse_utc
from nhl_edge.ingest.players import landing_row
from nhl_edge.lake.raw import RawStore
from nhl_edge.lake.schemas import (
    LATE_LEAGUE_SEASONS,
    LATE_LEAGUES,
    LATE_LINES_PUBLIC,
    LATE_SEASONS,
    PLAYER_LEAGUE_SEASONS_KEY,
    SEASON_LINE_GAME_TYPES,
    SEASON_LINES_PUBLIC,
    PlayerLeagueSeasons,
    dtypes,
    season_lines_public_utc,
)

LANDING_PREFIX = "nhl/player-landing"
# The NHL draft's age cutoff (month, day): eligibility turns on a player's age on September 15.
AGE_CUTOFF = (9, 15)
COUNTS = ("games_played", "goals", "assists")
# One per-team line of a landing page, before the lines of a league-season are summed.
LINE_SCHEMA: dict[str, pl.DataType] = {
    "player_id": pl.Int64(),
    "season": pl.Int32(),
    "league_abbrev": pl.String(),
    "game_type": pl.Int8(),
    "games_played": pl.Int16(),
    "goals": pl.Int16(),
    "assists": pl.Int16(),
    "raw_key": pl.String(),
}

# Known variants of one league, after trimming and upper case, mapped to the name the cached pages
# use for its latest seasons. The NHL API names many leagues one way up to about 2015-16 and
# another way after, and the seasons in each comment are those the cache lists under the variant
# (checked 2026-10-02). Predecessor leagues stay apart: Russia (the Superleague, to 2007-08) is
# not the KHL, and Russia-2 (the Vysshaya Liga, before and after the VHL began in 2010) is not the
# VHL. CHL (1998-99 to 2013-14) is the Central Hockey League, not the Champions HL. Upper case
# alone merges MtJHL with MTJHL.
LEAGUE_VARIANTS: dict[str, str] = {
    "RUS-KHL": "KHL",  # 2008-09 to 2010-11, the KHL's first seasons
    "SWEDEN": "SHL",  # to 2015-16: Elitserien, renamed SHL in 2013
    "FINLAND": "LIIGA",  # to 2014-15: SM-liiga, renamed Liiga in 2013
    "NLA": "NL",  # 2002-03 to 2016-17: Swiss National League A, renamed National League in 2017
    "SWISS": "NL",  # to 2015-16: the same Swiss top league
    "CZREP": "CZECHIA",  # 1993-94 to 2015-16: the Czech Extraliga
    "CZECH": "CZECHIA",  # to 2017-18: the Czech Extraliga (Czechoslovak league before 1993-94)
    "CZREP-2": "CZECHIA2",  # 1999-00 to 2015-16: the Czech second tier
    "CZECH2": "CZECHIA2",  # to 2017-18: the Czech second tier
    "GERMANY": "DEL",  # to 2015-16: the DEL (the Bundesliga before 1994-95)
    "EBEL": "ICEHL",  # 2007-08 to 2019-20: Erste Bank Eishockey Liga, ICE Hockey League from 2020
    "AUSTRIA": "ICEHL",  # 2003-04 to 2015-16: the Austrian top league, the EBEL
    "ALLSVENSKAN": "HOCKEYALLSVENSKAN",  # to 2014-15: the Swedish second tier
    "SWEDEN-2": "HOCKEYALLSVENSKAN",  # to 2015-16: the Swedish second tier
    "FINLAND-2": "MESTIS",  # to 2015-16: the Finnish second tier
    "RUSSIA-JR.": "MHL",  # 2009-10 to 2015-16: the MHL, Russia's junior league since 2009
    "SWE-JR.": "J20 NATIONELL",  # to 2015-16: Sweden's top U20 league
    "SUPERELIT": "J20 NATIONELL",  # 1996-97 to 2014-15: J20 SuperElit, Sweden's top U20 league
    "J20 SUPERELIT": "J20 NATIONELL",  # 2015-16 to 2019-20: the same league
    "FIN-JR.": "U20 SM-SARJA",  # to 2015-16: Finland's top U20 league
    "FINLAND-JR.": "U20 SM-SARJA",  # one line of 2008-09, spelt FInland-Jr.
    "JR. A SM-LIIGA": "U20 SM-SARJA",  # 2004-05 to 2014-15: the same league
    "U20 SM-LIIGA": "U20 SM-SARJA",  # 2015-16 to 2019-20: the same league
    "USDP": "NTDP",  # 2008-09 to 2017-18: the US National Team Development Program
    "CCHA": "NCAA",  # to 2012-13: an NCAA Division I conference
    "ECAC": "NCAA",  # to 2015-16: an NCAA Division I conference
    "H-EAST": "NCAA",  # to 2015-16: Hockey East, an NCAA Division I conference
    "WCHA": "NCAA",  # to 2014-15: an NCAA Division I conference
    "NCHC": "NCAA",  # 2013-14 and 2014-15: an NCAA Division I conference
    "BIG TEN": "NCAA",  # 2013-14 and 2014-15: an NCAA Division I conference
    "WC-A": "WC",  # to 2015-16: the World Championship's top division
    "WJC-A": "WJC-20",  # to 2015-16: the World Junior Championship's top division
    "WJ18-A": "WJC-18",  # to 2015-16: the U18 World Championship's top division
    "OLYMPICS": "OG",  # to 2013-14: the Olympic Games
    "W-CUP": "WCUP",  # 1996-97 (and game types 6 and 7 of 2003-04): the World Cup of Hockey
}


def season_lines_public(season: int, league_abbrev: str) -> datetime:
    """When the lines of a season given as 20152016 count as public, as season_lines_public_utc in
    lake/schemas.py: July 1 (00:00 UTC) of its second year, October 1 for a late league, and never
    before the end of a late season or league-season."""
    league = league_name_of(league_abbrev)
    month, day = LATE_LINES_PUBLIC if league in LATE_LEAGUES else SEASON_LINES_PUBLIC
    public = datetime(season % 10_000, month, day, tzinfo=UTC)
    return max(
        public,
        LATE_SEASONS.get(season, public),
        LATE_LEAGUE_SEASONS.get((season, league), public),
    )


def league_name_of(abbrev: str) -> str:
    """league_name for one raw abbreviation."""
    name = abbrev.strip().upper()
    return LEAGUE_VARIANTS.get(name, name)


def league_name(abbrev: pl.Expr) -> pl.Expr:
    """The league of a raw leagueAbbrev: trimmed, upper case, and a known variant of a league
    mapped to its one name (LEAGUE_VARIANTS)."""
    return abbrev.str.strip_chars().str.to_uppercase().replace(LEAGUE_VARIANTS)


def age_at_season(season: pl.Expr, birth_date: pl.Expr) -> pl.Expr:
    """Whole years of age on September 15 of the season's first year, the NHL draft cutoff, so
    that age means what it does for the draft. Null without a birth date."""
    month, day = AGE_CUTOFF
    born_after = (birth_date.dt.month() > month) | (
        (birth_date.dt.month() == month) & (birth_date.dt.day() > day)
    )
    return season // 10_000 - birth_date.dt.year() - born_after.cast(pl.Int32)


@dataclass
class PageLines:
    """The per-team lines of one landing page that count, and how many were left out."""

    player_id: int
    rows: list[dict[str, Any]]
    other_game_types: int = 0
    partial: int = 0


def landing_lines(body: bytes, fetched_utc: datetime, raw_key: str) -> PageLines:
    """The regular-season and playoff lines, one per team, that were public (season_lines_public)
    when the page was fetched. Other game types (the World Cup of Hockey's 6 and 7) and the lines
    of a season still under way are counted and left out. A page without seasonTotals has none."""
    data = json.loads(body)
    page = PageLines(data["playerId"], [])
    for line in data.get("seasonTotals") or []:
        if line["gameTypeId"] not in SEASON_LINE_GAME_TYPES:
            page.other_game_types += 1
        elif season_lines_public(line["season"], line["leagueAbbrev"]) > fetched_utc:
            page.partial += 1
        else:
            page.rows.append(
                {
                    "player_id": page.player_id,
                    "season": line["season"],
                    "league_abbrev": line["leagueAbbrev"],
                    "game_type": line["gameTypeId"],
                    "games_played": line.get("gamesPlayed"),
                    "goals": line.get("goals"),
                    "assists": line.get("assists"),
                    "raw_key": raw_key,
                }
            )
    return page


def first_boxscores(lineups: pl.DataFrame) -> pl.DataFrame:
    """Each player's first_boxscore_utc: when his first boxscore in actual_lineups became
    public."""
    return lineups.group_by("player_id").agg(first_boxscore_utc=pl.col("observed_utc").min())


def lineup_problems(
    games: pl.DataFrame, lineups: pl.DataFrame, expected: Mapping[int, int]
) -> list[str]:
    """Why lineups (actual_lineups) cannot give every player's first boxscore: a season short of
    its games (expected), or a game without a boxscore. A rebuild from a partial copy would date
    debuts too late and drop players, and replace the whole table with that."""
    problems = []
    for season in sorted(expected):
        count = games.filter(pl.col("season") == season).height
        if count != expected[season]:
            problems.append(f"{season}: {count:,} of {expected[season]:,} games")
    missing = games.join(lineups.select("game_id").unique(), on="game_id", how="anti")
    if missing.height:
        examples = ", ".join(map(str, missing.sort("game_id")["game_id"].head(3).to_list()))
        problems.append(
            f"{missing.height:,} games without a boxscore in actual_lineups, e.g. {examples}"
        )
    return problems


def player_league_seasons(
    lines: pl.DataFrame, players: pl.DataFrame, debuts: pl.DataFrame
) -> pl.DataFrame:
    """Sum each player's per-team lines (LINE_SCHEMA) into one row per season, league
    abbreviation and game type, with teams counting the lines. A count is null when any line
    summed lacks it, since a partial sum would read as a whole season. The age comes from
    players.birth_date. debuts (first_boxscores) gives when each player's first boxscore became
    public: his rows are observed no earlier, and a player without one has none, since his lines
    would tell that he will reach the NHL."""
    summed = [
        pl.when(pl.col(count).is_null().any())
        .then(None)
        .otherwise(pl.col(count).sum())
        .alias(count)
        for count in COUNTS
    ]
    frame = (
        lines.group_by(list(PLAYER_LEAGUE_SEASONS_KEY))
        .agg(pl.len().alias("teams"), *summed, pl.col("raw_key").first())
        .join(players.select("player_id", "birth_date"), on="player_id", how="left")
        .join(debuts.select("player_id", "first_boxscore_utc"), on="player_id", how="inner")
        .with_columns(league=league_name(pl.col("league_abbrev")))
        .with_columns(
            age_at_season=age_at_season(pl.col("season"), pl.col("birth_date")),
            observed_utc=pl.max_horizontal(
                season_lines_public_utc(pl.col("season"), pl.col("league")),
                pl.col("first_boxscore_utc"),
            ),
        )
    )
    schema = dtypes(PlayerLeagueSeasons)
    frame = frame.select(list(schema)).cast(schema)  # type: ignore[arg-type]
    return PlayerLeagueSeasons.validate(frame.sort(PLAYER_LEAGUE_SEASONS_KEY))


@dataclass
class Report:
    """What one rebuild read and wrote, in counts."""

    players: int = 0  # in the players table
    pages: int = 0  # landing pages read
    without_page: list[int] = field(default_factory=list)  # players with no cached page
    # Players with a page but no boxscore yet, and their lines left out.
    without_boxscore: list[int] = field(default_factory=list)
    # Players with a boxscore but not in players (his page could not be fetched): no lines.
    not_in_players: list[int] = field(default_factory=list)
    unplayed_lines: int = 0
    lines: int = 0  # per-team lines kept
    other_game_types: int = 0  # lines of game types other than 2 and 3
    partial: int = 0  # lines of a season not over when the page was fetched
    rows: int = 0  # rows of the table


def build(
    store: RawStore, players: pl.DataFrame, lineups: pl.DataFrame
) -> tuple[pl.DataFrame, Report]:
    """The whole table, from the newest cached landing page of every player in players, read
    with its fetch time as the ingest reads it, and his first boxscore in lineups
    (actual_lineups). A player without a cached page, without a boxscore, or with a boxscore but
    not in players, is counted in the report, not an error: his lines stay out until his page is
    fetched and he has played. A page that belongs to another player is an error."""
    report = Report(players=players.height)
    debuts = first_boxscores(lineups)
    played = set(debuts["player_id"].to_list())
    report.not_in_players = sorted(played - set(players["player_id"].to_list()))
    rows: list[dict[str, Any]] = []
    for player_id in sorted(players["player_id"].to_list()):
        raw_key = store.latest(f"{LANDING_PREFIX}/{player_id}")
        if raw_key is None:
            report.without_page.append(player_id)
            continue
        fetched_utc = parse_utc(store.meta(raw_key)["fetched_utc"])
        page = landing_lines(store.get(raw_key), fetched_utc, raw_key)
        if page.player_id != player_id:
            raise ValueError(f"{raw_key} is the landing page of player {page.player_id}")
        report.pages += 1
        report.other_game_types += page.other_game_types
        report.partial += page.partial
        if player_id not in played:
            report.without_boxscore.append(player_id)
            report.unplayed_lines += len(page.rows)
            continue
        rows += page.rows
    report.lines = len(rows)
    frame = player_league_seasons(pl.DataFrame(rows, schema=LINE_SCHEMA), players, debuts)
    report.rows = frame.height
    return frame, report


@dataclass
class RefreshReport:
    """The yearly refresh of one season's players' landing pages."""

    season: int
    players: int = 0
    fetched: int = 0
    reused: int = 0
    missing: list[int] = field(default_factory=list)
    # players rows for those with a boxscore who aren't in players, whose page the ingest could
    # not fetch before and the refresh now has.
    recovered: list[dict[str, Any]] = field(default_factory=list)


def refresh_season(lineups: pl.DataFrame, now: datetime) -> int | None:
    """The latest season with boxscores in lineups (actual_lineups) whose NHL lines are public
    by now (season_lines_public), or None: the season the yearly refresh fetches pages for."""
    seasons = sorted(set(lineups["season"].to_list()), reverse=True)
    return next((s for s in seasons if season_lines_public(s, "NHL") <= now), None)


def refresh(
    api: NhlApi, lineups: pl.DataFrame, season: int, known: Collection[int]
) -> RefreshReport:
    """Fetch again the landing page of every player with a boxscore in season, unless his newest
    copy was fetched once the season's lines were public, so a rerun reuses it and makes no
    request. Each new copy is stored beside the old ones, and build then reads it as the newest:
    the season enters player_league_seasons with every row's observed_utc as before, set by the
    season's public date and the player's first boxscore, never by the fetch. A later replay of
    players reads the new copy too, which changes only its provenance (fetched_utc, raw_key):
    players has no observed_utc, and holds only facts fixed before a debut. A page the API no
    longer has is counted. A player not among known (players' ids) whose page it now has comes
    back as a players row (recovered), for the caller to add before the rebuild."""
    public = season_lines_public(season, "NHL")
    ids = sorted(set(lineups.filter(pl.col("season") == season)["player_id"].to_list()))
    report = RefreshReport(season, players=len(ids))
    for player_id in ids:
        try:
            response = api.player_landing(player_id, fetched_after(public))
        except NotFoundError:
            report.missing.append(player_id)
            continue
        if response.cached:
            report.reused += 1
        else:
            report.fetched += 1
        if player_id not in known:
            report.recovered.append(
                landing_row(response.body, response.fetched_utc, response.raw_key)
            )
    return report
