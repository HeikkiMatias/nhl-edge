"""The corrections replay (#173, ADR 0004): post-game corrections measured at the level of model
inputs and predictions. It is a retrospective sensitivity check, and never enters the ledger, the
live counts or any fit.

#30 counted how often a game's week-later copy differs from the first copy its tables read. Here
the live season is rebuilt twice, each in its own scratch copy of the lake:

- **Base:** the lake as it is, every game's feed rows from its first copies.
- **Corrected:** the same lake, with each rechecked game's rows in the seven feed tables replaced
  by rows parsed from its recheck through the ingest's own parser (corrected_feeds, swap_in). A
  rechecked shift chart that came back empty leaves the first chart in place (#237). A game whose
  first copies don't parse to the lake's rows is left out, so the swap changes nothing but the
  corrections.

Both run the live build's ten steps over the season (STEPS, run_steps), so every later game's
inputs come from the same code on the two versions of its history. Then:

- **Inputs:** each feature table's rows from the first corrected game's date on, compared row by
  row (table_changes): the corrected games' own scored shots and stints, and every later game's
  pre-game rows.
- **Predictions:** each complete run bundle's day decided again (bundle.replay) with the rows its
  models read rebuilt from each version (model_tables, with_rows): B2, B3, u and the blend's
  probability, and, as a diagnostic only, the bet, its side and its stake at the day's own prices.
  The fits, the live blend, the quotes, the bankroll and a v2 day's starters are the day's own in
  both, so only the rows differ. The base's replay is checked against the day's ledger.

The lake and the raw cache are paths relative to the working directory, so the steps run with a
scratch directory as theirs, and nothing touches the repository's lake, its reports or R2.
swap_in and run_steps refuse a lake mirrored to R2 or the repository's own.
"""

import subprocess
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from pathlib import Path
from typing import Any, Protocol

import polars as pl

from nhl_edge.backtest.metrics import week_of
from nhl_edge.game import b2, b3, uncertainty
from nhl_edge.ingest.corrections import ADDED, REMOVED, parse_copies
from nhl_edge.ingest.toi_reports import CachedReports
from nhl_edge.lake.raw import RawStore
from nhl_edge.lake.tables import FEED_TABLES, LAKE_DIR, TABLES, Lake
from nhl_edge.lineup import confirmed
from nhl_edge.live import blend_fit
from nhl_edge.live import bundle as lb
from nhl_edge.live import predict as lp
from nhl_edge.live.features import TARGET_TABLES

# The live build's steps in its order (nhl live features), each run over the season.
STEPS = (
    "xg",
    "stints",
    "team-strength",
    "goalie-start",
    "lineups",
    "goalie-effect",
    "schedule-terms",
    "rapm",
    "power-plays",
    "finishing",
)
# The tables they write, compared between the versions.
FEATURE_TABLES = ("shot_xg", "stints", *TARGET_TABLES)
# Columns that name the build that wrote a row, not what it says.
BUILD_COLUMNS = frozenset({"artifact_version", "xg_version"})
# Numbers that name a player: a change counts, but has no size.
IDENTIFIERS = frozenset({"home_goalie", "away_goalie"})
# The same code on the same rows, up to the order of float sums.
TOLERANCE = lb.TOLERANCE
CHANGED = "any value"
CHANGE_SCHEMA = {
    "table": pl.String,
    "column": pl.String,
    "rows": pl.Int64,
    "games": pl.Int64,
    "median_abs": pl.Float64,
    "max_abs": pl.Float64,
}
# Read whole: a v2 starter outside a game's candidates takes his latest goalie-effect and
# multiplier rows before the cutoff, which can be an earlier season's (confirmed.adjusted); u
# reads every season's league lines. The other tables are read for the season, as bundle.inputs
# keeps only the slate's rows.
WHOLE = ("goalie_effects", "goal_multipliers", "player_league_seasons")
# Each replayed game's columns compared.
PROBABILITIES = ("p_b2", "p_b3", "u", "p_blend")
BETS = ("bet", "side", "stake")


# A row of table_changes: table, column, rows, games, median and largest absolute change.
Change = tuple[str, str, int, int | None, float | None, float | None]


class Reader(Protocol):
    """A lake as model_tables reads it (lake/tables.Lake)."""

    def read(self, table: str, seasons: Collection[int] | None = None) -> pl.DataFrame: ...


