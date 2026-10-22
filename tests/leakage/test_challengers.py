"""Leakage guard for the challengers (#255, ADR 0038): a challenger reads the lake only point in
time, through a reviewed reader for each table it registered, at its bundle's input cutoff; and
no decision day on or before its registration is ever computed. conservative_blend (ADR 0041)
trains only on earlier weeks' v2 decisions published, and results public, before the cutoff;
goalie_spread_blend (ADR 0042) adds only those decisions' spreads, each from its day's bundle;
team_residual_blend (ADR 0043) corrects each row's B3 by team effects from earlier weeks only."""

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

import polars as pl
import pytest

from nhl_edge.betting.selection import POLICY_V1, POLICY_V2
from nhl_edge.ingest.games import result_public_utc
from nhl_edge.live import bundle as lb
from nhl_edge.live import challengers as lc

CUTOFF = datetime(2026, 10, 16, 16, 45, 30, tzinfo=UTC)
SECOND = timedelta(seconds=1)
UTC_US = pl.Datetime("us", "UTC")


class Lake:
    """A lake of one table per name."""

    def __init__(self, **tables: pl.DataFrame) -> None:
        self.tables = tables

    def read(self, table: str, seasons: Any = None) -> pl.DataFrame:
        return self.tables[table]


def test_the_odds_are_read_as_they_were_before_the_cutoff() -> None:
    # Quotes observed just before, at and just after the cutoff, with the columns set
    # afterwards: the closing proxy flag and the game a quote was matched to (Codex on #265).
    odds = pl.DataFrame(
        {
            "event_id": ["e1", "e1", "e1"],
            "snapshot_utc": [CUTOFF - SECOND, CUTOFF, CUTOFF + SECOND],
            "price_decimal": [1.9, 1.95, 2.0],
            "is_closing_proxy": [True, False, False],
            "game_id": [2026020101] * 3,
            "game_type": [2] * 3,
        },
        schema_overrides={"snapshot_utc": UTC_US, "game_type": pl.Int8},
    )
    reader = lc.PointInTime(Lake(odds_snapshots=odds), CUTOFF, ("odds_snapshots",))
    read = reader.read("odds_snapshots")
    assert read["price_decimal"].to_list() == [1.9]
    assert not set(lc.ODDS_AFTERWARDS) & set(read.columns)
    assert list(reader.reads) == ["odds_snapshots"]


def test_the_schedule_holds_only_played_games_and_the_slates_own() -> None:
    # The schedule holds only games that went on to be played (Codex on #265): an upcoming game
    # outside the slate, public or not, would reveal whether it was postponed.
    day = CUTOFF.date()
    played, slate, upcoming = 2026020090, 2026020101, 2026020120
    schedule = pl.DataFrame(
        {
            "game_id": [played, slate, upcoming],
            "game_date": [day - timedelta(days=1), day, day + timedelta(days=1)],
            "observed_utc": [CUTOFF - timedelta(days=3)] * 3,
        },
        schema_overrides={"observed_utc": UTC_US},
    )
    assert result_public_utc(day - timedelta(days=1)).replace(tzinfo=UTC) < CUTOFF
    reader = lc.PointInTime(Lake(schedule=schedule), CUTOFF, ("schedule",), (slate,))
    assert sorted(reader.read("schedule")["game_id"].to_list()) == [played, slate]


def test_results_are_read_only_once_public() -> None:
    games = pl.DataFrame(
        {"game_id": [1, 2, 3], "observed_utc": [CUTOFF - SECOND, CUTOFF, CUTOFF + SECOND]},
        schema_overrides={"observed_utc": UTC_US},
    )
    reader = lc.PointInTime(Lake(games=games), CUTOFF, ("games",))
    assert reader.read("games")["game_id"].to_list() == [1]


