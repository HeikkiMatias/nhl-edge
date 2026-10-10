"""A decision day's run bundle (#171): the rows nhl predict read for the slate's games, the B2 and
B3 fits it used, and a manifest of hashes, written once beside the day's ledger. The lake's
partitions are mutable, and the nightly rewrites a date's rows once its games are final, so the
bundle is what lets a logged decision be explained and reproduced afterwards.

- **inputs/<table>.parquet:** the slate, the date's feature_builds record and the day's parsed
  odds quotes; every row of the tables B2, B3 and u read for the slate's games (by game_id, and
  rapm_terms by the slate's dates); and for u's rookie share the candidates' boxscores of the
  season and their league seasons.
- **fits.json:** B2's and B3's fitted intercepts, weights, standardization, games and cutoffs.
- **manifest.json,** written last as the completion mark: the run's identity (date, decision,
  publication and input cutoff times, bankroll, policy, live fit and its sha256, feature build,
  code, uv.lock's sha256, the ledger's key), each file's sha256 and rows, and every odds response
  the run attempted, its body and metadata, with their sha256.

Every object is written once (IfNoneMatch in R2, exclusive creation locally). A write that failed
part way can be run again: an object already there with the same bytes counts as written. A
reader refuses a bundle without its manifest, or with a file whose bytes don't match it.

replay() makes the day's decision again from the bundle alone and the committed live fit: B2, B3
and u's parts through the half of b2.predictions and b3.predictions after the fit, with the saved
fits, and uncertainty.parts itself; then predict.decide on the saved quotes, slate and bankroll,
at the saved decision and publication times. Its ledger is compared with the day's, every column.
"""

import hashlib
import io
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Protocol

import polars as pl

from nhl_edge.backtest import e3
from nhl_edge.game import b2, b3, uncertainty
from nhl_edge.game.b2 import B2Model
from nhl_edge.game.b3 import B3Model
from nhl_edge.lake.r2 import list_keys
from nhl_edge.lake.schemas import Games, dtypes
from nhl_edge.live import blend_fit
from nhl_edge.live import predict as lp
from nhl_edge.live.targets import with_targets

PREFIX = "bundles/live"
MANIFEST = "manifest.json"
FITS = "fits.json"
# Replay against the ledger: the same code on the same rows, up to the order of float sums.
TOLERANCE = 1e-9


class Conflict(ValueError):
    """An object of the bundle already written with other bytes."""


class Store(Protocol):
    """Where a bundle's objects live: put writes once, get reads back, keys lists them."""

    def put(self, key: str, body: bytes) -> None: ...

    def get(self, key: str) -> bytes: ...

    def keys(self, prefix: str) -> list[str]: ...


@dataclass(frozen=True)
class R2Store:
    objects: Any
    bucket: str

    def put(self, key: str, body: bytes) -> None:
        self.objects.put_object(Bucket=self.bucket, Key=key, Body=body, IfNoneMatch="*")

    def get(self, key: str) -> bytes:
        return self.objects.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def keys(self, prefix: str) -> list[str]:
        return [key for key, _ in list_keys(self.objects, self.bucket, f"{prefix}/")]


@dataclass(frozen=True)
class LocalStore:
    root: Path

    def put(self, key: str, body: bytes) -> None:
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as file:
            file.write(body)

    def get(self, key: str) -> bytes:
        return (self.root / key).read_bytes()

    def keys(self, prefix: str) -> list[str]:
        root = self.root / prefix
        found = root.rglob("*") if root.exists() else []
        return sorted(p.relative_to(self.root).as_posix() for p in found if p.is_file())


@dataclass(frozen=True)
class Bundle:
    manifest: Mapping[str, Any]
    inputs: Mapping[str, pl.DataFrame]
    b2: B2Model | None
    b3: B3Model | None


def sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def prefix(day: date) -> str:
    return f"{PREFIX}/{day.isoformat()}"


# The rows every decision saves, and those it adds when its models were read (inputs()).
BASE_INPUTS = ("slate", "feature_builds", "quotes")
MODEL_INPUTS = (
    "team_strength",
    "schedule_terms",
    "goalie_starts",
    "goalie_effects",
    "lineups",
    "lineup_replacements",
    "player_ratings",
    "rapm_terms",
    "expected_power_plays",
    "goal_multipliers",
    "actual_lineups",
    "player_league_seasons",
)


