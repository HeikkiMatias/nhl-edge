"""The corrections replay (#173): the corrected version replaces only the rechecked games' feed
rows, a correction moves only what reads it, and the replay never writes the repository's lake or
one mirrored to R2."""

import json
import sys
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl
import pytest
from feed_fixtures import OPENING_WEEK_GAMES, feed, games_row
from test_bundle import B2_TABLES, B3_TABLES, U_TABLES, fixture_slate, model_bundle

from nhl_edge.audit import corrections_replay as cr
from nhl_edge.ingest.corrections import ADDED, REMOVED, parse_copies
from nhl_edge.ingest.nhl_api import FEED_KINDS, RECHECK_SUFFIX
from nhl_edge.lake.raw import RawStore
from nhl_edge.lake.tables import FEED_TABLES, Lake
from nhl_edge.live import bundle as lb
from nhl_edge.live import predict as lp

FIRST = datetime(2010, 10, 9, 9, 0, tzinfo=UTC)
LATER = FIRST + timedelta(days=7, hours=1)
GAME = OPENING_WEEK_GAMES[0]
TABLES = (B2_TABLES, B3_TABLES, U_TABLES)


def games() -> pl.DataFrame:
    return pl.DataFrame([games_row(game_id) for game_id in OPENING_WEEK_GAMES])


def credit_first_goal_to(player_id: int) -> Any:
    def edit(body: bytes) -> bytes:
        data = json.loads(body)
        goal = next(play for play in data["plays"] if play["typeDescKey"] == "goal")
        goal["details"]["scoringPlayerId"] = player_id
        return json.dumps(data).encode()

    return edit


def stored_copies(tmp_path: Path, edits: dict[int, dict[str, Any]] | None = None) -> RawStore:
    """Each opening-week game's first copies and, a week later, its rechecks, rewritten by
    edits[game][kind] where given; the last game has no recheck."""
    store = RawStore(tmp_path / "raw")
    for game in games().iter_rows(named=True):
        entity = f"{game['season']}/{game['game_id']}"
        for kind in FEED_KINDS:
            body = feed(kind, game["game_id"])
            meta = {"fetched_utc": FIRST.isoformat()}
            store.put("nhl", f"{kind}/{entity}/{FIRST:%Y%m%dT%H%M%SZ}", body, meta)
            if game["game_id"] == OPENING_WEEK_GAMES[-1]:
                continue
            edit = (edits or {}).get(game["game_id"], {}).get(kind)
            later = edit(body) if edit else body
            meta = {"fetched_utc": LATER.isoformat()}
            key = f"{kind}{RECHECK_SUFFIX}/{entity}/{LATER:%Y%m%dT%H%M%SZ}"
            store.put("nhl", key, later, meta)
    return store


def ingested(store: RawStore, lake: Lake) -> dict[str, pl.DataFrame]:
    """The lake's feed tables from each game's first copies, as nhl ingest writes them."""
    parsed: dict[str, list[pl.DataFrame]] = {table: [] for table in FEED_TABLES}
    for game in games().iter_rows(named=True):
        key = f"nhl/{{}}/{game['season']}/{game['game_id']}/{FIRST:%Y%m%dT%H%M%SZ}"
        from nhl_edge.ingest.nhl_ingest import parse_feeds

        feeds = {kind: (store.get(key.format(kind)), key.format(kind)) for kind in FEED_KINDS}
        for table, frame in parse_feeds(game, feeds).items():
            parsed[table].append(frame)
    dates = games()["game_date"].unique().to_list()
    for table, frames in parsed.items():
        lake.replace_dates(table, pl.concat(frames), dates)
    return {table: lake.read(table) for table in FEED_TABLES}


