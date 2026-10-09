import { easternTime, estimate, percent, shortDate } from "@/lib/format";
import { GLOSSARY, type TermKey } from "@/lib/glossary";
import {
  bankroll,
  betsPerDay,
  record,
  result,
  REVIEW_DRAWDOWN,
  scoreLine,
  START,
} from "@/lib/results";
import type { Bet, Figure, LiveReport, Prediction, Score } from "@/lib/types";

import { ColumnChart, LineChart, ScatterChart } from "./charts";

// Probability gaps above this many points get a hand review (hard rule 8), as in the report.
const GAP = 0.08;

const key = (game_date: string, game_id: number) => `${game_date}/${game_id}`;

/** A day's slate: each game's decision, B1 and the blend, and its bet if any. */
export function Slate({
  day,
  rows,
  bets,
}: {
  day: string | null;
  rows: Prediction[];
  bets: Bet[];
}) {
  if (!day) return <p className="muted">No decision yet.</p>;
  const placed = new Map(bets.map((b) => [key(b.game_date, b.game_id), b]));
  return (
    <div className="scroll">
      <table>
        <thead>
          <tr>
            <th>Game</th>
            <th>Start (ET)</th>
            <th>Status</th>
            <th className="number">Home price</th>
            <th className="number">Away price</th>
            <th className="number">B1</th>
            <th className="number">Blend</th>
            <th className="number">Gap</th>
            <th>Bet</th>
            <th className="number">Stake</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const bet = placed.get(key(r.game_date, r.game_id));
            const gap = r.p_blend !== null && r.p_b1 !== null ? r.p_blend - r.p_b1 : null;
            return (
              <tr key={r.game_id}>
                <td>
                  {r.away} at {r.home}
                </td>
                <td>{easternTime(r.start_utc)}</td>
                <td>{r.status}</td>
                <td className="number">{price(r.home_price)}</td>
                <td className="number">{price(r.away_price)}</td>
                <td className="number">{percent(r.p_b1)}</td>
                <td className="number">{percent(r.p_blend)}</td>
                <td className="number">
                  {gap === null ? "" : `${(100 * gap).toFixed(1)}`}
                  {gap !== null && Math.abs(gap) > GAP ? " review" : ""}
                </td>
                <td>
                  {bet ? `${bet.side === "home" ? bet.home : bet.away} @ ${price(bet.price)}` : ""}
                </td>
                <td className="number">{bet ? bet.stake.toFixed(2) : ""}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <p className="muted">
        Slate of {day}. Gap is the blend minus B1 in points; above {100 * GAP} it goes to hand
        review.
      </p>
      <Explain terms={["price", "b1", "blend", "gap", "status", "stake"]} />
    </div>
  );
}

/** The paper bets, newest first, with their result, score, profit and closing line value once
 * settled (ADR 0034). scores is null while the owner can't read the games' scores. */
export function Ledger({ bets, scores }: { bets: Bet[]; scores: Record<number, Score> | null }) {
  if (bets.length === 0) return <p className="muted">No paper bet yet.</p>;
  return (
    <div className="scroll">
      <table>
        <thead>
          <tr>
            <th>Date</th>
            <th>Game</th>
            <th>Side</th>
            <th className="number">Price</th>
            <th className="number">p</th>
            <th className="number">EV</th>
            <th className="number">Stake</th>
            <th>Result</th>
            <th>Score</th>
            <th className="number">Profit</th>
            <th>Close</th>
            <th className="number">CLV</th>
            <th className="number">Fair move</th>
          </tr>
        </thead>
        <tbody>
          {bets.map((b) => (
            <tr key={key(b.game_date, b.game_id)}>
              <td>{b.game_date}</td>
              <td>
                {b.away} at {b.home}
              </td>
              <td>{b.side === "home" ? b.home : b.away}</td>
              <td className="number">{price(b.price)}</td>
              <td className="number">{percent(b.p_side)}</td>
              <td className="number">{percent(b.ev)}</td>
              <td className="number">{b.stake.toFixed(2)}</td>
              <td className={b.settlement === "settled" ? (b.won ? "won" : "lost") : undefined}>
                {result(b)}
              </td>
              <td>{scoreLine(b, scores?.[b.game_id])}</td>
              <td className="number">{b.profit === null ? "" : signedUnits(b.profit)}</td>
              <td>{b.close_status ?? ""}</td>
              <td className="number">{signed(b.clv)}</td>
              <td className="number">{signed(b.fair_move)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {scores === null ? (
        <p className="muted">
          Scores show once the owner applies the migration
          supabase/migrations/20261009120000_games_owner_reads.sql (web/README.md).
        </p>
      ) : null}
      <Explain
        terms={[
          "side", "price", "p", "ev", "hurdle", "u", "stake",
          "result", "score", "profit", "close", "clv", "fairMove",
        ]}
      />
    </div>
  );
}

/** The headline figures: CLV per bet first, the measure the test is judged on, then the bankroll,
 * the record and the drawdown (ADR 0034). */
export function Headline({ bets, report }: { bets: Bet[]; report: LiveReport | null }) {
  const days = bankroll(bets);
  const now = days[days.length - 1];
  const r = record(bets);
  const clv = report?.closing_value.clv_per_bet;
  const centre = clv?.mean ?? clv?.value;
  return (
    <div className="tiles">
      <div className="tile">
        <div className="label">CLV per bet, the measure</div>
        <div className="value">{centre === undefined ? "–" : signed(centre)}</div>
        <div className="sub">{clv ? estimate(clv, "bets") : "no report yet"}</div>
      </div>
      <div className="tile">
        <div className="label">Bankroll (paper)</div>
        <div className="value">{(now?.balance ?? START).toFixed(2)}</div>
        <div className="sub">
          units, from {START}
          {r.open ? `; ${r.inPlay.toFixed(2)} in play` : ""}
        </div>
      </div>
      <div className="tile">
        <div className="label">Record</div>
        <div className="value">
          {r.won}–{r.lost}
        </div>
        <div className="sub">
          won–lost; {r.open} awaiting{r.void ? `, ${r.void} void` : ""}
        </div>
      </div>
      <div className="tile">
        <div className="label">Drawdown from the peak</div>
        <div className="value">{percent(now ? -now.drawdown : 0)}</div>
        <div className="sub">review at {(100 * REVIEW_DRAWDOWN).toFixed(0)}%</div>
      </div>
    </div>
  );
}

/** The results during the season (ADR 0034): the bankroll after each settled day with the
 * drawdown review line, the bets per day, and each settled bet's expected return against its CLV,
 * with the note that over this few bets results are mostly luck. */
export function Results({ bets }: { bets: Bet[] }) {
  if (bets.length === 0) return <p className="muted">No paper bet yet.</p>;
  const days = bankroll(bets);
  const r = record(bets);
  const withClv = bets.filter((b) => b.clv !== null);
  // The review line is drawn once the bankroll has fallen half way to it; before that it would
  // only squash the bankroll's own line, and the caption says how far off it is.
  const deepest = Math.max(0, ...days.map((d) => -d.drawdown));
  const nearReview = deepest >= REVIEW_DRAWDOWN / 2;
  return (
    <>
      <p className="notice">
        Over this few bets, results are mostly luck: a 58% bet still loses 42% of the time. CLV
        is the measure; nothing in the policy changes because of these numbers (ADR 0034).
      </p>
      <p>
        {r.won + r.lost} settled bets: {r.won} won, {r.lost} lost. Profit{" "}
        {signedUnits(r.profit)} units on {r.staked.toFixed(2)} staked.
      </p>
      <h3>Bankroll after each day</h3>
      {days.length === 0 ? (
        <p className="muted">No settled bet yet: the nightly run settles each night&apos;s games.</p>
      ) : (
        <>
          <LineChart
            title="Paper bankroll after each settled day, in units"
            points={days.map((d) => ({ label: shortDate(d.date), value: d.balance }))}
            format={(v) => v.toFixed(1)}
            reference={
              nearReview
                ? {
                    values: days.map((d) => d.peak * (1 - REVIEW_DRAWDOWN)),
                    label: `${(100 * REVIEW_DRAWDOWN).toFixed(0)}% below the peak: review`,
                  }
                : undefined
            }
          />
          {nearReview ? null : (
            <p className="muted">
              The review line, {(100 * REVIEW_DRAWDOWN).toFixed(0)}% below the peak, is off this
              scale: the deepest drawdown so far is {percent(deepest)}.
            </p>
          )}
          <details className="explain">
            <summary>As a table</summary>
            <div className="scroll">
              <table>
                <thead>
                  <tr>
                    <th>Day</th>
                    <th className="number">Bets</th>
                    <th className="number">Profit</th>
                    <th className="number">Bankroll</th>
                    <th className="number">Drawdown</th>
                  </tr>
                </thead>
                <tbody>
                  {days.map((d) => (
                    <tr key={d.date}>
                      <td>{d.date}</td>
                      <td className="number">{d.bets}</td>
                      <td className="number">{signedUnits(d.profit)}</td>
                      <td className="number">{d.balance.toFixed(2)}</td>
                      <td className="number">{percent(-d.drawdown)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </details>
        </>
      )}
      <h3>Bets per day</h3>
      <ColumnChart
        title="Paper bets per game day"
        bars={betsPerDay(bets).map((d) => ({ label: shortDate(d.date), value: d.bets }))}
        format={(v) => v.toFixed(0)}
      />
      <h3>Expected return against CLV</h3>
      {withClv.length === 0 ? (
        <p className="muted">No bet with a closing price yet.</p>
      ) : (
        <>
          <ScatterChart
            title="Each settled bet's expected return at the decision against its CLV at the close"
            points={withClv.map((b) => ({
              x: b.ev,
              y: b.clv as number,
              label: `${b.game_date} ${b.away} at ${b.home}`,
            }))}
            xTitle="EV"
            yTitle="CLV"
            format={(v) => percent(v)}
          />
          <p className="muted">
            Right of the middle line: the model expected a profit. Above it: the price beat the
            fair closing price. Bets in the top right are the ones the model was right about
            before the game started, whatever the result.
          </p>
        </>
      )}
      <Explain terms={["units", "result", "profit", "drawdown", "luck", "ev", "clv"]} />
    </>
  );
}

/** Each nightly report's CLV per bet to its date, the primary measure as it built up, newest
 * first. */
export function ClvHistory({ rows }: { rows: { as_of: string; clv: Figure }[] }) {
  if (rows.length === 0) {
    return <p className="muted">No live report yet: the nightly run writes one.</p>;
  }
  const oldest = [...rows].reverse().filter((r) => (r.clv.mean ?? r.clv.value) !== undefined);
  return (
    <div className="scroll">
      {oldest.length ? (
        <LineChart
          title="CLV per bet to date, with its 95% interval"
          points={oldest.map((r) => ({
            label: shortDate(r.as_of),
            value: (r.clv.mean ?? r.clv.value) as number,
            low: r.clv.low,
            high: r.clv.high,
          }))}
          format={(v) => percent(v)}
          zero
        />
      ) : (
        <p className="muted">
          The chart starts once a report has four weeks of bets, enough for an interval.
        </p>
      )}
      <table>
        <thead>
          <tr>
            <th>Report</th>
            <th>CLV per bet to date</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.as_of}>
              <td>{r.as_of}</td>
              <td>{estimate(r.clv, "bets")}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <Explain terms={["clv", "interval"]} />
    </div>
  );
}

/** The latest live report: coverage, CLV with its interval, the comparisons, calibration, gaps
 * and alerts, with the verdicts it gives (none before the formal review). */
export function Report({ report }: { report: LiveReport | null }) {
  if (!report) return <p className="muted">No live report yet: the nightly run writes one.</p>;
  const cover = report.coverage;
  const value = report.closing_value;
  const ops = report.alerts.operations;
  const dd = report.alerts.drawdown;
  const calibration = report.calibration;
  return (
    <>
      <p className="muted">
        As of {report.as_of}, policy <code>{report.policy_version}</code>, live fit{" "}
        {report.blend_versions.join(", ") || "none"}. Every figure has its 95% weekly block
        bootstrap interval; one over fewer than four weeks gives only its counts.
      </p>

      <h3>Coverage</h3>
      <ul>
        <li>
          Slate games {cover.slate_games}, predicted {cover.predicted}, awaiting a result{" "}
          {cover.awaiting_result}.
        </li>
        <li>
          Bets {cover.bets}: {cover.settled} settled, {cover.awaiting_settlement} awaiting a result,{" "}
          {cover.void_postponed} void.
        </li>
        <li>
          Eligible {cover.eligible} (after {cover.no_pregame_snapshot} with no pre-game snapshot),
          with a closing proxy {cover.with_proxy}.
        </li>
      </ul>

      <h3>Closing line value, the primary measure</h3>
      <ul>
        <li>
          <strong>CLV per bet</strong> against Pinnacle&apos;s fair closing proxy:{" "}
          {estimate(value.clv_per_bet, "bets")}
        </li>
        <li>Stake-weighted: {estimate(value.clv_stake_weighted)}</li>
        <li>Fair move per bet: {estimate(value.fair_move_per_bet, "bets")}</li>
        <li>
          Coverage floor:{" "}
          {value.floor.share_with_proxy === null
            ? "no eligible bet yet"
            : `${(100 * value.floor.share_with_proxy).toFixed(1)}% of eligible bets have a proxy`}{" "}
          (at least {(100 * value.floor.required).toFixed(0)}% needed).
        </li>
        {Object.entries(value.bound).map(([name, bound]) => (
          <li key={name}>
            Bound at the {name.slice(1)}th percentile ({signed(bound.stand_in)} for each of{" "}
            {bound.imputed} without a proxy): {estimate(bound.clv_per_bet, "bets")}
          </li>
        ))}
        <li>Verdict: {report.verdicts.closing_value}.</li>
      </ul>

      <h3>Model comparisons</h3>
      <p className="muted">
        Mean log loss difference per game, paired: negative favours the first.
      </p>
      <ul>
        {Object.entries(report.comparisons).map(([name, compared]) => (
          <li key={name}>
            {name}: {estimate(compared.difference)}
            {compared.left_out ? `; ${compared.left_out} left out` : ""}
          </li>
        ))}
      </ul>

      <h3>Calibration of the blend</h3>
      {calibration.at ? (
        <>
          <p>
            Intercept {estimate(calibration.intercept)}, slope {estimate(calibration.slope)}, on{" "}
            {calibration.games} games over {calibration.weeks} weeks.
          </p>
          <div className="scroll">
            <table>
              <thead>
                <tr>
                  <th className="number">Forecast</th>
                  <th>Band</th>
                  <th>Recalibrated</th>
                </tr>
              </thead>
              <tbody>
                {calibration.at.map((r) => (
                  <tr key={r.forecast}>
                    <td className="number">{percent(r.forecast)}</td>
                    <td>
                      {percent(r.band[0])} to {percent(r.band[1])}
                    </td>
                    <td>
                      {percent(r.recalibrated.value)} [{percent(r.recalibrated.low)},{" "}
                      {percent(r.recalibrated.high)}]
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      ) : (
        <p>{estimate(calibration)}</p>
      )}
      <p>Verdict: {report.verdicts.calibration}.</p>

      <h3>Gaps above {100 * GAP} points for hand review</h3>
      {report.gaps.length === 0 ? (
        <p>None.</p>
      ) : (
        <div className="scroll">
          <table>
            <thead>
              <tr>
                <th>Date</th>
                <th>Game</th>
                <th className="number">B1</th>
                <th className="number">B3</th>
                <th className="number">Blend</th>
                <th className="number">Gap</th>
                <th>Bet</th>
              </tr>
            </thead>
            <tbody>
              {report.gaps.map((g) => (
                <tr key={key(g.game_date, g.game_id)}>
                  <td>{g.game_date}</td>
                  <td>
                    {g.away} at {g.home}
                  </td>
                  <td className="number">{percent(g.p_b1)}</td>
                  <td className="number">{percent(g.p_b3)}</td>
                  <td className="number">{percent(g.p_blend)}</td>
                  <td className="number">{(100 * g.gap).toFixed(1)}</td>
                  <td>{g.bet ? (g.side ?? "") : ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <Explain terms={["interim", "coverage", "clv", "fairMove", "interval", "comparison", "calibration", "gap"]} />

      <h3>Operational alerts</h3>
      <p className="muted">These never change the policy.</p>
      <ul>
        <li>
          Days {ops.days}, skipped {ops.days_skipped}. Without Pinnacle&apos;s midday price{" "}
          {ops.no_price}, stale {ops.stale_price}, missing an input {ops.missing_input}.
        </li>
        <li>
          The guard stopped {ops.guarded} of {ops.picked} picked bets.
        </li>
        <li>
          {dd.triggered ? (
            <strong>
              The {(100 * dd.threshold).toFixed(0)}% drawdown review is triggered
              {dd.first_utc ? ` (${dd.first_utc.slice(0, 10)})` : ""}: review the data and code,
              never the model.
            </strong>
          ) : (
            `The ${(100 * dd.threshold).toFixed(0)}% drawdown review is not triggered.`
          )}
        </li>
        {Object.entries(report.alerts.u_range)
          .filter(([name]) => name !== "games")
          .map(([name, n]) => (
            <li key={name}>
              {name.replaceAll("_", " ")}: {n} of {report.alerts.u_range.games} predicted games
            </li>
          ))}
      </ul>

      {report.returns ? (
        <>
          <h3>Return at the taken price (the season&apos;s end only)</h3>
          <ul>
            <li>
              {report.returns.bets} settled bets, {report.returns.staked.toFixed(2)} units staked,
              profit {report.returns.profit.toFixed(2)} units.
            </li>
            <li>Return per bet: {estimate(report.returns.return_per_bet, "bets")}</li>
            {dd.max_drawdown !== undefined ? (
              <li>Largest drawdown: {percent(dd.max_drawdown)}</li>
            ) : null}
          </ul>
        </>
      ) : null}
    </>
  );
}

/** A collapsed "What these mean" panel with the given glossary entries. */
export function Explain({ terms }: { terms: TermKey[] }) {
  return (
    <details className="explain">
      <summary>What these mean</summary>
      <dl>
        {terms.map((t) => (
          <div key={t}>
            <dt>{GLOSSARY[t].term}</dt>
            <dd>{GLOSSARY[t].meaning}</dd>
          </div>
        ))}
      </dl>
    </details>
  );
}

/** Every term on the page, explained. */
export function Glossary() {
  return (
    <dl>
      {Object.entries(GLOSSARY).map(([key, t]) => (
        <div key={key}>
          <dt>
            <strong>{t.term}</strong>
          </dt>
          <dd>{t.meaning}</dd>
        </div>
      ))}
    </dl>
  );
}

function signedUnits(x: number): string {
  return (x >= 0 ? "+" : "") + x.toFixed(2);
}

function price(x: number | null): string {
  return x === null ? "" : Number(x).toFixed(3);
}

function signed(x: number | null): string {
  return x === null ? "" : (x >= 0 ? "+" : "") + x.toFixed(4);
}
