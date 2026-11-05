"use client";

// The Models tab (#398 step 5): the model against the market over time, each challenger as a
// card, the morning view and the close forecasters, then the full report. Every figure is the
// nightly report's own; a chart draws a point only once it has its interval (hard rule 7), and
// nothing here is a verdict before the formal review (ADR 0032).

import type { SupabaseClient } from "@supabase/supabase-js";
import { useEffect, useState } from "react";

import { easternDate, estimate, shortDate } from "@/lib/format";
import {
  type ChallengerEntry,
  elapsed,
  fitProgress,
  type ForecastSection,
  intervalAbout,
  loadModels,
  type ModelNight,
  primaryFigure,
  primaryName,
  series,
} from "@/lib/models";
import type { Figure, LiveReport } from "@/lib/types";

import { LineChart } from "./charts";
import { Report } from "./sections";
import { Card, Chip } from "./ui";

export function ModelsTab({ client, report }: { client: SupabaseClient; report: LiveReport | null }) {
  const [nights, setNights] = useState<ModelNight[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const policy = report?.policy_version ?? null;
  useEffect(() => {
    if (policy === null) return;
    loadModels(client, policy).then(setNights, (e: Error) => setError(e.message));
  }, [client, policy]);

  if (!report) return <p className="muted">No live report yet: the nightly run writes one.</p>;
  const history = nights ?? [];
  const today = easternDate(new Date());
  const entries = Object.entries(report.challengers?.challengers ?? {});
  return (
    <>
      {error ? <p className="notice">Could not read the reports&apos; history: {error}</p> : null}
      {nights === null && !error ? <p className="muted">Loading the reports&apos; history…</p> : null}
      <Card title="The model against the market" topic="models" wide>
        <div className="model-grid">
          <Measure
            title="BLEND − B1, log loss per game"
            figure={report.comparisons["BLEND - B1"]?.difference}
            asOf={report.as_of}
            points={series(history, (n) => n.comparisons?.["BLEND - B1"]?.difference)}
            zero
          />
          <Measure
            title="B3 − B2, log loss per game"
            figure={report.comparisons["B3 - B2"]?.difference}
            asOf={report.as_of}
            points={series(history, (n) => n.comparisons?.["B3 - B2"]?.difference)}
            zero
          />
          <Measure
            title="The blend's calibration slope"
            figure={report.calibration.slope}
            asOf={report.as_of}
            points={series(history, (n) => n.slope)}
            reference={1}
            digits={3}
          />
        </div>
      </Card>
      <Card title="Challengers" topic="challengers" wide>
        {entries.length === 0 ? (
          <p className="muted">No challenger in this report.</p>
        ) : (
          <div className="model-grid">
            {entries.map(([key, entry]) => (
              <ChallengerCard
                key={key}
                id={key}
                entry={entry}
                nights={history}
                asOf={report.as_of}
                review={report.review_date}
                today={today}
              />
            ))}
          </div>
        )}
      </Card>
      <Card title="The morning view and the close forecasters" topic="forecasters" wide>
        <div className="model-grid">
          <Forecaster
            title="The morning view (ADR 0052)"
            measure="Slope of the move to the close on B3's morning disagreement (above 0: the market follows B3)"
            section={report.morning_view}
            points={series(history, (n) => n.morning)}
            asOf={report.as_of}
            review={report.review_date}
            today={today}
            digits={3}
          />
          <Forecaster
            title="The close forecaster (ADR 0054)"
            measure="Its forecast's squared error less no move's, in thousandths (below 0: it forecasts the move)"
            section={report.close_forecast}
            points={series(history, (n) => n.close1)}
            asOf={report.as_of}
            review={report.review_date}
            today={today}
            digits={2}
          />
          <Forecaster
            title="The close forecaster, version 2 (ADR 0056)"
            measure="Its squared error less version 1's, in thousandths (below 0 favours version 2)"
            section={report.close_forecast_2}
            points={series(history, (n) => n.close2)}
            asOf={report.as_of}
            review={report.review_date}
            today={today}
            digits={2}
          />
        </div>
      </Card>
      <Card title="Live report" topic="report" wide>
        <details className="explain">
          <summary>The full report of {report.as_of}</summary>
          <Report report={report} />
        </details>
      </Card>
    </>
  );
}

type Point = { as_of: string; value: number; low: number; high: number };

/** A figure with its chart: the points with their intervals, or about when it gets one. */
function Measure({
  title,
  figure,
  asOf,
  points,
  zero = false,
  reference,
  digits = 4,
  unit = "games",
}: {
  title: string;
  figure: Figure | null | undefined;
  asOf: string;
  points: Point[];
  zero?: boolean;
  reference?: number;
  digits?: number;
  unit?: string;
}) {
  const about = intervalAbout(figure, asOf);
  return (
    <div className="model-card">
      <h3>{title}</h3>
      <p className="figure">{estimate(figure, unit, digits)}</p>
      {points.length ? (
        <LineChart
          title={`${title}, with its 95% interval`}
          points={points.map((p) => ({ label: shortDate(p.as_of), value: p.value, low: p.low, high: p.high }))}
          format={(v) => v.toFixed(digits)}
          zero={zero}
          reference={
            reference === undefined
              ? undefined
              : { values: points.map(() => reference), label: `${reference}: calibrated` }
          }
        />
      ) : null}
      {about ? (
        <p className="muted">
          <Chip>counts only</Chip> The chart starts once the figure has its interval, about {about}.
        </p>
      ) : null}
    </div>
  );
}

function notPredicted(counts: Record<string, number>): string {
  const parts = Object.entries(counts).map(([reason, n]) => `${n} ${reason}`);
  return parts.length ? `; not predicted: ${parts.join(", ")}` : "";
}

/** Registration, the interval's date and the formal review, with where today stands. */
function Timeline({
  registered,
  about,
  review,
  today,
  unfitted = false,
}: {
  registered: string;
  about: string | null;
  review: string;
  today: string;
  /** Not fitted yet: its 4 weeks start once it is. */
  unfitted?: boolean;
}) {
  const share = elapsed(registered, review, today);
  return (
    <div className="timeline">
      <div
        className="timeline-bar"
        role="img"
        aria-label={`${Math.round(100 * share)}% of the way from registration to the formal review`}
      >
        <span style={{ width: `${(100 * share).toFixed(1)}%` }} />
      </div>
      <p className="muted">
        Registered {registered} →{" "}
        {unfitted
          ? "intervals 4 weeks after it is fitted"
          : about
            ? `intervals from about ${about}`
            : "intervals now"}{" "}
        → formal review {review}
      </p>
    </div>
  );
}

function ChallengerCard({
  id,
  entry,
  nights,
  asOf,
  review,
  today,
}: {
  id: string;
  entry: ChallengerEntry;
  nights: ModelNight[];
  asOf: string;
  review: string;
  today: string;
}) {
  const figure = primaryFigure(entry);
  const fit = fitProgress(entry);
  const points = series(nights, (n) => primaryFigure(n.challengers?.[id]));
  const withheld = entry.missing_days.length > 0;
  const control = primaryName(entry).startsWith("The control");
  const unfitted = entry.predicted === 0 && (entry.not_predicted.unfitted ?? 0) > 0;
  const about = control ? null : intervalAbout(figure, asOf);
  return (
    <article className="model-card" aria-label={id}>
      <div className="model-head">
        <h3>{entry.name}</h3>
        <span className="muted">
          version {entry.version} · ADR {entry.adr}
        </span>
        {control ? <Chip>control</Chip> : null}
        {unfitted ? <Chip tone="warn">unfitted</Chip> : null}
        {withheld ? <Chip tone="warn">withheld</Chip> : null}
      </div>
      <p className="muted">
        {entry.days} game day{entry.days === 1 ? "" : "s"}, {entry.games} games, {entry.predicted}{" "}
        predicted{notPredicted(entry.not_predicted)}.
      </p>
      {fit ? (
        <div className="fit">
          <div
            className="progress"
            role="progressbar"
            aria-label="Training games toward the minimum"
            aria-valuemin={0}
            aria-valuemax={fit.minimum}
            aria-valuenow={fit.games}
          >
            <span style={{ width: `${(100 * fit.share).toFixed(1)}%` }} />
          </div>
          <p className="muted">
            {fit.games} of {fit.minimum} training games
            {fit.games >= fit.minimum ? ": fitted" : ": it predicts once fitted"}
          </p>
        </div>
      ) : null}
      {withheld ? (
        <p className="warning">
          Withheld: {entry.missing_days.length} game day{entry.missing_days.length === 1 ? "" : "s"}{" "}
          without its output, so none of its figures show.
        </p>
      ) : (
        <>
          <p>
            <strong>{primaryName(entry)}</strong>
            {control ? null : <>: {estimate(figure, "games")}</>}
          </p>
          {points.length ? (
            <LineChart
              title={`${entry.name}'s primary measure, with its 95% interval`}
              points={points.map((p) => ({ label: shortDate(p.as_of), value: p.value, low: p.low, high: p.high }))}
              format={(v) => v.toFixed(4)}
              zero
            />
          ) : null}
        </>
      )}
      {control ? null : (
        <Timeline
          registered={entry.registered_on}
          about={about}
          review={review}
          today={today}
          unfitted={unfitted}
        />
      )}
    </article>
  );
}

function Forecaster({
  title,
  measure,
  section,
  points,
  asOf,
  review,
  today,
  digits,
}: {
  title: string;
  measure: string;
  section: ForecastSection | null | undefined;
  points: Point[];
  asOf: string;
  review: string;
  today: string;
  digits: number;
}) {
  if (!section) {
    return (
      <div className="model-card">
        <h3>{title}</h3>
        <p className="muted">Not in this report yet.</p>
      </div>
    );
  }
  const about = intervalAbout(section.primary, asOf);
  return (
    <div className="model-card">
      <h3>{title}</h3>
      <p className="muted">
        {section.version.includes("/") ? section.version : `Version ${section.version}`}:{" "}
        {section.days} game day{section.days === 1 ? "" : "s"},{" "}
        {section.games} games, {section.predicted} forecast{notPredicted(section.not_predicted)}.
      </p>
      <p>
        <strong>{measure}</strong>: {estimate(section.primary, "games", digits)}
      </p>
      {points.length ? (
        <LineChart
          title={`${title}, with its 95% interval`}
          points={points.map((p) => ({ label: shortDate(p.as_of), value: p.value, low: p.low, high: p.high }))}
          format={(v) => v.toFixed(digits)}
          zero
        />
      ) : null}
      <Timeline registered={section.registered_on} about={about} review={review} today={today} />
    </div>
  );
}
