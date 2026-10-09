// Test support: the real supabase-js client over a fetch that answers as PostgREST would, so the
// tests see the requests the dashboard sends and how it reads the answers. Only tests import it.
import { createClient, type SupabaseClient } from "@supabase/supabase-js";

export const SUPABASE_URL = "https://abcdefghijklmnop.supabase.co";
let clients = 0;

export type Answer = { status?: number; body: unknown };
export type Sent = { method: string; url: URL; token: string | null };
/** A stored live_reports row, as the tests give them. */
export type StoredReport = {
  as_of: string;
  kind: string;
  policy_version: string;
  code_version: string;
  report: { closing_value: { clv_per_bet: unknown } } & Record<string, unknown>;
};
export type Tables = {
  predictions?: unknown[];
  paper_bets?: unknown[];
  live_reports?: StoredReport[];
  games?: unknown[];
};

/**
 * A client whose requests reach `tables`, newest first as the dashboard orders them, read under
 * row-level security: the RPC is_dashboard_owner says whether the caller's token is an owner's,
 * and a table gives a non-owner no rows. `offset` and `limit` page the rows, and live_reports
 * keeps its `policy_version=eq.` filter and its CLV projection. `answer` can replace any reply.
 */
export function stubbed(
  tables: Tables,
  owners: ReadonlySet<string>,
  answer?: (sent: Sent) => Answer | undefined,
): { supabase: SupabaseClient; requests: Sent[] } {
  const requests: Sent[] = [];
  const fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(input instanceof Request ? input.url : String(input));
    const method = init?.method ?? (input instanceof Request ? input.method : "GET");
    const bearer = new Headers(init?.headers).get("authorization");
    const sent = { method, url, token: bearer?.replace(/^Bearer /, "") ?? null };
    requests.push(sent);
    const reply = answer?.(sent) ?? postgrest(tables, owners, sent);
    return new Response(JSON.stringify(reply.body), {
      status: reply.status ?? 200,
      headers: { "content-type": "application/json" },
    });
  };
  const supabase = createClient(SUPABASE_URL, "anon", {
    global: { fetch },
    auth: {
      persistSession: false,
      autoRefreshToken: false,
      detectSessionInUrl: false,
      // Each test's client keeps its own auth state.
      storageKey: `test-${++clients}`,
    },
  });
  return { supabase, requests };
}

function postgrest(tables: Tables, owners: ReadonlySet<string>, sent: Sent): Answer {
  const path = sent.url.pathname.replace("/rest/v1/", "");
  const owner = sent.token !== null && owners.has(sent.token);
  if (path === "rpc/is_dashboard_owner" && sent.method === "POST") return { body: owner };
  if (!(path in tables) || sent.method !== "GET") {
    return { status: 404, body: { code: "PGRST205", message: `no table ${path}` } };
  }
  let rows = owner ? [...(tables[path as keyof Tables] ?? [])] : [];
  const params = sent.url.searchParams;
  const policy = params.get("policy_version");
  if (policy) {
    rows = (rows as StoredReport[]).filter((r) => `eq.${r.policy_version}` === policy);
  }
  if (params.get("select")?.includes("clv:")) {
    rows = (rows as StoredReport[]).map((r) => ({
      as_of: r.as_of,
      clv: r.report.closing_value.clv_per_bet,
    }));
  }
  const offset = Number(params.get("offset") ?? 0);
  const limit = params.has("limit") ? Number(params.get("limit")) : rows.length;
  return { body: rows.slice(offset, offset + limit) };
}
