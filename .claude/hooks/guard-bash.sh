#!/usr/bin/env bash
# .claude/hooks/guard-bash.sh
cmd=$(jq -r '.tool_input.command // empty')
if echo "$cmd" | grep -qE 'the-odds-api\.com/v4/historical|nhl odds backfill' \
   && [ "${ALLOW_PAID_ODDS:-0}" != "1" ]; then
  echo "Blocked: paid historical odds call. Start Claude with ALLOW_PAID_ODDS=1 to allow." >&2
  exit 2
fi
if echo "$cmd" | grep -qE 'rm -rf|git reset --hard|git push (-f|--force)'; then
  echo "Blocked: destructive command." >&2
  exit 2
fi
exit 0