def test_a_table_is_read_only_when_registered_and_reviewed() -> None:
    lake = Lake(games=pl.DataFrame(), goalie_starts=pl.DataFrame())
    with pytest.raises(ValueError, match="not among the tables"):
        lc.PointInTime(lake, CUTOFF, ()).read("games")
    # A table without a reviewed reader can't be read, nor registered.
    with pytest.raises(ValueError, match="no point-in-time reader"):
        lc.PointInTime(lake, CUTOFF, ("goalie_starts",)).read("goalie_starts")
    with pytest.raises(ValueError, match="no point-in-time reader for goalie_starts"):
        replace(lc.REGISTRY[0], reads=("goalie_starts",))
    for challenger in lc.REGISTRY:
        assert set(challenger.reads) <= set(lc.READERS)


def test_no_day_on_or_before_the_registration_is_ever_computed() -> None:
    challenger = replace(lc.REGISTRY[0], registered_on=date(2026, 10, 15))
    assert not challenger.counts(date(2026, 10, 14))
    assert not challenger.counts(date(2026, 10, 15))
    assert challenger.counts(date(2026, 10, 16))

    class Store:
        def keys(self, prefix: str) -> list[str]:
            if prefix != "bundles/live":
                return []
            days = ("2026-10-14", "2026-10-15", "2026-10-16")
            return [f"bundles/live/{day}/manifest.json" for day in days]

    assert lc.due(Store(), (challenger,)) == [(challenger, date(2026, 10, 16))]  # type: ignore[arg-type]

    on_the_day = lb.Bundle({"day": "2026-10-15"}, {}, None, None)
    with pytest.raises(ValueError, match="never its evidence"):
        lc.run(challenger, on_the_day, "x", None, Lake(), "x", CUTOFF + SECOND)  # type: ignore[arg-type]


def test_the_ledgers_are_read_only_as_published_before_the_cutoff() -> None:
    # Decisions published just before, at and just after the cutoff: a ledger is written once
    # and has no column set after its publication.
    ledger = pl.DataFrame(
        {
            "game_date": [CUTOFF.date()] * 3,
            "game_id": [3, 1, 2],
            "published_utc": [CUTOFF - SECOND, CUTOFF - 2 * SECOND, CUTOFF + SECOND],
        },
        schema_overrides={"published_utc": UTC_US},
    )
    reader = lc.PointInTime(Lake(paper_ledger=ledger), CUTOFF, ("paper_ledger",))
    assert reader.read("paper_ledger")["game_id"].to_list() == [1, 3]


