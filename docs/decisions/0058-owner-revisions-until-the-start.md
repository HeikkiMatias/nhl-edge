# 0058. The owner's own revisions of a real bet's entry, until its game starts

- Status: Accepted (by the owner, 2026-11-05), as written: a required note of up to 200 characters, and no revision from the game's start
- Date: 2026-11-05
- Amends: ADR 0039, item 7 (the records): an entry is still written once and never edited, and revisions are appended beside it.

## Context

**The owner (2026-11-05), on the dashboard's redesign (#398):** "make sure that all recordings or clicks have a way to revert or correct the inputs."

**Today's rule (#268, ADR 0039):**
- The form inserts one entry per alerted bet, never updated or deleted. The database refuses a second one.
- A mistake is corrected only at the weekly check against the venue's statement (`nhl real correct`). Those corrections are appended in R2 and applied in order.

**What goes wrong:**
- A typo in the stake or the bet id, or a "not placed" clicked by mistake, stands for up to a week.
- Until then the real record, the real bankroll and the daily 20% stop all read the wrong entry.
- Real stakes may start from 2026-11-16 (ADR 0039), so this matters soon.

## Options

1. **Keep the weekly check as the only correction.** Nothing changes in the record's rules, but a mistake stays wrong for days, and the stop rule reads it.
2. **Let the form edit or delete an entry.** The simplest change, but the record loses what was entered first and when: an edit after the result would be invisible.
3. **Append the owner's revisions until the game's start.** Each revision is a new row with its own time, author and note. The entry and every revision stay in the record, and revisions end at the start. After the start, the weekly check corrects as before.

## Decision

Option 3 (the owner, 2026-11-05).

1. **What a revision is:** a new, complete entry for the same bet, appended by an owner through the form with a short note saying why, such as "typo in the stake".
   - It is placed, with its price, stake, bet id and time, or not placed, with its reason.
   - Or it withdraws the entry ("undo"): the bet is then unrecorded again, and a later revision can record it afresh.
   - It never changes or removes an earlier row.
2. **Only before the game's start.** A revision at or after the start is refused, so no revision can follow the result. After the start, a mistake waits for the weekly check against the venue's statement, as now.
3. **The same rules as an entry:**
   - a placed bet's time is after its alert, before the start, and not in the future;
   - the database sets the revision's time and author;
   - a revision needs an entry from the form to revise. Kalshi's automatic entries (ADR 0040) are the pipeline's, from their fills, and are not revisable from the form.
4. **The record:**
   - `nhl real sync` copies each revision to R2 once (`real/live/revisions/`), as it copies entries.
   - The real record applies the entry, then its revisions in order, then the weekly check's corrections in order.
   - The real bets table counts the revisions beside the corrections.
   - The bet's lateness is still its first entry's.
5. **The dashboard** shows each entry's history under it (when entered, each revision with its note, each correction) and offers "Correct" and "Undo" until the start.

## Backtest evidence

None: a change to the real record's bookkeeping, not to the model or the policy. The frozen policy, the paper ledger and every verdict are unchanged.

## Consequences

- **A mistake can be put right at once,** and the real bankroll and the stop rule then read the right entry.
- **Nothing is lost:** what was entered first, and when, stays in the record. The weekly reconciliation can see every revision.
- **Work, in #398's step 4:**
  - a migration (`real_entry_revisions`, owner inserts only, with the rules above in a trigger);
  - the sync and the record's `bets()`;
  - the lake's `real_bets` schema (a revisions count);
  - the form's Correct and Undo, and the history;
  - tests for each rule.
- **The weekly check is unchanged** and still the only correction after the start.

## Revisit when

- A revision is ever needed after a start that the weekly check can't serve.
- An automated venue (ADR 0040) takes over every real bet, leaving the form unused.
