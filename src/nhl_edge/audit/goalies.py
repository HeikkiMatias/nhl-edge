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

from datetime import time

import polars as pl

from nhl_edge.backtest.metrics import Estimate, bootstrap
from nhl_edge.ingest.odds import ET
from nhl_edge.lineup import confirmed
from nhl_edge.lineup.confirmed import name_key as name_key

# A game whose last poll came earlier than this before the start is a problem: the polls exist to
# catch the confirmations of the last hour (docs/plan.md section 10). A reporting threshold only.
LAST_HOUR_MIN = 60
CONFIRMED = "Confirmed"
# Policy v2's decision (ADR 0037): 12:45 ET on the game date.
DECISION_ET = time(12, 45)

TEAM_GAME_SCHEMA = {
    "game_id": pl.Int64,
    "game_date": pl.Date,
    "team": pl.String,
    "opponent": pl.String,
    "start_utc": pl.Datetime("us", "UTC"),
    "starter_id": pl.Int64,
    "starter_name": pl.String,
    "surname_unique": pl.Boolean,
}


def _key(column: str) -> pl.Expr:
    return confirmed.key(column)


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
    # Whether no other goalie dressed by the team shares the starter's surname: only then does a
    # call by surname name him, as policy v2 matches the team's candidates (ADR 0037).
    names = players.select("player_id", "name")
    surnames = (
        lineups.filter(pl.col("role") == "G")
        .join(names, on="player_id", how="left")
        .group_by("game_id", "team", surname=confirmed.surname("name"))
        .agg(dressed=pl.len())
    )
    return (
        sides.join(started, on=["game_id", "team"])
        .join(names.rename({"name": "starter_name"}), left_on="starter_id", right_on="player_id")
        .with_columns(surname=confirmed.surname("starter_name"))
        .join(surnames, on=["game_id", "team", "surname"], how="left")
        .with_columns(surname_unique=pl.col("dressed").fill_null(1) == 1)
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
    is_confirmed = pl.col("status") == CONFIRMED
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
            dfo_reported_name=pl.col("goalie_name").filter(reported).first(),
            dfo_confirmed_min=pl.col("minutes").filter(is_confirmed).first(),
            dfo_confirmed_key=pl.col("key").filter(is_confirmed).first(),
            dfo_confirmed_name=pl.col("goalie_name").filter(is_confirmed).first(),
        )
    )
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
            # One goalie by name or, as v2 matches him, by a surname unique on the team.
            dfo_reported_right=called_right("dfo_reported_name"),
            dfo_confirmed_right=called_right("dfo_confirmed_name"),
            dfo_last_right=called_right("dfo_last_name"),
        )
        .with_columns(pl.col("nhl_polls", "dfo_polls").fill_null(0))
    )


def called_right(name: str) -> pl.Expr:
    """Whether the goalie Daily Faceoff named in the column started: the starter's name, or his
    surname when no other goalie the team dressed shares it, as policy v2 resolves a call among
    the team's candidates (ADR 0037; Codex on #239)."""
    by_name = confirmed.key(name) == confirmed.key("starter_name")
    by_surname = confirmed.surname(name) == confirmed.surname("starter_name")
    return by_name | (by_surname & pl.col("surname_unique"))


def decision_utc() -> pl.Expr:
    """DECISION_ET on each row's game date, in UTC."""
    at = pl.duration(hours=DECISION_ET.hour, minutes=DECISION_ET.minute)
    local = pl.col("game_date").cast(pl.Datetime("us")) + at
    return local.dt.replace_time_zone(ET.key).dt.convert_time_zone("UTC")


def midday_snapshots(odds: pl.DataFrame) -> pl.DataFrame:
    """Each ET date's midday odds snapshot (game_date, moment_utc): the decision's input cutoff,
    which the midday goalie poll precedes in the same dispatch."""
    midday = odds.filter(pl.col("slot") == "midday")
    return midday.group_by(
        game_date=pl.col("snapshot_utc").dt.convert_time_zone(ET.key).dt.date()
    ).agg(moment_utc=pl.col("snapshot_utc").min())