ModelTables = tuple[b2.Tables, b3.Tables, uncertainty.Tables]


@dataclass(frozen=True)
class Corrected:
    """The rechecked games swapped in, with the seasons and dates they were played on, and their
    feed tables' rows parsed from the recheck (rows, by table); the games left out because their
    first copies don't parse to the lake's rows (unmatched); and those whose rechecked chart came
    back empty, so their first chart stands (charts_kept)."""

    games: list[int]
    seasons: list[int]
    dates: list[date]
    rows: dict[str, pl.DataFrame]
    unmatched: list[int] = field(default_factory=list)
    charts_kept: list[int] = field(default_factory=list)


def _schema(table: str) -> dict[str, pl.DataType]:
    return dict(TABLES[table].empty().schema)


def _same(parsed: pl.DataFrame, stored: pl.DataFrame, table: str) -> bool:
    key = list(TABLES[table].key)
    columns = _schema(table)
    ours = parsed.select(list(columns)).cast(columns).sort(key)  # type: ignore[arg-type]
    return ours.equals(stored.select(list(columns)).sort(key))


def corrected_feeds(
    store: RawStore,
    games: pl.DataFrame,
    lake_rows: Mapping[str, pl.DataFrame],
    toi_reports: CachedReports | None = None,
) -> Corrected:
    """Each game of games (rows of the lake's games) with both copies of its feeds: its feed
    tables' rows parsed from the recheck (parse_copies), kept only when the rows parsed from its
    first copies are the lake's own (lake_rows, by table), every column."""
    kept: list[dict[str, Any]] = []
    unmatched, charts_kept = [], []
    frames: dict[str, list[pl.DataFrame]] = {table: [] for table in FEED_TABLES}
    for game in games.sort("game_date", "game_id").iter_rows(named=True):
        copies = parse_copies(store, game, toi_reports)
        if copies is None:
            continue
        game_id = game["game_id"]
        stored = {t: lake_rows[t].filter(pl.col("game_id") == game_id) for t in FEED_TABLES}
        if not all(_same(copies.first[t], stored[t], t) for t in FEED_TABLES):
            unmatched.append(game_id)
            continue
        kept.append(game)
        if not copies.chart_rechecked:
            charts_kept.append(game_id)
        for table in FEED_TABLES:
            columns = _schema(table)
            frames[table].append(copies.later[table].select(list(columns)).cast(columns))  # type: ignore[arg-type]
    rows = {t: pl.concat([TABLES[t].empty(), *frames[t]]) for t in FEED_TABLES}
    return Corrected(
        games=[game["game_id"] for game in kept],
        seasons=sorted({game["season"] for game in kept}),
        dates=sorted({game["game_date"] for game in kept}),
        rows=rows,
        unmatched=unmatched,
        charts_kept=charts_kept,
    )


def scratch(lake_dir: Path, protected: Path) -> None:
    """Refuse the protected lake (the repository's own), or a folder inside it, as a scratch
    lake."""
    here, kept = lake_dir.resolve(), protected.resolve()
    if here == kept or kept in here.parents:
        raise ValueError(f"{lake_dir} is the repository's lake: the replay writes only a copy")


def swap_in(lake: Lake, corrected: Corrected, protected: Path) -> dict[str, int]:
    """Make each corrected game's feed rows its recheck's, in a scratch lake, date by date, and
    return the rows written per table. Refused for a lake mirrored to R2 or the protected one, the
    repository's own: the corrected version never reaches the record."""
    if lake.objects is not None:
        raise ValueError("the corrections replay never writes a lake mirrored to R2")
    scratch(lake.base_dir, protected)
    if not corrected.games:
        return {}
    ids = pl.Series(corrected.games, dtype=pl.Int64).implode()
    dates = pl.Series(corrected.dates, dtype=pl.Date).implode()
    written = {}
    for table in FEED_TABLES:
        held = lake.read(table, seasons=corrected.seasons).filter(pl.col("game_date").is_in(dates))
        frame = pl.concat([held.filter(~pl.col("game_id").is_in(ids)), corrected.rows[table]])
        lake.replace_dates(table, frame, corrected.dates)
        written[table] = corrected.rows[table].height
    return written


