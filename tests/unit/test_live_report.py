"""The live report (#166) under ADR 0032's rules, on synthetic seasons: too few weeks, interim
dates and the formal review, the coverage floor and the bound, eligibility, paired comparisons
with different coverage, the calibration band, the gaps, and the alerts."""

from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import numpy as np
import polars as pl
import pytest

from nhl_edge.betting.selection import POLICY_VERSION
from nhl_edge.ingest.odds import ODDS_FRAME_SCHEMA
from nhl_edge.lake.schemas import PaperLedger, dtypes
from nhl_edge.live import predict as lp
from nhl_edge.live import report as lr
from nhl_edge.live import settle as ls
from nhl_edge.market import closing

FIRST = date(2026, 10, 12)  # a Monday
SCALE = {
    "means": {"goalie_doubt": 0.3, "availability_doubt": 1.6, "rookie_share": 0.18},
    "sds": {"goalie_doubt": 0.09, "availability_doubt": 0.5, "rookie_share": 0.07},
}
HISTORY = {
    "season": pl.Int32,
    "game_date": pl.Date,
    "game_id": pl.Int64,
    "opener_loss": pl.Float64,
    "close_loss": pl.Float64,
}
SETTLEMENT = {
    "game_date": pl.Date,
    "game_id": pl.Int64,
    "status": pl.String,
    "close_status": pl.String,
    "close_snapshot_utc": pl.Datetime("us", "UTC"),
    "clv": pl.Float64,
    "fair_move": pl.Float64,
    "profit": pl.Float64,
    "result_utc": pl.Datetime("us", "UTC"),
}


def at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=UTC)


