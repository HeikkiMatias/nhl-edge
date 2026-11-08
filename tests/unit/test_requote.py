"""Requote after a starter change (ADR 0059, #410) on a SYNTHETIC league's bundle: with no new call
the publication's value comes back; a confirmed backup moves only his game's value; each change is
requoted at its moment, or held and counted by its reason."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest
from requote_fixtures import (
    BACKUP,
    CHANGE,
    CONFIRMED_BACKUP,
    CUTOFF,
    GAME,
    LIVE,
    PUBLISHED,
    STARTS,
    calls,
    games,
    priced,
    publication,
    write_bundle,
)

from nhl_edge.live import blend_fit
from nhl_edge.live import bundle as lb
from nhl_edge.live import kalshi_quotes as kq
from nhl_edge.live import requote as rq


@pytest.fixture(scope="module")
def bundle(tmp_path_factory: pytest.TempPathFactory) -> lb.Bundle:
    return write_bundle(Path(tmp_path_factory.mktemp("bundle")))


def test_with_no_new_call_the_publication_comes_back(bundle: lb.Bundle) -> None:
    published = rq.blend_of(bundle, priced(), LIVE)
    assert published is not None
    same = rq.with_starters(bundle, GAME, CHANGE + rq.REQUOTE_AFTER, calls())
    assert same.inputs["goalie_starts"].equals(bundle.inputs["goalie_starts"])
    assert rq.blend_of(same, priced(), LIVE) == pytest.approx(published, abs=1e-12)


def test_a_confirmed_backup_moves_only_his_games_value(bundle: lb.Bundle) -> None:
    moved = rq.with_starters(bundle, GAME, CHANGE + rq.REQUOTE_AFTER, calls(CONFIRMED_BACKUP))
    rows = moved.inputs["goalie_starts"].filter(pl.col("game_id") == GAME, pl.col("team") == "CHI")
    # ADR 0037's rule: the confirmed starter is certain, and the other candidate drops out.
    assert rows.select("goalie_id", "p_start").rows() == [(BACKUP, 1.0)]
    # The rows keep their own times: the call's is the requote's moment.
    assert rows["observed_utc"].to_list() == [datetime(2018, 10, 19, 14, tzinfo=UTC)]
    before, after = lb.models(bundle), lb.models(moved)
    assert before is not None and after is not None
    p_before = dict(before.b3.select("game_id", "p_b3").iter_rows())
    p_after = dict(after.b3.select("game_id", "p_b3").iter_rows())
    assert p_after[GAME] != pytest.approx(p_before[GAME], abs=1e-6)
    for other in set(p_before) - {GAME}:
        assert p_after[other] == pytest.approx(p_before[other], abs=1e-12)
    # u's goalie doubt drops with the certain starter.
    doubt = {r["game_id"]: r["goalie_doubt"] for r in after.parts.iter_rows(named=True)}
    was = {r["game_id"]: r["goalie_doubt"] for r in before.parts.iter_rows(named=True)}
    assert doubt[GAME] < was[GAME]
    value, published = rq.blend_of(moved, priced(), LIVE), rq.blend_of(bundle, priced(), LIVE)
    assert value is not None and published is not None and value != pytest.approx(published)


def test_each_change_is_requoted_at_its_moment_or_held_by_its_reason(bundle: lb.Bundle) -> None:
    published = rq.blend_of(bundle, priced(), LIVE)
    assert published is not None
    moment = CHANGE + rq.REQUOTE_AFTER
    expected = rq.blend_of(
        rq.with_starters(bundle, GAME, moment, calls(CONFIRMED_BACKUP)), priced(), LIVE
    )
    found, counts = rq.requotes(
        games(),
        {GAME: [CHANGE]},
        [publication(bundle, published)],
        LIVE,
        calls(CONFIRMED_BACKUP),
        kq.STOP_BEFORE,
    )
    assert found == {GAME: [(moment, expected)]}
    assert counts == {"changes": 1, "requoted": 1}
    stop = STARTS - kq.STOP_BEFORE

    def held(
        changes: list[datetime], pubs: list[rq.Publication], live: blend_fit.LiveFit = LIVE
    ) -> dict[str, int]:
        values, counted = rq.requotes(
            games(), {GAME: changes}, pubs, live, calls(CONFIRMED_BACKUP), kq.STOP_BEFORE
        )
        assert values == {}
        return counted

    ok = publication(bundle, published)
    # Too late to post again before the stop; before the day's first publication.
    assert held([stop - timedelta(seconds=30)], [ok]) == {"changes": 1, rq.LATE: 1}
    assert held([PUBLISHED - timedelta(minutes=2)], [ok]) == {"changes": 1, rq.NO_PUBLICATION: 1}
    # No bundle; another live fit; a bundle that doesn't give the published value back.
    assert held([CHANGE], [publication(None, published)]) == {"changes": 1, rq.NO_BUNDLE: 1}
    other = replace(LIVE, version="blend-live-20261007-other")
    assert held([CHANGE], [ok], other) == {"changes": 1, rq.OTHER_FIT: 1}
    off = publication(bundle, published + 0.01)
    assert held([CHANGE], [off]) == {"changes": 1, rq.UNREPRODUCED: 1}


def test_the_newest_publication_before_the_moment_is_requoted(bundle: lb.Bundle) -> None:
    # The morning view and the decision: a change after the decision requotes the decision's;
    # one between them the morning's, whose guard here fails, so it is held.
    published = rq.blend_of(bundle, priced(), LIVE)
    assert published is not None
    morning = publication(bundle, published + 0.02, CUTOFF - timedelta(hours=5))
    decision = publication(bundle, published)
    found, counts = rq.requotes(
        games(),
        {GAME: [PUBLISHED - timedelta(minutes=2), CHANGE]},
        [decision, morning],
        LIVE,
        calls(CONFIRMED_BACKUP),
        kq.STOP_BEFORE,
    )
    assert [t for t, _ in found[GAME]] == [CHANGE + rq.REQUOTE_AFTER]
    assert counts == {"changes": 2, rq.UNREPRODUCED: 1, "requoted": 1}
