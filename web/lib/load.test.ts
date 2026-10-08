import assert from "node:assert/strict";

import { test } from "vitest";

import { load, PAGE } from "./load.ts";
import { type StoredReport, stubbed, type Tables } from "./testing.ts";

// Without a session, supabase-js sends the anon key as the caller's token.
const OWNER = new Set(["anon"]);
const NOBODY = new Set<string>();

const prediction = (game_date: string, game_id: number) => ({
  game_date,
  game_id,
  start_utc: `${game_date}T23:00:00+00:00`,
  home: "WSH",
  away: "PIT",
  status: "predicted",
  home_price: 1.9,
  away_price: 2.0,
  p_b1: 0.5,
  p_b3: 0.52,
  p_blend: 0.51,
  bet: false,
});

const bet = (game_date: string, game_id: number) => ({
  game_date,
  game_id,
  side: "home",
  price: 2.0,
  stake: 1.0,
});

const report = (as_of: string, policy_version: string): StoredReport => ({
  as_of,
  kind: "interim",
  policy_version,
  code_version: "live-report-20261008-abc1234",
  report: { as_of, policy_version, closing_value: { clv_per_bet: { games: 3, weeks: 1 } } },
});

const tables = (more: Tables = {}): Tables => ({
  predictions: [],
  paper_bets: [],
  live_reports: [],
  ...more,
});

const sentTo = (requests: { url: URL }[], table: string) =>
  requests.filter((r) => r.url.pathname === `/rest/v1/${table}`);

test("a signed-in user who isn't an owner reads nothing", async () => {
  const { supabase, requests } = stubbed(tables(), NOBODY);
  const data = await load(supabase);
  assert.equal(data.owner, false);
  assert.deepEqual([data.slate, data.bets, data.history, data.report], [[], [], [], null]);
  // Only the owner check: no table is asked for.
  assert.deepEqual(
    requests.map((r) => `${r.method} ${r.url.pathname}`),
    ["POST /rest/v1/rpc/is_dashboard_owner"],
  );
});

test("an owner reads the latest day's slate, the bets and the report, newest first", async () => {
  const { supabase, requests } = stubbed(
    tables({
      // PostgREST returns them ordered by game date, newest first.
      predictions: [
        prediction("2026-10-09", 3),
        prediction("2026-10-09", 4),
        prediction("2026-10-08", 1),
      ],
      paper_bets: [bet("2026-10-08", 1)],
      live_reports: [report("2026-10-09", "policy-b"), report("2026-10-08", "policy-a")],
    }),
    OWNER,
  );
  const data = await load(supabase);
  assert.equal(data.owner, true);
  assert.equal(data.day, "2026-10-09");
  assert.deepEqual(
    data.slate.map((r) => r.game_id),
    [3, 4],
  );
  assert.equal(data.bets.length, 1);
  assert.equal(data.report?.as_of, "2026-10-09");

  const [predictions] = sentTo(requests, "predictions");
  assert.equal(predictions.url.searchParams.get("order"), "game_date.desc,start_utc.asc");
  // A bet's result and profit are never read before the season's end.
  const [bets] = sentTo(requests, "paper_bets");
  const columns = bets.url.searchParams.get("select")?.split(",") ?? [];
  assert.ok(columns.includes("clv") && columns.includes("stake"));
  assert.ok(!columns.includes("won") && !columns.includes("profit"));
  assert.equal(bets.url.searchParams.get("order"), "game_date.desc,start_utc.asc,game_id.asc");
  const [latest, history] = sentTo(requests, "live_reports");
  assert.equal(latest.url.searchParams.get("order"), "as_of.desc");
  assert.equal(latest.url.searchParams.get("limit"), "1");
  assert.equal(
    history.url.searchParams.get("select"),
    "as_of,clv:report->closing_value->clv_per_bet",
  );
});

test("the CLV history is the latest report's policy only", async () => {
  // Codex on #197: a new policy restarts the CLV count, so an earlier policy's reports stay out.
  const { supabase, requests } = stubbed(
    tables({
      live_reports: [
        report("2026-10-10", "policy-b"),
        report("2026-10-09", "policy-b"),
        report("2026-10-08", "policy-a"),
      ],
    }),
    OWNER,
  );
  const data = await load(supabase);
  assert.deepEqual(
    data.history.map((r) => r.as_of),
    ["2026-10-10", "2026-10-09"],
  );
  assert.deepEqual(data.history[0].clv, { games: 3, weeks: 1 });
  const history = requests.find((r) => r.url.searchParams.get("select")?.includes("clv:"));
  assert.equal(history?.url.searchParams.get("policy_version"), "eq.policy-b");
  assert.equal(data.day, null);
});

test("every bet and every report of the policy are read, a page at a time", async () => {
  // Codex on #197: a season has about 500 bets and 180 nightly reports, and PostgREST caps a
  // response, so nothing is cut off at a fixed count.
  const days = Array.from({ length: PAGE + 300 }, (_, i) => {
    const day = new Date(Date.UTC(2026, 9, 7) + i * 86_400_000).toISOString().slice(0, 10);
    return day;
  }).reverse();
  const { supabase, requests } = stubbed(
    tables({
      paper_bets: days.flatMap((day, i) => [bet(day, 2 * i), bet(day, 2 * i + 1)]),
      live_reports: days.map((day) => report(day, "policy-a")),
    }),
    OWNER,
  );
  const data = await load(supabase);
  assert.equal(data.bets.length, 2 * days.length);
  assert.equal(data.history.length, days.length);
  assert.equal(data.history.at(-1)?.as_of, "2026-10-07");
  // The bets in three pages and an empty one; the history in two and an empty one.
  assert.equal(sentTo(requests, "paper_bets").length, 4);
  const offsets = sentTo(requests, "paper_bets").map((r) => r.url.searchParams.get("offset"));
  assert.deepEqual(offsets, ["0", "1000", "2000", "2600"]);
});

test("without a report there is no history to read", async () => {
  const { supabase, requests } = stubbed(tables(), OWNER);
  const data = await load(supabase);
  assert.equal(data.report, null);
  assert.deepEqual(data.history, []);
  assert.equal(sentTo(requests, "live_reports").length, 1);
});

test("a failed read is an error, not an empty dashboard", async () => {
  const { supabase } = stubbed(tables(), OWNER, (sent) =>
    sent.url.pathname.endsWith("/paper_bets")
      ? { status: 401, body: { code: "PGRST301", message: "JWT expired" } }
      : undefined,
  );
  await assert.rejects(load(supabase), { message: "JWT expired" });
});

test("a failed owner check is an error too", async () => {
  const { supabase } = stubbed(tables(), OWNER, () => ({
    status: 404,
    body: { code: "PGRST202", message: "Could not find the function public.is_dashboard_owner" },
  }));
  await assert.rejects(load(supabase), { message: /is_dashboard_owner/ });
});
