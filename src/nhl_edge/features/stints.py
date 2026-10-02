"""Stints (#97, ADR 0015): the stretches of a game in which the players on the ice do not change,
RAPM's rows (docs/plan.md sections 4 and 5).

A game's stints come from its shift chart, and only a complete chart gives any (ShiftCoverage,
ADR 0009): every dressed player's shifts add up to his time on ice. Each period is cut at every
shift start and end, and at every goal, so a stint has one score. A player is on the ice for the
moments t with start_s < t <= end_s, as in Shifts, so a stint holds the shots and goals of
(start_s, end_s]. Two stretches in a row with the same players and score are one stint.

The chart's counts are kept as they are (ADR 0009 trusts a complete chart over situationCode).
RAPM leaves out only a stint whose counts are impossible (ADR 0015): a team with fewer than 3 or
more than 6 skaters, or with two goalies. Those stay in the table with a drop_reason, so the audit
can say what was left out.

Each stint also has the score at its start, the zone of a faceoff at its start (none for a change
on the fly), and each team's xG and goals, xG from the season's xG model (shot_xg), which was
fitted before the season began. A game's stints are public with its feeds, at 10:00 UTC the
morning after (ADR 0004), so no prediction for the game sees them.

player_seconds gives each skater's seconds at 5v5, on the power play, short-handed and otherwise,
per game, for the lineup projection (#100) and B3 (#106).
"""

import polars as pl

from nhl_edge.backtest.walk_forward import fold_start
from nhl_edge.features import xg
from nhl_edge.lake.schemas import STINT_SKATERS, Stints, dtypes

# Strength states from one team's side, both goalies in (as team strength's).
EVEN = "5v5"
POWER_PLAY = ("5v4", "5v3", "4v3")
PENALTY_KILL = ("4v5", "3v5", "3v4")
STATES = ("5v5", "pp", "pk", "other")
GAME_KEYS = ("game_id", "season", "game_date")


def _goal_times(shots: pl.DataFrame) -> pl.DataFrame:
    """The moment of every goal, penalty shots included: the score changes there."""
    return shots.filter(pl.col("is_goal")).select("game_id", "period", t="seconds").unique()


def _on_ice(shifts: pl.DataFrame, lineups: pl.DataFrame, goals: pl.DataFrame) -> pl.DataFrame:
    """Each stretch between two consecutive shift starts or ends or goals of a period
    (start_s, end_s], with every player on the ice for it, the player's side and whether he is a
    goalie."""
    bounds = (
        pl.concat(
            [
                shifts.select("game_id", "period", t="start_s"),
                shifts.select("game_id", "period", t="end_s"),
                goals.join(shifts.select("game_id").unique(), on="game_id"),
            ]
        )
        .unique()
        .sort("game_id", "period", "t")
        .with_columns(k=pl.int_range(pl.len()).over("game_id", "period"))
    )
    sides = lineups.select("game_id", "team", "is_home").unique()
    goalies = lineups.filter(pl.col("role") == "G").select(
        "game_id", "player_id", is_goalie=pl.lit(True)
    )
    first = bounds.rename({"t": "start_s", "k": "k_start"})
    last = bounds.rename({"t": "end_s", "k": "k_end"})
    return (
        shifts.select("game_id", "period", "team", "player_id", "start_s", "end_s")
        .join(sides, on=["game_id", "team"])
        .join(goalies, on=["game_id", "player_id"], how="left")
        .join(first, on=["game_id", "period", "start_s"])
        .join(last, on=["game_id", "period", "end_s"])
        # A shift from bound k_start to bound k_end covers the stretches k_start to k_end - 1.
        .select(
            "game_id",
            "period",
            "player_id",
            "is_home",
            pl.col("is_goalie").fill_null(False),
            k=pl.int_ranges("k_start", "k_end"),
        )
        .explode("k", empty_as_null=False)
        .join(
            bounds.with_columns(end_s=pl.col("t").shift(-1).over("game_id", "period")).rename(
                {"t": "start_s"}
            ),
            on=["game_id", "period", "k"],
        )
    )


