"""Live target rows are history's rows (#162, hard rules 1 and 9). On the morning of a night, the
lake holds only the games before it, and the night's games come as a slate. Each builder, given
that lake with the slate appended as targets the way its command does (live/targets.py), must
give every one of the night's games the row the history path gives it from the whole season.

So a target row is the quantity B3 was trained and tested on. The game's own boxscore, stints,
shots, penalties and result, and every later game, are absent from the live inputs: they can't
reach a target row.
"""

from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import finishing_fixtures as ff
import goalie_fixtures as gf
import lineup_fixtures as lf
import penalty_fixtures as pf
import polars as pl
import rapm_fixtures as rf
import schedule_fixtures as sf
import team_fixtures as tf
from polars.testing import assert_frame_equal

from nhl_edge.features import goalie as ge
from nhl_edge.features import schedule_terms as st
from nhl_edge.features import team_strength as ts
from nhl_edge.lake.schemas import ExpectedPowerPlays, Finishing, GoalMultipliers, PenaltyRates
from nhl_edge.lineup import goalie_start as gs
from nhl_edge.lineup import minutes as mins
from nhl_edge.lineup import projection as proj
from nhl_edge.live.targets import in_time, rated, with_schedule_targets, with_targets
from nhl_edge.ratings import finishing as fn
from nhl_edge.ratings import penalty_rates as pr
from nhl_edge.ratings import rapm
from nhl_edge.reference import Reference

# The night's slate is fetched at 09:00 UTC, before any game's as-of time.
FETCHED = time(9)


def version(component: str) -> str:
    return f"{component}-20261007-abc1234"


def night_of(games: pl.DataFrame, season: int, k: int = 10) -> date:
    return games.filter(pl.col("season") == season)["game_date"].unique().sort()[k]


def slate_of(games: pl.DataFrame, night: date, fetched: datetime | None = None) -> pl.DataFrame:
    """The night's games as its slate lists them that morning: schedule columns, no result. The
    slate goes through in_time, as every builder's --targets does."""
    tonight = games.filter(pl.col("game_date") == night)
    columns = tonight.columns
    return tonight.select(
        "game_id",
        "season",
        "game_date",
        "start_utc",
        "home",
        "away",
        venue=pl.col("venue") if "venue" in columns else pl.lit("Arena"),
        neutral_site=pl.col("neutral_site") if "neutral_site" in columns else pl.lit(False),
        limited_attendance=pl.lit(False),
        game_state=pl.lit("FUT"),
        observed_utc=pl.lit(fetched or datetime.combine(night, FETCHED, UTC)),
        raw_key=pl.lit("nhl/schedule/test"),
    ).pipe(in_time)


def before(frame: pl.DataFrame, games: pl.DataFrame, night: date) -> pl.DataFrame:
    """A per-game table as the lake holds it that morning: the games before the night."""
    earlier = games.filter(pl.col("game_date") < night).select("game_id")
    return frame.join(earlier, on="game_id", how="semi")


def tonight(frame: pl.DataFrame, games: pl.DataFrame, night: date) -> pl.DataFrame:
    ids = games.filter(pl.col("game_date") == night)["game_id"].implode()
    return frame.filter(pl.col("game_id").is_in(ids))


def same(history: pl.DataFrame, live: pl.DataFrame, keys: list[str]) -> None:
    assert live.height > 0
    assert_frame_equal(live.sort(keys), history.sort(keys), check_column_order=False)


def test_team_strength() -> None:
    league = tf.league()
    games, season = league["games"], tf.SEASONS[1]
    night = night_of(games, season)
    settings = ts.Settings(half_life=20, prior_games=10)

    def strength(frames: dict[str, pl.DataFrame], rated_games: pl.DataFrame) -> pl.DataFrame:
        history = ts.team_games(frames["shots"], frames["shot_xg"], frames["strength_time"])
        return ts.rows(
            rated_games.filter(pl.col("season") == season),
            history,
            settings,
            version(ts.COMPONENT),
        )

    full = strength(league, games)
    lake = {name: before(frame, games, night) for name, frame in league.items()}
    live = strength(lake, with_targets(lake["games"], slate_of(games, night)))
    same(tonight(full, games, night), tonight(live, games, night), ["game_id"])
    # Not vacuous: each target read its teams' earlier games.
    assert (tonight(live, games, night)["home_history"] > 0).all()
    # Targets leave every other game's row as it was.
    same(strength(lake, lake["games"]), live.filter(pl.col("game_date") < night), ["game_id"])


