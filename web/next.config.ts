import type { NextConfig } from "next";

// A client-rendered dashboard: it holds only Supabase's public anon key, and row-level security
// decides what a signed-in user may read. No server-side secret exists to leak.
const config: NextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
};

export default config;
