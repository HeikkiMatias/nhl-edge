"""The `nhl` command line: one interface for you, Claude Code and GitHub Actions."""

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, NoReturn

import typer

from nhl_edge import __version__
from nhl_edge.backtest.seasons import DEVELOPMENT_SEASONS, SEASON_ROLES

if TYPE_CHECKING:
    import polars as pl

    from nhl_edge.backtest.one_time import Places
    from nhl_edge.game import b2, b3
    from nhl_edge.lake.status import TableState
    from nhl_edge.lake.tables import Lake

app = typer.Typer(
    help="NHL moneyline model that must add information beyond a recalibrated market.",
    no_args_is_help=True,
)
odds_app = typer.Typer(help="Live odds snapshots and historical odds.", no_args_is_help=True)
app.add_typer(odds_app, name="odds")
goalies_app = typer.Typer(
    help="Pre-game starting goalies, polled before puck drop.", no_args_is_help=True
)
app.add_typer(goalies_app, name="goalies")
lake_app = typer.Typer(help="The R2 lake: raw responses and parquet tables.", no_args_is_help=True)
app.add_typer(lake_app, name="lake")
audit_app = typer.Typer(
    help="Data audits, reviewed by hand before a model depends on the data.", no_args_is_help=True
)
app.add_typer(audit_app, name="audit")
live_app = typer.Typer(
    help="Phase 5's live path: the day's slate and its feature rows.", no_args_is_help=True
)
app.add_typer(live_app, name="live")

DEFAULT_BACKTEST_SEASONS = ",".join(str(season) for season in DEVELOPMENT_SEASONS)
DEFAULT_BACKTEST_OUT = Path("reports/backtest")
# The version component of 2022-23's one run, apart from the development runs' (#145).
MARKET_VALIDATION_RUN = "market-validation"
DEFAULT_AUDIT_OUT = Path("reports/audit")
DEFAULT_XG_OUT = Path("reports/xg")
DEFAULT_TUNING_OUT = Path("reports/tuning")
DEFAULT_GOALIE_START_OUT = Path("reports/goalie-start")
DEFAULT_LINEUPS_OUT = Path("reports/lineups")
DEFAULT_RATINGS_OUT = Path("reports/ratings")
DEFAULT_POWER_PLAYS_OUT = Path("reports/power-plays")
DEFAULT_FINISHING_OUT = Path("reports/finishing")
# A live build's builder reports: beside the lake, never committed.
DEFAULT_LIVE_OUT = Path("data/live/reports")

TargetsOption = Annotated[
    datetime | None,
    typer.Option(
        "--targets",
        formats=["%Y-%m-%d"],
        help="Also rate this date's slate (nhl live features): its games not played yet.",
    ),
]


def _slate_targets(
    lake: "Lake", day: datetime | None, wanted: list[int], tune: bool = False
) -> "pl.DataFrame | None":
    """The date's slate games fetched before their as-of time, to rate beside the seasons' games
    (#162); None without --targets. They must be in the seasons rated."""
    if day is None:
        return None
    if tune:
        raise typer.BadParameter("--tune rates no slate", param_hint="--targets")
    from nhl_edge.live import slate
    from nhl_edge.live.targets import in_time

    games = in_time(slate.of_day(lake.read("slate"), day.date()))
    outside = sorted(set(games["season"].to_list()) - set(wanted))
    if outside:
        raise typer.BadParameter(
            f"the slate of {day:%Y-%m-%d} is in {outside}, outside the seasons rated",
            param_hint="--targets",
        )
    return games


def _not_implemented(command: str, phase: str) -> NoReturn:
    typer.echo(f"nhl {command}: not implemented yet ({phase}, see docs/plan.md).", err=True)
    raise typer.Exit(code=1)


@app.command()
def ingest(
    seasons: Annotated[
        str | None,
        typer.Option(help="Seasons as 20232024, a comma list, or a range 20102011-20252026."),
    ] = None,
    start: Annotated[
        datetime | None, typer.Option(formats=["%Y-%m-%d"], help="First game date.")
    ] = None,
    end: Annotated[
        datetime | None,
        typer.Option(formats=["%Y-%m-%d"], help="Last game date (default: --start)."),
    ] = None,
    recent: Annotated[
        int | None,
        typer.Option(
            min=1, help="The last N game dates up to yesterday (US Eastern); 1 is yesterday."
        ),
    ] = None,
    feeds: Annotated[
        bool, typer.Option(help="Cache play-by-play, boxscore and shift chart per game.")
    ] = True,
    players: Annotated[bool, typer.Option(help="Rosters and player landing pages.")] = True,
    replay: Annotated[
        bool, typer.Option("--replay", help="Rebuild from the raw cache only, no network.")
    ] = False,
    r2: Annotated[
        bool, typer.Option("--r2", help="Mirror raw responses and lake tables to R2.")
    ] = False,
    supabase: Annotated[
        bool, typer.Option("--supabase", help="Upsert games to Supabase and keep it awake.")
    ] = False,
) -> None:
    """Ingest NHL games and players into the lake, caching every raw response."""
    from nhl_edge.ingest.nhl_api import NhlApi, utc_now
    from nhl_edge.ingest.nhl_ingest import (
        DateRange,
        Ingest,
        Season,
        Window,
        parse_seasons,
        recent_days,
    )
    from nhl_edge.lake.raw import RawStore
    from nhl_edge.lake.supabase import Supabase
    from nhl_edge.lake.tables import Lake
    from nhl_edge.settings import load_env

    if sum(option is not None for option in (seasons, start, recent)) != 1:
        raise typer.BadParameter("pass exactly one of --seasons, --start or --recent")
    if end is not None and start is None:
        raise typer.BadParameter("--end needs --start")
    windows: list[Window]
    if seasons is not None:
        try:
            windows = [Season(season) for season in parse_seasons(seasons)]
        except ValueError as exc:
            raise typer.BadParameter(str(exc), param_hint="--seasons") from None
    elif start is not None:
        last = (end or start).date()
        if last < start.date():
            raise typer.BadParameter("--end is before --start")
        windows = [DateRange(start.date(), last)]
    else:
        windows = [recent_days(utc_now(), recent or 1)]

    load_env()
    store = RawStore.from_env(mirror=r2, flag="--r2")
    lake = Lake.from_env(mirror=r2)
    writer = Supabase.from_env() if supabase else None
    if writer is not None:
        typer.echo(f"supabase project: {writer.project_ref}")
    Ingest(
        api=NhlApi(store, offline=replay),
        lake=lake,
        supabase=writer,
        feeds=feeds,
        players=players,
        echo=typer.echo,
    ).run(windows)


@app.command()
def recheck(
    recent: Annotated[
        int,
        typer.Option(min=1, help="Games whose tables' copy was fetched 7 to 7+N days ago."),
    ] = 3,
    r2: Annotated[
        bool, typer.Option("--r2", help="Read games and raw copies from R2, and mirror rechecks.")
    ] = False,
) -> None:
    """Fetch each final game's play-by-play, boxscore and shift chart again a week after the copy
    the tables read, stored under <kind>-recheck where no table reads them, to measure post-game
    corrections (#30, ADR 0004). `nhl audit report` compares the two copies."""
    from datetime import timedelta

    import polars as pl

    from nhl_edge.ingest.corrections import first_fetches
    from nhl_edge.ingest.corrections import recheck as recheck_feeds
    from nhl_edge.ingest.nhl_api import NhlApi, utc_now
    from nhl_edge.lake.raw import RawStore
    from nhl_edge.lake.tables import Lake
    from nhl_edge.settings import load_env

    load_env()
    store = RawStore.from_env(mirror=r2, flag="--r2")
    lake = Lake.from_env(mirror=r2)
    now = utc_now()
    if r2:
        lake.pull("games")
    games = lake.read("games")
    # The seasons of the last year's games, whose lineups date each game's first copy.
    seasons = games.filter(pl.col("game_date") >= now.date() - timedelta(days=365))["season"]
    if r2:
        lake.pull("actual_lineups", seasons=seasons.unique().to_list())
    lineups = lake.read("actual_lineups").filter(pl.col("season").is_in(seasons.unique()))
    summary = recheck_feeds(NhlApi(store), games, first_fetches(lineups), now, recent)
    start, end = summary.window
    window = f"copies fetched {start:%Y-%m-%d %H:%M}..{end:%Y-%m-%d %H:%M} UTC"
    typer.echo(
        f"recheck {window}: {summary.games} games, {summary.fetched} feeds fetched, "
        f"{summary.reused} already rechecked; {len(summary.not_due)} not a week old yet, "
        f"{len(summary.never_ingested)} never ingested"
    )
    for missing in summary.not_found:
        typer.echo(f"warning: no {missing} to recheck", err=True)


@app.command("toi-reports")
def toi_reports(
    seasons: Annotated[
        str, typer.Option(help="Seasons as 20242025, a comma list or a range.")
    ] = "20242025",
    r2: Annotated[
        bool,
        typer.Option(
            "--r2", help="Read shift_coverage from R2, reuse its stored pages and mirror new ones."
        ),
    ] = False,
) -> None:
    """Fetch the NHL's HTML time-on-ice reports, home and visitor, of the games whose shift chart
    gave no shifts (#68), once each, into the raw store under nhl/toi-home/ and nhl/toi-visitor/.
    Only the 57 games of 2024-25 the owner allowed on 2026-10-02 are fetched, and a stored page is
    never fetched again. nhl ingest then builds those games' shifts from the stored pages."""
    from nhl_edge.ingest.nhl_api import NhlApi
    from nhl_edge.ingest.nhl_ingest import parse_seasons
    from nhl_edge.ingest.toi_reports import chartless_games, fetch_reports
    from nhl_edge.lake.raw import RawStore
    from nhl_edge.lake.tables import Lake
    from nhl_edge.settings import load_env

    try:
        wanted = parse_seasons(seasons)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--seasons") from None
    load_env()
    store = RawStore.from_env(mirror=r2, flag="--r2")
    lake = Lake.from_env(mirror=r2)
    if r2:
        lake.pull("shift_coverage", wanted)
    games = chartless_games(lake.read("shift_coverage", wanted))
    typer.echo(f"{len(games)} games in {seasons} whose shift chart gave no shifts")
    api = NhlApi(store)
    try:
        summary = fetch_reports(api, games)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(
        f"{summary.reports} time-on-ice reports: {summary.stored_before} already stored, "
        f"{summary.fetched} fetched in {api.requests} requests; "
        f"{summary.players} players, {summary.shifts} shifts"
    )
    for problem in summary.problems:
        typer.echo(f"warning: {problem}", err=True)
    if summary.problems:
        raise typer.Exit(code=1)


@app.command("player-seasons")
def player_seasons(
    r2: Annotated[
        bool,
        typer.Option(
            "--r2",
            help="Restore the landing pages, players, games and boxscores from R2 first, and "
            "mirror the table.",
        ),
    ] = False,
    refresh: Annotated[
        bool,
        typer.Option(
            "--refresh",
            help="First fetch again the landing page of every player with a boxscore in the "
            "latest season whose lines are public (July 1 after it) or in the two seasons "
            "before, once (#117, #199).",
        ),
    ] = False,
) -> None:
    """Rebuild the lake's player_league_seasons (#98) from the player landing pages in the raw
    cache: each player's season lines in every league, for the NHLe priors. Makes no request
    unless --refresh, the yearly step that brings in the season just played. A player in players
    without a cached page, or without a boxscore in actual_lineups yet, is counted and left out."""
    from nhl_edge.ingest.games import EXPECTED_GAMES
    from nhl_edge.ingest.player_seasons import LANDING_PREFIX, build, lineup_problems
    from nhl_edge.lake.raw import RawStore
    from nhl_edge.lake.tables import Lake
    from nhl_edge.settings import load_env

    load_env()
    store = RawStore.from_env(mirror=r2, flag="--r2")
    if r2:
        restored = store.restore_from_r2(f"{LANDING_PREFIX}/").copied
        typer.echo(f"restored {restored} raw landing pages from R2")
    lake = Lake.from_env(mirror=r2)
    if r2:
        for table in ("players", "games", "actual_lineups"):
            lake.pull(table)
    players, lineups = lake.read("players"), lake.read("actual_lineups")
    if players.is_empty() or lineups.is_empty():
        # Without boxscores every player would look unplayed and the rebuild would empty the table.
        typer.echo("no players or no boxscores in the lake: run nhl ingest", err=True)
        raise typer.Exit(code=1)
    # A partial copy of the boxscores would date debuts too late and drop players.
    problems = lineup_problems(lake.read("games"), lineups, EXPECTED_GAMES)
    if problems:
        for problem in problems:
            typer.echo(problem, err=True)
        typer.echo("the boxscores are incomplete: run nhl ingest --replay, or pass --r2", err=True)
        raise typer.Exit(code=1)
    if refresh:
        from nhl_edge.ingest.nhl_api import NhlApi, utc_now
        from nhl_edge.ingest.player_seasons import refresh as refresh_pages
        from nhl_edge.ingest.player_seasons import refresh_season, refresh_seasons

        season = refresh_season(lineups, utc_now())
        if season is None:
            typer.echo(
                "no season with boxscores has public lines yet: nothing to refresh", err=True
            )
            raise typer.Exit(code=1)
        played = set(lineups["season"].unique().to_list())
        uncounted = [s for s in refresh_seasons(season) if s in played and s not in EXPECTED_GAMES]
        if uncounted:
            # Without its game count the check above can't tell a season's boxscores are all
            # here, and a missing game would leave its players out of the refresh.
            typer.echo(
                f"{', '.join(map(str, uncounted))} has no expected game count (ingest/games.py "
                "EXPECTED_GAMES), so its boxscores can't be checked complete: add it first",
                err=True,
            )
            raise typer.Exit(code=1)
        from nhl_edge.ingest.players import parse_players

        api = NhlApi(store)
        done = refresh_pages(api, lineups, season, set(players["player_id"].to_list()))
        typer.echo(
            f"refreshed {season}: {done.players:,} players with a boxscore in it or the "
            f"{len(done.seasons) - 1} seasons before, {done.fetched:,} landing pages fetched in "
            f"{api.requests:,} requests, {done.reused:,} already fetched after its lines were "
            f"public, {len(done.missing):,} not found"
        )
        if done.missing:
            typer.echo(
                f"warning: no landing page for {', '.join(map(str, done.missing))}", err=True
            )
        if done.recovered:
            # Players whose page the ingest couldn't fetch before: in players, so their lines
            # count.
            lake.upsert("players", parse_players(done.recovered))
            players = lake.read("players")
            typer.echo(f"added {len(done.recovered)} players whose page the ingest lacked")
    try:
        frame, report = build(store, players, lineups)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    if not report.pages:
        # Without a single page the rebuild would empty the table: the cache is not here.
        typer.echo(
            "no landing pages in the raw cache: run nhl lake restore-raw or pass --r2", err=True
        )
        raise typer.Exit(code=1)
    lake.replace("player_league_seasons", frame)
    typer.echo(
        f"player_league_seasons: {report.players:,} players, {report.pages:,} landing pages read, "
        f"{len(report.without_page):,} without a page and {len(report.without_boxscore):,} "
        f"without a boxscore yet; {report.lines:,} team lines kept, "
        f"{report.other_game_types:,} of other game types, {report.partial:,} of seasons under "
        f"way at the fetch and {report.unplayed_lines:,} of players without a boxscore left out; "
        f"{report.rows:,} rows written"
    )
    if report.without_page:
        typer.echo(
            f"warning: no landing page for {len(report.without_page)} players: "
            f"{', '.join(map(str, report.without_page))}",
            err=True,
        )
    if report.not_in_players:
        typer.echo(
            f"warning: {len(report.not_in_players)} players with a boxscore are not in players, "
            f"so they have no lines: {', '.join(map(str, report.not_in_players))} "
            "(nhl ingest fetches their pages)",
            err=True,
        )


@app.command()
def rate() -> None:
    """Refresh team, goalie and player ratings."""
    _not_implemented("rate", "phase 2")