def inputs(
    slate: pl.DataFrame,
    record: pl.DataFrame,
    quotes: pl.DataFrame,
    models: tuple[b2.Tables, b3.Tables, uncertainty.Tables] | None,
) -> dict[str, pl.DataFrame]:
    """The rows the day's decision read: the slate, its build record and the quotes, and, when the
    models were read, the rows they read for the slate's games, from the tables they were given."""
    read = {"slate": slate, "feature_builds": record, "quotes": quotes}
    if models is None:
        return read
    tables, b3_tables, u_tables = models
    ids = slate["game_id"].implode()
    (season,) = slate["season"].unique().to_list()

    def games_rows(frame: pl.DataFrame) -> pl.DataFrame:
        return frame.filter(pl.col("game_id").is_in(ids))

    lineups = games_rows(b3_tables.lineups)
    skaters = lineups.filter(pl.col("role").is_in(uncertainty.SKATERS))["player_id"].implode()
    return read | {
        "team_strength": games_rows(tables.team_strength),
        "schedule_terms": games_rows(tables.schedule_terms),
        "goalie_starts": games_rows(tables.goalie_starts),
        "goalie_effects": games_rows(tables.goalie_effects),
        "lineups": lineups,
        "lineup_replacements": games_rows(b3_tables.lineup_replacements),
        "player_ratings": games_rows(b3_tables.player_ratings),
        # B3's league rates are read by game date.
        "rapm_terms": b3_tables.rapm_terms.filter(
            pl.col("game_date").is_in(slate["game_date"].implode())
        ),
        "expected_power_plays": games_rows(b3_tables.expected_power_plays),
        "goal_multipliers": games_rows(b3_tables.goal_multipliers),
        # u counts a skater's games of the season from his boxscores, and earlier seasons'.
        "actual_lineups": u_tables.actual_lineups.filter(
            pl.col("season") == season, pl.col("player_id").is_in(skaters)
        ),
        "player_league_seasons": u_tables.player_league_seasons.filter(
            pl.col("player_id").is_in(skaters)
        ),
    }


def fit_record(model: B2Model | B3Model) -> dict[str, Any]:
    record = asdict(model)
    record["train_cutoff"] = model.train_cutoff.isoformat()
    # A fit without arena shifts or input terms (every v1 fit) keeps v1's bundle bytes (ADR
    # 0035, 0036).
    if not record.get("arenas", True):
        del record["arenas"]
    if isinstance(model, B3Model) and model.inputs == b3.INPUTS:
        del record["inputs"]
    return record


def _fit(record: Mapping[str, Any], kind: type[Any]) -> Any:
    # A B3 fit with v2's terms carries its arena shifts (ADR 0035) and its inputs (ADR 0036);
    # v1's carry neither.
    extra: dict[str, Any] = {}
    if kind is B3Model and record.get("arenas"):
        extra["arenas"] = tuple((str(a), float(x)) for a, x in record["arenas"])
    if kind is B3Model and record.get("inputs"):
        extra["inputs"] = tuple(str(name) for name in record["inputs"])
    return kind(
        season=record["season"],
        settings=b2.Settings(**record["settings"]),
        intercept=record["intercept"],
        weights=tuple(record["weights"]),
        means=tuple(record["means"]),
        scales=tuple(record["scales"]),
        games=record["games"],
        train_cutoff=datetime.fromisoformat(record["train_cutoff"]),
        **extra,
    )


def parquet(frame: pl.DataFrame) -> bytes:
    body = io.BytesIO()
    frame.write_parquet(body)
    return body.getvalue()


def write_files(
    store: Store,
    day: date,
    rows: Mapping[str, pl.DataFrame],
    fits: Mapping[str, B2Model | B3Model],
) -> dict[str, bytes]:
    """Write the rows the decision read and its fits once, before its ledger (#188), so they
    survive a run that stops after it; and return them by name. Written again, the same bytes
    count as written."""
    files = {f"inputs/{name}.parquet": parquet(frame) for name, frame in rows.items()}
    files[FITS] = json.dumps({k: fit_record(v) for k, v in fits.items()}, indent=1).encode()
    where = prefix(day)
    for name, body in files.items():
        put_once(store, f"{where}/{name}", body)
    return files


