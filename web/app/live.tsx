"use client";

import { useEffect, useState } from "react";

import { easternDate, easternTime } from "@/lib/format";
import { LIVE_POLL_MS, type LiveGame, nextPoll, provisional, type Scores, statusLine } from "@/lib/live";
import { result } from "@/lib/results";
import type { Bet, Prediction } from "@/lib/types";

import { Explain } from "./sections";

/** Whether the slate's day is today's or yesterday's US Eastern date: a late game of yesterday's
 * slate is still on after midnight ET, and older days have nothing live to show. */
function current(day: string, now: Date): boolean {
  const yesterday = new Date(now.getTime() - 86_400_000);
  return day === easternDate(now) || day === easternDate(yesterday);
}

/** The slate's games on the NHL's scoreboard (#211): score, period and clock, the paper bet's side,
 * and its result: provisional from the final score, official once the nightly run settles it.
 * Refreshed every 30 seconds while a game is on, and display only. */
export function LiveScores({
  day,
  rows,
  bets,
}: {
  day: string | null;
  rows: Prediction[];
  bets: Bet[];
}) {
  const [scores, setScores] = useState<Scores | null>(null);
  const [error, setError] = useState<string | null>(null);
  const live = day !== null && current(day, new Date());

  useEffect(() => {
    if (!live) return;
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function read() {
      let wait: number | null = LIVE_POLL_MS;
      try {
        const answer = await fetch(`/api/scores?date=${day}`);
        const body = await answer.json();
        if (!answer.ok) throw new Error(body.error ?? `HTTP ${answer.status}`);
        if (!alive) return;
        setScores(body as Scores);
        setError(null);
        wait = nextPoll((body as Scores).games, new Date());
      } catch (e) {
        if (alive) setError((e as Error).message);
      }
      if (alive && wait !== null) timer = setTimeout(read, wait);
    }
    read();
    return () => {
      alive = false;
      clearTimeout(timer);
    };
  }, [day, live]);

  if (!day) return null;
  if (!live) {
    return <p className="muted">Live scores show for today&apos;s and last night&apos;s games.</p>;
  }
  const byId = new Map<number, LiveGame>((scores?.games ?? []).map((g) => [g.game_id, g]));
  const placed = new Map(bets.filter((b) => b.game_date === day).map((b) => [b.game_id, b]));
  return (
    <div className="scroll">
      {error ? <p className="muted">Live scores unavailable: {error}</p> : null}
      {scores === null && !error ? <p className="muted">Loading the scoreboard…</p> : null}
      {scores !== null ? (
        <table>
          <thead>
            <tr>
              <th>Score</th>
              <th>Status</th>
              <th>Bet</th>
              <th>Result</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const g = byId.get(r.game_id);
              const bet = placed.get(r.game_id);
              const team = (abbrev: string, side: "away" | "home") =>
                bet?.side === side ? <strong className="bet-side">{abbrev}</strong> : abbrev;
              const scored = g && g.away_score !== null && g.home_score !== null;
              return (
                <tr key={r.game_id}>
                  <td>
                    {scored ? (
                      <>
                        {team(r.away, "away")} {g.away_score}–{g.home_score} {team(r.home, "home")}
                      </>
                    ) : (
                      <>
                        {team(r.away, "away")} at {team(r.home, "home")}
                      </>
                    )}
                  </td>
                  <td>{g ? statusLine(g, easternTime(g.start_utc)) : "Not on the scoreboard"}</td>
                  <td>{bet ? (bet.side === "home" ? bet.home : bet.away) : ""}</td>
                  <td>{outcome(bet, g)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      ) : null}
      {scores !== null ? (
        <p className="muted">
          Scoreboard of {day}, read at {easternTime(scores.fetched_utc)} ET. It refreshes every 30
          seconds while a game is on.
        </p>
      ) : null}
      <Explain terms={["live", "provisional"]} />
    </div>
  );
}

/** The bet's result: official once settled, otherwise provisional from a final score. */
function outcome(bet: Bet | undefined, g: LiveGame | undefined) {
  if (!bet) return "";
  if (bet.settlement !== null) {
    const official = result(bet);
    const mark = bet.settlement === "settled" ? (bet.won ? "won" : "lost") : undefined;
    return <span className={mark}>{official}</span>;
  }
  const p = provisional(bet, g);
  if (p === null) return "";
  return <span className={p}>{p === "won" ? "Provisionally won" : "Provisionally lost"}</span>;
}
