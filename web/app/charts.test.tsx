// @vitest-environment jsdom
// The charts' axis ticks: round values, and whole numbers only on a count chart (#247).
import { cleanup, render } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";

import { ColumnChart, ticks } from "./charts";

afterEach(cleanup);

test("ticks fall at 1, 2 or 5 times a power of ten", () => {
  expect(ticks(0, 1.08)).toEqual([0, 0.5, 1]);
  expect(ticks(101, 104.2)).toEqual([101, 102, 103, 104]);
});

test("a count's ticks are whole numbers, however small its largest value", () => {
  expect(ticks(0, 1.08, 4, 1)).toEqual([0, 1]);
  expect(ticks(0, 2.16, 4, 1)).toEqual([0, 1, 2]);
  expect(ticks(0, 7.56, 4, 1)).toEqual([0, 2, 4, 6]);
});

test("one bet a day labels the axis 0 and 1, never 1, 1 and 0", () => {
  const bars = ["Oct 8", "Oct 9"].map((label) => ({ label, value: 1 }));
  const { container } = render(
    <ColumnChart title="Paper bets per game day" bars={bars} format={(v) => v.toFixed(0)} counts />,
  );
  // The y-axis labels are the ticks right-aligned against the plot.
  const axis = [...container.querySelectorAll("text.tick[text-anchor='end']")]
    .filter((t) => !bars.some((b) => b.label === t.textContent))
    .map((t) => t.textContent);
  expect(axis).toEqual(["0", "1"]);
});
