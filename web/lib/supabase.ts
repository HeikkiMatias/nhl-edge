import { createClient, type SupabaseClient } from "@supabase/supabase-js";

let client: SupabaseClient | null = null;

/**
 * The browser's Supabase client, made on first use from the public URL and anon key (null when
 * they aren't set). It holds only the anon key: row-level security lets a signed-in dashboard
 * owner read the ledger and reports, and no one else anything.
 */
export function supabase(): SupabaseClient | null {
  const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
  const key = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;
  if (!url || !key) return null;
  client ??= createClient(url, key, { auth: { persistSession: true, flowType: "pkce" } });
  return client;
}
