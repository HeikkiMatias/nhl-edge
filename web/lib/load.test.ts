import assert from "node:assert/strict";
import { test } from "node:test";

import { createClient } from "@supabase/supabase-js";

import { load } from "./load.ts";

// The real supabase-js client, with fetch answering as PostgREST would: so the tests see the
// requests the dashboard sends (tables, projections, filters) and how it reads the answers.
const URL = "https://abcdefghijklmnop.supabase.co";

type Route = (url: URL, method: string) => { status?: number; body: unknown } | undefined;

function client(route: Route) {
  const requests: { method: string; url: URL }[] = [];
  const fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new globalThis.URL(input instanceof Request ? input.url : String(input));
    const method = init?.method ?? (input instanceof Request ? input.method : "GET");
    requests.push({ method, url });
    const answer = route(url, method);
    if (!answer) return new Response(JSON.stringify({ message: "not found" }), { status: 404 });
    return new Response(JSON.stringify(answer.body), {
      status: answer.status ?? 200,
      headers: { "content-type": "application/json" },
    });
  };
  const supabase = createClient(URL, "anon", {
    global: { fetch },
    auth: { persistSession: false, autoRefreshToken: false, detectSessionInUrl: false },
  });
  return { supabase, requests };
}

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

const report = (as_of: string, policy_version: string) => ({
  as_of,
  kind: "interim",
  policy_version,
  code_version: "live-report-20261008-abc1234",
  report: { as_of, policy_version, closing_value: { clv_per_bet: { games: 3, weeks: 1 } } },
});

function owner(isOwner: boolean, tables: Record<string, unknown[]>): Route {
  return (url, method) => {
    const path = url.pathname.replace("/rest/v1/", "");
    if (path === "rpc/is_dashboard_owner" && method === "POST") return { body: isOwner };
    if (path === "live_reports" && url.searchParams.get("select")?.includes("clv:")) {
      const policy = url.searchParams.get("policy_version")?.replace("eq.", "");
      const rows = (tables.live_reports as ReturnType<typeof report>[])
        .filter((r) => r.policy_version === policy)
        .map((r) => ({ as_of: r.as_of, clv: r.report.closing_value.clv_per_bet }));
      return { body: rows };
    }
    if (path in tables && method === "GET") {
      const rows = tables[path];
      return { body: path === "live_reports" ? rows.slice(0, 1) : rows };
    }
    return undefined;
  };
}

test("a signed-in user who isn't an owner reads nothing", async () => {
  const { supabase, requests } = client(owner(false, {}));
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
  const { supabase, requests } = client(
    owner(true, {
      // PostgREST returns them ordered by game date, newest first.
      predictions: [
        prediction("2026-10-09", 3),
        prediction("2026-10-09", 4),
        prediction("2026-10-08", 1),
      ],
      paper_bets: [{ game_date: "2026-10-08", game_id: 1, side: "home", price: 2.0, stake: 1.0 }],
      live_reports: [report("2026-10-09", "policy-b"), report("2026-10-08", "policy-a")],
    }),
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

  const sent = (table: string) => requests.filter((r) => r.url.pathname === `/rest/v1/${table}`);
  const [predictions] = sent("predictions");
  assert.equal(predictions.url.searchParams.get("order"), "game_date.desc,start_utc.asc");
  // A bet's result and profit are never read before the season's end.
  const [bets] = sent("paper_bets");
  const columns = bets.url.searchParams.get("select")?.split(",") ?? [];
  assert.ok(columns.includes("clv") && columns.includes("stake"));
  assert.ok(!columns.includes("won") && !columns.includes("profit"));
  const [latest, history] = sent("live_reports");
  assert.equal(latest.url.searchParams.get("order"), "as_of.desc");
  assert.equal(latest.url.searchParams.get("limit"), "1");
  assert.equal(
    history.url.searchParams.get("select"),
    "as_of,clv:report->closing_value->clv_per_bet",
  );
});

test("the CLV history is the latest report's policy only", async () => {
  // Codex on #197: a new policy restarts the CLV count, so an earlier policy's reports stay out.
  const { supabase, requests } = client(
    owner(true, {
      predictions: [],
      paper_bets: [],
      live_reports: [
        report("2026-10-10", "policy-b"),
        report("2026-10-09", "policy-b"),
        report("2026-10-08", "policy-a"),
      ],
    }),
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

test("without a report there is no history to read", async () => {
  const { supabase, requests } = client(
    owner(true, { predictions: [], paper_bets: [], live_reports: [] }),
  );
  const data = await load(supabase);
  assert.equal(data.report, null);
  assert.deepEqual(data.history, []);
  assert.equal(requests.filter((r) => r.url.pathname === "/rest/v1/live_reports").length, 1);
});

test("a failed read is an error, not an empty dashboard", async () => {
  const route = owner(true, { predictions: [], live_reports: [] });
  const { supabase } = client((url, method) =>
    url.pathname.endsWith("/paper_bets")
      ? { status: 401, body: { code: "PGRST301", message: "JWT expired" } }
      : route(url, method),
  );
  await assert.rejects(load(supabase), { message: "JWT expired" });
});

test("a failed owner check is an error too", async () => {
  const { supabase } = client(() => ({
    status: 404,
    body: { code: "PGRST202", message: "Could not find the function public.is_dashboard_owner" },
  }));
  await assert.rejects(load(supabase), { message: /is_dashboard_owner/ });
});