def _players(on_ice: pl.DataFrame, goals: pl.DataFrame) -> pl.DataFrame:
    """Per stretch, each team's skaters (sorted ids) and goalies, merged with the stretch before
    when the same players are on the ice, no gap separates them and no goal changed the score."""
    skater, goalie, home = ~pl.col("is_goalie"), pl.col("is_goalie"), pl.col("is_home")
    ids = pl.col("player_id")
    stretches = (
        on_ice.group_by("game_id", "period", "start_s", "end_s")
        .agg(
            home_skaters=ids.filter(skater & home).unique().sort(),
            away_skaters=ids.filter(skater & ~home).unique().sort(),
            home_goalies=ids.filter(goalie & home).unique().sort(),
            away_goalies=ids.filter(goalie & ~home).unique().sort(),
        )
        .sort("game_id", "period", "start_s")
    )
    players = ("home_skaters", "away_skaters", "home_goalies", "away_goalies")
    by = ("game_id", "period")
    same = pl.all_horizontal(pl.col(c) == pl.col(c).shift(1).over(by) for c in players)
    joined = pl.col("start_s") == pl.col("end_s").shift(1).over(by)
    after_goal = goals.select("game_id", "period", start_s="t", after_goal=pl.lit(True))
    return (
        stretches.join(after_goal, on=["game_id", "period", "start_s"], how="left")
        .with_columns(new=~(same & joined).fill_null(False) | pl.col("after_goal").fill_null(False))
        .with_columns(group=pl.col("new").cum_sum())
        .group_by("game_id", "period", "group")
        .agg(
            pl.col("start_s").min(),
            pl.col("end_s").max(),
            *(pl.col(c).first() for c in players),
        )
        .drop("group")
    )


def _score(stints: pl.DataFrame, shots: pl.DataFrame) -> pl.DataFrame:
    """Each stint's home goals minus away goals at its start: every goal at or before start_s."""
    goals = shots.filter(pl.col("is_goal")).select(
        "game_id", t="seconds", sign=pl.when(pl.col("is_home")).then(1).otherwise(-1)
    )
    margin = (
        stints.select("game_id", "period", "start_s")
        .join(goals, on="game_id")
        .filter(pl.col("t") <= pl.col("start_s"))
        .group_by("game_id", "period", "start_s")
        .agg(score_state=pl.col("sign").sum())
    )
    return stints.join(margin, on=["game_id", "period", "start_s"], how="left").with_columns(
        pl.col("score_state").fill_null(0).cast(pl.Int8)
    )


def _zone(stints: pl.DataFrame, faceoffs: pl.DataFrame) -> pl.DataFrame:
    """The zone of a faceoff at each stint's start, from the home team's side; the last one when
    a second has two."""
    starts = (
        faceoffs.sort("sort_order")
        .group_by("game_id", "period", "seconds")
        .agg(zone_start=pl.col("zone").last())
        .rename({"seconds": "start_s"})
    )
    return stints.join(starts, on=["game_id", "period", "start_s"], how="left")


def _xg_models(
    games: pl.DataFrame, shots: pl.DataFrame, shot_xg: pl.DataFrame, calendar: pl.DataFrame
) -> pl.DataFrame:
    """The xG model of each game (game_id, xg_version, xg_train_cutoff), from its season's rows of
    shot_xg; a game of a season without any has none. Fails when a season's rows come from more
    than one model, when its model was not fitted before the season's first game (fold_start,
    hard rule 1), or when a shot the model scores (xg.scorable) has no row: nhl xg must run again.
    games holds the game_id and season of the games whose shots are passed."""
    seasons = (
        shot_xg.join(games.select("game_id"), on="game_id")
        .group_by("season")
        .agg(
            xg_version=pl.col("artifact_version").first(),
            xg_train_cutoff=pl.col("train_cutoff").first(),
            models=pl.struct("artifact_version", "train_cutoff").n_unique(),
        )
    )
    for row in seasons.sort("season").iter_rows(named=True):
        if row["models"] > 1:
            raise ValueError(f"{row['season']} has xG from more than one model: rerun nhl xg")
        start = fold_start(calendar, row["season"])
        if row["xg_train_cutoff"] >= start:
            raise ValueError(
                f"{row['season']}'s xG model was fitted at {row['xg_train_cutoff']}, not before "
                f"the season's first game at {start}: rerun nhl xg"
            )
    models = games.select("game_id", "season").join(seasons.drop("models"), on="season")
    unscored = (
        shots.join(models.select("game_id"), on="game_id")
        .filter(xg.scorable())
        .join(shot_xg.select("game_id", "event_id"), on=["game_id", "event_id"], how="anti")
        .sort("game_id", "event_id")
    )
    if unscored.height:
        examples = ", ".join(
            f"{g} event {e}" for g, e in unscored.select("game_id", "event_id").head(3).rows()
        )
        raise ValueError(
            f"{unscored.height} shots of {unscored['game_id'].n_unique()} games have no xG, "
            f"e.g. {examples}: rerun nhl xg"
        )
    return models.drop("season")


