"""The SBR odds archive (#7): how its games joined the NHL's, the moneyline vig per season, whether
each game's opener and close agree, and whether its closing puck line agrees with its moneyline.

The join is re-run from the stored pages with ingest.sbr's own parse_season and match_season, so
the SBR rows that matched no NHL game are seen too. An unmatched row that the cached schedule
listings name as a playoff game is not a problem: match_season tells a playoff game apart only
after the regular season's last date, and in 2020-21 the playoffs began before it.

The price checks read sbr_odds and de-vig only through market/devig.py, with the multiplicative
method: the default method is chosen after this audit (#10), and a favourite or an open-to-close
move barely depends on it. They leave out 2022-23, the market validation season, which phase 1
does not inspect (the owner's decision on #10, 2026-09-29). Its join is still reported, since
that counts rows and reads no price.
"""

from collections.abc import Iterable, Mapping
from datetime import date
from itertools import pairwise

import numpy as np
import polars as pl

from nhl_edge.audit.games import EXAMPLES
from nhl_edge.backtest.seasons import SeasonRole, season_role
from nhl_edge.ingest.games import EXPECTED_GAMES
from nhl_edge.ingest.nhl_api import PLAYOFFS
from nhl_edge.ingest.sbr import SOURCE, SeasonReport, match_season, parse_season
from nhl_edge.lake.raw import RawStore
from nhl_edge.market.devig import OVERROUND_TOLERANCE, Method, fair_probabilities, overround

# The seasons whose prices the audit may read. A market validation season counts as development
# once inspected (backtest/seasons.py), and later seasons have no SBR prices.
PRICE_ROLES = frozenset({SeasonRole.TRAINING, SeasonRole.TRAINING_FLAGGED, SeasonRole.DEVELOPMENT})
# A move of the de-vigged home probability from open to close larger than this is reviewed by
# hand. Over 2010-11 to 2021-22 the 90th percentile is 4 to 5 points, and the largest moves pair
# a price like -1010 with a close near even.
BIG_MOVE = 0.15
# A closing moneyline favourite at least this likely should be the -1.5 side of the closing puck
# line. Near even the two often disagree, since the puck line's price moves instead of its side.
CLEAR_FAVOURITE = 0.55
# A season's median vig this far from the season before's points to another book or source.
VIG_SHIFT = 0.01

MONEYLINE_SCHEMA = {
    "season": pl.Int32,
    "game_id": pl.Int64,
    "quote": pl.String,
    "home": pl.Float64,
    "away": pl.Float64,
    "home_american": pl.Int32,
    "away_american": pl.Int32,
    "overround": pl.Float64,
    "p_home": pl.Float64,
}


def price_seasons(seasons: Iterable[int]) -> list[int]:
    """The seasons whose SBR prices the audit reads."""
    return [season for season in seasons if season_role(season) in PRICE_ROLES]


def join_reports(
    store: RawStore,
    schedule: pl.DataFrame,
    games: pl.DataFrame,
    seasons: Iterable[int],
    expected_games: Mapping[int, int] = EXPECTED_GAMES,
) -> tuple[list[SeasonReport], list[str]]:
    """Each season's stored SBR page joined to the NHL's games, as `nhl odds sbr` reports it, and
    a problem for each season it cannot join: one with no stored page, or one whose schedule is
    short of the season's games, since match_season takes every SBR row after the schedule's last
    date for a playoff game (as import_seasons, which refuses such a season)."""
    reports, found = [], []
    for season in seasons:
        raw_key = store.latest(f"{SOURCE}/{season}")
        in_season = pl.col("season") == season
        season_schedule = schedule.filter(in_season)
        if raw_key is None:
            found.append(f"{season}: no SBR page in the raw cache, so its join is not checked")
            continue
        if season_schedule.height != expected_games[season]:
            found.append(
                f"{season}: the schedule has {season_schedule.height:,} of "
                f"{expected_games[season]:,} games, so the SBR join is not checked"
            )
            continue
        parsed = parse_season(store.get(raw_key), season)
        _, report = match_season(parsed, season_schedule, games.filter(in_season), raw_key)
        reports.append(report)
    return reports, found


def unpriced(odds: pl.DataFrame, priced: Iterable[int]) -> list[str]:
    """A problem for each season whose prices the audit reads but sbr_odds lacks."""
    have = set(odds["season"].unique().to_list())
    return [f"{season}: no SBR prices in sbr_odds" for season in priced if season not in have]


