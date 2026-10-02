"""RAPM's report (#101, ADR 0019): what each season's ratings rest on, with no score. Scoring waits
for the tuning (#103) and B3 (#106).

Per season, the game dates, fits and candidate ratings. For a shown season also:
- the share of candidates with 5v5 data and their median decayed hours;
- the terms of the season's last fit: each model's rate for the season (its intercept plus its
  season term, the rate of skaters rated 0), and the terms that only remove bias;
- the five highest and lowest 5v5 net ratings (offense plus defense), each player's at his last
  game of the season, among those with at least LEADER_HOURS hours, a face-validity check.
The development and held-out seasons show only their counts until gate 2.
"""

from collections.abc import Collection

import polars as pl

from nhl_edge.ratings.rapm import DEFENSEMEN, EV, PP, SCORES, SIGMA, ZONES, Settings

LEADER_HOURS = 10.0
LEADERS = 5
# The terms shown per model, besides its rate and, at 5v5, the arenas' spread.
SHOWN_TERMS = {
    EV: ("home", *(f"score:{s:+d}" for s in SCORES), *(f"zone:{z}" for z in ZONES), SIGMA),
    PP: ("home", "situation:5v3", "situation:4v3", DEFENSEMEN, SIGMA),
}


def season_counts(ratings: pl.DataFrame) -> pl.DataFrame:
    """Per season: game dates, fits, candidates rated, and the share and median hours of those
    with 5v5 data."""
    ev = ratings.filter(pl.col("component") == "ev_off")
    return (
        ev.group_by("season")
        .agg(
            dates=pl.col("game_date").n_unique(),
            fits=pl.col("known_utc").n_unique(),
            candidates=pl.len(),
            with_data=(pl.col("hours") > 0).mean(),
            median_hours=pl.col("hours").filter(pl.col("hours") > 0).median(),
        )
        .sort("season")
    )


def last_terms(terms: pl.DataFrame, season: int) -> dict[tuple[str, str], float]:
    """The terms of the season's last fit, with each model's rate for the season as "rate" and
    the arenas' spread as "arena sd"."""
    season_terms = terms.filter(pl.col("season") == season)
    if season_terms.is_empty():
        return {}
    last = season_terms.filter(pl.col("known_utc") == season_terms["known_utc"].max())
    last = last.unique(["model", "term"])
    values = {(m, t): float(v) for m, t, v in last.select("model", "term", "value").iter_rows()}
    for model in (EV, PP):
        if (model, "intercept") in values:
            values[(model, "rate")] = values[(model, "intercept")] + values.get(
                (model, f"season:{season}"), 0.0
            )
    arenas = last.filter(pl.col("model") == EV, pl.col("term").str.starts_with("arena:"))
    if arenas.height > 1:
        values[(EV, "arena sd")] = float(arenas["value"].std())  # type: ignore[arg-type]
    return values


def leaders(ratings: pl.DataFrame, players: pl.DataFrame, season: int) -> pl.DataFrame:
    """Each player's 5v5 ratings at his last game of the season, with LEADER_HOURS hours of 5v5
    data or more, by net 5v5 rating, best first."""
    season_rows = ratings.filter(pl.col("season") == season)
    if season_rows.is_empty():
        return pl.DataFrame()
    latest = pl.col("as_of_utc") == pl.col("as_of_utc").max().over("player_id")
    last = season_rows.filter(latest)
    wide = (
        last.filter(pl.col("component").is_in(["ev_off", "ev_def"]))
        .pivot(on="component", index=["player_id", "team", "role"], values=["mean", "hours"])
        .filter(pl.col("hours_ev_off") >= LEADER_HOURS)
    )
    return (
        wide.join(players.select("player_id", "name"), on="player_id", how="left")
        .with_columns(net=pl.col("mean_ev_off") + pl.col("mean_ev_def"))
        .sort("net", "player_id", descending=[True, False])
    )


def markdown_report(
    ratings: pl.DataFrame,
    terms: pl.DataFrame,
    players: pl.DataFrame,
    shown: Collection[int],
    version: str,
    settings: Settings,
) -> str:
    lines = [
        f"# RAPM: {version}",
        "",
        f"Provisional settings until #103 (ADR 0019): {settings.label}.",
        "Ratings are xG per hour of ice time, refit every game day from the stints public before",
        "each game. Nothing is scored here. Held-out seasons, the development seasons among them",
        "until gate 2, show only their counts.",
        "",
        "## Per season",
        "",
        "| Season | Dates | Fits | Candidate ratings | With 5v5 data | Median 5v5 hours |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    counts = season_counts(ratings)
    for row in counts.iter_rows(named=True):
        if row["season"] in shown:
            hours = row["median_hours"]
            cells = f"{row['with_data']:.1%} | {'' if hours is None else f'{hours:.1f}'}"
        else:
            cells = "held out | "
        counted = f"{row['dates']} | {row['fits']} | {row['candidates']:,}"
        lines.append(f"| {row['season']} | {counted} | {cells} |")
    seasons = [s for s in counts["season"].to_list() if s in shown]
    lines += [
        "",
        "## Terms of each season's last fit",
        "",
        "The rate is xG per hour of skaters rated 0: the intercept plus the season's term. On the",
        "power play that is five forwards, each defenseman adding the defensemen term. The other",
        "terms only remove bias. Score is the attacking team's lead and zone the faceoff that",
        "opens the stint, from its side; sigma is the residual sd per square-root hour.",
        "",
    ]
    for model, title in ((EV, "5v5"), (PP, "Power play")):
        names = ("rate", *SHOWN_TERMS[model], *(("arena sd",) if model == EV else ()))
        lines += [
            f"### {title}",
            "",
            "| Season | " + " | ".join(names) + " |",
            "| --- |" + " ---: |" * len(names),
        ]
        for season in seasons:
            values = last_terms(terms, season)
            cells = [f"{values[(model, n)]:+.3f}" if (model, n) in values else "" for n in names]
            lines.append(f"| {season} | " + " | ".join(cells) + " |")
        lines.append("")
    lines += [
        "## 5v5 leaders at each player's last game of the season",
        "",
        f"Players with at least {LEADER_HOURS:g} decayed hours of 5v5 data, by offense plus",
        "defense, with the team of that game. A face-validity check, not a score.",
        "",
        "| Season | Rank | Player | Team | Role | Offense | Defense | Net | Hours |",
        "| --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for season in seasons:
        table = leaders(ratings, players, season)
        if table.is_empty():
            continue
        picks = [("top", i, r) for i, r in enumerate(table.head(LEADERS).iter_rows(named=True))]
        bottom = table.tail(LEADERS).reverse().iter_rows(named=True)
        picks += [("bottom", i, r) for i, r in enumerate(bottom)]
        for end, i, row in picks:
            lines.append(
                f"| {season} | {end} {i + 1} | {row['name'] or row['player_id']} | {row['team']} "
                f"| {row['role']} | {row['mean_ev_off']:+.3f} | {row['mean_ev_def']:+.3f} "
                f"| {row['net']:+.3f} | {row['hours_ev_off']:.1f} |"
            )
    return "\n".join(lines) + "\n"