def test_conservative_blend_trains_only_on_earlier_weeks_published_v2_decisions() -> None:
    # The cutoff is Friday 2026-10-16's; its ET week starts on Monday 10-12, and its fold at
    # 12:45 ET that day, when the decision window opens: at or before every cutoff of the week.
    day, monday = CUTOFF.date(), date(2026, 10, 12)
    assert lc.week_of(day) == monday
    fold = datetime(2026, 10, 12, 16, 45, tzinfo=UTC)
    assert lc.fold_start(day) == fold < CUTOFF

    def start(on: date) -> datetime:
        return datetime(on.year, on.month, on.day, 23, tzinfo=UTC)

    earlier = monday - timedelta(days=2)
    decisions = {
        # game_id: (decided for, its policy, its status, published)
        1: (earlier, POLICY_V2, "predicted", None),  # trained on
        2: (monday, POLICY_V2, "predicted", None),  # the day's own week
        3: (earlier, POLICY_V1, "predicted", None),  # v1's B3 read no confirmed starters
        4: (earlier, POLICY_V2, "stale", None),  # no prediction
        5: (earlier, POLICY_V2, "predicted", CUTOFF),  # published at the cutoff
        6: (earlier - timedelta(days=1), POLICY_V2, "predicted", None),  # postponed...
        7: (earlier, POLICY_V2, "predicted", None),  # its result public only later
        8: (earlier, POLICY_V2, "predicted", None),  # no result yet
        9: (earlier, POLICY_V2, "predicted", None),  # its result public during the week
    }
    rows = [
        {
            "game_date": on,
            "season": 20262027,
            "game_id": game_id,
            "start_utc": start(on),
            "home": "BOS",
            "away": "TOR",
            "published_utc": published or start(on) - timedelta(hours=6),
            "status": status,
            "policy_version": policy,
            "p_b0": 0.5,
            "p_b3": 0.55,
        }
        for game_id, (on, policy, status, published) in decisions.items()
    ]
    # ...and decided again on its new date, two days later: only the decision it was played on.
    rows.append({**rows[5], "game_date": earlier + timedelta(days=1)})
    rows[-1]["start_utc"] = start(earlier + timedelta(days=1))
    ledger = pl.DataFrame(rows, schema_overrides={"season": pl.Int32})
    # Every game but 8 has been played, 6 on its new date; 7's result is public only after the
    # cutoff, and 9's after the fold start but before the cutoff (Codex on #304).
    played = {row["game_id"]: row["start_utc"] for row in rows if row["game_id"] != 8}
    public = {game_id: at + timedelta(hours=11) for game_id, at in played.items()}
    public[7] = CUTOFF + SECOND
    public[9] = fold + SECOND
    games = pl.DataFrame(
        {
            "game_id": list(played),
            "season": [20262027] * len(played),
            "start_utc": list(played.values()),
            "home_score": [3] * len(played),
            "away_score": [2] * len(played),
            "observed_utc": [public[game_id] for game_id in played],
        },
        schema_overrides={"season": pl.Int32, "home_score": pl.Int16, "away_score": pl.Int16},
    )
    lake = lc.PointInTime(Lake(paper_ledger=ledger, games=games), CUTOFF, lc.REGISTRY[1].reads)
    trained = lc.conservative_training(day, [20262027], lake)
    assert trained.select("game_id", "game_date").rows() == [
        (1, earlier),
        (6, earlier + timedelta(days=1)),
    ]
    assert trained["home_win"].to_list() == [1, 1]
    # Monday's decision fits on the same games as Friday's: the fit is the week's.
    on_monday = fold + timedelta(seconds=30)
    lake = lc.PointInTime(Lake(paper_ledger=ledger, games=games), on_monday, lc.REGISTRY[1].reads)
    assert lc.conservative_training(monday, [20262027], lake).equals(trained)


def test_the_goalie_spreads_are_read_only_from_decisions_before_the_cutoff() -> None:
    # A spread is a function of its day's bundle, known at that day's input cutoff; when it was
    # computed, and by which code, is not part of what a challenger reads.
    spreads = pl.DataFrame(
        {
            "game_date": [CUTOFF.date()] * 3,
            "game_id": [2, 1, 3],
            "goalie_spread": [0.1, 0.2, 0.3],
            "input_cutoff_utc": [CUTOFF - SECOND, CUTOFF - 2 * SECOND, CUTOFF],
            "code_version": ["x"] * 3,
            "computed_utc": [CUTOFF + timedelta(hours=16)] * 3,
        },
        schema_overrides={"input_cutoff_utc": UTC_US, "computed_utc": UTC_US},
    )
    reader = lc.PointInTime(Lake(goalie_spreads=spreads), CUTOFF, ("goalie_spreads",))
    read = reader.read("goalie_spreads")
    assert read["game_id"].to_list() == [1, 2]
    assert not {"code_version", "computed_utc"} & set(read.columns)


