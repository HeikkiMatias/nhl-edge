"use client";

// Market making › Results (#398): the Kalshi quoting simulator's figures, owner-only. Paper
// only: a simulator on Kalshi's real books from 2026-10-31, whose fills are an upper bound. Sums
// are shown as they stand; every per-contract figure is the nightly report's, with its weekly
// block bootstrap interval from 4 weeks and counts only before (hard rule 7). No verdict.

import type { SupabaseClient } from "@supabase/supabase-js";
import { useEffect, useState } from "react";

import { estimate, percent, shortDate } from "@/lib/format";
import {
  cents,
  changesOf,
  daysOf,
  DEFAULT_CENTS,
  DEFAULT_FAIR,
  dollars,
  type Fair,
  FAIR_NAMES,
  FAIRS,
  figuresOf,
  HALF_SPREADS,
  HELD_REASONS,
  HOLD_NAMES,
  HOLDS,
  HOLDS_RECORD,
  intervalsFrom,
  loadQuotes,
  MEASURE_NAMES,
  MEASURES,
  PRIMARY_CENTS,
  type QuoteDay,
  type Quotes,
  RECORD,
  REQUOTE_NAMES,
  REQUOTE_RECORD,
  REQUOTE_RULES,
  running,
  totals,
  variantName,
} from "@/lib/quotes";
import type { Estimate } from "@/lib/format";

import { ColumnChart, MultiLineChart } from "./charts";
import { Card, Chip, Segmented } from "./ui";

export function MarketResults({ client }: { client: SupabaseClient }) {
  const [quotes, setQuotes] = useState<Quotes | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [fair, setFair] = useState<Fair>(DEFAULT_FAIR);
  const [half, setHalf] = useState<number>(DEFAULT_CENTS);

  useEffect(() => {
    loadQuotes(client).then(setQuotes, (e: Error) => setError(e.message));
  }, [client]);

  if (error) return <p className="notice">Could not read the simulator&apos;s figures: {error}</p>;
  if (!quotes) return <p className="muted">Loading…</p>;
  if (!quotes.applied) {
    return (
      <p className="notice">
        The quotes migration isn&apos;t applied yet, so there is nothing to show. Apply{" "}
        <code>supabase/migrations/20261106120000_kalshi_quotes.sql</code> and set the repository
        variable <code>SUPABASE_QUOTES=true</code>: the next nightly run copies every day kept
        since 2026-10-31 (web/README.md, owner setup).
      </p>
    );
  }
  const record = quotes.days.filter((d) => d.record === RECORD);
  if (record.length === 0) {
    return (
      <p className="muted">
        No simulator day copied yet: the nightly run copies them once{" "}
        <code>SUPABASE_QUOTES</code> is true.
      </p>
    );
  }
  const dates = [...new Set(record.map((d) => d.game_date))].sort();
  const fairs = FAIRS.filter((f) => record.some((d) => d.fair === f));
  const shown = fairs.includes(fair) ? fair : fairs[0];
  const variant = variantName(shown, half);
  const days = daysOf(record, variant);
  const t = totals(days);
  const from = intervalsFrom(dates[0]);

  return (
    <>
      <p className="notice">
        <Chip tone="info">Paper only</Chip> A simulator of two-sided bids on Kalshi&apos;s real
        order books, game days {dates[0]} to {dates[dates.length - 1]}. A fill counts only when a
        real trade reached our bid, so fills are an upper bound. No real quote has been posted.
        Per-contract figures get their 95% interval once their fills span 4 weeks, from about
        game date {from}; until then, counts only.
        {quotes.report ? ` Figures as of ${quotes.report.as_of}.` : ""}
      </p>

      <div className="pickers">
        <Segmented
          label="Fair value"
          options={fairs.map((f) => ({ value: f, label: FAIR_NAMES[f] }))}
          value={shown}
          onChange={setFair}
        />
        <Segmented
          label="Half-spread"
          options={HALF_SPREADS.map((h) => ({ value: h, label: `${h}¢` }))}
          value={half}
          onChange={setHalf}
        />
      </div>

      <Card title={`${FAIR_NAMES[shown]} at ${half}¢ below: so far`} topic="mmTotals" wide>
        <Tiles t={t} first={days[0]?.game_date} last={days[days.length - 1]?.game_date} />
      </Card>

      <div className="cards">
        <Card title="Luck against edge" topic="luck" wide>
          <Luck days={days} closeShare={t.closeShare} />
        </Card>
        <Card title="Profit per game day" topic="daily">
          <Daily days={days} />
        </Card>
        <Card title="Per contract" topic="perContract">
          <PerContract figures={figuresOf(quotes.report, variant)} from={from} />
        </Card>
        <Card title="Every variant" topic="variants" wide>
          <Variants
            record={record}
            fairs={fairs}
            selected={variant}
            onSelect={(f, h) => {
              setFair(f);
              setHalf(h);
            }}
          />
        </Card>
        <Card title="Hold rules after a pull" topic="holds" wide>
          <Holds days={quotes.days.filter((d) => d.record === HOLDS_RECORD)} report={quotes.holds} />
        </Card>
        <Card title="Requote after a starter change" topic="requote" wide>
          <Requotes
            days={quotes.days.filter((d) => d.record === REQUOTE_RECORD)}
            report={quotes.requote}
          />
        </Card>
      </div>
    </>
  );
}

