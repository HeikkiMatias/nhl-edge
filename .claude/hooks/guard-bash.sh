#!/usr/bin/env bash
# .claude/hooks/guard-bash.sh
# Matches on the command text: it stops accidents, not deliberate workarounds.
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
# Secrets: any env file except the committed template. Code loads them itself.
if echo "$cmd" | sed 's/\.env\.example//g' | grep -qE '(^|[^[:alnum:]_.])\.env'; then
  echo "Blocked: .env files hold secrets. Load them inside code, never through a command." >&2
  exit 2
fi
# Protected paths: writes that name data/raw/ or tests/golden/. Ingest code writing its own
# cache is fine, since its paths are not in the command. 2>&1 and >/dev/null are not writes.
writes=$(echo "$cmd" | sed -E 's/[0-9]?>&[0-9]//g; s#[0-9]?>[[:space:]]*/dev/null##g')
write_ops='>|(^|[^[:alnum:]_-])(rm|mv|cp|tee|touch|truncate|ln|dd|rsync|unlink|chmod|install)([[:space:]]|$)'
write_ops+='|sed[^|;&]*[[:space:]]-[[:alpha:]]*i|-delete|git[[:space:]]+(rm|mv|checkout|restore|clean)'
if echo "$cmd" | grep -qE 'data/raw|tests/golden' && echo "$writes" | grep -qE "$write_ops"; then
  echo "Blocked: data/raw/ and tests/golden/ are protected. Change raw data and golden fixtures by hand only." >&2
  exit 2
fi
exit 0