def test_goalie_spread_blend_trains_on_conservative_blends_rows_and_their_spreads_only() -> None:
    # Two earlier-week v2 decisions with public results, and three that conservative_blend leaves
    # out: one of the day's own week, one with its result public only after the cutoff, and one
    # published after the fold start. Every one has a kept spread.
    day, fold = CUTOFF.date(), datetime(2026, 10, 12, 16, 45, tzinfo=UTC)
    earlier = date(2026, 10, 9)

    def start(on: date) -> datetime:
        return datetime(on.year, on.month, on.day, 23, tzinfo=UTC)

    decided = {1: earlier, 2: earlier, 3: date(2026, 10, 13), 4: earlier, 5: earlier}
    ledger = pl.DataFrame(
        {
            "game_date": list(decided.values()),
            "season": [20262027] * 5,
            "game_id": list(decided),
            "start_utc": [start(on) for on in decided.values()],
            "home": ["BOS"] * 5,
            "away": ["TOR"] * 5,
            "published_utc": [
                fold + SECOND if game_id == 5 else start(on) - timedelta(hours=6)
                for game_id, on in decided.items()
            ],
            "status": ["predicted"] * 5,
            "policy_version": [POLICY_V2] * 5,
            "p_b0": [0.5] * 5,
            "p_b3": [0.55] * 5,
        },
        schema_overrides={"season": pl.Int32},
    )
    games = pl.DataFrame(
        {
            "game_id": list(decided),
            "season": [20262027] * 5,
            "start_utc": [start(on) for on in decided.values()],
            "home_score": [3] * 5,
            "away_score": [2] * 5,
            "observed_utc": [
                CUTOFF + SECOND if game_id == 4 else start(on) + timedelta(hours=11)
                for game_id, on in decided.items()
            ],
        },
        schema_overrides={"season": pl.Int32, "home_score": pl.Int16, "away_score": pl.Int16},
    )
    spreads = ledger.select(
        "game_date",
        "season",
        "game_id",
        goalie_spread=pl.lit(0.1),
        input_cutoff_utc=pl.col("start_utc") - timedelta(hours=6, minutes=5),
        code_version=pl.lit("x"),
        computed_utc=pl.col("start_utc") + timedelta(hours=10),
    )
    challenger = lc.REGISTRY[2]
    lake = lc.PointInTime(
        Lake(paper_ledger=ledger, games=games, goalie_spreads=spreads), CUTOFF, challenger.reads
    )
    bundle = lb.Bundle({"day": day.isoformat()}, {}, None, None)
    replayed = pl.DataFrame(
        {"game_id": [9], "season": [20262027], "status": ["predicted"], "p_b0": 0.5, "p_b3": 0.6}
    )
    output = challenger.predict(bundle, replayed, None, lake)  # type: ignore[arg-type]
    fit = json.loads(output["fit"][0])
    assert (fit["games"], fit["without_spread"]) == (2, 0)


@pytest.fixture(scope="module")
def league_bundle() -> lb.Bundle:
    """A decision day's bundle from the synthetic B3 league (b3_fixtures): its first test date's
    four games, each team with two candidate goalies, B3 fitted before the season, and the
    decision at the games' start."""
    from b3_fixtures import league

    from nhl_edge.backtest.walk_forward import fold_start
    from nhl_edge.game import b3
    from nhl_edge.lake.schemas import Slate, dtypes

    tables = league()
    season = 20182019
    games = tables.games.filter(pl.col("season") == season)
    day = games["game_date"].min()
    slate = games.filter(pl.col("game_date") == day)
    cutoff = slate["start_utc"].min()
    moments = games.select("game_id", prediction_utc="start_utc")
    start = fold_start(tables.games.select("season", "start_utc"), season)
    _, model = b3.predictions(tables, moments, season, start)
    listed = slate.with_columns(
        game_state=pl.lit("FUT"), observed_utc=pl.lit(cutoff - timedelta(days=1))
    )
    inputs = {"slate": Slate.validate(listed.select(list(dtypes(Slate))).cast(dtypes(Slate)))}
    for name in (
        "schedule_terms",
        "goalie_starts",
        "actual_lineups",
        "lineups",
        "lineup_replacements",
        "player_ratings",
        "rapm_terms",
        "expected_power_plays",
        "goal_multipliers",
    ):
        inputs[name] = getattr(tables, name)
    manifest = {"day": day.isoformat(), "input_cutoff": cutoff.isoformat()}
    return lb.Bundle(manifest, inputs, None, model)


