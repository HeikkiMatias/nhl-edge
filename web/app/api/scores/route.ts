// The scoreboard for the dashboard's live scores (#211): a relay of the NHL's public
// api-web.nhle.com/v1/score/{date}, which a browser can't call itself (no CORS header). It holds no
// secret and reads no Supabase data. Vercel's CDN keeps each answer for CACHE_SECONDS, so however
// many pages are open, the NHL gets about one request per date in that time (docs/data-sources.md).

import { easternDate } from "@/lib/format";
import { CACHE_SECONDS, dateAllowed, NHL_SCORES, parseScores } from "@/lib/live";

export async function GET(request: Request): Promise<Response> {
  const date = new URL(request.url).searchParams.get("date") ?? "";
  const now = new Date();
  if (!dateAllowed(date, easternDate(now))) {
    return Response.json({ error: "date must be YYYY-MM-DD within two days of today" }, { status: 400 });
  }
  let answer: Response;
  try {
    answer = await fetch(`${NHL_SCORES}/${date}`, { cache: "no-store" });
  } catch (e) {
    return Response.json({ error: `NHL API unreachable: ${(e as Error).message}` }, { status: 502 });
  }
  if (!answer.ok) {
    return Response.json({ error: `NHL API answered ${answer.status}` }, { status: 502 });
  }
  return Response.json(parseScores(date, await answer.json(), now), {
    headers: {
      "Cache-Control": `public, max-age=0, s-maxage=${CACHE_SECONDS}, stale-while-revalidate=${CACHE_SECONDS}`,
    },
  });
}