def test_an_unchanged_recheck_swaps_in_the_same_rows(tmp_path: Path) -> None:
    store = stored_copies(tmp_path)
    lake = Lake(tmp_path / "scratch" / "lake")
    before = ingested(store, lake)
    corrected = cr.corrected_feeds(store, games(), before)
    # The last game has no recheck, so it isn't part of the corrected version.
    assert corrected.games == list(OPENING_WEEK_GAMES[:2])
    assert corrected.unmatched == [] and corrected.charts_kept == []
    written = cr.swap_in(lake, corrected, tmp_path / "repository" / "lake")
    assert written["shots"] > 0
    for table in FEED_TABLES:
        after = lake.read(table)
        # Only the raw key, naming the recheck's copy, differs.
        assert (
            cr.table_changes(before[table].drop("raw_key"), after.drop("raw_key"), table)[
                "rows"
            ].to_list()
            == []
        ), table
        moved = after.filter(pl.col("game_id") == GAME)["raw_key"].unique().to_list()
        assert all(RECHECK_SUFFIX in key for key in moved), table


def test_a_corrected_scorer_changes_that_one_value_and_nothing_else(tmp_path: Path) -> None:
    store = stored_copies(tmp_path, {GAME: {"play-by-play": credit_first_goal_to(8_000_001)}})
    lake = Lake(tmp_path / "scratch" / "lake")
    before = ingested(store, lake)
    cr.swap_in(lake, cr.corrected_feeds(store, games(), before), tmp_path / "repository")
    found = []
    for table in FEED_TABLES:
        after = lake.read(table)
        changes = cr.table_changes(before[table].drop("raw_key"), after.drop("raw_key"), table)
        found += changes.select("table", "column", "rows", "games").rows()
    assert found == [("shots", cr.CHANGED, 1, 1), ("shots", "shooter_id", 1, 1)]


def test_a_game_whose_first_copies_are_not_the_lakes_rows_is_left_out(tmp_path: Path) -> None:
    store = stored_copies(tmp_path, {GAME: {"play-by-play": credit_first_goal_to(8_000_001)}})
    lake = Lake(tmp_path / "scratch" / "lake")
    before = ingested(store, lake)
    # The lake's game no longer holds its first copies' rows, so swapping it in would change
    # more than the correction.
    shots = before["shots"].with_columns(
        x=pl.when(pl.col("game_id") == GAME).then(pl.col("x") + 1).otherwise(pl.col("x"))
    )
    corrected = cr.corrected_feeds(store, games(), {**before, "shots": shots})
    assert corrected.unmatched == [GAME] and corrected.games == [OPENING_WEEK_GAMES[1]]


def test_an_empty_rechecked_chart_keeps_the_first_chart(tmp_path: Path) -> None:
    empty = {GAME: {"shiftcharts": lambda _: b'{"data": [], "total": 0}'}}
    store = stored_copies(tmp_path, empty)
    lake = Lake(tmp_path / "scratch" / "lake")
    before = ingested(store, lake)
    corrected = cr.corrected_feeds(store, games(), before)
    assert corrected.charts_kept == [GAME]
    copies = parse_copies(store, games_row(GAME))
    assert copies is not None and not copies.chart_rechecked
    shifts = corrected.rows["shifts"].filter(pl.col("game_id") == GAME)
    assert shifts.drop("raw_key").equals(
        before["shifts"].filter(pl.col("game_id") == GAME).drop("raw_key")
    )


def test_the_replay_never_writes_the_repositorys_lake_or_r2(tmp_path: Path) -> None:
    store = stored_copies(tmp_path)
    lake = Lake(tmp_path / "lake")
    before = ingested(store, lake)
    corrected = cr.corrected_feeds(store, games(), before)
    with pytest.raises(ValueError, match="repository's lake"):
        cr.swap_in(lake, corrected, tmp_path / "lake")
    mirrored = Lake(tmp_path / "copy", bucket="bucket", objects=object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="mirrored to R2"):
        cr.swap_in(mirrored, corrected, tmp_path / "lake")
    work = tmp_path / "work"
    (work / "data" / "lake").mkdir(parents=True)
    log = tmp_path / "steps.log"
    with pytest.raises(ValueError, match="repository's lake"):
        cr.run_steps(work, 20262027, work / "data" / "lake", ["true"], log)
    with pytest.raises(ValueError, match="no lake"):
        cr.run_steps(tmp_path / "empty", 20262027, tmp_path / "lake", ["true"], log)