def unmatched(report: SeasonReport, listed: pl.DataFrame) -> list[tuple[date, str, str]]:
    """The season's SBR games that match no NHL game, less those the listings name as playoffs."""
    playoffs = {
        (game_date, frozenset((home, away)))
        for game_date, home, away in listed.filter(pl.col("game_type") == PLAYOFFS)
        .select("game_date", "home", "away")
        .iter_rows()
    }
    return [(d, a, b) for d, a, b in report.unmatched if (d, frozenset((a, b))) not in playoffs]


def join_report(reports: list[SeasonReport], listed: pl.DataFrame) -> pl.DataFrame:
    rows = []
    for r in reports:
        left = len(unmatched(r, listed))
        rows.append(
            {
                "season": r.season,
                "sbr_games": r.sbr_games,
                "matched": r.matched,
                "playoffs": r.after_regular_season + len(r.unmatched) - left,
                "unmatched": left,
                "nhl_games": r.nhl_games,
                "without_sbr": r.nhl_without_sbr,
                "join_rate": r.join_rate,
                "score_mismatches": len(r.score_mismatches),
                "missing_prices": r.missing_prices,
            }
        )
    return pl.DataFrame(rows)


def moneylines(odds: pl.DataFrame) -> pl.DataFrame:
    """One row per game and quote (open or close): both moneyline prices, their overround, and the
    multiplicative de-vigged home probability, null where the prices sum below 100%, which
    de-vigging refuses."""
    h2h = odds.filter(pl.col("market") == "h2h")

    def side(name: str) -> pl.DataFrame:
        return h2h.filter(pl.col("side") == name).select(
            "season",
            "game_id",
            "quote",
            pl.col("price_decimal").alias(name),
            pl.col("price_american").alias(f"{name}_american"),
        )

    # match_season keeps a price only with the other side's.
    wide = side("home").join(side("away"), on=["season", "game_id", "quote"])
    if wide.is_empty():
        return pl.DataFrame(schema=MONEYLINE_SCHEMA)
    prices = wide.select("home", "away").to_numpy()
    total = overround(prices)
    fair = total >= 1 - OVERROUND_TOLERANCE
    p_home = np.full(len(total), np.nan)
    if fair.any():
        p_home[fair] = fair_probabilities(prices[fair], Method.MULTIPLICATIVE)[:, 0]
    return (
        wide.with_columns(overround=pl.Series(total), p_home=pl.Series(p_home).fill_nan(None))
        .select(list(MONEYLINE_SCHEMA))
        .sort("season", "game_id", "quote")
    )


def below_100(lines: pl.DataFrame) -> pl.DataFrame:
    return lines.filter(pl.col("p_home").is_null())


def vig_report(lines: pl.DataFrame) -> pl.DataFrame:
    """Per season, the moneyline vig (overround minus 1) at the open and at the close: median and
    10th to 90th percentile of the markets de-vigging accepts, and the count of those summing
    below 100%, which it refuses."""
    vig = pl.col("overround").filter(pl.col("p_home").is_not_null()) - 1
    per_quote = lines.group_by("season", "quote").agg(
        pl.col("game_id").n_unique().alias("games"),
        vig.median().alias("median"),
        vig.quantile(0.1).alias("p10"),
        vig.quantile(0.9).alias("p90"),
        pl.col("p_home").is_null().sum().alias("below_100"),
    )
    report = lines.group_by("season").agg(pl.col("game_id").n_unique().alias("games"))
    for quote in ("open", "close"):
        report = report.join(
            per_quote.filter(pl.col("quote") == quote).select(
                "season",
                *(pl.col(name).alias(f"{quote}_{name}") for name in ("median", "p10", "p90")),
                pl.col("below_100").alias(f"{quote}_below_100"),
            ),
            on="season",
            how="left",
        )
    return report.sort("season")


def moves(lines: pl.DataFrame) -> pl.DataFrame:
    """Each game with a de-vigged opener and close: the home probability at each, the move between
    them, and whether both prices stayed the same."""
    fair = lines.filter(pl.col("p_home").is_not_null())

    def at(quote: str) -> pl.DataFrame:
        return fair.filter(pl.col("quote") == quote).select(
            "season",
            "game_id",
            pl.col("p_home", "home_american", "away_american").name.suffix(f"_{quote}"),
        )

    return (
        at("open")
        .join(at("close"), on=["season", "game_id"])
        .select(
            "season",
            "game_id",
            p_open=pl.col("p_home_open"),
            p_close=pl.col("p_home_close"),
            move=(pl.col("p_home_close") - pl.col("p_home_open")).abs(),
            unchanged=(pl.col("home_american_open") == pl.col("home_american_close"))
            & (pl.col("away_american_open") == pl.col("away_american_close")),
        )
        .sort("season", "game_id")
    )