def test_a_slate_games_spread_reads_its_goalie_rows_only_once_known(
    league_bundle: lb.Bundle,
) -> None:
    # The slate's own spread comes from its bundle's goalie rows known before the input cutoff,
    # through B3's scenarios (lb.b3_scenarios): a confirmation that arrives at the cutoff changes
    # nothing, and a game whose rows all arrive then is as without them.
    before = lc.goalie_spreads(league_bundle)
    assert (before["goalie_spread"] > 0).all()
    starts = league_bundle.inputs["goalie_starts"]
    cutoff = datetime.fromisoformat(league_bundle.manifest["input_cutoff"])
    game = before["game_id"][0]
    first = starts.filter(pl.col("game_id") == game).sort("team", "goalie_id").head(1)
    confirmed = first.with_columns(p_start=pl.lit(1.0), observed_utc=pl.lit(cutoff, UTC_US))

    def spreads(rows: pl.DataFrame) -> pl.DataFrame:
        inputs = {**league_bundle.inputs, "goalie_starts": rows}
        return lc.goalie_spreads(replace(league_bundle, inputs=inputs))

    assert spreads(pl.concat([starts, confirmed])).equals(before)
    late = starts.with_columns(
        observed_utc=pl.when(pl.col("game_id") == game)
        .then(pl.lit(cutoff, UTC_US))
        .otherwise(pl.col("observed_utc"))
    )
    dropped = starts.filter(pl.col("game_id") != game)
    assert spreads(late).equals(spreads(dropped))
    # Without candidates, both goalies count as average: one scenario, no spread.
    assert spreads(dropped).filter(pl.col("game_id") == game)["goalie_spread"].item() == 0


def test_a_rows_team_correction_never_reads_its_own_weeks_results() -> None:
    # Decisions over three weeks before Friday 10-16's week, each BOS at TOR. A training row is
    # corrected by the effects of its own week's fold start, so flipping the results of the week
    # of 10-05 changes no correction of that week's rows, only those of later rows and the day's.
    weeks = (date(2026, 9, 28), date(2026, 10, 5))
    days = [week + timedelta(days=k) for week in weeks for k in range(5)]
    starts = [datetime(d.year, d.month, d.day, 23, tzinfo=UTC) for d in days]
    ledger = pl.DataFrame(
        {
            "game_date": days,
            "season": [20262027] * len(days),
            "game_id": list(range(1, len(days) + 1)),
            "start_utc": starts,
            "home": ["BOS"] * len(days),
            "away": ["TOR"] * len(days),
            "published_utc": [at - timedelta(hours=6) for at in starts],
            "status": ["predicted"] * len(days),
            "policy_version": [POLICY_V2] * len(days),
            "p_b0": [0.5] * len(days),
            "p_b3": [0.5] * len(days),
        },
        schema_overrides={"season": pl.Int32},
    )

    def games(home_score: list[int]) -> pl.DataFrame:
        return pl.DataFrame(
            {
                "game_id": list(range(1, len(days) + 1)),
                "season": [20262027] * len(days),
                "start_utc": starts,
                "home_score": home_score,
                "away_score": [2] * len(days),
                "observed_utc": [at + timedelta(hours=11) for at in starts],
            },
            schema_overrides={"season": pl.Int32, "home_score": pl.Int16, "away_score": pl.Int16},
        )

    day = CUTOFF.date()
    won = games([3] * len(days))
    lost = games([3] * 5 + [1] * 5)  # the week of 10-05 lost instead
    rows_won, effects_won = lc.corrected_training(ledger, won, day)
    rows_lost, effects_lost = lc.corrected_training(ledger, lost, day)
    in_week = pl.col("game_date") >= weeks[1]
    # The week of 10-05's own corrections come from the week before only.
    assert rows_won.filter(in_week)["p_b3"].equals(rows_lost.filter(in_week)["p_b3"])
    assert (rows_won.filter(in_week)["p_b3"] > 0.5).all()
    # The first week has no earlier games: uncorrected.
    assert rows_won.filter(~in_week)["p_b3"].to_list() == [0.5] * 5
    # The day's week's effects read every earlier week, so the flip moves them.
    assert effects_won["BOS"] > 0 > effects_lost["BOS"]
