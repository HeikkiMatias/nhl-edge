from datetime import UTC, date, datetime, time, timedelta

import polars as pl

from nhl_edge.backtest import subsets

SEASON = 20182019
UTC_TYPE = pl.Datetime("us", "UTC")


def game(game_id: int, day: date, home: str, away: str) -> dict[str, object]:
    return {
        "game_id": game_id,
        "season": SEASON,
        "game_date": day,
        "start_utc": datetime.combine(day, time(23), UTC),
        "home": home,
        "away": away,
    }


def dressed(
    game_id: int,
    day: date,
    team: str,
    skaters: dict[int, str],
    toi: dict[int, int] | None = None,
    public: datetime | None = None,
) -> list[dict[str, object]]:
    observed = public or datetime.combine(day + timedelta(days=1), time(10), UTC)
    return [
        {
            "game_id": game_id,
            "game_date": day,
            "team": team,
            "player_id": player,
            "role": role,
            "toi_s": (toi or {}).get(player, 900),
            "observed_utc": observed,
        }
        for player, role in skaters.items()
    ]


def projected(game_id: int, team: str, chances: dict[int, float]) -> list[dict[str, object]]:
    return [
        {
            "game_id": game_id,
            "team": team,
            "player_id": player,
            # The flags read a projection's skaters, whatever their position.
            "role": "F",
            "p_available": p,
        }
        for player, p in chances.items()
    ]


def test_each_subset_on_a_hand_example() -> None:
    before, day = date(2018, 10, 1), date(2018, 10, 5)
    aaa = {1: "F", 2: "F", 3: "F", 4: "D"}
    ccc = {11: "F", 12: "F", 13: "F", 20: "F", 14: "D"}
    bbb = {21: "F", 22: "F", 23: "F", 24: "D"}
    ddd = {31: "F", 32: "F", 33: "F", 34: "D"}
    # EEE dresses 12 forwards, the last three with the least ice time, and 4 defensemen.
    eee = {p: "F" for p in range(41, 53)} | {p: "D" for p in range(53, 57)}
    eee_toi = {p: 1200 - 10 * p for p in eee}
    fff = {61: "F", 62: "F", 63: "F", 64: "D"}
    ari = {71: "F", 72: "F", 73: "F", 74: "D"}
    iii = {81: "F", 82: "F", 83: "F", 84: "D"}
    games = pl.DataFrame(
        [
            game(1, before, "AAA", "CCC"),
            game(2, before, "BBB", "DDD"),
            game(6, before, "EEE", "FFF"),
            game(10, before, "ARI", "III"),
            # Played the day before, published only after the targets' as-of time.
            game(9, day - timedelta(days=1), "GGG", "HHH"),
            game(3, day, "AAA", "DDD"),
            game(4, day, "BBB", "CCC"),
            game(7, day, "EEE", "FFF"),
            game(11, day, "UTA", "III"),
        ]
    )
    late = datetime.combine(day + timedelta(days=1), time(10), UTC)
    boxscores = pl.DataFrame(
        dressed(1, before, "AAA", aaa)
        + dressed(1, before, "CCC", ccc)
        + dressed(2, before, "BBB", bbb)
        + dressed(2, before, "DDD", ddd)
        + dressed(6, before, "EEE", eee, eee_toi)
        + dressed(6, before, "FFF", fff)
        + dressed(10, before, "ARI", ari)
        + dressed(10, before, "III", iii)
        # DDD's 31 dressed for GGG, but that is public only after game 3's as-of time.
        + dressed(9, day - timedelta(days=1), "GGG", {31: "F", 91: "F"}, public=late)
        + dressed(9, day - timedelta(days=1), "HHH", {95: "F"}, public=late)
    ).with_columns(pl.col("observed_utc").cast(UTC_TYPE), pl.col("toi_s").cast(pl.Int32))
    projections = pl.DataFrame(
        # AAA's regular 2 is unlikely to play: an injury, but one skater is no lineup change.
        projected(3, "AAA", {1: 0.9, 2: 0.3, 3: 0.9, 4: 0.9})
        + projected(3, "DDD", dict.fromkeys(ddd, 0.9))
        # BBB adds CCC's 20: a trade, and for CCC a regular missing.
        + projected(4, "BBB", dict.fromkeys(bbb, 0.9) | {20: 0.9})
        + projected(4, "CCC", {11: 0.9, 12: 0.9, 13: 0.9, 14: 0.9})
        # EEE leaves out its three forwards with the least ice time: a lineup change only.
        + projected(7, "EEE", {p: 0.9 for p in eee if p not in (50, 51, 52)})
        + projected(7, "FFF", dict.fromkeys(fff, 0.9))
        # ARI moved to Utah: the same line, so no trade.
        + projected(11, "UTA", dict.fromkeys(ari, 0.9))
        + projected(11, "III", dict.fromkeys(iii, 0.9))
    )
    flags = subsets.flags(games, boxscores, projections, [SEASON], lines={"UTA": "ARI"})
    rows = {row["game_id"]: row for row in flags.iter_rows(named=True)}
    expected = {
        3: (False, True, False),
        4: (True, True, False),
        7: (False, False, True),
        11: (False, False, False),
    }
    for game_id, (trade, injury, change) in expected.items():
        row = rows[game_id]
        assert (row["trade"], row["injury"], row["lineup_change"]) == (trade, injury, change), (
            game_id
        )
        assert row["any"] == (trade or injury or change)
    # The first games have no history: in no subset.
    for game_id in (1, 2, 6, 9, 10):
        assert not rows[game_id]["any"]
    assert flags["game_id"].to_list() == sorted(rows)
    # Another season's games are not flagged.
    assert subsets.flags(games, boxscores, projections, [20172018], lines={}).height == 0