def _shot_totals(
    stints: pl.DataFrame, shots: pl.DataFrame, shot_xg: pl.DataFrame, models: pl.DataFrame
) -> pl.DataFrame:
    """Each team's xG and goals in each stint from its unblocked shots, penalty shots left out,
    with the game's xG model (models); xG null for a game without one. A shot the model does not
    score, at an empty net or without coordinates, counts 0 xG."""
    located = (
        shots.filter(~pl.col("is_penalty_shot"))
        .join(shot_xg.select("game_id", "event_id", "xg"), on=["game_id", "event_id"], how="left")
        .select(
            "game_id",
            "period",
            "is_home",
            "is_goal",
            pl.col("xg").fill_null(0.0),
            # The stint with start_s < seconds is the last one starting at seconds - 1 or before.
            start_s=pl.col("seconds") - 1,
            seconds="seconds",
        )
        .sort("game_id", "period", "start_s")
        .join_asof(
            stints.select(
                "game_id", "period", "start_s", stint_start="start_s", stint_end="end_s"
            ).sort("game_id", "period", "start_s"),
            on="start_s",
            by=["game_id", "period"],
            strategy="backward",
            check_sortedness=False,
        )
        .filter(pl.col("seconds") <= pl.col("stint_end"))
        .drop("start_s")
        .rename({"stint_start": "start_s"})
    )
    home = pl.col("is_home")
    totals = located.group_by("game_id", "period", "start_s").agg(
        home_xg=pl.col("xg").filter(home).sum(),
        away_xg=pl.col("xg").filter(~home).sum(),
        home_goals=(pl.col("is_goal") & home).sum().cast(pl.Int8),
        away_goals=(pl.col("is_goal") & ~home).sum().cast(pl.Int8),
    )
    versioned = pl.col("xg_version").is_not_null()
    return (
        stints.join(totals, on=["game_id", "period", "start_s"], how="left")
        .join(models, on="game_id", how="left")
        .with_columns(
            pl.when(versioned).then(pl.col(c).fill_null(0.0)).alias(c)
            for c in ("home_xg", "away_xg")
        )
        .with_columns(pl.col(c).fill_null(0).cast(pl.Int8) for c in ("home_goals", "away_goals"))
    )


