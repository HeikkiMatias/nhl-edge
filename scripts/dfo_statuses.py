"""#121 step 3: are Daily Faceoff's injury statuses reliable enough for the lineup model? Measured
on live games by the rule fixed on the issue before it was read (the comment of 2026-11-04).

    uv run python scripts/dfo_statuses.py

**The games:** regular-season games from FIRST_DAY through the last date with final boxscores,
wherever the paper ledger has a decision. **The snapshot:** each team-game's last Daily Faceoff
page fetched before the decision's prediction_utc, what the 12:45 ET decision could have read.

**The players:** each team-game's candidate skaters (lineup.projection.candidates: early_exit and
whether he dressed) with his p_available from the lake's lineups, matched to the page by team and
his last boxscore's jersey number with his normalized last name agreeing, or, without a jersey on
either side, by full normalized name within the team.

**Daily Faceoff's group** (GROUPS, the first that applies): out (status out or ir, or the ir group),
dtd, lines (F1-F4 or D1-D4 without a status), other (only power-play, penalty-kill or goalie
groups), absent (not on the page).

**The measures,** each with a weekly block bootstrap interval, counts only for a group under
MIN_WEEKS weeks: by group, the share who dressed, our mean p_available and its gap; the value
(primary), the paired log loss of whether each matched candidate dressed under a Daily
Faceoff-informed probability less our p_available, the informed one being p_available but for the
out and dtd groups' dressed share in the earlier weeks (Codex on #394: the rule's leave one week
out read later weeks), both clipped to [CLIP, 1 - CLIP]; and the same on #120's early exits.

**The rule:** reliable for a lineup ADR when the out group's dressed share has its interval's upper
end below OUT_CEILING and the value's interval is below 0.

Live-only and read-only: no source dates past statuses, so nothing here enters a backtest, and no
model or table changes. It writes reports/lineups/dfo-statuses-<yyyymmdd>-<shortsha>.md.
"""

import argparse
import json
import re
import unicodedata
from collections.abc import Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import polars as pl

from nhl_edge.backtest import metrics, reports
from nhl_edge.ingest.games import result_public_utc
from nhl_edge.lake.tables import Lake
from nhl_edge.lineup import projection

FIRST_DAY = date(2026, 10, 8)
# The second measurement (#121, the owner's go-ahead of 2026-11-05, its rule fixed before its
# first game): groups by status wherever a player is listed, scored on WINDOW_2's games only, with
# the value's shares from every earlier week since FIRST_DAY.
RULES = (1, 2)
WINDOW_2 = (date(2026, 11, 5), date(2026, 12, 6))
SEASON = 20262027
REGULAR_SEASON = 2
OUT, DTD, LINES, OTHER, ABSENT = "out", "dtd", "lines", "other", "absent"
GROUPS = (OUT, DTD, LINES, OTHER, ABSENT)
INFORMED = (OUT, DTD)
LINE_GROUPS = ("f1", "f2", "f3", "f4", "d1", "d2", "d3", "d4")
OUT_STATUSES = ("out", "ir")
CLIP = 0.01
OUT_CEILING = 0.10
MIN_WEEKS = 4


def normalized(name: pl.Expr) -> pl.Expr:
    """A name in lower-case letters only, accents and punctuation dropped."""
    return name.map_elements(_ascii, return_dtype=pl.String)


def _ascii(name: str | None) -> str | None:
    if name is None:
        return None
    plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z ]", "", plain.lower()).strip()


def last_name(full: pl.Expr) -> pl.Expr:
    return full.str.split(" ").list.last()


def snapshots(lines: pl.DataFrame, decisions: pl.DataFrame) -> pl.DataFrame:
    """Each team-game date's last Daily Faceoff page fetched before the decision's prediction time
    (decisions: game_date, prediction_utc), its rows only."""
    fetched = lines.join(decisions, on="game_date").filter(
        pl.col("observed_utc") < pl.col("prediction_utc")
    )
    last = fetched.group_by("game_date", "team").agg(page_utc=pl.col("observed_utc").max())
    return fetched.join(last, on=["game_date", "team"]).filter(
        pl.col("observed_utc") == pl.col("page_utc")
    )


