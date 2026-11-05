"use client";

import { useEffect, useState } from "react";

import { helsinkiTime } from "@/lib/format";
import {
  isCurrent,
  LIVE_POLL_MS,
  isFinal,
  type LiveGame,
  nextPoll,
  type Scores,
  statusLine,
} from "@/lib/live";
import { money, type OurBet, ourBets, pays, rightNow, totals } from "@/lib/live-bets";
import type { RealState } from "@/lib/real";
import type { Bet, Prediction } from "@/lib/types";

import { Explain } from "./sections";
import { Logo } from "./ui";

/** The slate's games on the NHL's scoreboard (#211), each with the bet we hold on it (#398 step 4):
 * the recorded one in euros, or else the paper bet in units, with what it pays and what it costs,
 * and where it stands right now; a finished game's result is provisional until the nightly
 * settlement. The day's totals add the open and finished bets. Refreshed every 30 seconds while a
 * game is on, and display only. */
export function LiveScores({
  day,
  rows,
  bets,
  real = null,
}: {
  day: string | null;
  rows: Prediction[];
  bets: Bet[];
  /** The real bets' day as the form read it, for the bets recorded on it. */
  real?: RealState | null;
}) {
  const [scores, setScores] = useState<Scores | null>(null);
  const [error, setError] = useState<string | null>(null);
  const live = day !== null && isCurrent(day, new Date());

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
    return <p className="muted">Live games show for today&apos;s and last night&apos;s games.</p>;
  }
  const byId = new Map<number, LiveGame>((scores?.games ?? []).map((g) => [g.game_id, g]));
  const ours = ourBets(day, bets, real);
  const sum = scores !== null ? totals([...ours.values()], byId) : null;
  return (
    <>
      {error ? <p className="muted">Live scores unavailable: {error}</p> : null}
      {scores === null && !error ? <p className="muted">Loading the scoreboard…</p> : null}
      {sum ? <Totals sum={sum} /> : null}
      {scores !== null ? (
        <div className="live-games">
          {rows.map((r) => (
            <GameCard
              key={r.game_id}
              row={r}
              game={byId.get(r.game_id)}
              bet={ours.get(r.game_id)}
              day={day}
            />
          ))}
        </div>
      ) : null}
      {scores !== null ? (
        <p className="muted">
          Scoreboard of {day}, the NHL&apos;s game day, read at {helsinkiTime(scores.fetched_utc)}{" "}
          Helsinki time. Times are Helsinki time. It refreshes every 30 seconds while a game is on.
        </p>
      ) : null}
      <Explain terms={["live", "provisional"]} />
    </>
  );
}

const KIND_NAMES: Record<OurBet["kind"], string> = {
  real: "Our bet",
  practice: "Practice bet",
  paper: "Paper bet",
};

function Totals({ sum }: { sum: NonNullable<ReturnType<typeof totals>> }) {
  const of =
    sum.kind === "paper"
      ? "the paper bets, in units"
      : sum.kind === "practice"
        ? "the practice bets recorded"
        : "the bets recorded";
  return (
    <div className="tiles live-totals" aria-label={`Today's totals: ${of}`}>
      <div className="tile">
        <div className="label">Staked in play</div>
        <div className="value">{money(sum.inPlay, sum.unit, false)}</div>
        <div className="sub">
          {sum.bets - sum.over} of {sum.bets} open; {of}
        </div>
      </div>
      <div className="tile">
        <div className="label">If all open bets win</div>
        <div className="value">{money(sum.best, sum.unit)}</div>
        <div className="sub">the best case from here</div>
      </div>
      <div className="tile">
        <div className="label">If all open bets lose</div>
        <div className="value">{money(sum.worst, sum.unit)}</div>
        <div className="sub">the worst case from here</div>
      </div>
      <div className="tile">
        <div className="label">Finished today</div>
        <div className="value">{money(sum.finished, sum.unit)}</div>
        <div className="sub">{sum.over} over; provisional until the nightly settlement</div>
      </div>
    </div>
  );
}

function GameCard({
  row,
  game,
  bet,
  day,
}: {
  row: Prediction;
  game: LiveGame | undefined;
  bet: OurBet | undefined;
  day: string;
}) {
  const scored = game && game.away_score !== null && game.home_score !== null;
  const team = (code: string, side: "away" | "home") =>
    bet?.side === side ? <strong className="bet-side">{code}</strong> : <span>{code}</span>;
  const status = game ? statusLine(game, helsinkiTime(game.start_utc, day)) : "Not on the scoreboard";
  const on = game && (game.state === "LIVE" || game.state === "CRIT");
  const now = bet ? rightNow(bet, game) : null;
  return (
    <article className={bet ? "live-game has-bet" : "live-game"} aria-label={`${row.away} at ${row.home}`}>
      <div className="score-row">
        <span className="team">
          <Logo team={row.away} size={32} />
          {team(row.away, "away")}
        </span>
        <span className={scored ? "goals" : "goals none"}>
          {scored ? `${game.away_score}–${game.home_score}` : "at"}
        </span>
        <span className="team home">
          {team(row.home, "home")}
          <Logo team={row.home} size={32} />
        </span>
      </div>
      <p className={on ? "clock on" : game && isFinal(game) ? "clock over" : "clock"}>{status}</p>
      {bet && now ? (
        <div className="our-bet">
          <p>
            <Logo team={bet.team} />
            {KIND_NAMES[bet.kind]}: <strong>{bet.team}</strong> at {bet.price}, stake{" "}
            {money(bet.stake, bet.unit, false)}
          </p>
          <dl className="pays">
            <div>
              <dt>If it wins</dt>
              <dd>{money(pays(bet), bet.unit)}</dd>
            </div>
            <div>
              <dt>If it loses</dt>
              <dd>{money(-bet.stake, bet.unit)}</dd>
            </div>
          </dl>
          {/* Won and lost carry the board's own ✓ and ✗; the others a dot in their colour. */}
          <p className={`now ${now.tone}`}>
            {now.tone === "won" || now.tone === "lost" ? null : (
              <span className="dot" aria-hidden="true" />
            )}
            {now.text}
          </p>
        </div>
      ) : (
        <p className="muted">No bet on this game.</p>
      )}
    </article>
  );
}
