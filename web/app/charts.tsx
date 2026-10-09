"use client";

// Small SVG charts for the dashboard (#210), drawn to the dataviz method: one series each in the
// series blue, 2px lines, rounded columns at most 24px wide, 8px dots with a surface ring,
// hairline grid and axes, the value in the readout and the name after it. Each chart is drawn at
// its container's width, so its text stays legible on a phone, and answers hover, touch and the
// arrow keys with a readout; the same numbers are in the tables beside it.

import { type KeyboardEvent, type PointerEvent, useEffect, useRef, useState } from "react";

const HEIGHT = 220;
const MARGIN = { top: 16, right: 16, bottom: 28, left: 52 };

/** The container's width in pixels, followed as it resizes (640 where nothing can measure). */
function useWidth() {
  const ref = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(640);
  useEffect(() => {
    const node = ref.current;
    if (!node || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(([entry]) => {
      if (entry.contentRect.width > 0) setWidth(Math.round(entry.contentRect.width));
    });
    observer.observe(node);
    return () => observer.disconnect();
  }, []);
  return { ref, width };
}

/** Round tick values covering [low, high]: about four, at 1, 2 or 5 times a power of ten. */
export function ticks(low: number, high: number, count = 4): number[] {
  if (high === low) return [low];
  const raw = (high - low) / count;
  const power = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 5, 10].map((m) => m * power).find((s) => s >= raw) ?? raw;
  const out: number[] = [];
  for (let t = Math.ceil(low / step) * step; t <= high + step * 1e-9; t += step) {
    out.push(Number(t.toFixed(10)));
  }
  return out;
}

/** The value domain of the numbers, padded by 8% and widened to its round ticks. */
function domain(values: number[]): [number, number] {
  let low = Math.min(...values);
  let high = Math.max(...values);
  if (low === high) {
    low -= Math.abs(low) * 0.05 || 1;
    high += Math.abs(high) * 0.05 || 1;
  }
  const pad = (high - low) * 0.08;
  const t = ticks(low - pad, high + pad);
  return [Math.min(low - pad, t[0]), Math.max(high + pad, t[t.length - 1])];
}

type Readout = { x: number; y: number; value: string; label: string };

function Tooltip({ readout }: { readout: Readout | null }) {
  if (!readout) return null;
  return (
    <div className="chart-tip" style={{ left: readout.x, top: readout.y }} role="status">
      <strong>{readout.value}</strong>
      <span>{readout.label}</span>
    </div>
  );
}

function YAxis({ values, y, width, format }: {
  values: number[];
  y: (v: number) => number;
  width: number;
  format: (v: number) => string;
}) {
  return (
    <g>
      {values.map((v) => (
        <g key={v}>
          <line className="grid" x1={MARGIN.left} x2={width - MARGIN.right} y1={y(v)} y2={y(v)} />
          <text className="tick" x={MARGIN.left - 6} y={y(v)} dy="0.32em" textAnchor="end">
            {format(v)}
          </text>
        </g>
      ))}
    </g>
  );
}

/** The x labels to draw: the first and the last, and up to two between, so none collide. */
function xLabels(count: number, width: number): number[] {
  if (count <= 1) return [0];
  const room = Math.max(2, Math.min(4, Math.floor((width - MARGIN.left) / 90)));
  const picks = new Set<number>();
  for (let k = 0; k < room; k += 1) picks.add(Math.round((k * (count - 1)) / (room - 1)));
  return [...picks];
}

export type LinePoint = { label: string; value: number; low?: number; high?: number };

/** A line over the points, in their order: with its interval as a 10% band when the points carry
 * one, and a reference line (such as the drawdown review line) when given. The readout follows
 * the nearest point. */
