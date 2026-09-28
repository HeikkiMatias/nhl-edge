#!/usr/bin/env bash
# .claude/hooks/session-context.sh
# SessionStart: print what's next from GitHub. Tasks are issues; the current milestone is the
# earliest one with open issues. Prints nothing when offline or when gh is unavailable.
cd "${CLAUDE_PROJECT_DIR:-.}" || exit 0
command -v gh >/dev/null || exit 0
issues=$(gh issue list --state open --limit 100 --json number,title,milestone,labels 2>/dev/null) \
  || exit 0
prs=$(gh pr list --state open --json number,title,headRefName 2>/dev/null) || prs='[]'

echo "$issues" | jq -r '
  map(select(.milestone != null) | .priority = ([.labels[].name] | index("priority") != null))
  | sort_by(.milestone.title) as $open
  | if ($open | length) == 0 then "No open milestone issues."
    else $open[0].milestone.title as $m
      | "Current milestone: \($m). Open tasks, priority first:",
        ($open | map(select(.milestone.title == $m)) | sort_by((.priority | not), .number)[]
          | "  #\(.number) \(.title)\(if .priority then " [priority]" else "" end)")
    end'
echo "$prs" | jq -r '
  if length > 0 then "Open PRs:", (.[] | "  #\(.number) \(.title) (\(.headRefName))")
  else empty end'
exit 0
