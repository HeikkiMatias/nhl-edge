"""A live feature build (nhl live features, #162): a game date's slate, the live season's
played-game tables brought up to date, the slate games' target rows, and the build's record.

The steps run in the handover's order, each the same command a history rebuild runs, with the
slate as targets from team strength on. The record (FeatureBuilds) is written last and deleted
first, so it exists only for a build that finished; it names each target table's artifact
version and coverage of the slate, for the prediction's checks (#170).
"""

import re
from collections.abc import Mapping
from datetime import date

import polars as pl

from nhl_edge.lake.schemas import FeatureBuilds, dtypes
from nhl_edge.live.targets import in_time

COMPONENT = "live-features"
# The tables holding the slate games' target rows, in the order the steps write them.
TARGET_TABLES = (
    "team_strength",
    "goalie_starts",
    "lineups",
    "lineup_replacements",
    "goalie_effects",
    "schedule_terms",
    "player_ratings",
    "rapm_terms",
    "penalty_rates",
    "expected_power_plays",
    "finishing",
    "goal_multipliers",
)
# Every table a build reads or writes, pulled from R2 first on a fresh machine.
LAKE_TABLES = (
    "games",
    "schedule",
    "players",
    "player_league_seasons",
    "shots",
    "shifts",
    "actual_lineups",
    "shift_coverage",
    "strength_time",
    "penalties",
    "faceoffs",
    "shot_xg",
    "stints",
    "slate",
    "feature_builds",
    *TARGET_TABLES,
)
SLATE = "slate"


def component(version: str) -> str:
    """An artifact version's component: the part before -<yyyymmdd>-<shortsha>."""
    match = re.match(r"^(.+)-\d{8}-", version)
    return match.group(1) if match else version


def table_rows(
    name: str, frame: pl.DataFrame, slate: pl.DataFrame
) -> list[tuple[str, int, int | None]]:
    """A target table's rows of the slate's date, per artifact version: the version, its rows,
    and the slate games they cover (None for a table without game_id). A table can hold another
    component's rows (lineups copies goalie_starts' goalies), but a date with no rows, or with
    rows of two versions of one component (two builds), is refused."""
    if frame.is_empty():
        raise ValueError(f"{name} has no rows for the slate's date")
    versions = frame["artifact_version"].unique().sort().to_list()
    for part in {component(v) for v in versions}:
        same = [v for v in versions if component(v) == part]
        if len(same) > 1:
            raise ValueError(f"{name} holds rows of {len(same)} {part} versions: {same}")
    out = []
    for version in versions:
        rows = frame.filter(pl.col("artifact_version") == version)
        games = None
        if "game_id" in rows.columns:
            covered = rows.join(slate.select("game_id"), on="game_id", how="semi")
            games = covered["game_id"].n_unique()
        out.append((version, rows.height, games))
    return out


def record(
    slate: pl.DataFrame,
    day: date,
    season: int,
    tables: Mapping[str, pl.DataFrame],
    build: Mapping[str, object],
) -> pl.DataFrame:
    """The build's FeatureBuilds rows: the slate's, then one per target table and artifact version
    from its rows of the date (tables). build holds build_id, code_version, slate_raw_key,
    slate_fetched_utc, started_utc and finished_utc."""
    # The slate's games are all its rows; those fetched in time to be rated are its games.
    rows: list[dict[str, object]] = [
        {
            "table": SLATE,
            "artifact_version": build["build_id"],
            "rows": slate.height,
            "games": in_time(slate).height,
        }
    ]
    for name, frame in tables.items():
        for version, height, games in table_rows(name, frame, slate):
            rows.append(
                {"table": name, "artifact_version": version, "rows": height, "games": games}
            )
    frame = pl.DataFrame(rows).with_columns(
        game_date=pl.lit(day),
        season=pl.lit(season),
        slate_games=pl.lit(slate.height),
        **{key: pl.lit(value) for key, value in build.items()},
    )
    columns = dtypes(FeatureBuilds)
    return FeatureBuilds.validate(frame.select(list(columns)).cast(columns))  # type: ignore[arg-type]


def uncovered(slate: pl.DataFrame, tables: Mapping[str, pl.DataFrame]) -> list[str]:
    """The slate games each target table has no row for: their predictions will lack an input."""
    lines = []
    for name, frame in tables.items():
        if "game_id" not in frame.columns:
            continue
        missing = slate.join(frame.select("game_id"), on="game_id", how="anti")
        if missing.height:
            games = ", ".join(map(str, missing["game_id"].sort().to_list()))
            lines.append(f"{name}: no rows for {missing.height} slate games ({games})")
    return lines