def run_steps(work: Path, season: int, protected: Path, nhl: Sequence[str], log: Path) -> None:
    """Run the live build's steps over the season with work as their working directory, so they
    read and write work's lake (the lake's path is relative), their output appended to log. nhl
    is the command that runs the CLI. Refused for the repository's own lake, or a work directory
    without a lake to rebuild."""
    lake_dir = work / LAKE_DIR
    scratch(lake_dir, protected)
    if not lake_dir.is_dir():
        raise ValueError(f"{work} has no lake to rebuild ({LAKE_DIR})")
    with log.open("a") as out:
        for step in STEPS:
            out.write(f"$ nhl {step} --seasons {season}\n")
            out.flush()
            done = subprocess.run(
                [*nhl, step, "--seasons", str(season)],
                cwd=work,
                stdout=out,
                stderr=subprocess.STDOUT,
                check=False,
            )
            if done.returncode:
                raise RuntimeError(f"nhl {step} failed in {work} (exit {done.returncode}): {log}")


def table_changes(base: pl.DataFrame, corrected: pl.DataFrame, table: str) -> pl.DataFrame:
    """Per column of one feature table, its rows whose value differs between the base and the
    corrected version (a number beyond TOLERANCE, a null or NaN on one side only), with the games
    they belong to and a number's median and largest absolute change; the rows with any differing
    value (CHANGED); and the rows only one version has. Columns naming the build are not compared,
    and only counts above zero are listed."""
    key = list(TABLES[table].key)
    fields = [c for c in base.columns if c not in key and c not in BUILD_COLUMNS]
    a = base.select(*key, *fields).with_columns(_base=pl.lit(True))
    b = corrected.select(*key, *fields).with_columns(_corrected=pl.lit(True))
    joined = a.join(b, on=key, how="full", suffix="_corrected", coalesce=True, nulls_equal=True)
    both = joined.filter(pl.col("_base").is_not_null() & pl.col("_corrected").is_not_null())
    games = "game_id" in key

    def counted(name: str, rows: pl.DataFrame, gap: pl.Expr | None = None) -> Change:
        # A change from or to null has no size: it counts, but not in the median or the largest.
        sizes = rows.select(gap).to_series().drop_nulls() if gap is not None else None
        sized = sizes is not None and not sizes.is_empty()
        return (
            table,
            name,
            rows.height,
            rows["game_id"].n_unique() if games else None,
            float(sizes.median()) if sized else None,  # type: ignore[arg-type, union-attr]
            float(sizes.max()) if sized else None,  # type: ignore[arg-type, union-attr]
        )

    found = [
        counted(ADDED, joined.filter(pl.col("_base").is_null())),
        counted(REMOVED, joined.filter(pl.col("_corrected").is_null())),
    ]
    differs = []
    for name in fields:
        x, y = pl.col(name), pl.col(f"{name}_corrected")
        dtype = base.schema[name]
        if dtype.is_numeric() and name not in IDENTIFIERS:
            gap = (y.cast(pl.Float64) - x.cast(pl.Float64)).abs()
            nan = (x.is_nan() != y.is_nan()).fill_null(False) if dtype.is_float() else pl.lit(False)
            differ = (x.is_null() != y.is_null()) | nan | (gap > TOLERANCE).fill_null(False)
            found.append(counted(name, both.filter(differ), gap))
        else:
            differ = x.ne_missing(y)
            found.append(counted(name, both.filter(differ)))
        differs.append(differ)
    if differs:
        found.insert(2, counted(CHANGED, both.filter(pl.any_horizontal(differs))))
    rows = [row for row in found if row[2]]
    return pl.DataFrame(rows, schema=CHANGE_SCHEMA, orient="row")


def model_tables(lake: Reader, season: int) -> ModelTables:
    """The tables nhl predict reads its models' rows from (bundle.inputs), the season's but for
    WHOLE: B3 shares B2's schedule terms, goalie starts and boxscores."""

    def read(name: str) -> pl.DataFrame:
        return lake.read(name) if name in WHOLE else lake.read(name, seasons=[season])

    games = read("games")
    tables = b2.Tables(
        games,
        read("team_strength"),
        read("schedule_terms"),
        read("goalie_starts"),
        read("goalie_effects"),
        read("actual_lineups"),
    )
    b3_tables = b3.Tables(
        games,
        tables.schedule_terms,
        tables.goalie_starts,
        tables.actual_lineups,
        read("lineups"),
        read("lineup_replacements"),
        read("player_ratings"),
        read("rapm_terms"),
        read("expected_power_plays"),
        read("goal_multipliers"),
    )
    u_tables = uncertainty.Tables(
        games,
        tables.goalie_starts,
        b3_tables.lineups,
        b3_tables.lineup_replacements,
        tables.actual_lineups,
        read("player_league_seasons"),
    )
    return tables, b3_tables, u_tables


