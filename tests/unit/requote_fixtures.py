"""SYNTHETIC fixtures for requote after a starter change (ADR 0059, #410): a league's day as a
publication's bundle, its models read at CUTOFF, and Daily Faceoff polls naming its starters."""

import json
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import polars as pl
from b2_fixtures import feature_tables
from b3_fixtures import league

from nhl_edge.backtest.walk_forward import fold_start
from nhl_edge.game import uncertainty
from nhl_edge.ingest.odds import ODDS_FRAME_SCHEMA
from nhl_edge.lake.schemas import PlayerLeagueSeasons, Slate, dtypes
from nhl_edge.lineup.confirmed import DFO_CONFIRMED
from nhl_edge.live import blend_fit
from nhl_edge.live import bundle as lb
from nhl_edge.live import kalshi_quotes as kq
from nhl_edge.live import predict as lp
from nhl_edge.live import requote as rq

LEAGUE = league()
TEST = 20182019
START = fold_start(LEAGUE.games.select("season", "start_utc"), TEST)
FEATURES = feature_tables(LEAGUE.games, 4)
# The columns a starter outside the candidates is rated from (confirmed.adjusted), which the
# models themselves don't read: an effect per shot over 30 shots, and no xG per shot.
SHOTS = 30.0
EFFECTS = FEATURES.goalie_effects.with_columns(
    effect=pl.col("goals_saved") / SHOTS, goalie_games=pl.lit(20), expected_shots=pl.lit(SHOTS)
)
B2_TABLES = replace(FEATURES, games=LEAGUE.games, goalie_effects=EFFECTS)
B3_TABLES = replace(
    LEAGUE,
    schedule_terms=B2_TABLES.schedule_terms,
    goalie_starts=B2_TABLES.goalie_starts,
    actual_lineups=B2_TABLES.actual_lineups,
    goal_multipliers=LEAGUE.goal_multipliers.with_columns(xg_per_shot=pl.lit(None, pl.Float64)),
)
U_TABLES = uncertainty.Tables(
    LEAGUE.games,
    B2_TABLES.goalie_starts,
    LEAGUE.lineups,
    LEAGUE.lineup_replacements,
    B2_TABLES.actual_lineups,
    pl.DataFrame(schema=dtypes(PlayerLeagueSeasons)),
)
LIVE = blend_fit.load(
    json.loads((blend_fit.REPORTS / "blend-live-20261007-89ec631.json").read_text())
)
UTC_TYPE = pl.Datetime("us", "UTC")


def slate() -> pl.DataFrame:
    """The B3 fixture's test season, its tenth game date, as a day's slate: four games at 23:00."""
    games = LEAGUE.games.filter(pl.col("season") == TEST)
    day = games["game_date"].unique().sort()[9]
    return Slate.validate(
        games.filter(pl.col("game_date") == day)
        .with_columns(game_state=pl.lit("FUT"))
        .select(list(dtypes(Slate)))
    )


SLATE = slate()
GAME = int(SLATE["game_id"][0])  # TOR at CHI
STARTS = SLATE["start_utc"][0]
CUTOFF = STARTS - timedelta(hours=6, minutes=15)
PUBLISHED = CUTOFF + timedelta(minutes=4)
CHANGE = STARTS - timedelta(hours=4)


def letters(goalie_id: int) -> str:
    """A name of letters only, so name keys tell goalies apart."""
    return "".join(chr(ord("a") + int(d)) for d in str(goalie_id))


def candidates(team: str) -> list[int]:
    """The team's candidates for GAME, likeliest first."""
    rows = B2_TABLES.goalie_starts.filter(pl.col("game_id") == GAME, pl.col("team") == team)
    return rows.sort("p_start", descending=True)["goalie_id"].to_list()


def players(effects: pl.DataFrame = EFFECTS) -> pl.DataFrame:
    """A name of letters for every goalie of the candidates and of effects."""
    ids = pl.concat([B2_TABLES.goalie_starts["goalie_id"], effects["goalie_id"]]).unique()
    return pl.DataFrame(
        {"player_id": ids, "name": [f"Goalie {letters(i)}" for i in ids.to_list()]},
        schema={"player_id": pl.Int64, "name": pl.String},
    )


