// The simulator's figures (#398) on SYNTHETIC rows: the reads under row-level security, the
// totals and running lines, and when an interval can appear.
import { expect, test } from "vitest";

import { quoteDays, quoteReports } from "./fixtures/quotes.ts";
import {
  cents,
  daysOf,
  dollars,
  HOLDS_RECORD,
  intervalsFrom,
  loadQuotes,
  RECORD,
  running,
  totals,
} from "./quotes.ts";
import { stubbed } from "./testing.ts";

// Without a session the client sends the anon key as its token: here an owner's.
const OWNER = new Set(["anon"]);

test("an owner reads every day and each record's latest night", async () => {
  const { supabase, requests } = stubbed(
    { quote_days: quoteDays(), quote_reports: quoteReports() },
    OWNER,
  );
  const quotes = await loadQuotes(supabase);
  expect(quotes.applied).toBe(true);
  expect(quotes.days.length).toBe(quoteDays().length);
  expect(quotes.report?.record).toBe(RECORD);
  expect(quotes.holds?.record).toBe(HOLDS_RECORD);
  const reads = requests.map((r) => r.url.searchParams.get("record")).filter(Boolean);
  expect(reads.sort()).toEqual([`eq.${RECORD}`, `eq.${HOLDS_RECORD}`]);
  // Only the columns the dashboard shows are asked for.
  const days = requests.find((r) => r.url.pathname.endsWith("/quote_days"));
  expect(days?.url.searchParams.get("select")).not.toContain("*");
});

test("a non-owner reads nothing, and a project without the migration says so", async () => {
  const stranger = stubbed({ quote_days: quoteDays(), quote_reports: quoteReports() }, new Set());
  expect(await loadQuotes(stranger.supabase)).toEqual({
    applied: true,
    days: [],
    report: null,
    holds: null,
  });
  const before = stubbed({}, OWNER);
  expect((await loadQuotes(before.supabase)).applied).toBe(false);
  // Any other failure is the reader's to see.
  const failing = stubbed({ quote_days: [] }, OWNER, () => ({
    status: 401,
    body: { code: "PGRST301", message: "JWT expired" },
  }));
  await expect(loadQuotes(failing.supabase)).rejects.toMatchObject({ message: "JWT expired" });
});

test("a variant's totals and running lines", () => {
  const days = daysOf(quoteDays(), "model-1c");
  expect(days.map((d) => d.game_date)).toEqual(["2026-10-31", "2026-11-01", "2026-11-02"]);
  const t = totals(days);
  expect(t).toMatchObject({ days: 3, games: 12, fills: 6, contracts: 60, unsettledDays: 1 });
  expect(t.profit).toBe(-1);
  expect(t.twoSided).toBe(0.25);
  expect(t.closeShare).toBe(0.5);
  expect(t.edgeFair).toBeCloseTo(0.9);
  const r = running(days);
  // The paper profit stops at the first day awaiting its results; the expected lines go on.
  expect(r.profit).toEqual([2, -1, null]);
  expect(r.fair.map((v) => Number(v.toFixed(2)))).toEqual([0.3, 0.6, 0.9]);
  expect(r.close.map((v) => Number(v.toFixed(2)))).toEqual([-0.2, -0.4, -0.6]);
  // The hold rules' days are their own record's.
  expect(daysOf(quoteDays(), "model-1c-trust", HOLDS_RECORD).length).toBe(1);
  expect(daysOf(quoteDays(), "model-1c-trust").length).toBe(0);
});

test("an interval can appear from the fourth week of the first day's", () => {
  // 2026-10-31 is a Saturday: its week starts on 10-26, the fourth on 11-16.
  expect(intervalsFrom("2026-10-31")).toBe("2026-11-16");
  // The hold rules from Wednesday 11-04: the fourth week starts on 11-23 (#385).
  expect(intervalsFrom("2026-11-04")).toBe("2026-11-23");
  expect(intervalsFrom("2026-11-02")).toBe("2026-11-23");
});

test("dollars and cents read as signed figures", () => {
  expect(dollars(2)).toBe("$2.00");
  expect(dollars(2, true)).toBe("+$2.00");
  expect(dollars(-0.5, true)).toBe("−$0.50");
  expect(dollars(null)).toBe("–");
  expect(cents(0.015)).toBe("+1.5¢");
  expect(cents(-0.012)).toBe("−1.2¢");
  expect(cents(0)).toBe("0.0¢");
});
