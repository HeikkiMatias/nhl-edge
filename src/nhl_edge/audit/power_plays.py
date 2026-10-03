"""Expected power plays' report (#104, ADR 0021): what each season's penalty rates and expected
power plays rest on, and how well the power-play minutes are predicted.

Per season, the team-games and candidates. For a shown season also:
- the pulls in hours per role and component, and the league's figures at the season's last game;
- the squared error of each team-game's power-play minutes, against B2's team-level estimate
  (team strength's, with its frozen settings), with the paired difference's weekly block
  bootstrap interval;
- the five skaters taking and drawing the most penalties per hour, at each one's last game, a
  face-validity check.
The development and held-out seasons show only their counts until gate 2, and so does any season
after one of them, since its rates read every earlier season's games.
"""

from collections.abc import Collection, Mapping

import polars as pl

from nhl_edge.backtest import metrics
from nhl_edge.ratings.penalty_rates import COMPONENTS, DRAWN, HALF_LIFE_DAYS, TAKEN, SeasonPulls
from nhl_edge.ratings.rapm import FIRST_SEASON

LEADER_HOURS = 20.0
LEADERS = 5
TITLES = {TAKEN: "Taken", DRAWN: "Drawn"}


def scored(expected: pl.DataFrame, actual: pl.DataFrame, reference: pl.DataFrame) -> pl.DataFrame:
    """Each team-game with actual power-play minutes (team_games' pp_minutes) and an estimate
    from B2 (reference, team_strength.power_play_minutes), which has none before the first games
    with xG are public: the squared error of the expected minutes, of B2's, and their
    difference."""
    keys = ["game_id", "team"]
    return (
        expected.select("game_id", "season", "game_date", "team", "pp_minutes")
        .join(actual.select(*keys, actual="pp_minutes"), on=keys, how="inner")
        .join(reference.select(*keys, b2="pp_minutes"), on=keys, how="inner")
        .filter(pl.col("b2").is_finite())
        .with_columns(
            error=(pl.col("pp_minutes") - pl.col("actual")) ** 2,
            error_b2=(pl.col("b2") - pl.col("actual")) ** 2,
        )
        .with_columns(difference=pl.col("error") - pl.col("error_b2"))
        .sort("game_id", "team")
    )


def leaders(
    rates: pl.DataFrame, players: pl.DataFrame, season: int, component: str
) -> pl.DataFrame:
    """Each skater's rate at his last game of the season, with LEADER_HOURS decayed hours or
    more, highest first."""
    own = rates.filter(pl.col("season") == season, pl.col("component") == component)
    last = own.filter(pl.col("as_of_utc") == pl.col("as_of_utc").max().over("player_id"))
    return (
        last.filter(pl.col("hours") >= LEADER_HOURS)
        .unique("player_id", keep="first")
        .join(players.select("player_id", "name"), on="player_id", how="left")
        .sort("mean", "player_id", descending=[True, False])
    )


