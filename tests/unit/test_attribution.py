"""The live bets' attribution (#193): each bet's move from the market, split as history's bets are
(backtest/e3.attribution), with B3's parts from the day's run bundle and the usual levels fixed
once beside the live fit."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import polars as pl
import pytest
from test_bundle import (
    B2_TABLES,
    B3_TABLES,
    FIT,
    NO_QUOTES,
    RECORD,
    START,
    TEST,
    U_TABLES,
    fixture_slate,
    model_bundle,
)

from nhl_edge.backtest import e3
from nhl_edge.betting.selection import POLICY_VERSION
from nhl_edge.lake.schemas import PaperLedger, dtypes
from nhl_edge.live import attribution as la
from nhl_edge.live import blend_fit
from nhl_edge.live import bundle as lb
from nhl_edge.live import predict as lp

LIVE = blend_fit.load(json.loads(FIT.read_text()))
# Usual levels: each part's line on logit p_mkt.
LINES = {
    "skaters": (0.02, 0.30),
    "goalies": (0.0, 0.05),
    "home_ice": (0.15, 0.0),
    "schedule": (0.0, 0.0),
}


def logit(p: float) -> float:
    return float(np.log(p / (1 - p)))


def ledger(rows: list[dict]) -> pl.DataFrame:
    """Ledger rows, the given columns set and the rest empty."""
    full = []
    for row in rows:
        base = {name: None for name in dtypes(PaperLedger)}
        base.update(status=lp.PREDICTED, bet=True, away="PIT", home="WSH", price=2.0, ev=0.03)
        base.update(row)
        full.append(base)
    return pl.DataFrame(full, schema=dtypes(PaperLedger))


def at_usual(game_id: int, p_b0: float, **away: float) -> dict:
    """B3's parts at their usual levels for the market price, moved by `away` in log-odds."""
    x = logit(p_b0)
    parts = {part: alpha + beta * x for part, (alpha, beta) in LINES.items()}
    for part, move in away.items():
        parts[part] += move
    return {"game_id": game_id, "intercept": -0.05, **parts}


def test_a_live_bets_edge_from_one_input_is_driven_by_it() -> None:
    # The done-when: the home side's skaters are rated 0.4 above their usual level at the price,
    # every other input sits at its usual level.
    bets = ledger([{"game_id": 1, "side": "home", "p_b0": 0.52, "u": 0.0}])
    parts = pl.DataFrame([at_usual(1, 0.52, skaters=0.4)])
    out = la.bet_parts(bets, parts, LINES, LIVE)
    [row] = out.to_dicts()
    _, _, b_x, _ = LIVE.blends["BLEND"].weights
    assert row["driver"] == "skaters"
    assert row["part_skaters"] == pytest.approx(b_x * 0.4)
    for part in ("goalies", "home_ice", "schedule"):
        assert row[f"part_{part}"] == pytest.approx(0.0, abs=1e-12)
    # Bet on the away side, the same move counts against it.
    away = la.bet_parts(bets.with_columns(side=pl.lit("away")), parts, LINES, LIVE)
    assert away["part_skaters"].item() == pytest.approx(-b_x * 0.4)
    assert away["driver"].item() != "skaters"


def test_the_parts_add_up_to_the_blends_move_from_the_market() -> None:
    # With B3's log-odds its intercept plus its parts, the parts sum to the live blend's logit
    # minus the market's, toward the bet's side: nothing is left out.
    p_b0, u = 0.45, 0.8
    parts = at_usual(7, p_b0, skaters=-0.1, goalies=0.3, schedule=0.05)
    p_b3 = 1 / (1 + np.exp(-(parts["intercept"] + sum(parts[p] for p in e3.INPUTS))))
    p_blend = LIVE.blends["BLEND"].predict(np.array([p_b0]), np.array([p_b3]), np.array([u]))[0]
    for side, sign in (("home", 1.0), ("away", -1.0)):
        bets = ledger([{"game_id": 7, "side": side, "p_b0": p_b0, "u": u}])
        row = la.bet_parts(bets, pl.DataFrame([parts]), LINES, LIVE).to_dicts()[0]
        total = sum(row[f"part_{p}"] for p in e3.PARTS)
        assert total == pytest.approx(sign * (logit(p_blend) - logit(p_b0)), abs=1e-9)


def test_only_bets_are_attributed_and_one_without_b3_parts_has_no_driver() -> None:
    bets = ledger(
        [
            {"game_id": 1, "side": "home", "p_b0": 0.5, "u": 0.0},
            {"game_id": 2, "side": "away", "p_b0": 0.5, "u": 0.0},
            {"game_id": 3, "side": None, "p_b0": 0.5, "u": 0.0, "bet": False},
        ]
    )
    out = la.bet_parts(bets, pl.DataFrame([at_usual(1, 0.5, goalies=0.2)]), LINES, LIVE)
    assert sorted(out["game_id"].to_list()) == [1, 2]
    assert out.filter(pl.col("game_id") == 2)["driver"].item() is None
    assert "no B3 parts" in la.markdown(out)


def test_b3_parts_from_the_bundle_alone_match_those_on_the_whole_lake(tmp_path: Path) -> None:
    store = lb.LocalStore(tmp_path)
    fitted = model_bundle(store)
    saved = lb.read(store, fixture_slate()["game_date"][0])
    from_bundle = lb.b3_parts(saved)
    assert from_bundle is not None
    cutoff = datetime.fromisoformat(saved.manifest["input_cutoff"])
    moments = fixture_slate().select("game_id", prediction_utc=pl.lit(cutoff))
    assert fitted.b3_model is not None
    whole = e3.b3_parts(moments, B3_TABLES, fitted.b3_model, TEST)
    assert from_bundle.height == whole.height == fixture_slate().height
    a, b = from_bundle.sort("game_id"), whole.sort("game_id")
    for column in ("intercept", *e3.INPUTS):
        assert a[column].to_list() == pytest.approx(b[column].to_list(), abs=1e-12)