def polls(*named: tuple[datetime, str, int]) -> pl.DataFrame:
    """Daily Faceoff polls for GAME: (observed, team, goalie_id), each Confirmed."""
    game = SLATE.filter(pl.col("game_id") == GAME).row(0, named=True)
    return pl.DataFrame(
        [
            {
                "season": game["season"],
                "game_date": game["game_date"],
                "start_utc": game["start_utc"],
                "team": team,
                "is_home": team == game["home"],
                "goalie_name": f"Goalie {letters(goalie)}",
                "dfo_goalie_id": goalie,
                "status": DFO_CONFIRMED,
                "reported_utc": observed,
                "source_url": None,
                "observed_utc": observed,
                "raw_key": "dailyfaceoff/x",
            }
            for observed, team, goalie in named
        ],
        schema={
            "season": pl.Int32,
            "game_date": pl.Date,
            "start_utc": UTC_TYPE,
            "team": pl.String,
            "is_home": pl.Boolean,
            "goalie_name": pl.String,
            "dfo_goalie_id": pl.Int64,
            "status": pl.String,
            "reported_utc": UTC_TYPE,
            "source_url": pl.String,
            "observed_utc": UTC_TYPE,
            "raw_key": pl.String,
        },
    )


NO_NHL = pl.DataFrame(
    schema={
        "game_id": pl.Int64,
        "team": pl.String,
        "observed_utc": UTC_TYPE,
        "starter_id": pl.Int64,
    }
)


def calls(dailyfaceoff: pl.DataFrame | None = None, effects: pl.DataFrame = EFFECTS) -> rq.Calls:
    """What the starters are read from: the polls, the names, and the lake's goalie effects."""
    return rq.Calls(
        NO_NHL, polls() if dailyfaceoff is None else dailyfaceoff, players(effects), effects
    )


BACKUP = candidates("CHI")[1]
CONFIRMED_BACKUP = polls((CHANGE, "CHI", BACKUP))


def write_bundle(root: Path) -> lb.Bundle:
    """The day's bundle under root, its models read at CUTOFF, as a publication saves it."""
    store = lb.LocalStore(root)
    moments = SLATE.select("game_id", prediction_utc=pl.lit(CUTOFF))
    fitted = lp.models(B2_TABLES, B3_TABLES, U_TABLES, SLATE, moments, START, TEST)
    assert fitted.b2_model is not None and fitted.b3_model is not None
    rows = lb.inputs(
        SLATE,
        pl.DataFrame({"table": ["slate"]}),
        pl.DataFrame(schema=ODDS_FRAME_SCHEMA),
        (B2_TABLES, B3_TABLES, U_TABLES),
    )
    identity = {
        "day": str(SLATE["game_date"][0]),
        "input_cutoff": CUTOFF.isoformat(),
        "blend_version": LIVE.version,
    }
    fits = {"b2": fitted.b2_model, "b3": fitted.b3_model}
    lb.write_once(store, SLATE["game_date"][0], rows, fits, identity, {})
    return lb.read(store, SLATE["game_date"][0])


def priced(game_id: int = GAME) -> dict[str, object]:
    return {"game_id": game_id, "home_price": 1.9, "away_price": 2.05}


def publication(
    bundle: lb.Bundle | None, p_blend: float, at: datetime = PUBLISHED
) -> rq.Publication:
    rows = pl.DataFrame(
        [priced() | {"status": lp.PREDICTED, "p_blend": p_blend, "published_utc": at}],
        schema={
            "game_id": pl.Int64,
            "home_price": pl.Float64,
            "away_price": pl.Float64,
            "status": pl.String,
            "p_blend": pl.Float64,
            "published_utc": UTC_TYPE,
        },
    )
    return rq.Publication(at, rows, bundle)


def games() -> list[kq.Game]:
    return [
        kq.Game(r["game_id"], r["season"], r["game_date"], r["home"], r["away"], r["start_utc"])
        for r in SLATE.iter_rows(named=True)
    ]
