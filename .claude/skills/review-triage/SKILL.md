---
name: review-triage
description: Triage Codex review findings on a pull request under the review budget in CLAUDE.md - fix
  what qualifies, file follow-up issues, reply to every finding, and report merge readiness. Use when a
  Codex review lands on a PR.
argument-hint: "[PR number, default: the current branch's PR]"
---
Apply the review budget in CLAUDE.md to PR $ARGUMENTS (default: the PR for the current branch).

1. **Collect.** List Codex reviews with `gh api repos/{owner}/{repo}/pulls/<n>/reviews` to find the
   round, and top-level review comments with `gh api repos/{owner}/{repo}/pulls/<n>/comments`. A
   finding is unanswered when no comment has it as `in_reply_to_id`.
2. **Classify** each unanswered finding:

   | Finding | Decision |
   | --- | --- |
   | P0, or a hard-rule violation (leakage, settlement, de-vig, secrets, paid endpoints, protected paths), whatever its label | Fix |
   | P1 in lines this PR changed, small (a few lines, no redesign) | Fix |
   | P1 otherwise | Follow-up issue in the current milestone |
   | P2 and below | Fix in this PR. If it needs a redesign or lies outside the PR's diff, ask me first |
   | A guard bypass that needs deliberate effort | Won't fix |

   Show me the table (finding, label, decision, reason). Wait for my OK only when a P0 or a
   hard-rule finding would be answered "Won't fix", or a fix needs a redesign or reaches outside
   the PR's diff. Otherwise apply the decisions the budget dictates.
3. **Fix** the approved items in one commit, run
   `uv run ruff check . && uv run pyright src && uv run pytest -m "not slow" -q`, and push.
4. **File** follow-ups with `gh issue create --milestone <current> --label <area>`, linking the PR.
5. **Reply** to every finding with "Fixed in <sha>", "Follow-up #n" or "Won't fix: <reason>". Write each
   body to a file first and post it with
   `gh api repos/{owner}/{repo}/pulls/<n>/comments/<id>/replies -F body=@<file>`, since reply text often
   names paths the Bash guard blocks.
6. **Next round.** After round 1 with P0 or P1 fixes, request round 2 with
   `gh pr comment <n> --body "@codex review"`. P2-only fixes get a reply but no new round. After
   round 2, request another round only when a P0 was fixed.
7. **Report** readiness: CI (`gh pr checks <n>`), open P0s, unanswered findings, rounds used, follow-ups
   filed. When it is ready and its issue is done, merge it yourself with
   `gh pr merge <n> --squash --delete-branch` (CLAUDE.md, Workflow). A P0 you would answer
   "Won't fix", an ADR or a scope change goes to me first.
