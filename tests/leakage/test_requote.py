"""Point in time for requote after a starter change (hard rule 1, ADR 0059): a requote at its moment
reads only polls observed before it, and its value is known only from then. On a SYNTHETIC
league's bundle."""

from datetime import timedelta
from pathlib import Path

import polars as pl
import pytest
from requote_fixtures import (
    BACKUP,
    CHANGE,
    CUTOFF,
    EFFECTS,
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
from nhl_edge.live import kalshi_quotes as kq
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
        kq.STOP_BEFORE,
    )
    # The change is seen at CHANGE; the value is stamped a minute later, never earlier.
    ((at, _),) = found[GAME]
    assert at == CHANGE + rq.REQUOTE_AFTER > CHANGE


def test_a_goalie_outside_the_candidates_is_rated_from_rows_before_the_cutoff(
    bundle: lb.Bundle,
) -> None:
    # Daily Faceoff names a call-up for CHI. His rows come from other games in the lake read at
    # night: one known before the publication's cutoff, one after it, which never rates him.
    outsider = 77777771
    template = EFFECTS.filter(pl.col("game_id") != GAME).head(1)
    rows = pl.concat(
        [
            template.with_columns(
                game_id=pl.lit(2018029001, pl.Int64),
                goalie_id=pl.lit(outsider, pl.Int64),
                effect=pl.lit(-0.5),
                goals_saved=pl.lit(-15.0),
                as_of_utc=pl.lit(CUTOFF - timedelta(days=1)),
                observed_utc=pl.lit(CUTOFF - timedelta(days=1)),
            ),
            template.with_columns(
                game_id=pl.lit(2018029002, pl.Int64),
                goalie_id=pl.lit(outsider, pl.Int64),
                effect=pl.lit(0.5),
                goals_saved=pl.lit(15.0),
                as_of_utc=pl.lit(CUTOFF),
                observed_utc=pl.lit(CUTOFF + timedelta(hours=1)),
            ),
        ]
    ).cast(EFFECTS.schema)  # type: ignore[arg-type]
    effects = pl.concat([EFFECTS, rows])
    named = calls(polls((CHANGE, "CHI", outsider)), effects)
    moved = rq.with_starters(bundle, GAME, CHANGE + rq.REQUOTE_AFTER, named)
    starts = moved.inputs["goalie_starts"].filter(
        pl.col("game_id") == GAME, pl.col("team") == "CHI"
    )
    assert starts.select("goalie_id", "p_start").rows() == [(outsider, 1.0)]
    rated = moved.inputs["goalie_effects"].filter(
        pl.col("game_id") == GAME, pl.col("goalie_id") == outsider
    )
    assert rated["effect"].to_list() == [-0.5]
    # Only the slate's rows stay in the bundle.
    assert set(moved.inputs["goalie_effects"]["game_id"]) <= set(bundle.inputs["slate"]["game_id"])
    assert rq.blend_of(moved, priced(), LIVE) is not None
