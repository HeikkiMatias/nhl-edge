---
name: adr
description: Write an architecture decision record for a modeling or design choice into docs/decisions/.
  Use whenever a modeling choice is made, changed or reversed.
argument-hint: "[decision title]"
---
Write a decision record for: $ARGUMENTS

1. Find the highest number in docs/decisions/ and use the next one, zero-padded to four digits. Name the
   file NNNN-short-kebab-title.md.
2. Fill in [template.md](template.md): context, options, decision, backtest evidence, consequences, and
   what would reopen it. Keep it to one page.
3. Backtest evidence must quote intervals from reports/backtest/, never point estimates alone. If no
   backtest exists yet, write "None yet" and name the run that would provide it.
4. If the decision supersedes an earlier ADR, set that ADR's status to "Superseded by NNNN".
5. If the decision changes a hard rule, update CLAUDE.md and the review guidelines in AGENTS.md together.
6. Show me the ADR and wait for my confirmation before marking it Accepted.