function Tiles({ t, first, last }: { t: ReturnType<typeof totals>; first?: string; last?: string }) {
  return (
    <div className="tiles">
      <div className="tile">
        <div className="label">Game days</div>
        <div className="value">{t.days}</div>
        <div className="sub">
          {t.games} games{first && last ? `, ${shortDate(first)} to ${shortDate(last)}` : ""}
        </div>
      </div>
      <div className="tile">
        <div className="label">Fills</div>
        <div className="value">{t.fills}</div>
        <div className="sub">
          {t.contracts.toFixed(0)} contracts; {t.posted} bids posted, {t.pulls} pulls
        </div>
      </div>
      <div className="tile">
        <div className="label">Two-sided</div>
        <div className="value">{t.twoSided === null ? "–" : percent(t.twoSided)}</div>
        <div className="sub">of contracts, their payout locked in</div>
      </div>
      <div className="tile">
        <div className="label">Paper profit</div>
        <div className="value">{dollars(t.profit, true)}</div>
        <div className="sub">
          mostly luck at this size
          {t.unsettledDays ? `; ${t.unsettledDays} day${t.unsettledDays > 1 ? "s" : ""} awaiting results` : ""}
        </div>
      </div>
      <div className="tile">
        <div className="label">Expected by our fair value</div>
        <div className="value">{dollars(t.edgeFair, true)}</div>
        <div className="sub">after the fee, on every fill</div>
      </div>
      <div className="tile">
        <div className="label">Expected by Pinnacle&apos;s close</div>
        <div className="value">{t.closeShare ? dollars(t.edgeClose, true) : "–"}</div>
        <div className="sub">
          {t.closeShare === null
            ? "no fill yet"
            : `after the fee, on ${percent(t.closeShare)} of contracts`}
        </div>
      </div>
    </div>
  );
}