export function LineChart({
  title,
  points,
  format,
  reference,
  zero = false,
}: {
  title: string;
  points: LinePoint[];
  format: (v: number) => string;
  reference?: { values: number[]; label: string };
  zero?: boolean;
}) {
  const { ref, width } = useWidth();
  const [active, setActive] = useState<number | null>(null);
  if (points.length === 0) return null;
  const all = points.flatMap((p) => [p.value, p.low ?? p.value, p.high ?? p.value]);
  const [low, high] = domain([...all, ...(reference?.values ?? []), ...(zero ? [0] : [])]);
  const plot = width - MARGIN.left - MARGIN.right;
  const x = (i: number) =>
    MARGIN.left + (points.length === 1 ? plot / 2 : (i * plot) / (points.length - 1));
  const y = (v: number) =>
    MARGIN.top + ((high - v) / (high - low)) * (HEIGHT - MARGIN.top - MARGIN.bottom);
  const path = (vs: number[]) => vs.map((v, i) => `${i ? "L" : "M"}${x(i)},${y(v)}`).join("");
  const banded = points.every((p) => p.low !== undefined && p.high !== undefined);
  const band = banded
    ? `${path(points.map((p) => p.high as number))}${points
        .map((p, i) => [i, p.low as number] as const)
        .reverse()
        .map(([i, v]) => `L${x(i)},${y(v)}`)
        .join("")}Z`
    : null;
  const last = points.length - 1;
  const nearest = (event: PointerEvent<SVGSVGElement>) => {
    const box = event.currentTarget.getBoundingClientRect();
    const at = ((event.clientX - box.left) / box.width) * width;
    const i = points.length === 1 ? 0 : Math.round(((at - MARGIN.left) / plot) * last);
    setActive(Math.max(0, Math.min(last, i)));
  };
  const key = (event: KeyboardEvent<SVGSVGElement>) => {
    const step = { ArrowLeft: -1, ArrowRight: 1 }[event.key];
    if (step === undefined) return;
    event.preventDefault();
    setActive((i) => Math.max(0, Math.min(last, (i ?? last) + step)));
  };
  const shown = active === null ? null : points[active];
  const readout: Readout | null = shown
    ? {
        x: x(active as number),
        y: y(shown.value),
        value: format(shown.value),
        label:
          shown.low !== undefined && shown.high !== undefined
            ? `${shown.label}, interval ${format(shown.low)} to ${format(shown.high)}`
            : shown.label,
      }
    : null;
  return (
    <div className="chart" ref={ref}>
      <svg
        width={width}
        height={HEIGHT}
        role="img"
        aria-label={`${title}: ${format(points[last].value)} at ${points[last].label}`}
        tabIndex={0}
        onPointerMove={nearest}
        onPointerDown={nearest}
        onPointerLeave={() => setActive(null)}
        onFocus={() => setActive(last)}
        onBlur={() => setActive(null)}
        onKeyDown={key}
      >
        <YAxis values={ticks(low, high)} y={y} width={width} format={format} />
        {zero && low < 0 && high > 0 ? (
          <line className="axis" x1={MARGIN.left} x2={width - MARGIN.right} y1={y(0)} y2={y(0)} />
        ) : null}
        {band ? <path className="band" d={band} /> : null}
        {reference ? (
          <>
            <path className="reference" d={path(reference.values)} />
            <text
              className="tick"
              x={width - MARGIN.right}
              y={y(reference.values[reference.values.length - 1])}
              dy="-0.5em"
              textAnchor="end"
            >
              {reference.label}
            </text>
          </>
        ) : null}
        <path className="line" d={path(points.map((p) => p.value))} />
        {shown ? (
          <line
            className="crosshair"
            x1={x(active as number)}
            x2={x(active as number)}
            y1={MARGIN.top}
            y2={HEIGHT - MARGIN.bottom}
          />
        ) : null}
        <circle className="dot" cx={x(shown ? (active as number) : last)} cy={y((shown ?? points[last]).value)} r={4} />
        {xLabels(points.length, width).map((i) => (
          <text
            key={i}
            className="tick"
            x={x(i)}
            y={HEIGHT - 8}
            textAnchor={points.length === 1 ? "middle" : i === 0 ? "start" : i === last ? "end" : "middle"}
          >
            {points[i].label}
          </text>
        ))}
      </svg>
      <Tooltip readout={readout} />
    </div>
  );
}