def test_goalie_starts() -> None:
    league = gf.league()
    games, lineups, season = league["games"], league["lineups"], gf.SEASONS[2]
    night = night_of(games, season)
    full, _, _ = gs.score(lineups, games, [season], version(gs.COMPONENT))
    live_games = with_targets(before(games, games, night), slate_of(games, night))
    live, _, _ = gs.score(
        before(lineups, games, night), live_games, [season], version(gs.COMPONENT)
    )
    same(
        tonight(full, games, night),
        tonight(live, games, night),
        ["game_id", "team", "goalie_id"],
    )


def test_lineups_and_replacements() -> None:
    league = lf.league()
    games, boxscores, season = league["games"], league["lineups"], lf.SEASONS[2]
    night = night_of(games, season, 20)
    minutes = lf.minutes_frame(boxscores)

    def tables(
        games: pl.DataFrame, boxscores: pl.DataFrame, minutes: pl.DataFrame
    ) -> tuple[pl.DataFrame, pl.DataFrame]:
        # The order nhl lineups runs them in, after nhl goalie-start wrote the goalies.
        rows = proj.candidates(games, boxscores, {})
        skaters, scored, _ = proj.score(
            boxscores, games, [season], version(proj.COMPONENT), {}, rows
        )
        constants = {season: mins.season_constants(minutes, rows, season, games)}
        projected, replacements = mins.project(scored, minutes, constants, games, {})
        skaters = mins.with_minutes(skaters, projected, constants)
        starts, _, _ = gs.score(boxscores, games, [season], version(gs.COMPONENT))
        table = proj.with_goalies(skaters, starts)
        return table, mins.replacement_table(replacements, skaters)

    full, full_spare = tables(games, boxscores, minutes)
    live, live_spare = tables(
        with_targets(before(games, games, night), slate_of(games, night)),
        before(boxscores, games, night),
        before(minutes, games, night),
    )
    same(
        tonight(full, games, night),
        tonight(live, games, night),
        ["game_id", "team", "player_id"],
    )
    same(
        tonight(full_spare, games, night),
        tonight(live_spare, games, night),
        ["game_id", "team", "role"],
    )
    # Not vacuous: the targets' skaters have minutes from their earlier games.
    skaters = tonight(live, games, night).filter(pl.col("role") != "G")
    assert (skaters["exp_5v5"] > 0).any()


def test_goalie_effects() -> None:
    league = gf.with_shots(gf.league())
    games, season = league["games"], gf.SEASONS[2]
    night = night_of(games, season)
    settings = ge.Settings(half_life=20, prior_shots=200)

    def effects(frames: dict[str, pl.DataFrame], games: pl.DataFrame) -> pl.DataFrame:
        # nhl goalie-start, then nhl goalie-effect on its rows of the games rated.
        starts, _, _ = gs.score(frames["lineups"], games, [season], version(gs.COMPONENT))
        goalies = ge.goalie_games(frames["shots"], frames["shot_xg"])
        team_shots = ge.team_shot_games(games, frames["shots"], frames["shot_xg"])
        season_games = games.filter(pl.col("season") == season)
        candidates = rated(starts.filter(pl.col("season") == season), season_games)
        frame = ge.effects(candidates, season_games, goalies, team_shots, settings)
        return ge.rows(frame, settings, version(ge.COMPONENT))

    full = effects(league, games)
    lake = {name: before(frame, games, night) for name, frame in league.items()}
    live = effects(lake, with_targets(lake["games"], slate_of(games, night)))
    same(
        tonight(full, games, night),
        tonight(live, games, night),
        ["game_id", "team", "goalie_id"],
    )


