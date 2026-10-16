"""The screen for gate 1's review of B2's gaps above 8 points (#79, hard rule 8), as the owner
approved it on 2026-10-01.

Every gap game gets B2's log-odds broken into its inputs' contributions, beside the market's, and
flags for the signatures a bug would leave (plan §10, edge attribution):
- **history_gap:** a team's strength read fewer of its earlier games than it played, so a
  game's xG or ice time is missing;
- **out_of_range:** an input more than OUT_OF_RANGE standard deviations from the fold's training
  games;
- **away_from_home:** a non-neutral game outside the home team's primary home arena;
- **no_candidates:** a team without goalie candidates, whose goalie counts as average;
- **early_season:** a team with fewer than EARLY games this season, rated mostly on last season's;
- **limited_seats:** an arena open to only part of its seats.

The review set is every flagged game, every gap above BIG, and a seeded random SAMPLE of the
rest, split evenly across seasons. Nothing here reads a game's result: the review checks inputs
against public facts, never who won.
"""

from typing import Any

import numpy as np
import polars as pl

from nhl_edge.backtest.walk_forward import fold_start
from nhl_edge.features import team_strength as ts
from nhl_edge.game import b2
from nhl_edge.reference import Reference, lineage

OUT_OF_RANGE = 4.0
EARLY = 5
BIG = 0.20
SAMPLE = 40
SEED = 20261001
FLAGS = (
    "history_gap",
    "out_of_range",
    "away_from_home",
    "no_candidates",
    "early_season",
    "limited_seats",
)


def _logit(p: pl.Expr) -> pl.Expr:
    return (p / (1 - p)).log()


def contributions(tables: b2.Tables, gaps: pl.DataFrame) -> pl.DataFrame:
    """Each gap game (game_id, season, p_b2) with its inputs' contributions to B2's log-odds
    from its fold's fit, relative to the training games' average input; the goalie mixture's
    contribution (the log-odds with it, less those with an average goalie on both sides); h_s;
    and how many of its inputs sit beyond OUT_OF_RANGE standard deviations."""
    calendar = tables.games.select("season", "start_utc")
    inputs = b2.game_inputs(tables)
    others = [name for name in b2.INPUTS if name != "delta_g"]
    out = []
    for (season,), rows in gaps.group_by("season"):
        start = fold_start(calendar, int(season))  # type: ignore[arg-type]
        moments = tables.games.filter(pl.col("game_id").is_in(rows["game_id"].implode())).select(
            "game_id", prediction_utc="start_utc"
        )
        _, model = b2.predictions(tables, moments, int(season), start, b2.TUNED)  # type: ignore[arg-type]
        index = [b2.INPUTS.index(name) for name in others]
        means, scales, weights = (
            np.asarray(v)[index] for v in (model.means, model.scales, model.weights)
        )
        frame = inputs.join(rows.select("game_id", "p_b2"), on="game_id")
        z = (frame.select(others).to_numpy() - means) / scales
        parts = z * weights
        average = model.intercept + frame["offset"].to_numpy() + parts.sum(axis=1)
        # With both goalies average, ΔG sits at its training mean and adds nothing.
        no_goalie = 1 / (1 + np.exp(-average))
        p_b2 = frame["p_b2"].to_numpy()
        goalie = np.log(p_b2 / (1 - p_b2)) - np.log(no_goalie / (1 - no_goalie))
        out.append(
            frame.select("game_id").with_columns(
                *(pl.Series(f"c_{name}", parts[:, i]) for i, name in enumerate(others)),
                c_goalie=pl.Series(goalie),
                c_h_s=frame["offset"],
                out_of_range=pl.Series((np.abs(z) > OUT_OF_RANGE).sum(axis=1), dtype=pl.Int32),
            )
        )
    return pl.concat(out)


def history_shortfall(tables: b2.Tables, gaps: pl.DataFrame) -> pl.DataFrame:
    """Each gap game with how many earlier games each team's strength did not read: its games
    from 2011-12 with results public before the as-of time, through its line of team codes, less
    those team_strength counted (home_history, away_history)."""
    lines = lineage(Reference.load().teams)
    played = pl.concat(
        [
            tables.games.filter(pl.col("season") >= ts.FIRST_SEASON).select(
                team=pl.col(side), result_utc="observed_utc"
            )
            for side in ("home", "away")
        ]
    ).with_columns(line=pl.col("team").replace(lines))
    strength = tables.team_strength.filter(pl.col("game_id").is_in(gaps["game_id"].implode()))
    rows = []
    for row in strength.join(
        tables.games.select("game_id", "home", "away"), on="game_id"
    ).iter_rows(named=True):
        shortfall = {}
        for side in ("home", "away"):
            line = lines.get(row[side], row[side])
            earlier = played.filter(
                pl.col("line") == line, pl.col("result_utc") < row["as_of_utc"]
            ).height
            shortfall[f"{side}_missing"] = earlier - row[f"{side}_history"]
        rows.append({"game_id": row["game_id"], **shortfall})
    return pl.DataFrame(
        rows, schema={"game_id": pl.Int64, "home_missing": pl.Int64, "away_missing": pl.Int64}
    )


