"""The `nhl` command line: one interface for you, Claude Code and GitHub Actions."""

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, NoReturn

import typer

from nhl_edge import __version__
from nhl_edge.backtest.seasons import DEVELOPMENT_SEASONS, SEASON_ROLES

if TYPE_CHECKING:
    from nhl_edge.lake.status import TableState

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

DEFAULT_BACKTEST_SEASONS = ",".join(str(season) for season in DEVELOPMENT_SEASONS)
DEFAULT_BACKTEST_OUT = Path("reports/backtest")
DEFAULT_AUDIT_OUT = Path("reports/audit")
DEFAULT_XG_OUT = Path("reports/xg")
DEFAULT_TUNING_OUT = Path("reports/tuning")


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
    """Run the walk-forward backtest. Phase 1 has the market baselines on the SBR archive, for E1
    (the close) and E2 (the opener): B0 under each de-vig method, and B1 fitted per season on the
    earlier seasons' prices. E2 refuses implausible openers (ADR 0007), and E2 on every opener is
    reported beside it, as is the diagnostic of SBR's change of closing book (#65)."""
    from datetime import UTC

    import polars as pl

    from nhl_edge.backtest import book_era, reports, sensitivity, walk_forward
    from nhl_edge.backtest.seasons import OPEN_ROLES, OPEN_SEASONS, season_role
    from nhl_edge.ingest.games import EXPECTED_GAMES
    from nhl_edge.ingest.nhl_ingest import parse_seasons
    from nhl_edge.ingest.sbr import SEASON_PAGES
    from nhl_edge.lake.tables import Lake

    try:
        wanted = sorted(set(parse_seasons(seasons)))
        held_out = [season for season in wanted if season_role(season) not in OPEN_ROLES]
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--seasons") from None
    if held_out:
        raise typer.BadParameter(
            f"{held_out} are held out: a backtest runs on training and development seasons only "
            "until their phase (docs/plan.md section 5, #10)",
            param_hint="--seasons",
        )
    # B1 is fitted on every open SBR season before the test season, so those need prices too.
    history = [s for s in SEASON_PAGES if s in OPEN_SEASONS]
    first = [season for season in wanted if season <= min(history)]
    if first:
        raise typer.BadParameter(
            f"{first} have no earlier SBR season to fit B1 on", param_hint="--seasons"
        )
    lake = Lake()
    needed = set(wanted) | {s for s in history if s < max(wanted)}
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
    predictions, coverage, fits = walk_forward.run(sbr_odds, games, wanted)
    now = datetime.now(UTC)
    run_version = reports.version("backtest", now)
    report = reports.summary(predictions, coverage, fits, wanted, run_version, now)
    report["sensitivity"] = sensitivity.every_opener(sbr_odds, games, wanted)
    report["diagnostics"] = {"book_era": book_era.diagnostic(sbr_odds, games)}
    path = reports.write(report, out)
    typer.echo(f"{path}: {report['version']}")
    parts = [(name, body) for name, body in report["experiments"].items()]
    parts += [(f"E2 {name}", body["E2"]) for name, body in report["sensitivity"].items()]
    for experiment, body in parts:
        for model, results in body["models"].items():
            for method, estimates in results["log_loss"].items():
                pooled = estimates["pooled"]
                typer.echo(
                    f"  {experiment} {model} {method}: log loss {pooled['mean']:.4f} "
                    f"[{pooled['low']:.4f}, {pooled['high']:.4f}] over {pooled['games']:,} games"
                )
    eras = report["diagnostics"]["book_era"]
    for name, cost in eras["b0_e2_minus_e1"]["eras"].items():
        gain = eras["b0_minus_b1"]["E1"]["eras"][name]
        typer.echo(
            f"  {name}: B0 E2 minus E1 {cost['mean']:+.4f} [{cost['low']:+.4f}, "
            f"{cost['high']:+.4f}]; E1 B0 minus B1 {gain['mean']:+.4f} [{gain['low']:+.4f}, "
            f"{gain['high']:+.4f}] over {gain['games']:,} games"
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
) -> None:
    """Rate every game's rolling team strength ΔS with the frozen settings (#74, ADR 0011) into the
    lake's team_strength. With --tune, score the 16 candidate settings on the training seasons
    instead and write the log to <out>/team-strength-<version>.md."""
    from datetime import UTC

    import polars as pl

    from nhl_edge.backtest import reports, tuning
    from nhl_edge.features import team_strength as ts
    from nhl_edge.ingest.nhl_ingest import parse_seasons
    from nhl_edge.lake.tables import Lake
    from nhl_edge.settings import load_env

    load_env()
    lake = Lake.from_env(mirror=r2)
    games = lake.read("games")
    known = sorted(games["season"].unique().to_list())
    try:
        wanted = parse_seasons(seasons) if seasons else [s for s in known if s >= ts.FIRST_SEASON]
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--seasons") from None
    last = max(ts.TUNING_SEASONS) if tune else max(wanted)
    shot_xg, strength_time = lake.read("shot_xg"), lake.read("strength_time")
    problems = ts.input_problems(games, shot_xg, strength_time, last)
    if problems:
        for problem in problems:
            typer.echo(problem, err=True)
        typer.echo("run nhl ingest --replay and nhl xg for those seasons", err=True)
        raise typer.Exit(code=1)
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
    try:
        rated = games.filter(pl.col("season").is_in(wanted))
        if rated.is_empty():
            raise ValueError(f"no games of {wanted} in the lake")
        frame = ts.rows(rated, history, ts.TUNED, version)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--seasons") from None
    lake.replace_dates("team_strength", frame, rated["game_date"].unique().to_list())
    typer.echo(f"team_strength: {frame.height:,} games rated with {ts.TUNED.label} ({version})")


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
    if report.in_play:
        typer.echo(f"  left out {report.in_play:,} quotes on games already under way")
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
    mirror_raw: Annotated[
        bool, typer.Option(help="Mirror the raw responses to R2 (needs the R2_* variables).")
    ] = False,
) -> None:
    """Store the pre-game boxscore, landing and right-rail of every game starting soon, and Daily
    Faceoff's starting goalies for their dates."""
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
    failed_pages = []
    if daily_faceoff and report.dates:
        dfo = dailyfaceoff.DailyFaceoff(store)
        failed_pages = dailyfaceoff.run_poll(dfo=dfo, days=report.dates, echo=typer.echo)
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
    """Rebuild the lake's pregame_goalies and dailyfaceoff_goalies from the stored pre-game
    boxscores and Daily Faceoff pages. Never calls either source. Without --recent, every stored
    date is replayed."""
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
        roots = [BOXSCORE_PREFIX, dailyfaceoff.PREFIX]
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
        "sbr_odds": "nhl odds sbr --replay --r2",
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
    # The replayed tables are rebuilt by their own replay, every other table by the NHL ingest.
    replayed_here = sorted(
        {key.split("/")[0] for key in missing_here if key.split("/")[0] in REPLAYED}
    )
    replayed_there = sorted(
        {key.split("/")[0] for key in missing_there if key.split("/")[0] in REPLAYED}
    )
    drifted_here, drifted_there = missing_here, missing_there
    missing_here = [key for key in missing_here if key.split("/")[0] not in REPLAYED]
    missing_there = [key for key in missing_there if key.split("/")[0] not in REPLAYED]
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
    if polls_behind:
        typer.echo("  pre-game goalie polls are behind R2: nhl lake restore-raw copies them")
    if polls_ahead:
        typer.echo("  R2 lacks pre-game goalie polls here: nhl lake sync-raw copies them")
    if differ:
        # A size difference does not tell which copy is current, so no direction is suggested.
        tables = sorted({key.split("/")[0] for key in differ})
        typer.echo(
            f"  {len(differ):,} files differ from R2 in {', '.join(tables)}: check which copy is "
            "current before syncing either way"
        )
    in_step = not (missing_here or missing_there or replayed_here or replayed_there or differ)
    if in_step and not (raw_behind or raw_ahead or polls_behind or polls_ahead):
        typer.echo("  up to date with R2")
