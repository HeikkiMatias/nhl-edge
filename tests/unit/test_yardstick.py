"""The market-relative yardstick (#361, ADR 0053) on synthetic training seasons: the move skill,
the result beyond the close, the rested games, a feature's first season, SBR's market, B2's and
B3's features at the opener's time, the report, and a run kept once."""

from datetime import UTC, datetime
from pathlib import Path

import market_history as mh
import numpy as np
import polars as pl
import pytest
import yardstick_fixtures as yf
from typer.testing import CliRunner

from nhl_edge.backtest import walk_forward as wf
from nhl_edge.backtest import yardstick as ys
from nhl_edge.cli import app
from nhl_edge.features.team_strength import TUNED_CUTOFF
from nhl_edge.live import bundle as lb
from nhl_edge.market.devig import fair_probabilities

ONLY = {"x": ("x",), "z": ("z",), "w": ("w",)}


def test_the_move_skill_rewards_a_feature_that_anticipates_the_move() -> None:
    _, found = ys.run(yf.moves(), "explore", ONLY)
    # The move follows x: its forecast beats the opener's drift. z is noise.
    assert found["x"]["move"]["high"] < 0
    assert (
        found["z"]["move"]["low"] < 0.0005
        and found["z"]["move"]["mean"] > found["x"]["move"]["mean"]
    )
    assert found["x"]["slope"]["low"] > 0.4


def test_the_result_beyond_the_close_rewards_what_the_close_misses() -> None:
    _, found = ys.run(yf.moves(), "explore", ONLY)
    # The result follows w beyond the close; x is in the close already.
    assert found["w"]["result"]["high"] < 0
    assert found["x"]["result"]["mean"] > found["w"]["result"]["high"]


def test_each_season_is_fitted_on_the_earlier_ones() -> None:
    frame = yf.moves()
    games = ys.scored(frame, ("x",), ys.EXPLORE)
    assert sorted(games["season"].unique().to_list()) == list(ys.EXPLORE)
    # 2012-13 is fitted on 2011-12 alone: the fit on that season gives the same forecast.
    first = frame.filter(pl.col("season") <= 20122013)
    alone = ys.scored(first, ("x",), (20122013,))
    both = games.filter(pl.col("season") == 20122013)
    assert np.allclose(alone["y_hat"].to_numpy(), both["y_hat"].to_numpy())


def test_a_candidate_is_scored_from_a_season_after_its_features_first_inputs() -> None:
    frame = yf.moves().with_columns(finishing_diff=pl.col("x"))
    per_game, found = ys.run(frame, "explore", {"finishing_diff": ("finishing_diff",), "x": ("x",)})
    seasons = per_game.group_by("candidate").agg(pl.col("season").min()).sort("candidate")
    assert seasons.rows() == [("finishing_diff", 20132014), ("x", 20122013)]
    assert "20122013" not in found["finishing_diff"]["move_per_season"]
    assert ys.first_inputs(ys.CANDIDATES["all"]) == 20122013


def test_the_rested_figures_read_only_the_rested_games() -> None:
    frame = yf.moves()
    _, found = ys.run(frame, "explore", {"x": ("x",)})
    scored = frame.filter(pl.col("season").is_in(ys.EXPLORE))
    assert found["x"]["rested_games"] == scored["rested"].sum() < found["x"]["games"]


def test_the_market_is_the_opener_and_the_close_de_vigged() -> None:
    sbr, games = mh.seasons([20102011, 20112012, 20122013])
    found = ys.market(sbr, games)
    assert set(found["season"].unique().to_list()) == {20112012, 20122013}
    row = found.row(0, named=True)
    h2h = sbr.filter(pl.col("game_id") == row["game_id"], pl.col("market") == "h2h")
    for quote, column in (("open", "o"), ("close", "c")):
        prices = h2h.filter(pl.col("quote") == quote).sort("side", descending=True)
        p = fair_probabilities(np.array([prices["price_decimal"].to_list()]), wf.B1_METHOD)[0, 0]
        assert row[column] == pytest.approx(np.log(p / (1 - p)))
    assert row["y"] == pytest.approx(row["c"] - row["o"])


def test_an_opener_e2_refuses_is_dropped() -> None:
    sbr, games = mh.seasons([20102011, 20112012])
    planted = int(games.filter(pl.col("season") == 20112012)["game_id"][3])
    found = ys.market(mh.implausible_opener(sbr, planted), games)
    assert planted not in found["game_id"].to_list()
    assert found.height == games.filter(pl.col("season") == 20112012).height - 1


def test_the_features_are_b2s_and_b3s_inputs_at_the_opener_time() -> None:
    teams, players, sbr = yf.league()
    teams, players = ys.through(teams, 20142015), ys.through(players, 20142015)
    market = ys.market(sbr, players.games)
    found = ys.features(teams, players, market.select("game_id", "prediction_utc"))
    assert found.height == market.height
    # Δĝ is the sum of its four parts.
    parts = sum(
        pl.col(name) for name in ("five_diff", "special_diff", "finishing_diff", "goalie_diff")
    )
    assert found.select((parts - pl.col("delta_g_hat")).abs().max()).item() < 1e-9
    rows = ys.rows(market, found, players.games)
    assert rows.height == market.height and rows["rested"].dtype == pl.Boolean


def test_untuned_reads_a_row_at_its_datas_time() -> None:
    as_of = datetime(2013, 11, 1, 14, tzinfo=UTC)
    frame = pl.DataFrame(
        {
            "as_of_utc": [as_of, datetime(2018, 11, 1, 14, tzinfo=UTC)],
            "observed_utc": [TUNED_CUTOFF, datetime(2018, 11, 1, 14, tzinfo=UTC)],
        }
    ).with_columns(pl.all().cast(ys.UTC))
    assert ys.untuned(frame)["observed_utc"].to_list() == frame["as_of_utc"].to_list()


def test_the_report_names_every_candidate_and_each_unscored_season() -> None:
    frame = yf.moves().with_columns(finishing_diff=pl.col("x"))
    _, found = ys.run(frame, "explore", {"x": ("x",), "finishing_diff": ("finishing_diff",)})
    report = ys.markdown(found, "explore", "yardstick-20261030-abc1234")
    assert "| x |" in report and "| finishing_diff |" in report
    assert "not scored" in report


def test_a_paired_difference_favours_the_better_candidate() -> None:
    per_game, _ = ys.run(yf.moves(), "explore", ONLY)
    assert ys.paired(per_game, "x", "z")["high"] < 0


def test_a_run_is_kept_once(tmp_path: Path) -> None:
    store = lb.LocalStore(tmp_path)
    per_game, found = ys.run(yf.moves(), "explore", {"x": ("x",)})
    now = datetime(2026, 10, 30, 15, tzinfo=UTC)
    key = ys.run_key("confirm", "yardstick-20261030-abc1234", "five", now)
    ys.keep(store, key, per_game, found, "report", {"half": "confirm"}, now)
    assert ys.kept_rows(store, key).equals(per_game)
    with pytest.raises(ValueError, match="kept already"):
        ys.keep(store, key, per_game, found, "report", {"half": "confirm"}, now)
    assert ys.run_key("explore", "v", None, now) != ys.run_key(
        "explore", "v", None, now.replace(hour=16)
    )


def test_a_confirm_run_needs_its_claim_and_r2() -> None:
    result = CliRunner().invoke(app, ["research", "yardstick", "--half", "confirm"])
    assert result.exit_code == 2
    assert "needs --claim and --r2" in " ".join(result.output.split())
    result = CliRunner().invoke(app, ["research", "yardstick", "--claim", "five"])
    assert result.exit_code == 2
