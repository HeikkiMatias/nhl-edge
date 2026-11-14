"""#422 (#417): late-season stakes. Once a team has clinched or is eliminated, does it dress
other skaters than the lineup projection expects, and do the flags anticipate the market's move
after the opener? Each definition was fixed on the issue before any figure was read (the comment
of 2026-11-14).

    uv run nhl standings fetch --seasons 20122013-20142015
    uv run python scripts/late_season_stakes.py

**The standings** (step 1): the raw copies of `nhl standings fetch` for every date from the first
to the last regular-season game date of the fit season and the explore half, read offline. The
standings of date D count D's games, so they are observed at 10:00 UTC on D+1, and a game on D
reads those of D-1 (standings_as_of). On every date, each team's games played and points must
equal the lake's count from its final regular-season games through that date, or the run stops.

**The flags** on each date:
- clinched: the API's clinch flag (x, y, z or p);
- eliminated: our bound. A team's best case is its points plus 2 for each game left. It is
  eliminated when, among the teams of its conference whose points already exceed that best case,
  at least DIVISION_PLACES are in its own division and at least OTHER_PLACES are beyond the first
  DIVISION_PLACES of their own divisions. Ties and head-to-head games are ignored, so the bound
  can lag the true elimination, never lead it;
- shown beside, never used: the same bound turned round for clinching, which must never come
  before the API's flag.

**Step 2, the lineups** (no result or price read): the explore half's regular-season team-games
after the team's LATE-th, flagged when clinched or eliminated in the standings known at the
projection's time. A projected skater's minutes if he dresses are m = (exp_5v5 + exp_pp + exp_pk)
/ p_available. The surprise is the sum of m over the projected skaters who did not dress, less
the sum of (1 - p_available) * m. The test is the flagged team-games' mean surprise less the
unflagged ones', the slope of the surprise on the 0/1 flag, which the weekly block bootstrap
refits on each draw of weeks (metrics.slope). Expected positive.

**Step 3, the yardstick** (ADR 0053, the explore half, E2's prediction time): clinched_diff and
eliminated_diff, the home flag less the away flag from the standings known at the prediction
time, each added to B3's split, and paired against B3's split alone on the move skill. Expected
negative.

**The decision:** Holm's step-down at ALPHA over the three tests. A test holds when it passes with
its expected sign. A yardstick candidate that holds goes to the owner as a challenger input, as
development evidence only.

It reads the lake to the explore half's last season only, and writes
reports/yardstick/late-season-stakes-<yyyymmdd>-<shortsha>.md and .json. It writes nothing to the
lake or the raw cache.
"""

import argparse
import json
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl
from nhl_edge.ingest.standings import fetch_standings, standings_as_of

from nhl_edge.backtest import metrics, reports
from nhl_edge.backtest import yardstick as ys
from nhl_edge.features.team_strength import TUNED_CUTOFF
from nhl_edge.game import b3
from nhl_edge.ingest.nhl_api import NhlApi
from nhl_edge.lake.raw import RawStore
from nhl_edge.lake.tables import Lake, Reader

ALPHA = ys.ALPHA
FAMILY = ("lineups", "clinched", "eliminated")
SEASONS = (ys.FIRST_FIT, *ys.EXPLORE)
# A team-game counts in step 2 after the team's LATE-th regular-season game, and in the look at
# the same teams before any flag from its PRE_LATE-th to its LATE-th.
LATE = 65
PRE_LATE = 50
CLINCH_FLAGS = ("x", "y", "z", "p")
# Each team's games in a season, and the playoff places: per division (the division winner in
# 2012-13, three from 2013-14) and the rest of each conference's eight (five in 2012-13, two wild
# cards from 2013-14).
SEASON_GAMES = {20122013: 48}
DIVISION_PLACES = {20122013: 1}
OTHER_PLACES = {20122013: 5}
FULL_SEASON, DIVISION_DEFAULT, OTHER_DEFAULT = 82, 3, 2
SKATERS = ("F", "D")
BASE = "b3 split"
CANDIDATES: dict[str, tuple[str, ...]] = {
    BASE: b3.SPLIT,
    "clinched": (*b3.SPLIT, "clinched_diff"),
    "eliminated": (*b3.SPLIT, "eliminated_diff"),
    # Descriptive, never part of the decision: each column alone, for the move's slope on it.
    "clinched_diff": ("clinched_diff",),
    "eliminated_diff": ("eliminated_diff",),
}
COLUMNS = {"clinched": "clinched_diff", "eliminated": "eliminated_diff"}