def with_rows(bundle: lb.Bundle, tables: ModelTables) -> lb.Bundle:
    """The bundle with the rows its models read taken from tables (model_tables) instead, as nhl
    predict would have saved them: a v2 day with its saved starters in place at its input cutoff
    (confirmed.adjusted), as live/goalies.with_starters put them. The slate, the quotes, the build
    record, the calls and the fits stay the day's own. A day whose models were never read is
    returned as it is."""
    saved = bundle.inputs
    if bundle.b2 is None or bundle.b3 is None:
        return bundle
    b2_tables, b3_tables, u_tables = tables
    if "goalie_starters" in saved:
        cutoff = datetime.fromisoformat(bundle.manifest["input_cutoff"])
        starts, effects, multipliers = confirmed.adjusted(
            b2_tables.goalie_starts,
            b2_tables.goalie_effects,
            b3_tables.goal_multipliers,
            saved["goalie_starters"],
            cutoff,
        )
        b2_tables = replace(b2_tables, goalie_starts=starts, goalie_effects=effects)
        b3_tables = replace(b3_tables, goalie_starts=starts, goal_multipliers=multipliers)
        u_tables = replace(u_tables, goalie_starts=starts)
    rows = lb.inputs(
        saved["slate"], saved["feature_builds"], saved["quotes"], (b2_tables, b3_tables, u_tables)
    )
    return replace(bundle, inputs={**saved, **rows})


def decision_changes(base: pl.DataFrame, corrected: pl.DataFrame) -> pl.DataFrame:
    """Per game of a day's two replayed ledgers (bundle.replay), its status, probabilities, bet,
    side and stake in the base version and, under <column>_corrected, in the corrected one."""
    keep = ["game_date", "game_id", "status", *PROBABILITIES, *BETS]
    return base.select(keep).join(
        corrected.select(keep),
        on=["game_date", "game_id"],
        how="full",
        coalesce=True,
        suffix="_corrected",
    )


@dataclass(frozen=True)
class Decisions:
    """The decision days replayed: each day's games side by side (decision_changes); the days
    whose base replay doesn't reproduce their ledger, with what differs; and those whose own
    bundle doesn't (unreplayable), which would leave an official decision unexplained."""

    games: pl.DataFrame
    not_reproduced: dict[date, list[str]]
    unreplayable: dict[date, list[str]] = field(default_factory=dict)
    # Slate games not played when the lakes were rebuilt: their rows are the morning build's in
    # both versions, so they aren't compared.
    unplayed: list[int] = field(default_factory=list)


def replay_days(
    bundles: Mapping[date, tuple[lb.Bundle, pl.DataFrame]],
    live: Mapping[date, blend_fit.LiveFit],
    base: ModelTables,
    corrected: ModelTables,
    played: Collection[int],
) -> Decisions:
    """Each day's bundle (with its ledger) decided again with the rows from both versions and the
    live fit it names (live, by day), and from its own rows, which a correction arriving later
    never changes. Only the games of played (the rebuilt lakes' games) are compared: the steps
    rate every played game again, and leave an unplayed one's rows as the morning build wrote
    them, in both versions."""
    frames, not_reproduced, unreplayable = [], {}, {}
    for day, (bundle, ledger) in sorted(bundles.items()):
        fit = live[day]
        own = lb.differences(lb.replay(bundle, fit), ledger)
        if own:
            unreplayable[day] = own
        replayed = lb.replay(with_rows(bundle, base), fit)
        problems = lb.differences(replayed, ledger)
        if problems:
            not_reproduced[day] = problems
        again = lb.replay(with_rows(bundle, corrected), fit)
        frames.append(decision_changes(replayed, again))
    every = pl.concat(frames) if frames else pl.DataFrame(schema={"game_id": pl.Int64})
    rated = pl.col("game_id").is_in(pl.Series(list(played), dtype=pl.Int64).implode())
    unplayed = every.filter(~rated)["game_id"].to_list()
    return Decisions(every.filter(rated), not_reproduced, unreplayable, unplayed)