def build(
    coverage: pl.DataFrame,
    shifts: pl.DataFrame,
    lineups: pl.DataFrame,
    shots: pl.DataFrame,
    shot_xg: pl.DataFrame,
    faceoffs: pl.DataFrame,
    calendar: pl.DataFrame,
) -> pl.DataFrame:
    """The stints of every game coverage rates complete, as Stints rows, from the games' own
    per-game tables and their shots' xG. calendar holds the season and start_utc of every game of
    the seasons, for each season's fold start."""
    complete = coverage.filter(pl.col("complete")).select("game_id", "season")
    shifts = shifts.join(complete, on="game_id", how="semi")
    if shifts.is_empty():
        return pl.DataFrame(schema=dtypes(Stints))
    goals = _goal_times(shots)
    models = _xg_models(complete, shots, shot_xg, calendar)
    low, high = STINT_SKATERS
    count = {side: pl.col(f"{side}_skaters").list.len() for side in ("home", "away")}
    goalies = {side: pl.col(f"{side}_goalies") for side in ("home", "away")}
    stints = _players(_on_ice(shifts, lineups, goals), goals).with_columns(
        seconds=pl.col("end_s") - pl.col("start_s"),
        strength=pl.format("{}v{}", count["home"], count["away"]),
        drop_reason=pl.when(
            ~(count["home"].is_between(low, high) & count["away"].is_between(low, high))
        )
        .then(pl.lit("skaters"))
        .when((goalies["home"].list.len() > 1) | (goalies["away"].list.len() > 1))
        .then(pl.lit("goalies")),
        **{
            f"{side}_goalie": pl.when(goalies[side].list.len() == 1).then(
                goalies[side].list.first()
            )
            for side in ("home", "away")
        },
    )
    stints = _shot_totals(_zone(_score(stints, shots), faceoffs), shots, shot_xg, models)
    observed = (
        pl.concat(
            [
                frame.select("game_id", "observed_utc")
                for frame in (shifts, lineups, shots, faceoffs, coverage)
            ]
        )
        .group_by("game_id")
        .agg(pl.col("observed_utc").max())
    )
    keys = coverage.select(*GAME_KEYS)
    frame = (
        stints.join(keys, on="game_id")
        .join(observed, on="game_id")
        .sort("game_id", "period", "start_s")
        .with_columns(stint_id=pl.int_range(1, pl.len() + 1).over("game_id"))
    )
    frame = frame.select(list(dtypes(Stints))).cast(dtypes(Stints))  # type: ignore[arg-type]
    return Stints.validate(frame)


def player_seconds(stints: pl.DataFrame) -> pl.DataFrame:
    """Each skater's seconds per game in each state of STATES, from the stints RAPM keeps: 5v5 and
    the power play and penalty kill states of team strength, both goalies in; anything else, such
    as 4v4 or an empty net, is other. observed_utc is the game's stints', so a reader takes a
    game's seconds only once they are public (hard rule 1)."""
    kept = stints.filter(pl.col("drop_reason").is_null())
    both_in = pl.col("home_goalie").is_not_null() & pl.col("away_goalie").is_not_null()
    sides = []
    for own, other in (("home", "away"), ("away", "home")):
        state = pl.format(
            "{}v{}", pl.col(f"{own}_skaters").list.len(), pl.col(f"{other}_skaters").list.len()
        )
        sides.append(
            kept.select(
                *GAME_KEYS,
                "observed_utc",
                player_id=pl.col(f"{own}_skaters"),
                is_home=pl.lit(own == "home"),
                state=pl.when(both_in & (state == EVEN))
                .then(pl.lit("5v5"))
                .when(both_in & state.is_in(POWER_PLAY))
                .then(pl.lit("pp"))
                .when(both_in & state.is_in(PENALTY_KILL))
                .then(pl.lit("pk"))
                .otherwise(pl.lit("other")),
                seconds="seconds",
            ).explode("player_id", empty_as_null=False)
        )
    return (
        pl.concat(sides)
        .group_by(*GAME_KEYS, "player_id", "is_home", "state", "observed_utc")
        .agg(pl.col("seconds").sum())
        .sort("game_id", "player_id", "state")
    )


def input_problems(
    games: pl.DataFrame, coverage: pl.DataFrame, shot_xg: pl.DataFrame, seasons: list[int]
) -> list[str]:
    """Why the lake cannot build these seasons' stints: games without a shift_coverage row, which
    the ingest did not parse, or a season from xG's first on without any xG (run nhl xg)."""
    problems = []
    wanted = games.filter(pl.col("season").is_in(seasons))
    missing = wanted.join(coverage.select("game_id"), on="game_id", how="anti")
    for (season,), frame in missing.sort("game_id").group_by("season", maintain_order=True):
        examples = ", ".join(str(g) for g in frame["game_id"].head(3).to_list())
        problems.append(f"{season}: {frame.height:,} games without shift coverage, e.g. {examples}")
    with_xg = set(shot_xg["season"].unique().to_list())
    for season in seasons:
        if season >= xg.FIRST_SEASON and season not in with_xg:
            problems.append(f"{season}: no xG")
    return sorted(problems)