def test_the_steps_run_in_the_live_builds_order_in_the_work_directory(tmp_path: Path) -> None:
    work = tmp_path / "work"
    (work / "data" / "lake").mkdir(parents=True)
    script = "import sys, pathlib; pathlib.Path('ran.txt').open('a').write(sys.argv[1] + '\\n')"
    cr.run_steps(
        work, 20262027, tmp_path / "lake", [sys.executable, "-c", script], tmp_path / "log"
    )
    assert (work / "ran.txt").read_text().split() == list(cr.STEPS)
    with pytest.raises(RuntimeError, match="nhl xg failed"):
        cr.run_steps(work, 20262027, tmp_path / "lake", ["false"], tmp_path / "log")


def test_table_changes_count_values_beyond_the_tolerance_and_ignore_the_build() -> None:
    base = pl.DataFrame(
        {
            "game_id": [1, 2, 3, 4],
            "delta_s": [0.1, 0.2, 0.3, None],
            "home": ["A", "B", "C", "D"],
            "prior_games": [1.0, 2.0, 3.0, 4.0],
            "artifact_version": ["team-strength-20261021-aaaaaaa"] * 4,
        }
    )
    corrected = pl.DataFrame(
        {
            "game_id": [1, 2, 3, 4, 5],
            "delta_s": [0.1 + 1e-12, 0.25, 0.1, 0.4, 0.0],
            "home": ["A", "B", "C", "X", "E"],
            "prior_games": [1.0, 2.0, 3.0, None, 5.0],
            "artifact_version": ["team-strength-20261021-bbbbbbb"] * 5,
        }
    )
    assert (
        cr.table_changes(base, base.with_columns(artifact_version=pl.lit("x")), "team_strength")[
            "rows"
        ].to_list()
        == []
    )
    found = {row[1]: row[2:] for row in cr.table_changes(base, corrected, "team_strength").rows()}
    assert found[ADDED] == (1, 1, None, None) and REMOVED not in found
    assert found[cr.CHANGED][:2] == (3, 3)
    # 1e-12 is the order of a float sum, not a change; a null on one side is.
    rows, changed_games, median, largest = found["delta_s"]
    assert (rows, changed_games) == (3, 3)
    assert median == pytest.approx(0.125) and largest == pytest.approx(0.2)
    assert found["home"] == (1, 1, None, None)
    # A value that became null counts, with no size.
    assert found["prior_games"] == (1, 1, None, None)
    assert "artifact_version" not in found
    goalies = pl.DataFrame({"game_id": [1], "stint_id": [1], "home_goalie": [8_470_000]})
    swapped = goalies.with_columns(home_goalie=pl.lit(8_478_000))
    assert cr.table_changes(goalies, swapped, "stints").rows()[-1] == (
        "stints",
        "home_goalie",
        1,
        1,
        None,
        None,
    )


def saved_day(tmp_path: Path) -> lb.Bundle:
    store = lb.LocalStore(tmp_path)
    model_bundle(store)
    return lb.read(store, fixture_slate()["game_date"][0])


def models(bundle: lb.Bundle, tables: cr.ModelTables) -> lp.Models:
    fitted = lb.models(cr.with_rows(bundle, tables))
    assert fitted is not None
    return fitted


def by_game(frame: pl.DataFrame, column: str) -> dict[int, float]:
    return dict(frame.select("game_id", column).iter_rows())


def test_rows_rebuilt_from_the_same_tables_are_the_bundles_own(tmp_path: Path) -> None:
    saved = saved_day(tmp_path)
    again = cr.with_rows(saved, TABLES)
    for name in lb.MODEL_INPUTS:
        assert again.inputs[name].equals(saved.inputs[name]), name
    for name in lb.BASE_INPUTS:
        assert again.inputs[name] is saved.inputs[name]


def home_rows(frame: pl.DataFrame, game: dict[str, Any]) -> pl.Expr:
    return (pl.col("game_id") == game["game_id"]) & (pl.col("team") == game["home"])