def at_decision(
    starters: pl.DataFrame,
    nhl: pl.DataFrame,
    dailyfaceoff: pl.DataFrame,
    decisions: pl.DataFrame | None = None,
) -> pl.DataFrame:
    """Each team-game with a known starter, from the first date either source was polled, that
    starts after policy v2's decision, with each source's call at its latest poll before then, as
    the decision reads them (confirmed.calls), and whether the called goalie started: nhl_right
    where the NHL flagged one, dfo_right where Daily Faceoff named one. The decision is the date's
    midday odds snapshot (decisions: game_date, moment_utc), else 12:45 ET. The season comes from
    the game id."""
    polled = pl.concat([nhl.select("game_date"), dailyfaceoff.select("game_date")])
    first = polled["game_date"].min()
    if first is None:
        return confirmed.calls(
            starters.clear().with_columns(moment_utc=decision_utc()), nhl, dailyfaceoff
        )
    moments = starters.filter(pl.col("game_date") >= first)
    if decisions is not None:
        moments = moments.join(
            decisions.select("game_date", midday="moment_utc"), on="game_date", how="left"
        )
    else:
        moments = moments.with_columns(midday=pl.lit(None, pl.Datetime("us", "UTC")))
    moments = (
        moments.with_columns(moment_utc=pl.coalesce("midday", decision_utc()))
        .drop("midday")
        .filter(pl.col("start_utc") > pl.col("moment_utc"))
    )
    start_year = pl.col("game_id") // 1_000_000
    return confirmed.calls(moments, nhl, dailyfaceoff).with_columns(
        season=(start_year * 10_000 + start_year + 1).cast(pl.Int32),
        nhl_right=pl.col("nhl_id") == pl.col("starter_id"),
        dfo_right=called_right("dfo_name"),
    )


def decision_share(frame: pl.DataFrame, call: pl.Expr, right: str) -> Estimate | None:
    """The share of the team-games with the call whose called goalie started, with its weekly
    block bootstrap interval (hard rule 7); None without any."""
    rows = frame.filter(call).with_columns(pl.col(right).cast(pl.Float64))
    return bootstrap(rows, right) if rows.height else None


def _share_row(label: str, share: Estimate | None) -> str:
    if share is None:
        return f"| {label} | 0 | - | - |"
    right = round(share.mean * share.games)
    return (
        f"| {label} | {share.games} | {right} | "
        f"{share.mean:.3f} [{share.low:.3f}, {share.high:.3f}] |"
    )


def decision_markdown(frame: pl.DataFrame) -> str:
    """The calls at 12:45 ET: how many team-games had each, and the share right, with intervals.
    Daily Faceoff's Likely share is ADR 0037's L, fixed at v2's freeze."""
    if frame.is_empty():
        return "No team-game with a known starter started after 12:45 ET since the polls began."
    status = pl.col("dfo_status")
    flagged = pl.col("nhl_id").is_not_null()
    called = status.is_in([confirmed.DFO_CONFIRMED, confirmed.DFO_LIKELY]).fill_null(False)
    neither = ~flagged & ~called
    lines = [
        f"**At policy v2's 12:45 ET decision (ADR 0037):** each source's call at its latest poll "
        "before the date's midday odds snapshot, the decision's input cutoff (12:45 ET on a date "
        f"without one), for the {frame.height} team-games starting after it. "
        "Right means the called goalie started. Shares carry 95% weekly block bootstrap "
        "intervals.",
        "",
        "| Call at 12:45 ET | Team-games | Right | Share right |",
        "| --- | --- | --- | --- |",
        _share_row("NHL starter flag", decision_share(frame, flagged, "nhl_right")),
        _share_row(
            "Daily Faceoff, Confirmed",
            decision_share(frame, status == confirmed.DFO_CONFIRMED, "dfo_right"),
        ),
        _share_row(
            "Daily Faceoff, Likely (ADR 0037's L)",
            decision_share(frame, status == confirmed.DFO_LIKELY, "dfo_right"),
        ),
        f"| No call from either | {frame.filter(neither).height} | - | - |",
    ]
    return "\n".join(lines)


def decision_problems(frame: pl.DataFrame) -> list[str]:
    """Every team-game whose call at 12:45 ET named a goalie who didn't start: a flag or a
    Confirmed that v2 would take as certain, or a Likely."""
    found = []
    for row in frame.iter_rows(named=True):
        who = f"{row['game_date']} {row['team']} ({row['game_id']})"
        if row["nhl_right"] is False:
            found.append(
                f"{who}: the NHL flagged {row['nhl_id']} at 12:45 ET, "
                f"{row['starter_name']} ({row['starter_id']}) started"
            )
        if row["dfo_right"] is False and row["dfo_status"] in (
            confirmed.DFO_CONFIRMED,
            confirmed.DFO_LIKELY,
        ):
            found.append(
                f"{who}: Daily Faceoff had {row['dfo_name']} {row['dfo_status']} at 12:45 ET, "
                f"{row['starter_name']} started"
            )
    return found


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