def write_files_first(
    store: Store,
    day: date,
    rows: Mapping[str, pl.DataFrame],
    fits: Mapping[str, B2Model | B3Model],
) -> str | None:
    """write_files before the ledger, in one try, so a slow store never holds up the ledger: a
    failure comes back as its message, for the run to log and the write after the ledger to try
    again. A Conflict raises: an earlier run wrote the day's files from other inputs, and a ledger
    published over them could never be finished (Codex on #204)."""
    try:
        write_files(store, day, rows, fits)
    except Conflict:
        raise
    except Exception as exc:
        return str(exc)
    return None


def manifest_of(
    identity: Mapping[str, Any], files: Mapping[str, bytes], raw: Mapping[str, bytes]
) -> dict[str, Any]:
    """The manifest: the run's identity, each file's sha256 and rows, and each raw response the
    decision read, by sha256."""
    return {
        **identity,
        "files": {
            name: {
                "sha256": sha256(body),
                "rows": pl.read_parquet(body).height if name.endswith(".parquet") else None,
            }
            for name, body in sorted(files.items())
        },
        "raw": {key: sha256(body) for key, body in sorted(raw.items())},
    }


def write_manifest(store: Store, day: date, manifest: Mapping[str, Any]) -> str:
    """Write the manifest, the bundle's completion mark, once; and return the bundle's prefix."""
    where = prefix(day)
    put_once(store, f"{where}/{MANIFEST}", json.dumps(manifest, indent=1, default=str).encode())
    return where


def write_once(
    store: Store,
    day: date,
    rows: Mapping[str, pl.DataFrame],
    fits: Mapping[str, B2Model | B3Model],
    identity: Mapping[str, Any],
    raw: Mapping[str, bytes],
) -> str:
    """Write the day's bundle once, the manifest last, and return its prefix. raw maps each raw
    response the decision read to its bytes, recorded by sha256."""
    files = write_files(store, day, rows, fits)
    return write_manifest(store, day, manifest_of(identity, files, raw))


def finish(
    store: Store,
    day: date,
    ledger: pl.DataFrame,
    ledger_at: str,
    raw: Mapping[str, bytes],
    live: blend_fit.LiveFit,
    live_fit: str,
    live_fit_sha256: str,
) -> str:
    """Complete a bundle whose run stopped after its ledger (#188): its manifest rebuilt from what
    is stored. The identity comes from the ledger's own columns (decision and publication times,
    versions, bankroll), the input cutoff from the saved quotes as the run computed it, the files
    from the stored objects, the raw odds responses, which are immutable, from raw (those stored
    by the publication), and the live fit the ledger names (live, its file live_fit and sha256).
    The manifest, the completion mark, is written only once the stored files reproduce the
    ledger through replay, every column: files from a run other than the ledger's never seal.
    It never writes an input, and refuses a bundle already complete or missing any input the run
    writes."""
    where = prefix(day)
    stored = {key.removeprefix(f"{where}/") for key in store.keys(where)}
    if MANIFEST in stored:
        raise ValueError(f"{where} is complete already")
    if FITS not in stored:
        raise ValueError(f"{where}: no {FITS}, so the run's files were never written")
    fits = json.loads(store.get(f"{where}/{FITS}"))
    wanted = BASE_INPUTS + (MODEL_INPUTS if fits else ())
    missing = [name for name in wanted if f"inputs/{name}.parquet" not in stored]
    if missing:
        raise ValueError(f"{where}: missing {', '.join(missing)}, so it can't be finished")
    files = {
        name: store.get(f"{where}/{name}")
        for name in [FITS, *(f"inputs/{name}.parquet" for name in wanted)]
    }
    quotes = pl.read_parquet(files["inputs/quotes.parquet"])

    def one(column: str) -> Any:
        values = ledger[column].drop_nulls().unique().to_list()
        if len(values) > 1:
            raise ValueError(f"the ledger holds {len(values)} values of {column}")
        return values[0] if values else None

    decision = one("prediction_utc")
    if decision is None:
        raise ValueError("the ledger has no decision instant")
    identity = {
        "day": day.isoformat(),
        "decision_utc": decision.isoformat(),
        "published_utc": one("published_utc").isoformat(),
        "input_cutoff": lp.input_cutoff(quotes, decision).isoformat(),
        # The ledger's; none when it predicted no game, where it moves nothing.
        "bankroll": one("bankroll"),
        "policy_version": one("policy_version"),
        "blend_version": one("blend_version"),
        "live_fit": live_fit,
        "live_fit_sha256": live_fit_sha256,
        "feature_build": one("feature_build"),
        "code_version": one("code_version"),
        # The run's own lock file is not stored: a later checkout can't stand in for it.
        "uv_lock_sha256": None,
        "ledger": ledger_at,
        "finished_later": True,
    }
    manifest = manifest_of(identity, files, raw)
    problems = differences(replay(_bundle(manifest, files), live), ledger)
    if problems:
        raise ValueError(
            f"{where}: its stored files don't reproduce the ledger ({'; '.join(problems)}), "
            "so it stays unfinished"
        )
    return write_manifest(store, day, manifest)


