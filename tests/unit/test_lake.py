from datetime import UTC, date, datetime
from pathlib import Path

import pandera.errors
import polars as pl
import pytest
from fakes import MemoryBucket

from nhl_edge.ingest.games import listed_games, parse_games
from nhl_edge.ingest.players import landing_row, parse_players
from nhl_edge.lake.r2 import bucket_usage, list_keys
from nhl_edge.lake.tables import Lake

FIXTURES = Path(__file__).parent / "fixtures" / "nhl_api"
OPENING_DAYS = {date(2010, 10, 7), date(2010, 10, 8)}
GAMES = parse_games(
    listed_games((FIXTURES / "schedule_2010-10-07.json").read_bytes(), OPENING_DAYS), "k"
)
FETCHED = datetime(2026, 9, 28, 13, 0, tzinfo=UTC)
OCT_7 = "games/season=20102011/game_date=2010-10-07/part-0.parquet"
OCT_8 = "games/season=20102011/game_date=2010-10-08/part-0.parquet"


def players(*ids: int) -> pl.DataFrame:
    rows = []
    for player_id in ids:
        body = (
            (FIXTURES / "landing_8478402.json")
            .read_bytes()
            .replace(b"8478402", str(player_id).encode())
        )
        rows.append(landing_row(body, FETCHED, f"nhl/player-landing/{player_id}/x"))
    return parse_players(rows)


def test_games_are_partitioned_by_season_and_date(tmp_path: Path) -> None:
    lake = Lake(tmp_path)
    written = lake.write("games", GAMES)
    assert written == [
        "games/season=20102011/game_date=2010-10-07/part-0.parquet",
        "games/season=20102011/game_date=2010-10-08/part-0.parquet",
    ]
    assert lake.read("games").equals(GAMES.sort("game_id"))


def test_writing_a_partition_replaces_it(tmp_path: Path) -> None:
    lake = Lake(tmp_path)
    lake.write("games", GAMES)
    lake.write("games", GAMES.filter(pl.col("game_id") == 2010020004))
    # 2010-10-07 now holds only the rewritten game; 2010-10-08 is untouched.
    assert lake.read("games")["game_id"].to_list() == [2010020004, 2010020008]


def test_replacing_dates_drops_partitions_the_frame_no_longer_has(tmp_path: Path) -> None:
    bucket = MemoryBucket()
    laptop = Lake(tmp_path / "laptop", "b", bucket)
    laptop.write("games", GAMES)
    # A fresh machine replays both dates, and a parser fix has dropped the game on 2010-10-08.
    runner = Lake(tmp_path / "runner", "b", bucket)
    kept = GAMES.filter(pl.col("game_date") == date(2010, 10, 7))
    assert runner.replace_dates("games", kept, OPENING_DAYS) == [OCT_7]
    assert sorted(bucket.objects) == [f"lake/{OCT_7}"]
    # The laptop's stale local copy goes too once it replaces the same dates.
    laptop.replace_dates("games", kept, OPENING_DAYS)
    assert not (tmp_path / "laptop" / OCT_8).exists()
    assert laptop.read("games")["game_id"].to_list() == [2010020003, 2010020004]


def test_replacing_dates_leaves_other_dates_alone(tmp_path: Path) -> None:
    lake = Lake(tmp_path)
    lake.write("games", GAMES)
    lake.replace_dates("games", GAMES.head(0), {date(2010, 10, 9)})
    assert lake.read("games").height == 3
    lake.replace_dates("games", GAMES.head(0), {date(2010, 10, 8)})
    assert lake.read("games")["game_id"].to_list() == [2010020003, 2010020004]
    with pytest.raises(ValueError, match="not partitioned by game_date"):
        lake.replace_dates("players", players(1), {date(2010, 10, 8)})


def test_write_validates(tmp_path: Path) -> None:
    with pytest.raises(pandera.errors.SchemaError):
        Lake(tmp_path).write("games", GAMES.with_columns(pl.col("home_score").alias("away_score")))
    assert not list(tmp_path.rglob("*.parquet"))


def test_missing_table_reads_empty_with_its_columns(tmp_path: Path) -> None:
    empty = Lake(tmp_path).read("players")
    assert empty.is_empty()
    assert empty.schema == players(1).schema


def test_mirror_uploads_the_same_bytes_under_lake(tmp_path: Path) -> None:
    bucket = MemoryBucket()
    written = Lake(tmp_path, "lake-bucket", bucket).write("games", GAMES)
    assert sorted(bucket.objects) == [f"lake/{key}" for key in written]
    for key in written:
        assert bucket.objects[f"lake/{key}"] == (tmp_path / key).read_bytes()


def test_pull_restores_a_table_on_a_fresh_machine(tmp_path: Path) -> None:
    bucket = MemoryBucket(page_size=1)
    Lake(tmp_path / "laptop", "b", bucket).write("games", GAMES)
    runner = Lake(tmp_path / "runner", "b", bucket)
    assert runner.pull("games") == 2
    assert runner.read("games").equals(GAMES.sort("game_id"))
    assert Lake(tmp_path / "local").pull("games") == 0


def test_upsert_merges_by_key_and_keeps_rows_from_elsewhere(tmp_path: Path) -> None:
    bucket = MemoryBucket()
    Lake(tmp_path / "laptop", "b", bucket).upsert("players", players(1, 2))
    runner = Lake(tmp_path / "runner", "b", bucket)
    renamed = players(2).with_columns(pl.lit("Renamed").alias("name"))
    merged = runner.upsert("players", pl.concat([renamed, players(3)]))
    assert merged["player_id"].to_list() == [1, 2, 3]
    assert merged.filter(pl.col("player_id") == 2)["name"].item() == "Renamed"
    assert runner.read("players").equals(merged)


def test_upsert_refuses_partitioned_tables(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="partitioned"):
        Lake(tmp_path).upsert("games", GAMES)


def test_bucket_usage_follows_pagination() -> None:
    bucket = MemoryBucket(page_size=2)
    for i in range(5):
        bucket.put_object(Key=f"raw/nhl/{i}", Body=b"x" * (i + 1))
    bucket.put_object(Key="lake/games/a", Body=b"yy")
    assert bucket_usage(bucket, "b") == (6, 17)
    assert bucket_usage(bucket, "b", "raw/") == (5, 15)
    assert len(list_keys(bucket, "b", "lake/")) == 1


def test_mirror_needs_bucket_and_client(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="both"):
        Lake(tmp_path, bucket="lake")