def test_a_changed_input_moves_only_the_models_that_read_it(tmp_path: Path) -> None:
    saved = saved_day(tmp_path)
    game = fixture_slate().row(0, named=True)
    base = models(saved, TABLES)
    others = [g for g in fixture_slate()["game_id"].to_list() if g != game["game_id"]]

    def same(a: dict[int, float], b: dict[int, float], games: list[int]) -> bool:
        return all(a[g] == pytest.approx(b[g], abs=1e-12) for g in games)

    # A goalie's effect feeds B2 alone: B3 reads goalies through its goal multipliers.
    effects = B2_TABLES.goalie_effects.with_columns(
        goals_saved=pl.when(home_rows(B2_TABLES.goalie_effects, game))
        .then(pl.col("goals_saved") + 5)
        .otherwise(pl.col("goals_saved"))
    )
    moved = models(saved, (replace(B2_TABLES, goalie_effects=effects), B3_TABLES, U_TABLES))
    b2_base, b2_moved = by_game(base.b2, "p_b2"), by_game(moved.b2, "p_b2")
    assert b2_moved[game["game_id"]] != pytest.approx(b2_base[game["game_id"]], abs=1e-6)
    assert same(b2_base, b2_moved, others)
    assert same(by_game(base.b3, "p_b3"), by_game(moved.b3, "p_b3"), list(b2_base))
    # A player's rating feeds B3 alone.
    home = B3_TABLES.lineups.filter(home_rows(B3_TABLES.lineups, game))["player_id"].implode()
    ratings = B3_TABLES.player_ratings.with_columns(
        mean=pl.when(
            (pl.col("game_id") == game["game_id"])
            & pl.col("player_id").is_in(home)
            & (pl.col("component") == "ev_off")
        )
        .then(pl.col("mean") + 0.5)
        .otherwise(pl.col("mean"))
    )
    moved = models(saved, (B2_TABLES, replace(B3_TABLES, player_ratings=ratings), U_TABLES))
    b3_base, b3_moved = by_game(base.b3, "p_b3"), by_game(moved.b3, "p_b3")
    assert b3_moved[game["game_id"]] != pytest.approx(b3_base[game["game_id"]], abs=1e-6)
    assert same(b3_base, b3_moved, others)
    assert same(b2_base, by_game(moved.b2, "p_b2"), list(b2_base))
    # A change to a game off the slate moves nothing of the day.
    later = B3_TABLES.player_ratings.with_columns(
        mean=pl.when(~pl.col("game_id").is_in(fixture_slate()["game_id"].implode()))
        .then(pl.col("mean") + 0.5)
        .otherwise(pl.col("mean"))
    )
    moved = models(saved, (B2_TABLES, replace(B3_TABLES, player_ratings=later), U_TABLES))
    assert same(b3_base, by_game(moved.b3, "p_b3"), list(b3_base))


def test_a_v2_day_keeps_its_saved_starters(tmp_path: Path) -> None:
    saved = saved_day(tmp_path)
    game = fixture_slate().row(0, named=True)
    cutoff = datetime.fromisoformat(saved.manifest["input_cutoff"])
    candidates = B2_TABLES.goalie_starts.filter(home_rows(B2_TABLES.goalie_starts, game))
    backup = candidates.sort("p_start")["goalie_id"][0]
    named = pl.DataFrame(
        {
            "game_id": [game["game_id"]],
            "team": [game["home"]],
            "source": ["nhl"],
            "goalie_id": [backup],
            "p": [0.9],
            "known_utc": [cutoff - timedelta(hours=1)],
        }
    )
    v2 = replace(saved, inputs={**saved.inputs, "goalie_starters": named})
    starts = cr.with_rows(v2, TABLES).inputs["goalie_starts"].filter(home_rows(candidates, game))
    shares = dict(starts.select("goalie_id", "p_start").iter_rows())
    assert shares[backup] == pytest.approx(0.9) and sum(shares.values()) == pytest.approx(1.0)
    assert cr.with_rows(v2, TABLES).inputs["goalie_starters"] is named


def ledger(day: date, rows: list[tuple[int, float, bool, float]]) -> pl.DataFrame:
    """A replayed ledger's compared columns: each game's p_blend, bet and stake."""
    return pl.DataFrame(
        {
            "game_date": [day] * len(rows),
            "game_id": [r[0] for r in rows],
            "status": [lp.PREDICTED] * len(rows),
            "p_b2": [0.5] * len(rows),
            "p_b3": [0.5] * len(rows),
            "u": [0.0] * len(rows),
            "p_blend": [r[1] for r in rows],
            "bet": [r[2] for r in rows],
            "side": ["home" if r[2] else None for r in rows],
            "stake": [r[3] for r in rows],
        }
    )


