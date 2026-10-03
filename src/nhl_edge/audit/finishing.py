"""Finishing and goalie conversion's report (#105, ADR 0022): what each season's φ and gamma rest
on, and how well they turn a team's xG into its goals.

Per season, the team-games and candidates. For a shown season also:
- the pulls per role, and the league's finishing and xG per shot at the season's last game;
- the spread of the teams' φ and the goalies' gamma;
- the squared error of each team-game's goals on shots with xG, from its actual xG times the
  league's finishing (the reference), times the team's φ, times the opposing starter's gamma, and
  times both, each against the reference with its weekly block bootstrap interval;
- the forwards finishing furthest above and below the league, at each one's last game, a
  face-validity check.
The development and held-out seasons show only their counts until gate 2, and so does any season
after one of them, since its figures read every earlier season's games.
"""

from collections.abc import Collection, Mapping

import polars as pl

from nhl_edge.backtest import metrics
from nhl_edge.ratings.finishing import HALF_LIFE_DAYS, SeasonPulls
from nhl_edge.ratings.rapm import FIRST_SEASON

LEADER_EXPECTED_GOALS = 15.0
LEADERS = 5
# The predictions scored against the reference, with the multiplier each applies.
VARIANTS = {"phi": pl.col("phi"), "gamma": pl.col("gamma"), "both": pl.col("phi") * pl.col("gamma")}
TITLES = {"phi": "phi", "gamma": "gamma", "both": "phi times gamma"}


def team_goals(shots: pl.DataFrame, shot_xg: pl.DataFrame) -> pl.DataFrame:
    """Each team-game's xG and goals on its unblocked shots with xG."""
    return (
        shot_xg.select("game_id", "event_id", "xg")
        .join(shots.select("game_id", "event_id", "team", "is_goal"), on=["game_id", "event_id"])
        .group_by("game_id", "team")
        .agg(xg=pl.col("xg").sum(), goals=pl.col("is_goal").sum().cast(pl.Float64))
    )


def scored(multipliers: pl.DataFrame, goals: pl.DataFrame, boxscores: pl.DataFrame) -> pl.DataFrame:
    """Each team-game whose opposing starter (boxscores' starting_goalie) was a candidate: its
    goals, the reference's prediction (xG times league_finishing) and each variant's, and each
    variant's squared error less the reference's (difference_<variant>). The game's own xG and
    starter are read only to score."""
    starters = boxscores.filter(pl.col("starting_goalie")).select(
        "game_id", opponent="team", goalie_id="player_id"
    )
    frame = (
        multipliers.join(starters, on=["game_id", "opponent", "goalie_id"], how="inner")
        .join(goals, on=["game_id", "team"], how="inner")
        .filter(pl.col("league_finishing").is_not_null())
        .with_columns(reference=pl.col("xg") * pl.col("league_finishing"))
        .with_columns(error=(pl.col("reference") - pl.col("goals")) ** 2)
    )
    return frame.with_columns(
        **{
            f"difference_{name}": (pl.col("reference") * factor - pl.col("goals")) ** 2
            - pl.col("error")
            for name, factor in VARIANTS.items()
        }
    ).sort("game_id", "team")


def leaders(finishing: pl.DataFrame, players: pl.DataFrame, season: int) -> pl.DataFrame:
    """Each forward's φ at his last game of the season, with LEADER_EXPECTED_GOALS decayed
    expected goals or more, highest first."""
    own = finishing.filter(pl.col("season") == season, pl.col("role") == "F")
    last = own.filter(pl.col("as_of_utc") == pl.col("as_of_utc").max().over("player_id"))
    return (
        last.filter(pl.col("expected_goals") >= LEADER_EXPECTED_GOALS)
        .unique("player_id", keep="first")
        .join(players.select("player_id", "name"), on="player_id", how="left")
        .sort("phi", "player_id", descending=[True, False])
    )


