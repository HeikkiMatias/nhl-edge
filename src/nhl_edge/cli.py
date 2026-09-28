"""The `nhl` command line: one interface for you, Claude Code and GitHub Actions."""

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

DEFAULT_BACKTEST_SEASONS = ",".join(str(season) for season in DEVELOPMENT_SEASONS)
DEFAULT_BACKTEST_OUT = Path("reports/backtest")


def _not_implemented(command: str, phase: str) -> NoReturn:
    typer.echo(f"nhl {command}: not implemented yet ({phase}, see docs/plan.md).", err=True)
    raise typer.Exit(code=1)


@app.command()
def ingest() -> None:
    """Ingest NHL API games, shifts and rosters into the lake."""
    _not_implemented("ingest", "phase 1")


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