def regular(games: pl.DataFrame) -> pl.DataFrame:
    """The regular-season games: the game type digits of game_id are 02."""
    return games.filter((pl.col("game_id") // 10_000 % 100) == 2)


def season_days(games: pl.DataFrame, seasons: Iterable[int]) -> list[date]:
    """Every date from the first to the last regular-season game date of each season."""
    days: list[date] = []
    for season in sorted(seasons):
        dates = regular(games).filter(pl.col("season") == season)["game_date"]
        if dates.is_empty():
            raise ValueError(f"the lake has no regular-season games of {season}")
        first, last = dates.min(), dates.max()
        assert isinstance(first, date) and isinstance(last, date)
        days += [first + timedelta(days=i) for i in range((last - first).days + 1)]
    return days


def team_games(games: pl.DataFrame) -> pl.DataFrame:
    """One row per team and regular-season game: season, game_id, game_date, team, the team's
    points from it (2 for a win, 1 for an OT or shootout loss) and its game number in the season,
    in order of start."""
    played = regular(games)
    home_won = pl.col("home_score") > pl.col("away_score")
    extra = pl.col("decided_in").is_in(["OT", "SO"])
    sides = []
    for side, won in (("home", home_won), ("away", ~home_won)):
        sides.append(
            played.select(
                "season",
                "game_id",
                "game_date",
                "start_utc",
                team=pl.col(side),
                points=pl.when(won).then(2).when(extra).then(1).otherwise(0),
            )
        )
    return (
        pl.concat(sides)
        .sort("season", "team", "start_utc", "game_id")
        .with_columns(number=pl.int_range(1, pl.len() + 1).over("season", "team"))
        .drop("start_utc")
    )


def lake_counts(games: pl.DataFrame, standings: pl.DataFrame) -> pl.DataFrame:
    """Each standings row's team with its games played and points through its date from the
    lake's final regular-season games (lake_games, lake_points)."""
    played = team_games(games).select("season", "team", "game_date", "points")
    asks = standings.select("season", "standings_date", "team")
    joined = asks.join(played, on=["season", "team"], how="left").filter(
        pl.col("game_date") <= pl.col("standings_date")
    )
    counts = joined.group_by("standings_date", "team").agg(
        lake_games=pl.len(), lake_points=pl.col("points").sum()
    )
    return asks.join(counts, on=["standings_date", "team"], how="left").with_columns(
        pl.col("lake_games").fill_null(0), pl.col("lake_points").fill_null(0)
    )


def count_check(games: pl.DataFrame, standings: pl.DataFrame) -> dict[str, Any]:
    """Whether every standings row's games played and points equal the lake's through its date:
    the rows compared, those that differ, and up to ten of them."""
    both = standings.join(
        lake_counts(games, standings), on=["season", "standings_date", "team"]
    ).filter(
        (pl.col("games_played") != pl.col("lake_games"))
        | (pl.col("points") != pl.col("lake_points"))
    )
    return {
        "rows": standings.height,
        "dates": standings["standings_date"].n_unique(),
        "mismatches": both.height,
        "examples": both.head(10)
        .select("standings_date", "team", "games_played", "lake_games", "points", "lake_points")
        .with_columns(pl.col("standings_date").cast(pl.String))
        .to_dicts(),
    }


def _places(season: pl.Expr, places: Mapping[int, int], default: int) -> pl.Expr:
    return season.replace_strict(places, default=default, return_dtype=pl.Int64)


def flags(standings: pl.DataFrame) -> pl.DataFrame:
    """Each standings row (standings_date, team, observed_utc) with its flags: clinched (the
    API's flag), eliminated (our bound) and clinched_bound (the bound turned round, shown beside
    only)."""
    rows = standings.with_columns(
        best=pl.col("points")
        + 2 * (_places(pl.col("season"), SEASON_GAMES, FULL_SEASON) - pl.col("games_played")),
        k=_places(pl.col("season"), DIVISION_PLACES, DIVISION_DEFAULT),
        w=_places(pl.col("season"), OTHER_PLACES, OTHER_DEFAULT),
    )
    others = rows.select(
        "standings_date",
        "conference",
        other="team",
        other_division="division",
        other_points="points",
        other_best="best",
    )
    pairs = (
        rows.select("standings_date", "conference", "team", "division", "points", "best", "k", "w")
        .join(others, on=["standings_date", "conference"])
        .filter(pl.col("team") != pl.col("other"))
    )
    # Per division of the conference: the teams surely above (points beyond the team's best case)
    # and those that could still finish level or above (best case at least the team's points).
    per_division = pairs.group_by("standings_date", "team", "other_division").agg(
        above=(pl.col("other_points") > pl.col("best")).sum(),
        reach=(pl.col("other_best") >= pl.col("points")).sum(),
        own=(pl.col("other_division") == pl.col("division")).first(),
        k=pl.col("k").first(),
        w=pl.col("w").first(),
    )
    beyond = (pl.col("above") - pl.col("k")).clip(lower_bound=0)
    beyond_reach = (pl.col("reach") - pl.col("k")).clip(lower_bound=0)
    per_team = per_division.group_by("standings_date", "team").agg(
        above_own=pl.col("above").filter(pl.col("own")).sum(),
        above_beyond=beyond.sum(),
        reach_own=pl.col("reach").filter(pl.col("own")).sum(),
        reach_beyond=beyond_reach.sum(),
        k=pl.col("k").first(),
        w=pl.col("w").first(),
    )
    bounds = per_team.select(
        "standings_date",
        "team",
        eliminated=(pl.col("above_own") >= pl.col("k")) & (pl.col("above_beyond") >= pl.col("w")),
        clinched_bound=~(
            (pl.col("reach_own") >= pl.col("k")) & (pl.col("reach_beyond") >= pl.col("w"))
        ),
    )
    return standings.join(bounds, on=["standings_date", "team"], how="left").with_columns(
        clinched=pl.col("clinch_indicator").is_in(CLINCH_FLAGS).fill_null(False),
        eliminated=pl.col("eliminated").fill_null(False),
        clinched_bound=pl.col("clinched_bound").fill_null(False),
    )


def flag_checks(flagged: pl.DataFrame, games: pl.DataFrame) -> dict[str, Any]:
    """The flags against what happened, shown beside: team-dates where our clinch bound comes
    before the API's flag (must be none), the API's clinches and our eliminations of teams that
    then did or didn't play in the playoffs (must be none), and how many days the API's clinch
    leads our bound's."""
    playoffs = (
        games.filter((pl.col("game_id") // 10_000 % 100) == 3)
        .select("season", team=pl.concat_list("home", "away"))
        .explode("team")
        .unique()
        .with_columns(in_playoffs=pl.lit(True))
    )
    seasons = flagged.join(playoffs, on=["season", "team"], how="left").with_columns(
        pl.col("in_playoffs").fill_null(False)
    )
    first = (
        flagged.group_by("season", "team")
        .agg(
            api=pl.col("standings_date").filter(pl.col("clinched")).min(),
            bound=pl.col("standings_date").filter(pl.col("clinched_bound")).min(),
        )
        .filter(pl.col("api").is_not_null() & pl.col("bound").is_not_null())
        .with_columns(lead=(pl.col("bound") - pl.col("api")).dt.total_days())
    )
    leads = first["lead"]
    return {
        "bound_before_api": flagged.filter(pl.col("clinched_bound") & ~pl.col("clinched")).height,
        "clinched_missed_playoffs": seasons.filter(pl.col("clinched") & ~pl.col("in_playoffs"))
        .select("season", "team")
        .unique()
        .height,
        "eliminated_in_playoffs": seasons.filter(pl.col("eliminated") & pl.col("in_playoffs"))
        .select("season", "team")
        .unique()
        .height,
        "api_lead_days": {
            "teams": first.height,
            "median": float(leads.median()) if first.height else None,  # type: ignore[arg-type]
            "max": int(leads.max()) if first.height else None,  # type: ignore[arg-type]
        },
        "team_seasons": {
            name: flagged.filter(pl.col(name)).select("season", "team").unique().height
            for name in ("clinched", "eliminated", "clinched_bound")
        },
    }


def known_flags(flagged: pl.DataFrame, asks: pl.DataFrame) -> pl.DataFrame:
    """Each ask (its columns, with team and as_of_utc) with the team's clinched and eliminated
    flags from the latest standings observed before as_of_utc, False when there are none, and
    the date of those standings (null when none)."""
    found = standings_as_of(
        flagged.select("team", "standings_date", "observed_utc", "clinched", "eliminated"), asks
    )
    return found.with_columns(
        pl.col("clinched").fill_null(False), pl.col("eliminated").fill_null(False)
    )


def lineup_rows(
    lineups: pl.DataFrame, actual: pl.DataFrame, games: pl.DataFrame, flagged: pl.DataFrame
) -> pl.DataFrame:
    """Step 2's team-games of the given lineups' seasons (regular season, every number): the
    skaters' surprise, the projected starting goalie's (goalie_surprise), the team's game number
    and its flags known at the projection's time."""
    dressed = actual.select("game_id", "team", "player_id", dressed=pl.lit(True))
    skaters = (
        lineups.filter(pl.col("role").is_in(SKATERS))
        .join(dressed, on=["game_id", "team", "player_id"], how="left")
        .with_columns(
            pl.col("dressed").fill_null(False),
            m=(pl.col("exp_5v5") + pl.col("exp_pp") + pl.col("exp_pk")) / pl.col("p_available"),
        )
        .group_by("game_id", "team")
        .agg(
            missing=(pl.col("m") * (~pl.col("dressed")).cast(pl.Float64)).sum(),
            expected=(pl.col("m") * (1 - pl.col("p_available"))).sum(),
            observed_utc=pl.col("observed_utc").first(),
        )
        .with_columns(surprise=pl.col("missing") - pl.col("expected"))
    )
    starters = actual.filter(pl.col("starting_goalie")).select(
        "game_id", "team", starter="player_id"
    )
    goalies = (
        lineups.filter(pl.col("role") == "G")
        .sort("p_start", "player_id", descending=[True, False])
        .group_by("game_id", "team", maintain_order=True)
        .first()
        .join(starters, on=["game_id", "team"], how="left")
        .select(
            "game_id",
            "team",
            goalie_surprise=(pl.col("player_id") != pl.col("starter")).cast(pl.Float64)
            - (1 - pl.col("p_start")),
        )
    )
    numbered = team_games(games).select("season", "game_id", "game_date", "team", "number")
    rows = (
        skaters.join(numbered, on=["game_id", "team"])
        .join(goalies, on=["game_id", "team"], how="left")
        .with_columns(as_of_utc=pl.col("observed_utc"))
    )
    known = known_flags(flagged, rows)
    return known.with_columns(
        flagged=(pl.col("clinched") | pl.col("eliminated")).cast(pl.Float64),
        clinched_f=pl.col("clinched").cast(pl.Float64),
        eliminated_f=pl.col("eliminated").cast(pl.Float64),
    ).sort("game_id", "team")


def _slope(frame: pl.DataFrame, x: str, y: str) -> dict[str, Any] | None:
    """The slope of y on a 0/1 column x (the mean of y where x is 1 less where it is 0), with its
    weekly block bootstrap interval and p-value, or None without both groups."""
    if frame.is_empty() or frame[x].n_unique() < 2:
        return None
    spread = metrics.slope(frame, x, y)
    return {
        "mean": spread.value,
        "low": spread.low,
        "high": spread.high,
        "p_value": metrics.slope_p_value(frame, x, y),
        "flagged": int(frame[x].sum()),
        "team_games": frame.height,
    }


def lineup_figures(rows: pl.DataFrame) -> dict[str, Any]:
    """Step 2's test and the figures shown beside it."""
    late = rows.filter(pl.col("number") > LATE)
    unflagged = late.filter(pl.col("flagged") == 0)
    clinched = pl.concat([late.filter(pl.col("clinched")), unflagged])
    eliminated = pl.concat([late.filter(pl.col("eliminated")), unflagged])
    later = late.group_by("season", "team").agg(later=pl.col("flagged").max())
    before = (
        rows.filter(pl.col("number").is_between(PRE_LATE, LATE))
        .join(later, on=["season", "team"], how="left")
        .with_columns(pl.col("later").fill_null(0.0))
    )
    goalies = late.filter(pl.col("goalie_surprise").is_not_null())
    return {
        "test": _slope(late, "flagged", "surprise"),
        "late_team_games": late.height,
        "mean_surprise": {
            "flagged": float(late.filter(pl.col("flagged") == 1)["surprise"].mean() or 0.0),
            "unflagged": float(unflagged["surprise"].mean() or 0.0),
        },
        "clinched": _slope(clinched, "clinched_f", "surprise"),
        "eliminated": _slope(eliminated, "eliminated_f", "surprise"),
        "goalie": _slope(goalies, "flagged", "goalie_surprise"),
        "before_any_flag": _slope(before, "later", "surprise"),
    }


def with_columns(read: ys.Read, flagged: pl.DataFrame) -> tuple[pl.DataFrame, int]:
    """The yardstick's rows with clinched_diff and eliminated_diff from the standings known at
    each game's prediction time, and how many of the explore half's games had no standings for a
    side then (each season's first date, before any flag can exist; counted as unflagged)."""
    frame = read.frame
    sides = []
    for side in ("home", "away"):
        asks = frame.join(read.games.select("game_id", side), on="game_id").select(
            "game_id", team=pl.col(side), as_of_utc=pl.col("prediction_utc")
        )
        found = known_flags(flagged, asks).select(
            "game_id",
            pl.col("standings_date").alias(f"{side}_standings"),
            pl.col("clinched").cast(pl.Float64).alias(f"{side}_clinched"),
            pl.col("eliminated").cast(pl.Float64).alias(f"{side}_eliminated"),
        )
        sides.append(found)
    columns = (
        sides[0]
        .join(sides[1], on="game_id")
        .with_columns(
            clinched_diff=pl.col("home_clinched") - pl.col("away_clinched"),
            eliminated_diff=pl.col("home_eliminated") - pl.col("away_eliminated"),
        )
    )
    out = frame.join(
        columns.select(
            "game_id", "clinched_diff", "eliminated_diff", "home_standings", "away_standings"
        ),
        on="game_id",
    )
    explore = out.filter(pl.col("season").is_in(ys.EXPLORE))
    unknown = explore.filter(
        pl.col("home_standings").is_null() | pl.col("away_standings").is_null()
    ).height
    return out.drop("home_standings", "away_standings").sort("game_id"), unknown


def paired_frame(per_game: pl.DataFrame, a: str, b: str, measure: str = "move") -> pl.DataFrame:
    """a's measure less b's, game by game on the games both scored (difference)."""
    left = per_game.filter(pl.col("candidate") == a)
    right = per_game.filter(pl.col("candidate") == b).select("game_id", other=measure)
    return left.join(right, on="game_id").with_columns(difference=pl.col(measure) - pl.col("other"))


def decide(
    lineups: Mapping[str, Any], per_game: pl.DataFrame, frame: pl.DataFrame
) -> dict[str, Any]:
    """The family's three estimates, their p-values, Holm's step-down at ALPHA and whether each
    test holds (passes with its expected sign)."""
    test = lineups["test"]
    if test is None:
        raise ValueError("step 2 has no flagged or no unflagged team-game")
    estimates: dict[str, dict[str, Any]] = {
        "lineups": {
            "primary": {k: test[k] for k in ("mean", "low", "high")},
            "p_value": test["p_value"],
            "sign": "+",
        }
    }
    nonzero = frame.select("game_id", "clinched_diff", "eliminated_diff")
    for name, column in COLUMNS.items():
        both = paired_frame(per_game, name, BASE).join(nonzero, on="game_id")
        flagged = both.filter(pl.col(column) != 0)
        estimates[name] = {
            "primary": metrics.bootstrap(both, "difference").to_dict(),
            "p_value": metrics.p_value(both, "difference"),
            "sign": "-",
            "rested": metrics.bootstrap(both.filter(pl.col("rested")), "difference").to_dict(),
            "result": metrics.bootstrap(
                paired_frame(per_game, name, BASE, "result"), "difference"
            ).to_dict(),
            "flagged_games": flagged.height,
            "on_flagged_games": metrics.bootstrap(flagged, "difference").to_dict()
            if flagged.height
            else None,
        }
    steps = metrics.holm({n: e["p_value"] for n, e in estimates.items()}, ALPHA)
    for name, entry in estimates.items():
        mean = entry["primary"]["mean"]
        right_sign = mean > 0 if entry["sign"] == "+" else mean < 0
        entry |= {
            "threshold": steps[name]["threshold"],
            "holds": steps[name]["passes"] and right_sign,
        }
    return {"family": list(FAMILY), "estimates": estimates}


def run(lake: Reader, api: NhlApi) -> dict[str, Any]:
    """Every step on the lake to the explore half's last season, and the family's decision."""
    last = max(ys.EXPLORE)
    read = ys.read(lake, last)
    games = read.games
    standings, failed = fetch_standings(api, season_days(games, SEASONS))
    if failed:
        raise ValueError(f"{len(failed)} standings dates failed: {failed[:3]}")
    check = count_check(games, standings)
    if check["mismatches"]:
        raise ValueError(f"the standings differ from the lake's games: {check['examples']}")
    flagged = flags(standings)
    explore_games = games.filter(pl.col("season").is_in(ys.EXPLORE))
    rows = lineup_rows(
        lake.read("lineups").filter(pl.col("season").is_in(ys.EXPLORE)),
        lake.read("actual_lineups").filter(pl.col("season").is_in(ys.EXPLORE)),
        explore_games,
        flagged,
    )
    lineups = lineup_figures(rows)
    frame, unknown = with_columns(read, flagged)
    per_game, figures = ys.run(frame, "explore", CANDIDATES)
    return {
        "standings": check | {"flags": flag_checks(flagged, games)},
        "lineups": lineups,
        "explore_games_without_standings": unknown,
        "tuning_cutoff": str(TUNED_CUTOFF),
        "figures": figures,
        "decision": decide(lineups, per_game, frame),
    }


def _plain(estimate: Mapping[str, Any] | None, digits: int = 2) -> str:
    if estimate is None:
        return "none"
    mean = estimate.get("mean", estimate.get("value"))
    return f"{mean:+.{digits}f} [{estimate['low']:+.{digits}f}, {estimate['high']:+.{digits}f}]"


def _thousandths(estimate: Mapping[str, Any] | None) -> str:
    if estimate is None:
        return "none"
    return (
        f"{estimate['mean'] * 1000:+.3f} [{estimate['low'] * 1000:+.3f}, "
        f"{estimate['high'] * 1000:+.3f}]"
    )


def markdown(found: Mapping[str, Any], version: str) -> str:
    standings, lineups = found["standings"], found["lineups"]
    checks, decision = standings["flags"], found["decision"]["estimates"]
    test = lineups["test"]
    lines = [
        f"# Late-season stakes ({version})",
        "",
        "#422 (#417), each definition fixed on the issue before any figure was read (the comment",
        "of 2026-11-14). The fit season is 2012-13 and the explore half 2013-14 and 2014-15.",
        "Intervals are 95% weekly block bootstrap. Not fresh: earlier studies read these seasons,",
        "so every p-value is development evidence only.",
        "",
        "**In-sample settings:** the yardstick's features were tuned on these seasons, through",
        f"{found['tuning_cutoff']}, and are read at their data's time under the training seasons'",
        "exception (ADR 0053, ADR 0011). None of the yardstick's figures is out of sample.",
        "",
        "## Step 1: the standings",
        "",
        f"- **The check:** {standings['rows']:,} team-dates on {standings['dates']} dates;",
        f"  games played and points differ from the lake's on {standings['mismatches']}.",
        f"- **Team-seasons flagged:** clinched {checks['team_seasons']['clinched']},",
        f"  eliminated {checks['team_seasons']['eliminated']}; our clinch bound",
        f"  {checks['team_seasons']['clinched_bound']}.",
        f"- **Our clinch bound before the API's flag:** {checks['bound_before_api']} team-dates",
        "  (must be 0). The API's clinch leads our bound's by a median of",
        f"  {checks['api_lead_days']['median']} days (at most {checks['api_lead_days']['max']}).",
        "- **Against the playoffs:** clinched teams that missed them,",
        f"  {checks['clinched_missed_playoffs']}; eliminated teams that played in them,",
        f"  {checks['eliminated_in_playoffs']} (both must be 0).",
        "",
        "## Step 2: the lineups",
        "",
        f"Team-games after the team's {LATE}th: {lineups['late_team_games']:,}, of them",
        f"{test['flagged'] if test else 0} flagged. The surprise is in skater-minutes per",
        "team-game; positive means more of the projection's minutes missing than it expected.",
        "",
        "| Figure | Flagged less unflagged | p | Flagged team-games |",
        "| --- | --- | --- | --- |",
    ]
    for label, key in (
        ("**Clinched or eliminated (the test)**", "test"),
        ("Clinched", "clinched"),
        ("Eliminated", "eliminated"),
        ("Starting goalie (share, not minutes)", "goalie"),
        (f"Games {PRE_LATE} to {LATE}, flagged later less the rest", "before_any_flag"),
    ):
        entry = lineups[key]
        lines.append(
            f"| {label} | {_plain(entry, 3 if key == 'goalie' else 2)} | "
            f"{entry['p_value']:.4f} | {entry['flagged']} |"
            if entry
            else f"| {label} | none | | |"
        )
    means = lineups["mean_surprise"]
    lines += [
        "",
        f"Mean surprise: flagged {means['flagged']:+.2f}, unflagged {means['unflagged']:+.2f}.",
        "",
        "## Step 3: the yardstick",
        "",
        "Move skill in thousandths, the candidate less B3's split alone (negative anticipates the",
        f"move). Explore games without standings for a side at the prediction time: "
        f"{found['explore_games_without_standings']} (counted as unflagged).",
        "",
        "| Candidate | All games | Rested | On games with the flag | Games with the flag | "
        "Result beyond the close |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for name in COLUMNS:
        entry = decision[name]
        lines.append(
            f"| {name} | {_thousandths(entry['primary'])} | {_thousandths(entry['rested'])} | "
            f"{_thousandths(entry['on_flagged_games'])} | {entry['flagged_games']} | "
            f"{_thousandths(entry['result'])} |"
        )
    lines += ["", "Each column's move slope on its own (log-odds per unit):", ""]
    for name in COLUMNS.values():
        slope = found["figures"][name].get("slope")
        lines.append(f"- `{name}`: {_plain(slope, 4)}")
    lines += [
        "",
        "## The decision",
        "",
        f"Holm's step-down at {ALPHA} over the three tests; a test holds when it passes with its",
        "expected sign.",
        "",
        "| Test | Estimate | Expected sign | p | Threshold | Holds |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for name in FAMILY:
        entry = decision[name]
        shown = _plain(entry["primary"]) if name == "lineups" else _thousandths(entry["primary"])
        lines.append(
            f"| {name} | {shown} | {entry['sign']} | {entry['p_value']:.4f} | "
            f"{entry['threshold']:.4f} | {'yes' if entry['holds'] else 'no'} |"
        )
    kept = [n for n in COLUMNS if decision[n]["holds"]]
    lines += [
        "",
        f"**Kept for the owner:** {', '.join(kept) if kept else 'none'}.",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Late-season stakes (#422).")
    parser.add_argument("--out", type=Path, default=Path("reports/yardstick"))
    args = parser.parse_args()
    found = run(Lake(), NhlApi(RawStore(), offline=True))
    version = reports.version("late-season-stakes", datetime.now(UTC))
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / f"{version}.json").write_text(
        json.dumps(found, indent=2, sort_keys=True, default=str)
    )
    path = args.out / f"{version}.md"
    path.write_text(markdown(found, version))
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