@app.command()
def predict(
    day: Annotated[
        datetime | None,
        typer.Option("--date", formats=["%Y-%m-%d"], help="The game date (default: today, ET)."),
    ] = None,
    r2: Annotated[
        bool,
        typer.Option(
            "--r2",
            help="Pull the lake's tables and the day's odds from R2 first; without --dry-run, "
            "write the day's ledger to R2, once.",
        ),
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Write the ledger locally only, never to R2.")
    ] = False,
    at: Annotated[
        datetime | None,
        typer.Option(
            formats=["%Y-%m-%dT%H:%M:%S%z"],
            help="A dry run's decision instant, such as 2026-10-07T16:47:00+00:00 (default: now).",
        ),
    ] = None,
    fit: Annotated[
        Path | None,
        typer.Option(help="The live fit to read (default: the season's under reports/live/)."),
    ] = None,
    out: Annotated[
        Path, typer.Option(help="Where a dry run writes the ledger and its run bundle, once.")
    ] = Path("data/live/dry"),
    cron: Annotated[
        str | None,
        typer.Option(
            help="The odds workflow's fallback cron line: decide only if it is today's midday "
            "slot, and otherwise do nothing."
        ),
    ] = None,
) -> None:
    """Decide the day's paper bets at the midday snapshot (#164, ADRs 0028, 0030 and 0033): every
    slate game gets one ledger row, its prediction and bet or why it has none. The decision
    instant is fixed when the run starts, and a game that starts before the ledger is published
    gets no prediction. A real run (--r2) writes the day once to R2
    (ledger/live/<date>.parquet), every prediction before its game's start, and to the lake's
    paper_ledger;
    after the window it writes the day as skipped, so no late run can reconstruct it."""
    import json
    import time
    from datetime import UTC
    from zoneinfo import ZoneInfo

    import polars as pl

    from nhl_edge.backtest import reports
    from nhl_edge.backtest.walk_forward import outcomes
    from nhl_edge.betting.selection import POLICY_VERSION
    from nhl_edge.game import b2, b3, uncertainty
    from nhl_edge.ingest.odds import ET
    from nhl_edge.lake.raw import RawStore
    from nhl_edge.lake.tables import Lake
    from nhl_edge.live import blend_fit
    from nhl_edge.live import bundle as lb
    from nhl_edge.live import features as lf
    from nhl_edge.live import predict as lp
    from nhl_edge.live.slate import of_day
    from nhl_edge.settings import load_env

    started = datetime.now(UTC)
    if cron is not None:
        from nhl_edge.ingest.odds import resolve_slot

        today = started.astimezone(ZoneInfo("America/New_York")).date()
        slot = resolve_slot("free-tier", cron, today)
        if slot is None or slot.name != "midday":
            typer.echo(f"{cron!r} is not today's midday slot: no decision")
            return
    if at is not None and not dry_run:
        raise typer.BadParameter("only a dry run takes a decision instant", param_hint="--at")
    if not dry_run and not r2:
        raise typer.BadParameter("a real run writes to R2: pass --r2, or --dry-run")
    decision = at.astimezone(UTC) if at is not None else started
    game_date = day.date() if day is not None else decision.astimezone(ET).date()
    code_version = reports.version("predict", started)
    if not dry_run:
        if code_version.endswith("-dirty"):
            raise typer.BadParameter("commit first: a logged decision names its commit")
        if game_date != decision.astimezone(ET).date():
            raise typer.BadParameter(
                "a real run decides today's slate only; check another day with --dry-run --at",
                param_hint="--date",
            )
        if fit is not None:
            raise typer.BadParameter(
                "a real run reads the season's committed fit only", param_hint="--fit"
            )
        if not lp.in_window(decision) and not lp.after_window(decision):
            raise typer.BadParameter(
                "the decision window opens at 12:45 ET: a run before it writes nothing"
            )
    elif (out / lb.prefix(game_date)).exists():
        # A dry run's bundle is written once too: refused before its ledger is overwritten.
        raise typer.BadParameter(
            f"{out} already holds {game_date}'s run bundle: pass a fresh --out", param_hint="--out"
        )
    load_env()
    lake = Lake.from_env(mirror=r2)
    store = RawStore.from_env(mirror=r2, flag="--r2")
    if r2:
        pulled = sum(lake.pull(table) for table in lp.LAKE_TABLES)
        restored = store.restore_from_r2(prefix=f"odds/{game_date.isoformat()}/")
        typer.echo(f"pulled {pulled:,} table files and the day's odds ({restored}) from R2")
    paths = [fit] if fit is not None else sorted(blend_fit.REPORTS.glob("blend-live-*.json"))
    if len(paths) != 1:
        typer.echo(f"expected the season's one live fit, found {len(paths)}", err=True)
        raise typer.Exit(code=1)
    try:
        live = blend_fit.load(json.loads(paths[0].read_text()))
    except ValueError as exc:
        typer.echo(f"{paths[0]}: {exc}", err=True)
        raise typer.Exit(code=1) from None
    slate = of_day(lake.read("slate"), game_date)
    if slate.is_empty():
        # Only a build that found no games makes an empty slate an off day; without its record
        # the nightly build may have failed, which is an alert, never a quiet success.
        marker = lake.read("feature_builds").filter(
            pl.col("game_date") == game_date, pl.col("table") == "slate"
        )
        if marker.height and marker["slate_games"].max() == 0:
            typer.echo(f"{game_date}: no games scheduled, nothing to decide")
            return
        typer.echo(f"{game_date}: no slate and no record of a build that found no games", err=True)
        raise typer.Exit(code=1)
    (season,) = slate["season"].unique().to_list()
    record = lake.read("feature_builds", seasons=[season]).filter(pl.col("game_date") == game_date)
    target_rows = {
        name: lake.read(name, seasons=[season]).filter(pl.col("game_date") == game_date)
        for name in lf.TARGET_TABLES
    }
    quotes, failed = lp.day_quotes(store, decision.date())
    for problem in failed:
        typer.echo(f"odds snapshot left out, it did not parse: {problem}", err=True)
    # Model inputs are cut at the decision snapshot, when the price bet was observed.
    cutoff = lp.input_cutoff(quotes, decision)
    problems = lp.build_problems(record, slate, cutoff, target_rows, committed=not dry_run)
    for problem in problems:
        typer.echo(problem, err=True)
    games = lake.read("games")
    fitted = None
    read: tuple[b2.Tables, b3.Tables, uncertainty.Tables] | None = None
    if not problems:
        tables = b2.Tables(
            games,
            *(
                lake.read(name)
                for name in (
                    "team_strength",
                    "schedule_terms",
                    "goalie_starts",
                    "goalie_effects",
                    "actual_lineups",
                )
            ),
        )
        b3_tables = _b3_tables(lake, tables)
        u_tables = uncertainty.Tables(
            games,
            tables.goalie_starts,
            b3_tables.lineups,
            b3_tables.lineup_replacements,
            tables.actual_lineups,
            lake.read("player_league_seasons"),
        )
        moments = slate.select("game_id", prediction_utc=pl.lit(cutoff))
        fitted = lp.models(tables, b3_tables, u_tables, slate, moments, live.fold_start)
        read = (tables, b3_tables, u_tables)
    if r2:
        # The ledgers in R2 are the record: the lake's copy is rebuilt from them first, so a day
        # whose lake copy failed after its R2 write still counts in the bankroll.
        assert lake.objects is not None and lake.bucket is not None
        synced = lp.sync_ledgers(lake.objects, lake.bucket, season)
        if synced.height and not dry_run:
            lake.replace_dates("paper_ledger", synced, synced["game_date"].unique().to_list())
        earlier = synced.filter(pl.col("game_date") < game_date)
    else:
        earlier = lake.read("paper_ledger", seasons=[season]).filter(
            pl.col("game_date") < game_date
        )
    inputs = lp.Day(
        day=game_date,
        decision_utc=decision,
        slate=slate,
        quotes=quotes,
        fitted=fitted,
        live=live,
        bankroll=lp.bankroll(
            earlier,
            games.select("game_id", played_utc="start_utc").join(outcomes(games), on="game_id"),
            decision,
        ),
        versions={
            "blend_version": live.version,
            "feature_build": record["build_id"].first() if record.height else None,
            "code_version": code_version,
            "b2_train_cutoff": fitted.b2_cutoff if fitted else None,
            "b3_train_cutoff": fitted.b3_cutoff if fitted else None,
        },
    )

    def clock() -> datetime:
        """The publication clock (#170): the actual one in a real run; a dry run with --at shifts
        it by the run's own time since its start."""
        return decision + (datetime.now(UTC) - started)

    # The run bundle's rows and fits (#171) go first, each written once (#188): a run that stops
    # after its ledger leaves them for nhl live bundle --finish. One try, so a slow store never
    # holds up the ledger; the write after it tries again. Files of an earlier run with other
    # bytes stop the run before its ledger, which they could never reproduce.
    fits = {}
    if fitted is not None and fitted.b2_model is not None and fitted.b3_model is not None:
        fits = {"b2": fitted.b2_model, "b3": fitted.b3_model}
    rows = lb.inputs(slate, record, quotes, read)
    if dry_run:
        bundles: lb.Store = lb.LocalStore(out)
    else:
        assert lake.objects is not None and lake.bucket is not None
        bundles = lb.R2Store(lake.objects, lake.bucket)
    try:
        failed = lb.write_files_first(bundles, game_date, rows, fits)
    except lb.Conflict as exc:
        typer.echo(
            f"{exc}: an earlier run wrote {game_date}'s run bundle files from other inputs, and "
            "a ledger published over them could never be replayed, so nothing is published "
            "(docs/data-sources.md, run bundles)",
            err=True,
        )
        raise typer.Exit(code=1) from None
    if failed is not None:
        typer.echo(f"run bundle files not written before the ledger ({failed})", err=True)

    if dry_run:
        try:
            ledger = lp.publish(inputs, clock)
        except ValueError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1) from None
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"{game_date.isoformat()}.parquet"
        ledger.write_parquet(path)
        where = str(path)
    else:
        assert lake.objects is not None and lake.bucket is not None
        try:
            # Written once: a second run, or a late one, can't replace the day (ADR 0033).
            ledger, key = lp.write_published(lake.objects, lake.bucket, inputs, clock)
        except Exception as exc:
            typer.echo(f"the {game_date} ledger was not written ({exc}): never rewritten", err=True)
            raise typer.Exit(code=1) from None
        where = f"R2 {key}"
        lake.replace_dates("paper_ledger", ledger, [game_date])
    counts = ledger.group_by("status").len().sort("status").iter_rows()
    typer.echo(f"{where}: " + ", ".join(f"{n} {status}" for status, n in counts))
    for row in ledger.filter(pl.col("bet").fill_null(False)).iter_rows(named=True):
        typer.echo(
            f"  bet {row['away']} at {row['home']}: {row['side']} at {row['price']:.2f}, "
            f"EV {row['ev']:+.3f} (hurdle {row['hurdle']:.3f}), stake {row['stake']:.2f}"
        )
    # The day's run bundle (#171), completed once after the ledger, which stands whatever happens
    # here: its manifest of hashes, which names the publication time.
    try:
        lock = Path("uv.lock")
        identity = {
            "day": game_date.isoformat(),
            "decision_utc": decision.isoformat(),
            "published_utc": ledger["published_utc"][0].isoformat(),
            "input_cutoff": cutoff.isoformat(),
            "bankroll": inputs.bankroll,
            "policy_version": POLICY_VERSION,
            "blend_version": live.version,
            "live_fit": paths[0].name,
            "live_fit_sha256": lb.sha256(paths[0].read_bytes()),
            "feature_build": inputs.versions["feature_build"],
            "code_version": code_version,
            "uv_lock_sha256": lb.sha256(lock.read_bytes()) if lock.exists() else None,
            "ledger": where,
        }
        raw = lp.raw_responses(store, decision.date())
        # A write that failed part way is run again, after a pause that rides out a brief R2
        # outage: what is already there with the same bytes counts as written.
        written = ""
        for attempt in range(4):
            try:
                written = lb.write_once(bundles, game_date, rows, fits, identity, raw)
                break
            except lb.Conflict:
                raise
            except Exception as exc:
                if attempt == 3:
                    raise
                typer.echo(f"run bundle write failed ({exc}); trying again", err=True)
                time.sleep(15 * (attempt + 1))
    except Exception as exc:
        typer.echo(
            f"the {game_date} run bundle was not written ({exc}); the ledger stands", err=True
        )
        raise typer.Exit(code=1) from None
    typer.echo(f"run bundle: {written}")


