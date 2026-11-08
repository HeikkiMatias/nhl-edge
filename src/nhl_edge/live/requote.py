"""Requote after a starter change (ADR 0059, #410): the model's value for a game with the starters
named by a moment, recomputed from the publication the game was quoted from, for the quoting
simulator's requote record. It reads no market but Pinnacle's pair the publication priced with.

**The moment.** A starter change observed at t is requoted at m = t + REQUOTE_AFTER, the time to
see the change and post again. Only polls observed before m count (hard rule 1).

**The publication:** the newest one published before m that predicted the game, the morning
view's or the decision's, with its bundle (live/bundle.py's shape).

**The starters** at m, by ADR 0037's rule (lineup/confirmed.py), the decision's own at its cutoff:
the NHL's flag, else Daily Faceoff's Confirmed, else its Likely. They are put in place in the
bundle's goalie rows (confirmed.adjusted). A goalie outside the candidates is rated from his
latest goalie effect before the publication's cutoff, or as one with no earlier game. The rows
keep their own times: the call's time is m, which the record keeps by using the value only from
m on.

**The value:** B2, B3 and u's parts through the bundle's frozen path (bundle.models), and the
blend at the publication's own Pinnacle pair (predict.predictions, hard rule 3). The guard: the
same path with no new call gives the published value to bundle.TOLERANCE, or the change is held.
A change with no value holds the game as wait does, counted by its reason.
"""

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import polars as pl

from nhl_edge.betting import selection
from nhl_edge.lineup import confirmed
from nhl_edge.live import blend_fit
from nhl_edge.live import bundle as lb
from nhl_edge.live import goalies as live_goalies
from nhl_edge.live import predict as lp

REQUOTE_AFTER = timedelta(minutes=1)
# The lake tables the starters at a moment are read from, beyond the simulator's own.
TABLES = ("players", "goalie_effects", "pregame_goalies")
# Each game's requoted values (home side) by when they are known.
Values = dict[int, list[tuple[datetime, float]]]
# The rows keep their own times (confirmed.adjusted takes the later of a row's and the call's).
EARLIEST = datetime(1970, 1, 1, tzinfo=UTC)
# Why a change got no value: the record counts each.
LATE, NO_PUBLICATION, NO_BUNDLE, OTHER_FIT, UNREPRODUCED, NO_VALUE, FAILED = (
    "late",
    "no_publication",
    "no_bundle",
    "other_fit",
    "unreproduced",
    "no_value",
    "failed",
)


class Scheduled(Protocol):
    """A game of the day: its id and start."""

    @property
    def game_id(self) -> int: ...

    @property
    def start_utc(self) -> datetime: ...


@dataclass(frozen=True)
class Publication:
    """A publication of the model's prediction: when, its rows (game_id, status, p_blend and the
    Pinnacle pair, home_price and away_price), and its bundle, None when it can't be read."""

    published_utc: datetime
    rows: pl.DataFrame
    bundle: lb.Bundle | None

    def row(self, game_id: int) -> dict[str, Any] | None:
        """The game's row when the publication predicted it."""
        found = self.rows.filter(
            pl.col("game_id") == game_id,
            pl.col("status") == lp.PREDICTED,
            pl.col("p_blend").is_not_null(),
        )
        return found.row(0, named=True) if found.height else None


@dataclass(frozen=True)
class Calls:
    """What the starters at a moment are read from: the day's NHL and Daily Faceoff polls, the
    players' names, and every goalie-effect row of the lake."""

    nhl: pl.DataFrame
    dailyfaceoff: pl.DataFrame
    players: pl.DataFrame
    goalie_effects: pl.DataFrame