def training(n: int = 40) -> pl.DataFrame:
    """Training games whose parts lie on known lines in logit_mkt."""
    rng = np.random.default_rng(3)
    x = rng.normal(0, 0.4, n)
    seasons = [20182019] * (n // 2) + [20192020] * (n - n // 2)
    when = [datetime(2019, 1, 1, tzinfo=UTC) + timedelta(hours=i) for i in range(n)]
    frame = {"season": seasons, "game_id": list(range(n)), "prediction_utc": when, "logit_mkt": x}
    for part, (alpha, beta) in LINES.items():
        frame[part] = alpha + beta * x
    frame["intercept"] = [0.0] * n
    return pl.DataFrame(frame)


def test_the_levels_record_their_lines_and_refuse_a_row_known_after_the_live_start() -> None:
    rows = training()
    cutoffs = [datetime(2018, 9, 1, tzinfo=UTC)]
    now = datetime(2026, 10, 8, tzinfo=UTC)
    record = la.artifact("attribution-levels-x", LIVE.version, LIVE.fold_start, rows, cutoffs, now)
    assert record["live_fit"] == LIVE.version and record["policy"] == POLICY_VERSION
    assert record["training"]["per_season"] == {"20182019": 20, "20192020": 20}
    assert record["train_cutoff"] == rows["prediction_utc"].max().isoformat()
    loaded = la.load(record, LIVE.version)
    for part, (alpha, beta) in LINES.items():
        assert loaded[part] == pytest.approx((alpha, beta), abs=1e-9)
    with pytest.raises(ValueError, match="not"):
        la.load(record, "blend-live-20991231-0000000")
    late = rows.with_columns(prediction_utc=pl.lit(LIVE.fold_start))
    with pytest.raises(ValueError, match="after"):
        la.artifact("attribution-levels-x", LIVE.version, LIVE.fold_start, late, cutoffs, now)


def test_the_levels_stand_on_exactly_the_live_fits_games() -> None:
    record = json.loads(FIT.read_text())
    seasons = [int(s) for s, n in record["training"]["per_season"].items() for _ in range(n)]
    history = pl.DataFrame({"season": seasons})
    assert la.count_problems(history, record) == []
    short = history.filter(pl.int_range(pl.len()) > 0)
    [problem] = la.count_problems(short, record)
    assert "games rebuilt" in problem


def test_the_levels_are_fixed_once_per_live_fit(tmp_path: Path) -> None:
    assert la.path_for(LIVE.version, tmp_path) is None
    (tmp_path / "attribution-levels-a.json").write_text(json.dumps({"live_fit": LIVE.version}))
    (tmp_path / "attribution-levels-b.json").write_text(json.dumps({"live_fit": "other"}))
    assert la.path_for(LIVE.version, tmp_path) == tmp_path / "attribution-levels-a.json"
    (tmp_path / "attribution-levels-c.json").write_text(json.dumps({"live_fit": LIVE.version}))
    with pytest.raises(ValueError, match="more than one"):
        la.path_for(LIVE.version, tmp_path)


def test_the_slate_section_says_why_it_has_no_attribution(tmp_path: Path) -> None:
    day = fixture_slate()["game_date"][0]
    empty = ledger([])
    assert "pass --r2 or --from" in la.slate_section(None, day, empty)
    assert "no manifest" in la.slate_section(lb.LocalStore(tmp_path), day, empty)


def test_the_slate_section_attributes_the_days_bets(tmp_path: Path) -> None:
    # The fixture day's bundle, decided with the committed live fit, and its usual levels beside
    # it: every bet gets its parts and a driver.
    games = fixture_slate()
    day = games["game_date"][0]
    cutoff = games["start_utc"].min()
    assert isinstance(cutoff, datetime)
    moments = games.select("game_id", prediction_utc=pl.lit(cutoff))
    fitted = lp.models(B2_TABLES, B3_TABLES, U_TABLES, games, moments, START, TEST)
    store = lb.LocalStore(tmp_path / "bundles")
    identity = {
        "day": str(day),
        "input_cutoff": cutoff.isoformat(),
        "live_fit": FIT.name,
        "live_fit_sha256": lb.sha256(FIT.read_bytes()),
    }
    rows = lb.inputs(games, RECORD, NO_QUOTES, (B2_TABLES, B3_TABLES, U_TABLES))
    fits = {"b2": fitted.b2_model, "b3": fitted.b3_model}
    lb.write_once(store, day, rows, fits, identity, {})
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / FIT.name).write_bytes(FIT.read_bytes())
    first, second = games.head(2)["game_id"].to_list()
    bets = ledger(
        [
            {"game_id": first, "side": "home", "p_b0": 0.55, "u": 0.2},
            {"game_id": second, "side": "away", "p_b0": 0.48, "u": -0.3},
        ]
    )
    missing = la.slate_section(store, day, bets, reports)
    assert "no usual levels" in missing
    levels = la.artifact(
        "attribution-levels-x",
        LIVE.version,
        LIVE.fold_start,
        training(),
        [datetime(2018, 9, 1, tzinfo=UTC)],
        datetime(2026, 10, 8, tzinfo=UTC),
    )
    (reports / "attribution-levels-x.json").write_text(json.dumps(levels))
    section = la.slate_section(store, day, bets, reports)
    assert section.startswith("Attribution of each bet")
    table = [line for line in section.splitlines() if line.startswith("| PIT at WSH")]
    assert len(table) == 2
    assert all("no B3 parts" not in line for line in table)