def page_groups(page: pl.DataFrame, rule: int = 1) -> pl.DataFrame:
    """Each player on a page with his group (the first of out, dtd, lines, other that applies),
    his jersey and normalized names. Under rule 1 (step 3) the whole "ir" group is out; under
    rule 2 (the second measurement) only status decides out and dtd, wherever he is listed."""
    if rule not in RULES:
        raise ValueError(f"no rule {rule}: {RULES}")
    status = pl.col("injury_status")
    out = status.is_in(OUT_STATUSES)
    if rule == 1:
        out = out | (pl.col("group") == "ir")
    per = page.group_by("game_date", "team", "dfo_player_id").agg(
        player_name=pl.col("player_name").first(),
        jersey=pl.col("jersey").drop_nulls().first(),
        out=out.any(),
        dtd=(status == "dtd").any(),
        lines=pl.col("group").is_in(LINE_GROUPS).any(),
    )
    group = (
        pl.when(pl.col("out"))
        .then(pl.lit(OUT))
        .when(pl.col("dtd"))
        .then(pl.lit(DTD))
        .when(pl.col("lines"))
        .then(pl.lit(LINES))
        .otherwise(pl.lit(OTHER))
    )
    full = normalized(pl.col("player_name"))
    return per.select(
        "game_date",
        "team",
        "dfo_player_id",
        "jersey",
        dfo_group=group,
        dfo_full=full,
        dfo_last=last_name(full),
    )


def jerseys(actual: pl.DataFrame, cands: pl.DataFrame) -> pl.DataFrame:
    """Each candidate's jersey in his latest boxscore public before the candidate's as-of time."""
    worn = (
        actual.filter(pl.col("sweater_number").is_not_null())
        .select("player_id", "observed_utc", jersey=pl.col("sweater_number").cast(pl.Int16))
        .sort("observed_utc")
    )
    return (
        cands.select("game_id", "team", "player_id", "as_of_utc")
        .sort("as_of_utc")
        .join_asof(
            worn,
            left_on="as_of_utc",
            right_on="observed_utc",
            by="player_id",
            strategy="backward",
            allow_exact_matches=False,
            check_sortedness=False,
        )
        .select("game_id", "team", "player_id", "jersey")
    )


def matched(cands: pl.DataFrame, groups: pl.DataFrame) -> pl.DataFrame:
    """Each candidate with his Daily Faceoff group: by team and jersey with the last name
    agreeing, else by full normalized name within the team; absent without either."""
    by_jersey = (
        cands.filter(pl.col("jersey").is_not_null())
        .join(
            groups.filter(pl.col("jersey").is_not_null()).select(
                "game_date", "team", "jersey", "dfo_player_id", "dfo_group", "dfo_last"
            ),
            on=["game_date", "team", "jersey"],
        )
        .filter(pl.col("last") == pl.col("dfo_last"))
    )
    keys = ["game_id", "team", "player_id"]
    rest = cands.join(by_jersey.select(keys), on=keys, how="anti")
    taken = by_jersey.select("game_date", "team", "dfo_player_id")
    by_name = rest.join(
        groups.join(taken, on=["game_date", "team", "dfo_player_id"], how="anti").select(
            "game_date", "team", "dfo_player_id", "dfo_group", "dfo_full"
        ),
        left_on=["game_date", "team", "full"],
        right_on=["game_date", "team", "dfo_full"],
    )
    found = pl.concat(
        [
            by_jersey.select(*keys, "dfo_player_id", "dfo_group", how=pl.lit("jersey")),
            by_name.select(*keys, "dfo_player_id", "dfo_group", how=pl.lit("name")),
        ]
    )
    # A player Daily Faceoff lists under two ids can match twice: the first group that applies
    # wins, then the lower id, so the match never depends on a join's order.
    rank = pl.col("dfo_group").replace_strict(
        {g: i for i, g in enumerate(GROUPS)}, return_dtype=pl.Int8
    )
    found = found.sort([*keys, rank, "dfo_player_id"]).unique(  # type: ignore[list-item]
        subset=keys, keep="first", maintain_order=True
    )
    return cands.join(found, on=keys, how="left").with_columns(
        dfo_group=pl.col("dfo_group").fill_null(ABSENT)
    )


