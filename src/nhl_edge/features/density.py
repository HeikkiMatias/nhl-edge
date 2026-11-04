"""Schedule density (#370, ADR 0056): how much each team has played lately, from the season's games
whose results were public before the prediction time (ADR 0003), and tonight's slate, whose
schedule is public a day before (ADR 0005).

The five columns (DENSITY), each home less away or signed toward the home side:
- `dense3_diff` and `dense6_diff`: each team's games over the 3 and 6 days before tonight;
- `mismatch`: +1 when the away team is on a back-to-back and the home team rested 2 days or more,
  -1 the other way, else 0;
- `home_return`: the home team's road games in a row just before tonight;
- `away_trip`: the away team's road games in a row before tonight.

#370 scored them on the yardstick's explore half, where they anticipated the market's move from
the opener to the close (reports/yardstick/targeted-features-20261104-ee21ec8.md). close_forecast
version 2 reads them at the morning snapshot (ADR 0056).
"""

from datetime import timedelta

import polars as pl

DENSITY = ("dense3_diff", "dense6_diff", "mismatch", "home_return", "away_trip")


def team_games(games: pl.DataFrame, moments: pl.DataFrame) -> pl.DataFrame:
    """Each game of moments (game_id, prediction_utc) for each of its teams, from the team's
    earlier games of the season whose results were public before the game's prediction time
    (ADR 0003): its games over the 3 and 6 days before, its rest in days since the latest (None
    without one), its road games in a row just before, and the game's number in the season.

    games holds the games of moments and the season's earlier games (game_id, season, game_date,
    start_utc, home, away, observed_utc, when the result became public). A game whose
    observed_utc is null or not before the prediction is never counted."""
    sides = pl.concat(
        [
            games.select(
                "game_id",
                "season",
                "game_date",
                "start_utc",
                "observed_utc",
                team=side,
                home=pl.lit(side == "home"),
            )
            for side in ("home", "away")
        ]
    )
    targets = sides.join(moments, on="game_id")
    earlier = sides.select(
        "season",
        "team",
        other_date="game_date",
        other_start="start_utc",
        other_utc="observed_utc",
        other_home="home",
    )
    date = pl.col("game_date")
    known = (
        targets.join(earlier, on=["season", "team"])
        .filter(
            pl.col("other_start") < pl.col("start_utc"),
            pl.col("other_utc") < pl.col("prediction_utc"),
        )
        .group_by("game_id", "team")
        .agg(
            before=pl.len(),
            dense3=(pl.col("other_date") >= date - timedelta(days=3)).sum(),
            dense6=(pl.col("other_date") >= date - timedelta(days=6)).sum(),
            last=pl.col("other_date").max(),
            # Road games in a row: from the latest back, until the first at home.
            road=(~pl.col("other_home"))
            .cast(pl.Int32)
            .sort_by("other_start", descending=True)
            .cum_prod()
            .sum(),
        )
    )
    return (
        targets.join(known, on=["game_id", "team"], how="left")
        .with_columns(pl.col("before", "dense3", "dense6", "road").fill_null(0).cast(pl.Int32))
        .with_columns(rest=(date - pl.col("last")).dt.total_days(), number=pl.col("before") + 1)
    )


def density(games: pl.DataFrame, moments: pl.DataFrame) -> pl.DataFrame:
    """Each game of moments with its density columns (DENSITY), from the season's games whose
    results were public before its prediction time (team_games)."""
    per = team_games(games, moments)
    out = games.join(moments.select("game_id"), on="game_id").select("game_id", "home", "away")
    for side in ("home", "away"):
        part = per.select(
            "game_id",
            pl.col("team").alias(side),
            *(pl.col(c).alias(f"{side}_{c}") for c in ("rest", "dense3", "dense6", "road")),
        )
        out = out.join(part, on=["game_id", side])
    tired = {side: pl.col(f"{side}_rest") == 1 for side in ("home", "away")}
    fresh = {side: pl.col(f"{side}_rest").fill_null(99) >= 2 for side in ("home", "away")}
    return out.select(
        "game_id",
        dense3_diff=(pl.col("home_dense3") - pl.col("away_dense3")).cast(pl.Float64),
        dense6_diff=(pl.col("home_dense6") - pl.col("away_dense6")).cast(pl.Float64),
        mismatch=(
            (tired["away"].fill_null(False) & fresh["home"]).cast(pl.Float64)
            - (tired["home"].fill_null(False) & fresh["away"]).cast(pl.Float64)
        ),
        home_return=pl.col("home_road").cast(pl.Float64),
        away_trip=pl.col("away_road").cast(pl.Float64),
    )
