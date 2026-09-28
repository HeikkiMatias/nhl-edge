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
        bool, typer.Option(help="Exit quietly when the NHL schedule has no games.")
    ] = False,
) -> None:
    """Pull one odds snapshot into odds_snapshots."""
    _not_implemented("odds snapshot", "phase 1")


@odds_app.command()
def backfill() -> None:
    """Backfill historical odds from the paid Odds API endpoint."""
    _not_implemented("odds backfill", "deferred to v2")


@app.command()
def status(
    brief: Annotated[bool, typer.Option(help="One line, for the SessionStart hook.")] = False,
) -> None:
    """Show project phase and data state."""
    typer.echo(f"nhl-edge {__version__} · phase 0 (setup) · no data ingested, no models fitted")
    if brief:
        return
    typer.echo("\nSeason roles (docs/plan.md section 5):")
    for season, role in SEASON_ROLES.items():
        typer.echo(f"  {season}  {role}")
    typer.echo("  20262027+ live")
