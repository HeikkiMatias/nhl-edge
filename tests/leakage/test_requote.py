"""Point in time for requote after a starter change (hard rule 1, ADR 0059): a requote at its moment
reads only polls observed before it, and its value is known only from then. On a SYNTHETIC
league's bundle."""

from datetime import timedelta
from pathlib import Path

import pytest
from requote_fixtures import (
    BACKUP,
    CHANGE,
    GAME,
    LIVE,
    calls,
    games,
    polls,
    priced,
    publication,
    write_bundle,
)

from nhl_edge.live import bundle as lb
from nhl_edge.live import requote as rq


@pytest.fixture(scope="module")
def bundle(tmp_path_factory: pytest.TempPathFactory) -> lb.Bundle:
    return write_bundle(Path(tmp_path_factory.mktemp("bundle")))


def test_a_poll_at_or_after_the_moment_never_enters_its_value(bundle: lb.Bundle) -> None:
    moment = CHANGE + rq.REQUOTE_AFTER
    published = rq.blend_of(bundle, priced(), LIVE)
    assert published is not None

    def value(observed: object) -> float | None:
        named = calls(polls((observed, "CHI", BACKUP)))  # type: ignore[arg-type]
        return rq.blend_of(rq.with_starters(bundle, GAME, moment, named), priced(), LIVE)

    # Observed at the moment or later: as if never polled.
    assert value(moment) == pytest.approx(published, abs=1e-12)
    assert value(moment + timedelta(hours=1)) == pytest.approx(published, abs=1e-12)
    # Observed just before it: the backup is in.
    assert value(moment - timedelta(microseconds=1)) != pytest.approx(published, abs=1e-6)


def test_a_requoted_value_is_known_only_from_its_moment(bundle: lb.Bundle) -> None:
    published = rq.blend_of(bundle, priced(), LIVE)
    assert published is not None
    found, _ = rq.requotes(
        games(),
        {GAME: [CHANGE]},
        [publication(bundle, published)],
        LIVE,
        calls(polls((CHANGE, "CHI", BACKUP))),
    )
    # The change is seen at CHANGE; the value is stamped a minute later, never earlier.
    ((at, _),) = found[GAME]
    assert at == CHANGE + rq.REQUOTE_AFTER > CHANGE