def unfinished(store: Store, days: list[date]) -> list[date]:
    """The decision days among days whose bundle has no manifest."""
    return [day for day in days if f"{prefix(day)}/{MANIFEST}" not in store.keys(prefix(day))]


def put_once(store: Store, key: str, body: bytes) -> None:
    """Write an object once. If it is already there with the same bytes, as after a write that
    failed part way, it counts as written; with other bytes, it is refused."""
    try:
        store.put(key, body)
    except Exception as exc:
        try:
            held = store.get(key)
        except Exception:
            raise exc from None
        if sha256(held) != sha256(body):
            raise Conflict(f"{key} is already written, with other bytes") from exc


def read(store: Store, day: date) -> Bundle:
    """The day's bundle, refused without its manifest (an unfinished write) or with any file
    missing or not matching its hash."""
    where = prefix(day)
    try:
        manifest = json.loads(store.get(f"{where}/{MANIFEST}"))
    except Exception as exc:
        raise ValueError(f"{where}: no manifest, so no complete bundle ({exc})") from None
    bodies = {}
    for name, entry in manifest["files"].items():
        try:
            body = store.get(f"{where}/{name}")
        except Exception as exc:
            raise ValueError(f"{where}/{name}: missing ({exc})") from None
        if sha256(body) != entry["sha256"]:
            raise ValueError(f"{where}/{name}: its bytes don't match the manifest")
        bodies[name] = body
    return _bundle(manifest, bodies)


def _bundle(manifest: Mapping[str, Any], bodies: Mapping[str, bytes]) -> Bundle:
    """A bundle from its manifest and its files' bytes, by name."""
    frames = {
        name.removeprefix("inputs/").removesuffix(".parquet"): pl.read_parquet(body)
        for name, body in bodies.items()
        if name.startswith("inputs/")
    }
    fits = json.loads(bodies[FITS]) if FITS in bodies else {}
    return Bundle(
        manifest,
        frames,
        _fit(fits["b2"], B2Model) if "b2" in fits else None,
        _fit(fits["b3"], B3Model) if "b3" in fits else None,
    )


def _slate(bundle: Bundle) -> tuple[pl.DataFrame, pl.DataFrame, int]:
    """The slate's games as targets, each at the decision's input cutoff, and its season."""
    slate = bundle.inputs["slate"]
    (season,) = slate["season"].unique().to_list()
    cutoff = datetime.fromisoformat(bundle.manifest["input_cutoff"])
    games = with_targets(pl.DataFrame(schema=dtypes(Games)), slate)
    moments = slate.select("game_id", prediction_utc=pl.lit(cutoff, dtype=pl.Datetime("us", "UTC")))
    return games, moments, int(season)


def _b3_tables(bundle: Bundle, games: pl.DataFrame, season: int) -> b3.Tables:
    """The rows B3 read, through the season."""
    t = bundle.inputs
    return b3.through(
        b3.Tables(
            games,
            t["schedule_terms"],
            t["goalie_starts"],
            t["actual_lineups"],
            t["lineups"],
            t["lineup_replacements"],
            t["player_ratings"],
            t["rapm_terms"],
            t["expected_power_plays"],
            t["goal_multipliers"],
        ),
        season,
    )


def b3_parts(bundle: Bundle) -> pl.DataFrame | None:
    """B3's log-odds in parts at the decision for each slate game it predicted (e3.b3_parts:
    game_id, intercept and each input), from the bundle alone: the saved fit and rows at the
    input cutoff. None when the day's models were never read (#193)."""
    if bundle.b3 is None:
        return None
    games, moments, season = _slate(bundle)
    return e3.b3_parts(moments, _b3_tables(bundle, games, season), bundle.b3, season)