def markdown_report(
    rates: pl.DataFrame,
    expected: pl.DataFrame,
    scores: pl.DataFrame,
    players: pl.DataFrame,
    pulls: Mapping[int, SeasonPulls],
    shown: Collection[int],
    version: str,
) -> str:
    lines = [
        f"# Expected power plays: {version}",
        "",
        f"Penalty rates decay with RAPM's frozen memory, a half-life of {HALF_LIFE_DAYS:g} league",
        "game days (#103), and are pulled toward their role's rate by an amount measured on the",
        "season before (ADR 0021). Held-out seasons, the development seasons among them until",
        "gate 2, show only their counts.",
        "",
        "## Per season",
        "",
        "Opportunities and minutes are per team-game, expected against actual, over the",
        "team-games with power-play time measured.",
        "",
        "| Season | Team-games | Candidates | Expected opportunities | Expected PP minutes "
        "| Actual PP minutes |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    seasons = sorted(expected["season"].unique().to_list())
    whole = {s for s in seasons if _reads_only(s, shown)}
    candidates = rates.group_by("season").agg(pl.col("player_id").n_unique())
    per_season = dict(candidates.iter_rows())
    for season in seasons:
        own = expected.filter(pl.col("season") == season)
        counted = f"{own.height:,} | {per_season.get(season, 0):,}"
        if season in whole:
            actual = scores.filter(pl.col("season") == season)["actual"].mean()
            cells = f"{own['opportunities'].mean():.2f} | {own['pp_minutes'].mean():.2f} | " + (
                "" if actual is None else f"{actual:.2f}"
            )
        else:
            cells = "held out | | "
        lines.append(f"| {season} | {counted} | {cells} |")
    shown_seasons = [s for s in seasons if s in whole]
    lines += [
        "",
        "## Pulls and league figures",
        "",
        "Pulls in hours of ice time, from the season before's skaters with 20 games or more. The",
        "league's figures are those at the season's last as-of time: unoffset penalties per",
        "team-game (L), power-play minutes per unoffset penalty (length) and shorthanded xG per",
        "penalty-kill minute (s).",
        "",
        "| Season | F taken | F drawn | D taken | D drawn | L | Length | s |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for season in shown_seasons:
        pull = pulls[season].pull
        cells = [f"{pull[role, c]:.1f}" for role in ("F", "D") for c in COMPONENTS]
        last = (
            expected.filter(pl.col("season") == season).sort("as_of_utc").tail(1).row(0, named=True)
        )
        s = last["sh_xg_per_pk_minute"]
        cells += [
            f"{last['league_opportunities']:.3f}",
            f"{last['pp_length']:.3f}",
            "" if s is None else f"{s:.4f}",
        ]
        lines.append(f"| {season} | " + " | ".join(cells) + " |")
    lines += [
        "",
        "## Power-play minutes against B2's",
        "",
        "Mean squared error of each team-game's power-play minutes, expected against actual; B2's",
        "estimate is team strength's, with its frozen settings. The difference is ours less B2's,",
        "paired by team-game, with its 95% weekly block bootstrap interval: below 0 is better.",
        "Team-games before B2 has any game with xG to rate from (2011-12's first) are left out.",
        "",
        "| Season | Team-games | Error | B2's error | Difference |",
        "| --- | ---: | ---: | ---: | --- |",
    ]
    for season in shown_seasons:
        lines.append(_score_line(str(season), scores.filter(pl.col("season") == season)))
    pooled = scores.filter(pl.col("season").is_in(shown_seasons))
    if len(shown_seasons) > 1 and pooled.height:
        lines.append(_score_line("Pooled", pooled))
    lines += [
        "",
        "## Most penalties per hour at each skater's last game of the season",
        "",
        f"Skaters with at least {LEADER_HOURS:g} decayed hours, a face-validity check.",
        "",
        "| Season | Component | Rank | Player | Team | Role | Per hour | Hours |",
        "| --- | --- | --- | --- | --- | --- | ---: | ---: |",
    ]
    for season in shown_seasons:
        for component in COMPONENTS:
            table = leaders(rates, players, season, component).head(LEADERS)
            for i, row in enumerate(table.iter_rows(named=True)):
                lines.append(
                    f"| {season} | {TITLES[component]} | {i + 1} | "
                    f"{row['name'] or row['player_id']} | {row['team']} | {row['role']} | "
                    f"{row['mean']:.3f} | {row['hours']:.1f} |"
                )
    return "\n".join(lines) + "\n"


def _score_line(label: str, frame: pl.DataFrame) -> str:
    if frame.is_empty():
        return f"| {label} | 0 | | | |"
    difference = metrics.bootstrap(frame, "difference")
    return (
        f"| {label} | {frame.height:,} | {frame['error'].mean():.3f} | "
        f"{frame['error_b2'].mean():.3f} | {difference.mean:+.3f} "
        f"[{difference.low:+.3f}, {difference.high:+.3f}] |"
    )


def _reads_only(season: int, shown: Collection[int]) -> bool:
    """Whether the season and every season from FIRST_SEASON before it are shown."""
    return all(s in shown for s in range(FIRST_SEASON, season + 1, 10_001))
