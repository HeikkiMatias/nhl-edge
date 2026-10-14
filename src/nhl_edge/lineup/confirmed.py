"""Starting goalies named before a decision (ADR 0037, #222): policy v2's starter of each team at
the 12:45 ET decision, from the pre-game polls, and the goalie rows B2, B3 and u then read.

**The calls** of each team-game, from the polls observed before its moment (hard rule 1):
- the NHL's starter flag at its latest poll (pregame_goalies, #42);
- Daily Faceoff's goalie and status at the latest poll of the team's listing whose start is nearest
  the game's (dailyfaceoff_goalies, #48). A page can list a team twice on a date, such as a stale
  listing of a re-timed game, so each listing keeps its own polls.

**The starter**, in ADR 0037's order: the NHL's flag; else Daily Faceoff's Confirmed; else its
Likely; else none, and the goalie-start model's mix stands.
- **Daily Faceoff's name** is an NHL goalie when it matches one uniquely, in this order: by
  name_key among the team's candidates for the game; by surname among them, since a first name
  can differ between sources (Matthew Murray for the NHL's Matt Murray); by name_key among the
  goalies with an earlier goalie-effect row. A name matching none is UNKNOWN_GOALIE, rated as a
  goalie with no earlier game.
- **His chance of starting** is 1 when the NHL flags him or Daily Faceoff confirms him. When
  Daily Faceoff calls him Likely, it is the larger of likely_share (L, measured once before the
  freeze) and the goalie-start model's own p_start for him.

**The rows** (adjusted): for each team-game with a starter, goalie_starts holds him at his chance,
and the other candidates share the rest in the goalie-start model's proportions (none once he is
certain). Every row of the team-game is known from the later of its own time and the call's. A
starter outside the candidates gets a goalie_effects row and the opponent's goal_multipliers row
from his latest goalie-effect row known before the cutoff, from any team: his effect then, his
new team's expected shots in this game, and gamma from the attacking team's xG per shot (ADR
0022). So u's goalie doubt, 1 - the likeliest p_start, is 0 for a certain starter and 1 - his
chance for a likely one (ADR 0026).
"""

import re
import unicodedata
from datetime import datetime

import polars as pl

NHL = "nhl"
CONFIRMED = "confirmed"
LIKELY = "likely"
# Daily Faceoff's statuses (#48). Any other, or none, names no starter.
DFO_CONFIRMED = "Confirmed"
DFO_LIKELY = "Likely"
# A named starter who matches no NHL goalie: rated as a goalie with no earlier game.
UNKNOWN_GOALIE = -1

CALL_COLUMNS = ("nhl_id", "nhl_utc", "dfo_name", "dfo_status", "dfo_utc")


def name_key(name: str | None) -> str | None:
    """A goalie's name without accents, case, punctuation or spaces, for matching across sources:
    Ukko-Pekka Luukkonen and Ukko Pekka Luukkonen are one goalie."""
    if name is None:
        return None
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z]", "", ascii_name.lower())


def surname_key(name: str | None) -> str | None:
    """name_key of a name's last word: Matthew Murray and Matt Murray share one."""
    if name is None or not name.split():
        return None
    return name_key(name.split()[-1])


def key(column: str) -> pl.Expr:
    """name_key of a name column."""
    return pl.col(column).map_elements(name_key, return_dtype=pl.String)


def surname(column: str) -> pl.Expr:
    """surname_key of a name column."""
    return pl.col(column).map_elements(surname_key, return_dtype=pl.String)


def calls(team_games: pl.DataFrame, nhl: pl.DataFrame, dailyfaceoff: pl.DataFrame) -> pl.DataFrame:
    """team_games (game_id, game_date, team, start_utc, moment_utc) with what each source said at
    its latest poll observed before moment_utc: the NHL's flagged starter (nhl_id, null when none)
    and Daily Faceoff's goalie and status (dfo_name, dfo_status), with each poll's time."""
    moments = team_games.select("game_id", "game_date", "team", "start_utc", "moment_utc")
    nhl_side = (
        nhl.select("game_id", "team", "observed_utc", "starter_id")
        .join(moments.select("game_id", "team", "moment_utc"), on=["game_id", "team"])
        .filter(pl.col("observed_utc") < pl.col("moment_utc"))
        .sort("observed_utc")
        .group_by("game_id", "team")
        .agg(nhl_id=pl.col("starter_id").last(), nhl_utc=pl.col("observed_utc").last())
    )
    listed = (
        dailyfaceoff.select(
            "game_date", "team", "observed_utc", "goalie_name", "status", listing="start_utc"
        )
        .join(moments, on=["game_date", "team"])
        .filter(pl.col("observed_utc") < pl.col("moment_utc"))
        .with_columns(gap=(pl.col("listing") - pl.col("start_utc")).abs())
    )
    nearest = listed.group_by("game_id", "team").agg(pl.col("gap").min())
    dfo_side = (
        listed.join(nearest, on=["game_id", "team", "gap"])
        .sort("observed_utc")
        .group_by("game_id", "team")
        .agg(
            dfo_name=pl.col("goalie_name").last(),
            dfo_status=pl.col("status").last(),
            dfo_utc=pl.col("observed_utc").last(),
        )
    )
    return team_games.join(nhl_side, on=["game_id", "team"], how="left").join(
        dfo_side, on=["game_id", "team"], how="left"
    )


