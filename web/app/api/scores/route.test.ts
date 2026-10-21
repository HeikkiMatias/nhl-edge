// The scoreboard route: it relays the NHL's answer for an allowed date, parsed and cacheable for
// CACHE_SECONDS, and answers a bad date or an NHL failure with an error rather than a board.
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import finals from "@/lib/fixtures/score-2026-10-08.json";
import type { Scores } from "@/lib/live";

import { GET } from "./route";

const nhl = vi.fn<typeof fetch>();

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-10-09T10:00:00Z")); // 06:00 ET on 2026-10-09
  vi.stubGlobal("fetch", nhl);
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  nhl.mockReset();
});

const ask = (date: string) => GET(new Request(`https://dashboard.example/api/scores?date=${date}`));

test("an allowed date gets the NHL's scoreboard, parsed and cacheable", async () => {
  nhl.mockResolvedValue(Response.json(finals));
  const answer = await ask("2026-10-08");
  expect(answer.status).toBe(200);
  expect(nhl).toHaveBeenCalledWith("https://api-web.nhle.com/v1/score/2026-10-08", {
    cache: "no-store",
  });
  expect(answer.headers.get("Cache-Control")).toContain("s-maxage=20");
  const body = (await answer.json()) as Scores;
  expect(body.date).toBe("2026-10-08");
  expect(body.games).toHaveLength(10);
  expect(body.games[0]).not.toHaveProperty("tvBroadcasts");
});

test("a date that isn't real, or far from today, is refused without asking the NHL", async () => {
  for (const date of ["2026-10-20", "yesterday", "2026-10-09%2F..%2Fplayer"]) {
    const answer = await ask(date);
    expect(answer.status).toBe(400);
  }
  expect(nhl).not.toHaveBeenCalled();
});

test("an NHL failure is an error, never an empty board", async () => {
  nhl.mockResolvedValueOnce(new Response("busy", { status: 503 }));
  let answer = await ask("2026-10-09");
  expect(answer.status).toBe(502);
  expect(await answer.json()).toEqual({ error: "NHL API answered 503" });
  nhl.mockRejectedValueOnce(new TypeError("fetch failed"));
  answer = await ask("2026-10-09");
  expect(answer.status).toBe(502);
  expect(answer.headers.get("Cache-Control")).toBeNull();
});