@app.command()
def backtest(
    seasons: Annotated[
        str, typer.Option(help="Comma-separated seasons as 20182019.")
    ] = DEFAULT_BACKTEST_SEASONS,
    out: Annotated[Path, typer.Option(help="Report directory.")] = DEFAULT_BACKTEST_OUT,
    hockey_only: Annotated[
        bool,
        typer.Option(
            "--hockey-only",
            help=(
                "Score B2 and B3 at the as-of time on outcomes alone, for seasons without prices: "
                "the open seasons and the hockey validation seasons 2023-24 and 2024-25."
            ),
        ),
    ] = False,
    one_time_test: Annotated[
        bool,
        typer.Option(
            "--one-time-test",
            help=(
                "With --hockey-only, gate 2's one-time test on 2025-26 (#107): it runs once, and a "
                "second run is refused."
            ),
        ),
    ] = False,
    market_validation: Annotated[
        bool,
        typer.Option(
            "--market-validation",
            help=(
                "Phase 4's one run on 2022-23's 342 SBR-priced games, after the freeze (#145, ADR "
                "0025): it is claimed before anything is scored, and a second run is refused."
            ),
        ),
    ] = False,
) -> None:
    """Run the walk-forward backtest on the SBR archive, for E1 (the close) and E2 (the opener):
    B0 under each de-vig method, B1 fitted per season on the earlier seasons' prices, B2, the
    team and goalie model (ADR 0013), and B3, the player layer (ADR 0023), each with its
    calibration, its gaps to B1 above 8 points and its lineup quality, and B3 against B2 overall
    and on gate 2's subsets. E2 refuses implausible openers (ADR 0007), and E2 on every opener is
    reported beside it, as is the diagnostic of SBR's change of closing book (#65). The folds the
    market blend learns from (2018-19 up to the season before the latest tested one, #138) are
    predicted too; a fold not tested gets counts only, under training_folds. With
    --hockey-only, B2 and B3 alone, at the as-of time on outcomes, to
    <out>/hockey-<version>.json, logged in <out>/runs.csv."""
    from datetime import UTC

    import polars as pl

    from nhl_edge.backtest import (
        b2_report,
        b3_report,
        bets,
        book_era,
        e3,
        one_time,
        reports,
        sensitivity,
        subsets,
        walk_forward,
    )
    from nhl_edge.backtest import blend as blend_backtest
    from nhl_edge.backtest.seasons import (
        HOCKEY_ROLES,
        MARKET_VALIDATION_SEASONS,
        ONE_TIME_SEASONS,
        OPEN_ROLES,
        OPEN_SEASONS,
        SeasonRole,
        blend_training_seasons,
        season_role,
    )
    from nhl_edge.game import b2, b3, uncertainty
    from nhl_edge.ingest.games import EXPECTED_GAMES
    from nhl_edge.ingest.nhl_ingest import parse_seasons
    from nhl_edge.ingest.sbr import SEASON_PAGES
    from nhl_edge.ingest.sbr_suspect import load_suspect_openers
    from nhl_edge.lake.tables import LAKE_DIR, Lake
    from nhl_edge.lineup import minutes as mins
    from nhl_edge.settings import MissingSettingError, load_env

    # The hockey-only mode also scores the hockey validation seasons, opened at gate 2 (#107),
    # and, once, the one-time test season.
    roles = HOCKEY_ROLES if hockey_only else OPEN_ROLES
    where = None
    if one_time_test:
        if not hockey_only:
            raise typer.BadParameter("needs --hockey-only", param_hint="--one-time-test")
        if sorted(set(parse_seasons(seasons))) != list(ONE_TIME_SEASONS):
            raise typer.BadParameter(
                f"the one-time test scores {list(ONE_TIME_SEASONS)} alone", param_hint="--seasons"
            )
        load_env()
        try:
            where = one_time.places(LAKE_DIR, (DEFAULT_BACKTEST_OUT, out))
        except MissingSettingError as exc:
            raise typer.BadParameter(
                f"the one-time test is claimed in R2: {exc}", param_hint="--one-time-test"
            ) from None
        earlier = one_time.records(where)
        if earlier:
            raise typer.BadParameter(
                f"the one-time test already ran: {'; '.join(earlier)} (docs/plan.md section 5)",
                param_hint="--one-time-test",
            )
        roles = roles | {SeasonRole.ONE_TIME_TEST}
    if market_validation:
        if hockey_only:
            raise typer.BadParameter("prices the season", param_hint="--market-validation")
        if sorted(set(parse_seasons(seasons))) != list(MARKET_VALIDATION_SEASONS):
            raise typer.BadParameter(
                f"the market validation run scores {list(MARKET_VALIDATION_SEASONS)} alone",
                param_hint="--seasons",
            )
        if reports.version(MARKET_VALIDATION_RUN, datetime.now(UTC)).endswith("-dirty"):
            raise typer.BadParameter(
                "commit first: the one run must be reproducible from its commit (ADR 0025)",
                param_hint="--market-validation",
            )
        load_env()
        try:
            where = one_time.places(
                LAKE_DIR, (DEFAULT_BACKTEST_OUT, out), one_time.MARKET_VALIDATION
            )
        except MissingSettingError as exc:
            raise typer.BadParameter(
                f"the market validation run is claimed in R2: {exc}",
                param_hint="--market-validation",
            ) from None
        earlier = one_time.records(where)
        if earlier:
            raise typer.BadParameter(
                f"the market validation run already ran: {'; '.join(earlier)} (ADR 0025)",
                param_hint="--market-validation",
            )
        roles = roles | {SeasonRole.MARKET_VALIDATION}
    try:
        wanted = sorted(set(parse_seasons(seasons)))
        held_out = [season for season in wanted if season_role(season) not in roles]
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--seasons") from None
    if held_out:
        scope = (
            "the hockey-only mode adds 2023-24 and 2024-25 (gate 2, #107)"
            if hockey_only
            else "a backtest runs on training and development seasons only"
        )
        raise typer.BadParameter(
            f"{held_out} are held out: {scope} until their phase (docs/plan.md section 5, #10)",
            param_hint="--seasons",
        )
    lake = Lake()
    if hockey_only:
        _hockey_backtest(lake, wanted, out, where)
        return
    # B1 is fitted on every open SBR season before the test season, so those need prices too.
    history = [s for s in SEASON_PAGES if s in OPEN_SEASONS]
    first = [season for season in wanted if season <= min(history)]
    if first:
        raise typer.BadParameter(
            f"{first} have no earlier SBR season to fit B1 on", param_hint="--seasons"
        )
    # Each tested season's market blend learns from the out-of-sample predictions of the folds
    # before it (#138), so those folds are predicted too, and kept out of the test metrics.
    learned = {s for season in wanted for s in blend_training_seasons(season)}
    training_only = sorted(learned - set(wanted))
    folds = sorted(set(wanted) | set(training_only))
    needed = set(folds) | {s for s in history if s < max(folds)}
    sbr_odds = lake.read("sbr_odds").filter(pl.col("season").is_in(needed))
    missing = sorted(needed - set(sbr_odds["season"].unique().to_list()))
    if missing:
        typer.echo(f"no SBR prices for {missing} in the lake: run nhl odds sbr", err=True)
        raise typer.Exit(code=1)
    games = lake.read("games")
    # A season short of results would be scored, or B1 fitted, on a subset of its games.
    short = {
        season: height
        for season in sorted(needed)
        if (height := games.filter(pl.col("season") == season).height) != EXPECTED_GAMES[season]
    }
    if short:
        counts = ", ".join(f"{n:,} of {EXPECTED_GAMES[s]:,} in {s}" for s, n in short.items())
        typer.echo(f"games has {counts}: run nhl ingest for those seasons", err=True)
        raise typer.Exit(code=1)
    # B2 (ADR 0013) reads the feature tables, which cover every game from 2011-12.
    tables = b2.Tables(
        games,
        *(
            lake.read(name)
            for name in (
                "team_strength",
                "schedule_terms",
                "goalie_starts",
                "goalie_effects",
                "actual_lineups",
            )
        ),
    )
    # B3 (ADR 0023) reads the player layer's tables, which cover every game from 2011-12.
    b3_tables = _b3_tables(lake, tables)
    problems = b2.input_problems(tables, max(wanted), EXPECTED_GAMES)
    problems += b3.input_problems(b3_tables, max(wanted))
    if problems:
        for problem in problems:
            typer.echo(problem, err=True)
        typer.echo("run the feature commands for those seasons", err=True)
        raise typer.Exit(code=1)
    every_b2_fit: dict[str, dict[int, b2.B2Model]] = {}
    every_b3_fit: dict[str, dict[int, b3.B3Model]] = {}
    now = datetime.now(UTC)
    run_version = reports.version(MARKET_VALIDATION_RUN if market_validation else "backtest", now)
    validation_places = where if market_validation else None
    try:
        every_prediction, every_coverage, every_fit = walk_forward.run(
            sbr_odds,
            games,
            folds,
            b2_tables=tables,
            b2_fits=every_b2_fit,
            b3_tables=b3_tables,
            b3_fits=every_b3_fit,
            validated=wanted if market_validation else (),
            # Claimed before anything is scored: a failed run is never rerun (ADR 0025).
            claim=(
                None
                if validation_places is None
                else lambda: one_time.claim(validation_places, run_version)
            ),
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--seasons") from None
    predictions, coverage, fits = walk_forward.tests_only(
        every_prediction, every_coverage, every_fit, wanted
    )
    b2_fits = {e: {s: f for s, f in by.items() if s in wanted} for e, by in every_b2_fit.items()}
    b3_fits = {e: {s: f for s, f in by.items() if s in wanted} for e, by in every_b3_fit.items()}
    # The uncertainty score's parts (#139, ADR 0026) for every game B3 predicted, at each
    # experiment's prediction time: the blend's input, reported here as inputs only.
    u_tables = uncertainty.Tables(
        games,
        tables.goalie_starts,
        b3_tables.lineups,
        b3_tables.lineup_replacements,
        tables.actual_lineups,
        lake.read("player_league_seasons"),
    )
    u_parts = {
        experiment: uncertainty.parts(
            u_tables,
            every_prediction.filter(experiment=experiment, model="B3").select(
                "game_id", "prediction_utc"
            ),
        ).join(games.select("game_id", "season"), on="game_id")
        for experiment in sorted(every_coverage)
    }
    # The market blend (#140, ADR 0027): each tested season's fits learn from the folds before it.
    blend_input = blend_backtest.rows(every_prediction, u_parts, games)
    try:
        blend_predictions, blend_fits, blend_scales, blend_coverage = blend_backtest.run(
            blend_input, walk_forward.fold_starts(sbr_odds, games, wanted), wanted
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--seasons") from None
    predictions = pl.concat([predictions, blend_predictions])
    report = reports.summary(predictions, coverage, fits, wanted, run_version, now)
    report["training_folds"] = walk_forward.training_folds(
        every_coverage, every_b2_fit, every_b3_fit, training_only
    )
    report["uncertainty"] = {
        experiment: {
            str(season): uncertainty.spread(rows)
            for (season,), rows in frame.sort("season").group_by("season", maintain_order=True)
        }
        for experiment, frame in u_parts.items()
    }
    report = b2_report.add(
        report,
        predictions,
        b2_fits,
        games,
        tables.goalie_starts,
        tables.actual_lineups,
    )
    # Gate 2's subsets, fixed from boxscores and the projection (ADR 0023).
    flags = subsets.flags(games, tables.actual_lineups, b3_tables.lineups, wanted)
    scored_boxscores = tables.actual_lineups.filter(pl.col("season").is_in(wanted))
    report = b3_report.add(
        report,
        predictions,
        b3_fits,
        games,
        tables.goalie_starts,
        tables.actual_lineups,
        b3_tables.lineups,
        mins.lake_minutes(lake, scored_boxscores, max(wanted)),
        flags,
    )
    report = blend_backtest.add(
        report, predictions, blend_fits, blend_scales, blend_coverage, blend_input, games
    )
    # The policy's bets on E2's blend at the opener (#141, ADR 0028).
    picked, settled = bets.ledger(predictions, blend_input, blend_scales, sbr_odds, games)
    report["bets"] = bets.report(picked, settled)
    # The market move guard (#142, ADR 0029): its frozen threshold, and how often it would fire.
    report["bets"]["guard"] = bets.guard_report(settled, sbr_odds)
    # E3 on history (#143): the bets' closing line value against SBR's close, and each bet's
    # driver among the blend's parts.
    e2 = bets.EXPERIMENT
    # Each input part is measured against its usual level at the market price, learnt on the
    # earlier blend-training seasons with their own folds' fits (#154).
    settled = e3.attribution(
        e3.closing_value(settled, e3.sbr_closes(sbr_odds)),
        b3_tables,
        every_b3_fit.get(e2, {}),
        {season: by_name["BLEND"] for season, by_name in blend_fits.get(e2, {}).items()},
        e3.market_inputs(blend_input, blend_scales.get(e2, {}), e2),
        e3.market_history(blend_input, e2),
    )
    groups = blend_backtest.groups(blend_input, games).filter(pl.col("experiment") == e2)
    report["e3"] = e3.report(settled, groups.drop("experiment"))
    report["e3"]["sensitivity_without_suspect_openers"] = e3.without_suspects(
        settled, load_suspect_openers()["game_id"], groups.drop("experiment")
    )
    files = out
    if market_validation:
        # Its own report and files, so the development run's summary.json stays as it is.
        report["market_validation"] = {
            "adr": "0025",
            "run_once": True,
            "note": "342 games of October and November 2022, priced by SBR: B3's ratings then "
            "rest mostly on the season before, and the intervals are wide (ADR 0025)",
        }
        path = reports.write(report, out, f"{run_version}.json")
        files = out / run_version
        files.mkdir(parents=True, exist_ok=True)
    else:
        report["sensitivity"] = sensitivity.every_opener(sbr_odds, games, wanted)
        report["diagnostics"] = {"book_era": book_era.diagnostic(sbr_odds, games)}
        path = reports.write(report, out)
    b2_report.write_gaps(predictions, games, files)
    b3_report.write_gaps(predictions, games, files)
    blend_backtest.write_gaps(predictions, games, files)
    bets.write_ledger(settled, games, files, report["version"])
    typer.echo(f"{path}: {report['version']}")
    for experiment, folds_by_season in report["training_folds"]["folds"].items():
        for season, counts in folds_by_season.items():
            typer.echo(
                f"  {experiment} {season}, the blend's training fold only: "
                f"B2 {counts['b2_scored']:,} and B3 {counts['b3_scored']:,} games predicted"
            )
    parts = [(name, body) for name, body in report["experiments"].items()]
    parts += [(f"E2 {name}", body["E2"]) for name, body in report.get("sensitivity", {}).items()]
    for experiment, body in parts:
        for model, results in body["models"].items():
            for method, estimates in results["log_loss"].items():
                pooled = estimates["pooled"]
                typer.echo(
                    f"  {experiment} {model} {method}: log loss {pooled['mean']:.4f} "
                    f"[{pooled['low']:.4f}, {pooled['high']:.4f}] over {pooled['games']:,} games"
                )
    for season, placed in report["bets"]["per_season"].items():
        typer.echo(
            f"  E2 bets {season}: {placed['bets']:,} of {placed['games']:,} games, "
            f"{placed['staked']:.1f} units staked, profit {placed['profit']:+.1f}"
        )
    for season, valued in report["e3"]["per_season"].items():
        clv = valued["clv_per_bet"]
        typer.echo(
            f"  E3 {season}: CLV per bet {clv['mean']:+.4f} [{clv['low']:+.4f}, "
            f"{clv['high']:+.4f}] over {clv['games']:,} bets against SBR's close"
        )
    eras = report.get("diagnostics", {}).get("book_era", {"b0_e2_minus_e1": {"eras": {}}})
    for name, cost in eras["b0_e2_minus_e1"]["eras"].items():
        gain = eras["b0_minus_b1"]["E1"]["eras"][name]
        typer.echo(
            f"  {name}: B0 E2 minus E1 {cost['mean']:+.4f} [{cost['low']:+.4f}, "
            f"{cost['high']:+.4f}]; E1 B0 minus B1 {gain['mean']:+.4f} [{gain['low']:+.4f}, "
            f"{gain['high']:+.4f}] over {gain['games']:,} games"
        )


def _check_blend_gaps(
    rows: "pl.DataFrame",
    gaps: Path,
    games: "pl.DataFrame",
    sbr_odds: "pl.DataFrame",
    tables: "b2.Tables",
    b3_tables: "b3.Tables",
    lake: "Lake",
) -> None:
    """Recompute each blend gap's probability from its fold's fit, recorded in the run's report
    (b3_gaps.run_summary, #156), from the market's de-vigged probability and u's parts at the
    gap's prediction time. Raises when one doesn't follow."""
    import json

    import polars as pl

    from nhl_edge.audit import b3_gaps
    from nhl_edge.backtest.market import Experiment, market_prices
    from nhl_edge.backtest.walk_forward import B1_METHOD, b0
    from nhl_edge.game import uncertainty

    if rows.is_empty():
        return
    fits = b3_gaps.fold_blends(json.loads(b3_gaps.run_summary(gaps).read_text()))
    u_tables = uncertainty.Tables(
        games,
        tables.goalie_starts,
        b3_tables.lineups,
        b3_tables.lineup_replacements,
        tables.actual_lineups,
        lake.read("player_league_seasons"),
    )
    markets, doubts = [], []
    for (experiment,), part in rows.group_by("experiment", maintain_order=True):
        named = pl.lit(str(experiment)).alias("experiment")
        priced = b0(market_prices(sbr_odds, Experiment(str(experiment))), B1_METHOD)
        markets.append(priced.select(named, "game_id", p_mkt="p_home"))
        moments = part.select("game_id", pl.col("prediction_utc").cast(pl.Datetime("us", "UTC")))
        doubts.append(
            uncertainty.parts(u_tables, moments).select(named, "game_id", *uncertainty.PARTS)
        )
    b3_gaps.blend_check(rows, pl.concat(markets), pl.concat(doubts), fits)


def _b3_tables(lake: "Lake", tables: "b2.Tables") -> "b3.Tables":
    """B3's tables (ADR 0023), sharing B2's schedule terms, goalie starts and boxscores."""
    from nhl_edge.game.b3 import Tables

    return Tables(
        games=tables.games,
        schedule_terms=tables.schedule_terms,
        goalie_starts=tables.goalie_starts,
        actual_lineups=tables.actual_lineups,
        lineups=lake.read("lineups"),
        lineup_replacements=lake.read("lineup_replacements"),
        player_ratings=lake.read("player_ratings"),
        rapm_terms=lake.read("rapm_terms"),
        expected_power_plays=lake.read("expected_power_plays"),
        goal_multipliers=lake.read("goal_multipliers"),
    )


def _hockey_backtest(
    lake: "Lake", wanted: list[int], out: Path, one_time_places: "Places | None" = None
) -> None:
    """B2 and B3 at the as-of time on outcomes alone (ADR 0023), to <out>/hockey-<version>.json,
    with every run logged in <out>/runs.csv."""
    import json
    from datetime import UTC

    from nhl_edge.backtest import b3_report, one_time, reports, subsets, walk_forward
    from nhl_edge.game import b2, b3
    from nhl_edge.ingest.games import EXPECTED_GAMES

    games = lake.read("games")
    tables = b2.Tables(
        games,
        *(
            lake.read(name)
            for name in (
                "team_strength",
                "schedule_terms",
                "goalie_starts",
                "goalie_effects",
                "actual_lineups",
            )
        ),
    )
    b3_tables = _b3_tables(lake, tables)
    problems = b2.input_problems(tables, max(wanted), EXPECTED_GAMES)
    problems += b3.input_problems(b3_tables, max(wanted))
    if problems:
        for problem in problems:
            typer.echo(problem, err=True)
        typer.echo("run the feature commands for those seasons", err=True)
        raise typer.Exit(code=1)
    b2_fits: dict[str, dict[int, b2.B2Model]] = {}
    b3_fits: dict[str, dict[int, b3.B3Model]] = {}
    now = datetime.now(UTC)
    run_version = reports.version("backtest-hockey", now)
    try:
        predictions, coverage = walk_forward.hockey_only(
            games,
            wanted,
            tables,
            b3_tables,
            b2_fits=b2_fits,
            b3_fits=b3_fits,
            # The one-time test is claimed before it scores (one_time.claim).
            one_time=(
                None
                if one_time_places is None
                else lambda: one_time.claim(one_time_places, run_version)
            ),
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--seasons") from None
    flags = subsets.flags(games, tables.actual_lineups, b3_tables.lineups, wanted)
    report = b3_report.hockey(
        predictions, coverage, b3_fits, flags, wanted, run_version, now, b2_fits=b2_fits
    )
    # One file per run, and every run logged: held-out seasons must not be rerun unseen.
    out.mkdir(parents=True, exist_ok=True)
    path = out / b3_report.hockey_file(report)
    with path.open("x") as handle:  # never over an earlier run's report
        handle.write(json.dumps(report, indent=2) + "\n")
    reports.log_runs(b3_report.hockey_runs(report), out)
    typer.echo(f"{path}: {report['version']}")
    for model, body in report["models"].items():
        pooled = body["log_loss"]["pooled"]
        spread = (
            "no interval: one week a season"
            if pooled["low"] is None
            else f"[{pooled['low']:.4f}, {pooled['high']:.4f}]"
        )
        typer.echo(
            f"  {model}: log loss {pooled['mean']:.4f} {spread} over {pooled['games']:,} games"
        )


@app.command()
def xg(
    seasons: Annotated[
        str | None,
        typer.Option(
            help="Seasons to score, as 20232024, a comma list or a range. Default: 2011-12 on."
        ),
    ] = None,
    out: Annotated[Path, typer.Option(help="Report directory.")] = DEFAULT_XG_OUT,
    r2: Annotated[bool, typer.Option("--r2", help="Mirror the shot_xg table to R2.")] = False,
) -> None:
    """Fit the xG model per season on earlier seasons' shots (#73, ADR 0010), write every scored
    shot's xG to the lake's shot_xg, and the calibration report to <out>/<version>.md: figures
    per open season but the development seasons, which wait for gate 1, and group tables over the
    training seasons only."""
    from datetime import UTC

    import polars as pl

    from nhl_edge.audit import xg as xg_report
    from nhl_edge.backtest import reports
    from nhl_edge.backtest.seasons import DEVELOPMENT_SEASONS, OPEN_SEASONS, TRAINING_SEASONS
    from nhl_edge.features import xg as xg_model
    from nhl_edge.ingest.games import EXPECTED_GAMES
    from nhl_edge.ingest.nhl_ingest import parse_seasons
    from nhl_edge.lake.tables import Lake
    from nhl_edge.settings import load_env

    load_env()
    lake = Lake.from_env(mirror=r2)
    shots, games = lake.read("shots"), lake.read("games")
    known = sorted(games["season"].unique().to_list())
    try:
        wanted = (
            parse_seasons(seasons) if seasons else [s for s in known if s >= xg_model.FIRST_SEASON]
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--seasons") from None
    # A fit on a season short of its games or shots would be biased with nothing to show for it.
    problems = xg_model.input_problems(shots, games, wanted, EXPECTED_GAMES) if wanted else []
    if problems:
        for problem in problems:
            typer.echo(problem, err=True)
        typer.echo("run nhl ingest --replay for those seasons", err=True)
        raise typer.Exit(code=1)
    try:
        now = datetime.now(UTC)
        version = reports.version(xg_model.COMPONENT, now)
        scored, models = xg_model.score(shots, games, wanted, version)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--seasons") from None
    days = games.filter(pl.col("season").is_in(wanted))["game_date"].unique().to_list()
    lake.replace_dates("shot_xg", scored, days)
    # The development seasons stay unseen until gate 1 (phase 2 plan).
    shown = [season for season in OPEN_SEASONS if season not in DEVELOPMENT_SEASONS]
    report = xg_report.markdown_report(
        xg_report.scored_shots(scored, shots), models, shown, TRAINING_SEASONS, version
    )
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{version}.md"
    path.write_text(report)
    typer.echo(f"{path}: {scored.height:,} shots scored in {len(models)} seasons")
    for model in models:
        typer.echo(
            f"  {model.season}: fitted on {model.shots:,} shots to {model.train_cutoff:%Y-%m-%d}"
        )


@app.command()
def stints(
    seasons: Annotated[
        str | None,
        typer.Option(
            help="Seasons to build, as 20232024, a comma list or a range. Default: every season."
        ),
    ] = None,
    r2: Annotated[bool, typer.Option("--r2", help="Mirror the stints table to R2.")] = False,
) -> None:
    """Cut every game with a complete shift chart into stints (#97, ADR 0015) in the lake's
    stints: the stretches with the same players on the ice, each with its strength, the score and
    the faceoff zone at its start, and each team's xG and goals. A stint with an impossible count
    keeps a drop_reason. Run it after nhl xg, whose xG it reads."""
    import polars as pl

    from nhl_edge.backtest.seasons import OPEN_SEASONS
    from nhl_edge.features import stints as st
    from nhl_edge.ingest.nhl_ingest import parse_seasons
    from nhl_edge.lake.tables import Lake
    from nhl_edge.settings import load_env

    load_env()
    lake = Lake.from_env(mirror=r2)
    games = lake.read("games")
    known = sorted(games["season"].unique().to_list())
    try:
        wanted = parse_seasons(seasons) if seasons else known
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--seasons") from None
    problems = st.input_problems(games, lake.read("shift_coverage"), lake.read("shot_xg"), wanted)
    if problems:
        for problem in problems:
            typer.echo(problem, err=True)
        typer.echo("run nhl ingest --replay and nhl xg for those seasons", err=True)
        raise typer.Exit(code=1)
    for season in wanted:
        tables = {
            name: lake.read(name, [season])
            for name in (
                "shift_coverage",
                "shifts",
                "actual_lineups",
                "shots",
                "shot_xg",
                "faceoffs",
            )
        }
        try:
            frame = st.build(
                tables["shift_coverage"],
                tables["shifts"],
                tables["actual_lineups"],
                tables["shots"],
                tables["shot_xg"],
                tables["faceoffs"],
                games.filter(pl.col("season") == season).select("season", "start_utc"),
            )
        except ValueError as exc:
            typer.echo(f"{season}: {exc}", err=True)
            raise typer.Exit(code=1) from None
        dates = games.filter(pl.col("season") == season)["game_date"].unique().to_list()
        lake.replace_dates("stints", frame, dates)
        # Counts of a held-out or live season stay unseen, as in the audit report.
        if season not in OPEN_SEASONS:
            typer.echo(f"stints {season}: written")
            continue
        dropped = frame.filter(pl.col("drop_reason").is_not_null()).height
        typer.echo(
            f"stints {season}: {frame.height:,} in {frame['game_id'].n_unique():,} games, "
            f"{dropped:,} left out of RAPM"
        )


@app.command("team-strength")
def team_strength(
    seasons: Annotated[
        str | None,
        typer.Option(
            help="Seasons to rate, as 20232024, a comma list or a range. Default: 2011-12 on."
        ),
    ] = None,
    tune: Annotated[
        bool,
        typer.Option(
            "--tune", help="Run the tuning grid on the training seasons and log it; write no table."
        ),
    ] = False,
    out: Annotated[Path, typer.Option(help="Tuning report directory.")] = DEFAULT_TUNING_OUT,
    r2: Annotated[bool, typer.Option("--r2", help="Mirror the team_strength table to R2.")] = False,
    targets: TargetsOption = None,
) -> None:
    """Rate every game's rolling team strength ΔS with the frozen settings (#74, ADR 0011) into the
    lake's team_strength. With --tune, score the 16 candidate settings on the training seasons
    instead and write the log to <out>/team-strength-<version>.md. With --targets, also rate that
    date's slate games (#162)."""
    from datetime import UTC

    import polars as pl

    from nhl_edge.backtest import reports, tuning
    from nhl_edge.features import team_strength as ts
    from nhl_edge.ingest.games import EXPECTED_GAMES
    from nhl_edge.ingest.nhl_ingest import parse_seasons
    from nhl_edge.lake.tables import Lake
    from nhl_edge.live.targets import with_targets
    from nhl_edge.settings import load_env

    load_env()
    lake = Lake.from_env(mirror=r2)
    games = lake.read("games")
    known = sorted(games["season"].unique().to_list())
    try:
        wanted = parse_seasons(seasons) if seasons else [s for s in known if s >= ts.FIRST_SEASON]
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--seasons") from None
    early = [season for season in wanted if season < ts.FIRST_SEASON]
    if early and not tune:
        raise typer.BadParameter(
            f"{early} have no xG, so no team strength: it starts with {ts.FIRST_SEASON}",
            param_hint="--seasons",
        )
    last = max(ts.TUNING_SEASONS) if tune else max(wanted)
    shot_xg, strength_time = lake.read("shot_xg"), lake.read("strength_time")
    problems = ts.input_problems(games, shot_xg, strength_time, last, EXPECTED_GAMES)
    if problems:
        for problem in problems:
            typer.echo(problem, err=True)
        typer.echo("run nhl ingest --replay and nhl xg for those seasons", err=True)
        raise typer.Exit(code=1)
    slate = _slate_targets(lake, targets, wanted, tune)
    history = ts.team_games(lake.read("shots"), shot_xg, strength_time)
    version = reports.version(ts.COMPONENT, datetime.now(UTC))
    if tune:
        rated = games.filter(pl.col("season").is_between(ts.FIRST_SEASON, last))
        candidates = [
            tuning.Candidate(
                settings,
                settings.label,
                tuning.scored_games(
                    ts.strength(rated, history, settings).select("game_id", x="delta_s"),
                    games,
                    ts.TUNING_SEASONS,
                ),
            )
            for settings in ts.GRID
        ]
        choice = tuning.choose(candidates, ts.steadiness)
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"{version}.md"
        path.write_text(tuning.markdown(choice, "team strength", version, ts.TUNING_SEASONS))
        frozen = "matches" if choice.chosen == ts.TUNED else "differs from"
        typer.echo(f"{path}: chose {choice.chosen.label}, which {frozen} the frozen TUNED")
        return
    if slate is not None:
        games = with_targets(games, slate)
    try:
        rated = games.filter(pl.col("season").is_in(wanted))
        if rated.is_empty():
            raise ValueError(f"no games of {wanted} in the lake")
        frame = ts.rows(rated, history, ts.TUNED, version)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--seasons") from None
    lake.replace_dates("team_strength", frame, rated["game_date"].unique().to_list())
    typer.echo(f"team_strength: {frame.height:,} games rated with {ts.TUNED.label} ({version})")


@app.command("goalie-effect")
def goalie_effect(
    seasons: Annotated[
        str | None,
        typer.Option(
            help="Seasons to rate, as 20232024, a comma list or a range. Default: 2011-12 on."
        ),
    ] = None,
    tune: Annotated[
        bool,
        typer.Option(
            "--tune", help="Run the tuning grid on the training seasons and log it; write no table."
        ),
    ] = False,
    out: Annotated[Path, typer.Option(help="Tuning report directory.")] = DEFAULT_TUNING_OUT,
    r2: Annotated[
        bool, typer.Option("--r2", help="Mirror the goalie_effects table to R2.")
    ] = False,
    targets: TargetsOption = None,
) -> None:
    """Rate every goalie_starts candidate's effect with the frozen settings (#75, ADR 0011) into
    the lake's goalie_effects. With --tune, score the 16 candidate settings on the training seasons
    instead, by the ΔG expected under the goalie-start probabilities, and write the log to
    <out>/goalie-effect-<version>.md. With --targets, also rate that date's slate games (#162),
    whose goalie_starts rows nhl goalie-start --targets wrote."""
    from datetime import UTC

    import polars as pl

    from nhl_edge.backtest import reports, tuning
    from nhl_edge.features import goalie as ge
    from nhl_edge.ingest.games import EXPECTED_GAMES
    from nhl_edge.ingest.nhl_ingest import parse_seasons
    from nhl_edge.lake.tables import Lake
    from nhl_edge.live.targets import rated, with_targets
    from nhl_edge.settings import load_env

    load_env()
    lake = Lake.from_env(mirror=r2)
    games = lake.read("games")
    known = sorted(games["season"].unique().to_list())
    try:
        wanted = parse_seasons(seasons) if seasons else [s for s in known if s >= ge.FIRST_SEASON]
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--seasons") from None
    early = [season for season in wanted if season < ge.FIRST_SEASON]
    if early and not tune:
        raise typer.BadParameter(
            f"{early} have no xG, so no goalie effect: it starts with {ge.FIRST_SEASON}",
            param_hint="--seasons",
        )
    last = max(ge.TUNING_SEASONS) if tune else max(wanted)
    shots, shot_xg, starts = lake.read("shots"), lake.read("shot_xg"), lake.read("goalie_starts")
    problems = ge.input_problems(games, shot_xg, starts, last, EXPECTED_GAMES)
    if problems:
        for problem in problems:
            typer.echo(problem, err=True)
        typer.echo(
            "run nhl ingest --replay, nhl xg and nhl goalie-start for those seasons", err=True
        )
        raise typer.Exit(code=1)
    slate = _slate_targets(lake, targets, wanted, tune)
    goalies = ge.goalie_games(shots, shot_xg)
    team_shots = ge.team_shot_games(games, shots, shot_xg)
    version = reports.version(ge.COMPONENT, datetime.now(UTC))
    if tune:
        rated_games = games.filter(pl.col("season").is_between(ge.FIRST_SEASON, last))
        candidates = starts.filter(pl.col("season").is_between(ge.FIRST_SEASON, last))
        scored = []
        for settings in ge.GRID:
            rated = ge.effects(candidates, rated_games, goalies, team_shots, settings)
            feature = ge.expected_delta(rated, candidates, rated_games)
            games_scored = tuning.scored_games(feature, games, ge.TUNING_SEASONS)
            scored.append(tuning.Candidate(settings, settings.label, games_scored))
        choice = tuning.choose(scored, ge.steadiness)
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"{version}.md"
        path.write_text(tuning.markdown(choice, "goalie effect", version, ge.TUNING_SEASONS))
        frozen = "matches" if choice.chosen == ge.TUNED else "differs from"
        typer.echo(f"{path}: chose {choice.chosen.label}, which {frozen} the frozen TUNED")
        return
    if slate is not None:
        games = with_targets(games, slate)
    try:
        rated_games = games.filter(pl.col("season").is_in(wanted))
        if rated_games.is_empty():
            raise ValueError(f"no games of {wanted} in the lake")
        candidates = rated(starts.filter(pl.col("season").is_in(wanted)), rated_games)
        rated = ge.effects(candidates, rated_games, goalies, team_shots, ge.TUNED)
        frame = ge.rows(rated, ge.TUNED, version)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--seasons") from None
    lake.replace_dates("goalie_effects", frame, rated_games["game_date"].unique().to_list())
    typer.echo(
        f"goalie_effects: {frame.height:,} candidates rated with {ge.TUNED.label} ({version})"
    )


@app.command("schedule-terms")
def schedule_terms(
    seasons: Annotated[
        str | None,
        typer.Option(
            help="Seasons to rate, as 20232024, a comma list or a range. Default: 2011-12 on."
        ),
    ] = None,
    tune: Annotated[
        bool,
        typer.Option(
            "--tune", help="Run the tuning grid on the training seasons and log it; write no table."
        ),
    ] = False,
    out: Annotated[Path, typer.Option(help="Tuning report directory.")] = DEFAULT_TUNING_OUT,
    r2: Annotated[
        bool, typer.Option("--r2", help="Mirror the schedule_terms table to R2.")
    ] = False,
    targets: TargetsOption = None,
) -> None:
    """Rate every game's rest, travel, open seats and season home edge with the frozen setting
    (#77, ADR 0011) into the lake's schedule_terms. With --tune, score the home edge's candidate
    pulls on the training seasons instead and write the log to <out>/schedule-terms-<version>.md.
    With --targets, also rate that date's slate games (#162)."""
    from datetime import UTC

    import polars as pl

    from nhl_edge.backtest import reports, tuning
    from nhl_edge.features import schedule_terms as st
    from nhl_edge.ingest.games import EXPECTED_GAMES
    from nhl_edge.ingest.nhl_ingest import parse_seasons
    from nhl_edge.lake.tables import Lake
    from nhl_edge.live.targets import with_schedule_targets
    from nhl_edge.settings import load_env

    load_env()
    lake = Lake.from_env(mirror=r2)
    schedule, games = lake.read("schedule"), lake.read("games")
    known = sorted(schedule["season"].unique().to_list())
    try:
        wanted = parse_seasons(seasons) if seasons else [s for s in known if s >= st.FIRST_SEASON]
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--seasons") from None
    early = [season for season in wanted if season < st.FIRST_SEASON]
    if early and not tune:
        raise typer.BadParameter(
            f"{early} have no earlier season for the home edge: it starts with {st.FIRST_SEASON}",
            param_hint="--seasons",
        )
    last = max(st.TUNING_SEASONS) if tune else max(wanted)
    problems = st.input_problems(schedule, games, last, EXPECTED_GAMES)
    if problems:
        for problem in problems:
            typer.echo(problem, err=True)
        typer.echo("run nhl ingest --replay, or fix the reference files", err=True)
        raise typer.Exit(code=1)
    slate = _slate_targets(lake, targets, wanted, tune)
    version = reports.version(st.COMPONENT, datetime.now(UTC))
    if tune:
        rated_seasons = [s for s in known if st.FIRST_SEASON <= s <= last]
        candidates = [
            tuning.Candidate(
                settings,
                settings.label,
                tuning.scored_games(
                    st.tuning_feature(st.terms(schedule, games, settings, rated_seasons)),
                    games,
                    st.TUNING_SEASONS,
                ),
            )
            for settings in st.GRID
        ]
        choice = tuning.choose(candidates, st.steadiness)
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"{version}.md"
        path.write_text(tuning.markdown(choice, "season home edge", version, st.TUNING_SEASONS))
        frozen = "matches" if choice.chosen == st.TUNED else "differs from"
        typer.echo(f"{path}: chose {choice.chosen.label}, which {frozen} the frozen TUNED")
        return
    if slate is not None:
        schedule = with_schedule_targets(schedule, slate)
    try:
        if schedule.filter(pl.col("season").is_in(wanted)).is_empty():
            raise ValueError(f"no games of {wanted} in the schedule")
        frame = st.rows(st.terms(schedule, games, st.TUNED, wanted), st.TUNED, version)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--seasons") from None
    lake.replace_dates("schedule_terms", frame, frame["game_date"].unique().to_list())
    typer.echo(f"schedule_terms: {frame.height:,} games rated with {st.TUNED.label} ({version})")


@app.command("tune-b2")
def tune_b2(
    out: Annotated[Path, typer.Option(help="Tuning report directory.")] = DEFAULT_TUNING_OUT,
) -> None:
    """Score B2's candidate L2 penalties on the training seasons (#78, ADR 0011, ADR 0013), each
    season predicted by a fit on the earlier ones, and write the log to <out>/b2-<version>.md.
    Reads the feature tables from the lake; writes no table."""
    from datetime import UTC

    import polars as pl

    from nhl_edge.backtest import reports, tuning
    from nhl_edge.game import b2
    from nhl_edge.ingest.games import EXPECTED_GAMES
    from nhl_edge.lake.tables import Lake

    lake = Lake()
    tables = b2.Tables(
        *(
            lake.read(name)
            for name in (
                "games",
                "team_strength",
                "schedule_terms",
                "goalie_starts",
                "goalie_effects",
                "actual_lineups",
            )
        )
    )
    last = max(b2.TUNING_SEASONS)
    problems = b2.input_problems(tables, last, EXPECTED_GAMES)
    if problems:
        for problem in problems:
            typer.echo(problem, err=True)
        typer.echo("run the feature commands for those seasons", err=True)
        raise typer.Exit(code=1)
    version = reports.version(b2.COMPONENT, datetime.now(UTC))
    window = pl.col("season") <= last
    tables = b2.Tables(*(frame.filter(window) for frame in tables.__dict__.values()))
    candidates = [
        tuning.Candidate(
            settings, settings.label, b2.tuning_scores(tables, settings, b2.TUNING_SEASONS)
        )
        for settings in b2.GRID
    ]
    choice = tuning.choose(candidates, b2.steadiness)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{version}.md"
    path.write_text(tuning.markdown(choice, "B2", version, b2.TUNING_SEASONS))
    frozen = "matches" if choice.chosen == b2.TUNED else "differs from"
    typer.echo(f"{path}: chose {choice.chosen.label}, which {frozen} the frozen TUNED")


@app.command("goalie-start")
def goalie_start(
    seasons: Annotated[
        str | None,
        typer.Option(
            help="Seasons to score, as 20232024, a comma list or a range. Default: 2011-12 on."
        ),
    ] = None,
    out: Annotated[Path, typer.Option(help="Report directory.")] = DEFAULT_GOALIE_START_OUT,
    r2: Annotated[bool, typer.Option("--r2", help="Mirror the goalie_starts table to R2.")] = False,
    targets: TargetsOption = None,
) -> None:
    """Fit the goalie-start model per season on earlier seasons' boxscores (#76, ADR 0012), write
    each candidate goalie's start probability to the lake's goalie_starts, and the report to
    <out>/<version>.md: figures per open season but the development seasons, which wait for
    gate 1. With --targets, also score that date's slate games (#162)."""
    from datetime import UTC

    import polars as pl

    from nhl_edge.audit import goalie_start as report
    from nhl_edge.backtest import reports
    from nhl_edge.backtest.seasons import DEVELOPMENT_SEASONS, OPEN_SEASONS
    from nhl_edge.ingest.games import EXPECTED_GAMES
    from nhl_edge.ingest.nhl_ingest import parse_seasons
    from nhl_edge.lake.tables import Lake
    from nhl_edge.lineup import goalie_start as gs
    from nhl_edge.live.targets import with_targets
    from nhl_edge.settings import load_env

    load_env()
    lake = Lake.from_env(mirror=r2)
    games, lineups = lake.read("games"), lake.read("actual_lineups")
    known = sorted(games["season"].unique().to_list())
    try:
        wanted = parse_seasons(seasons) if seasons else [s for s in known if s >= gs.FIRST_SEASON]
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--seasons") from None
    problems = gs.input_problems(games, lineups, max(wanted), EXPECTED_GAMES) if wanted else []
    if problems:
        for problem in problems:
            typer.echo(problem, err=True)
        typer.echo("run nhl ingest --replay for those seasons", err=True)
        raise typer.Exit(code=1)
    slate = _slate_targets(lake, targets, wanted)
    if slate is not None:
        games = with_targets(games, slate)
    try:
        version = reports.version(gs.COMPONENT, datetime.now(UTC))
        table, scored, models = gs.score(lineups, games, wanted, version)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--seasons") from None
    days = games.filter(pl.col("season").is_in(wanted))["game_date"].unique().to_list()
    lake.replace_dates("goalie_starts", table, days)
    # The development seasons stay unseen until gate 1 (phase 2 plan).
    shown = [season for season in OPEN_SEASONS if season not in DEVELOPMENT_SEASONS]
    scores = report.team_game_scores(scored, lineups)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{version}.md"
    path.write_text(report.markdown_report(scores, models, shown, version))
    typer.echo(f"{path}: {table.height:,} candidate rows in {len(models)} seasons")
    # A fit's team-games count only starters among the candidates, so they would give a held-out
    # season's missed starters: shown, as in the report, only when every season it read is.
    for fit in report.fits(models, shown):
        size = "held out" if fit["team_games"] is None else f"{fit['team_games']:,} team-games"
        typer.echo(f"  {fit['season']}: fitted on {size} to {fit['train_cutoff'][:10]}")


@app.command("lineups")
def lineups(
    seasons: Annotated[
        str | None,
        typer.Option(
            help="Seasons to score, as 20232024, a comma list or a range. Default: 2011-12 on."
        ),
    ] = None,
    out: Annotated[Path, typer.Option(help="Report directory.")] = DEFAULT_LINEUPS_OUT,
    r2: Annotated[
        bool, typer.Option("--r2", help="Mirror lineups and lineup_replacements to R2.")
    ] = False,
    targets: TargetsOption = None,
) -> None:
    """Fit the lineup model per season on earlier seasons' boxscores (#99, ADR 0017), project each
    candidate skater's minutes from earlier games' stints (#100, ADR 0018), and write his
    probability of dressing, expected minutes and power-play unit, and each candidate goalie's
    start probability from goalie_starts, to the lake's lineups; the replacement skaters to
    lineup_replacements; and the report to <out>/<version>.md: figures for the training seasons,
    while the development and held-out seasons wait for gate 2. With --targets, also project that
    date's slate games (#162), whose goalie_starts rows nhl goalie-start --targets wrote."""
    from datetime import UTC

    import polars as pl

    from nhl_edge.audit import projection as report
    from nhl_edge.backtest import reports
    from nhl_edge.backtest.seasons import DEVELOPMENT_SEASONS, OPEN_SEASONS
    from nhl_edge.features import team_strength
    from nhl_edge.ingest.games import EXPECTED_GAMES
    from nhl_edge.ingest.nhl_ingest import parse_seasons
    from nhl_edge.lake.tables import Lake
    from nhl_edge.lineup import minutes as mins
    from nhl_edge.lineup import projection as proj
    from nhl_edge.live.targets import rated, with_targets
    from nhl_edge.settings import load_env

    load_env()
    lake = Lake.from_env(mirror=r2)
    games, boxscores = lake.read("games"), lake.read("actual_lineups")
    known = sorted(games["season"].unique().to_list())
    try:
        wanted = parse_seasons(seasons) if seasons else [s for s in known if s >= proj.FIRST_SEASON]
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--seasons") from None
    problems = proj.input_problems(games, boxscores, max(wanted), EXPECTED_GAMES) if wanted else []
    if problems:
        for problem in problems:
            typer.echo(problem, err=True)
        typer.echo("run nhl ingest --replay for those seasons", err=True)
        raise typer.Exit(code=1)
    slate = _slate_targets(lake, targets, wanted)
    if slate is not None:
        games = with_targets(games, slate)
    try:
        version = reports.version(proj.COMPONENT, datetime.now(UTC))
        lines = team_strength.team_lines()
        rows = proj.candidates(games.filter(pl.col("season") <= max(wanted)), boxscores, lines)
        skaters, scored, models = proj.score(boxscores, games, wanted, version, lines, rows)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--seasons") from None
    played = mins.lake_minutes(lake, boxscores, max(wanted))
    # Stints built for part of a season would measure its figures on part of it.
    problems = mins.input_problems(lake.read("shift_coverage"), played, max(wanted))
    if problems:
        for problem in problems:
            typer.echo(problem, err=True)
        typer.echo("run nhl stints for those seasons", err=True)
        raise typer.Exit(code=1)
    try:
        constants = {
            season: mins.season_constants(played, rows, season, games) for season in wanted
        }
        projected, replacements = mins.project(scored, played, constants, games, lines)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        typer.echo("run nhl stints for those seasons", err=True)
        raise typer.Exit(code=1) from None
    skaters = mins.with_minutes(skaters, projected, constants)
    try:
        table = proj.with_goalies(skaters, rated(lake.read("goalie_starts"), games))
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        typer.echo("run nhl goalie-start for those seasons", err=True)
        raise typer.Exit(code=1) from None
    replacement_rows = mins.replacement_table(replacements, skaters)
    days = games.filter(pl.col("season").is_in(wanted))["game_date"].unique().to_list()
    lake.replace_dates("lineups", table, days)
    lake.replace_dates("lineup_replacements", replacement_rows, days)
    # The development seasons stay unseen until gate 2 (phase 3 plan).
    shown = [season for season in OPEN_SEASONS if season not in DEVELOPMENT_SEASONS]
    scores = report.team_game_scores(scored, boxscores)
    ice_time = report.ice_time_scores(projected, played, games)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{version}.md"
    path.write_text(
        report.markdown_report(
            scores, models, shown, version, ice_time, [constants[s] for s in wanted]
        )
    )
    goalies = table.filter(pl.col("role") == "G").height
    typer.echo(
        f"{path}: {table.height - goalies:,} skater and {goalies:,} goalie rows "
        f"in {len(models)} seasons, {replacement_rows.height:,} replacement rows"
    )
    # A fit's team-games and newcomers would describe a held-out season: shown, as in the
    # report, only when every season it read is.
    for fit in report.fits(models, shown):
        size = "held out" if fit["team_games"] is None else f"{fit['team_games']:,} team-games"
        typer.echo(f"  {fit['season']}: fitted on {size} to {fit['train_cutoff'][:10]}")


@app.command(name="rapm")
def rapm_command(
    seasons: Annotated[
        str | None,
        typer.Option(
            help="Seasons to rate, as 20232024, a comma list or a range. Default: 2011-12 on."
        ),
    ] = None,
    out: Annotated[Path, typer.Option(help="Report directory.")] = DEFAULT_RATINGS_OUT,
    r2: Annotated[
        bool, typer.Option("--r2", help="Mirror player_ratings and rapm_terms to R2.")
    ] = False,
    tune: Annotated[
        bool,
        typer.Option(
            "--tune", help="Run the tuning grid on the training seasons and log it; write no table."
        ),
    ] = False,
    targets: TargetsOption = None,
) -> None:
    """Refit RAPM every game day from the stints public before it (#101, ADR 0019), with the
    settings tuned in #103 (ADR 0011) and each season's priors (#102, ADR 0020), and write each
    lineup candidate's ratings to the lake's player_ratings, each fit's terms to rapm_terms, and
    the report to <out>/<version>.md: counts for every season, and terms, leaders and priors
    for the training seasons only. With --tune, score the 36 candidate settings on the training
    seasons by the projected 5v5 expected-goal difference (#103, ADR 0011) and log them to
    reports/tuning/. With --targets, also rate that date's slate games' candidates (#162), whose
    lineups rows nhl lineups --targets wrote."""
    from collections.abc import Iterator
    from datetime import UTC

    import polars as pl

    from nhl_edge.audit import rapm as report
    from nhl_edge.backtest import reports
    from nhl_edge.backtest.seasons import DEVELOPMENT_SEASONS, OPEN_SEASONS
    from nhl_edge.ingest.nhl_ingest import parse_seasons
    from nhl_edge.lake.tables import Lake
    from nhl_edge.lineup import minutes as mins
    from nhl_edge.live.targets import rated, with_targets
    from nhl_edge.ratings import rapm
    from nhl_edge.reference import load_venues
    from nhl_edge.settings import load_env

    load_env()
    if tune and (seasons or r2):
        raise typer.BadParameter("--tune reads the training seasons and writes no table")
    lake = Lake.from_env(mirror=r2)
    games = lake.read("games")
    known = sorted(games["season"].unique().to_list())
    try:
        if tune:
            last = max(rapm.TUNING_SEASONS)
            wanted = [s for s in known if rapm.FIRST_SEASON <= s <= last]
        elif seasons:
            wanted = parse_seasons(seasons)
        else:
            wanted = [s for s in known if s >= rapm.FIRST_SEASON]
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--seasons") from None
    if not wanted or min(wanted) < rapm.FIRST_SEASON:
        raise typer.BadParameter(
            f"RAPM starts in {rapm.FIRST_SEASON}, the first season with xG", param_hint="--seasons"
        )
    read = [s for s in known if rapm.FIRST_SEASON <= s <= max(wanted)]
    # Stints built for part of a season would rate its players on part of it.
    played = pl.concat([lake.read("stints", seasons=[s]).select("game_id").unique() for s in read])
    coverage = lake.read("shift_coverage").filter(pl.col("season") >= rapm.FIRST_SEASON)
    problems = mins.input_problems(coverage, played, max(wanted))
    slate = _slate_targets(lake, targets, wanted, tune)
    if slate is not None:
        games = with_targets(games, slate)
    candidates = rapm.targets(rated(lake.read("lineups", seasons=wanted), games), games, wanted)
    without = games.filter(pl.col("season").is_in(wanted)).join(
        candidates.select("game_id").unique(), on="game_id", how="anti"
    )
    if without.height:
        examples = ", ".join(map(str, without["game_id"].sort().head(3).to_list()))
        problems.append(f"{without.height:,} games without lineups, e.g. {examples}")
    if problems:
        for problem in problems:
            typer.echo(problem, err=True)
        typer.echo("run nhl stints and nhl lineups for those seasons", err=True)
        raise typer.Exit(code=1)

    def stint_seasons() -> Iterator[pl.DataFrame]:
        # A season at a time: every season's stints at once would not fit in memory.
        for season in read:
            yield lake.read("stints", seasons=[season])

    version = reports.version(rapm.COMPONENT, datetime.now(UTC))
    roles = lake.read("actual_lineups").select("game_id", "player_id", "role")
    if tune:
        _tune_rapm(lake, games, roles, candidates, read, version)
        return
    try:
        ratings, terms, fits = rapm.rate(
            stint_seasons(),
            games,
            roles,
            load_venues(),
            candidates,
            rapm.TUNED,
            version,
            players=lake.read("players"),
            league_seasons=lake.read("player_league_seasons"),
        )
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    days = games.filter(pl.col("season").is_in(wanted))["game_date"].unique().to_list()
    lake.replace_dates("player_ratings", ratings, days)
    lake.replace_dates("rapm_terms", terms, days)
    # The development seasons stay unseen until gate 2 (phase 3 plan).
    shown = [season for season in OPEN_SEASONS if season not in DEVELOPMENT_SEASONS]
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{version}.md"
    path.write_text(
        report.markdown_report(
            ratings, terms, lake.read("players"), shown, version, rapm.TUNED, fits
        )
    )
    typer.echo(
        f"{path}: {ratings.height:,} ratings of {ratings['player_id'].n_unique():,} skaters "
        f"in {len(wanted)} seasons, {terms.height:,} terms"
    )


def _tune_rapm(
    lake: "Lake",
    games: "pl.DataFrame",
    roles: "pl.DataFrame",
    candidates: "pl.DataFrame",
    read: list[int],
    version: str,
) -> None:
    """Score each of RAPM's grid settings (#103, ADR 0011): the 5v5 model only, its ratings
    turned into each game's projected 5v5 expected-goal difference, and that feature scored on
    the training seasons. Logs every candidate and the choice to reports/tuning/."""
    from nhl_edge.backtest import tuning
    from nhl_edge.ratings import rapm
    from nhl_edge.reference import load_venues

    stints = {season: lake.read("stints", seasons=[season]) for season in read}
    lineups = lake.read("lineups", seasons=read)
    players, lines = lake.read("players"), lake.read("player_league_seasons")
    venues = load_venues()
    scored = []
    for settings in rapm.GRID:
        ratings, _, _ = rapm.rate(
            (stints[s] for s in read),
            games,
            roles,
            venues,
            candidates,
            settings,
            version,
            players=players,
            league_seasons=lines,
            kinds=(rapm.EV,),
            spread=False,
        )
        feature = rapm.expected_difference(ratings, lineups, games)
        games_scored = tuning.scored_games(feature, games, rapm.TUNING_SEASONS)
        scored.append(tuning.Candidate(settings, settings.label, games_scored))
        loss = games_scored["log_loss"].mean()
        typer.echo(f"  {settings.label}: log loss {loss:.5f}")
    choice = tuning.choose(scored, rapm.steadiness)
    DEFAULT_TUNING_OUT.mkdir(parents=True, exist_ok=True)
    path = DEFAULT_TUNING_OUT / f"{version}.md"
    path.write_text(tuning.markdown(choice, "RAPM", version, rapm.TUNING_SEASONS))
    frozen = "matches" if choice.chosen == rapm.TUNED else "differs from"
    typer.echo(f"{path}: chosen {choice.chosen.label}, which {frozen} the frozen TUNED")


@app.command(name="power-plays")
def power_plays_command(
    seasons: Annotated[
        str | None,
        typer.Option(
            help="Seasons to rate, as 20232024, a comma list or a range. Default: 2011-12 on."
        ),
    ] = None,
    out: Annotated[Path, typer.Option(help="Report directory.")] = DEFAULT_POWER_PLAYS_OUT,
    r2: Annotated[
        bool, typer.Option("--r2", help="Mirror penalty_rates and expected_power_plays to R2.")
    ] = False,
    targets: TargetsOption = None,
) -> None:
    """Rate every lineup candidate's penalties taken and drawn per hour, and each team's expected
    power plays, power-play minutes and shorthanded xG, from both projected lineups (#104, ADR
    0021). Writes penalty_rates and expected_power_plays to the lake and the report to
    <out>/<version>.md: counts for every season, and pulls, league figures, the power-play
    minutes against B2's and the leaders for the training seasons only. With --targets, also rate
    that date's slate games (#162), whose lineups rows nhl lineups --targets wrote."""
    from datetime import UTC

    import polars as pl

    from nhl_edge.audit import power_plays as report
    from nhl_edge.backtest import reports
    from nhl_edge.backtest.seasons import DEVELOPMENT_SEASONS, OPEN_SEASONS
    from nhl_edge.features import team_strength as ts
    from nhl_edge.ingest.nhl_ingest import parse_seasons
    from nhl_edge.lake.schemas import ExpectedPowerPlays, PenaltyRates
    from nhl_edge.lake.tables import Lake
    from nhl_edge.lineup import minutes as mins
    from nhl_edge.live.targets import rated, with_targets
    from nhl_edge.ratings import penalty_rates as pr
    from nhl_edge.ratings import rapm
    from nhl_edge.settings import load_env

    load_env()
    lake = Lake.from_env(mirror=r2)
    games = lake.read("games")
    known = sorted(games["season"].unique().to_list())
    try:
        wanted = parse_seasons(seasons) if seasons else [s for s in known if s >= rapm.FIRST_SEASON]
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--seasons") from None
    if not wanted or min(wanted) < rapm.FIRST_SEASON:
        raise typer.BadParameter(
            f"expected power plays start in {rapm.FIRST_SEASON}, the first season with lineups",
            param_hint="--seasons",
        )
    last = max(wanted)
    boxscores = lake.read("actual_lineups").filter(pl.col("season") <= last)
    minutes = mins.lake_minutes(lake, boxscores, last)
    # Stints built for part of a season would rate its players on part of it.
    problems = mins.input_problems(lake.read("shift_coverage"), minutes, last)
    lineups = lake.read("lineups", seasons=wanted).filter(pl.col("role").is_in(["F", "D"]))
    without = games.filter(pl.col("season").is_in(wanted)).join(
        lineups.select("game_id").unique(), on="game_id", how="anti"
    )
    if without.height:
        examples = ", ".join(map(str, without["game_id"].sort().head(3).to_list()))
        problems.append(f"{without.height:,} games without lineups, e.g. {examples}")
    penalties = lake.read("penalties").filter(pl.col("season") <= last)
    read = [s for s in known if s <= last]
    shots = lake.read("shots", seasons=read)
    shot_xg = lake.read("shot_xg", seasons=read)
    strength_time = lake.read("strength_time").filter(pl.col("season") <= last)
    # A game or date missing from these would rate on part of the data, silently (#130).
    problems += pr.input_problems(games, penalties, strength_time, shot_xg, last)
    if problems:
        for problem in problems:
            typer.echo(problem, err=True)
        typer.echo(
            "run nhl stints and nhl lineups for those seasons, or replay the games' feeds and "
            "nhl xg",
            err=True,
        )
        raise typer.Exit(code=1)
    slate = _slate_targets(lake, targets, wanted)
    if slate is not None:
        games = with_targets(games, slate)
    as_of = ts.as_of(pl.col("game_date"), pl.col("start_utc"))
    times = games.filter(pl.col("season").is_in(wanted)).select("season", as_of_utc=as_of)
    candidates = rated(lineups, games).join(
        games.select("game_id", as_of_utc=as_of), on="game_id", how="left"
    )
    weighted = pr.unoffset(penalties)
    version = reports.version(pr.COMPONENT, datetime.now(UTC))
    try:
        rows = pr.player_games(minutes, weighted, games)
        pulls = {season: pr.season_pulls(rows, season, games) for season in wanted}
        rates = pr.rates(
            candidates.select(
                "game_id", "season", "game_date", "team", "player_id", "role", "as_of_utc"
            ),
            rows,
            pulls,
        )
        history = pr.team_games(strength_time, weighted, shots, shot_xg)
        expected = pr.expected(
            rates,
            candidates,
            rated(lake.read("lineup_replacements", seasons=wanted), games),
            pr.role_rates(rows, times),
            pr.league_figures(history, times),
            games,
        )
        rates_table = pr.stamp(rates, PenaltyRates, pulls, version)
        expected_table = pr.stamp(expected, ExpectedPowerPlays, pulls, version)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    days = games.filter(pl.col("season").is_in(wanted))["game_date"].unique().to_list()
    lake.replace_dates("penalty_rates", rates_table, days)
    lake.replace_dates("expected_power_plays", expected_table, days)
    # B2's team-level power-play minutes, the reference (ADR 0021).
    reference = ts.power_play_minutes(
        games.filter(pl.col("season").is_in(wanted)),
        ts.team_games(shots, shot_xg, strength_time),
        ts.TUNED,
    )
    scores = report.scored(expected_table, history, reference)
    # The development seasons stay unseen until gate 2 (phase 3 plan).
    shown = [season for season in OPEN_SEASONS if season not in DEVELOPMENT_SEASONS]
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{version}.md"
    path.write_text(
        report.markdown_report(
            rates_table, expected_table, scores, lake.read("players"), pulls, shown, version
        )
    )
    typer.echo(
        f"{path}: {rates_table.height:,} rates of {rates_table['player_id'].n_unique():,} skaters "
        f"and {expected_table.height:,} team-games in {len(wanted)} seasons"
    )


@app.command(name="finishing")
def finishing_command(
    seasons: Annotated[
        str | None,
        typer.Option(
            help="Seasons to rate, as 20232024, a comma list or a range. Default: 2011-12 on."
        ),
    ] = None,
    out: Annotated[Path, typer.Option(help="Report directory.")] = DEFAULT_FINISHING_OUT,
    r2: Annotated[
        bool, typer.Option("--r2", help="Mirror finishing and goal_multipliers to R2.")
    ] = False,
    targets: TargetsOption = None,
) -> None:
    """Rate every lineup candidate's finishing φ and xG share, and each team's goal multipliers
    against each opposing candidate goalie: the team's φ times the goalie's conversion (#105, ADR
    0022). Writes finishing and goal_multipliers to the lake and the report to
    <out>/<version>.md: counts for every season, and pulls, league figures, the goals against
    the league's finishing and the leaders for the training seasons only. With --targets, also
    rate that date's slate games (#162), whose lineups and goalie_effects rows the builders before
    it wrote with --targets."""
    from datetime import UTC

    import polars as pl

    from nhl_edge.audit import finishing as report
    from nhl_edge.backtest import reports
    from nhl_edge.backtest.seasons import DEVELOPMENT_SEASONS, OPEN_SEASONS
    from nhl_edge.features import team_strength as ts
    from nhl_edge.ingest.nhl_ingest import parse_seasons
    from nhl_edge.lake.schemas import Finishing, GoalMultipliers
    from nhl_edge.lake.tables import Lake
    from nhl_edge.lineup import minutes as mins
    from nhl_edge.live.targets import rated, with_targets
    from nhl_edge.ratings import finishing as fn
    from nhl_edge.ratings import rapm
    from nhl_edge.settings import load_env

    load_env()
    lake = Lake.from_env(mirror=r2)
    games = lake.read("games")
    known = sorted(games["season"].unique().to_list())
    try:
        wanted = parse_seasons(seasons) if seasons else [s for s in known if s >= rapm.FIRST_SEASON]
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--seasons") from None
    if not wanted or min(wanted) < rapm.FIRST_SEASON:
        raise typer.BadParameter(
            f"finishing starts in {rapm.FIRST_SEASON}, the first season with xG",
            param_hint="--seasons",
        )
    last = max(wanted)
    boxscores = lake.read("actual_lineups").filter(pl.col("season") <= last)
    minutes = mins.lake_minutes(lake, boxscores, last)
    # Stints built for part of a season would rate its players on part of it.
    problems = mins.input_problems(lake.read("shift_coverage"), minutes, last)
    read = [s for s in known if s <= last]
    shots = lake.read("shots", seasons=read)
    shot_xg = lake.read("shot_xg", seasons=read)
    # A game without xG would drop out of its shooters' histories unnoticed.
    problems += fn.input_problems(games, shots, shot_xg, last)
    lineups = lake.read("lineups", seasons=wanted).filter(pl.col("role").is_in(["F", "D"]))
    effects = lake.read("goalie_effects", seasons=wanted)
    for name, frame in (("lineups", lineups), ("goalie effects", effects)):
        without = games.filter(pl.col("season").is_in(wanted)).join(
            frame.select("game_id").unique(), on="game_id", how="anti"
        )
        if without.height:
            examples = ", ".join(map(str, without["game_id"].sort().head(3).to_list()))
            problems.append(f"{without.height:,} games without {name}, e.g. {examples}")
    if problems:
        for problem in problems:
            typer.echo(problem, err=True)
        typer.echo(
            "run nhl xg, nhl stints, nhl lineups and nhl goalie-effect for those seasons",
            err=True,
        )
        raise typer.Exit(code=1)
    slate = _slate_targets(lake, targets, wanted)
    if slate is not None:
        games = with_targets(games, slate)
    as_of = ts.as_of(pl.col("game_date"), pl.col("start_utc"))
    times = games.filter(pl.col("season").is_in(wanted)).select("season", as_of_utc=as_of)
    candidates = rated(lineups, games).join(
        games.select("game_id", as_of_utc=as_of), on="game_id", how="left"
    )
    version = reports.version(fn.COMPONENT, datetime.now(UTC))
    try:
        rows = fn.shooter_games(minutes, shots, shot_xg, games)
        pulls = {season: fn.season_pulls(rows, season, games) for season in wanted}
        rates = fn.rates(
            candidates.select(
                "game_id", "season", "game_date", "team", "player_id", "role", "as_of_utc"
            ),
            rows,
            pulls,
        )
        shared, multipliers = fn.multipliers(
            rates,
            candidates,
            rated(lake.read("lineup_replacements", seasons=wanted), games),
            fn.league_rates(rows, times),
            rated(effects, games),
            fn.shot_figures(shots, shot_xg, times),
            games,
        )
        finishing_table = fn.stamp(shared, Finishing, pulls, version)
        multipliers_table = fn.stamp(multipliers, GoalMultipliers, pulls, version)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    days = games.filter(pl.col("season").is_in(wanted))["game_date"].unique().to_list()
    lake.replace_dates("finishing", finishing_table, days)
    lake.replace_dates("goal_multipliers", multipliers_table, days)
    scores = report.scored(
        multipliers_table,
        report.team_goals(shots, shot_xg),
        lake.read("goalie_starts", seasons=wanted),
    )
    # The development seasons stay unseen until gate 2 (phase 3 plan).
    shown = [season for season in OPEN_SEASONS if season not in DEVELOPMENT_SEASONS]
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{version}.md"
    path.write_text(
        report.markdown_report(
            finishing_table, multipliers_table, scores, lake.read("players"), pulls, shown, version
        )
    )
    typer.echo(
        f"{path}: {finishing_table.height:,} skaters' finishing and {multipliers_table.height:,} "
        f"goal multipliers in {len(wanted)} seasons"
    )


@live_app.command("features")
def live_features(
    day: Annotated[
        datetime, typer.Option("--date", formats=["%Y-%m-%d"], help="The game date to rate.")
    ],
    out: Annotated[Path, typer.Option(help="The builders' report directory.")] = DEFAULT_LIVE_OUT,
    r2: Annotated[
        bool,
        typer.Option(
            "--r2", help="Pull the lake's tables from R2 first, and mirror what it writes to R2."
        ),
    ] = False,
) -> None:
    """Rate a game date's slate (#162). Fetch the date's schedule, and refuse, before changing
    anything, while a game of the week before is not final in the lake, no slate game was
    fetched before its as-of time, or no game of the slate's season is final yet (#181). Then
    write the slate, bring its season's played-game tables up to date (xg, stints, then each
    builder) with the slate games' target rows beside them, and record the build in
    feature_builds once every step is done."""
    from datetime import UTC

    import polars as pl

    from nhl_edge.backtest import reports
    from nhl_edge.ingest.dailyfaceoff import season_of
    from nhl_edge.ingest.nhl_api import NhlApi
    from nhl_edge.lake.raw import RawStore
    from nhl_edge.lake.tables import TABLES, Lake
    from nhl_edge.live import features as lf
    from nhl_edge.live import slate as live_slate
    from nhl_edge.live.targets import in_time
    from nhl_edge.settings import load_env

    load_env()
    started = datetime.now(UTC)
    game_date = day.date()
    lake = Lake.from_env(mirror=r2)
    store = RawStore.from_env(mirror=r2, flag="--r2")
    if r2:
        pulled = sum(lake.pull(table) for table in lf.LAKE_TABLES)
        typer.echo(f"pulled {pulled:,} table files from R2")
    api = NhlApi(store)
    fetched = live_slate.fetch(api, game_date)
    slate = fetched.games
    typer.echo(f"slate {game_date}: {slate.height} games ({fetched.raw_key})")
    # Checked before any table changes, so a refused run leaves the date's last build standing.
    late = slate.join(in_time(slate), on="game_id", how="anti")
    if late.height:
        games = ", ".join(map(str, late["game_id"].to_list()))
        typer.echo(f"fetched at or after their as-of time, so not rated: {games}", err=True)
        if late.height == slate.height:
            raise typer.Exit(code=1)
    played = lake.read("games")
    problems = live_slate.opening(slate, played)
    if slate.height and not problems:
        problems = live_slate.settled_problems(api, game_date, played)
    if problems:
        for problem in problems:
            typer.echo(problem, err=True)
        typer.echo("run nhl ingest for those dates, then this again", err=True)
        raise typer.Exit(code=1)
    # The record goes first, so a build cut short leaves none (#170).
    lake.replace_dates("feature_builds", TABLES["feature_builds"].empty(), [game_date])
    lake.replace_dates("slate", slate, [game_date])
    build_id = reports.version(lf.COMPONENT, started)
    build = {
        "build_id": build_id,
        "code_version": build_id.removeprefix(f"{lf.COMPONENT}-{started:%Y%m%d}-"),
        "slate_raw_key": fetched.raw_key,
        "slate_fetched_utc": fetched.fetched_utc,
        "started_utc": started,
    }
    if slate.is_empty():
        # A day without games is recorded too, so a prediction tells it from a missed build.
        empty = lf.record(
            slate, game_date, season_of(game_date), {}, {**build, "finished_utc": datetime.now(UTC)}
        )
        lake.replace_dates("feature_builds", empty, [game_date])
        return
    (season,) = slate["season"].unique().to_list()
    seasons = str(season)
    xg(seasons=seasons, out=out / "xg", r2=r2)
    stints(seasons=seasons, r2=r2)
    team_strength(seasons=seasons, r2=r2, targets=day)
    goalie_start(seasons=seasons, out=out / "goalie-start", r2=r2, targets=day)
    lineups(seasons=seasons, out=out / "lineups", r2=r2, targets=day)
    goalie_effect(seasons=seasons, r2=r2, targets=day)
    schedule_terms(seasons=seasons, r2=r2, targets=day)
    rapm_command(seasons=seasons, out=out / "rapm", r2=r2, targets=day)
    power_plays_command(seasons=seasons, out=out / "power-plays", r2=r2, targets=day)
    finishing_command(seasons=seasons, out=out / "finishing", r2=r2, targets=day)
    tables = {
        name: lake.read(name, seasons=[season]).filter(pl.col("game_date") == game_date)
        for name in lf.TARGET_TABLES
    }
    try:
        record = lf.record(
            slate, game_date, season, tables, {**build, "finished_utc": datetime.now(UTC)}
        )
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    lake.replace_dates("feature_builds", record, [game_date])
    for line in lf.uncovered(slate, tables):
        typer.echo(line, err=True)
    typer.echo(f"{build_id}: {slate.height} slate games rated into {len(tables)} tables")


@app.command()
def bets() -> None:
    """Show the paper bet ledger and CLV."""
    _not_implemented("bets", "phase 5")


@live_app.command("bundle")
def live_bundle(
    day: Annotated[
        datetime | None,
        typer.Option("--date", formats=["%Y-%m-%d"], help="The decision day to finish."),
    ] = None,
    finish: Annotated[
        bool,
        typer.Option(
            "--finish",
            help="Complete the day's bundle, written up to its ledger, with its manifest rebuilt "
            "from what is stored (#188).",
        ),
    ] = False,
    check: Annotated[
        bool,
        typer.Option("--check", help="List the decision days whose bundle has no manifest."),
    ] = False,
    r2: Annotated[bool, typer.Option("--r2", help="The bundles and ledgers in R2.")] = False,
    source: Annotated[
        Path | None,
        typer.Option("--from", help="A dry run's --out directory instead (--finish only)."),
    ] = None,
) -> None:
    """Run bundles that a decision left unfinished (#188). --check lists, from R2, each day with a
    ledger whose bundle has no manifest, and exits 1 if there is one. --finish completes one:
    its manifest rebuilt from the ledger's own columns, the stored inputs and fits, the day's raw
    odds responses and the committed live fit the ledger names. It never writes an input, and
    refuses a bundle missing one."""
    import io
    import json
    from datetime import date

    import polars as pl

    from nhl_edge.lake.raw import RawStore
    from nhl_edge.lake.tables import Lake
    from nhl_edge.live import blend_fit
    from nhl_edge.live import bundle as lb
    from nhl_edge.live import predict as lp
    from nhl_edge.settings import load_env

    if check == finish:
        raise typer.BadParameter("pass --check or --finish, one of them")
    if check and not r2:
        raise typer.BadParameter("--check reads the ledgers in R2: pass --r2")
    if finish and (day is None or r2 == (source is not None)):
        raise typer.BadParameter("--finish takes --date, and --r2 or --from, one of them")
    load_env()
    if check:
        lake = Lake.from_env(mirror=True)
        assert lake.objects is not None and lake.bucket is not None
        store: lb.Store = lb.R2Store(lake.objects, lake.bucket)
        days = sorted(
            date.fromisoformat(key.rsplit("/", 1)[1].removesuffix(".parquet"))
            for key in store.keys(lp.LEDGER_PREFIX)
            if key.endswith(".parquet")
        )
        missing = lb.unfinished(store, days)
        for each in missing:
            typer.echo(
                f"{each}: the run bundle has no manifest, so nhl live bundle --date {each} "
                "--finish --r2 completes it",
                err=True,
            )
        typer.echo(f"{len(days) - len(missing)} of {len(days)} decision days' bundles complete")
        if missing:
            raise typer.Exit(code=1)
        return
    assert day is not None
    game_date = day.date()
    if source is not None:
        store = lb.LocalStore(source)
        ledger = pl.read_parquet(source / f"{game_date.isoformat()}.parquet")
        ledger_at = str(source / f"{game_date.isoformat()}.parquet")
        raw_store = RawStore.from_env(mirror=False)
    else:
        lake = Lake.from_env(mirror=True)
        assert lake.objects is not None and lake.bucket is not None
        store = lb.R2Store(lake.objects, lake.bucket)
        key = lp.ledger_key(game_date)
        ledger = pl.read_parquet(io.BytesIO(store.get(key)))
        ledger_at = f"R2 {key}"
        raw_store = RawStore.from_env(mirror=True, flag="--r2")
        raw_store.restore_from_r2(prefix=f"odds/{game_date.isoformat()}/")
    decided = ledger["prediction_utc"].drop_nulls().unique().to_list()
    (blend_version,) = ledger["blend_version"].unique().to_list()
    fits = [
        path
        for path in sorted(blend_fit.REPORTS.glob(f"{blend_fit.COMPONENT}-*.json"))
        if json.loads(path.read_text()).get("version") == blend_version
    ]
    if len(decided) != 1 or len(fits) != 1:
        typer.echo(
            f"{game_date}: the ledger needs one decision instant and its live fit {blend_version} "
            "under reports/live/",
            err=True,
        )
        raise typer.Exit(code=1)
    (published,) = ledger["published_utc"].unique().to_list()
    raw = lp.raw_responses(raw_store, decided[0].date(), by=published)
    try:
        where = lb.finish(
            store,
            game_date,
            ledger,
            ledger_at,
            raw,
            blend_fit.load(json.loads(fits[0].read_text())),
            fits[0].name,
            lb.sha256(fits[0].read_bytes()),
        )
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"{where}: finished, its manifest rebuilt from what is stored")


@live_app.command("replay")
def live_replay(
    day: Annotated[
        datetime, typer.Option("--date", formats=["%Y-%m-%d"], help="The decision day to replay.")
    ],
    r2: Annotated[
        bool, typer.Option("--r2", help="Read the day's run bundle and ledger from R2.")
    ] = False,
    source: Annotated[
        Path | None,
        typer.Option("--from", help="Read them from a dry run's --out directory instead."),
    ] = None,
    fit: Annotated[
        Path | None,
        typer.Option(help="The live fit the day was decided with (default: under reports/live/)."),
    ] = None,
) -> None:
    """Replay a decision day from its run bundle alone (#171): make the day's decision again from
    the rows, quotes and fits it saved and the committed live fit, with the lake set aside, and
    compare the ledger with the day's, every column. Exits 1 if the bundle is incomplete or
    altered, the fit is not the one it names, or the two ledgers disagree."""
    import io
    import json

    import polars as pl

    from nhl_edge.lake.tables import Lake
    from nhl_edge.live import blend_fit
    from nhl_edge.live import bundle as lb
    from nhl_edge.live import predict as lp
    from nhl_edge.settings import load_env

    game_date = day.date()
    if r2 == (source is not None):
        raise typer.BadParameter("pass --r2 or --from, one of them")
    if source is not None:
        store: lb.Store = lb.LocalStore(source)
        ledger = pl.read_parquet(source / f"{game_date.isoformat()}.parquet")
    else:
        load_env()
        lake = Lake.from_env(mirror=True)
        assert lake.objects is not None and lake.bucket is not None
        store = lb.R2Store(lake.objects, lake.bucket)
        ledger = pl.read_parquet(io.BytesIO(store.get(lp.ledger_key(game_date))))
    try:
        saved = lb.read(store, game_date)
        path = fit or blend_fit.REPORTS / saved.manifest["live_fit"]
        body = path.read_bytes()
        if lb.sha256(body) != saved.manifest["live_fit_sha256"]:
            raise ValueError(f"{path} is not the live fit the day was decided with")
        replayed = lb.replay(saved, blend_fit.load(json.loads(body)))
    except (OSError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    problems = lb.differences(replayed, ledger)
    predicted = ledger.filter(pl.col("status") == lp.PREDICTED).height
    typer.echo(
        f"{game_date}: {replayed.height} games decided again from the bundle, {predicted} "
        f"predicted, {int(ledger['bet'].fill_null(False).sum())} bets in the ledger"
    )
    for problem in problems:
        typer.echo(problem, err=True)
    if problems:
        raise typer.Exit(code=1)
    typer.echo(f"the ledger is reproduced, every column, floats within {lb.TOLERANCE:g}")


@live_app.command("settle")
def live_settle(
    r2: Annotated[
        bool,
        typer.Option(
            "--r2",
            help="Read the ledgers from R2, pull the games and odds first, and mirror "
            "paper_settlements to R2.",
        ),
    ] = False,
) -> None:
    """Settle the season's paper bets whose games are final (#165): each one's result on the
    full game, its profit, and its CLV against Pinnacle's closing proxy (ADR 0033), or why it has
    none. Rebuilds paper_settlements whole, from the ledgers (in R2 with --r2), the games and the
    odds that nhl odds replay brings up to date; each close is found at this run's clock."""
    from datetime import UTC

    import polars as pl

    from nhl_edge.backtest import reports
    from nhl_edge.lake.tables import Lake
    from nhl_edge.live import predict as lp
    from nhl_edge.live import settle as ls
    from nhl_edge.live.blend_fit import LIVE_SEASON
    from nhl_edge.settings import load_env

    load_env()
    now = datetime.now(UTC)
    version = reports.version(ls.COMPONENT, now)
    if r2 and version.endswith("-dirty"):
        raise typer.BadParameter("commit first: the settlements name their commit")
    lake = Lake.from_env(mirror=r2)
    if r2:
        assert lake.objects is not None and lake.bucket is not None
        pulled = sum(lake.pull(table) for table in ("games", "odds_snapshots"))
        typer.echo(f"pulled {pulled:,} table files from R2")
        # The ledgers in R2 are the record.
        ledger = lp.sync_ledgers(lake.objects, lake.bucket, LIVE_SEASON)
    else:
        ledger = lake.read("paper_ledger", seasons=[LIVE_SEASON])
    games = lake.read("games", seasons=[LIVE_SEASON])
    settled = ls.settle(ledger, games, lake.read("odds_snapshots"), now, {"code_version": version})
    lake.replace("paper_settlements", settled)
    bets = int(ledger["bet"].fill_null(False).sum()) if ledger.height else 0
    valued = settled.filter(pl.col("clv").is_not_null())
    # Counts only: CLV is reported with its interval by the live report (hard rule 7).
    typer.echo(
        f"paper_settlements: {settled.height} of {bets} bets settled, {valued.height} with a CLV"
    )
    for (status,), rows in settled.group_by("close_status", maintain_order=True):
        typer.echo(f"  {status}: {rows.height}")


@live_app.command("supabase")
def live_supabase(
    r2: Annotated[
        bool,
        typer.Option(
            "--r2", help="Read the ledgers from R2 and pull the settlements first (the record)."
        ),
    ] = False,
    cron: Annotated[
        str | None,
        typer.Option(
            help="The odds workflow's fallback cron line: copy only if it is today's midday "
            "slot, as nhl predict decides, and otherwise do nothing."
        ),
    ] = None,
) -> None:
    """Copy the paper ledger to Supabase for the dashboard (#167): every new prediction and bet,
    inserted once, then every settlement onto its bet. A rerun sends nothing new, so the same
    command backfills the season. Needs the paper ledger migration applied."""
    from datetime import UTC
    from zoneinfo import ZoneInfo

    if cron is not None:
        from nhl_edge.ingest.odds import resolve_slot

        today = datetime.now(UTC).astimezone(ZoneInfo("America/New_York")).date()
        slot = resolve_slot("free-tier", cron, today)
        if slot is None or slot.name != "midday":
            typer.echo(f"{cron!r} is not today's midday slot: nothing copied")
            return
    from nhl_edge.lake.supabase import Supabase
    from nhl_edge.lake.tables import Lake
    from nhl_edge.live import blend_fit, serving
    from nhl_edge.live import predict as lp
    from nhl_edge.settings import load_env

    load_env()
    lake = Lake.from_env(mirror=r2)
    if r2:
        assert lake.objects is not None and lake.bucket is not None
        lake.pull("paper_settlements")
        ledger = lp.sync_ledgers(lake.objects, lake.bucket, blend_fit.LIVE_SEASON)
    else:
        ledger = lake.read("paper_ledger", seasons=[blend_fit.LIVE_SEASON])
    supabase = Supabase.from_env()
    sent = serving.sync(supabase, ledger, lake.read("paper_settlements"))
    typer.echo(
        f"supabase {supabase.project_ref}: {sent[serving.PREDICTIONS]} predictions and "
        f"{sent[serving.PAPER_BETS]} bets sent (new ones inserted), "
        f"{sent['settlements']} settlements set"
    )


@live_app.command("report")
def live_report(
    as_of: Annotated[
        datetime | None,
        typer.Option(
            "--as-of", formats=["%Y-%m-%d"], help="The report's date (default: today, ET)."
        ),
    ] = None,
    r2: Annotated[
        bool,
        typer.Option(
            "--r2",
            help="Read the ledgers from R2, and pull the games, odds, settlements and SBR's odds "
            "first.",
        ),
    ] = False,
    out: Annotated[Path, typer.Option(help="Where the report is written.")] = Path("reports/live"),
    supabase: Annotated[
        bool,
        typer.Option(
            "--supabase",
            help="Also upsert the report into Supabase's live_reports for the dashboard (#168).",
        ),
    ] = False,
) -> None:
    """The weekly live report (#166, ADR 0032): coverage, CLV against Pinnacle's closing proxy
    with the coverage floor and bound, the model comparisons, the blend's calibration band, the
    gaps for hand review and the operational alerts, every figure with its weekly block bootstrap
    interval. Interim until the formal review: no verdict before it. Writes
    report-<date>.json and .md under out."""
    import json
    from datetime import UTC

    from nhl_edge.ingest.odds import ET
    from nhl_edge.lake.tables import Lake
    from nhl_edge.live import blend_fit
    from nhl_edge.live import predict as lp
    from nhl_edge.live import report as lr
    from nhl_edge.settings import load_env

    load_env()
    day = as_of.date() if as_of is not None else datetime.now(UTC).astimezone(ET).date()
    lake = Lake.from_env(mirror=r2)
    if r2:
        assert lake.objects is not None and lake.bucket is not None
        tables = ("games", "odds_snapshots", "paper_settlements", "sbr_odds")
        pulled = sum(lake.pull(table) for table in tables)
        typer.echo(f"pulled {pulled:,} table files from R2")
        # The ledgers in R2 are the record.
        ledger = lp.sync_ledgers(lake.objects, lake.bucket, blend_fit.LIVE_SEASON)
    else:
        ledger = lake.read("paper_ledger", seasons=[blend_fit.LIVE_SEASON])
    paths = sorted(blend_fit.REPORTS.glob("blend-live-*.json"))
    if len(paths) != 1:
        typer.echo(f"expected the season's one live fit, found {len(paths)}", err=True)
        raise typer.Exit(code=1)
    scale = next(iter(json.loads(paths[0].read_text())["fits"].values()))["u_scale"]
    games = lake.read("games")
    result = lr.report(
        ledger,
        lake.read("paper_settlements"),
        games,
        lake.read("odds_snapshots"),
        lr.sbr_history(lake.read("sbr_odds"), games),
        scale,
        day,
    )
    json_path, md_path = lr.write(result, out)
    if supabase:
        from nhl_edge.backtest import reports
        from nhl_edge.lake.supabase import Supabase

        version = reports.version("live-report", datetime.now(UTC))
        Supabase.from_env().upsert_records("live_reports", [lr.record(result, version)], ["as_of"])
        typer.echo(f"supabase live_reports: the {day} report upserted")
    cover = result["coverage"]
    typer.echo(
        f"{result['kind']} report {day}: {cover['slate_games']} slate games, {cover['bets']} bets, "
        f"{cover['with_proxy']} of {cover['eligible']} eligible with a closing proxy"
    )
    typer.echo(f"wrote {md_path} and {json_path}")


@live_app.command("slate")
def live_slate(
    day: Annotated[
        datetime | None,
        typer.Option("--date", formats=["%Y-%m-%d"], help="The game date (default: today, ET)."),
    ] = None,
    r2: Annotated[
        bool,
        typer.Option(
            "--r2", help="Read the day's ledger from R2, and pull its lineup replacements first."
        ),
    ] = False,
    source: Annotated[
        Path | None,
        typer.Option("--from", help="A dry run's ledger file (nhl predict --dry-run) instead."),
    ] = None,
    fit: Annotated[
        Path | None,
        typer.Option(help="The live fit a dry run was decided with, if not under reports/live/."),
    ] = None,
) -> None:
    """The day's ledger for the daily-slate review (#166): each game's status, B1, B3, the blend,
    the gap, u and the bet, the flags for hand review, and the lineup gaps; then each bet's driver
    and parts from the day's run bundle (#193), with --r2 or --from."""
    import io
    from datetime import UTC

    import polars as pl

    from nhl_edge.ingest.odds import ET
    from nhl_edge.lake.tables import Lake
    from nhl_edge.live import blend_fit
    from nhl_edge.live import predict as lp
    from nhl_edge.live import report as lr
    from nhl_edge.settings import load_env

    if r2 and source is not None:
        raise typer.BadParameter("pass at most one of --r2 and --from")
    load_env()
    lake = Lake.from_env(mirror=r2)
    season = [blend_fit.LIVE_SEASON]
    if source is not None:
        ledger = pl.read_parquet(source)
        dates = ledger["game_date"].unique().to_list()
        # A dry run's ledger holds one date: its own, unless --date picks another.
        if day is None and len(dates) != 1:
            raise typer.BadParameter(f"{source} holds {len(dates)} dates: pass --date")
        game_date = day.date() if day is not None else dates[0]
    else:
        game_date = day.date() if day is not None else datetime.now(UTC).astimezone(ET).date()
        if r2:
            assert lake.objects is not None and lake.bucket is not None
            lake.pull("lineup_replacements", seasons=season)
            key = lp.ledger_key(game_date)
            listed = lake.objects.list_objects_v2(Bucket=lake.bucket, Prefix=key)
            # Only an absent ledger is "no ledger": any other storage error surfaces as it is.
            if not any(item["Key"] == key for item in listed.get("Contents", [])):
                typer.echo(f"no ledger for {game_date} in R2", err=True)
                raise typer.Exit(code=1)
            body = lake.objects.get_object(Bucket=lake.bucket, Key=key)
            ledger = pl.read_parquet(io.BytesIO(body["Body"].read()))
        else:
            ledger = lake.read("paper_ledger", seasons=season)
    ledger = ledger.filter(pl.col("game_date") == game_date)
    if ledger.is_empty():
        typer.echo(f"no ledger rows for {game_date}", err=True)
        raise typer.Exit(code=1)
    replacements = lake.read("lineup_replacements", seasons=season).filter(
        pl.col("game_date") == game_date
    )
    typer.echo(f"# Slate, {game_date}\n")
    typer.echo(lr.slate_markdown(ledger, replacements))
    from nhl_edge.live import attribution as la
    from nhl_edge.live import bundle as lb

    # Each bet's driver and parts, from the day's run bundle (#193): R2's, or a dry run's beside
    # its ledger.
    store: lb.Store | None = None
    if source is not None:
        store = lb.LocalStore(source.parent)
    elif r2:
        assert lake.objects is not None and lake.bucket is not None
        store = lb.R2Store(lake.objects, lake.bucket)
    typer.echo(la.slate_section(store, game_date, ledger, fit=fit))


@live_app.command("attribution-levels")
def live_attribution_levels(
    write: Annotated[
        bool,
        typer.Option(
            "--write",
            help="Fix them: written once to reports/live/ to commit, beside the live fit. "
            "Refused once written, or from uncommitted code.",
        ),
    ] = False,
    out: Annotated[Path, typer.Option(help="Where a dry run writes them, beside the lake.")] = Path(
        "data/live/attribution"
    ),
    fit: Annotated[
        Path | None,
        typer.Option(help="The live fit (default: the one under reports/live/)."),
    ] = None,
) -> None:
    """The usual level of each B3 input part at a market price, for attributing the live bets
    (#193): its least-squares line on logit p_mkt over the live fit's training games, E1's rows of
    2018-19 to 2022-23 rebuilt as nhl live blend-fit rebuilds them, each with its own fold's B3
    parts. Inputs and prices only, never a result. Fixed once with --write; without it, a dry
    run to --out."""
    import json
    from datetime import UTC

    import polars as pl

    from nhl_edge.backtest import reports
    from nhl_edge.game import b2, b3, uncertainty
    from nhl_edge.ingest.games import EXPECTED_GAMES
    from nhl_edge.lake.tables import Lake
    from nhl_edge.live import attribution as la
    from nhl_edge.live import blend_fit as bf
    from nhl_edge.settings import load_env

    load_env()
    now = datetime.now(UTC)
    version = reports.version(la.COMPONENT, now)
    if write and version.endswith("-dirty"):
        raise typer.BadParameter(
            "commit first: the usual levels are fixed once, reproducible from their commit",
            param_hint="--write",
        )
    fits = [fit] if fit is not None else sorted(bf.REPORTS.glob(f"{bf.COMPONENT}-*.json"))
    if len(fits) != 1:
        raise typer.BadParameter(f"one live fit needed, found {len(fits)}: pass --fit")
    record = json.loads(fits[0].read_text())
    live = bf.load(record)
    if write and la.path_for(live.version) is not None:
        typer.echo(f"the usual levels of {live.version} are fixed already", err=True)
        raise typer.Exit(code=1)
    lake = Lake()
    games, sbr_odds = lake.read("games"), lake.read("sbr_odds")
    last = max(bf.TRAINING_SEASONS)
    tables = b2.Tables(
        games,
        *(
            lake.read(name)
            for name in (
                "team_strength",
                "schedule_terms",
                "goalie_starts",
                "goalie_effects",
                "actual_lineups",
            )
        ),
    )
    b3_tables = _b3_tables(lake, tables)
    problems = [
        f"{season}: {height:,} of {EXPECTED_GAMES[season]:,} games"
        for season in bf.TRAINING_SEASONS
        if (height := games.filter(pl.col("season") == season).height) != EXPECTED_GAMES[season]
    ]
    problems += b2.input_problems(tables, last, EXPECTED_GAMES)
    problems += b3.input_problems(b3_tables, last)
    if problems:
        for problem in problems:
            typer.echo(problem, err=True)
        typer.echo("run nhl ingest, nhl odds sbr and the feature commands first", err=True)
        raise typer.Exit(code=1)
    u_tables = uncertainty.Tables(
        games,
        tables.goalie_starts,
        b3_tables.lineups,
        b3_tables.lineup_replacements,
        tables.actual_lineups,
        lake.read("player_league_seasons"),
    )
    priced = bf.market(sbr_odds, games)
    starts = {s: bf.fold_start(sbr_odds, games, s) for s in bf.TRAINING_SEASONS}
    rows, history = la.training_history(tables, b3_tables, u_tables, priced, starts)
    problems = la.identity_problems(rows, history, record)
    if problems:
        for problem in problems:
            typer.echo(problem, err=True)
        typer.echo(f"the rebuilt games are not {live.version}'s: nothing written", err=True)
        raise typer.Exit(code=1)
    try:
        training, cutoffs = la.levels(b3_tables, history, starts)
        levels = la.artifact(version, live.version, live.fold_start, training, cutoffs, now)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    body = json.dumps(levels, indent=2, sort_keys=True) + "\n"
    if write:
        path = bf.REPORTS / f"{version}.json"
        with path.open("x") as handle:
            handle.write(body)
    else:
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"{version}.json"
        path.write_text(body)
    typer.echo(
        f"{path}: {levels['training']['games']:,} training games of {live.version}, "
        f"train_cutoff {levels['train_cutoff']}"
    )
    for part, line in levels["lines"].items():
        typer.echo(f"  {part}: {line['intercept']:+.4f} {line['slope']:+.4f}·logit p_mkt")


@live_app.command("blend-fit")
def live_blend_fit(
    out: Annotated[
        Path, typer.Option(help="Where a dry run writes the fit, beside the lake.")
    ] = Path("data/live/blend"),
    r2: Annotated[
        bool,
        typer.Option(
            "--r2",
            help="The one fit of the season: claimed and written once to R2, and to "
            "reports/live/ to commit. Refused once claimed.",
        ),
    ] = False,
) -> None:
    """Fit the live blend once for 2026-27 (ADR 0030, #163). Rebuild E1's out-of-sample rows of
    2018-19 to 2022-23 (2022-23 predicted, never scored), check that the rows of 2018-19 to
    2021-22 reproduce the 2022-23 fold's fits the one run recorded, then fit u's scale, BLEND and
    its twins on every row public before the live fold starts, and B1 on every earlier SBR close.
    Without --r2 it is a dry run, written to --out."""
    import json
    from datetime import UTC

    import polars as pl

    from nhl_edge.backtest import blend as blend_backtest
    from nhl_edge.backtest import one_time, reports
    from nhl_edge.backtest.seasons import blend_training_seasons
    from nhl_edge.game import b2, b3, uncertainty
    from nhl_edge.ingest.games import EXPECTED_GAMES
    from nhl_edge.lake.r2 import R2Config
    from nhl_edge.lake.tables import LAKE_DIR, Lake
    from nhl_edge.live import blend_fit as bf
    from nhl_edge.settings import load_env

    load_env()
    now = datetime.now(UTC)
    version = reports.version(bf.COMPONENT, now)
    if r2 and (version.endswith("-dirty") or not bf.committed()):
        raise typer.BadParameter(
            f"commit first: the season's one fit, and {bf.REFERENCE} that checks it, must be "
            "reproducible from its commit",
            param_hint="--r2",
        )
    where = None
    if r2:
        config = R2Config.require("--r2")
        where = one_time.Places(LAKE_DIR, (bf.REPORTS,), config.client(), config.bucket, bf.CLAIM)
        # An earlier fit is the first refusal, before any table is read.
        earlier = one_time.records(where)
        if earlier:
            typer.echo(f"the season's live fit exists: {'; '.join(earlier)}", err=True)
            raise typer.Exit(code=1)
    lake = Lake()
    games, sbr_odds = lake.read("games"), lake.read("sbr_odds")
    last = max(bf.TRAINING_SEASONS)
    priced_seasons = set(sbr_odds["season"].unique().to_list())
    problems = [f"{s}: no SBR prices" for s in bf.TRAINING_SEASONS if s not in priced_seasons]
    problems += [
        f"{season}: {height:,} of {EXPECTED_GAMES[season]:,} games"
        for season in sorted(priced_seasons)
        if season <= last
        and (height := games.filter(pl.col("season") == season).height) != EXPECTED_GAMES[season]
    ]
    tables = b2.Tables(
        games,
        *(
            lake.read(name)
            for name in (
                "team_strength",
                "schedule_terms",
                "goalie_starts",
                "goalie_effects",
                "actual_lineups",
            )
        ),
    )
    b3_tables = _b3_tables(lake, tables)
    problems += b2.input_problems(tables, last, EXPECTED_GAMES)
    problems += b3.input_problems(b3_tables, last)
    if problems:
        for problem in problems:
            typer.echo(problem, err=True)
        typer.echo("run nhl ingest, nhl odds sbr and the feature commands first", err=True)
        raise typer.Exit(code=1)
    u_tables = uncertainty.Tables(
        games,
        tables.goalie_starts,
        b3_tables.lineups,
        b3_tables.lineup_replacements,
        tables.actual_lineups,
        lake.read("player_league_seasons"),
    )
    priced = bf.market(sbr_odds, games)
    starts = {s: bf.fold_start(sbr_odds, games, s) for s in bf.TRAINING_SEASONS}
    live_start = bf.fold_start(sbr_odds, games, bf.LIVE_SEASON)
    frames, parts = [], []
    for season in bf.TRAINING_SEASONS:
        predicted, doubts = bf.season_predictions(
            tables, b3_tables, u_tables, priced, season, starts[season]
        )
        frames.append(predicted)
        parts.append(doubts)
        counts = predicted.group_by("model").len().sort("model").iter_rows()
        typer.echo(f"  {season}: " + ", ".join(f"{m} {n:,}" for m, n in counts))
    rows = blend_backtest.rows(pl.concat(frames), {bf.EXPERIMENT.value: pl.concat(parts)}, games)
    problems = bf.early(rows, starts) + bf.short(rows, priced, bf.recorded_coverage())
    if problems:
        for problem in problems:
            typer.echo(problem, err=True)
        raise typer.Exit(code=1)
    try:
        reference = bf.fit(
            rows,
            blend_training_seasons(bf.REFERENCE_SEASON),
            starts[bf.REFERENCE_SEASON],
            bf.REFERENCE_SEASON,
        )
        reproduced = bf.describe(reference)
        differences = bf.differences(reproduced, bf.recorded())
        if differences:
            for line in differences:
                typer.echo(line, err=True)
            typer.echo(
                f"the rebuilt rows do not reproduce {bf.REFERENCE}'s 2022-23 fold: nothing written",
                err=True,
            )
            raise typer.Exit(code=1)
        typer.echo(f"reproduced the {bf.REFERENCE_SEASON} fold's fits of {bf.REFERENCE}")
        fold = bf.fit(rows, list(bf.TRAINING_SEASONS), live_start, bf.LIVE_SEASON)
        record = bf.artifact(
            version, fold, bf.b1_fit(priced, live_start), live_start, now, reproduced
        )
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    body = json.dumps(record, indent=2, sort_keys=True) + "\n"
    if not r2:
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"{version}.json"
        path.write_text(body)
    else:
        assert where is not None
        objects, key = where.objects, f"{bf.R2_PREFIX}/{version}.json"
        # Written before the claim, so a refused upload leaves the fit to copy, not to refit.
        bf.REPORTS.mkdir(parents=True, exist_ok=True)
        path = bf.REPORTS / f"{version}.json"
        with path.open("x") as handle:
            handle.write(body)
        try:
            # The season has one fit (ADR 0030): claimed before its copy goes to R2.
            one_time.claim(where, version)
        except ValueError as exc:
            path.unlink()
            typer.echo(f"the live fit was not written: {exc}", err=True)
            raise typer.Exit(code=1) from None
        try:
            objects.put_object(Bucket=where.bucket, Key=key, Body=body.encode(), IfNoneMatch="*")
        except Exception as exc:
            typer.echo(
                f"claimed, but R2 refused {key} ({exc}): upload {path} there unchanged, "
                "never refit",
                err=True,
            )
            raise typer.Exit(code=1) from None
    typer.echo(
        f"{path}: {record['training']['games']:,} training games, fold start "
        f"{record['fold_start']}, train_cutoff {record['train_cutoff']}"
    )
    for name, entry in record["fits"].items():
        weights = ", ".join(f"{t} {w:+.3f}" for t, w in entry["weights"].items())
        typer.echo(f"  {name}: {weights}")
    b1 = record["b1"]
    typer.echo(
        f"  B1: intercept {b1['intercept']:+.4f}, slope {b1['slope']:.4f} on {b1['games']:,}"
    )


@odds_app.command()
def snapshot(
    regions: Annotated[str, typer.Option(help="Odds API regions.")] = "eu",
    slot_plan: Annotated[str, typer.Option(help="Markets per time slot.")] = "free-tier",
    skip_if_no_games: Annotated[
        bool, typer.Option(help="Exit quietly when the NHL schedule has no games in the slot.")
    ] = False,
    cron: Annotated[
        str | None,
        typer.Option(help="Cron line that fired the run (github.event.schedule); picks the slot."),
    ] = None,
    slot: Annotated[str | None, typer.Option(help="Slot name, for manual runs.")] = None,
    mirror_raw: Annotated[
        bool, typer.Option(help="Mirror the raw response to R2 (needs the R2_* variables).")
    ] = False,
    dry_run: Annotated[
        bool, typer.Option(help="Fetch and parse, keep the raw copy locally, skip R2 and Supabase.")
    ] = False,
) -> None:
    """Pull one odds snapshot into odds_snapshots."""
    import polars as pl

    from nhl_edge.ingest.nhl_api import NhlApi
    from nhl_edge.ingest.odds import (
        SLOT_PLANS,
        OddsApi,
        Sink,
        resolve_slot,
        run_snapshot,
        slot_by_name,
        utc_now,
    )
    from nhl_edge.lake.raw import RawStore
    from nhl_edge.lake.schemas import ODDS_KEY
    from nhl_edge.lake.supabase import Supabase
    from nhl_edge.settings import load_env, require

    if slot_plan not in SLOT_PLANS:
        raise typer.BadParameter(f"choose one of {sorted(SLOT_PLANS)}", param_hint="--slot-plan")
    if (cron is None) == (slot is None):
        raise typer.BadParameter("pass exactly one of --cron and --slot")
    now = utc_now()
    if cron is not None:
        chosen = resolve_slot(slot_plan, cron, now.date())
        if chosen is None:
            typer.echo(f"odds snapshot: cron {cron!r} is not a slot at this time of year, skipped")
            return
    else:
        try:
            chosen = slot_by_name(slot_plan, slot or "")
        except KeyError as exc:
            raise typer.BadParameter(str(exc), param_hint="--slot") from None

    load_env()
    store = RawStore.from_env(mirror=mirror_raw and not dry_run)
    sink: Sink | None = None
    if not dry_run:
        supabase = Supabase.from_env()
        typer.echo(f"supabase project: {supabase.project_ref}")

        def write(frame: pl.DataFrame) -> int:
            return supabase.insert_new("odds_snapshots", frame, ODDS_KEY)

        sink = write

    run_snapshot(
        slot=chosen,
        regions=regions,
        skip_if_no_games=skip_if_no_games,
        nhl=NhlApi(store),
        odds=OddsApi(require("ODDS_API_KEY")),
        store=store,
        sink=sink,
        now=now,
        echo=typer.echo,
    )


@odds_app.command()
def backfill() -> None:
    """Backfill historical odds from the paid Odds API endpoint."""
    _not_implemented("odds backfill", "deferred to v2")


@odds_app.command()
def sbr(
    seasons: Annotated[
        str,
        typer.Option(
            help="Seasons as 20182019, a comma list or a range, within 2010-11 to 2022-23."
        ),
    ] = "20102011-20222023",
    replay: Annotated[
        bool, typer.Option("--replay", help="Parse the stored pages only, no network.")
    ] = False,
    r2: Annotated[
        bool,
        typer.Option(
            "--r2", help="Mirror raw pages to R2 (and restore them first), and the table."
        ),
    ] = False,
) -> None:
    """Import the SBR odds archive into the lake's sbr_odds table, matched to NHL games through
    the schedule table, and print the join rate per season. Pages are fetched once and kept raw."""
    import polars as pl

    from nhl_edge.ingest.nhl_ingest import parse_seasons
    from nhl_edge.ingest.sbr import SBR_SEASONS, SOURCE, SbrArchive, import_seasons, report_lines
    from nhl_edge.lake.raw import RawStore
    from nhl_edge.lake.tables import Lake
    from nhl_edge.settings import load_env

    try:
        wanted = parse_seasons(seasons)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--seasons") from None
    outside = [season for season in wanted if season not in SBR_SEASONS]
    if outside:
        raise typer.BadParameter(f"SBR has no NHL odds for {outside}", param_hint="--seasons")
    load_env()
    store = RawStore.from_env(mirror=r2, flag="--r2")
    if r2:
        restored = store.restore_from_r2(f"{SOURCE}/").copied
        typer.echo(f"restored {restored} raw SBR pages from R2")
    lake = Lake.from_env(mirror=r2)
    if r2:
        # Only the imported seasons, and only files this machine lacks or holds differently.
        pulled = lake.pull("schedule", wanted) + lake.pull("games", wanted)
        typer.echo(f"pulled {pulled} schedule and games files from R2")
    schedule, results = lake.read("schedule"), lake.read("games")
    archive = SbrArchive(store, offline=replay)
    try:
        # Every season is checked and parsed before anything is written.
        frames, reports = import_seasons(archive, wanted, schedule, results)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    # import_seasons refuses a season with no prices, so every season's partition is replaced.
    for frame in frames.values():
        lake.write("sbr_odds", frame)
    typer.echo("\n".join(report_lines(reports)))
    total = pl.concat(frames.values()).height if frames else 0
    typer.echo(
        f"sbr_odds: {total:,} prices over {len(frames)} seasons, {archive.requests} requests"
    )


@odds_app.command()
def replay(
    start: Annotated[
        datetime | None, typer.Option(formats=["%Y-%m-%d"], help="First snapshot date (UTC).")
    ] = None,
    end: Annotated[
        datetime | None,
        typer.Option(formats=["%Y-%m-%d"], help="Last snapshot date (default: --start)."),
    ] = None,
    recent: Annotated[
        int | None, typer.Option(min=1, help="Snapshot dates of the last N days, today included.")
    ] = None,
    r2: Annotated[
        bool,
        typer.Option(
            "--r2",
            help="Restore the raw snapshots and schedules from R2 first, and mirror the table.",
        ),
    ] = False,
    supabase: Annotated[
        bool,
        typer.Option(
            "--supabase",
            help="Upsert the replayed dates' h2h quotes of started games that Supabase holds, with "
            "their closing-proxy flag. Needs --r2, so the flag is derived over the whole history.",
        ),
    ] = False,
) -> None:
    """Rebuild the lake's odds_snapshots from the stored raw snapshots, matching each event to its
    NHL game and marking each started game's closing proxy (#21, ADR 0033). Never calls the Odds
    API. Without a window, every stored snapshot is replayed."""
    from datetime import UTC, timedelta

    from nhl_edge.ingest.odds_lake import SCHEDULE_DAYS, SCHEDULE_PREFIX, replay_odds
    from nhl_edge.lake.raw import RawStore
    from nhl_edge.lake.tables import Lake
    from nhl_edge.settings import load_env

    if start is not None and recent is not None:
        raise typer.BadParameter("pass at most one of --start and --recent")
    if end is not None and start is None:
        raise typer.BadParameter("--end needs --start")
    if supabase and not r2:
        # A local lake may lack part of a game's history, and its flags would reach production.
        raise typer.BadParameter("--supabase needs --r2: the flag is derived over R2's history")
    dates = None
    if start is not None:
        first, last = start.date(), (end or start).date()
        if last < first:
            raise typer.BadParameter("--end is before --start")
        dates = [first + timedelta(days=i) for i in range((last - first).days + 1)]
    elif recent is not None:
        today = datetime.now(UTC).date()
        dates = [today - timedelta(days=i) for i in range(recent - 1, -1, -1)]

    load_env()
    store = RawStore.from_env(mirror=r2, flag="--r2")
    if r2:
        if dates is None:
            prefixes = ["odds/", f"{SCHEDULE_PREFIX}/"]
        else:
            # Snapshots of the window, and the schedules that can list their games: events are
            # priced up to about ten days ahead, and a schedule response covers seven days.
            span = range(-SCHEDULE_DAYS - 1, (dates[-1] - dates[0]).days + 3 * SCHEDULE_DAYS)
            prefixes = [f"odds/{day.isoformat()}/" for day in dates]
            prefixes += [
                f"{SCHEDULE_PREFIX}/{(dates[0] + timedelta(days=d)).isoformat()}/" for d in span
            ]
        restored = sum(store.restore_from_r2(prefix).copied for prefix in prefixes)
        typer.echo(f"restored {restored} raw responses from R2")
    lake = Lake.from_env(mirror=r2)
    if r2:
        # The whole history, over which each closing proxy is derived (#21).
        typer.echo(f"pulled {lake.pull('odds_snapshots'):,} odds_snapshots files from R2")
    now = datetime.now(UTC)
    report = replay_odds(store, lake, dates, now=now)
    if supabase:
        import polars as pl

        from nhl_edge.ingest.odds import supabase_window
        from nhl_edge.lake.schemas import ODDS_KEY, OddsSnapshots, dtypes
        from nhl_edge.lake.supabase import Supabase

        # The rows the snapshot job inserted (supabase_window) of games started by now: the flag
        # is final for them. Upserted on the quote's key, the rest of the row unchanged.
        replayed = lake.read("odds_snapshots").filter(
            pl.col("snapshot_date").is_in([*report.dates, *report.reflagged]),
            pl.col("market") == "h2h",
            pl.col("commence_time_utc") <= now,
        )
        rows = supabase_window(replayed).select(list(dtypes(OddsSnapshots)))
        sent = Supabase.from_env().upsert("odds_snapshots", rows, ODDS_KEY)
        pairs = rows.filter("is_closing_proxy").select(*ODDS_KEY[:4]).unique().height
        typer.echo(f"supabase odds_snapshots: {sent:,} h2h quotes upserted, {pairs:,} proxies")
    matched = ", ".join(f"{kind} {n}" for kind, n in sorted(report.matched.items())) or "none"
    window = f"{report.dates[0]}..{report.dates[-1]}" if report.dates else "no snapshots"
    typer.echo(
        f"odds replay {window}: {report.snapshots} snapshots, {report.quotes:,} quotes, "
        f"{report.events} events; matched: {matched}; unmatched {len(report.unmatched)}"
    )
    for event_id, home, away, commence in report.unmatched:
        typer.echo(f"  unmatched {event_id}: {away} at {home}, {commence:%Y-%m-%d %H:%M} UTC")
    if report.in_play:
        typer.echo(f"  left out {report.in_play:,} quotes on games already under way")
    if report.reflagged:
        days = ", ".join(str(day) for day in report.reflagged)
        typer.echo(f"  closing proxies moved on other dates, rewritten: {days}")
    for raw_key in report.incomplete:
        typer.echo(f"warning: {raw_key} has no sidecar (an interrupted write), skipped")


@goalies_app.command()
def poll(
    cron: Annotated[
        str | None,
        typer.Option(
            help="Cron line that fired the run; the poll is skipped when it is not an odds slot."
        ),
    ] = None,
    within: Annotated[
        int,
        typer.Option(min=1, help="Poll games starting within this many minutes (default 18 h)."),
    ] = 18 * 60,
    daily_faceoff: Annotated[
        bool, typer.Option(help="Also store Daily Faceoff's starting-goalies page of each date.")
    ] = True,
    lines: Annotated[
        bool,
        typer.Option(
            help="With --daily-faceoff, also store Daily Faceoff's line-combinations page of each "
            "team playing in the window (#121): the slot polls only."
        ),
    ] = False,
    mirror_raw: Annotated[
        bool, typer.Option(help="Mirror the raw responses to R2 (needs the R2_* variables).")
    ] = False,
) -> None:
    """Store the pre-game boxscore, landing and right-rail of every game starting soon, Daily
    Faceoff's starting goalies for their dates, and with --lines its line combinations of every
    team playing."""
    from datetime import timedelta

    from nhl_edge.ingest import dailyfaceoff
    from nhl_edge.ingest.nhl_api import NhlApi, utc_now
    from nhl_edge.ingest.odds import resolve_slot
    from nhl_edge.ingest.pregame import run_poll
    from nhl_edge.lake.raw import RawStore
    from nhl_edge.settings import load_env

    now = utc_now()
    if cron is not None and resolve_slot("free-tier", cron, now.date()) is None:
        typer.echo(f"goalie poll: cron {cron!r} is not an odds slot at this time of year, skipped")
        return
    load_env()
    store = RawStore.from_env(mirror=mirror_raw)
    report = run_poll(
        nhl=NhlApi(store), now=now, echo=typer.echo, horizon=timedelta(minutes=within)
    )
    failed_pages: list[object] = []
    if daily_faceoff and report.dates:
        dfo = dailyfaceoff.DailyFaceoff(store)
        failed_pages += dailyfaceoff.run_poll(dfo=dfo, days=report.dates, echo=typer.echo)
        if lines:
            failed_pages += dailyfaceoff.run_lines_poll(
                dfo=dfo, teams=report.playing, echo=typer.echo
            )
    if report.failed or failed_pages:
        raise typer.Exit(code=1)


@goalies_app.command("replay")
def goalies_replay(
    recent: Annotated[
        int | None,
        typer.Option(min=1, help="Game dates (US Eastern) of the last N days, today included."),
    ] = None,
    r2: Annotated[
        bool,
        typer.Option("--r2", help="Restore the raw pre-game boxscores from R2 first, and mirror."),
    ] = False,
) -> None:
    """Rebuild the lake's pregame_goalies, dailyfaceoff_goalies and dailyfaceoff_lines from the
    stored pre-game boxscores and Daily Faceoff pages. Never calls either source. Without
    --recent, every stored date is replayed."""
    from datetime import timedelta

    from nhl_edge.ingest import dailyfaceoff
    from nhl_edge.ingest.nhl_api import utc_now
    from nhl_edge.ingest.pregame import BOXSCORE_PREFIX, ET, replay_pregame_goalies
    from nhl_edge.lake.raw import RawStore
    from nhl_edge.lake.tables import Lake
    from nhl_edge.settings import load_env

    dates = None
    if recent is not None:
        today = utc_now().astimezone(ET).date()
        dates = [today - timedelta(days=i) for i in range(recent - 1, -1, -1)]
    load_env()
    store = RawStore.from_env(mirror=r2, flag="--r2")
    if r2:
        roots = [BOXSCORE_PREFIX, dailyfaceoff.PREFIX, dailyfaceoff.LINES_PREFIX]
        prefixes = (
            [f"{root}/" for root in roots]
            if dates is None
            else [f"{root}/{day.isoformat()}/" for root in roots for day in dates]
        )
        restored = sum(store.restore_from_r2(prefix).copied for prefix in prefixes)
        typer.echo(f"restored {restored} raw responses from R2")
    lake = Lake.from_env(mirror=r2)
    report = replay_pregame_goalies(store, lake, dates)
    window = f"{report.dates[0]}..{report.dates[-1]}" if report.dates else "no stored polls"
    typer.echo(
        f"goalie replay {window}: {report.responses} boxscores, {report.rows} rows; "
        f"{report.after_start} fetched after the start left out, "
        f"{len(report.incomplete)} incomplete"
    )
    dfo = dailyfaceoff.replay_starting_goalies(store, lake, dates)
    window = f"{dfo.dates[0]}..{dfo.dates[-1]}" if dfo.dates else "no stored pages"
    typer.echo(
        f"daily faceoff replay {window}: {dfo.pages} pages, {dfo.rows} rows; "
        f"{len(dfo.incomplete)} incomplete"
    )
    team_pages = dailyfaceoff.replay_line_combinations(store, lake, dates)
    window = (
        f"{team_pages.dates[0]}..{team_pages.dates[-1]}" if team_pages.dates else "no stored pages"
    )
    typer.echo(
        f"daily faceoff lines replay {window}: {team_pages.pages} team pages, "
        f"{team_pages.rows} rows; {len(team_pages.incomplete)} incomplete, "
        f"{len(team_pages.wrong_team)} another team's page, {len(team_pages.unparsed)} unparsed"
    )


@lake_app.command()
def size(
    max_gb: Annotated[
        float, typer.Option(help="Fail above this many GB (2^30 bytes); R2 is free to 10 GB.")
    ] = 8.0,
) -> None:
    """Report the R2 bucket's object count and size, and fail loudly above --max-gb."""
    import os

    from nhl_edge.lake.r2 import R2Config, bucket_usage
    from nhl_edge.settings import load_env

    load_env()
    config = R2Config.require("nhl lake size")
    count, total = bucket_usage(config.client(), config.bucket)
    gb = total / 2**30
    typer.echo(f"R2 bucket {config.bucket}: {count} objects, {gb:.3f} GB (limit {max_gb:g} GB)")
    if gb > max_gb:
        prefix = "::error::" if os.environ.get("GITHUB_ACTIONS") == "true" else "error: "
        typer.echo(
            f"{prefix}R2 bucket is {gb:.2f} GB, above the {max_gb:g} GB limit. R2 charges above "
            "10 GB and has no spending cap: prune or move data before it grows further.",
            err=True,
        )
        raise typer.Exit(code=1)


PrefixOption = Annotated[
    str, typer.Option(help="Only raw keys starting with this, such as odds/ or nhl/schedule/.")
]
WorkersOption = Annotated[int, typer.Option(min=1, help="Parallel transfers.")]


@lake_app.command("sync-raw")
def sync_raw(prefix: PrefixOption = "", workers: WorkersOption = 16) -> None:
    """Upload local raw responses that R2 lacks, such as those cached without --r2."""
    from nhl_edge.lake.raw import RawStore
    from nhl_edge.settings import load_env

    load_env()
    counts = RawStore.from_env(mirror=True, flag="nhl lake sync-raw").sync_to_r2(prefix, workers)
    typer.echo(
        f"raw/{prefix}: {counts.local} responses here, {counts.remote} in R2; uploaded "
        f"{counts.copied}, skipped {counts.skipped} incomplete here"
    )


@lake_app.command("restore-raw")
def restore_raw(prefix: PrefixOption = "", workers: WorkersOption = 16) -> None:
    """Download raw responses from R2 that are missing here, never overwriting a local file. Keeps
    this machine a second copy of the raw cache and lets --replay run anywhere."""
    from nhl_edge.lake.raw import RawStore
    from nhl_edge.settings import load_env

    load_env()
    store = RawStore.from_env(mirror=True, flag="nhl lake restore-raw")
    counts = store.restore_from_r2(prefix, workers)
    typer.echo(
        f"raw/{prefix}: {counts.remote} responses in R2, {counts.local} here; downloaded "
        f"{counts.copied}, skipped {counts.skipped} incomplete"
    )


@audit_app.command("shifts")
def audit_shifts(
    seasons: Annotated[
        str | None,
        typer.Option(
            help="Seasons as 20232024, a comma list or a range. Default: every season that may "
            "inform design, which leaves out the one-time test season."
        ),
    ] = None,
) -> None:
    """Shift chart coverage per season, from the lake's shift_coverage table."""
    import polars as pl

    from nhl_edge.backtest.seasons import SEASON_ROLES, SeasonRole
    from nhl_edge.ingest.nhl_ingest import parse_seasons
    from nhl_edge.ingest.shift_coverage import markdown_report, season_report
    from nhl_edge.lake.tables import Lake

    coverage = Lake().read("shift_coverage")
    if seasons is None:
        # The report informs design choices, such as which charts RAPM trusts, so the one-time
        # test season (and live seasons) are shown only when asked for by name.
        wanted = [s for s, role in SEASON_ROLES.items() if role is not SeasonRole.ONE_TIME_TEST]
    else:
        try:
            wanted = parse_seasons(seasons)
        except ValueError as exc:
            raise typer.BadParameter(str(exc), param_hint="--seasons") from None
    coverage = coverage.filter(pl.col("season").is_in(wanted))
    if coverage.is_empty():
        typer.echo("no shift coverage in the lake for these seasons: run nhl ingest", err=True)
        raise typer.Exit(code=1)
    typer.echo(markdown_report(season_report(coverage)))


@audit_app.command("reference")
def audit_reference() -> None:
    """Check the reference files (team codes, arenas, venues, home arenas, coach tenures,
    attendance limits) against every game in the lake's games table."""
    from nhl_edge.lake.tables import Lake
    from nhl_edge.reference import check_games

    games = Lake().read("games")
    if games.is_empty():
        typer.echo("no games in the lake: run nhl ingest", err=True)
        raise typer.Exit(code=1)
    problems = check_games(games)
    typer.echo(
        f"{games.height:,} games, {games['season'].min()} to {games['season'].max()}: "
        f"{len(problems)} problems"
    )
    for problem in problems:
        typer.echo(f"  {problem}")
    if problems:
        raise typer.Exit(code=1)


@audit_app.command("report")
def audit_report(
    out: Annotated[Path, typer.Option(help="Report directory.")] = DEFAULT_AUDIT_OUT,
    as_of: Annotated[
        str | None,
        typer.Option(
            help="Last ET game date the report covers, as 2026-10-15. Default: yesterday."
        ),
    ] = None,
) -> None:
    """Write the data audit report (#9) to <out>/<as-of>.md: games per season, shift coverage,
    reference files and live odds snapshots, from the local lake and raw cache."""
    from datetime import UTC, date

    from nhl_edge.audit.report import build, markdown
    from nhl_edge.ingest.nhl_ingest import yesterday_et
    from nhl_edge.lake.raw import RawStore
    from nhl_edge.lake.tables import Lake

    now = datetime.now(UTC)
    try:
        last = date.fromisoformat(as_of) if as_of else yesterday_et(now)
    except ValueError:
        raise typer.BadParameter(f"not a date: {as_of}", param_hint="--as-of") from None
    lake = Lake()
    if lake.read("games").is_empty():
        typer.echo("no games in the lake: run nhl ingest", err=True)
        raise typer.Exit(code=1)
    sections = build(lake, RawStore(), last)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{last.isoformat()}.md"
    path.write_text(markdown(sections, last, now))
    total = sum(len(section.problems) for section in sections)
    typer.echo(f"{path}: {total} problems")
    for section in sections:
        typer.echo(f"  {section.title}: {len(section.problems)}")


@audit_app.command("gaps")
def audit_gaps(
    gaps: Annotated[Path, typer.Option(help="A backtest's gaps_b3.csv.")] = DEFAULT_BACKTEST_OUT
    / "gaps_b3.csv",
    out: Annotated[Path, typer.Option(help="Report directory.")] = Path("reports/gaps"),
    blend: Annotated[
        bool,
        typer.Option(
            "--blend",
            help="Screen the market blend's gaps (a backtest's gaps_blend.csv, E1 and E2), every "
            "one for review (#144).",
        ),
    ] = False,
) -> None:
    """Screen B3's gaps above 8 points against B1 at the close (E1) for bug signatures, hard rule
    8's review (#107): each game's log-odds in parts, both teams' expected goals and the flags,
    to <out>/b3-gaps-<version>.md (the games to review) and .csv (every gap). No result is
    read. With --blend, the market blend's gaps instead, each game once, with B3's own terms at
    the backtest's prediction time, to <out>/blend-gaps-<version>.md and .csv; each gap's blend
    probability is first recomputed from its fold's fit in the run's summary.json (#156)."""
    from datetime import UTC

    import polars as pl

    from nhl_edge.audit import b3_gaps
    from nhl_edge.backtest import reports, walk_forward
    from nhl_edge.backtest.walk_forward import fold_start
    from nhl_edge.game import b2
    from nhl_edge.lake.tables import Lake

    if blend and gaps == DEFAULT_BACKTEST_OUT / "gaps_b3.csv":
        gaps = DEFAULT_BACKTEST_OUT / "gaps_blend.csv"
    if not gaps.exists():
        raise typer.BadParameter(f"{gaps} does not exist: run nhl backtest", param_hint="--gaps")
    rows = pl.read_csv(gaps, try_parse_dates=True)
    try:
        rows = b3_gaps.blend_gaps(rows) if blend else rows.filter(pl.col("experiment") == "E1")
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    lake = Lake()
    games = lake.read("games")
    tables = b2.Tables(
        games,
        *(
            lake.read(name)
            for name in (
                "team_strength",
                "schedule_terms",
                "goalie_starts",
                "goalie_effects",
                "actual_lineups",
            )
        ),
    )
    seasons = sorted(int(s) for s in rows["season"].unique().to_list())
    b3_tables = _b3_tables(lake, tables)
    sbr_odds = lake.read("sbr_odds").filter(pl.col("season").is_in(seasons)) if blend else None
    if blend:
        assert sbr_odds is not None
        # Each experiment's gaps are refit at its own fold start, as in the backtest: E2's comes
        # at the season's first opener when that is before its first game.
        folds = walk_forward.fold_starts(sbr_odds, games, seasons)
        batches = [
            (str(experiment), part, {s: folds[(str(experiment), s)] for s in seasons})
            for (experiment,), part in rows.group_by("experiment", maintain_order=True)
        ]
    else:
        calendar = games.select("season", "start_utc")
        batches = [("E1", rows, {s: fold_start(calendar, s) for s in seasons})]
    try:
        found = [(name, b3_gaps.screen(b3_tables, part, starts)) for name, part, starts in batches]
        if sbr_odds is not None:
            _check_blend_gaps(rows, gaps, games, sbr_odds, tables, b3_tables, lake)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    frames = [frame.with_columns(experiment=pl.lit(name)) for name, frame in found if frame.height]
    if not frames:
        typer.echo(f"{gaps}: no gaps to screen")
        return
    screened = pl.concat(frames, how="diagonal_relaxed").sort(
        "season", "game_date", "game_id", "experiment"
    )
    marked = b3_gaps.every_gap(screened) if blend else b3_gaps.review_set(screened)
    now = datetime.now(UTC)
    version = reports.version("blend-gaps" if blend else "b3-gaps", now)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{version}.md"
    path.write_text(b3_gaps.markdown(marked, str(gaps), version, "blend" if blend else "B3"))
    marked.write_csv(out / f"{version}.csv")
    facts = b3_gaps.summary(marked)
    typer.echo(f"{path}: {facts['games']:,} gaps, {facts['flagged']} flagged")
    for flag, count in facts["flags"].items():
        typer.echo(f"  {flag}: {count}")
    typer.echo("  to review: " + ", ".join(f"{k} {v}" for k, v in facts["review"].items()))


@app.command()
def status(
    brief: Annotated[bool, typer.Option(help="One line, for the SessionStart hook.")] = False,
) -> None:
    """Show data and model state. Open tasks live in GitHub issues."""
    from nhl_edge.lake.status import brief_line, compact, local_tables
    from nhl_edge.lake.tables import Lake

    states = local_tables(Lake())
    typer.echo(f"nhl-edge {__version__} · {brief_line(states)} · no models fitted")
    if brief:
        return
    typer.echo("\nLocal lake (data/lake):")
    for state in states:
        rows = compact(state.rows or 0)
        latest = f", to {state.latest}" if state.latest else ""
        typer.echo(f"  {state.table:<16}{rows:>8} rows in {len(state.files):,} files{latest}")
    _status_against_r2(states)
    typer.echo("\nSeason roles (docs/plan.md section 5):")
    for season, role in SEASON_ROLES.items():
        typer.echo(f"  {season}  {role}")
    typer.echo("  20262027+ live")


# Replayed tables whose command takes --seasons: sbr_odds refuses a season whose schedule is
# incomplete here, so its command names only the seasons that drifted.
SEASON_SCOPED = frozenset({"sbr_odds"})


def _replay_command(command: str, table: str, keys: list[str]) -> str:
    """The replay command for a table, with --seasons from its drifted partition keys (such as
    sbr_odds/season=20182019/part-0.parquet) when the table is season-scoped."""
    if table not in SEASON_SCOPED:
        return command
    seasons = sorted(
        {
            part.removeprefix("season=")
            for key in keys
            if key.split("/")[0] == table
            for part in key.split("/")
            if part.startswith("season=")
        }
    )
    return f"{command} --seasons {','.join(seasons)}" if seasons else command


def _status_against_r2(local: "list[TableState]") -> None:
    """Which lake files and daily raw responses differ between this machine and R2, with the
    commands that bring them in step. Skipped without R2 settings."""
    from nhl_edge.lake.r2 import R2Config
    from nhl_edge.lake.raw import RawStore
    from nhl_edge.lake.status import (
        PREGAME_RAW,
        TOI_RAW,
        compare,
        raw_lag,
        raw_sets,
        remote_tables,
        replay_window,
    )

    # Tables rebuilt by a replay of their own raw responses, not by the NHL ingest.
    REPLAYED = {
        "odds_snapshots": "nhl odds replay --r2",
        "pregame_goalies": "nhl goalies replay --r2",
        "dailyfaceoff_goalies": "nhl goalies replay --r2",
        "dailyfaceoff_lines": "nhl goalies replay --r2",
        "sbr_odds": "nhl odds sbr --replay --r2",
        "player_league_seasons": "nhl player-seasons --r2",
    }
    # Tables fitted from the others, rebuilt by their own command in this order (#110).
    FITTED = {
        "shot_xg": "nhl xg",
        "stints": "nhl stints",
        "team_strength": "nhl team-strength",
        "goalie_starts": "nhl goalie-start",
        "goalie_effects": "nhl goalie-effect",
        "schedule_terms": "nhl schedule-terms",
        "lineups": "nhl lineups",
        "lineup_replacements": "nhl lineups",
        "player_ratings": "nhl rapm",
        "rapm_terms": "nhl rapm",
    }
    from nhl_edge.lake.tables import Lake
    from nhl_edge.settings import load_env

    load_env()
    if R2Config.from_env() is None:
        typer.echo("\nR2: not configured, so no comparison with the mirror")
        return
    typer.echo("\nAgainst R2:")
    missing_here: list[str] = []  # lake files R2 has and this machine lacks
    missing_there: list[str] = []  # lake files this machine has that R2 lacks
    differ: list[str] = []  # lake files on both sides with different sizes
    for here, there in zip(local, remote_tables(Lake.from_env(mirror=True)), strict=True):
        diff = compare(here, there)
        if diff:
            typer.echo(
                f"  {diff.table}: {len(diff.only_there):,} files only in R2, "
                f"{len(diff.only_here):,} only here, {len(diff.differ):,} differ in size"
            )
            missing_here += diff.only_there
            missing_there += diff.only_here
            differ += diff.differ
    store = RawStore.from_env(mirror=True)
    raw_behind = raw_ahead = False
    for prefix, here_key, there_key in raw_lag(store):
        if here_key != there_key:
            raw_behind |= (here_key or "") < (there_key or "")
            raw_ahead |= (here_key or "") > (there_key or "")
            typer.echo(f"  raw/{prefix}: newest here {here_key}, in R2 {there_key}")
    # The pre-game polls have no replay into another raw source: only their two copies keep them.
    polls_behind = polls_ahead = False
    for prefix, only_here, only_there in raw_sets(store, PREGAME_RAW):
        if only_here or only_there:
            polls_behind |= only_there > 0
            polls_ahead |= only_here > 0
            typer.echo(
                f"  raw/{prefix}: {only_there:,} responses only in R2, {only_here:,} only here"
            )
    # Nor can the time-on-ice reports: the owner allowed one fetch (#68, #114). Without them, the
    # replay falls back to the games' empty shift charts.
    reports_behind = reports_ahead = False
    for prefix, only_here, only_there in raw_sets(store, TOI_RAW):
        if only_here or only_there:
            reports_behind |= only_there > 0
            reports_ahead |= only_here > 0
            typer.echo(f"  raw/{prefix}: {only_there:,} pages only in R2, {only_here:,} only here")
    # The replayed tables are rebuilt by their own replay, every other table by the NHL ingest.
    replayed_here = sorted(
        {key.split("/")[0] for key in missing_here if key.split("/")[0] in REPLAYED}
    )
    replayed_there = sorted(
        {key.split("/")[0] for key in missing_there if key.split("/")[0] in REPLAYED}
    )
    fitted_here = [t for t in FITTED if any(key.split("/")[0] == t for key in missing_here)]
    fitted_there = [t for t in FITTED if any(key.split("/")[0] == t for key in missing_there)]
    drifted_here, drifted_there = missing_here, missing_there
    own = {*REPLAYED, *FITTED}
    missing_here = [key for key in missing_here if key.split("/")[0] not in own]
    missing_there = [key for key in missing_there if key.split("/")[0] not in own]
    if missing_here or raw_behind:
        window = replay_window(missing_here) or "--recent 3"
        typer.echo(
            f"  this machine is behind R2: nhl lake restore-raw, then nhl ingest {window} --replay"
        )
    for table in replayed_here:
        command = _replay_command(REPLAYED[table], table, drifted_here)
        typer.echo(f"  {table} is behind R2: {command} restores and rebuilds it")
    if missing_there or raw_ahead:
        window = replay_window(missing_there) or "--recent 3"
        typer.echo(
            f"  R2 lacks what is here: nhl lake sync-raw, then nhl ingest {window} --replay --r2"
        )
    for table in replayed_there:
        command = _replay_command(REPLAYED[table], table, drifted_there)
        typer.echo(f"  R2 lacks {table} rows here: nhl lake sync-raw, then {command}")
    for table in fitted_here:
        typer.echo(
            f"  {table} is behind R2: with the tables it reads up to date, {FITTED[table]} "
            "rebuilds it"
        )
    for table in fitted_there:
        typer.echo(
            f"  R2 lacks {table} rows here: {FITTED[table]} --r2 from a clean main checkout "
            "writes them"
        )
    if polls_behind:
        typer.echo("  pre-game goalie polls are behind R2: nhl lake restore-raw copies them")
    if polls_ahead:
        typer.echo("  R2 lacks pre-game goalie polls here: nhl lake sync-raw copies them")
    if reports_behind:
        typer.echo("  time-on-ice reports are behind R2: nhl lake restore-raw copies them")
    if reports_ahead:
        typer.echo("  R2 lacks time-on-ice reports here: nhl lake sync-raw copies them")
    if differ:
        # A size difference does not tell which copy is current, so no direction is suggested.
        tables = sorted({key.split("/")[0] for key in differ})
        typer.echo(
            f"  {len(differ):,} files differ from R2 in {', '.join(tables)}: check which copy is "
            "current before syncing either way"
        )
    in_step = not (
        missing_here
        or missing_there
        or replayed_here
        or replayed_there
        or fitted_here
        or fitted_there
        or differ
    )
    raw_drift = raw_behind or raw_ahead or polls_behind or polls_ahead
    if in_step and not (raw_drift or reports_behind or reports_ahead):
        typer.echo("  up to date with R2")
