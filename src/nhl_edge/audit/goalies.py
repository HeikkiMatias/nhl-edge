"""Starting goalies before the start (#42, #48, docs/plan.md section 10): how early and how often
right the pre-game goalie polls name each team's starter, against actual_lineups.

Two sources, polled together at the odds slots and hourly at :50 (docs/data-sources.md):
- pregame_goalies: the NHL gamecenter boxscore, whose starter flag names one goalie (starter_id).
- dailyfaceoff_goalies: Daily Faceoff's starting-goalies page, a goalie by name with a status,
  Likely or Confirmed, or none before any report.

For each team of each game with a known starter, from the first date either source was polled, the
report takes each source's first poll that names a goalie, how many minutes before the start it
was fetched, and whether that goalie started; and the same for the last poll before the start,
since a pick can change. Poll times count, not Daily Faceoff's own report times: a prediction can
use a report only once it has fetched it. Daily Faceoff names goalies, matched to the starter's
name in players without accents, case, punctuation or spaces.
"""

import re
import unicodedata

import polars as pl

# A game whose last poll came earlier than this before the start is a problem: the polls exist to
# catch the confirmations of the last hour (docs/plan.md section 10). A reporting threshold only.
LAST_HOUR_MIN = 60
CONFIRMED = "Confirmed"

TEAM_GAME_SCHEMA = {
    "game_id": pl.Int64,
    "game_date": pl.Date,
    "team": pl.String,
    "opponent": pl.String,
    "start_utc": pl.Datetime("us", "UTC"),
    "starter_id": pl.Int64,
    "starter_name": pl.String,
}


def name_key(name: str | None) -> str | None:
    """A goalie's name without accents, case, punctuation or spaces, for matching across sources:
    Ukko-Pekka Luukkonen and Ukko Pekka Luukkonen are one goalie."""
    if name is None:
        return None
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z]", "", ascii_name.lower())


def _key(column: str) -> pl.Expr:
    return pl.col(column).map_elements(name_key, return_dtype=pl.String)


def _minutes_before() -> pl.Expr:
    return (pl.col("start_utc") - pl.col("observed_utc")).dt.total_seconds() / 60


def starters(games: pl.DataFrame, lineups: pl.DataFrame, players: pl.DataFrame) -> pl.DataFrame:
    """One row per team of each game with a starting goalie in actual_lineups: the starter's id
    and name, the opponent and the scheduled start."""
    sides = pl.concat(
        [
            games.select(
                "game_id", "game_date", "start_utc", team=pl.col(team), opponent=pl.col(other)
            )
            for team, other in (("home", "away"), ("away", "home"))
        ]
    )
    started = lineups.filter(pl.col("starting_goalie")).select(
        "game_id", "team", starter_id="player_id"
    )
    return (
        sides.join(started, on=["game_id", "team"])
        .join(
            players.select("player_id", starter_name="name"),
            left_on="starter_id",
            right_on="player_id",
            how="left",
        )
        .select(list(TEAM_GAME_SCHEMA))
        .cast(TEAM_GAME_SCHEMA)  # type: ignore[arg-type]
        .sort("start_utc", "game_id", "team")
    )


def team_games(
    starters: pl.DataFrame, nhl: pl.DataFrame, dailyfaceoff: pl.DataFrame
) -> pl.DataFrame:
    """Each team-game with a known starter, from the first date either source was polled, with
    what each source said: minutes before the start of its last poll, of its first poll naming a
    goalie, and whether that goalie and the one at its last poll started. Daily Faceoff's first
    report counts any status, Likely or Confirmed, and its first confirmation only Confirmed."""
    polled = pl.concat([nhl.select("game_date"), dailyfaceoff.select("game_date")])
    first = polled["game_date"].min()
    if first is None:
        return starters.clear()
    by_time = pl.col("observed_utc")
    nhl_side = (
        nhl.with_columns(minutes=_minutes_before())
        .sort(by_time)
        .group_by("game_id", "team")
        .agg(
            nhl_polls=pl.len(),
            nhl_last_min=pl.col("minutes").last(),
            nhl_last_id=pl.col("starter_id").last(),
            nhl_named_min=pl.col("minutes").filter(pl.col("starter_id").is_not_null()).first(),
            nhl_named_id=pl.col("starter_id").drop_nulls().first(),
        )
    )
    reported = pl.col("status").is_not_null()
    confirmed = pl.col("status") == CONFIRMED
    # A Daily Faceoff listing is a game by its start and team. One page can list a team twice on
    # a date, such as a stale listing of a re-timed game, so each listing keeps its own polls.
    dfo_side = (
        dailyfaceoff.with_columns(minutes=_minutes_before(), key=_key("goalie_name"))
        .sort(by_time)
        .group_by("game_date", "team", dfo_start_utc=pl.col("start_utc"))
        .agg(
            dfo_polls=pl.len(),
            dfo_last_min=pl.col("minutes").last(),
            dfo_last_key=pl.col("key").last(),
            dfo_last_name=pl.col("goalie_name").last(),
            dfo_reported_min=pl.col("minutes").filter(reported).first(),
            dfo_reported_key=pl.col("key").filter(reported).first(),
            dfo_confirmed_min=pl.col("minutes").filter(confirmed).first(),
            dfo_confirmed_key=pl.col("key").filter(confirmed).first(),
        )
    )
    starter = pl.col("starter_key")
    return (
        starters.filter(pl.col("game_date") >= first)
        .with_columns(starter_key=_key("starter_name"))
        .join(nhl_side, on=["game_id", "team"], how="left")
        # Each team-game takes the team's listing that date whose start is nearest its own.
        .join(dfo_side, on=["game_date", "team"], how="left")
        .with_columns(dfo_gap=(pl.col("dfo_start_utc") - pl.col("start_utc")).abs())
        .sort("dfo_gap", nulls_last=True)
        .unique(["game_id", "team"], keep="first", maintain_order=True)
        .drop("dfo_start_utc", "dfo_gap")
        .sort("start_utc", "game_id", "team")
        .with_columns(
            nhl_named_right=pl.col("nhl_named_id") == pl.col("starter_id"),
            nhl_last_right=pl.col("nhl_last_id") == pl.col("starter_id"),
            dfo_reported_right=pl.col("dfo_reported_key") == starter,
            dfo_confirmed_right=pl.col("dfo_confirmed_key") == starter,
            dfo_last_right=pl.col("dfo_last_key") == starter,
        )
        .with_columns(pl.col("nhl_polls", "dfo_polls").fill_null(0))
    )