def move_report(moved: pl.DataFrame) -> pl.DataFrame:
    """Per season: the median and 90th percentile move, the moves above BIG_MOVE, the games whose
    favourite changed, and the share whose prices did not move at all."""
    flipped = (pl.col("p_open") - 0.5) * (pl.col("p_close") - 0.5) < 0
    return (
        moved.group_by("season")
        .agg(
            pl.len().alias("games"),
            pl.col("move").median().alias("median"),
            pl.col("move").quantile(0.9).alias("p90"),
            (pl.col("move") > BIG_MOVE).sum().alias("big"),
            flipped.sum().alias("favourite_changed"),
            pl.col("unchanged").mean().alias("unchanged"),
        )
        .sort("season")
    )


def puck_line_conflicts(odds: pl.DataFrame, lines: pl.DataFrame) -> pl.DataFrame:
    """Games whose closing moneyline makes a team at least a CLEAR_FAVOURITE while the closing
    puck line gives that team +1.5, so one of the two is likely on the wrong team."""
    home_line = odds.filter(
        pl.col("market") == "spreads", pl.col("quote") == "close", pl.col("side") == "home"
    ).select("season", "game_id", home_line="line")
    close = lines.filter(pl.col("quote") == "close", pl.col("p_home").is_not_null())
    return (
        close.join(home_line, on=["season", "game_id"])
        .filter(
            ((pl.col("p_home") >= CLEAR_FAVOURITE) & (pl.col("home_line") > 0))
            | ((pl.col("p_home") <= 1 - CLEAR_FAVOURITE) & (pl.col("home_line") < 0))
        )
        .select("season", "game_id", "p_home", "home_line")
        .sort("season", "game_id")
    )


def puck_line_report(odds: pl.DataFrame, conflicts: pl.DataFrame) -> pl.DataFrame:
    with_line = (
        odds.filter(pl.col("market") == "spreads", pl.col("quote") == "close")
        .group_by("season")
        .agg(pl.col("game_id").n_unique().alias("games"))
    )
    found = conflicts.group_by("season").agg(pl.len().alias("conflicts"))
    return (
        with_line.join(found, on="season", how="left")
        .with_columns(pl.col("conflicts").fill_null(0))
        .sort("season")
    )


def _examples(items: list[str]) -> str:
    return ", ".join(items[:EXAMPLES])


def _american(price: int) -> str:
    return f"+{price}" if price > 0 else str(price)


def problems(
    reports: list[SeasonReport],
    listed: pl.DataFrame,
    lines: pl.DataFrame,
    moved: pl.DataFrame,
    conflicts: pl.DataFrame,
    coverage: Iterable[str] = (),
) -> list[str]:
    """One line per season and kind of problem, naming example games, with the coverage problems
    of join_reports and unpriced."""
    found = list(coverage)
    for r in sorted(reports, key=lambda r: r.season):
        if r.nhl_without_sbr:
            found.append(
                f"{r.season}: SBR prices for {r.nhl_games - r.nhl_without_sbr:,} of "
                f"{r.nhl_games:,} games ({r.join_rate:.1%})"
            )
        left = unmatched(r, listed)
        if left:
            examples = _examples([f"{d} {a} and {b}" for d, a, b in left])
            found.append(f"{r.season}: {len(left)} SBR games match no NHL game, e.g. {examples}")
        if r.missing_prices:
            found.append(
                f"{r.season}: {r.missing_prices} SBR prices shown as NL, blank or malformed, "
                "left out with the other side's"
            )
        if r.score_mismatches:
            examples = _examples([f"{g} ({text})" for g, text in r.score_mismatches])
            found.append(
                f"{r.season}: {len(r.score_mismatches)} SBR final scores differ from the NHL's, "
                f"e.g. {examples}"
            )
    for (season,), rows in below_100(lines).group_by("season", maintain_order=True):
        examples = _examples(
            [
                f"{g} {q} (home {_american(h)}, away {_american(a)})"
                for g, q, h, a in rows.select(
                    "game_id", "quote", "home_american", "away_american"
                ).iter_rows()
            ]
        )
        found.append(
            f"{season}: {rows.height} moneylines sum below 100%, which de-vigging refuses, "
            f"e.g. {examples}"
        )
    vig = vig_report(lines)
    for quote, label in (("open", "opening"), ("close", "closing")):
        medians = vig.select("season", f"{quote}_median").drop_nulls().rows()
        for (before, previous), (season, median) in pairwise(medians):
            if abs(median - previous) > VIG_SHIFT:
                found.append(
                    f"{season}: median {label} vig {median:.1%}, against {previous:.1%} in {before}"
                )
    big = moved.filter(pl.col("move") > BIG_MOVE)
    for (season,), rows in big.group_by("season", maintain_order=True):
        examples = _examples(
            [
                f"{g} (home {p_open:.0%} to {p_close:.0%})"
                for g, p_open, p_close in rows.select("game_id", "p_open", "p_close").iter_rows()
            ]
        )
        found.append(
            f"{season}: {rows.height} games whose de-vigged home probability moves more than "
            f"{BIG_MOVE * 100:.0f} points from open to close, e.g. {examples}"
        )
    for (season,), rows in conflicts.group_by("season", maintain_order=True):
        examples = _examples(
            [
                f"{g} (home {p:.0%}, home puck line {line:+.1f})"
                for g, p, line in rows.select("game_id", "p_home", "home_line").iter_rows()
            ]
        )
        found.append(
            f"{season}: {rows.height} games whose closing moneyline favourite "
            f"({CLEAR_FAVOURITE:.0%} or more) is +1.5 on the closing puck line, e.g. {examples}"
        )
    return sorted(found, key=lambda line: line[:8])