function Luck({ days, closeShare }: { days: QuoteDay[]; closeShare: number | null }) {
  if (days.length === 0) return <p className="muted">This variant has no game day yet.</p>;
  const r = running(days);
  const share = closeShare === null ? "" : ` (${percent(closeShare)} of contracts)`;
  return (
    <>
      <MultiLineChart
        title="Running paper profit against the profit expected, in dollars"
        labels={r.dates.map(shortDate)}
        series={[
          { name: "Paper profit", values: r.profit },
          { name: "Expected by our fair value", values: r.fair },
          { name: `Expected by Pinnacle's close${share}`, values: r.close },
        ]}
        format={(v) => dollars(v, true)}
      />
      <details className="explain">
        <summary>As a table</summary>
        <div className="scroll">
          <table>
            <thead>
              <tr>
                <th>Game day</th>
                <th className="number">Fills</th>
                <th className="number">Paper profit</th>
                <th className="number">By our value</th>
                <th className="number">By the close</th>
              </tr>
            </thead>
            <tbody>
              {days.map((d, i) => (
                <tr key={d.game_date}>
                  <td>{d.game_date}</td>
                  <td className="number">{d.fills}</td>
                  <td className="number">{dollars(r.profit[i], true)}</td>
                  <td className="number">{dollars(r.fair[i], true)}</td>
                  <td className="number">{dollars(r.close[i], true)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </>
  );
}

function Daily({ days }: { days: QuoteDay[] }) {
  const settled = days.filter((d) => d.profit_usd !== null);
  if (settled.length === 0) return <p className="muted">No settled game day yet.</p>;
  return (
    <ColumnChart
      title="Paper profit per settled game day, in dollars"
      bars={settled.map((d) => ({ label: shortDate(d.game_date), value: d.profit_usd as number }))}
      format={(v) => dollars(v, true)}
      signed
    />
  );
}

const hasInterval = (e: Estimate) => e.low !== undefined && e.high !== undefined;

/** Each measure per contract on one axis in cents: a dot and its interval when the report gives
 * one, else its counts. */
function PerContract({
  figures,
  from,
}: {
  figures: ReturnType<typeof figuresOf>;
  from: string;
}) {
  if (!figures || figures.fills === 0) {
    return <p className="muted">No fill for this variant yet.</p>;
  }
  const rows = MEASURES.map((m) => ({ m, e: figures[m] ?? null }));
  const banded = rows.filter((r): r is { m: (typeof MEASURES)[number]; e: Estimate } =>
    r.e !== null && hasInterval(r.e),
  );
  // A shared scale: at least ±4¢, wide enough for every interval.
  const reach = Math.max(0.04, ...banded.flatMap((r) => [Math.abs(r.e.low as number), Math.abs(r.e.high as number)]));
  const at = (v: number) => `${50 + (50 * v) / reach}%`;
  return (
    <>
      {banded.length === 0 ? (
        <p className="muted">
          Counts only until the fills span 4 weeks, from about game date {from}.
        </p>
      ) : null}
      <ul className="intervals">
        {rows.map(({ m, e }) => (
          <li key={m}>
            <span className="interval-name">{MEASURE_NAMES[m]}</span>
            {e && hasInterval(e) ? (
              <>
                <span className="interval-track" aria-hidden="true">
                  <span className="interval-zero" />
                  <span
                    className="interval-band"
                    style={{ left: at(e.low as number), right: `calc(100% - ${at(e.high as number)})` }}
                  />
                  <span className="interval-dot" style={{ left: at((e.mean ?? e.value) as number) }} />
                </span>
                <span className="interval-value">
                  {cents(e.mean ?? e.value)} [{cents(e.low)}, {cents(e.high)}]
                </span>
              </>
            ) : (
              <span className="interval-value muted">
                {e
                  ? `${e.games ?? 0} fills over ${e.weeks ?? 0} week${e.weeks === 1 ? "" : "s"}: counts only`
                  : "none yet"}
              </span>
            )}
          </li>
        ))}
      </ul>
      {banded.length ? (
        <p className="muted">
          The scale runs from {cents(-reach)} to {cents(reach)} per contract; right of the centre
          line is good for us.
        </p>
      ) : null}
    </>
  );
}

function Variants({
  record,
  fairs,
  selected,
  onSelect,
}: {
  record: QuoteDay[];
  fairs: Fair[];
  selected: string;
  onSelect: (fair: Fair, cents: number) => void;
}) {
  return (
    <div className="scroll">
      <table>
        <thead>
          <tr>
            <th>Variant</th>
            <th className="number">Days</th>
            <th className="number">Fills</th>
            <th className="number">Contracts</th>
            <th className="number">Two-sided</th>
            <th className="number">Paper profit</th>
            <th className="number">By our value</th>
            <th className="number">By the close</th>
          </tr>
        </thead>
        <tbody>
          {fairs.flatMap((f) =>
            HALF_SPREADS.map((h) => {
              const name = variantName(f, h);
              const t = totals(daysOf(record, name));
              return (
                <tr key={name} className={name === selected ? "selected" : undefined}>
                  <td>
                    <button
                      type="button"
                      className="link"
                      aria-pressed={name === selected}
                      onClick={() => onSelect(f, h)}
                    >
                      {FAIR_NAMES[f]}, {h}¢
                    </button>
                  </td>
                  <td className="number">{t.days}</td>
                  <td className="number">{t.fills}</td>
                  <td className="number">{t.contracts.toFixed(0)}</td>
                  <td className="number">{t.twoSided === null ? "–" : percent(t.twoSided)}</td>
                  <td className="number">{dollars(t.profit, true)}</td>
                  <td className="number">{dollars(t.edgeFair, true)}</td>
                  <td className="number">
                    {t.closeShare ? `${dollars(t.edgeClose, true)} (${percent(t.closeShare)})` : "–"}
                  </td>
                </tr>
              );
            }),
          )}
        </tbody>
      </table>
    </div>
  );
}

function Holds({ days, report }: { days: QuoteDay[]; report: Quotes["holds"] }) {
  if (days.length === 0) return <p className="muted">No hold rules day copied yet.</p>;
  const against = report?.figures.against_wait ?? {};
  const holm = against.holm as Record<string, number> | undefined;
  return (
    <>
      <div className="scroll">
        <table>
          <thead>
            <tr>
              <th>Rule</th>
              <th className="number">Half-spread</th>
              <th className="number">Fills</th>
              <th className="number">Minutes quoted</th>
              <th className="number">Paper profit</th>
              <th>Profit per game less wait&apos;s</th>
            </tr>
          </thead>
          <tbody>
            {HOLDS.flatMap((hold) =>
              HALF_SPREADS.map((h) => {
                const name = variantName("model", h, hold);
                const own = days.filter((d) => d.variant === name);
                const t = totals(own);
                const minutes = own.reduce((s, d) => s + (d.quoted_minutes ?? 0), 0);
                const paired = against[name] as Estimate | undefined;
                return (
                  <tr key={name} className={h === PRIMARY_CENTS ? "primary" : undefined}>
                    <td>{HOLD_NAMES[hold]}</td>
                    <td className="number">{h}¢</td>
                    <td className="number">{t.fills}</td>
                    <td className="number">{minutes.toFixed(0)}</td>
                    <td className="number">{dollars(t.profit, true)}</td>
                    <td>
                      {hold === "wait"
                        ? "the baseline"
                        : paired
                          ? `${estimate(paired, "games", 2)}${holm?.[name] !== undefined ? `; Holm p ${holm[name].toFixed(3)}` : ""}`
                          : "–"}
                    </td>
                  </tr>
                );
              }),
            )}
          </tbody>
        </table>
      </div>
      <p className="muted">
        The primary measure is at 1¢ (highlighted). Its interval comes from game dates through
        2026-11-23; then you pick a rule (#385).
      </p>
    </>
  );
}

// The requote record's first game date (ADR 0059): the day after its rule was fixed.
const REQUOTE_FIRST_DAY = "2026-11-09";

function Requotes({ days, report }: { days: QuoteDay[]; report: Quotes["requote"] }) {
  if (days.length === 0) {
    return (
      <p className="muted">
        No requote day copied yet. The record starts with game date {REQUOTE_FIRST_DAY}, and the
        nightly run copies it once <code>supabase/migrations/20261108120000_quote_requote.sql</code>{" "}
        is applied.
      </p>
    );
  }
  const against = report?.figures.against_wait ?? {};
  const changes = changesOf(report);
  const first = [...new Set(days.map((d) => d.game_date))].sort()[0];
  return (
    <>
      <div className="scroll">
        <table>
          <thead>
            <tr>
              <th>Rule</th>
              <th className="number">Half-spread</th>
              <th className="number">Fills</th>
              <th className="number">Minutes quoted</th>
              <th className="number">Paper profit</th>
              <th>Profit per game less wait&apos;s</th>
            </tr>
          </thead>
          <tbody>
            {REQUOTE_RULES.flatMap((rule) =>
              HALF_SPREADS.map((h) => {
                const name = variantName("model", h, rule);
                const own = days.filter((d) => d.variant === name);
                const t = totals(own);
                const minutes = own.reduce((s, d) => s + (d.quoted_minutes ?? 0), 0);
                const paired = against[name] as Estimate | undefined;
                return (
                  <tr key={name} className={h === PRIMARY_CENTS ? "primary" : undefined}>
                    <td>{REQUOTE_NAMES[rule]}</td>
                    <td className="number">{h}¢</td>
                    <td className="number">{t.fills}</td>
                    <td className="number">{minutes.toFixed(0)}</td>
                    <td className="number">{dollars(t.profit, true)}</td>
                    <td>
                      {rule === "wait" ? "the baseline" : paired ? estimate(paired, "games", 2) : "–"}
                    </td>
                  </tr>
                );
              }),
            )}
          </tbody>
        </table>
      </div>
      {changes ? (
        <p>
          {changes.changes} starter {changes.changes === 1 ? "change" : "changes"}:{" "}
          {changes.requoted} requoted
          {changes.held.length
            ? `, ${changes.changes - changes.requoted} held (${changes.held
                .map(([reason, n]) => `${n} ${HELD_REASONS[reason] ?? reason}`)
                .join("; ")})`
            : ""}
          .
        </p>
      ) : null}
      <p className="muted">
        The primary measure is at 1¢ (highlighted). Its interval comes once the record spans 4
        weeks, from about game date {intervalsFrom(first)}; then you decide whether the
        simulator, the demo quoter or real orders should requote (ADR 0059).
      </p>
    </>
  );
}