def _source_row(frame: pl.DataFrame, label: str, named: str, right: str, last: str | None) -> str:
    total = frame.height
    named_rows = frame.filter(pl.col(named).is_not_null())
    lead = named_rows[named].median()
    lead_text = f"{lead:.0f}" if isinstance(lead, float | int) else "-"
    first_right = int(named_rows[right].sum())
    last_text = "-"
    if last is not None:
        last_rows = frame.filter(pl.col(last).is_not_null())
        last_text = f"{int(last_rows[last].sum())} of {last_rows.height}"
    return (
        f"| {label} | {named_rows.height} of {total} | {lead_text} | "
        f"{first_right} of {named_rows.height} | {last_text} |"
    )


def markdown_report(frame: pl.DataFrame) -> str:
    if frame.is_empty():
        return "No team-game with a known starter since the goalie polls began."
    games = frame.select("game_id").n_unique()
    polled = frame.filter(pl.min_horizontal("nhl_last_min", "dfo_last_min") <= LAST_HOUR_MIN)
    lines = [
        f"Team-games with a known starter from {frame['game_date'].min()} to "
        f"{frame['game_date'].max()}: {frame.height}, in {games} games. Games with a poll in the "
        f"last {LAST_HOUR_MIN} minutes before the start: {polled.select('game_id').n_unique()} "
        f"of {games}.",
        "",
        "| Source | Named a goalie | Median minutes before the start, first named | "
        "Right when first named | Right at the last poll |",
        "| --- | --- | --- | --- | --- |",
        _source_row(
            frame, "NHL starter flag", "nhl_named_min", "nhl_named_right", "nhl_last_right"
        ),
        _source_row(
            frame,
            "Daily Faceoff, Likely or Confirmed",
            "dfo_reported_min",
            "dfo_reported_right",
            "dfo_last_right",
        ),
        _source_row(
            frame,
            "Daily Faceoff, Confirmed",
            "dfo_confirmed_min",
            "dfo_confirmed_right",
            None,  # the last poll is the row above's
        ),
    ]
    if frame["nhl_named_min"].is_null().all():
        lines += [
            "",
            "The NHL's pre-game data named no starter before any start, so it does not confirm "
            "starters in time to use. Until it does, the goalie-start model (phase 2) is the "
            "only NHL-side source.",
        ]
    return "\n".join(lines)


def problems(frame: pl.DataFrame) -> list[str]:
    """Games whose last poll came more than LAST_HOUR_MIN before the start, or never, and every
    team-game where a source's last pick before the start was not the starter."""
    if frame.is_empty():
        return []
    found = []
    last = frame.group_by("game_id", "game_date", "start_utc").agg(
        pl.min_horizontal("nhl_last_min", "dfo_last_min").min().alias("last_min"),
        pl.col("team").sort().str.join(" and ").alias("teams"),
    )
    for row in last.sort("start_utc", "game_id").iter_rows(named=True):
        if row["last_min"] is None:
            found.append(f"{row['game_date']} {row['teams']} ({row['game_id']}): no goalie poll")
        elif row["last_min"] > LAST_HOUR_MIN:
            found.append(
                f"{row['game_date']} {row['teams']} ({row['game_id']}): last goalie poll "
                f"{row['last_min']:.0f} minutes before the start"
            )
    for row in frame.iter_rows(named=True):
        who = f"{row['game_date']} {row['team']} ({row['game_id']})"
        if row["nhl_last_right"] is False:
            found.append(
                f"{who}: the NHL's last pre-game starter was {row['nhl_last_id']}, "
                f"{row['starter_name']} ({row['starter_id']}) started"
            )
        if row["dfo_last_right"] is False:
            found.append(
                f"{who}: Daily Faceoff's last pick was {row['dfo_last_name']}, "
                f"{row['starter_name']} started"
            )
    return found
