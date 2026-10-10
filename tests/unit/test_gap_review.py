from datetime import date

import numpy as np
import polars as pl
from b2_fixtures import league

from nhl_edge.backtest import gap_review as review
from nhl_edge.features import team_strength as ts
from nhl_edge.game import b2

VENUES = {
    "BOS": "TD Garden",
    "TOR": "Scotiabank Arena",
    "MTL": "Centre Bell",
    "NYR": "Madison Square Garden",
    "CHI": "United Center",
    "DET": "Little Caesars Arena",
    "EDM": "Rogers Place",
    "CGY": "Scotiabank Saddledome",
}
TEST = 20182019


def tables() -> b2.Tables:
    """The B2 fixture league in real arenas, with team strength's history counts filled in as
    team_strength writes them: the team's earlier games public before the as-of time."""
    base = league()
    games = base.games.with_columns(venue=pl.col("home").replace_strict(VENUES))
    played = pl.concat(
        [games.select(team=pl.col(s), result_utc="observed_utc") for s in ("home", "away")]
    )
    rows = []
    for row in games.join(base.team_strength, on="game_id").iter_rows(named=True):
        counts = {
            f"{side}_history": played.filter(
                pl.col("team") == row[side], pl.col("result_utc") < row["as_of_utc"]
            ).height
            for side in ("home", "away")
        }
        rows.append({"game_id": row["game_id"], **counts})
    strength = base.team_strength.join(pl.DataFrame(rows), on="game_id")
    return b2.Tables(
        games,
        strength,
        base.schedule_terms,
        base.goalie_starts,
        base.goalie_effects,
        base.actual_lineups,
    )


TABLES = tables()


def gaps(tables: b2.Tables = TABLES, n: int = 60) -> pl.DataFrame:
    """Some of the test season's games as gaps.csv rows, with B2's own probability."""
    games = tables.games.filter(pl.col("season") == TEST).sort("game_id")
    moments = games.select("game_id", prediction_utc="start_utc")
    start = games["start_utc"].min()
    predicted, _ = b2.predictions(tables, moments, TEST, start, b2.TUNED)  # type: ignore[arg-type]
    return (
        games.head(n)
        .join(predicted.select("game_id", p_b2="p_home"), on="game_id")
        .select(
            "season",
            "game_id",
            "game_date",
            "home",
            "away",
            "p_b2",
            p_b1=pl.lit(0.5),
            gap=pl.col("p_b2") - 0.5,
        )
    )


def test_the_contributions_add_up_to_b2s_log_odds() -> None:
    rows = gaps()
    parts = review.contributions(TABLES, rows).join(rows, on="game_id")
    columns = [c for c in parts.columns if c.startswith("c_") and c != "c_h_s"]
    games = TABLES.games.filter(pl.col("season") == TEST)
    _, model = b2.predictions(
        TABLES,
        games.select("game_id", prediction_utc="start_utc"),
        TEST,
        games["start_utc"].min(),  # type: ignore[arg-type]
        b2.TUNED,
    )
    total = model.intercept + parts["c_h_s"].to_numpy() + parts.select(columns).to_numpy().sum(1)
    logit = parts.select((pl.col("p_b2") / (1 - pl.col("p_b2"))).log()).to_series().to_numpy()
    assert np.allclose(total, logit, atol=1e-9)


def test_a_team_strength_that_read_fewer_games_than_played_is_flagged() -> None:
    rows = gaps()
    clean = review.history_shortfall(TABLES, rows)
    assert (clean.select("home_missing", "away_missing").to_numpy() == 0).all()
    game = rows["game_id"][5]
    short = TABLES.team_strength.with_columns(
        home_history=pl.when(pl.col("game_id") == game)
        .then(pl.col("home_history") - 2)
        .otherwise(pl.col("home_history"))
    )
    tables = b2.Tables(**{**TABLES.__dict__, "team_strength": short})
    screened = review.screen(tables, rows)
    assert screened.filter("history_gap")["game_id"].to_list() == [game]


def test_flags_for_an_odd_arena_seats_a_missing_goalie_and_early_season() -> None:
    rows = gaps()
    moved, limited, alone = rows["game_id"][10], rows["game_id"][11], rows["game_id"][12]
    games = TABLES.games.with_columns(
        venue=pl.when(pl.col("game_id") == moved).then(pl.lit("Rogers Place")).otherwise("venue")
    )
    terms = TABLES.schedule_terms.with_columns(
        capacity_share=pl.when(pl.col("game_id") == limited).then(0.5).otherwise("capacity_share")
    )
    home = rows.filter(pl.col("game_id") == alone)["home"].item()
    starts = TABLES.goalie_starts.filter(~((pl.col("game_id") == alone) & (pl.col("team") == home)))
    tables = b2.Tables(
        **{**TABLES.__dict__, "games": games, "schedule_terms": terms, "goalie_starts": starts}
    )
    screened = review.screen(tables, rows)

    def flagged(flag: str) -> list[int]:
        return screened.filter(flag)["game_id"].to_list()

    assert flagged("away_from_home") == [moved]
    assert flagged("limited_seats") == [limited]
    assert flagged("no_candidates") == [alone]
    # A team with fewer than EARLY earlier games this season, counted here from the schedule.
    season = TABLES.games.filter(pl.col("season") == TEST)

    def before(team: str, day: date) -> int:
        plays = (pl.col("home") == team) | (pl.col("away") == team)
        return season.filter(plays, pl.col("game_date") < day).height

    early = {
        game
        for game, day, home, away in rows.select("game_id", "game_date", "home", "away").rows()
        if min(before(home, day), before(away, day)) < review.EARLY
    }
    assert early and set(flagged("early_season")) == early
    assert not screened.filter(pl.col("game_id") == moved)["history_gap"].item()


def test_the_review_set_takes_flagged_big_and_a_seeded_sample() -> None:
    rows = gaps(n=200).with_columns(
        gap=pl.when(pl.col("game_id") == pl.col("game_id").max()).then(0.3).otherwise(pl.col("gap"))
    )
    screened = review.screen(TABLES, rows)
    chosen = review.review_set(screened)
    reasons = dict(chosen.group_by("reason").len().iter_rows())
    assert reasons["flagged"] == screened["flagged"].sum()
    assert (
        reasons.get("over 20 points", 0)
        == screened.filter(~pl.col("flagged"), pl.col("gap").abs() > review.BIG).height
    )
    assert reasons["random sample"] == min(
        review.SAMPLE, screened.filter(~pl.col("flagged"), pl.col("gap").abs() <= review.BIG).height
    )
    # Seeded: the same set every run.
    assert review.review_set(screened).equals(chosen)
    assert ts.FIRST_SEASON <= TEST