def screen(tables: b2.Tables, gaps: pl.DataFrame) -> pl.DataFrame:
    """Every gap game (gaps.csv's rows for one experiment) with its contributions and flags."""
    ref = Reference.load()
    homes = ref.home_arenas.filter("primary").select(
        "team", "arena_id", "first_season", "last_season"
    )
    terms = tables.schedule_terms.select(
        "game_id", "neutral_site", "capacity_share", "home_rest_days", "away_rest_days"
    )
    arena = (
        tables.games.select("game_id", "season", "home", "venue")
        .join(ref.venues, on="venue", how="left")
        .join(homes.rename({"team": "home", "arena_id": "home_arena"}), on="home", how="left")
        .filter(
            pl.col("first_season") <= pl.col("season"),
            pl.col("last_season").is_null() | (pl.col("last_season") >= pl.col("season")),
        )
        .select("game_id", at_home_arena=pl.col("arena_id") == pl.col("home_arena"))
    )
    season_games = pl.concat(
        [
            tables.games.select("game_id", "season", "game_date", team=pl.col(s))
            for s in ("home", "away")
        ]
    ).with_columns(
        before=pl.col("game_date").rank("min").over("season", "team").cast(pl.Int32) - 1
    )
    early = (
        gaps.select("game_id", "home", "away")
        .join(
            season_games.select("game_id", home="team", home_before="before"),
            on=["game_id", "home"],
        )
        .join(
            season_games.select("game_id", away="team", away_before="before"),
            on=["game_id", "away"],
        )
        .select("game_id", early_season=pl.min_horizontal("home_before", "away_before") < EARLY)
    )
    teams = pl.concat([gaps.select("game_id", team=pl.col(s)) for s in ("home", "away")]).join(
        tables.goalie_starts.select("game_id", "team").unique(), on=["game_id", "team"], how="anti"
    )
    return (
        gaps.join(contributions(tables, gaps), on="game_id")
        .join(history_shortfall(tables, gaps), on="game_id")
        .join(terms, on="game_id")
        .join(arena, on="game_id", how="left")
        .join(early, on="game_id")
        .with_columns(
            market_logit=_logit(pl.col("p_b1")),
            b2_logit=_logit(pl.col("p_b2")),
            history_gap=(pl.col("home_missing") > 0) | (pl.col("away_missing") > 0),
            out_of_range=pl.col("out_of_range") > 0,
            away_from_home=~pl.col("neutral_site") & ~pl.col("at_home_arena").fill_null(False),
            no_candidates=pl.col("game_id").is_in(teams["game_id"].implode()),
            limited_seats=pl.col("capacity_share") < 1,
        )
        .with_columns(flagged=pl.any_horizontal(*FLAGS))
        .sort("game_date", "game_id")
    )


def review_set(screened: pl.DataFrame) -> pl.DataFrame:
    """The games to review by hand: every flagged one, every gap above BIG, and a seeded random
    SAMPLE of the rest split evenly across seasons, with why each was chosen."""
    chosen = screened.with_columns(
        reason=pl.when(pl.col("flagged"))
        .then(pl.lit("flagged"))
        .when(pl.col("gap").abs() > BIG)
        .then(pl.lit("over 20 points"))
        .otherwise(None)
    )
    rest = chosen.filter(pl.col("reason").is_null())
    seasons = sorted(rest["season"].unique().to_list())
    rng = np.random.default_rng(SEED)
    picks = []
    for k, season in enumerate(seasons):
        pool = rest.filter(pl.col("season") == season).sort("game_id")
        size = SAMPLE // len(seasons) + (1 if k < SAMPLE % len(seasons) else 0)
        index = rng.choice(pool.height, size=min(size, pool.height), replace=False)
        picks.append(pool[sorted(index.tolist())].with_columns(reason=pl.lit("random sample")))
    return pl.concat([chosen.filter(pl.col("reason").is_not_null()), *picks]).sort(
        "game_date", "game_id"
    )


def summary(screened: pl.DataFrame, reviewed: pl.DataFrame) -> dict[str, Any]:
    return {
        "gaps": screened.height,
        "flags": {flag: int(screened[flag].sum()) for flag in FLAGS},
        "flagged": int(screened["flagged"].sum()),
        "over_20_points": int((screened["gap"].abs() > BIG).sum()),
        "review_set": dict(reviewed.group_by("reason").len().iter_rows()),
    }