def models(bundle: Bundle) -> lp.Models | None:
    """B2, B3 and u's parts for each slate game that has them, from the bundle alone: the frozen
    code's predict path after the fit, with the saved fits, at the decision's input cutoff. None
    when the day's models were never read."""
    if bundle.b2 is None or bundle.b3 is None:
        return None
    t = bundle.inputs
    games, moments, season = _slate(bundle)
    # B2: b2.predictions after its fit.
    b2_tables = b2.Tables(
        games,
        t["team_strength"],
        t["schedule_terms"],
        t["goalie_starts"],
        t["goalie_effects"],
        t["actual_lineups"],
    )
    usable, ready = b2.known_before(
        b2.game_inputs(b2_tables).filter(pl.col("season") == season),
        b2.candidates(b2_tables),
        moments,
        "observed_utc",
    )
    p_b2 = bundle.b2.predict(usable, b2.scenarios(usable.select("game_id", "home", "away"), ready))
    # B3: b3.predictions after its fit.
    b3_tables = _b3_tables(bundle, games, season)
    pool = b3_tables.goalie_starts.select("game_id", "team", "goalie_id", "p_start", "observed_utc")
    usable, ready = b2.known_before(
        b3.game_inputs(b3_tables).filter(pl.col("season") == season), pool, moments, "observed_utc"
    )
    _, gammas = b3.multipliers(b3_tables)
    p_b3 = bundle.b3.predict(usable, b3.scenarios(usable, ready, gammas))
    # u: its parts for the games B3 predicted, as nhl predict reads them.
    parts = uncertainty.parts(
        uncertainty.Tables(
            games,
            t["goalie_starts"],
            t["lineups"],
            t["lineup_replacements"],
            t["actual_lineups"],
            t["player_league_seasons"],
        ),
        moments.join(p_b3.select("game_id"), on="game_id", how="semi"),
    )
    return lp.Models(
        p_b2.select("game_id", p_b2="p_home"),
        p_b3.select("game_id", p_b3="p_home"),
        parts.select("game_id", *uncertainty.PARTS),
        bundle.b2.train_cutoff,
        bundle.b3.train_cutoff,
        bundle.b2,
        bundle.b3,
    )


def replay(bundle: Bundle, live: blend_fit.LiveFit) -> pl.DataFrame:
    """The day's ledger made again from the bundle alone and the live fit: its models, then the
    decision on the saved quotes, slate and bankroll, at the saved decision and publication
    times."""
    manifest = bundle.manifest
    if live.version != manifest["blend_version"]:
        decided = manifest["blend_version"]
        raise ValueError(f"the bundle was decided with {decided}, not {live.version}")
    fitted = models(bundle)
    day = lp.Day(
        day=date.fromisoformat(manifest["day"]),
        decision_utc=datetime.fromisoformat(manifest["decision_utc"]),
        slate=bundle.inputs["slate"],
        quotes=bundle.inputs["quotes"],
        fitted=fitted,
        live=live,
        bankroll=manifest["bankroll"],
        versions={
            "blend_version": manifest["blend_version"],
            "feature_build": manifest["feature_build"],
            "code_version": manifest["code_version"],
            "b2_train_cutoff": fitted.b2_cutoff if fitted else None,
            "b3_train_cutoff": fitted.b3_cutoff if fitted else None,
        },
        published_utc=datetime.fromisoformat(manifest["published_utc"]),
    )
    return lp.ledger(lp.decide(day), day)


def differences(replayed: pl.DataFrame, ledger: pl.DataFrame) -> list[str]:
    """Where the replayed ledger and the day's disagree: other games, or a column that differs,
    floats beyond TOLERANCE."""
    if replayed.columns != ledger.columns:
        return [f"columns differ: {sorted(set(replayed.columns) ^ set(ledger.columns))}"]
    ours, theirs = replayed.sort("game_id"), ledger.sort("game_id")
    if not ours["game_id"].equals(theirs["game_id"]):
        return [f"games differ: {ours['game_id'].to_list()} against {theirs['game_id'].to_list()}"]
    problems = []
    for column in ledger.columns:
        a, b = ours[column], theirs[column]
        if a.dtype.is_float():
            # NaN is no number: one on either side differs, as a null does.
            nan = (a.is_nan() != b.is_nan()).fill_null(False)
            apart = (
                (a.is_null() != b.is_null()) | nan | ((a - b).abs() > TOLERANCE).fill_null(False)
            )
        else:
            apart = a.ne_missing(b)
        if apart.any():
            games = ours.filter(apart)["game_id"].to_list()
            problems.append(f"{column} differs for {games}")
    return problems
