"""Point in time for scripts/richer_xg.py (#421, hard rule 1): the previous play is never one after
the shot or in another period; the score state counts only goals earlier in game time, however
the feed logged them; a flurry discount reads only its chain's earlier shots; each season's fit
reads only earlier seasons' shots public before its fold start; the rebuilt parts' settings read
no result; and the script reads the lake, and the cached play-by-play, only to the explore
half."""

from typing import Any

import numpy as np
import polars as pl
import pytest
import richer_xg_fixtures as f
from shot_fixtures import games_from, synthetic_shots

from nhl_edge.backtest import yardstick as ys
from nhl_edge.backtest.walk_forward import fold_start

rx = f.load()


def test_the_previous_play_is_never_a_later_one_or_another_periods() -> None:
    plays = [
        f.play(1, "faceoff", "00:00", f.HOME, 0, 0, "N"),
        f.play(2, "shot-on-goal", "00:05", f.HOME, 70, 10, "O", sort_order=30),
        # Logged before the shot but timed after it: never its previous play.
        f.play(3, "hit", "00:08", f.AWAY, 50, 30, "D", sort_order=20),
        f.play(4, "shot-on-goal", "19:59", f.HOME, 80, 0, "O"),
        # The next period's first shot has no previous play.
        f.play(5, "shot-on-goal", "00:03", f.HOME, -75, 2, "O", period=2),
    ]
    rows = {r["event_id"]: r for r in rx.previous_coordinates(f.body(plays), f.GAME_ID)}
    assert rows[2]["prev_event_id"] == 1
    assert rows[4]["prev_event_id"] == 3
    assert rows[5]["prev_event_id"] is None and rows[5]["prev_x"] is None


def test_a_later_goal_changes_no_earlier_score_state() -> None:
    def game(n: int) -> pl.DataFrame:
        return pl.DataFrame(
            {
                "game_id": [1] * n,
                "event_id": list(range(1, n + 1)),
                "seconds": [10 * i for i in range(1, n + 1)],
                "sort_order": [10 * i for i in range(1, n + 1)],
                "is_home": [i % 2 == 0 for i in range(1, n + 1)],
                "is_goal": [True] * n,
            }
        )

    short = rx.score_state(game(3)).sort("event_id")
    longer = rx.score_state(game(6)).sort("event_id").head(3)
    assert short.equals(longer)


def test_a_goal_logged_before_a_shot_but_timed_after_it_never_counts() -> None:
    # The home goal is logged first (sort order 10) but scored 3 seconds after the away shot.
    game = pl.DataFrame(
        {
            "game_id": [1, 1],
            "event_id": [1, 2],
            "seconds": [103, 100],
            "sort_order": [10, 20],
            "is_home": [True, False],
            "is_goal": [True, False],
        }
    )
    state = {r["event_id"]: r for r in rx.score_state(game).iter_rows(named=True)}
    assert (state[2]["leads"], state[2]["trails"]) == (0.0, 0.0)
    assert (state[1]["leads"], state[1]["trails"]) == (0.0, 0.0)


def test_a_flurry_discount_reads_only_its_chains_earlier_shots() -> None:
    frame = pl.DataFrame(
        {
            "game_id": [1, 1, 1],
            "event_id": [1, 2, 3],
            "team": ["MTL"] * 3,
            "rebound": [False, True, True],
            "prev_event_id": [None, 1, 2],
        },
        schema_overrides={"prev_event_id": pl.Int64},
    )
    today = pl.DataFrame({"game_id": [1] * 3, "event_id": [1, 2, 3], "xg": [0.2, 0.5, 0.4]})
    later = today.with_columns(xg=pl.when(pl.col("event_id") == 3).then(0.9).otherwise("xg"))
    a = rx.flurry(today, frame).filter(pl.col("event_id") < 3)
    b = rx.flurry(later, frame).filter(pl.col("event_id") < 3)
    assert a.equals(b)


def test_each_seasons_fit_reads_only_earlier_seasons_before_its_fold_start() -> None:
    seasons = [20102011, 20112012, 20122013, 20132014, 20142015]
    shots = synthetic_shots(seasons, per_season=1500, seed=11)
    rng = np.random.default_rng(11)
    previous = f.no_previous(shots).with_columns(
        prev_x=pl.Series(rng.normal(30, 20, shots.height)),
        prev_y=pl.Series(rng.normal(0, 20, shots.height)),
    )
    frame = rx.richer_frame(shots, previous, f.players({}))
    calendar = games_from(shots).select("season", "start_utc")
    scored, fits = rx.predictions(frame, calendar)
    for model in fits:
        assert model.train_cutoff < fold_start(calendar, model.season)
        assert max(model.seasons) < model.season
    # Flipping 2014-15's goals leaves every earlier season's probabilities as they were.
    flipped = frame.with_columns(
        is_goal=pl.when(pl.col("season") == 20142015).then(~pl.col("is_goal")).otherwise("is_goal")
    )
    again, _ = rx.predictions(flipped, calendar)
    earlier = pl.col("season") < 20142015
    columns = [f"p_{level}" for level in rx.LEVELS]
    assert again.filter(earlier).select(columns).equals(scored.filter(earlier).select(columns))


def test_the_rebuilt_parts_settings_read_no_result() -> None:
    # Each grid's steadiest corner, by the tie order alone: no tuning run, so no result after a
    # fold start, picks them (Codex on #430). They happen to equal the frozen settings.
    from nhl_edge.features import goalie as ge
    from nhl_edge.features import team_strength as ts

    longest = max(s.half_life for s in ts.GRID)
    assert ts.Settings(longest, max(s.prior_games for s in ts.GRID)) == rx.FIVE_SETTINGS
    longest = max(s.half_life for s in ge.GRID)
    assert ge.Settings(longest, max(s.prior_shots for s in ge.GRID)) == rx.GOALIE_SETTINGS


def test_the_script_reads_the_lake_and_the_play_by_play_only_to_the_explore_half(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    last = max(ys.EXPLORE)
    asked: list[int] = []
    seen: dict[str, int] = {}

    class Lake:
        def read(self, table: str) -> pl.DataFrame:
            return pl.DataFrame({"season": [last, last + 10001], "table": [table, table]})

    def read(lake: Any, until: int) -> None:
        asked.append(until)

    def previous_frame(shots: pl.DataFrame, get: Any) -> None:
        seen["shots"] = int(shots["season"].max())  # type: ignore[arg-type]
        raise RuntimeError("stop here")

    monkeypatch.setattr(rx.ys, "read", read)
    monkeypatch.setattr(rx, "previous_frame", previous_frame)
    with pytest.raises(RuntimeError, match="stop here"):
        rx.run(Lake(), lambda key: b"")
    assert asked == [last] and seen == {"shots": last}
