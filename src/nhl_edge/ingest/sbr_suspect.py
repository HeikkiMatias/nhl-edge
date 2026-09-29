"""SBR openers that are likely wrong (#56): one row per game with the evidence, for E2 to decide
on. Whether E2 excludes, fixes or keeps these openers is the owner's call; until then it keeps
every game.

A game is listed when its moneyline meets at least one criterion, over the seasons whose prices
phase 1 reads (audit.sbr.price_seasons, so 2022-23 is not inspected):
- big_move: the de-vigged home probability moves more than BIG_MOVE (15 points) from open to
  close, while the 90th percentile move is 4 to 5 points (the #9 audit).
- extreme_open: the opener's home probability is outside EXTREME_OPEN (0.15 to 0.85). No close of
  2010-11 to 2021-22 is outside 0.21 to 0.84. SBR's page prints these openers as stored, such as
  Edmonton -1010 and Minnesota 705 on 2022-02-20, so they are errors in the source, not the
  parser.
- swapped: the opener and the close each have a clear favourite (CLEAR_FAVOURITE, 55%), they
  name different teams, and the opener with its two sides swapped is within SWAP_TOLERANCE (5
  points, about the 90th percentile move) of the close. Near even, a changed favourite is an
  ordinary line move, so it is not evidence of a swap.
- below_100: the two opening prices sum below 100%, which one book's market never does.

Probabilities come from audit.sbr.moneylines, which de-vigs through market/devig.py with the
multiplicative method, as the #9 audit does.

The list reads the close, which is public only at the start (ADR 0006). So it is hindsight, and
each row's observed_utc is the game's start. It is a data-quality annotation for E2's report,
never a model input.

The list is generated, not hand-compiled: `uv run python -m nhl_edge.ingest.sbr_suspect` rebuilds
reference/sbr_suspect_openers.csv from the local lake's sbr_odds.
"""

import polars as pl

from nhl_edge.audit.sbr import BIG_MOVE, CLEAR_FAVOURITE, moneylines, price_seasons
from nhl_edge.lake.schemas import SBR_SUSPECT_FLAGS, SbrSuspectOpeners, dtypes
from nhl_edge.lake.tables import Lake
from nhl_edge.reference import REFERENCE_DIR

SUSPECT_FILE = REFERENCE_DIR / "sbr_suspect_openers.csv"
EXTREME_OPEN = (0.15, 0.85)
SWAP_TOLERANCE = 0.05
# Probabilities, moves and gaps are stored to this many decimals, enough to read and compare.
DECIMALS = 4
CSV_DATETIME = "%Y-%m-%dT%H:%M:%SZ"


def suspect_openers(odds: pl.DataFrame) -> pl.DataFrame:
    """The games of sbr_odds rows whose opening moneyline meets a criterion, as rows of
    SbrSuspectOpeners sorted by game_id."""
    read = price_seasons(sorted(odds["season"].unique().to_list()))
    odds = odds.filter(pl.col("season").is_in(read))
    lines = moneylines(odds)

    def at(quote: str) -> pl.DataFrame:
        return lines.filter(pl.col("quote") == quote).select(
            "game_id",
            pl.col("home_american").alias(f"{quote}_home"),
            pl.col("away_american").alias(f"{quote}_away"),
            pl.col("p_home").alias(f"p_{quote}"),
        )

    games = (
        odds.filter(pl.col("market") == "h2h", pl.col("quote") == "open", pl.col("side") == "home")
        .select("game_id", "season", "game_date", "start_utc", "home", "away", "raw_key")
        .unique("game_id")
    )
    close_line = odds.filter(
        pl.col("market") == "spreads", pl.col("quote") == "close", pl.col("side") == "home"
    ).select("game_id", close_home_line="line")
    p_open, p_close = pl.col("p_open"), pl.col("p_close")
    low, high = EXTREME_OPEN
    clear = CLEAR_FAVOURITE - 0.5
    listed = (
        games.join(at("open"), on="game_id")
        .join(at("close"), on="game_id", how="left")
        .join(close_line, on="game_id", how="left")
        .with_columns(
            move=(p_close - p_open).abs(),
            unswapped_gap=((1 - p_open) - p_close).abs(),
        )
        .with_columns(
            big_move=(pl.col("move") > BIG_MOVE).fill_null(False),
            extreme_open=((p_open < low) | (p_open > high)).fill_null(False),
            swapped=(
                ((p_open - 0.5).abs() >= clear)
                & ((p_close - 0.5).abs() >= clear)
                & ((p_open - 0.5) * (p_close - 0.5) < 0)
                & (pl.col("unswapped_gap") <= SWAP_TOLERANCE)
            ).fill_null(False),
            below_100=p_open.is_null(),
            observed_utc=pl.col("start_utc"),
        )
        .filter(pl.any_horizontal(*SBR_SUSPECT_FLAGS))
        .with_columns(pl.col("p_open", "p_close", "move", "unswapped_gap").round(DECIMALS))
    )
    return listed.select(list(dtypes(SbrSuspectOpeners))).sort("game_id")


def load_suspect_openers() -> pl.DataFrame:
    """The committed list, validated."""
    frame = pl.read_csv(SUSPECT_FILE, schema=dtypes(SbrSuspectOpeners))
    return SbrSuspectOpeners.validate(frame)


def write_suspect_openers(frame: pl.DataFrame) -> None:
    SbrSuspectOpeners.validate(frame).write_csv(SUSPECT_FILE, datetime_format=CSV_DATETIME)


def main() -> None:
    frame = suspect_openers(Lake().read("sbr_odds"))
    write_suspect_openers(frame)
    counts = frame.select(pl.col(SBR_SUSPECT_FLAGS).sum())
    print(f"{frame.height} suspect openers written to {SUSPECT_FILE}")
    print(counts)


if __name__ == "__main__":
    main()