class Season:
    """A synthetic season of paper decisions: per_week games a week, each predicted, half of them
    bets, with results and settlements. Probabilities are calibrated unless miscalibration is
    given (the outcome then follows p^(1/miscalibration) in log-odds terms)."""

    def __init__(self, n_weeks: int, per_week: int = 30, seed: int = 7, slope: float = 1.0) -> None:
        rng = np.random.default_rng(seed)
        ledger, games, settlements = [], [], []
        for i in range(n_weeks * per_week):
            day = FIRST + timedelta(days=7 * (i // per_week) + i % 7)
            game_id = 2026020001 + i
            start = at(day, 23)
            p = float(rng.uniform(0.3, 0.7))
            truth = 1 / (1 + np.exp(-slope * np.log(p / (1 - p))))
            home_win = bool(rng.random() < truth)
            bet = i % 2 == 0
            row: dict[str, Any] = {name: None for name in dtypes(PaperLedger)}
            row.update(
                game_date=day,
                season=20262027,
                game_id=game_id,
                start_utc=start,
                home="WSH",
                away="PIT",
                prediction_utc=at(day, 16, 47),
                published_utc=at(day, 16, 50),
                status=lp.PREDICTED,
                event_id=f"e{game_id}",
                decision_snapshot_utc=at(day, 16, 45),
                home_price=1.9,
                away_price=2.0,
                last_update_utc=at(day, 16, 44),
                p_b0=p,
                p_b1=p,
                p_b2=p,
                p_b3=p,
                goalie_doubt=0.3,
                availability_doubt=1.6,
                rookie_share=0.18,
                u=0.0,
                u_sd=0.0,
                p_blend=p,
                p_blend_b2=p,
                p_blend_market=p,
                side="home" if bet else None,
                price=2.0 if bet else None,
                picked=bet,
                guarded=False if bet else None,
                bet=bet,
                stake=1.0 if bet else None,
                policy_version=POLICY_VERSION,
                blend_version="blend-live-20261007-89ec631",
                code_version="predict-20261008-abc1234",
            )
            ledger.append(row)
            result_utc = start + timedelta(hours=12)
            games.append(
                {
                    "game_id": game_id,
                    "start_utc": start,
                    "home_score": 3 if home_win else 1,
                    "away_score": 1 if home_win else 3,
                    "observed_utc": result_utc,
                }
            )
            if bet:
                settlements.append(
                    {
                        "game_date": day,
                        "game_id": game_id,
                        "status": ls.SETTLED,
                        "close_status": closing.PROXY,
                        "close_snapshot_utc": start - timedelta(minutes=15),
                        "clv": float(rng.normal(0.02, 0.03)),
                        "fair_move": float(rng.normal(0.0, 0.02)),
                        "profit": 1.0 if home_win else -1.0,
                        "result_utc": result_utc,
                    }
                )
        self.ledger = pl.DataFrame(ledger, schema=dtypes(PaperLedger))
        self.games = pl.DataFrame(games)
        self.settlements = pl.DataFrame(settlements, schema=SETTLEMENT)

    def without_proxy(self, every: int, status: str = closing.MISSING) -> "Season":
        """Every every-th settled bet without a proxy, for status's reason."""
        index = pl.int_range(pl.len())
        missing = index % every == 0
        self.settlements = self.settlements.with_columns(
            close_status=pl.when(missing).then(pl.lit(status)).otherwise("close_status"),
            clv=pl.when(missing).then(None).otherwise("clv"),
            fair_move=pl.when(missing).then(None).otherwise("fair_move"),
            close_snapshot_utc=pl.when(missing).then(None).otherwise("close_snapshot_utc"),
        )
        return self

    def report(self, as_of: date = date(2026, 12, 31), **changes: Any) -> dict[str, Any]:
        # The lake's odds_snapshots, with each event's game_id.
        odds = changes.pop("odds", pl.DataFrame(schema=ODDS_FRAME_SCHEMA | {"game_id": pl.Int64}))
        history = changes.pop("history", pl.DataFrame(schema=HISTORY))
        return lr.report(
            changes.pop("ledger", self.ledger),
            changes.pop("settlements", self.settlements),
            self.games,
            odds,
            history,
            SCALE,
            as_of,
            changes.pop("policy", POLICY_VERSION),
            now=changes.pop("now", None),
            challenger_outputs=changes.pop("challenger_outputs", None),
        )


def test_an_earlier_policys_record_is_reported_apart(tmp_path: Any) -> None:
    # v1's decisions stay on record under v1 after v2's freeze (ADR 0037): each policy's report
    # counts its own rows, and an earlier one's files sit beside the frozen policy's.
    from nhl_edge.betting.selection import POLICY_V1

    season = Season(n_weeks=2)
    older = season.ledger.with_columns(policy_version=pl.lit(POLICY_V1))
    assert season.report(ledger=older)["coverage"]["slate_games"] == 0
    v1 = season.report(ledger=older, policy=POLICY_V1)
    assert v1["policy_version"] == POLICY_V1 and v1["coverage"]["slate_games"] == 60
    json_path, md_path = lr.write(v1, tmp_path)
    assert md_path.name == f"report-2026-12-31-{POLICY_V1}.md" and json_path.exists()


def test_every_registered_challenger_is_reported_from_the_day_after_its_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ADR 0038: each registered challenger, failures included, over the counted games of the
    # days after its registration, against the blend and B1 on the same games.
    from dataclasses import replace

    from nhl_edge.lake.schemas import ChallengerPredictions
    from nhl_edge.live import challengers as lc

    season = Season(n_weeks=6)
    registered = FIRST + timedelta(days=6)
    shifted = replace(lc.REGISTRY[0], name="shifted", registered_on=registered)
    silent = replace(lc.REGISTRY[0], name="silent", registered_on=registered)
    monkeypatch.setattr(lc, "REGISTRY", (shifted, silent))
    # Every game's output, those of the registration's week too: only later days count.
    outputs = ChallengerPredictions.validate(
        season.ledger.select(
            "game_date",
            "season",
            "game_id",
            challenger=pl.lit("shifted"),
            challenger_version=pl.lit("1"),
            adr=pl.lit("0038"),
            p_home=(pl.col("p_blend") + 0.05).clip(upper_bound=0.99),
            status=pl.lit(lp.PREDICTED),
            bundle_sha256=pl.lit("0" * 64),
            input_cutoff_utc=pl.col("decision_snapshot_utc"),
            reads=pl.lit("{}"),
            code_version=pl.lit("challengers-20261016-abc1234"),
            computed_utc=pl.col("published_utc") + timedelta(hours=12),
        )
    )
    result = season.report(challenger_outputs=outputs)["challengers"]
    assert result["registered"] == 2
    counted = season.ledger.filter(pl.col("game_date") > registered)
    shifted_result = result["challengers"]["shifted/1"]
    assert shifted_result["games"] == shifted_result["predicted"] == counted.height
    assert shifted_result["days"] == counted["game_date"].n_unique()
    # The paired difference, the challenger's log loss less the blend's, on the same games.
    scored = counted.join(
        season.games.select(
            "game_id", home_win=(pl.col("home_score") > pl.col("away_score")).cast(pl.Int8)
        ),
        on="game_id",
    )
    p = (scored["p_blend"] + 0.05).clip(upper_bound=0.99).to_numpy()
    q, y = scored["p_blend"].to_numpy(), scored["home_win"].to_numpy()
    expected = np.mean(
        -(y * np.log(p) + (1 - y) * np.log(1 - p)) + y * np.log(q) + (1 - y) * np.log(1 - q)
    )
    for model in ("BLEND", "B1"):
        figure = shifted_result["comparisons"][model]["difference"]
        assert figure["mean"] == pytest.approx(expected)
        assert figure["games"] == counted.height
    # No closes in the odds, so no shadow bet is valued.
    assert shifted_result["shadow"]["bets"] == 0
    assert shifted_result["missing_days"] == shifted_result["pending_days"] == []
    # A challenger without the outputs of its days is withheld, never shown on fewer days.
    days = sorted(counted["game_date"].unique().to_list())
    silent_result = result["challengers"]["silent/1"]
    assert silent_result["games"] == 0 and "comparisons" not in silent_result
    assert silent_result["missing_days"] == [day.isoformat() for day in days]
    assert silent_result["pending_days"] == []
    # One day without its output withholds every figure (Codex on #265).
    holed = season.report(challenger_outputs=outputs.filter(pl.col("game_date") != days[3]))
    entry = holed["challengers"]["challengers"]["shifted/1"]
    assert entry["missing_days"] == [days[3].isoformat()] and "comparisons" not in entry
    # The latest day is pending only until the end of the next UTC day: the nightly scores it
    # the night after its decision. After that it is missing too, the season's last day or not.
    unscored = outputs.filter(pl.col("game_date") != days[-1])
    night_after = at(days[-1] + timedelta(days=1), 9, 30)
    entry = season.report(challenger_outputs=unscored, now=night_after)["challengers"]
    entry = entry["challengers"]["shifted/1"]
    assert entry["pending_days"] == [days[-1].isoformat()] and "comparisons" in entry
    later = season.report(challenger_outputs=unscored)["challengers"]["challengers"]["shifted/1"]
    assert later["missing_days"] == [days[-1].isoformat()] and "comparisons" not in later
    text = lr.markdown(season.report(challenger_outputs=outputs))
    assert "## Challengers (ADR 0038)" in text and "Registered: 2, each reported." in text
    assert (
        f"- **shifted** version 1 (ADR 0038), registered 2026-10-18: {shifted_result['days']} days"
        in text
    )
    assert "- **silent** version 1 (ADR 0038), registered 2026-10-18: 0 days" in text
    assert f"**Withheld:** {len(days)} decision days after its registration" in text
    assert "**Withheld:**" in lr.markdown(holed)
    pending_text = lr.markdown(season.report(challenger_outputs=unscored, now=night_after))
    assert f"Pending, scored the night after the decision: {days[-1].isoformat()}." in pending_text
    # A new version under the same name is reported beside the old one (Codex on #265).
    monkeypatch.setattr(lc, "REGISTRY", (shifted, replace(shifted, version="2")))
    both = season.report(challenger_outputs=outputs)["challengers"]["challengers"]
    assert sorted(both) == ["shifted/1", "shifted/2"]
    assert both["shifted/1"]["games"] == counted.height and both["shifted/2"]["games"] == 0


def test_too_few_weeks_give_counts_and_no_estimate() -> None:
    result = Season(n_weeks=3).report()
    per_bet = result["closing_value"]["clv_per_bet"]
    assert per_bet == {"games": 45, "weeks": 3}
    assert "mean" not in result["comparisons"]["BLEND - B1"]["difference"]
    assert "at" not in result["calibration"]
    assert "too few weeks for an estimate" in lr.markdown(result)
    # The stake-weighted CLV and the bounds' percentiles of the observed CLV are withheld too,
    # their counts kept (Codex on #235).
    value = result["closing_value"]
    assert value["clv_stake_weighted"] == {"games": 45, "weeks": 3}
    assert all(bound["stand_in"] is None for bound in value["bound"].values())
    text = lr.markdown(result)
    assert "Stake-weighted CLV: 45 bets over 3 weeks" in text
    assert "withheld until 4 weeks" in text


def test_an_interim_report_gives_no_verdict_and_the_review_does() -> None:
    season = Season(n_weeks=8)
    interim = season.report(as_of=date(2027, 4, 11))
    assert interim["kind"] == "interim"
    assert interim["verdicts"] == {
        "closing_value": lr.INTERIM,
        "calibration": lr.INTERIM,
        "incomplete": {},
    }
    # The interim report shows no return, nor the drawdown's size (plan §11).
    assert "returns" not in interim and "max_drawdown" not in interim["alerts"]["drawdown"]
    assert "Interim report" in lr.markdown(interim)
    review = season.report(as_of=lr.REVIEW_DATE)
    assert review["kind"] == "formal review"
    assert review["verdicts"]["closing_value"] == lr.FAVOURABLE
    assert review["returns"]["bets"] == 120
    assert "max_drawdown" in review["alerts"]["drawdown"]


def test_below_the_coverage_floor_the_verdict_is_insufficient() -> None:
    # One eligible bet in five without a proxy: 80% coverage, under the 90% floor.
    season = Season(n_weeks=8).without_proxy(every=5)
    review = season.report(as_of=lr.REVIEW_DATE)
    value = review["closing_value"]
    assert value["floor"]["share_with_proxy"] == pytest.approx(0.8)
    assert value["clv_per_bet"]["low"] > 0
    assert review["verdicts"]["closing_value"] == lr.INSUFFICIENT
    assert review["coverage"]["eligible_without_proxy"] == {closing.MISSING: 24}


def test_a_bound_can_overturn_a_verdict_above_the_floor() -> None:
    # Above the floor (one in eleven missing), but the observed CLV's lower tail is far below
    # zero: counting the missing bets at its 10th percentile pulls the bound's interval under 0.
    season = Season(n_weeks=8).without_proxy(every=11)
    tail = pl.int_range(pl.len()) % 8 == 3
    season.settlements = season.settlements.with_columns(
        clv=pl.when(pl.col("clv").is_null()).then(None).when(tail).then(-0.20).otherwise(0.05)
    )
    plain = season.report(as_of=lr.REVIEW_DATE)
    value = plain["closing_value"]
    assert value["floor"]["share_with_proxy"] > lr.FLOOR
    bound = value["bound"]["p10"]
    assert bound["stand_in"] == pytest.approx(-0.20)
    assert bound["imputed"] == 11
    assert value["clv_per_bet"]["mean"] > bound["clv_per_bet"]["mean"]
    assert value["clv_per_bet"]["low"] > 0 and bound["clv_per_bet"]["low"] < 0
    assert plain["verdicts"]["closing_value"] == lr.INSUFFICIENT


def test_bets_without_a_pregame_snapshot_due_are_outside_the_floor() -> None:
    # Matinees and late starts: excluded and counted, never against the floor.
    season = Season(n_weeks=8).without_proxy(every=5, status=closing.NO_PREGAME)
    review = season.report(as_of=lr.REVIEW_DATE)
    cover = review["coverage"]
    assert cover["no_pregame_snapshot"] == 24 and cover["eligible"] == 96
    assert review["closing_value"]["floor"]["share_with_proxy"] == 1.0
    assert review["verdicts"]["closing_value"] == lr.FAVOURABLE


def test_comparisons_are_paired_and_count_what_they_leave_out() -> None:
    # B3 has no probability on a tenth of the games: B3 - B2 leaves them out, the others don't.
    season = Season(n_weeks=6)
    ledger = season.ledger.with_columns(
        p_b3=pl.when(pl.int_range(pl.len()) % 10 == 0).then(None).otherwise("p_b3")
    )
    compared = season.report(ledger=ledger)["comparisons"]
    assert compared["B3 - B2"]["left_out"] == 18
    assert compared["B3 - B2"]["difference"]["games"] == 162
    assert compared["BLEND - B1"]["left_out"] == 0
    assert compared["BLEND - B1"]["difference"]["mean"] == pytest.approx(0.0)


def test_the_calibration_band() -> None:
    # A calibrated blend over a short season: intervals wider than the band, so insufficient.
    calibrated = Season(n_weeks=6).report()["calibration"]
    assert [r["forecast"] for r in calibrated["at"]] == list(lr.BAND_AT)
    assert calibrated["at"][1]["band"] == pytest.approx([0.4875, 0.5125])
    assert lr.band_verdict(calibrated) == lr.INSUFFICIENT
    # Badly over-confident forecasts: the truth is far flatter, wholly outside the band at 35%.
    flat = Season(n_weeks=20, per_week=60, slope=0.2).report()["calibration"]
    assert flat["at"][0]["recalibrated"]["low"] > flat["at"][0]["band"][1]
    assert lr.band_verdict(flat) == lr.FAIL
    # A pass needs every interval inside its band.
    inside = {
        "at": [
            {"band": [0.34, 0.36], "recalibrated": {"value": 0.35, "low": 0.345, "high": 0.355}},
            {"band": [0.49, 0.51], "recalibrated": {"value": 0.5, "low": 0.495, "high": 0.505}},
        ]
    }
    assert lr.band_verdict(inside) == lr.PASS


def test_gaps_above_8_points_are_listed_for_hand_review() -> None:
    season = Season(n_weeks=1)
    ledger = season.ledger.with_columns(
        p_blend=pl.when(pl.col("game_id") == 2026020003)
        .then(pl.col("p_b1") + 0.09)
        .otherwise("p_blend")
    )
    gaps = season.report(ledger=ledger)["gaps"]
    assert [g["game_id"] for g in gaps] == [2026020003]
    assert gaps[0]["gap"] == pytest.approx(0.09)


def test_the_drawdown_review_says_when_never_how_much_until_the_end() -> None:
    season = Season(n_weeks=6)
    # Every bet lost: the bankroll falls 1 unit a bet from 100, past 20% at the 21st loss.
    lost = season.settlements.with_columns(profit=pl.lit(-1.0))
    dd = season.report(settlements=lost)["alerts"]["drawdown"]
    twenty_first = lost.sort("result_utc")["result_utc"][20]
    assert (dd["triggered"], dd["first_utc"]) == (True, twenty_first)
    assert "max_drawdown" not in dd
    assert "drawdown review is triggered" in lr.markdown(season.report(settlements=lost))


def test_the_drawdown_follows_the_seasons_bankroll_across_policies() -> None:
    # A new policy carries on the earlier one's bankroll (ADR 0037), so its drawdown review counts
    # the earlier policy's losses too: here the 21st loss of the season, not v2's own 21st.
    from nhl_edge.betting.selection import POLICY_V1

    season = Season(n_weeks=6)
    lost = season.settlements.with_columns(profit=pl.lit(-1.0))
    first_week = pl.col("game_date") < date(2026, 10, 19)
    ledger = season.ledger.with_columns(
        policy_version=pl.when(first_week)
        .then(pl.lit(POLICY_V1))
        .otherwise(pl.col("policy_version"))
    )
    assert 0 < ledger.filter(first_week, pl.col("bet")).height < 21
    dd = season.report(ledger=ledger, settlements=lost)["alerts"]["drawdown"]
    twenty_first = lost.sort("result_utc")["result_utc"][20]
    assert (dd["triggered"], dd["first_utc"]) == (True, twenty_first)


def test_u_outside_its_training_range_is_counted() -> None:
    season = Season(n_weeks=1)
    ledger = season.ledger.with_columns(
        u_sd=pl.when(pl.col("game_id") < 2026020004).then(2.5).otherwise(0.0),
        goalie_doubt=pl.when(pl.col("game_id") == 2026020001).then(0.7).otherwise(0.3),
    )
    u = season.report(ledger=ledger)["alerts"]["u_range"]
    assert (u["games"], u["u_beyond_2_sd"], u["u_beyond_3_sd"]) == (30, 3, 0)
    assert u["goalie_doubt_beyond_3_sd"] == 1


def test_only_the_frozen_policys_regular_season_rows_count() -> None:
    season = Season(n_weeks=1)
    ledger = season.ledger.with_columns(
        policy_version=pl.when(pl.col("game_id") == 2026020001)
        .then(pl.lit("policy-20270101-0000000"))
        .otherwise("policy_version"),
        game_id=pl.when(pl.col("game_id") == 2026020002)
        .then(pl.lit(2026030002, pl.Int64))
        .otherwise("game_id"),
    )
    assert season.report(ledger=ledger)["coverage"]["slate_games"] == 28
    # And nothing after the report's date.
    assert season.report(as_of=FIRST)["coverage"]["slate_games"] == 5


def test_skipped_days_and_missing_prices_are_alerts() -> None:
    season = Season(n_weeks=1)
    ledger = season.ledger.with_columns(
        status=pl.when(pl.col("game_date") == FIRST)
        .then(pl.lit(lp.NO_SNAPSHOT))
        .when(pl.col("game_id") == 2026020002)
        .then(pl.lit(lp.STALE))
        .otherwise("status")
    )
    result = season.report(ledger=ledger)
    ops = result["alerts"]["operations"]
    assert (ops["days"], ops["days_skipped"], ops["stale_price"]) == (7, 1, 1)
    assert result["coverage"]["not_predicted"] == {lp.NO_SNAPSHOT: 5, lp.STALE: 1}


def test_pinnacle_is_placed_between_sbrs_opener_and_close() -> None:
    season = Season(n_weeks=6)
    history = pl.DataFrame(
        {
            "season": [20212022] * 70,
            "game_date": [date(2021, 10, 11) + timedelta(days=i) for i in range(70)],
            "game_id": list(range(70)),
            "opener_loss": [0.70] * 70,
            "close_loss": [0.68] * 70,
        },
        schema_overrides={"season": pl.Int32},
    )
    timing = season.report(history=history)["market_timing"]
    assert timing["sbr_opener"]["mean"] == pytest.approx(0.70)
    assert timing["sbr_close"]["mean"] == pytest.approx(0.68)
    live = timing["pinnacle_1245"]["mean"]
    assert timing["pinnacle_minus_opener"]["value"] == pytest.approx(live - 0.70)
    assert timing["pinnacle_minus_close"]["value"] == pytest.approx(live - 0.68)


def test_a_clearly_negative_clv_is_unfavourable_at_the_review() -> None:
    season = Season(n_weeks=8)
    season.settlements = season.settlements.with_columns(clv=pl.col("clv") - 0.04)
    review = season.report(as_of=lr.REVIEW_DATE)
    assert review["closing_value"]["clv_per_bet"]["high"] < 0
    assert review["verdicts"]["closing_value"] == lr.UNFAVOURABLE


def test_quote_freshness_is_shown_apart_from_the_lead() -> None:
    season = Season(n_weeks=1)
    proxies = season.settlements.join(
        season.ledger.select("game_id", "event_id"), on="game_id"
    ).select(
        "event_id",
        pl.lit("pinnacle").alias("book"),
        pl.lit("h2h").alias("market"),
        snapshot_utc="close_snapshot_utc",
        last_update_utc=pl.col("close_snapshot_utc") - timedelta(minutes=2),
    )
    odds = pl.concat([proxies, proxies.with_columns(book=pl.lit("bet365"))])
    rows = lr.counted(season.ledger, date(2026, 12, 31))
    fresh = lr.freshness(rows, lr.bets_of(rows, season.settlements), odds)
    assert fresh["decision_quote_age_min"] == {"median": 3.0, "max": 3.0}
    assert fresh["proxy_age_min"] == {"median": 2.0, "max": 2.0}
    assert fresh["proxy_lead_min"] == {"median": 15.0, "max": 15.0}


def test_the_daily_slate_lists_flags_and_lineup_gaps() -> None:
    season = Season(n_weeks=1)
    day = season.ledger.filter(pl.col("game_date") == FIRST).with_columns(
        p_blend=pl.when(pl.col("game_id") == 2026020001)
        .then(pl.col("p_b1") + 0.10)
        .otherwise("p_blend"),
        status=pl.when(pl.col("game_id") == 2026020008).then(pl.lit(lp.STALE)).otherwise("status"),
        ev=pl.when(pl.col("bet")).then(0.03),
    )
    replacements = pl.DataFrame({"team": ["WSH", "PIT"], "role": ["F", "D"], "count": [1.0, 0.0]})
    text = lr.slate_markdown(day, replacements)
    assert "PIT at WSH: the blend is +0.100 from B1, review by hand (hard rule 8)" in text
    assert f"PIT at WSH: not predicted, {lp.STALE}" in text
    assert "- WSH: 1.0 forwards" in text and "PIT: 0.0" not in text
    assert "home at 2.00, EV +0.030, stake 1.00" in text


def test_nhl_live_report_and_slate(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from nhl_edge.cli import app
    from nhl_edge.lake.schemas import LineupReplacements
    from nhl_edge.lake.tables import TABLES, Lake

    season = Season(n_weeks=5)
    tables = {
        "paper_ledger": season.ledger.with_columns(ev=pl.when(pl.col("bet")).then(0.03)),
        "paper_settlements": season.settlements,
        "games": season.games,
        "odds_snapshots": TABLES["odds_snapshots"].empty(),
        "sbr_odds": TABLES["sbr_odds"].empty(),
        "lineup_replacements": pl.DataFrame(schema=dtypes(LineupReplacements)),
        "challenger_predictions": TABLES["challenger_predictions"].empty(),
    }

    def read(self: Lake, table: str, seasons: Any = None) -> pl.DataFrame:
        return tables[table]

    monkeypatch.setattr(Lake, "read", read)
    monkeypatch.setattr(lr, "sbr_history", lambda sbr, games: pl.DataFrame(schema=HISTORY))
    runner = CliRunner()
    out = tmp_path / "live"
    result = runner.invoke(app, ["live", "report", "--as-of", "2026-12-31", "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert "interim report 2026-12-31: 150 slate games, 75 bets, 75 of 75 eligible" in result.output
    assert (out / "report-2026-12-31.json").exists()
    assert "Interim report" in (out / "report-2026-12-31.md").read_text()
    slate = runner.invoke(app, ["live", "slate", "--date", FIRST.isoformat()])
    assert slate.exit_code == 0, slate.output
    assert f"# Slate, {FIRST}" in slate.output and "Lineup gaps" in slate.output
    assert runner.invoke(app, ["live", "slate", "--date", "2026-09-01"]).exit_code == 1


def test_nhl_live_report_upserts_the_report_into_supabase(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Codex on #197: `--supabase` sends the day's report, whole, to live_reports keyed on as_of.
    import json

    import httpx
    from typer.testing import CliRunner

    from nhl_edge.cli import app
    from nhl_edge.lake.supabase import Supabase
    from nhl_edge.lake.tables import TABLES, Lake

    season = Season(n_weeks=5)
    tables = {
        "paper_ledger": season.ledger,
        "paper_settlements": season.settlements,
        "games": season.games,
        "odds_snapshots": TABLES["odds_snapshots"].empty(),
        "sbr_odds": TABLES["sbr_odds"].empty(),
        "challenger_predictions": TABLES["challenger_predictions"].empty(),
    }
    monkeypatch.setattr(Lake, "read", lambda self, table, seasons=None: tables[table])
    monkeypatch.setattr(lr, "sbr_history", lambda sbr, games: pl.DataFrame(schema=HISTORY))
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    url = "https://abcdefghijklmnop.supabase.co"
    monkeypatch.setattr(
        Supabase, "from_env", classmethod(lambda cls: Supabase(url, "sb_secret_x", client))
    )
    out = tmp_path / "live"
    result = CliRunner().invoke(
        app, ["live", "report", "--as-of", "2026-12-31", "--out", str(out), "--supabase"]
    )
    assert result.exit_code == 0, result.output
    assert "supabase live_reports: the 2026-12-31 report upserted" in result.output
    [request] = requests
    assert request.method == "POST"
    assert str(request.url) == f"{url}/rest/v1/live_reports?on_conflict=as_of"
    assert request.headers["Prefer"] == "resolution=merge-duplicates,return=minimal"
    [row] = json.loads(request.content)
    assert row["as_of"] == "2026-12-31" and row["kind"] == "interim"
    assert row["policy_version"] == POLICY_VERSION
    assert row["code_version"].startswith("live-report-")
    # The report as the committed JSON file has it, nested figures and all.
    assert row["report"] == json.loads((out / "report-2026-12-31.json").read_text())
    assert row["report"]["coverage"]["slate_games"] == 150
    assert "low" in row["report"]["closing_value"]["clv_per_bet"]


def test_the_review_needs_every_bet_settled_and_every_game_scored() -> None:
    # Codex on #191: at the formal review, a bet still unsettled or a game still without a
    # result leaves insufficient evidence, whatever the rest shows.
    season = Season(n_weeks=8)
    unsettled = season.settlements.filter(pl.col("game_id") != 2026020001)
    review = season.report(as_of=lr.REVIEW_DATE, settlements=unsettled)
    assert review["closing_value"]["clv_per_bet"]["low"] > 0
    assert review["verdicts"]["closing_value"] == lr.INSUFFICIENT
    assert review["verdicts"]["calibration"] == lr.INSUFFICIENT
    assert review["verdicts"]["incomplete"] == {"awaiting_settlement": 1}
    assert "The evidence is incomplete: 1 awaiting settlement." in lr.markdown(review)
    season.games = season.games.filter(pl.col("game_id") != 2026020002)
    review = season.report(as_of=lr.REVIEW_DATE)
    assert review["coverage"]["awaiting_result"] == 1
    assert review["verdicts"]["incomplete"] == {"awaiting_result": 1}
    # A slate game never predicted counts too (Codex on #191): every game, not only the scored.
    skipped = season.ledger.with_columns(
        status=pl.when(pl.col("game_id") == 2026020002).then(pl.lit(lp.STALE)).otherwise("status"),
        bet=pl.when(pl.col("game_id") == 2026020002).then(False).otherwise("bet"),
    )
    review = season.report(as_of=lr.REVIEW_DATE, ledger=skipped)
    assert review["verdicts"]["incomplete"] == {"awaiting_result": 1}
    assert review["verdicts"]["closing_value"] == lr.INSUFFICIENT


def test_a_postponed_games_first_forecast_is_not_scored() -> None:
    # Codex on #191: decided for its first date, played two days later and decided again; only
    # the forecast for the date it was played is scored, once.
    season = Season(n_weeks=6)
    moved = 2026020005
    first = season.ledger.filter(pl.col("game_id") == moved)
    again = first.with_columns(
        game_date=pl.col("game_date") + timedelta(days=2),
        start_utc=pl.col("start_utc") + timedelta(days=2),
    )
    season.games = season.games.with_columns(
        start_utc=pl.when(pl.col("game_id") == moved)
        .then(pl.col("start_utc") + timedelta(days=2))
        .otherwise("start_utc")
    )
    ledger = pl.concat([season.ledger, again])
    rows = lr.counted(ledger, date(2026, 12, 31))
    scored = lr.with_results(rows, season.games)
    assert scored.filter(pl.col("game_id") == moved)["game_date"].to_list() == [
        again["game_date"].item()
    ]
    compared = season.report(ledger=ledger)["comparisons"]
    assert compared["BLEND - B1"]["difference"]["games"] == 180


def test_the_slate_reads_a_dry_runs_date_and_flags_only_stopped_picks(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from nhl_edge.cli import app
    from nhl_edge.lake.schemas import LineupReplacements
    from nhl_edge.lake.tables import Lake

    season = Season(n_weeks=1)
    day = FIRST + timedelta(days=2)
    dry = season.ledger.filter(pl.col("game_date") == day).with_columns(
        ev=pl.when(pl.col("bet")).then(0.03),
        moved_against=pl.lit(0.05),
        # The guard's move is logged on every prediction; only a pick can be stopped.
        guarded=pl.lit(True),
    )
    path = tmp_path / f"{day}.parquet"
    dry.write_parquet(path)
    monkeypatch.setattr(
        Lake,
        "read",
        lambda self, table, seasons=None: pl.DataFrame(schema=dtypes(LineupReplacements)),
    )
    result = CliRunner().invoke(app, ["live", "slate", "--from", str(path)])
    assert result.exit_code == 0, result.output
    assert f"# Slate, {day}" in result.output
    stopped = result.output.count("the guard stopped")
    assert stopped == dry.filter(pl.col("picked")).height > 0


def test_the_slate_says_when_r2_has_no_ledger(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fakes import MemoryBucket
    from typer.testing import CliRunner

    from nhl_edge.cli import app
    from nhl_edge.lake.r2 import R2_ENV, R2Config

    for name in R2_ENV:
        monkeypatch.setenv(name, "test")
    monkeypatch.setattr("nhl_edge.settings.load_env", lambda: None)
    monkeypatch.setattr(R2Config, "client", lambda self: MemoryBucket())
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["live", "slate", "--date", "2026-10-12", "--r2"])
    assert result.exit_code == 1
    assert "no ledger for 2026-10-12 in R2" in result.output


def test_sbr_history_scores_the_opener_and_close_of_its_seasons_only() -> None:
    # Codex's P0 on #191: the history ADR 0030's check reads, built from SBR's rows. 2017-18
    # only bounds 2018-19's openers (ADR 0007), and 2022-23 is never read.
    from market_history import season as sbr_season

    from nhl_edge.backtest.metrics import EPSILON
    from nhl_edge.market.devig import fair_probabilities

    built = [sbr_season(s, 40, seed) for seed, s in enumerate((20172018, 20182019, 20222023))]
    odds = pl.concat([b[0] for b in built])
    games = pl.concat([b[1] for b in built])
    planted = 2018020005
    odds = odds.with_columns(
        price_decimal=pl.when((pl.col("game_id") == planted) & (pl.col("quote") == "open"))
        .then(pl.when(pl.col("side") == "home").then(1.099).otherwise(8.05))
        .otherwise("price_decimal")
    )
    history = lr.sbr_history(odds, games)
    assert set(history["season"]) == {20182019}
    # The planted opener, Edmonton -1010 against Minnesota 705 (#56), is refused with its game.
    assert history.height == 39 and planted not in set(history["game_id"])

    def loss(game_id: int, quote: str) -> float:
        rows = odds.filter(pl.col("game_id") == game_id, pl.col("quote") == quote)
        pair = [
            [
                rows.filter(pl.col("side") == side)["price_decimal"].item()
                for side in ("home", "away")
            ]
        ]
        p = min(max(float(fair_probabilities(pair)[0, 0]), EPSILON), 1 - EPSILON)
        won = games.filter(pl.col("game_id") == game_id)
        y = int(won["home_score"].item() > won["away_score"].item())
        return -(y * np.log(p) + (1 - y) * np.log(1 - p))

    row = history.filter(pl.col("game_id") == 2018020001).row(0, named=True)
    assert row["opener_loss"] == pytest.approx(loss(2018020001, "open"))
    assert row["close_loss"] == pytest.approx(loss(2018020001, "close"))


def test_the_report_goes_to_supabase_as_json() -> None:
    # #168: the dashboard reads the report's own figures, never computing any itself.
    import json

    season = Season(n_weeks=5)
    lost = season.settlements.with_columns(profit=pl.lit(-1.0))
    result = season.report(settlements=lost)
    row = lr.record(result, "live-report-20261008-abc1234")
    assert row["as_of"] == "2026-12-31" and row["kind"] == "interim"
    assert row["policy_version"] == POLICY_VERSION
    # JSON-ready throughout: dates and datetimes as strings, as the committed file has them.
    assert json.loads(json.dumps(row["report"])) == row["report"]
    assert isinstance(row["report"]["alerts"]["drawdown"]["first_utc"], str)


def test_a_report_row_has_the_live_reports_columns() -> None:
    import re
    from pathlib import Path

    migration = Path("supabase/migrations/20261008140000_live_reports.sql").read_text()
    body = re.search(r"create table public\.live_reports \((.*?)\n\);", migration, re.S)
    assert body is not None
    column = re.compile(r"^  ([a-z_0-9]+) (?:date|text|jsonb|timestamptz)\b")
    columns = {m.group(1) for line in body.group(1).splitlines() if (m := column.match(line))}
    row = lr.record(Season(n_weeks=1).report(), "live-report-20261008-abc1234")
    assert set(row) == columns - {"created_at"}
    # The kinds the migration's check admits.
    assert re.search(r"kind in \('interim', 'formal review'\)", migration)
    assert row["kind"] in ("interim", "formal review")


def moves(n_weeks: int, toward: float, per_week: int = 30, seed: int = 11) -> pl.DataFrame:
    """movement.table's rows for a synthetic season (#251): each game's B3 disagreement at the
    decision, and a move to the close of toward times it plus noise, in log-odds."""
    rng = np.random.default_rng(seed)
    n = n_weeks * per_week
    d = rng.normal(0, 0.3, n)
    move = toward * d + rng.normal(0, 0.05, n)
    p_decision = 0.5
    return pl.DataFrame(
        {
            "season": [20262027] * n,
            "game_date": [FIRST + timedelta(days=7 * (i // per_week) + i % 7) for i in range(n)],
            "close_status": [closing.PROXY] * n,
            "p_decision": [p_decision] * n,
            "p_close": 1 / (1 + np.exp(-move)),
            "move": move,
            "u_sd": np.where(np.arange(n) % 2 == 0, -0.5, 0.5),
            "d_B3": d,
            "d_BLEND": 0.5 * d,
        }
    )


def test_a_market_moving_toward_b3_shows_as_its_slope_share_and_points() -> None:
    result = lr.entry_to_close(moves(n_weeks=6, toward=0.4))
    b3 = result["models"]["B3"]
    assert b3["slope"]["low"] < 0.4 < b3["slope"]["high"]
    assert b3["share_toward"]["low"] > 0.5 and b3["points_toward"]["low"] > 0
    # The blend's disagreement is half B3's here, so the market closes twice its share of it.
    assert result["models"]["BLEND"]["slope"]["value"] == pytest.approx(2 * b3["slope"]["value"])
    halves = result["b3_by_u"]
    assert halves["u at or below average"]["slope"]["games"] == 90
    # A market that ignores B3: the slope's interval holds 0.
    ignored = lr.entry_to_close(moves(n_weeks=6, toward=0.0))["models"]["B3"]["slope"]
    assert ignored["low"] < 0 < ignored["high"]


def test_a_disagreement_without_spread_gives_counts_not_a_slope() -> None:
    # A blend that always matches the market (Codex on #261): no slope, and the report stands.
    flat = moves(n_weeks=6, toward=0.4).with_columns(d_BLEND=pl.lit(0.0))
    blend = lr.entry_to_close(flat)["models"]["BLEND"]
    assert blend["slope"] == {"games": 180, "weeks": 6}
    assert blend["share_toward"] == {"games": 0, "weeks": 0}


def test_entry_to_close_gives_counts_only_under_four_weeks() -> None:
    result = lr.entry_to_close(moves(n_weeks=3, toward=0.4))
    assert result["models"]["B3"]["slope"] == {"games": 90, "weeks": 3}
    assert "mean" not in result["move_points"]


def test_the_report_shows_entry_to_close_and_why_games_have_no_close() -> None:
    # Without any odds, no game has a close: each counts as missing, and the section says so.
    result = Season(n_weeks=2).report()
    moved = result["entry_to_close"]
    assert moved["games"] == 0 and moved["without_close"] == {closing.MISSING: 60}
    text = lr.markdown(result)
    assert "## Entry to close (#251)" in text
    # A started game counts before its result is in (Codex on #261).
    season = Season(n_weeks=2)
    season.games = season.games.head(50)
    assert season.report()["entry_to_close"]["without_close"] == {closing.MISSING: 60}
    # A game not started by the report's clock doesn't count yet: by 23:30 on the first day,
    # only that day's five 23:00 games have.
    early = Season(n_weeks=2).report(now=at(FIRST, 23, 30))["entry_to_close"]
    assert early["without_close"] == {closing.MISSING: 5}
    assert f"Without a close: {closing.MISSING} 60." in text
    assert "| B3 | 0 games, 0 weeks: too few |" in text


def test_the_report_shows_the_shadows_apart_from_its_verdicts() -> None:
    # Without odds no game has a close, so neither shadow values a bet yet (#252).
    result = Season(n_weeks=2).report()
    shadows = result["shadows"]
    assert shadows["market_only"]["bets"] == 0 and shadows["best_price"]["bets"] == 0
    assert shadows["market_only"]["policy_minus_market_only"] is None
    assert "shadows" not in result["verdicts"]
    text = lr.markdown(result)
    assert "## Shadow evaluations (#252)" in text
    assert "withheld until 4 weeks of both" in text
