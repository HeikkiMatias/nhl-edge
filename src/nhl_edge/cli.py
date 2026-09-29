"""The `nhl` command line: one interface for you, Claude Code and GitHub Actions."""

from datetime import datetime
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from nhl_edge import __version__
from nhl_edge.backtest.seasons import DEVELOPMENT_SEASONS, SEASON_ROLES

app = typer.Typer(
    help="NHL moneyline model that must add information beyond a recalibrated market.",
    no_args_is_help=True,
)
odds_app = typer.Typer(help="Live odds snapshots and historical odds.", no_args_is_help=True)
app.add_typer(odds_app, name="odds")
lake_app = typer.Typer(help="The R2 lake: raw responses and parquet tables.", no_args_is_help=True)
app.add_typer(lake_app, name="lake")
audit_app = typer.Typer(
    help="Data audits, reviewed by hand before a model depends on the data.", no_args_is_help=True
)
app.add_typer(audit_app, name="audit")

DEFAULT_BACKTEST_SEASONS = ",".join(str(season) for season in DEVELOPMENT_SEASONS)
DEFAULT_BACKTEST_OUT = Path("reports/backtest")


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
def rate() -> None:
    """Refresh team, goalie and player ratings."""
    _not_implemented("rate", "phase 2")


@app.command()
def predict() -> None:
    """Predict today's games against the market."""
    _not_implemented("predict", "phase 5")


@app.command()
def backtest(
    seasons: Annotated[
        str, typer.Option(help="Comma-separated seasons as 20182019.")
    ] = DEFAULT_BACKTEST_SEASONS,
    out: Annotated[Path, typer.Option(help="Report directory.")] = DEFAULT_BACKTEST_OUT,
) -> None:
    """Run the walk-forward backtest for B0 to B3 and the blend."""
    _not_implemented("backtest", "phase 1")


@app.command()
def bets() -> None:
    """Show the paper bet ledger and CLV."""
    _not_implemented("bets", "phase 5")


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
) -> None:
    """Rebuild the lake's odds_snapshots from the stored raw snapshots, matching each event to its
    NHL game. Never calls the Odds API. Without a window, every stored snapshot is replayed."""
    from datetime import UTC, timedelta

    from nhl_edge.ingest.odds_lake import SCHEDULE_DAYS, SCHEDULE_PREFIX, replay_odds
    from nhl_edge.lake.raw import RawStore
    from nhl_edge.lake.tables import Lake
    from nhl_edge.settings import load_env

    if start is not None and recent is not None:
        raise typer.BadParameter("pass at most one of --start and --recent")
    if end is not None and start is None:
        raise typer.BadParameter("--end needs --start")
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
    report = replay_odds(store, Lake.from_env(mirror=r2), dates)
    matched = ", ".join(f"{kind} {n}" for kind, n in sorted(report.matched.items())) or "none"
    window = f"{report.dates[0]}..{report.dates[-1]}" if report.dates else "no snapshots"
    typer.echo(
        f"odds replay {window}: {report.snapshots} snapshots, {report.quotes:,} quotes, "
        f"{report.events} events; matched: {matched}; unmatched {len(report.unmatched)}"
    )
    for event_id, home, away, commence in report.unmatched:
        typer.echo(f"  unmatched {event_id}: {away} at {home}, {commence:%Y-%m-%d %H:%M} UTC")
    for raw_key in report.incomplete:
        typer.echo(f"warning: {raw_key} has no sidecar (an interrupted write), skipped")


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


@app.command()
def status(
    brief: Annotated[bool, typer.Option(help="One line, for the SessionStart hook.")] = False,
) -> None:
    """Show data and model state. Open tasks live in GitHub issues."""
    typer.echo(f"nhl-edge {__version__} · no data ingested, no models fitted")
    if brief:
        return
    typer.echo("\nSeason roles (docs/plan.md section 5):")
    for season, role in SEASON_ROLES.items():
        typer.echo(f"  {season}  {role}")
    typer.echo("  20262027+ live")