def predicted(games: pl.DataFrame) -> pl.DataFrame:
    """The games predicted in both versions."""
    return games.filter(
        pl.col("status") == lp.PREDICTED, pl.col("status_corrected") == lp.PREDICTED
    )


def _pp(value: float | None) -> str:
    return "—" if value is None else f"{100 * value:.3f}"


def _plain(value: float | None) -> str:
    return "—" if value is None else f"{value:.4g}"


def spread(games: pl.DataFrame, column: str) -> tuple[int, int, float, float, float]:
    """A replayed column's games in both versions, those changed beyond TOLERANCE, and the
    absolute change's median, 90th percentile and largest."""
    gap = (pl.col(f"{column}_corrected") - pl.col(column)).abs()
    both = games.filter(pl.col(column).is_not_null(), pl.col(f"{column}_corrected").is_not_null())
    gaps = both.select(gap).to_series()
    if gaps.is_empty():
        return 0, 0, 0.0, 0.0, 0.0
    return (
        both.height,
        int((gaps > TOLERANCE).sum()),
        float(gaps.median()),  # type: ignore[arg-type]
        float(gaps.quantile(0.9, "nearest")),  # type: ignore[arg-type]
        float(gaps.max()),  # type: ignore[arg-type]
    )


def markdown(
    version: str,
    corrected: Corrected,
    feed_rows: Mapping[str, tuple[int, int]],
    changes: pl.DataFrame,
    compared: Mapping[str, tuple[int, int | None]],
    decisions: Decisions | None,
) -> str:
    """The report. feed_rows gives each feed table's rows of the corrected games in the base and
    the corrected version; changes every feature table's table_changes; compared each feature
    table's rows and games compared."""
    first, last = corrected.dates[0], corrected.dates[-1]
    lines = [
        "# Corrections replay (#173)",
        "",
        f"`{version}`. A retrospective sensitivity check of ADR 0004: it never enters the ledger, "
        "the live counts or any fit, and no result in it is read to prefer either version.",
        "",
        "## The corrected version",
        "",
        f"- **Games:** {len(corrected.games)} games played from {first} to {last} have both "
        "copies of their feeds. Their rows in the seven feed tables are replaced by rows parsed "
        "from the week-later copies, through the ingest's own parser.",
        f"- **Shift charts:** {len(corrected.charts_kept)} of them keep their first chart, "
        "because the rechecked chart came back empty (#237).",
    ]
    if corrected.unmatched:
        listed = ", ".join(map(str, corrected.unmatched[:5]))
        lines.append(
            f"- **Left out:** {len(corrected.unmatched)} games whose first copies don't parse to "
            f"the lake's rows, e.g. {listed}."
        )
    lines += ["", "| Feed table | Base rows | Corrected rows |", "| --- | ---: | ---: |"]
    lines += [f"| {t} | {a:,} | {b:,} |" for t, (a, b) in feed_rows.items()]
    lines += [
        "",
        "## Inputs",
        "",
        f"Both versions ran the live build's ten steps over the season. Every feature table's rows "
        f"from {first} on are compared, row by row. shot_xg and stints are the games' own scored "
        "shots and stints. The other tables are each game's pre-game rows, which a correction to "
        "an earlier game can reach.",
        "",
        "| Table | Rows | Games | Rows changed | Games changed | Rows added | Rows removed |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]

    def count(table: str, column: str, what: str = "rows") -> int:
        found = changes.filter(pl.col("table") == table, pl.col("column") == column)[what]
        return int(found.sum()) if found.len() else 0

    for table, (rows, games) in compared.items():
        # A table without game_id (rapm_terms, by date) counts rows only.
        changed = "—" if games is None else f"{count(table, CHANGED, 'games'):,}"
        lines.append(
            f"| {table} | {rows:,} | {'—' if games is None else f'{games:,}'} | "
            f"{count(table, CHANGED):,} | {changed} | {count(table, ADDED):,} | "
            f"{count(table, REMOVED):,} |"
        )
    columns = changes.filter(~pl.col("column").is_in([CHANGED, ADDED, REMOVED]))
    if columns.height:
        lines += [
            "",
            "| Table | Column | Rows changed | Median absolute change | Largest |",
            "| --- | --- | ---: | ---: | ---: |",
        ]
        lines += [
            f"| {table} | {column} | {rows:,} | {_plain(median)} | {_plain(largest)} |"
            for table, column, rows, _, median, largest in columns.rows()
        ]
    lines += ["", "## Predictions on the decision days", ""]
    if decisions is None:
        lines.append("Not replayed: the run bundles are read from R2 (--r2).")
        return "\n".join(lines) + "\n"
    games = decisions.games
    if games.is_empty():
        lines.append("No decision day's games had been played when the lakes were rebuilt.")
        return "\n".join(lines) + "\n"
    days = games["game_date"].unique().sort().to_list()
    both = predicted(games)
    lines += [
        f"Each complete run bundle's day, {len(days)} days from {days[0]} to {days[-1]}, decided "
        "again with the rows its models read rebuilt from each version. The fits, the live "
        "blend, the quotes, the bankroll and a v2 day's starters are the day's own in both.",
        "",
    ]
    weeks = games.select(week_of(pl.col("game_date")).n_unique()).item()
    if decisions.unreplayable:
        lines.append(
            f"- **Each day's own bundle:** it replays its ledger on "
            f"{len(days) - len(decisions.unreplayable)} of {len(days)} days. Elsewhere:"
        )
        for day, problems in sorted(decisions.unreplayable.items()):
            lines.append(f"  - {day}: {'; '.join(problems)}")
    else:
        lines.append(
            "- **Each day's own bundle:** it still replays the day's ledger, every column, so the "
            "official decisions stay reproducible from their own inputs, whatever the lake holds "
            "now."
        )
    if decisions.not_reproduced:
        lines.append(
            f"- **Base against the ledger:** the base rows reproduce the day's ledger on "
            f"{len(days) - len(decisions.not_reproduced)} of {len(days)} days. Elsewhere:"
        )
        for day, problems in sorted(decisions.not_reproduced.items()):
            lines.append(f"  - {day}: {'; '.join(problems)}")
    else:
        lines.append(
            f"- **Base against the ledger:** the base rows reproduce every day's ledger, every "
            f"column, floats within {TOLERANCE:g}."
        )
    if decisions.unplayed:
        lines.append(
            f"- **Not compared:** {len(decisions.unplayed)} slate games not yet played when the "
            "lakes were rebuilt, so their rows are the morning build's in both versions."
        )
    lines += [
        f"- **Games:** {both.height} predicted in both versions.",
        "",
        "| Measure | Games | Changed | Median absolute change | 90th percentile | Largest |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for column in PROBABILITIES:
        n, moved, median, p90, largest = spread(both, column)
        show = _plain if column == "u" else _pp
        unit = "" if column == "u" else " (points)"
        lines.append(
            f"| {column}{unit} | {n} | {moved} | {show(median)} | {show(p90)} | {show(largest)} |"
        )
    bet, bet_corrected = pl.col("bet").fill_null(False), pl.col("bet_corrected").fill_null(False)
    bets = both.filter(bet | bet_corrected)
    flipped = bets.filter(bet != bet_corrected)
    sided = bets.filter(bet & bet_corrected, pl.col("side") != pl.col("side_corrected"))
    _, staked, _, _, largest = spread(bets, "stake")
    lines += [
        "",
        "**Bets, a diagnostic only:** at the day's own prices, "
        f"{int(both['bet'].fill_null(False).sum())} bets in the base and "
        f"{int(both['bet_corrected'].fill_null(False).sum())} in the corrected version. "
        f"{flipped.height} games are bet in one version only, {sided.height} bet in both change "
        f"side, and "
        f"{staked} stakes change, the largest by {largest:.3f} units of the 100-unit bankroll.",
        "",
        "## Limits",
        "",
        f"- **Size:** {len(corrected.games)} corrected games, and {both.height} predictions on "
        f"{len(days)} decision days in {weeks} calendar weeks: too few weekly blocks for a block "
        "bootstrap interval (hard rule 7). So the figures are counts and ranges, no statistical "
        "verdict is made, and no pass or fail is read from them.",
        "- **History:** the rechecks are prospective, so this can't show how the 2010-26 "
        "backfill's late copies differed from first ones.",
    ]
    return "\n".join(lines) + "\n"