/** Columns from a zero baseline, one per item, each its own hit target. */
export function ColumnChart({
  title,
  bars,
  format,
}: {
  title: string;
  bars: { label: string; value: number }[];
  format: (v: number) => string;
}) {
  const { ref, width } = useWidth();
  const [active, setActive] = useState<number | null>(null);
  if (bars.length === 0) return null;
  const [, high] = domain([0, ...bars.map((b) => b.value)]);
  const plot = width - MARGIN.left - MARGIN.right;
  const slot = plot / bars.length;
  const thick = Math.min(24, slot * 0.6);
  const y = (v: number) =>
    MARGIN.top + ((high - v) / high) * (HEIGHT - MARGIN.top - MARGIN.bottom);
  const base = y(0);
  const column = (i: number, v: number) => {
    const left = MARGIN.left + i * slot + (slot - thick) / 2;
    const top = y(v);
    const r = Math.min(4, (base - top) / 2, thick / 2);
    return `M${left},${base}V${top + r}Q${left},${top} ${left + r},${top}H${left + thick - r}Q${
      left + thick
    },${top} ${left + thick},${top + r}V${base}Z`;
  };
  const shown = active === null ? null : bars[active];
  return (
    <div className="chart" ref={ref}>
      <svg width={width} height={HEIGHT} role="img" aria-label={title}>
        <YAxis values={ticks(0, high)} y={y} width={width} format={format} />
        {bars.map((b, i) => (
          <g key={b.label}>
            <path className={active === i ? "column lifted" : "column"} d={column(i, b.value)} />
            <rect
              className="hit"
              x={MARGIN.left + i * slot}
              y={MARGIN.top}
              width={slot}
              height={base - MARGIN.top}
              tabIndex={0}
              aria-label={`${b.label}: ${format(b.value)}`}
              onPointerEnter={() => setActive(i)}
              onPointerDown={() => setActive(i)}
              onPointerLeave={() => setActive(null)}
              onFocus={() => setActive(i)}
              onBlur={() => setActive(null)}
            />
          </g>
        ))}
        <line className="axis" x1={MARGIN.left} x2={width - MARGIN.right} y1={base} y2={base} />
        {xLabels(bars.length, width).map((i) => (
          <text
            key={i}
            className="tick"
            x={
              bars.length === 1
                ? MARGIN.left + slot / 2
                : i === 0
                  ? MARGIN.left + i * slot + (slot - thick) / 2
                  : i === bars.length - 1
                    ? MARGIN.left + i * slot + (slot + thick) / 2
                    : MARGIN.left + (i + 0.5) * slot
            }
            y={HEIGHT - 8}
            textAnchor={bars.length === 1 ? "middle" : i === 0 ? "start" : i === bars.length - 1 ? "end" : "middle"}
          >
            {bars[i].label}
          </text>
        ))}
      </svg>
      <Tooltip
        readout={
          shown
            ? {
                x: MARGIN.left + ((active as number) + 0.5) * slot,
                y: y(shown.value),
                value: format(shown.value),
                label: shown.label,
              }
            : null
        }
      />
    </div>
  );
}

/** One dot per item against two value axes, with zero lines where zero is in range. */
export function ScatterChart({
  title,
  points,
  xTitle,
  yTitle,
  format,
}: {
  title: string;
  points: { x: number; y: number; label: string }[];
  xTitle: string;
  yTitle: string;
  format: (v: number) => string;
}) {
  const { ref, width } = useWidth();
  const [active, setActive] = useState<number | null>(null);
  if (points.length === 0) return null;
  const [xLow, xHigh] = domain([0, ...points.map((p) => p.x)]);
  const [yLow, yHigh] = domain([0, ...points.map((p) => p.y)]);
  const bottom = HEIGHT - MARGIN.bottom - 12;
  const sx = (v: number) =>
    MARGIN.left + ((v - xLow) / (xHigh - xLow)) * (width - MARGIN.left - MARGIN.right);
  const sy = (v: number) => MARGIN.top + ((yHigh - v) / (yHigh - yLow)) * (bottom - MARGIN.top);
  const shown = active === null ? null : points[active];
  return (
    <div className="chart" ref={ref}>
      <svg width={width} height={HEIGHT} role="img" aria-label={title}>
        <YAxis values={ticks(yLow, yHigh)} y={sy} width={width} format={format} />
        {xLow < 0 && xHigh > 0 ? (
          <line className="axis" x1={sx(0)} x2={sx(0)} y1={MARGIN.top} y2={bottom} />
        ) : null}
        {yLow < 0 && yHigh > 0 ? (
          <line className="axis" x1={MARGIN.left} x2={width - MARGIN.right} y1={sy(0)} y2={sy(0)} />
        ) : null}
        {ticks(xLow, xHigh).map((v) => (
          <text key={v} className="tick" x={sx(v)} y={bottom + 14} textAnchor="middle">
            {format(v)}
          </text>
        ))}
        <text className="tick" x={width - MARGIN.right} y={HEIGHT - 4} textAnchor="end">
          {xTitle} →
        </text>
        <text className="tick" x={MARGIN.left} y={MARGIN.top - 4}>
          ↑ {yTitle}
        </text>
        {points.map((p, i) => (
          <g key={p.label}>
            <circle className={active === i ? "dot lifted" : "dot"} cx={sx(p.x)} cy={sy(p.y)} r={4} />
            <circle
              className="hit"
              cx={sx(p.x)}
              cy={sy(p.y)}
              r={12}
              tabIndex={0}
              aria-label={`${p.label}: ${xTitle} ${format(p.x)}, ${yTitle} ${format(p.y)}`}
              onPointerEnter={() => setActive(i)}
              onPointerDown={() => setActive(i)}
              onPointerLeave={() => setActive(null)}
              onFocus={() => setActive(i)}
              onBlur={() => setActive(null)}
            />
          </g>
        ))}
      </svg>
      <Tooltip
        readout={
          shown
            ? {
                x: sx(shown.x),
                y: sy(shown.y),
                value: `${yTitle} ${format(shown.y)}, ${xTitle} ${format(shown.x)}`,
                label: shown.label,
              }
            : null
        }
      />
    </div>
  );
}