def window(rule: int) -> tuple[date, date | None]:
    """The games a rule scores: step 3's from FIRST_DAY on, the second measurement's WINDOW_2."""
    return (FIRST_DAY, None) if rule == 1 else WINDOW_2


def frame(lake: Any, rule: int = 1) -> tuple[pl.DataFrame, dict[str, Any]]:
    """The measured candidates (game_id, season, game_date, team, player_id, early_exit,
    dressed, p_available, dfo_group, week) from FIRST_DAY to the rule's last day, grouped by the
    rule, and the counts behind the rule's scored games."""
    start, last = window(rule)
    games = lake.read("games").filter(
        pl.col("season") == SEASON,
        (pl.col("game_id") // 10_000 % 100) == REGULAR_SEASON,
        pl.col("game_date") >= FIRST_DAY,
        pl.col("game_date") <= (last or date.max),
    )
    actual = lake.read("actual_lineups").filter(pl.col("season") >= SEASON - 10_001)
    cands = projection.candidates(games, actual).filter(pl.col("dressed").is_not_null())
    lineups = lake.read("lineups", [SEASON]).select("game_id", "team", "player_id", "p_available")
    players = lake.read("players").select("player_id", full=normalized(pl.col("name")))
    decisions = (
        lake.read("paper_ledger", [SEASON])
        .group_by("game_date")
        .agg(prediction_utc=pl.col("prediction_utc").min())
    )
    pages = snapshots(lake.read("dailyfaceoff_lines"), decisions)
    with_page = pages.select("game_date", "team").unique()
    base = (
        cands.join(decisions.select("game_date"), on="game_date")
        .join(lineups, on=["game_id", "team", "player_id"], how="left")
        .join(players, on="player_id", how="left")
        .with_columns(last=last_name(pl.col("full")))
    )
    team_games = base.select("game_id", "team", "game_date").unique()
    paged = base.join(with_page, on=["game_date", "team"])
    paged = paged.join(jerseys(actual, paged), on=["game_id", "team", "player_id"], how="left")
    out = matched(paged, page_groups(pages, rule)).with_columns(
        week=metrics.week_of(pl.col("game_date")), dressed=pl.col("dressed").cast(pl.Float64)
    )
    scored = pl.col("game_date") >= start
    on_pages = page_groups(pages, rule).filter(pl.col("dfo_group").is_in([LINES, DTD, OUT]), scored)
    shown = out.filter(scored)
    unmatched_page = on_pages.join(
        shown.filter(pl.col("dfo_player_id").is_not_null()).select(
            "game_date", "team", "dfo_player_id"
        ),
        on=["game_date", "team", "dfo_player_id"],
        how="anti",
    )
    games_scored = team_games.filter(scored)
    counts = {
        "rule": rule,
        "team_games": games_scored.height,
        "team_games_with_a_page": games_scored.join(with_page, on=["game_date", "team"]).height,
        "candidates": shown.height,
        "matched": int(shown["dfo_player_id"].is_not_null().sum()),
        "by_jersey": int((shown["how"] == "jersey").sum()),
        "by_name": int((shown["how"] == "name").sum()),
        "page_players_unmatched": unmatched_page.height,
        "without_p_available": int(shown["p_available"].is_null().sum()),
        "first_day": str(shown["game_date"].min()),
        "last_day": str(shown["game_date"].max()),
    }
    return out.filter(pl.col("p_available").is_not_null()), counts


def log_loss(p: pl.Expr, y: pl.Expr) -> pl.Expr:
    clipped = p.clip(CLIP, 1 - CLIP)
    return -(y * clipped.log() + (1 - y) * (1 - clipped).log())


def informed(rows: pl.DataFrame) -> pl.DataFrame:
    """rows with p_informed: p_available, but for the out and dtd groups their dressed share in
    the earlier weeks, from the candidates whose boxscore (label_utc) was public before the week's
    first as-of time (hard rule 1, Codex on #394); a group with no such history keeps
    p_available, as every group does in the first week."""
    out = []
    for (week,), part in rows.sort("week").group_by("week", maintain_order=True):
        start = part["as_of_utc"].min()
        known = rows.filter(pl.col("week") < week, pl.col("label_utc") < start)
        shares = dict(
            known.filter(pl.col("dfo_group").is_in(INFORMED))
            .group_by("dfo_group")
            .agg(share=pl.col("dressed").mean())
            .iter_rows()
        )
        share = pl.col("dfo_group").replace_strict(shares, default=None, return_dtype=pl.Float64)
        out.append(
            part.with_columns(
                p_informed=pl.when(pl.col("dfo_group").is_in(list(shares)))
                .then(share)
                .otherwise(pl.col("p_available"))
            )
        )
    return pl.concat(out).with_columns(
        value=log_loss(pl.col("p_informed"), pl.col("dressed"))
        - log_loss(pl.col("p_available"), pl.col("dressed"))
    )


def estimate(rows: pl.DataFrame, column: str) -> dict[str, Any]:
    """column's mean with its weekly block bootstrap interval, or counts only under MIN_WEEKS."""
    weeks = rows["week"].n_unique() if rows.height else 0
    if weeks < MIN_WEEKS:
        return {"games": rows.height, "weeks": weeks}
    return metrics.bootstrap(rows, column).to_dict()


def by_group(rows: pl.DataFrame) -> dict[str, Any]:
    gapped = rows.with_columns(gap=pl.col("p_available") - pl.col("dressed"))
    return {
        group: {
            "candidates": part.height,
            "dressed": estimate(part, "dressed"),
            "p_available": estimate(part, "p_available"),
            "gap": estimate(part, "gap"),
        }
        for group in GROUPS
        for part in [gapped.filter(pl.col("dfo_group") == group)]
    }


def run(rows: pl.DataFrame, counts: Mapping[str, Any], start: date = FIRST_DAY) -> dict[str, Any]:
    """Every figure on the games from start: the informed shares read every earlier week, the
    measures only the scored games."""
    scored = pl.col("game_date") >= start
    matched_rows = informed(rows.filter(pl.col("dfo_player_id").is_not_null())).filter(scored)
    rows = rows.filter(scored)
    early = rows.filter(pl.col("early_exit") > 0)
    early_matched = matched_rows.filter(pl.col("early_exit") > 0)
    found: dict[str, Any] = {
        "counts": dict(counts),
        "groups": by_group(rows),
        "value": estimate(matched_rows, "value"),
        "early_exit": {
            "groups": by_group(early),
            "value": estimate(early_matched, "value"),
        },
    }
    out_share = found["groups"][OUT]["dressed"]
    value = found["value"]
    found["reliable"] = bool(
        "high" in out_share
        and out_share["high"] < OUT_CEILING
        and "high" in value
        and value["high"] < 0
    )
    return found


def _cell(figure: Mapping[str, Any], scale: float = 1.0, digits: int = 3) -> str:
    if "mean" not in figure:
        return f"{figure.get('games', 0)} rows over {figure.get('weeks', 0)} weeks: counts only"
    m, lo, hi = (figure[k] * scale for k in ("mean", "low", "high"))
    return f"{m:+.{digits}f} [{lo:+.{digits}f}, {hi:+.{digits}f}]"


def _table(groups: Mapping[str, Any]) -> list[str]:
    lines = [
        "| Daily Faceoff group | Candidates | Dressed | Our p_available "
        "| Gap (ours less dressed) |",
        "| --- | --- | --- | --- | --- |",
    ]
    for group in GROUPS:
        g = groups[group]
        lines.append(
            f"| {group} | {g['candidates']:,} | {_cell(g['dressed'])} | "
            f"{_cell(g['p_available'])} | {_cell(g['gap'])} |"
        )
    return lines


def markdown(found: Mapping[str, Any], version: str) -> str:
    c = found["counts"]
    lines = [
        f"# Daily Faceoff's injury statuses against who dressed: {version}",
        "",
        (
            "#121 step 3, by the rule fixed on the issue before it was read"
            if c["rule"] == 1
            else "#121's second measurement, by its rule fixed on the issue before its first game "
            "(groups by status wherever a player is listed; the value's shares from every earlier "
            "week since 2026-10-08)"
        )
        + ". Live games from "
        f"{c['first_day']} to {c['last_day']}: each team-game's last Daily Faceoff page "
        "before the paper decision, against the projection's candidate skaters and whether they "
        "dressed. Intervals are 95% weekly block bootstrap; a group under "
        f"{MIN_WEEKS} weeks has counts only.",
        "",
        f"- Team-games: {c['team_games']:,}, {c['team_games_with_a_page']:,} with a page before "
        "the decision.",
        f"- Candidates on them: {c['candidates']:,}; matched to the page {c['matched']:,} "
        f"({c['by_jersey']:,} by jersey, {c['by_name']:,} by name). Page players in the lines "
        f"or with a status that matched no candidate: {c['page_players_unmatched']:,}. "
        f"Without a p_available: {c['without_p_available']:,}.",
        "",
        "## Every candidate",
        "",
        *_table(found["groups"]),
        "",
        "**The value (primary):** paired log loss of whether he dressed, Daily Faceoff-informed "
        "(each week's out and dtd shares from the earlier weeks only) less our p_available, on "
        "the matched candidates (negative favours Daily Faceoff): "
        f"{_cell(found['value'], 1000)} thousandths.",
        "",
        "## #120's early exits",
        "",
        *_table(found["early_exit"]["groups"]),
        "",
        f"The value on them: {_cell(found['early_exit']['value'], 1000)} thousandths.",
        "",
        (
            "**Reliable by the rule:** the out group's dressed share is below "
            f"{OUT_CEILING:.0%} and the value's interval is below 0. Next: a Proposed lineup ADR "
            "for a later policy version (#222), covering #120's early exits; the owner decides."
            if found["reliable"]
            else "**Not reliable by the rule:** either the out group's dressed share isn't below "
            f"{OUT_CEILING:.0%}, or the value's interval isn't below 0. The figures stand here, "
            "and the logging continues."
        ),
        "",
    ]
    return "\n".join(lines)


def complete(rule: int, now: datetime) -> None:
    """Refused before every scored game's boxscore is public: a rule's figures are read once,
    on its whole window."""
    _, last = window(rule)
    if last is not None and now < result_public_utc(last):
        raise SystemExit(
            f"rule {rule}'s window ends {last}: its boxscores are public from "
            f"{result_public_utc(last):%Y-%m-%d %H:%M} UTC, so it can't be read yet"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Daily Faceoff's statuses (#121).")
    parser.add_argument("--out", type=Path, default=Path("reports/lineups"))
    parser.add_argument("--rule", type=int, choices=RULES, default=1)
    args = parser.parse_args()
    now = datetime.now(UTC)
    complete(args.rule, now)
    rows, counts = frame(Lake(), args.rule)
    found = run(rows, counts, window(args.rule)[0])
    name = "dfo-statuses" if args.rule == 1 else "dfo-statuses-2"
    version = reports.version(name, now)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / f"{version}.json").write_text(json.dumps(found, indent=2, sort_keys=True))
    path = args.out / f"{version}.md"
    path.write_text(markdown(found, version))
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
