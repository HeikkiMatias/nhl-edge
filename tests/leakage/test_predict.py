"""Point-in-time rules for the day's paper decisions (#164, hard rules 1, 4 and 5; ADRs 0030 and
0033). Everything a decision reads was known before its instant: quotes from snapshots before it,
B2, B3 and u read at it from fits cut at the live fold start, and the bankroll from results public
before it. B2's, B3's and u's own point-in-time rules at a prediction time are tested in
test_b2.py, test_b3.py and test_uncertainty.py, and the slate's target rows in
test_live_targets.py; here, that the decision feeds them the right moments."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import polars as pl
import predict_fixtures as pf

from nhl_edge.game import b2, b3, uncertainty
from nhl_edge.live import blend_fit
from nhl_edge.live import predict as lp


@dataclass
class Spy:
    calls: dict[str, Any]

    def model(self, name: str) -> Any:
        def predictions(
            tables: Any, moments: pl.DataFrame, season: int, start: datetime, *_: Any
        ) -> Any:
            self.calls[name] = (tables.games, moments, season, start)
            frame = moments.select("game_id", p_home=pl.lit(0.5))

            @dataclass
            class Fit:
                train_cutoff: datetime

            return frame, Fit(datetime(2026, 4, 17, tzinfo=UTC))

        return predictions

    def parts(self, tables: Any, moments: pl.DataFrame) -> pl.DataFrame:
        self.calls["parts"] = (tables.games, moments)
        return moments.select("game_id", *[pl.lit(0.1).alias(p) for p in uncertainty.PARTS])


@dataclass(frozen=True)
class FakeTables:
    games: pl.DataFrame


def test_the_models_read_every_game_at_the_decision_from_fits_cut_at_the_fold(
    monkeypatch: Any,
) -> None:
    spy = Spy({})
    monkeypatch.setattr(b2, "predictions", spy.model("B2"))
    monkeypatch.setattr(b3, "predictions", spy.model("B3"))
    monkeypatch.setattr(uncertainty, "parts", spy.parts)
    played = pl.DataFrame(
        {"game_id": [2026020001], "home_score": [3], "away_score": [2]},
        schema_overrides={"home_score": pl.Int16, "away_score": pl.Int16},
    )
    slate = pf.slate()
    moments = slate.select("game_id", prediction_utc=pl.lit(pf.DECISION))
    start = pf.live().fold_start
    tables: Any = FakeTables(played)
    lp.models(tables, tables, tables, slate, moments, start)
    for name in ("B2", "B3"):
        games, read, season, cut = spy.calls[name]
        assert (season, cut) == (blend_fit.LIVE_SEASON, start)
        assert set(read["prediction_utc"]) == {pf.DECISION}
        # The slate's games join history with no result: none can be read for them.
        targets = games.filter(pl.col("game_id").is_in(list(pf.GAMES)))
        assert targets.height == 3
        assert targets["home_score"].is_null().all()
    games, read = spy.calls["parts"]
    assert set(read["prediction_utc"]) == {pf.DECISION}
    assert games.filter(pl.col("game_id").is_in(list(pf.GAMES)))["home_score"].is_null().all()


def test_quotes_and_results_after_the_decision_change_nothing() -> None:
    inputs = pf.day()
    before = lp.ledger(lp.decide(inputs), inputs)
    later = pf.DECISION + timedelta(seconds=1)
    quotes = pl.concat(
        [
            pf.day_quotes(),
            # A later midday-labelled snapshot, still in the window, and a late morning one.
            pf.quotes(pf.quote(later, 2026020053, "pinnacle", 3.0, 1.4)),
            pf.quotes(pf.quote(later, 2026020054, "pinnacle", 1.2, 4.5, slot="morning")),
        ]
    )
    moved = pf.day(quotes=quotes)
    assert lp.ledger(lp.decide(moved), moved).equals(before)


def test_the_bankroll_never_reads_a_result_public_at_or_after_the_decision() -> None:
    earlier = pl.DataFrame(
        {"game_id": [1], "bet": [True], "side": ["home"], "price": [2.0], "stake": [1.5]}
    )
    for public, expected in ((pf.DECISION - timedelta(seconds=1), 101.5), (pf.DECISION, 100.0)):
        results = pl.DataFrame(
            {"game_id": [1], "home_win": [1], "result_utc": [public]},
            schema_overrides={"home_win": pl.Int8},
        )
        assert lp.bankroll(earlier, results, pf.DECISION) == expected


def test_the_decision_snapshot_precedes_the_decision() -> None:
    at = pf.MIDDAY
    quotes = pf.day_quotes()
    # Decided at the snapshot's own instant, the snapshot isn't yet known.
    assert lp.decision_snapshot(quotes, at) is None
    assert lp.decision_snapshot(quotes, at + timedelta(microseconds=1)) == at