def _ids(names: pl.DataFrame, by: list[str], keyed_by: str = "name_key") -> pl.DataFrame:
    """The names (by, goalie_id, name) whose key (name_key, or surname_key) is unique within by:
    by, the key, id."""
    keys = {"name_key": key("name"), "surname_key": surname("name")}
    keyed = names.with_columns(keys[keyed_by].alias(keyed_by)).unique([*by, "goalie_id"])
    unique = keyed.group_by([*by, keyed_by]).agg(goalie_id=pl.col("goalie_id").first(), n=pl.len())
    return unique.filter(pl.col("n") == 1).drop("n")


def starters(
    called: pl.DataFrame,
    candidates: pl.DataFrame,
    goalies: pl.DataFrame,
    likely_share: float,
) -> pl.DataFrame:
    """One row per team-game of called (calls()) that has a starter: game_id, team, source (NHL,
    CONFIRMED or LIKELY), goalie_id, p (his chance of starting) and known_utc (the call's poll).
    candidates holds the goalie-start rows (game_id, team, goalie_id, p_start) with each goalie's
    name; goalies every goalie with an earlier goalie-effect row (goalie_id, name)."""
    if not 0 < likely_share <= 1:
        raise ValueError(f"likely_share must be in (0, 1], not {likely_share}")
    status = pl.col("dfo_status")
    sourced = called.with_columns(
        source=pl.when(pl.col("nhl_id").is_not_null())
        .then(pl.lit(NHL))
        .when(status == DFO_CONFIRMED)
        .then(pl.lit(CONFIRMED))
        .when(status == DFO_LIKELY)
        .then(pl.lit(LIKELY)),
        name_key=key("dfo_name"),
        surname_key=surname("dfo_name"),
    ).filter(pl.col("source").is_not_null())
    on_team = _ids(candidates, ["game_id", "team"]).rename({"goalie_id": "team_id"})
    by_surname = _ids(candidates, ["game_id", "team"], "surname_key").rename(
        {"goalie_id": "surname_id"}
    )
    anyone = _ids(goalies, []).rename({"goalie_id": "league_id"})
    named = (
        sourced.join(on_team, on=["game_id", "team", "name_key"], how="left")
        .join(by_surname, on=["game_id", "team", "surname_key"], how="left")
        .join(anyone, on="name_key", how="left")
        .with_columns(
            goalie_id=pl.when(pl.col("source") == NHL)
            .then(pl.col("nhl_id"))
            .otherwise(pl.coalesce("team_id", "surname_id", "league_id", pl.lit(UNKNOWN_GOALIE)))
            .cast(pl.Int64),
            known_utc=pl.when(pl.col("source") == NHL)
            .then(pl.col("nhl_utc"))
            .otherwise(pl.col("dfo_utc")),
        )
    )
    model = candidates.select("game_id", "team", "goalie_id", model_p="p_start")
    return (
        named.join(model, on=["game_id", "team", "goalie_id"], how="left")
        .with_columns(
            p=pl.when(pl.col("source") == LIKELY)
            .then(pl.max_horizontal(pl.lit(likely_share), pl.col("model_p").fill_null(0.0)))
            .otherwise(1.0)
        )
        .select("game_id", "team", "source", "goalie_id", "p", "known_utc")
        .sort("game_id", "team")
    )


def latest_effects(goalie_effects: pl.DataFrame, cutoff: datetime) -> pl.DataFrame:
    """Each goalie's latest goalie-effect row known before cutoff, from any team: goalie_id, his
    effect and goalie_games then."""
    return (
        goalie_effects.filter(pl.col("observed_utc") < cutoff)
        .sort("as_of_utc", "observed_utc")
        .group_by("goalie_id")
        .agg(pl.col("effect").last(), pl.col("goalie_games").last())
    )


