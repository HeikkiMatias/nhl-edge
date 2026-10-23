"""#318's point-in-time rule: a real Kalshi fill is valued only against a Pinnacle reference
observed after it, never against one taken at or before it nor against a quote after the start,
and a fill at or after the start is never the bet's (hard rule 2)."""

from datetime import datetime, timedelta
from pathlib import Path

import fill_fixtures as ff
import polars as pl
import predict_fixtures as pf

from nhl_edge.live import bundle as lb
from nhl_edge.live import fills as lf
from nhl_edge.live import placement as pm


def referenced(tmp_path: Path, at: datetime, odds: pl.DataFrame | None = None) -> pl.DataFrame:
    store = lb.LocalStore(tmp_path)
    ff.record(store, [ff.fill(at, "1.00", "0.4800", "0.0044", False)])
    refs = lf.references(ff.odds() if odds is None else odds, ff.AFTER)
    return lf.referenced(lf.fills(store, pm.DEMO), refs, ff.TEAMS, ff.AFTER)


def test_a_fill_at_the_reference_or_after_it_gets_no_clv(tmp_path: Path) -> None:
    second = timedelta(seconds=1)
    before = referenced(tmp_path / "before", ff.PROXY - second)
    assert before["reference"].to_list() == [lf.VALUED]
    assert lf.bids(before).height == 1
    for name, at in (("at", ff.PROXY), ("after", ff.PROXY + second)):
        rows = referenced(tmp_path / name, at)
        assert rows["reference"].to_list() == [lf.AFTER_REFERENCE]
        assert lf.bids(rows).is_empty()


def test_a_quote_after_the_start_never_stands_in_as_the_reference(tmp_path: Path) -> None:
    # A fill after the proxy stays without one, though the Odds API quoted the game again once it
    # had started.
    odds = ff.odds(ff.PROXY, pf.START + timedelta(minutes=1))
    rows = referenced(tmp_path, ff.PROXY + timedelta(minutes=20), odds)
    assert rows["reference"].to_list() == [lf.AFTER_REFERENCE]


def test_a_fill_at_the_start_is_never_the_bets(tmp_path: Path) -> None:
    store = lb.LocalStore(tmp_path)
    ff.record(store, [ff.fill(pf.START, "1.00", "0.4800", "0.0044", False)])
    assert lf.fills(store, pm.DEMO).is_empty()
