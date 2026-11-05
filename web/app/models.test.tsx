// @vitest-environment jsdom
// The Models tab (#398 step 5): the history is read for the report's policy only, with the JSON
// paths the charts need; a card draws a chart only once its figure has an interval, and says
// about when otherwise; the control, an unfitted challenger and a withheld one say so. Synthetic
// figures only, on the PostgREST stand-in.
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import fixture from "@/lib/fixtures/live-report.json";
import { type Sent, stubbed } from "@/lib/testing";
import type { LiveReport } from "@/lib/types";

import { ModelsTab } from "./models";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-11-21T12:00:00Z"));
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

const counts = { games: 66, weeks: 2 };
const band = (mean: number) => ({ mean, low: mean - 0.01, high: mean + 0.01, games: 400, weeks: 5 });
const base = {
  version: "1",
  adr: "0041",
  registered_on: "2026-10-22",
  days: 20,
  games: 130,
  predicted: 130,
  not_predicted: {},
  missing_days: [],
  pending_days: [],
};
const CHALLENGERS = {
  "market_only/1": { ...base, name: "market_only", adr: "0038", primary: null, registered_on: "2026-10-15" },
  "conservative_blend/1": {
    ...base,
    name: "conservative_blend",
    primary: "BLEND",
    predicted: 0,
    not_predicted: { unfitted: 130 },
    latest_fit: { games: 77, minimum: 200, week: "2026-11-16" },
    comparisons: { BLEND: { difference: counts, left_out: 0 } },
  },
  "b3_split/1": {
    ...base,
    name: "b3_split",
    adr: "0044",
    primary: "B3",
    comparisons: { B3: { difference: band(-0.002), left_out: 0 } },
  },
  "team_residual_blend/1": { ...base, name: "team_residual_blend", adr: "0043", primary: "conservative_blend/1", missing_days: ["2026-11-03"] },
};
const REPORT = {
  ...fixture,
  as_of: "2026-11-20",
  kind: "interim",
  policy_version: "policy-v2",
  review_date: "2027-04-12",
  comparisons: { "BLEND - B1": { difference: band(-0.003), left_out: 0 }, "B3 - B2": { difference: counts, left_out: 0 } },
  calibration: { games: 400, weeks: 5, slope: { value: 0.95, low: 0.8, high: 1.1, games: 400, weeks: 5 } },
  challengers: { registered: 4, challengers: CHALLENGERS },
  morning_view: { version: "morning_view/1", registered_on: "2026-10-30", days: 15, games: 90, predicted: 88, not_predicted: { "no Pinnacle morning price": 2 }, missing_days: [], primary: counts },
  close_forecast: null,
} as unknown as LiveReport;

const night = (as_of: string, blend: object, b3: object) => ({
  as_of,
  comparisons: REPORT.comparisons && { "BLEND - B1": { difference: blend, left_out: 0 } },
  slope: null,
  challengers: { "b3_split/1": { ...CHALLENGERS["b3_split/1"], comparisons: { B3: { difference: b3, left_out: 0 } } } },
  morning: null,
  close1: null,
  close2: null,
});

function client() {
  const history = [night("2026-11-18", counts, counts), night("2026-11-19", band(-0.002), band(-0.001)), night("2026-11-20", band(-0.003), band(-0.002))];
  // One page, as PostgREST would page it: the next offset reads nothing.
  const answer = (sent: Sent) =>
    sent.url.pathname.endsWith("/live_reports")
      ? { body: Number(sent.url.searchParams.get("offset") ?? 0) > 0 ? [] : history }
      : undefined;
  return stubbed({ live_reports: [] }, new Set(["anon"]), answer);
}

test("the history is read for the report's policy, with only the charts' figures", async () => {
  const { supabase, requests } = client();
  render(<ModelsTab client={supabase} report={REPORT} />);
  await screen.findByText("BLEND − B1, log loss per game");
  await vi.waitFor(() => expect(requests.some((r) => r.url.pathname.endsWith("/live_reports"))).toBe(true));
  const read = requests.find((r) => r.url.pathname.endsWith("/live_reports"));
  expect(read?.url.searchParams.get("policy_version")).toBe("eq.policy-v2");
  expect(read?.url.searchParams.get("select")).toContain("challengers:report->challengers->challengers");
});

test("a figure with its interval is charted; one without says about when it comes", async () => {
  const { supabase } = client();
  render(<ModelsTab client={supabase} report={REPORT} />);
  expect(await screen.findByText(/^-0\.0030 \[-0\.0130, \+0\.0070\] \(400 games, 5 weeks\)$/)).toBeTruthy();
  // B3 − B2 has two weeks: counts only, about two weeks after the report.
  expect(screen.getByText(/^66 games over 2 weeks: too few weeks/)).toBeTruthy();
  expect(screen.getAllByText(/The chart starts once the figure has its interval, about 2026-12-04/).length).toBeGreaterThan(0);
  // The blend's chart draws the two nights with an interval, not the first.
  const chart = await screen.findByRole("img", { name: /BLEND − B1, log loss per game, with its 95% interval/ });
  expect(chart).toBeTruthy();
});

test("each challenger's card: the control, an unfitted one's progress, a withheld one, and the timeline", async () => {
  const { supabase } = client();
  render(<ModelsTab client={supabase} report={REPORT} />);
  const control = await screen.findByRole("article", { name: "market_only/1" });
  expect(control.textContent).toContain("The control: no primary measure");
  expect(control.textContent).toContain("control");
  const unfitted = screen.getByRole("article", { name: "conservative_blend/1" });
  expect(unfitted.textContent).toContain("unfitted");
  expect(unfitted.textContent).toContain("77 of 200 training games: it predicts once fitted");
  expect(unfitted.querySelector('[role="progressbar"]')?.getAttribute("aria-valuenow")).toBe("77");
  expect(unfitted.textContent).toContain("Registered 2026-10-22 → intervals 4 weeks after it is fitted → formal review 2027-04-12");
  const withheld = screen.getByRole("article", { name: "team_residual_blend/1" });
  expect(withheld.textContent).toContain("Withheld: 1 game day without its output");
  const charted = screen.getByRole("article", { name: "b3_split/1" });
  expect(charted.textContent).toContain("Log loss against B3");
  expect(charted.textContent).toContain("intervals now");
  // The morning view in counts; the forecasters not in the report say so.
  expect(screen.getAllByText("Not in this report yet.")).toHaveLength(2);
});