def test_schedule_terms() -> None:
    league = sf.league()
    games, schedule, season = league["games"], league["schedule"], 20122013
    night = night_of(games, season)
    settings, ref = st.Settings(prior_games=100), Reference.load()
    terms = st.terms(schedule, games, settings, [season], ref)
    full = st.rows(terms, settings, version(st.COMPONENT))
    live_schedule = with_schedule_targets(before(schedule, games, night), slate_of(schedule, night))
    live_terms = st.terms(live_schedule, before(games, games, night), settings, [season], ref)
    live = st.rows(live_terms, settings, version(st.COMPONENT))
    same(tonight(full, games, night), tonight(live, games, night), ["game_id"])
    alone = st.terms(
        before(schedule, games, night), before(games, games, night), settings, [season], ref
    )
    same(
        st.rows(alone, settings, version(st.COMPONENT)),
        live.filter(pl.col("game_date") < night),
        ["game_id"],
    )


def test_a_game_fetched_after_its_as_of_time_is_no_target() -> None:
    # Rated at the fetch time, a game fetched the next morning would read its own result and the
    # rest it gave its teams (the leakage audit of #162). It gets no row instead.
    league = sf.league()
    games, schedule = league["games"], league["schedule"]
    night = night_of(games, 20122013)
    as_of = ts.as_of(pl.col("game_date"), pl.col("start_utc"))
    times = schedule.filter(pl.col("game_date") == night).select(as_of_utc=as_of)["as_of_utc"]
    first, last = times.min(), times.max()
    assert isinstance(first, datetime) and isinstance(last, datetime)
    next_morning = datetime.combine(night + timedelta(days=1), time(11), UTC)
    assert slate_of(schedule, night, next_morning).is_empty()
    # At the as-of time is too late; just before it is on time.
    assert slate_of(schedule, night, last).is_empty()
    assert slate_of(schedule, night, first - timedelta(microseconds=1)).height == times.len()


def test_rapm_ratings_and_terms() -> None:
    league = rf.league()
    games, season = league["games"], rf.SEASONS[1]
    night = night_of(games, season)
    settings = rapm.Settings(half_life_days=20.0, pull_hours=2.0)

    def ratings(
        games: pl.DataFrame, stints: pl.DataFrame, roles: pl.DataFrame
    ) -> tuple[pl.DataFrame, pl.DataFrame]:
        wanted = rapm.targets(rated(league["lineups"], games), games, [season])
        wanted = wanted.filter(pl.col("game_date") <= night)
        frames, terms, _ = rapm.rate(
            rf.seasons_of(stints),
            games,
            roles,
            league["venues"],
            wanted,
            settings,
            version(rapm.COMPONENT),
        )
        return frames, terms.filter(pl.col("game_date") == night)

    full, full_terms = ratings(games, league["stints"], league["roles"])
    live, live_terms = ratings(
        with_targets(before(games, games, night), slate_of(games, night)),
        before(league["stints"], games, night),
        before(league["roles"], games, night),
    )
    same(
        tonight(full, games, night),
        tonight(live, games, night),
        ["game_id", "player_id", "component"],
    )
    same(full_terms, live_terms, ["model", "term"])


def _player_inputs(f: dict[str, Any], games: pl.DataFrame) -> tuple[pl.DataFrame, pl.Expr]:
    """The candidates of the games rated with their as-of times, as both commands join them."""
    as_of = ts.as_of(pl.col("game_date"), pl.col("start_utc"))
    candidates = rated(f["lineups"], games).join(
        games.select("game_id", as_of_utc=as_of), on="game_id", how="left"
    )
    return candidates, as_of


def _live_frames(
    league: dict[str, pl.DataFrame], night: date, features: tuple[str, ...]
) -> tuple[dict[str, pl.DataFrame], pl.DataFrame]:
    """The lake that morning: the feeds of earlier games, the feature rows of every game up to the
    night (written by the builders before), and the games with the night's slate."""
    games = league["games"]
    live_games = with_targets(before(games, games, night), slate_of(games, night))
    frames = {
        name: rated(frame, live_games) if name in features else before(frame, games, night)
        for name, frame in league.items()
        if "game_id" in frame.columns
    }
    return frames, live_games


