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
from nhl_edge.lake.schemas import DailyFaceoffGoalies, PaperLedger, dtypes
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
                    "game_date": day,
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
            unfinished_bundles=changes.pop("unfinished_bundles", None),
            goalie_spreads=changes.pop("goalie_spreads", None),
            dailyfaceoff=changes.pop("dailyfaceoff", None),
            scale_up=changes.pop("scale_up", None),
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
            fit=pl.lit(None, pl.String),
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


def test_a_fitting_challenger_shows_its_latest_fit_unfitted_weeks_included(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ADR 0041: conservative_blend's weeks without 200 training games are reported, as unfitted.
    import json
    from dataclasses import replace

    from nhl_edge.lake.schemas import ChallengerPredictions
    from nhl_edge.live import challengers as lc

    season = Season(n_weeks=6)
    registered = FIRST + timedelta(days=6)
    fitting = replace(lc.REGISTRY[1], registered_on=registered)
    monkeypatch.setattr(lc, "REGISTRY", (fitting,))
    unfitted = json.dumps({"week": "2026-10-19", "games": 46, "minimum": 200})
    outputs = ChallengerPredictions.validate(
        season.ledger.select(
            "game_date",
            "season",
            "game_id",
            challenger=pl.lit(fitting.name),
            challenger_version=pl.lit("1"),
            adr=pl.lit("0041"),
            p_home=pl.lit(None, pl.Float64),
            status=pl.lit(lc.UNFITTED),
            bundle_sha256=pl.lit("0" * 64),
            input_cutoff_utc=pl.col("decision_snapshot_utc"),
            reads=pl.lit("{}"),
            fit=pl.lit(unfitted),
            code_version=pl.lit("challengers-20261022-abc1234"),
            computed_utc=pl.col("published_utc") + timedelta(hours=12),
        )
    )
    report = season.report(challenger_outputs=outputs)
    entry = report["challengers"]["challengers"]["conservative_blend/1"]
    counted = season.ledger.filter(pl.col("game_date") > registered).height
    assert entry["predicted"] == 0 and entry["not_predicted"] == {lc.UNFITTED: counted}
    assert entry["latest_fit"] == json.loads(unfitted)
    assert entry["comparisons"]["BLEND"]["difference"]["games"] == 0
    text = lr.markdown(report)
    assert f"not predicted: unfitted {counted}." in text
    assert "  - Week of 2026-10-19: unfitted, on 46 training games of the 200 it needs." in text
    # Once fitted, the latest day's weights and standard errors.
    last = season.ledger["game_date"].max()
    fitted = {
        "week": "2026-11-16",
        "games": 230,
        "minimum": 200,
        "weights": {"a": -0.01, "b_m": 1.04, "b_d": 0.21},
        "standard_errors": {"a": 0.04, "b_m": 0.1, "b_d": 0.3},
        "train_cutoff": "2026-11-16T10:00:00+00:00",
    }
    later = outputs.with_columns(
        fit=pl.when(pl.col("game_date") == last).then(pl.lit(json.dumps(fitted))).otherwise("fit")
    )
    text = lr.markdown(season.report(challenger_outputs=later))
    assert (
        "  - Latest fit, week of 2026-11-16, on 230 games: a -0.010 (SE 0.040), "
        "b_m +1.040 (SE 0.100), b_d +0.210 (SE 0.300)." in text
    )


def test_a_challenger_with_a_baseline_is_paired_with_it_and_on_its_goalie_spreads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ADR 0042: goalie_spread_blend's primary measure is against conservative_blend, on every
    # game both predicted and on those with a goalie spread above 0.
    from dataclasses import replace

    from nhl_edge.lake.schemas import ChallengerPredictions
    from nhl_edge.live import challengers as lc

    season = Season(n_weeks=6)
    registered = FIRST + timedelta(days=6)
    base = replace(lc.REGISTRY[1], registered_on=registered)
    spread = replace(lc.REGISTRY[2], registered_on=registered)
    residual = replace(lc.REGISTRY[3], registered_on=registered)
    split = replace(lc.REGISTRY[4], registered_on=registered)
    monkeypatch.setattr(lc, "REGISTRY", (base, spread))

    def outputs(name: str, adr: str, shift: float) -> pl.DataFrame:
        return season.ledger.select(
            "game_date",
            "season",
            "game_id",
            challenger=pl.lit(name),
            challenger_version=pl.lit("1"),
            adr=pl.lit(adr),
            p_home=(pl.col("p_blend") + shift).clip(upper_bound=0.99),
            status=pl.lit(lp.PREDICTED),
            bundle_sha256=pl.lit("0" * 64),
            input_cutoff_utc=pl.col("decision_snapshot_utc"),
            reads=pl.lit("{}"),
            fit=pl.lit(None, pl.String),
            code_version=pl.lit("challengers-20261022-abc1234"),
            computed_utc=pl.col("published_utc") + timedelta(hours=12),
        )

    conservative = outputs("conservative_blend", "0041", 0.05)
    both = ChallengerPredictions.validate(
        pl.concat([conservative, outputs("goalie_spread_blend", "0042", 0.03)])
    )
    # Every other game has a goalie spread above 0.
    spreads = season.ledger.select(
        "game_date", "game_id", goalie_spread=(pl.col("game_id") % 2).cast(pl.Float64) * 0.1
    )
    entry = season.report(challenger_outputs=both, goalie_spreads=spreads)["challengers"]
    compared = entry["challengers"]["goalie_spread_blend/1"]["comparisons"]
    assert sorted(compared) == [
        "B1",
        "BLEND",
        "conservative_blend/1",
        "conservative_blend/1, goalie spread above 0",
    ]
    scored = (
        season.ledger.filter(pl.col("game_date") > registered)
        .join(
            season.games.select(
                "game_id", home_win=(pl.col("home_score") > pl.col("away_score")).cast(pl.Int8)
            ),
            on="game_id",
        )
        .with_columns(
            difference=log_loss((pl.col("p_blend") + 0.03).clip(upper_bound=0.99))
            - log_loss((pl.col("p_blend") + 0.05).clip(upper_bound=0.99))
        )
    )
    figure = compared["conservative_blend/1"]["difference"]
    assert figure["mean"] == pytest.approx(scored["difference"].mean())
    assert figure["games"] == scored.height
    odd = scored.filter(pl.col("game_id") % 2 == 1)
    within = compared["conservative_blend/1, goalie spread above 0"]["difference"]
    assert within["mean"] == pytest.approx(odd["difference"].mean())
    assert within["games"] == odd.height
    # Without the spreads, only the comparison on every game; the baseline itself has none.
    plain = season.report(challenger_outputs=both)["challengers"]["challengers"]
    assert (
        "conservative_blend/1, goalie spread above 0"
        not in plain["goalie_spread_blend/1"]["comparisons"]
    )
    assert sorted(plain["conservative_blend/1"]["comparisons"]) == ["B1", "BLEND"]
    text = lr.markdown(season.report(challenger_outputs=both, goalie_spreads=spreads))
    assert "  - Against conservative_blend/1, goalie spread above 0: " in text
    # A day without the baseline's output withholds the pairing (ADR 0038 rule 2), not the rest.
    day = sorted(scored["game_date"].unique().to_list())[2]
    gone = (pl.col("challenger") == "conservative_blend") & (pl.col("game_date") == day)
    holed = both.filter(~gone)
    held = season.report(challenger_outputs=holed, goalie_spreads=spreads)
    entry = held["challengers"]["challengers"]["goalie_spread_blend/1"]
    assert entry["baseline_missing_days"] == [day.isoformat()]
    assert sorted(entry["comparisons"]) == ["B1", "BLEND"]
    assert (
        f"  - **Against conservative_blend/1: withheld.** Its outputs are missing for 1 decision "
        f"days ({day.isoformat()})" in lr.markdown(held)
    )
    # A challenger that reads no spreads is paired with its baseline on every game only.
    monkeypatch.setattr(lc, "REGISTRY", (base, residual))
    team = ChallengerPredictions.validate(
        pl.concat([conservative, outputs("team_residual_blend", "0043", 0.03)])
    )
    entry = season.report(challenger_outputs=team, goalie_spreads=spreads)["challengers"]
    compared = entry["challengers"]["team_residual_blend/1"]["comparisons"]
    assert sorted(compared) == ["B1", "BLEND", "conservative_blend/1"]
    assert compared["conservative_blend/1"]["difference"]["mean"] == pytest.approx(
        scored["difference"].mean()
    )
    # A ledger model as the baseline (ADR 0044): B3's own p_b3, on every game, never withheld.
    monkeypatch.setattr(lc, "REGISTRY", (split,))
    outputs_b3 = ChallengerPredictions.validate(outputs("b3_split", "0044", 0.03))
    entry = season.report(challenger_outputs=outputs_b3, goalie_spreads=spreads)["challengers"]
    split_entry = entry["challengers"]["b3_split/1"]
    assert sorted(split_entry["comparisons"]) == ["B1", "B3", "BLEND"]
    assert split_entry["baseline_missing_days"] == []
    against = scored.with_columns(
        difference=log_loss((pl.col("p_blend") + 0.03).clip(upper_bound=0.99))
        - log_loss(pl.col("p_b3"))
    )
    figure = split_entry["comparisons"]["B3"]["difference"]
    assert figure["mean"] == pytest.approx(against["difference"].mean())
    assert figure["games"] == against.height


def log_loss(p: pl.Expr) -> pl.Expr:
    y = pl.col("home_win")
    return -(y * p.log() + (1 - y) * (1 - p).log())


def test_the_real_stakes_section_states_adr_0039s_start_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #267: each condition with its figure, the owner's veto and the verdict, in the frozen
    # policy's report only.
    from nhl_edge.betting.selection import POLICY_V1
    from nhl_edge.live import real

    season = Season(n_weeks=5)
    # Every settled bet won its stake: the paper bankroll ends above its 100 units.
    season.settlements = season.settlements.with_columns(profit=pl.lit(1.0))
    monday = date(2026, 11, 16)
    result = season.report(as_of=monday, unfinished_bundles=[])
    section = result["real_stakes"]
    a, c, d = (section["conditions"][name] for name in "acd")
    assert a["measured"] and a["holds"] and a["share_with_proxy"] == 1.0
    assert c["holds"] and c["undecided_days"] == [] and not d["holds"]
    veto = section["veto"]
    assert veto["paper_bankroll"] == pytest.approx(100 + season.settlements.height)
    assert not veto["applies"]
    assert section["verdict"] == "does not hold: (d)"
    assert section["clv_stop"] == {"measured": True, "weekly": True, "stop": False, "pause": False}
    text = lr.markdown(result)
    assert "## Real stakes (ADR 0039)" in text and "**Start rule: does not hold: (d).**" in text
    assert "- (d) Venue: not ready (#266): does not hold." in text
    assert "so the veto is lifted" in text
    assert "Real stakes follow only ADR 0039's start rule" in text
    # With a venue ready, the rule holds; with the bundles unchecked, (c) does not.
    monkeypatch.setattr(real, "VENUE", "pinnacle")
    assert season.report(as_of=monday, unfinished_bundles=[])["real_stakes"]["verdict"] == ("holds")
    assert season.report(as_of=monday)["real_stakes"]["failing"] == ["c"]
    assert "run bundles not checked" in lr.markdown(season.report(as_of=monday))
    # A bundle unfinished after as_of doesn't count (Codex on #273).
    later_day = [monday + timedelta(days=3)]
    assert season.report(as_of=monday, unfinished_bundles=later_day)["real_stakes"]["failing"] == []
    # A day with games and no decision at all is a day skipped without a reason (Codex on #273).
    first_v2_day = date(2026, 10, 15)
    gap = season.ledger.filter(pl.col("game_date") != first_v2_day)
    holed = season.report(as_of=monday, ledger=gap, unfinished_bundles=[])["real_stakes"]
    assert holed["conditions"]["c"]["undecided_days"] == [first_v2_day.isoformat()]
    assert holed["failing"] == ["c"]
    # A nightly report between the weekly ones decides nothing.
    tuesday = season.report(as_of=monday + timedelta(days=1), unfinished_bundles=[])
    assert tuesday["real_stakes"]["verdict"] == (
        "decided at the weekly report, the next on 2026-11-23"
    )
    assert tuesday["real_stakes"]["clv_stop"]["stop"] is None
    # Before the window and after it.
    early = season.report(as_of=date(2026, 11, 2), unfinished_bundles=[])["real_stakes"]
    assert early["verdict"] == "not before 2026-11-16"
    late = season.report(unfinished_bundles=[])["real_stakes"]
    assert late["verdict"].startswith("the window has closed")
    # The paper bankroll is the season's: an earlier policy's settled bets count in it.
    older = season.ledger.with_columns(policy_version=pl.lit(POLICY_V1))
    v1_only = season.report(as_of=monday, ledger=older, unfinished_bundles=[])
    assert v1_only["real_stakes"]["veto"]["paper_bankroll"] == pytest.approx(
        100 + season.settlements.height
    )
    # An earlier policy's report has no real stakes section, and its banner points to none.
    v1 = season.report(as_of=monday, ledger=older, policy=POLICY_V1, unfinished_bundles=[])
    assert v1["real_stakes"] is None
    assert "## Real stakes" not in lr.markdown(v1)
    assert "ADR 0039" not in lr.markdown(v1)
    # Only results public by the report's clock count, and a Monday report before Sunday's are
    # public decides nothing (Codex on #273).
    early = datetime(2026, 11, 16, 9, tzinfo=UTC)
    before = season.report(as_of=monday, now=early, unfinished_bundles=[])["real_stakes"]
    public = season.settlements.filter(pl.col("result_utc") <= early).height
    assert public < season.settlements.height
    assert before["veto"]["paper_bankroll"] == pytest.approx(100 + public)
    assert before["verdict"] == (
        "decided at the weekly report, once the previous day's results are public"
    )
    # A 20% fall of the paper bankroll opens the drawdown review, which blocks a start.
    season.settlements = season.settlements.with_columns(profit=pl.lit(-1.0))
    fallen = season.report(as_of=monday, unfinished_bundles=[])["real_stakes"]
    assert fallen["drawdown_review"]["applies"] and fallen["failing"] == []
    assert fallen["verdict"] == (
        "the rule holds, but the owner's veto applies and the drawdown review is open"
    )


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
        "goalie_spreads": TABLES["goalie_spreads"].empty(),
        "dailyfaceoff_goalies": TABLES["dailyfaceoff_goalies"].empty(),
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
        "goalie_spreads": TABLES["goalie_spreads"].empty(),
        "dailyfaceoff_goalies": TABLES["dailyfaceoff_goalies"].empty(),
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


def leads(n_weeks: int, per_hour: float, per_week: int = 30, seed: int = 7) -> pl.DataFrame:
    """movement.table's rows for a synthetic season (#254), each with its proxy's lead
    (lead_min, as proxy_leads gives it): taken 10 to 80 minutes before the start, with a fair move
    to it of 1 point plus per_hour points per hour of lead, plus noise."""
    rng = np.random.default_rng(seed)
    n = n_weeks * per_week
    lead = rng.uniform(10, 80, n)
    points = 1 + per_hour * lead / 60 + rng.normal(0, 0.2, n)
    return pl.DataFrame(
        {
            "season": [20262027] * n,
            "game_date": [FIRST + timedelta(days=7 * (i // per_week) + i % 7) for i in range(n)],
            "game_id": list(range(n)),
            "lead_min": lead,
            "p_decision": [0.5] * n,
            "p_close": 0.5 + points / 100,
            "move": points,
        }
    )


def lead_bets(frame: pl.DataFrame, clv_per_hour: float) -> pl.DataFrame:
    """The same games as settled bets with a proxy, their CLV falling clv_per_hour an hour of
    lead."""
    return frame.select(
        "season",
        "game_date",
        "game_id",
        settled=pl.lit(ls.SETTLED),
        close_status=pl.lit(closing.PROXY),
        clv=0.01 - clv_per_hour * pl.col("lead_min") / 60,
    )


def by_lead(games: pl.DataFrame, clv_per_hour: float) -> dict[str, Any]:
    lead = games.select("game_date", "game_id", "lead_min")
    return lr.proxy_lead(games.drop("lead_min"), lead_bets(games, clv_per_hour), lead)


def test_a_move_or_clv_that_changes_with_the_proxys_lead_shows_as_its_slope() -> None:
    games = leads(n_weeks=6, per_hour=0.5)
    result = by_lead(games, clv_per_hour=0.02)
    moved = result["move_points"]
    assert moved["slope_per_hour"]["low"] < 0.5 < moved["slope_per_hour"]["high"]
    bands = {b["band"]: b["estimate"] for b in moved["bands"]}
    assert list(bands) == ["up to 30 min", "30 to 60 min", "over 60 min"]
    assert bands["up to 30 min"]["mean"] < bands["over 60 min"]["mean"]
    assert sum(b["games"] for b in bands.values()) == games.height
    clv = result["clv"]["slope_per_hour"]
    assert clv["low"] < -0.02 < clv["high"]
    # A proxy whose lead changes nothing: the slope's interval holds 0.
    still = by_lead(leads(n_weeks=6, per_hour=0.0), 0.0)["move_points"]["slope_per_hour"]
    assert still["low"] < 0 < still["high"]


def test_the_proxys_lead_gives_counts_only_under_four_weeks() -> None:
    result = by_lead(leads(n_weeks=3, per_hour=0.5), 0.02)
    assert result["move_points"]["slope_per_hour"] == {"games": 90, "weeks": 3}
    assert all("mean" not in b["estimate"] for b in result["clv"]["bands"])


def test_the_proxys_lead_is_against_the_start_it_was_chosen_by() -> None:
    # The Odds API moved the start: the ledger's says 20 minutes, the proxy's own start 40
    # (Codex on #285). The game counts in its proxy's band, 30 to 60 minutes.
    start = datetime(2026, 10, 12, 23, tzinfo=UTC)
    rows = pl.DataFrame(
        {
            "game_date": [FIRST],
            "game_id": [1],
            "event_id": ["e1"],
            "start_utc": [start],
            "decision_snapshot_utc": [start - timedelta(hours=6)],
            "home_price": [2.0],
            "away_price": [1.9],
        }
    )
    proxy = start + timedelta(minutes=20) - timedelta(minutes=40)
    # The day's decision snapshot (12:50 ET), which a close has to follow, then the proxy.
    decision = datetime(2026, 10, 12, 16, 50, tzinfo=UTC)
    quotes = pl.DataFrame(
        [
            {
                "snapshot_utc": snapshot,
                "last_update_utc": snapshot,
                "event_id": "e1",
                "commence_time_utc": start + timedelta(minutes=20),
                "home": "BOS",
                "away": "TOR",
                "book": closing.BOOK,
                "market": "h2h",
                "side": side,
                "line": None,
                "price_decimal": price,
                "is_closing_proxy": False,
                "slot": "pre7",
                "raw_key": "k",
                "game_id": 1,
            }
            for snapshot in (decision, proxy)
            for side, price in (("home", 2.0), ("away", 1.9))
        ]
    )
    lead = lr.proxy_leads(rows, quotes, start + timedelta(hours=2))
    assert lead["lead_min"].to_list() == [40.0]


def test_the_report_shows_the_proxys_lead() -> None:
    # Without odds no game has a proxy whose lead is known, so no band has a game or bet yet.
    result = Season(n_weeks=2).report()
    assert result["proxy_lead"]["move_points"]["slope_per_hour"] == {"games": 0, "weeks": 0}
    assert result["proxy_lead"]["clv"]["slope_per_hour"] == {"games": 0, "weeks": 0}
    text = lr.markdown(result)
    assert "## The closing proxy's lead (#254)" in text
    assert "| up to 30 min | 0 games, 0 weeks: too few |" in text
    assert "- Slope on the lead, per hour: the move 0 games, 0 weeks: too few, CLV " in text


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


def test_the_edge_bids_shadow_counts_its_bids_and_waits_for_fills() -> None:
    # #308: without later snapshots no bid fills, so its CLV is counts only, and without Daily
    # Faceoff's polls the starter share is not read.
    season = Season(n_weeks=2)
    result = season.report()
    edge = result["shadows"]["edge_bids"]
    assert edge["bids"] > 0 and edge["filled"] == 0 and edge["with_close"] == 0
    assert edge["clv_per_fill"] == {"games": 0, "weeks": 0}
    assert edge["starter_changed"] is None
    text = lr.markdown(result)
    assert "**Edge bids on every game (#308):**" in text
    assert f"- Bids: {edge['bids']}; filled: 0 of {edge['bids']} (0.0%); with a close: 0." in text
    assert "- Filled after a named starter changed: not read." in text
    polled = season.report(dailyfaceoff=pl.DataFrame(schema=dtypes(DailyFaceoffGoalies)))
    assert polled["shadows"]["edge_bids"]["starter_changed"] == {"games": 0, "weeks": 0}
    polled_text = lr.markdown(polled)
    assert "- Filled after a named starter changed: 0 fills over 0 weeks" in polled_text


def test_the_edge_bids_shadow_values_its_fills_by_week() -> None:
    # A later fresh Pinnacle quote high on both sides fills every bid of a game (#308), and with
    # a close each fill is valued, by week as the bootstrap resamples them.
    season = Season(n_weeks=3)
    rows = season.ledger.filter(pl.col("status") == lp.PREDICTED)
    quotes = []
    for row in rows.iter_rows(named=True):
        for at, slot, proxy in (
            (row["decision_snapshot_utc"], "midday", False),
            (row["start_utc"] - timedelta(minutes=60), "pre7", False),
            (row["start_utc"] - timedelta(minutes=15), "pre8", True),
        ):
            for side in ("home", "away"):
                quotes.append(
                    {
                        "snapshot_utc": at,
                        "last_update_utc": at,
                        "event_id": row["event_id"],
                        "commence_time_utc": row["start_utc"],
                        "home": row["home"],
                        "away": row["away"],
                        "book": "pinnacle",
                        "market": "h2h",
                        "side": side,
                        "line": None,
                        "price_decimal": 9.0 if slot == "pre7" else 1.95,
                        "is_closing_proxy": proxy,
                        "slot": slot,
                        "raw_key": "k",
                        "game_id": row["game_id"],
                    }
                )
    odds = pl.DataFrame(quotes, schema=ODDS_FRAME_SCHEMA | {"game_id": pl.Int64})
    edge = season.report(odds=odds)["shadows"]["edge_bids"]
    assert edge["filled"] == edge["bids"] > 0 and edge["slots"] == {"pre7": edge["filled"]}
    # The fixture's 23:00 UTC starts come before the pre-game slots once DST ends, so those games
    # have no proxy and their fills no close; the fills with one span under four weeks.
    assert 0 < edge["with_close"] <= edge["filled"]
    assert edge["clv_per_fill"] == {"games": edge["with_close"], "weeks": 3}
    assert edge["fair_move_per_fill"]["games"] == edge["with_close"]


def test_the_selected_bets_split_gives_counts_under_four_weeks() -> None:
    # The fixture's games without a bet have no side at the decision: they are left out (#253).
    season = Season(n_weeks=2)
    sided = season.ledger.with_columns(
        p_side=pl.when(pl.col("side").is_not_null()).then(pl.col("p_blend"))
    )
    result = season.report(ledger=sided)
    section = result["selected_calibration"]
    assert section["without_a_side"] == 30 and section["games"] == 30
    assert section["selected"]["bets"] == {"games": 30, "weeks": 2}
    assert section["selected"]["other games"] == {"games": 0, "weeks": 0}
    text = lr.markdown(result)
    assert "## Calibration on the selected bets (#253)" in text
    assert "| selected | bets | 30 over 2 weeks | too few weeks | | |" in text
    assert "- 30 games without a side at the decision's price are left out" in text


def test_the_selected_bets_split_scores_each_game_on_its_side_from_four_weeks() -> None:
    season = Season(n_weeks=6)
    every = season.ledger.with_columns(
        side=pl.lit("home"),
        price=pl.lit(1.9),
        p_side=pl.col("p_blend"),
        u_sd=pl.when(pl.col("game_id") % 3 == 0).then(-0.5).otherwise(0.5),
    )
    section = season.report(ledger=every)["selected_calibration"]
    assert section["without_a_side"] == 0 and section["games"] == 180
    bets, other = section["selected"]["bets"], section["selected"]["other games"]
    assert bets["games"] == other["games"] == 90 and bets["weeks"] == 6
    # p_b1 equals p_blend in the fixture, so the blend ties B1 on every game
    assert bets["log_loss_minus_b1"]["mean"] == 0.0 and "slope" in bets
    # home at 1.9 against away at 2.0 is the favourite in every game
    assert section["price"]["favourites"]["games"] == 180
    assert section["price"]["underdogs"] == {"games": 0, "weeks": 0}
    assert section["u"]["u below its training mean"]["games"] == 60


def pinnacle_closes(season: "Season", close: tuple[float, float]) -> pl.DataFrame:
    """Pinnacle's midday quote of each predicted game at the ledger's prices, and a proxy at
    close before its start, at the pre7 slot."""
    quotes = []
    for row in season.ledger.filter(pl.col("status") == lp.PREDICTED).iter_rows(named=True):
        for when, slot, prices in (
            (row["decision_snapshot_utc"], "midday", (row["home_price"], row["away_price"])),
            (row["start_utc"] - timedelta(minutes=15), "pre7", close),
        ):
            for side, price in zip(("home", "away"), prices, strict=True):
                quotes.append(
                    {
                        "snapshot_utc": when,
                        "last_update_utc": when,
                        "event_id": row["event_id"],
                        "commence_time_utc": row["start_utc"],
                        "home": row["home"],
                        "away": row["away"],
                        "book": "pinnacle",
                        "market": "h2h",
                        "side": side,
                        "line": None,
                        "price_decimal": price,
                        "is_closing_proxy": slot == "pre7",
                        "slot": slot,
                        "raw_key": "k",
                        "game_id": row["game_id"],
                    }
                )
    return pl.DataFrame(quotes, schema=ODDS_FRAME_SCHEMA | {"game_id": pl.Int64})


def test_a_challenger_forecasting_the_close_is_scored_on_the_move_to_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ADR 0049: close_model's primary measure is its forecast move's squared error less no
    # move's, and its shadow bets are the policy's own its forecast close clears.
    from dataclasses import replace

    from nhl_edge.lake.schemas import ChallengerPredictions
    from nhl_edge.live import challengers as lc
    from nhl_edge.live import movement, shadow

    season = Season(n_weeks=3)
    registered = FIRST - timedelta(days=1)
    close_model = replace(lc.REGISTRY[5], registered_on=registered)
    monkeypatch.setattr(lc, "REGISTRY", (close_model,))
    odds = pinnacle_closes(season, (1.80, 2.10))

    def outputs(p_home: pl.Expr) -> pl.DataFrame:
        return ChallengerPredictions.validate(
            season.ledger.select(
                "game_date",
                "season",
                "game_id",
                challenger=pl.lit("close_model"),
                challenger_version=pl.lit("1"),
                adr=pl.lit("0049"),
                p_home=p_home,
                status=pl.lit(lp.PREDICTED),
                bundle_sha256=pl.lit("0" * 64),
                input_cutoff_utc=pl.col("decision_snapshot_utc"),
                reads=pl.lit("{}"),
                fit=pl.lit(None, pl.String),
                code_version=pl.lit("challengers-20261030-abc1234"),
                computed_utc=pl.col("published_utc") + timedelta(hours=12),
            )
        )

    # It forecasts the home side's fair close at 0.52, every game.
    result = season.report(odds=odds, challenger_outputs=outputs(pl.lit(0.52)))
    entry = result["challengers"]["challengers"]["close_model/1"]
    assert entry["forecasts"] == lc.CLOSE
    rows = lr.counted(season.ledger, date(2026, 12, 31))
    started = lr.scoreable(rows, season.games)
    moves = movement.table(started, ls.closes(odds, at(date(2027, 1, 1), 12)))
    with_close = moves.filter(pl.col("move").is_not_null())
    assert with_close.height > 0
    figure = entry["move"]["difference"]
    assert figure["games"] == with_close.height and figure["weeks"] < 4
    # Over 4 weeks of games, the figure is the mean of (y - ŷ)² - y².
    forecast = float(np.log(0.52 / 0.48)) - np.log(
        with_close["p_decision"] / (1 - with_close["p_decision"])
    )
    frame = with_close.with_columns(d_challenger=forecast)
    weeks = pl.concat(
        [frame.with_columns(pl.col("game_date") + timedelta(weeks=k)) for k in range(4)]
    )
    y, y_hat = weeks["move"].to_numpy(), weeks["d_challenger"].to_numpy()
    measured = lr._move_figures(weeks)["difference"]
    assert measured["mean"] == pytest.approx(np.mean((y - y_hat) ** 2 - y**2))
    # Its shadow bets: the policy's own, kept where its forecast close clears their price. A
    # forecast far above any price keeps every home bet and no away bet.
    own = shadow.valued(shadow.selected(started, "p_blend"), moves)
    assert own.filter(pl.col("side") == "home").height > 0
    assert own.filter(pl.col("side") == "away").height > 0
    high = season.report(odds=odds, challenger_outputs=outputs(pl.lit(0.99)))
    kept = high["challengers"]["challengers"]["close_model/1"]["shadow"]["bets"]
    assert kept == own.filter(pl.col("side") == "home").height
    low = season.report(odds=odds, challenger_outputs=outputs(pl.lit(0.01)))
    assert low["challengers"]["challengers"]["close_model/1"]["shadow"]["bets"] == (
        own.filter(pl.col("side") == "away").height
    )
    text = lr.markdown(result)
    assert "- Its forecast of the move to the close, squared error less no move's" in text
    assert "- The policy's own bets its forecast close clears, with a close: " in text


def test_the_scale_up_section_shows_counts_and_the_reading_date_only() -> None:
    # ADR 0047 (#322): the CLV figures and the verdict are Kalshi's fills, kept private (#280).
    from nhl_edge.betting.selection import POLICY_V1
    from nhl_edge.live import scale_up as su

    season = Season(n_weeks=2)
    assert season.report()["scale_up"] is None
    assert "## Scale-up (ADR 0047)" not in lr.markdown(season.report())
    sample = {"fills": 30, "weeks": 5, "started": 28, "valued": 26, "coverage": 26 / 28}
    counts = {
        "reading_date": "2027-02-08",
        "due": False,
        "read": False,
        "weeks_needed": su.WEEKS,
        "coverage_needed": su.COVERAGE,
        "samples": {
            "taker": sample,
            "maker": sample | {"fills": 0, "weeks": 0, "started": 0, "valued": 0, "coverage": None},
        },
    }
    text = lr.markdown(season.report(scale_up=counts))
    assert "## Scale-up (ADR 0047)" in text
    assert "- Reading: read once, on 2027-02-08." in text
    assert "- Taker fills: 30 over 5 weeks; 26 of 28 of started games with a reference" in text
    assert "(93%)" in text and "(none yet)" in text
    assert "over at least 12 weeks of fills with at least 90% of them referenced" in text
    read = lr.markdown(season.report(scale_up=counts | {"due": True, "read": True}))
    assert "- Reading: read on 2027-02-08, at the weekly report's clock." in read
    # Due, but an order of a game before the clock has no fills recorded yet (Codex on #351).
    due = lr.markdown(season.report(scale_up=counts | {"due": True}))
    assert "- Reading: due on 2027-02-08: it is taken once every order of a game before" in due
    first = counts | {"reading_date": None}
    assert "no real fill yet: it is read on the Monday 12 weeks after" in lr.markdown(
        season.report(scale_up=first)
    )
    # An earlier policy's report doesn't carry it.
    older = season.ledger.with_columns(policy_version=pl.lit(POLICY_V1))
    assert season.report(ledger=older, policy=POLICY_V1, scale_up=counts)["scale_up"] is None
