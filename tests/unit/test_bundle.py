"""A decision day's run bundle (#171): written once, read back only whole, and replayed from its
own rows to the predictions the day's models made from the whole lake."""

from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

import polars as pl
import pytest
from b2_fixtures import feature_tables
from b3_fixtures import league

from nhl_edge.backtest.walk_forward import fold_start
from nhl_edge.game import uncertainty
from nhl_edge.lake.schemas import PlayerLeagueSeasons, Slate, dtypes
from nhl_edge.live import bundle as lb
from nhl_edge.live import predict as lp

LEAGUE = league()
TEST = 20182019
START = fold_start(LEAGUE.games.select("season", "start_utc"), TEST)
B2_TABLES = replace(feature_tables(LEAGUE.games, 4), games=LEAGUE.games)
# B3 shares B2's schedule terms, goalie starts and boxscores, as nhl predict reads them.
B3_TABLES = replace(
    LEAGUE,
    schedule_terms=B2_TABLES.schedule_terms,
    goalie_starts=B2_TABLES.goalie_starts,
    actual_lineups=B2_TABLES.actual_lineups,
)
U_TABLES = uncertainty.Tables(
    LEAGUE.games,
    B2_TABLES.goalie_starts,
    LEAGUE.lineups,
    LEAGUE.lineup_replacements,
    B2_TABLES.actual_lineups,
    pl.DataFrame(schema=dtypes(PlayerLeagueSeasons)),
)


def slate() -> pl.DataFrame:
    """The test season's tenth game date, as the day's slate."""
    games = LEAGUE.games.filter(pl.col("season") == TEST)
    day = games["game_date"].unique().sort()[9]
    return Slate.validate(
        games.filter(pl.col("game_date") == day)
        .with_columns(game_state=pl.lit("FUT"))
        .select(list(dtypes(Slate)))
    )


def day_bundle(store: lb.Store) -> tuple[lp.Models, datetime]:
    games = slate()
    cutoff = games["start_utc"].min()
    assert isinstance(cutoff, datetime)
    moments = games.select("game_id", prediction_utc=pl.lit(cutoff))
    fitted = lp.models(B2_TABLES, B3_TABLES, U_TABLES, games, moments, START, TEST)
    assert fitted.b2_model is not None and fitted.b3_model is not None
    rows = lb.inputs(games, pl.DataFrame({"table": ["slate"]}), B2_TABLES, B3_TABLES, U_TABLES)
    identity = {"day": str(games["game_date"][0]), "input_cutoff": cutoff.isoformat()}
    raw = {"odds/2018-10-19/x_midday_eu.json.gz": b"{}"}
    lb.write_once(
        store,
        games["game_date"][0],
        rows,
        {"b2": fitted.b2_model, "b3": fitted.b3_model},
        identity,
        raw,
    )
    return fitted, cutoff


def test_the_replay_from_the_bundle_alone_matches_the_models_on_the_whole_lake(
    tmp_path: Path,
) -> None:
    store = lb.LocalStore(tmp_path)
    fitted, _ = day_bundle(store)
    read = lb.read(store, slate()["game_date"][0])
    # The bundle holds only the slate's rows, far from the whole lake the models read.
    assert read.inputs["lineups"]["game_id"].n_unique() == slate().height
    assert read.inputs["player_ratings"].height < LEAGUE.player_ratings.height / 100
    replayed = lb.replay(read)
    made = (
        fitted.b2.join(fitted.b3, on="game_id")
        .join(fitted.parts, on="game_id")
        .select(replayed.columns)
        .sort("game_id")
    )
    assert replayed.height == slate().height
    for column in lb.COMPARED:
        assert replayed[column].to_list() == pytest.approx(made[column].to_list(), abs=1e-12)
    # The ledger check: the replay agrees with the predicted rows, and a changed value shows.
    ledger = made.with_columns(status=pl.lit("predicted"))
    assert lb.differences(replayed, ledger) == []
    moved = ledger.with_columns(p_b3=pl.col("p_b3") + 1e-6)
    assert lb.differences(replayed, moved) == ["p_b3 differs by up to 1e-06"]
    assert lb.differences(replayed, ledger.head(1)) == [
        f"replayed but not predicted: {sorted(made['game_id'].to_list()[1:])}"
    ]


def test_a_bundle_is_written_once_and_read_only_whole(tmp_path: Path) -> None:
    store = lb.LocalStore(tmp_path)
    day_bundle(store)
    day = slate()["game_date"][0]
    where = tmp_path / lb.prefix(day)
    manifest = (where / lb.MANIFEST).read_text()
    assert '"odds/2018-10-19/x_midday_eu.json.gz"' in manifest
    # Never rewritten.
    with pytest.raises(FileExistsError):
        day_bundle(store)
    # A file whose bytes changed is refused.
    lineups = where / "inputs" / "lineups.parquet"
    good = lineups.read_bytes()
    lineups.write_bytes(good + b"x")
    with pytest.raises(ValueError, match="don't match the manifest"):
        lb.read(store, day)
    lineups.write_bytes(good)
    lb.read(store, day)
    # Without its manifest, the bundle never finished.
    (where / lb.MANIFEST).unlink()
    with pytest.raises(ValueError, match="no manifest"):
        lb.read(store, day)


def test_in_r2_every_object_is_written_once_and_the_manifest_last() -> None:
    from fakes import ConditionalBucket

    bucket = ConditionalBucket()
    order: list[str] = []
    put = bucket.put_object

    def tracked(**kwargs: Any) -> None:
        order.append(kwargs["Key"])
        put(**kwargs)

    bucket.put_object = tracked  # type: ignore[method-assign]
    day_bundle(lb.R2Store(bucket, "b"))
    assert order[-1].endswith(lb.MANIFEST)
    assert all(key.startswith(lb.prefix(slate()["game_date"][0])) for key in order)
    with pytest.raises(RuntimeError, match="PreconditionFailed"):
        day_bundle(lb.R2Store(bucket, "b"))


def test_nhl_live_replay_reads_only_the_bundle_and_the_ledger(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from nhl_edge.cli import app

    fitted, _ = day_bundle(lb.LocalStore(tmp_path))
    day = slate()["game_date"][0]
    made = fitted.b2.join(fitted.b3, on="game_id").join(fitted.parts, on="game_id")
    ledger = made.with_columns(status=pl.lit(lp.PREDICTED))
    ledger.write_parquet(tmp_path / f"{day.isoformat()}.parquet")
    command = ["live", "replay", "--date", day.isoformat(), "--from", str(tmp_path)]
    result = CliRunner().invoke(app, command)
    assert result.exit_code == 0, result.output
    assert "every prediction reproduced" in result.output
    # A ledger the bundle doesn't reproduce fails.
    ledger.with_columns(p_b2=pl.col("p_b2") * 0.9).write_parquet(
        tmp_path / f"{day.isoformat()}.parquet"
    )
    result = CliRunner().invoke(app, command)
    assert result.exit_code == 1
    assert "p_b2 differs" in result.output