def with_starters(bundle: lb.Bundle, game_id: int, moment: datetime, calls: Calls) -> lb.Bundle:
    """The bundle with the game's starters at moment (ADR 0037's rule, from the polls observed
    before it) in place in its goalie rows, the rows at their own times."""
    likely = selection.LIKELY_STARTS
    if likely is None:
        raise ValueError("the policy names no starters (ADR 0030)")
    t = bundle.inputs
    cutoff = datetime.fromisoformat(bundle.manifest["input_cutoff"])
    slate = t["slate"]
    game = slate.filter(pl.col("game_id") == game_id)
    called = confirmed.calls(live_goalies.team_games(game, moment), calls.nhl, calls.dailyfaceoff)
    names = calls.players.select(goalie_id="player_id", name="name")
    candidates = (
        t["goalie_starts"]
        .filter(pl.col("game_id") == game_id)
        .select("game_id", "team", "goalie_id", "p_start")
        .join(names, on="goalie_id", how="left")
    )
    rated = (
        calls.goalie_effects.filter(pl.col("observed_utc") < cutoff)
        .select("goalie_id")
        .unique()
        .join(names, on="goalie_id")
    )
    named = confirmed.starters(called, candidates, rated, likely).with_columns(
        known_utc=pl.lit(EARLIEST, dtype=pl.Datetime("us", "UTC"))
    )
    ids = slate["game_id"].implode()
    # A starter outside the candidates is rated from his latest row of any game before the cutoff.
    effects = pl.concat(
        [t["goalie_effects"], calls.goalie_effects.filter(~pl.col("game_id").is_in(ids))],
        how="vertical_relaxed",
    )
    starts, effects, multipliers = confirmed.adjusted(
        t["goalie_starts"], effects, t["goal_multipliers"], named, cutoff
    )
    return replace(
        bundle,
        inputs=dict(t)
        | {
            "goalie_starts": starts,
            "goalie_effects": effects.filter(pl.col("game_id").is_in(ids)),
            "goal_multipliers": multipliers,
        },
    )


def blend_of(bundle: lb.Bundle, row: Mapping[str, Any], live: blend_fit.LiveFit) -> float | None:
    """The game's blend from the bundle's models at the row's own Pinnacle pair."""
    fitted = lb.models(bundle)
    if fitted is None:
        return None
    priced = pl.DataFrame(
        {
            "game_id": [row["game_id"]],
            "home_price": [float(row["home_price"])],
            "away_price": [float(row["away_price"])],
        },
        schema={"game_id": pl.Int64, "home_price": pl.Float64, "away_price": pl.Float64},
    )
    found = lp.predictions(priced, fitted, live)
    if found.is_empty() or found["p_blend"][0] is None:
        return None
    return float(found["p_blend"][0])


def requotes(
    games: Sequence[Scheduled],
    changes: Mapping[int, Sequence[datetime]],
    publications: Sequence[Publication],
    live: blend_fit.LiveFit,
    calls: Calls,
    stop_before: timedelta,
) -> tuple[Values, dict[str, int]]:
    """Each game's requoted values (home side) at each starter change's moment m, and the day's
    counts: its changes, those requoted, and those held by reason. A change whose moment is at or
    after the game's stop, stop_before its start, is too late to quote again."""
    found: Values = {}
    counts: Counter[str] = Counter()
    ordered = sorted(publications, key=lambda p: p.published_utc)
    reproduced: dict[tuple[int, int], bool] = {}
    for game in games:
        for t in changes.get(game.game_id, []):
            counts["changes"] += 1
            moment = t + REQUOTE_AFTER
            if moment >= game.start_utc - stop_before:
                counts[LATE] += 1
                continue
            quoted = [
                (i, p, row)
                for i, p in enumerate(ordered)
                if p.published_utc < moment and (row := p.row(game.game_id)) is not None
            ]
            if not quoted:
                counts[NO_PUBLICATION] += 1
                continue
            i, publication, row = quoted[-1]
            bundle = publication.bundle
            if bundle is None:
                counts[NO_BUNDLE] += 1
                continue
            if bundle.manifest.get("blend_version") != live.version:
                counts[OTHER_FIT] += 1
                continue
            try:
                key = (i, game.game_id)
                if key not in reproduced:
                    again = blend_of(bundle, row, live)
                    reproduced[key] = again is not None and (
                        abs(again - float(row["p_blend"])) <= lb.TOLERANCE
                    )
                if not reproduced[key]:
                    counts[UNREPRODUCED] += 1
                    continue
                value = blend_of(with_starters(bundle, game.game_id, moment, calls), row, live)
            except (ValueError, KeyError, pl.exceptions.PolarsError):
                counts[FAILED] += 1
                continue
            if value is None:
                counts[NO_VALUE] += 1
                continue
            found.setdefault(game.game_id, []).append((moment, value))
            counts["requoted"] += 1
    return {k: sorted(v) for k, v in found.items()}, dict(counts)