def test_each_day_is_checked_against_its_ledger_and_compared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The replay itself is bundle.replay, tested in test_bundle: here, that each version's rows
    # reach it, the base is checked against the day's ledger, and the two are set side by side.
    saved = saved_day(tmp_path)
    day = fixture_slate()["game_date"][0]
    game = fixture_slate()["game_id"][0]

    def replayed(bundle: lb.Bundle, _: object) -> pl.DataFrame:
        effect = float(bundle.inputs["goalie_effects"]["goals_saved"].sum())
        return ledger(day, [(game, 0.5 + effect / 1e4, effect > 0, 1.0 if effect > 0 else 0.0)])

    monkeypatch.setattr(lb, "replay", replayed)
    base = replayed(cr.with_rows(saved, TABLES), None)
    effects = B2_TABLES.goalie_effects.with_columns(goals_saved=pl.col("goals_saved").abs() + 1)
    corrected = (replace(B2_TABLES, goalie_effects=effects), B3_TABLES, U_TABLES)
    played = [game]
    found = cr.replay_days({day: (saved, base)}, {day: None}, TABLES, corrected, played)  # type: ignore[dict-item]
    assert found.not_reproduced == found.unreplayable == {}
    games = cr.predicted(found.games)
    assert games.height == 1 and games["bet_corrected"].item()
    assert games["p_blend_corrected"].item() != games["p_blend"].item()
    stale = base.with_columns(p_blend=pl.col("p_blend") + 0.01)
    found = cr.replay_days({day: (saved, stale)}, {day: None}, TABLES, corrected, played)  # type: ignore[dict-item]
    # A ledger neither the base nor the day's own bundle reproduces is reported both ways.
    differs = {day: [f"p_blend differs for [{game}]"]}
    assert found.not_reproduced == found.unreplayable == differs
    # A game not played when the lakes were rebuilt has the morning's rows in both: not compared.
    found = cr.replay_days({day: (saved, base)}, {day: None}, TABLES, corrected, [])  # type: ignore[dict-item]
    assert found.games.is_empty() and found.unplayed == [game]


def test_the_report_counts_changed_predictions_and_bets() -> None:
    day = date(2026, 10, 14)
    base = ledger(day, [(1, 0.50, True, 2.0), (2, 0.60, False, 0.0), (3, 0.40, False, 0.0)])
    corrected = ledger(day, [(1, 0.51, True, 2.5), (2, 0.60, True, 1.0), (3, 0.40, False, 0.0)])
    decisions = cr.Decisions(cr.decision_changes(base, corrected), {})
    rows = {t: pl.DataFrame() for t in FEED_TABLES}
    corrected_version = cr.Corrected([1], [20262027], [date(2026, 10, 1)], rows, [], [1])
    changes = pl.DataFrame(
        [
            ("team_strength", cr.CHANGED, 2, 2, None, None),
            ("team_strength", "delta_s", 2, 2, 0.01, 0.02),
        ],
        schema=cr.CHANGE_SCHEMA,
        orient="row",
    )
    text = cr.markdown(
        "corrections-replay-20261021-abcdef0",
        corrected_version,
        {"shots": (10, 9)},
        changes,
        {"team_strength": (20, 20), "rapm_terms": (5, None)},
        decisions,
    )
    assert "| p_blend (points) | 3 | 1 | 0.000 | 1.000 | 1.000 |" in text
    assert "| team_strength | 20 | 20 | 2 | 2 | 0 | 0 |" in text
    assert "| rapm_terms | 5 | — | 0 | — | 0 | 0 |" in text
    assert "| team_strength | delta_s | 2 | 0.01 | 0.02 |" in text
    assert "1 bets in the base and 2 in the corrected version" in text
    assert (
        "1 games are bet in one version only, 0 bet in both change side, and 2 stakes change"
        in text
    )
    assert "the base rows reproduce every day's ledger" in text
    assert "it still replays the day's ledger" in text and "in 1 calendar weeks" in text
    assert "1 of them keep their first chart" in text