def adjusted(
    goalie_starts: pl.DataFrame,
    goalie_effects: pl.DataFrame,
    goal_multipliers: pl.DataFrame,
    named: pl.DataFrame,
    cutoff: datetime,
) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    """goalie_starts, goalie_effects and goal_multipliers with each named starter (starters()) in
    place: his chance, the rest shared in proportion, and rows for a starter outside the
    candidates. A team-game without goalie-start rows is left as it is."""
    if named.is_empty():
        return goalie_starts, goalie_effects, goal_multipliers
    keys = ["game_id", "team"]
    rows = goalie_starts.join(named.select(keys), on=keys, how="semi")
    rest = goalie_starts.join(named.select(keys), on=keys, how="anti")
    picked = named.join(rows.select(keys).unique(), on=keys, how="semi").select(
        *keys, starter="goalie_id", p="p", known_utc="known_utc"
    )
    others = rows.join(picked.select(*keys, "starter"), on=keys).filter(
        pl.col("goalie_id") != pl.col("starter")
    )
    totals = others.group_by(keys).agg(total=pl.col("p_start").sum())
    # With no other candidate, the starter takes the whole chance.
    picked = picked.join(totals, on=keys, how="left").with_columns(
        p=pl.when(pl.col("total").fill_null(0.0) > 0).then(pl.col("p")).otherwise(1.0)
    )
    template = rows.sort("goalie_id").group_by(keys).first()
    starter_rows = (
        picked.join(template.drop("goalie_id", "p_start"), on=keys)
        .with_columns(goalie_id=pl.col("starter"), p_start=pl.col("p"))
        .select(rows.columns)
    )
    shared = (
        others.join(picked.select(*keys, "total", p_starter="p"), on=keys)
        .with_columns(p_start=pl.col("p_start") / pl.col("total") * (1 - pl.col("p_starter")))
        .filter(pl.col("p_start") > 0)
        .select(rows.columns)
    )
    new_rows = (
        pl.concat([starter_rows, shared])
        .join(picked.select(*keys, "known_utc"), on=keys)
        .with_columns(observed_utc=pl.max_horizontal("observed_utc", "known_utc"))
        .select(rows.columns)
    )
    starts = pl.concat([rest, new_rows], how="vertical_relaxed").sort([*keys, "goalie_id"])
    effects, multipliers = _outside(picked, goalie_effects, goal_multipliers, cutoff)
    return starts, effects, multipliers


def _outside(
    picked: pl.DataFrame,
    goalie_effects: pl.DataFrame,
    goal_multipliers: pl.DataFrame,
    cutoff: datetime,
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """goalie_effects and goal_multipliers with rows for each starter (picked: game_id, team,
    starter, known_utc) the game's rows lack, from his latest goalie-effect row before cutoff.
    They keep the game's as-of times, as the morning build's rows do: his latest row is from an
    earlier or the same morning, and the call's time is on his goalie_starts row, which B2, B3
    and u read him through."""
    keys = ["game_id", "team"]
    present = goalie_effects.select(*keys, starter="goalie_id")
    missing = picked.join(present, on=[*keys, "starter"], how="anti")
    if missing.is_empty():
        return goalie_effects, goal_multipliers
    latest = latest_effects(goalie_effects, cutoff).rename({"goalie_id": "starter"})
    rated = (
        missing.rename({"known_utc": "call_utc"})
        .join(latest, on="starter", how="left")
        .with_columns(pl.col("effect").fill_null(0.0), pl.col("goalie_games").fill_null(0))
    )
    # His new team's row for the game: its expected shots, as-of time and fit.
    template = goalie_effects.sort("goalie_id").group_by(keys).first()
    effect_rows = (
        rated.join(template.drop("goalie_id", "effect", "goalie_games", "goals_saved"), on=keys)
        .with_columns(
            goalie_id="starter",
            goals_saved=pl.col("effect") * pl.col("expected_shots"),
        )
        .select(goalie_effects.columns)
    )
    # The opponent's attack against him: its row for the game, with his gamma.
    attack = (
        goal_multipliers.sort("goalie_id", nulls_last=True)
        .group_by("game_id", "team")
        .first()
        .rename({"team": "attacker", "opponent": "team"})
    )
    multiplier_rows = (
        rated.join(attack.drop("goalie_id", "gamma", "multiplier"), on=keys)
        .with_columns(
            gamma=pl.when(pl.col("xg_per_shot").is_not_null())
            .then(1 - pl.col("effect") / pl.col("xg_per_shot"))
            .otherwise(1.0),
        )
        .with_columns(
            multiplier=pl.col("league_finishing").fill_null(1.0) * pl.col("phi") * pl.col("gamma"),
            goalie_id="starter",
            opponent="team",
            team="attacker",
        )
        .select(goal_multipliers.columns)
    )
    return (
        pl.concat([goalie_effects, effect_rows], how="vertical_relaxed"),
        pl.concat([goal_multipliers, multiplier_rows], how="vertical_relaxed"),
    )
