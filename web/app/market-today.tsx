"use client";

// Market making › Today (#398 step 3): the quoting simulator's day as it stands, read again
// every minute from the owner-only quote_live. Paper only: a simulator on Kalshi's real books,
// whose fills are an upper bound. Prices are cents per $1 contract on a team to win, overtime
// and shootout included. Each game carries the NHL's live scoreboard, as Betting › Today's do,
// with where its holding stands (#408): display only, the nightly record stays the result.

import type { SupabaseClient } from "@supabase/supabase-js";
import { useEffect, useState } from "react";

import { helsinkiTime } from "@/lib/format";
import type { LiveGame as ScoreGame } from "@/lib/live";
import {
  dayTotals,
  gamesOf,
  type Live,
  type LiveGame,
  type LiveRow,
  loadLive,
  priceCents,
  REFRESH_MS,
  stale,
  standing,
  STATE_NAMES,
  updatedAt,
} from "@/lib/live-quotes";
import { dollars, HALF_SPREADS } from "@/lib/quotes";

import { ScoreRow, useScores } from "./live";
import { Card, Chip, Logo, Segmented } from "./ui";

export function MarketToday({ client }: { client: SupabaseClient }) {
  const [live, setLive] = useState<Live | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [half, setHalf] = useState<number>(1);
  const { scores, error: scoreError } = useScores(live?.day ?? null);

  useEffect(() => {
    let alive = true;
    const read = () =>
      loadLive(client).then(
        (found) => {
          if (!alive) return;
          setLive(found);
          setError(null);
        },
        (e: Error) => alive && setError(e.message),
      );
    read();
    const timer = setInterval(read, REFRESH_MS);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [client]);

  if (error && !live) return <p className="notice">Could not read the live view: {error}</p>;
  if (!live) return <p className="muted">Loading…</p>;
  if (!live.applied) {
    return (
      <p className="notice">
        The live migration isn&apos;t applied yet. Apply{" "}
        <code>supabase/migrations/20261106130000_quote_live.sql</code> and set the repository
        variable <code>SUPABASE_QUOTES_LIVE=true</code>: the next watch window fills this view
        (web/README.md, owner setup).
      </p>
    );
  }
  if (live.rows.length === 0 || live.day === null) {
    return (
      <p className="muted">
        No live view yet: it fills from the morning view&apos;s publication on a game day, every 5
        minutes while the watch runs.
      </p>
    );
  }
  const games = gamesOf(live.rows, half);
  const byId = new Map<number, ScoreGame>((scores?.games ?? []).map((g) => [g.game_id, g]));
  const t = dayTotals(games, scores ? byId : null);
  const at = updatedAt(live.rows);
  return (
    <>
      <p className="notice">
        <Chip tone="info">Paper only</Chip> Game day {live.day}: the simulator&apos;s quotes on
        Kalshi&apos;s real books as they stand
        {at ? `, updated ${helsinkiTime(at, live.day)} Helsinki time` : ""}. A fill counts only when a
        real trade reached our bid, so fills are an upper bound. No real quote is posted.
        {error ? ` The last read failed (${error}); showing the one before.` : ""}
      </p>
      {stale(live.rows, new Date()) ? (
        <p className="notice">
          <Chip tone="warn">Not updating</Chip> No new cycle for over 15 minutes while games are
          still to start: the watch&apos;s live job may have stopped.
        </p>
      ) : null}
      <div className="pickers">
        <Segmented
          label="Half-spread"
          options={HALF_SPREADS.map((h) => ({ value: h, label: `${h}¢` }))}
          value={half}
          onChange={setHalf}
        />
      </div>
      <Card title={`Our model at ${half}¢ below: today`} topic="mmToday" wide>
        <div className="tiles">
          <Tile label="Games" value={String(t.games)} sub={`${t.quoting} quoting now`} />
          <Tile
            label="Fills"
            value={String(t.fills)}
            sub={`${t.contracts.toFixed(0)} contracts held`}
          />
          <Tile label="Cost" value={dollars(t.cost)} sub="paid, the maker fee in it" />
          <Tile
            label="If every result goes our way"
            value={dollars(t.best, true)}
            sub={t.over ? "the best case from here, the games over included" : "the best case, on the full game"}
          />
          <Tile
            label="If every result goes against us"
            value={dollars(t.worst, true)}
            sub={t.over ? "the worst case from here" : "the worst case"}
          />
          {scores ? (
            <Tile
              label="Finished today"
              value={dollars(t.finished, true)}
              sub={`${t.over} of ${t.games} over; provisional until the nightly settlement`}
            />
          ) : null}
        </div>
      </Card>
      {scoreError ? <p className="muted">Live scores unavailable: {scoreError}</p> : null}
      <div className="cards">
        {games.map((g) => (
          <GameCard key={g.game_id} game={g} score={byId.get(g.game_id)} scored={scores !== null} />
        ))}
      </div>
      {scores ? (
        <p className="muted">
          Scores from the NHL&apos;s scoreboard of {live.day}, read at {helsinkiTime(scores.fetched_utc)}{" "}
          Helsinki time, every 30 seconds while a game is on. A score never enters the record: the
          nightly settlement and record stay the results.
        </p>
      ) : null}
    </>
  );
}

function Tile({ label, value, sub }: { label: string; value: string; sub: string }) {
  return (
    <div className="tile">
      <div className="label">{label}</div>
      <div className="value">{value}</div>
      <div className="sub">{sub}</div>
    </div>
  );
}

function tone(state: LiveRow["state"]): "good" | "warn" | "info" | undefined {
  if (state === "quoting") return "info";
  if (state === "pulled_move" || state === "pulled_starter") return "warn";
  return undefined;
}

function GameCard({
  game,
  score,
  scored,
}: {
  game: LiveGame;
  score: ScoreGame | undefined;
  /** Whether the scoreboard has been read: before, a card shows no standing. */
  scored: boolean;
}) {
  const state = game.home.state === "quoting" || game.away.state === "quoting" ? "quoting" : game.home.state;
  const now = scored ? standing(game, score) : null;
  return (
    <article className="card game-card" aria-label={`${game.away.team} at ${game.home.team}`}>
      <ScoreRow
        away={game.away.team}
        home={game.home.team}
        game={score}
        day={game.home.game_date}
        fallback={helsinkiTime(game.start_utc, game.home.game_date)}
      />
      <div className="card-title">
        <Chip tone={tone(state)}>{STATE_NAMES[state]}</Chip>
      </div>
      <div className="scroll">
        <table>
          <thead>
            <tr>
              <th>Team</th>
              <th className="number">Our value</th>
              <th className="number">Kalshi bid–ask</th>
              <th className="number">Our bid</th>
              <th className="number">Holding</th>
              <th className="number">If it wins</th>
            </tr>
          </thead>
          <tbody>
            {[game.away, game.home].map((r) => (
              <tr key={r.team}>
                <td>
                  <Logo team={r.team} /> {r.team}
                </td>
                <td className="number">{priceCents(r.fair)}</td>
                <td className="number">
                  {priceCents(r.yes_bid)}–{priceCents(r.yes_ask)}
                </td>
                <td className="number">
                  {r.bid_price === null ? "–" : `${priceCents(r.bid_price)} × ${r.bid_left ?? 0}`}
                </td>
                <td className="number">
                  {r.contracts > 0
                    ? `${r.contracts.toFixed(0)} at ${priceCents(r.cost_usd / r.contracts)}`
                    : "–"}
                </td>
                <td className="number">{dollars(r.home ? game.ifHome : game.ifAway, true)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {now ? (
        // Won and lost carry the board's own ✓ and ✗; the others a dot in their colour.
        <p className={`now ${now.tone}`}>
          {now.tone === "won" || now.tone === "lost" ? null : <span className="dot" aria-hidden="true" />}
          {now.text}
        </p>
      ) : null}
      {game.pairs > 0 ? (
        <p className="muted">
          {game.pairs.toFixed(0)} contracts on both teams: ${game.pairs.toFixed(0)} paid whatever
          happens.
        </p>
      ) : null}
    </article>
  );
}
