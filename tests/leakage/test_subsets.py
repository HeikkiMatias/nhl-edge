"""Point-in-time rules for gate 2's subsets (#106, ADR 0023, hard rules 1 and 9). A game's flags
read only boxscores public before its as-of time and its own projection: never its own boxscore,
nor one published at or after the as-of time."""

from datetime import UTC, date, datetime, time, timedelta

import numpy as np
import polars as pl
from polars.testing import assert_frame_equal

from nhl_edge.backtest import subsets
from nhl_edge.features import team_strength as ts

SEASON = 20182019
TEAMS = ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF")
UTC_TYPE = pl.Datetime("us", "UTC")


def synthetic(seed: int = 0, days: int = 40) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    """Games, boxscores and projections for a league of six teams playing three games a day, with
    rosters of 15 forwards and 8 defensemen, a trade every few days, and a projection that
    scratches a random few."""
    rng = np.random.default_rng(seed)
    roster = {
        team: {100 * t + k: ("F" if k < 15 else "D") for k in range(23)}
        for t, team in enumerate(TEAMS)
    }
    games, boxscores, projections = [], [], []
    for d in range(days):
        day = date(2018, 10, 4) + timedelta(days=d)
        if d % 4 == 3:
            a, b = rng.choice(len(TEAMS), size=2, replace=False)
            one, other = TEAMS[a], TEAMS[b]
            mover = int(rng.choice(list(roster[one])))
            roster[other][mover] = roster[one].pop(mover)
        order = rng.permutation(len(TEAMS))
        for g in range(3):
            home, away = TEAMS[order[2 * g]], TEAMS[order[2 * g + 1]]
            game_id = 2018020000 + 3 * d + g + 1
            games.append(
                {
                    "game_id": game_id,
                    "season": SEASON,
                    "game_date": day,
                    "start_utc": datetime.combine(day, time(23), UTC),
                    "home": home,
                    "away": away,
                }
            )
            # Some boxscores come out late, past the next games' as-of times.
            delay = timedelta(days=int(rng.integers(1, 4)))
            for team in (home, away):
                forwards = [p for p, r in roster[team].items() if r == "F"]
                defense = [p for p, r in roster[team].items() if r == "D"]
                chances = {p: float(rng.choice([0.1, 0.4, 0.9, 0.95])) for p in roster[team]}
                for player, p in chances.items():
                    projections.append(
                        {
                            "game_id": game_id,
                            "team": team,
                            "player_id": player,
                            "role": roster[team][player],
                            "p_available": p,
                        }
                    )
                played = list(rng.choice(forwards, size=12, replace=False)) + list(
                    rng.choice(defense, size=6, replace=False)
                )
                for player in played:
                    boxscores.append(
                        {
                            "game_id": game_id,
                            "game_date": day,
                            "team": team,
                            "player_id": int(player),
                            "role": roster[team][int(player)],
                            "toi_s": int(rng.integers(300, 1500)),
                            "observed_utc": datetime.combine(day + delay, time(10), UTC),
                        }
                    )
    box = pl.DataFrame(boxscores).with_columns(
        pl.col("observed_utc").cast(UTC_TYPE), pl.col("toi_s").cast(pl.Int32)
    )
    return pl.DataFrame(games), box, pl.DataFrame(projections)


GAMES, BOXSCORES, PROJECTIONS = synthetic()
FLAGS = subsets.flags(GAMES, BOXSCORES, PROJECTIONS, [SEASON], lines={})


def test_the_flags_are_not_vacuous() -> None:
    for name in (*subsets.SUBSETS, "any"):
        assert 0 < FLAGS[name].sum() < FLAGS.height, name


def test_each_day_reads_only_the_boxscores_public_before_its_as_of_time() -> None:
    timed = GAMES.with_columns(as_of_utc=ts.as_of(pl.col("game_date"), pl.col("start_utc")))
    for day in timed["game_date"].unique().sort()[::5]:
        on_day = timed.filter(pl.col("game_date") == day)
        moment = on_day["as_of_utc"].min()
        public = BOXSCORES.filter(pl.col("observed_utc") < moment)
        alone = subsets.flags(on_day, public, PROJECTIONS, [SEASON], lines={})
        expected = FLAGS.filter(pl.col("game_id").is_in(on_day["game_id"].implode()))
        assert_frame_equal(alone, expected)


def test_a_games_own_and_later_boxscores_never_move_its_flags() -> None:
    # Every boxscore of the day's games and after, scrambled: the day's flags stay.
    day = GAMES["game_date"].unique().sort()[20]
    later = pl.col("game_date") >= day
    scrambled = BOXSCORES.with_columns(
        player_id=pl.when(later).then(pl.col("player_id") + 7).otherwise(pl.col("player_id")),
        toi_s=pl.when(later).then(pl.col("toi_s") * 3).otherwise(pl.col("toi_s")),
    )
    on_day = GAMES.filter(pl.col("game_date") == day)["game_id"].implode()
    moved = subsets.flags(GAMES, scrambled, PROJECTIONS, [SEASON], lines={})
    # Later days' flags do move.
    assert not moved.equals(FLAGS)
    assert_frame_equal(
        moved.filter(pl.col("game_id").is_in(on_day)),
        FLAGS.filter(pl.col("game_id").is_in(on_day)),
    )


def test_a_boxscore_public_exactly_at_the_as_of_time_is_not_read() -> None:
    # One earlier game: a skater of another team dressed for the next opponent. Published a day
    # early, it flags the target game as after a trade; published at its as-of time, not.
    games = pl.DataFrame(
        [
            {
                "game_id": game_id,
                "season": SEASON,
                "game_date": day,
                "start_utc": datetime.combine(day, time(23), UTC),
                "home": home,
                "away": away,
            }
            for game_id, day, home, away in (
                (1, date(2018, 10, 4), "AAA", "BBB"),
                (2, date(2018, 10, 5), "CCC", "DDD"),
            )
        ]
    )
    as_of = games.select(ts.as_of(pl.col("game_date"), pl.col("start_utc")))["game_date"][1]
    projections = pl.DataFrame(
        {"game_id": [2], "team": ["CCC"], "player_id": [7], "role": ["F"], "p_available": [0.9]}
    )

    def flags(public: datetime) -> bool:
        boxscores = pl.DataFrame(
            {
                "game_id": [1],
                "game_date": [date(2018, 10, 4)],
                "team": ["AAA"],
                "player_id": [7],
                "role": ["F"],
                "toi_s": [900],
                "observed_utc": [public],
            }
        ).with_columns(pl.col("observed_utc").cast(UTC_TYPE), pl.col("toi_s").cast(pl.Int32))
        found = subsets.flags(games, boxscores, projections, [SEASON], lines={})
        return found.filter(pl.col("game_id") == 2)["trade"].item()

    assert flags(as_of - timedelta(seconds=1))
    assert not flags(as_of)