def markdown_report(
    finishing: pl.DataFrame,
    multipliers: pl.DataFrame,
    scores: pl.DataFrame,
    players: pl.DataFrame,
    pulls: Mapping[int, SeasonPulls],
    shown: Collection[int],
    version: str,
) -> str:
    lines = [
        f"# Finishing and goalie conversion: {version}",
        "",
        f"φ and the xG rates decay with RAPM's frozen memory, a half-life of {HALF_LIFE_DAYS:g}",
        "league game days (#103), and are pulled toward 1 and their role's rate by amounts",
        "measured on every earlier season (ADR 0022). gamma is 1 less the goalie effect per shot",
        "over the league's xG per shot. Held-out seasons, the development seasons among them",
        "until gate 2, show only their counts.",
        "",
        "## Per season",
        "",
        "Spreads are the 5th to 95th percentile over team-games (φ) and over candidate goalies",
        "(gamma).",
        "",
        "| Season | Team-games | Candidates | Team φ | Goalie gamma |",
        "| --- | ---: | ---: | --- | --- |",
    ]
    seasons = sorted(multipliers["season"].unique().to_list())
    whole = {s for s in seasons if _reads_only(s, shown)}
    candidates = dict(finishing.group_by("season").agg(pl.col("player_id").n_unique()).iter_rows())
    for season in seasons:
        own = multipliers.filter(pl.col("season") == season)
        teams = own.unique(["game_id", "team"])
        counted = f"{teams.height:,} | {candidates.get(season, 0):,}"
        if season in whole:
            cells = f"{_spread(teams['phi'])} | {_spread(own['gamma'])}"
        else:
            cells = "held out | "
        lines.append(f"| {season} | {counted} | {cells} |")
    shown_seasons = [s for s in seasons if s in whole]
    lines += [
        "",
        "## Pulls and league figures",
        "",
        "Finishing's pull in expected goals and the xG rate's in hours of ice time, measured on",
        "every earlier season's skaters with 20 games or more (∞: no earlier season, or no",
        "spread beyond noise). The league's finishing (goals over xG) and xG per unblocked shot",
        "are those at the season's last as-of time.",
        "",
        "| Season | F finishing | D finishing | F xG rate | D xG rate | Finishing | xG per shot |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for season in shown_seasons:
        p = pulls[season]
        cells = [_pull(p.finishing[r]) for r in ("F", "D")] + [_pull(p.rate[r]) for r in ("F", "D")]
        last = (
            multipliers.filter(pl.col("season") == season)
            .sort("as_of_utc")
            .tail(1)
            .row(0, named=True)
        )
        cells += [_number(last["league_finishing"], 3), _number(last["xg_per_shot"], 4)]
        lines.append(f"| {season} | " + " | ".join(cells) + " |")
    lines += [
        "",
        "## Goals against the league's finishing",
        "",
        "Mean squared error of each team-game's goals on shots with xG. The reference predicts",
        "its actual xG times the league's finishing; each variant multiplies that by the team's",
        "φ, the opposing starter's gamma, or both. Differences are the variant's error less the",
        "reference's, paired by team-game, with 95% weekly block bootstrap intervals: below 0 is",
        "better. Only",
        "team-games whose starter was a candidate goalie count. The training seasons are",
        "in-sample for RAPM's memory and the goalie effect's settings, tuned on them (ADR 0011):",
        "this compares the multipliers, and is not out-of-sample evidence.",
        "",
        "| Season | Team-games | Reference | " + " | ".join(TITLES.values()) + " |",
        "| --- | ---: | ---: |" + " --- |" * len(TITLES),
    ]
    for season in shown_seasons:
        lines.append(_score_line(str(season), scores.filter(pl.col("season") == season)))
    pooled = scores.filter(pl.col("season").is_in(shown_seasons))
    if len(shown_seasons) > 1 and pooled.height:
        lines.append(_score_line("Pooled", pooled))
    lines += [
        "",
        "## Forwards' finishing at their last game of the season",
        "",
        f"Forwards with at least {LEADER_EXPECTED_GOALS:g} decayed expected goals, the five",
        "furthest above the league and the five furthest below, a face-validity check.",
        "",
        "| Season | Rank | Player | Team | φ | Goals | Expected goals |",
        "| --- | --- | --- | --- | ---: | ---: | ---: |",
    ]
    for season in shown_seasons:
        table = leaders(finishing, players, season)
        if table.is_empty():
            continue
        picks = [("top", i, r) for i, r in enumerate(table.head(LEADERS).iter_rows(named=True))]
        bottom = table.tail(LEADERS).reverse().iter_rows(named=True)
        picks += [("bottom", i, r) for i, r in enumerate(bottom)]
        for end, i, row in picks:
            lines.append(
                f"| {season} | {end} {i + 1} | {row['name'] or row['player_id']} | {row['team']} "
                f"| {row['phi']:.3f} | {row['goals']:.1f} | {row['expected_goals']:.1f} |"
            )
    return "\n".join(lines) + "\n"


def _spread(values: pl.Series) -> str:
    low, high = values.quantile(0.05), values.quantile(0.95)
    return "" if low is None or high is None else f"{low:.3f} to {high:.3f}"


def _pull(value: float) -> str:
    return "∞" if value == float("inf") else f"{value:.1f}"


def _number(value: float | None, digits: int) -> str:
    return "" if value is None else f"{value:.{digits}f}"


def _score_line(label: str, frame: pl.DataFrame) -> str:
    if frame.is_empty():
        return f"| {label} | 0 | |" + " |" * len(TITLES)
    cells = []
    for name in VARIANTS:
        difference = metrics.bootstrap(frame, f"difference_{name}")
        cells.append(f"{difference.mean:+.4f} [{difference.low:+.4f}, {difference.high:+.4f}]")
    return (
        f"| {label} | {frame.height:,} | {frame['error'].mean():.4f} | " + " | ".join(cells) + " |"
    )


def _reads_only(season: int, shown: Collection[int]) -> bool:
    """Whether the season and every season from FIRST_SEASON before it are shown."""
    return all(s in shown for s in range(FIRST_SEASON, season + 1, 10_001))
