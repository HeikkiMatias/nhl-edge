// @vitest-environment jsdom
// Market making › Results (#398) on SYNTHETIC quote tables: the tiles, the luck-against-edge
// lines, the variant picker and table, counts only before 4 weeks and intervals after, the
// hold rules and the requote record.
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";

import { quoteDays, quoteReports } from "@/lib/fixtures/quotes";
import { stubbed } from "@/lib/testing";

import { MarketResults } from "./market";

afterEach(cleanup);

// Without a session the client sends the anon key as its token: here an owner's.
function client(withIntervals = false, owner = true) {
  return stubbed(
    { quote_days: quoteDays(), quote_reports: quoteReports(withIntervals) },
    new Set(owner ? ["anon"] : []),
  ).supabase;
}

test("the chosen variant's totals, its lines and its days", async () => {
  render(<MarketResults client={client()} />);
  await screen.findByText("Our model at 1¢ below: so far");
  expect(screen.getByText(/Paper only/)).toBeTruthy();
  expect(screen.getByText(/game days 2026-10-31 to 2026-11-02/)).toBeTruthy();
  // The intro and the per-contract card both say when intervals can appear.
  expect(screen.getAllByText(/from about game date 2026-11-16/).length).toBe(2);
  const tile = (label: string) =>
    [...document.querySelectorAll(".tile")]
      .find((t) => t.querySelector(".label")?.textContent === label)
      ?.querySelector(".value")?.textContent;
  expect(tile("Game days")).toBe("3");
  expect(screen.getByText("12 games, Oct 31 to Nov 2")).toBeTruthy();
  expect(tile("Fills")).toBe("6");
  expect(tile("Two-sided")).toBe("50.0%");
  expect(tile("Paper profit")).toBe("−$1.00");
  expect(tile("Expected by our fair value")).toBe("+$0.90");
  expect(tile("Expected by Pinnacle's close")).toBe("−$0.60");
  expect(screen.getByText(/1 day awaiting results/)).toBeTruthy();
  expect(screen.getByText("after the fee, on 50.0% of contracts")).toBeTruthy();
  // Three lines, each named in the legend with its latest value.
  const chart = screen.getByRole("img", { name: /Running paper profit against the profit expected/ });
  expect(chart.getAttribute("aria-label")).toContain("Paper profit −$1.00");
  const legend = chart.parentElement?.querySelector(".legend") as HTMLElement;
  expect(within(legend).getByText(/Expected by Pinnacle's close \(50.0% of contracts\)/)).toBeTruthy();
  // The settled days as signed columns: a gain and a loss.
  const columns = screen.getByRole("img", { name: /Paper profit per settled game day/ });
  expect(columns.querySelectorAll(".column.gain").length).toBe(1);
  expect(columns.querySelectorAll(".column.loss").length).toBe(1);
});

test("the picker and the variant table choose what is shown", async () => {
  render(<MarketResults client={client()} />);
  await screen.findByText("Our model at 1¢ below: so far");
  // Only the fair values with days are offered.
  const fairs = screen.getByRole("radiogroup", { name: "Fair value" });
  expect(within(fairs).getAllByRole("radio").map((r) => r.textContent)).toEqual([
    "Our model",
    "Pinnacle",
  ]);
  fireEvent.click(within(fairs).getByRole("radio", { name: "Pinnacle" }));
  expect(screen.getByText("Pinnacle at 1¢ below: so far")).toBeTruthy();
  fireEvent.click(screen.getByRole("radio", { name: "2¢" }));
  expect(screen.getByText("Pinnacle at 2¢ below: so far")).toBeTruthy();
  // A row of the table picks its variant.
  fireEvent.click(screen.getByRole("button", { name: "Our model, 0¢" }));
  expect(screen.getByText("Our model at 0¢ below: so far")).toBeTruthy();
  const row = screen.getByRole("button", { name: "Our model, 0¢" }).closest("tr");
  expect(row?.className).toBe("selected");
});

test("per contract: counts only under 4 weeks, then each figure with its interval", async () => {
  const { unmount } = render(<MarketResults client={client()} />);
  await screen.findByText(/Counts only until the fills span 4 weeks/);
  expect(screen.getAllByText("6 fills over 1 week: counts only").length).toBe(5);
  expect(screen.queryByText(/\[/)).toBeNull();
  unmount();
  render(<MarketResults client={client(true)} />);
  await screen.findByText("Our model at 1¢ below: so far");
  expect(screen.getAllByText("+1.5¢ [+1.0¢, +2.0¢]").length).toBe(5);
  expect(screen.getByText("−1.2¢ [−3.0¢, +0.4¢]")).toBeTruthy();
  expect(screen.getByText(/The scale runs from −4.0¢ to \+4.0¢/)).toBeTruthy();
});

test("the hold rules: wait is the baseline, the others against it", async () => {
  render(<MarketResults client={client(true)} />);
  const card = (await screen.findByText("Hold rules after a pull")).closest(".card") as HTMLElement;
  const table = within(card).getByText("Profit per game less wait's").closest("table") as HTMLElement;
  const trust = within(table)
    .getAllByText("Never pull (trust)")
    .map((c) => c.closest("tr") as HTMLElement)
    .find((r) => r.className === "primary") as HTMLElement;
  expect(trust.textContent).toContain("−$1.00");
  expect(trust.textContent).toContain("-0.40 [-0.90, +0.10] (50 games, 4 weeks)");
  const cool = within(table)
    .getAllByText("15-minute cool-off")
    .map((c) => c.closest("tr") as HTMLElement)
    .find((r) => r.className === "primary") as HTMLElement;
  expect(cool.textContent).toContain("4 games over 1 weeks: too few weeks");
  expect(within(table).getAllByText("the baseline").length).toBe(3);
});

test("the requote record: wait is the baseline, requote against it, and the changes", async () => {
  render(<MarketResults client={client(true)} />);
  const card = (await screen.findByText("Requote after a starter change")).closest(
    ".card",
  ) as HTMLElement;
  const rows = within(card)
    .getAllByText("Requote with the new starter")
    .map((c) => c.closest("tr") as HTMLElement);
  expect(rows.length).toBe(3);
  const primary = rows.find((r) => r.className === "primary") as HTMLElement;
  expect(primary.textContent).toContain("150");
  expect(primary.textContent).toContain("+$1.50");
  expect(primary.textContent).toContain("+0.30 [-0.20, +0.80] (80 games, 4 weeks)");
  expect(within(card).getAllByText("the baseline").length).toBe(3);
  expect(
    within(card).getByText(
      "5 starter changes: 2 requoted, 3 held (2 too close to the start; 1 a starter call withdrawn).",
    ),
  ).toBeTruthy();
  expect(within(card).getByText(/from about game date 2026-11-30/)).toBeTruthy();
});

test("before its first day is copied, the requote card says what it waits for", async () => {
  const days = quoteDays().filter((d) => d.record !== "kalshi_quotes/requote/1");
  render(
    <MarketResults
      client={stubbed({ quote_days: days, quote_reports: quoteReports() }, new Set(["anon"])).supabase}
    />,
  );
  const card = (await screen.findByText("Requote after a starter change")).closest(
    ".card",
  ) as HTMLElement;
  expect(within(card).getByText(/No requote day copied yet/).textContent).toContain(
    "20261108120000_quote_requote.sql",
  );
  // The other records are shown as before.
  expect(screen.getByText("Hold rules after a pull")).toBeTruthy();
});

test("a reader without the owner's rows sees no figure", async () => {
  render(<MarketResults client={client(false, false)} />);
  expect(await screen.findByText(/No simulator day copied yet/)).toBeTruthy();
  expect(screen.queryByText(/so far/)).toBeNull();
});