def test_expected_power_plays() -> None:
    league = pf.league()
    games, season = league["games"], pf.SEASONS[1]
    night = night_of(games, season)

    def tables(f: dict[str, Any], games: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
        # The order nhl power-plays runs them in.
        minutes = mins.player_minutes(f["stints"], f["actual_lineups"])
        weighted = pr.unoffset(f["penalties"])
        rows = pr.player_games(minutes, weighted, games)
        pulls = {season: pr.season_pulls(rows, season, games)}
        candidates, as_of = _player_inputs(f, games)
        times = games.filter(pl.col("season") == season).select("season", as_of_utc=as_of)
        rates = pr.rates(
            candidates.select(
                "game_id", "season", "game_date", "team", "player_id", "role", "as_of_utc"
            ),
            rows,
            pulls,
        )
        history = pr.team_games(f["strength_time"], weighted, f["shots"], f["shot_xg"])
        expected = pr.expected(
            rates,
            candidates,
            f["lineup_replacements"],
            pr.role_rates(rows, times),
            pr.league_figures(history, times),
            games,
        )
        return (
            pr.stamp(rates, PenaltyRates, pulls, version(pr.COMPONENT)),
            pr.stamp(expected, ExpectedPowerPlays, pulls, version(pr.COMPONENT)),
        )

    season_league = {**league, "lineups": league["lineups"].filter(pl.col("season") == season)}
    full, full_expected = tables(season_league, games)
    frames, live_games = _live_frames(season_league, night, ("lineups", "lineup_replacements"))
    live, live_expected = tables(frames, live_games)
    same(
        tonight(full, games, night),
        tonight(live, games, night),
        ["game_id", "player_id", "component"],
    )
    same(
        tonight(full_expected, games, night),
        tonight(live_expected, games, night),
        ["game_id", "team"],
    )


def test_goal_multipliers() -> None:
    league = ff.league()
    games, season = league["games"], ff.SEASONS[1]
    night = night_of(games, season)

    def tables(f: dict[str, Any], games: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
        # The order nhl finishing runs them in.
        minutes = mins.player_minutes(f["stints"], f["actual_lineups"])
        rows = fn.shooter_games(minutes, f["shots"], f["shot_xg"], games)
        pulls = {season: fn.season_pulls(rows, season, games)}
        candidates, as_of = _player_inputs(f, games)
        times = games.filter(pl.col("season") == season).select("season", as_of_utc=as_of)
        rates = fn.rates(
            candidates.select(
                "game_id", "season", "game_date", "team", "player_id", "role", "as_of_utc"
            ),
            rows,
            pulls,
        )
        shared, multipliers = fn.multipliers(
            rates,
            candidates,
            f["lineup_replacements"],
            fn.league_rates(rows, times),
            rated(f["goalie_effects"], games),
            fn.shot_figures(f["shots"], f["shot_xg"], times),
            games,
        )
        return (
            fn.stamp(shared, Finishing, pulls, version(fn.COMPONENT)),
            fn.stamp(multipliers, GoalMultipliers, pulls, version(fn.COMPONENT)),
        )

    season_league = {
        **league,
        "lineups": league["lineups"].filter(pl.col("season") == season),
        "goalie_effects": league["goalie_effects"].filter(pl.col("season") == season),
    }
    full, full_multipliers = tables(season_league, games)
    frames, live_games = _live_frames(
        season_league, night, ("lineups", "lineup_replacements", "goalie_effects")
    )
    live, live_multipliers = tables(frames, live_games)
    same(
        tonight(full, games, night),
        tonight(live, games, night),
        ["game_id", "player_id"],
    )
    same(
        tonight(full_multipliers, games, night),
        tonight(live_multipliers, games, night),
        ["game_id", "team", "goalie_id"],
    )
