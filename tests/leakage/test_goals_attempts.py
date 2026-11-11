"""Point in time for scripts/goals_attempts.py (#420, hard rule 1): a rate never counts a team-game
public at or after its as-of time, the game's own included; later games change no earlier rate;
the pull is chosen on 2012-13 without any later game; and the script reads the lake only to the
explore half."""

from datetime import timedelta
from typing import Any

import goals_attempts_fixtures as f
import polars as pl
import pytest

from nhl_edge.backtest import yardstick as ys
from nhl_edge.features import team_strength as ts

ga = f.load()
LEAGUE = f.league()
HISTORY = f.history(LEAGUE)
GAMES = LEAGUE["games"]
PULLS = {m: 10.0 for m in ga.MEASURES}


def test_a_team_game_public_at_the_as_of_time_is_not_counted() -> None:
    game = GAMES.filter(pl.col("season") == 20122013).row(0, named=True)
    target = pl.DataFrame(
        {"team": [game["home"]], "season": [game["season"]]},
        schema={"team": pl.String, "season": pl.Int32},
    ).with_columns(as_of_utc=pl.lit(game["start_utc"] - timedelta(hours=1)))
    as_of = target["as_of_utc"].item()
    past = HISTORY.filter(pl.col("observed_utc") < as_of)
    one = past.filter(pl.col("team") == game["home"]).sort("observed_utc").tail(1)
    last = pl.col("game_id") == one["game_id"].item()
    before = ga.team_sums(past, target)
    # The same team-game made public exactly at the as-of time drops out; a microsecond
    # earlier, it counts again.
    at = past.filter(~last).vstack(one.with_columns(observed_utc=pl.lit(as_of)))
    assert ga.team_sums(at, target)["games"].item() < before["games"].item()
    just = at.with_columns(
        observed_utc=pl.when(last)
        .then(pl.lit(as_of - timedelta(microseconds=1)))
        .otherwise("observed_utc")
    )
    assert ga.team_sums(just, target)["games"].item() == before["games"].item()


def test_a_games_own_counts_never_enter_its_own_columns() -> None:
    game = GAMES.filter(pl.col("season") == 20122013).row(5, named=True)
    own = pl.col("game_id") == game["game_id"]
    changed = HISTORY.with_columns(
        gf=pl.when(own).then(pl.col("gf") + 10).otherwise("gf"),
        cf=pl.when(own).then(pl.col("cf") + 50).otherwise("cf"),
        xgf_5v5=pl.when(own).then(pl.col("xgf_5v5") + 5).otherwise("xgf_5v5"),
    )
    games = GAMES.filter(own)
    assert ga.columns_for(games, changed, PULLS).equals(ga.columns_for(games, HISTORY, PULLS))
    # A later game of the same team does see it.
    later = GAMES.filter(
        (pl.col("game_date") > game["game_date"])
        & ((pl.col("home") == game["home"]) | (pl.col("away") == game["home"]))
    ).head(1)
    assert not ga.columns_for(later, changed, PULLS).equals(ga.columns_for(later, HISTORY, PULLS))


def test_later_games_change_no_earlier_column() -> None:
    cut = GAMES.filter(pl.col("season") == 20122013)["game_date"].sort()[10]
    earlier = GAMES.filter(pl.col("game_date") <= cut)
    known = HISTORY.filter(pl.col("game_date") < cut)
    full = ga.columns_for(earlier, HISTORY, PULLS)
    assert ga.columns_for(earlier, known, PULLS).equals(full)
    # And with a whole later season appended, whose games turn public a year on.
    longer = pl.concat([HISTORY, f.later(HISTORY)])
    assert ga.columns_for(earlier, longer, PULLS).equals(full)


def test_the_pull_reads_no_game_after_its_season() -> None:
    # Two later seasons' games, with three times the goals and attempts, change no loss.
    longer = pl.concat([HISTORY, f.later(HISTORY).with_columns(pl.col("gf", "cf") * 3)])
    games = pl.concat([GAMES, f.later(GAMES)])
    for measure in ga.MEASURES:
        assert ga.pull_losses(longer, games, measure, season=20122013) == ga.pull_losses(
            HISTORY, GAMES, measure, season=20122013
        )


def test_every_column_is_rated_at_team_strengths_as_of_time() -> None:
    found = ga.columns_for(GAMES, HISTORY, PULLS)
    expected = GAMES.select("game_id", t=ts.as_of(pl.col("game_date"), pl.col("start_utc")))
    both = found.join(expected, on="game_id")
    assert both["as_of_utc"].equals(both["t"])
    # Strictly before puck drop, and before every feed of the game itself is public.
    starts = both.join(GAMES.select("game_id", "start_utc", "observed_utc"), on="game_id")
    assert (starts["as_of_utc"] < starts["start_utc"]).all()
    assert (starts["as_of_utc"] < starts["observed_utc"]).all()


def test_the_script_reads_the_lake_only_to_the_explore_half(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[int] = []
    seen: dict[str, int] = {}

    class Lake:
        def read(self, table: str) -> pl.DataFrame:
            return pl.DataFrame({"season": [max(ys.EXPLORE), max(ys.EXPLORE) + 10001]})

    def read(lake: Any, last: int) -> None:
        asked.append(last)

    def team_games(*frames: pl.DataFrame) -> None:
        for i, frame in enumerate(frames):
            seen[str(i)] = int(frame["season"].max())  # type: ignore[arg-type]
        raise RuntimeError("stop here")

    monkeypatch.setattr(ga.ys, "read", read)
    monkeypatch.setattr(ga, "team_games", team_games)
    with pytest.raises(RuntimeError, match="stop here"):
        ga.run(Lake())
    assert asked == [max(ys.EXPLORE)]
    assert len(seen) == 4 and set(seen.values()) == {max(ys.EXPLORE)}