def _pct(value: float | None) -> str:
    return "" if value is None else f"{value:.1%}"


def _spread(low: float | None, high: float | None) -> str:
    return "" if low is None or high is None else f"{low:.1%} to {high:.1%}"


def markdown_report(
    joined: pl.DataFrame,
    vig: pl.DataFrame,
    moved: pl.DataFrame,
    puck_lines: pl.DataFrame,
) -> str:
    parts = [
        "**Join to the NHL's regular-season games**, from the stored pages. Playoff rows are "
        "left out of `sbr_odds`; unmatched rows are neither playoff nor regular-season games.",
        "",
        "| Season | SBR games | Matched | Playoffs | Unmatched | NHL games | Without SBR "
        "| Join rate | Score mismatches | Missing prices |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    parts += [
        f"| {r['season']} | {r['sbr_games']:,} | {r['matched']:,} | {r['playoffs']} "
        f"| {r['unmatched']} | {r['nhl_games']:,} | {r['without_sbr']:,} | {r['join_rate']:.1%} "
        f"| {r['score_mismatches']} | {r['missing_prices']} |"
        for r in joined.iter_rows(named=True)
    ]
    parts += [
        "",
        "**Moneyline vig** (the implied probabilities' sum minus 1), per game.",
        "",
        "| Season | Games | Opening median | Opening 10th to 90th | Closing median "
        "| Closing 10th to 90th | Below 100% |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    parts += [
        f"| {r['season']} | {r['games']:,} | {_pct(r['open_median'])} "
        f"| {_spread(r['open_p10'], r['open_p90'])} | {_pct(r['close_median'])} "
        f"| {_spread(r['close_p10'], r['close_p90'])} "
        f"| {(r['open_below_100'] or 0) + (r['close_below_100'] or 0)} |"
        for r in vig.iter_rows(named=True)
    ]
    parts += [
        "",
        "**Open against close**: the move of the de-vigged home probability, in percentage points.",
        "",
        f"| Season | Games | Median move | 90th percentile | Over {BIG_MOVE * 100:.0f} points "
        "| Favourite changed | Prices unchanged |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    parts += [
        f"| {r['season']} | {r['games']:,} | {r['median']:.1%} | {r['p90']:.1%} | {r['big']} "
        f"| {r['favourite_changed']} | {r['unchanged']:.0%} |"
        for r in move_report(moved).iter_rows(named=True)
    ]
    parts += [
        "",
        f"**Closing puck line against the closing moneyline**: games whose moneyline favourite "
        f"({CLEAR_FAVOURITE:.0%} or more) is +1.5.",
        "",
        "| Season | Games with a puck line | Favourite at +1.5 |",
        "| --- | ---: | ---: |",
    ]
    parts += [
        f"| {r['season']} | {r['games']:,} | {r['conflicts']} |"
        for r in puck_lines.iter_rows(named=True)
    ]
    return "\n".join(parts)
