#!/usr/bin/env bash
# .claude/hooks/stop-checks.sh
# Keeps Claude working while fast tests or pyright fail on changed Python code. Gives up after two
# consecutive blocks in a session, so a failure that cannot be fixed does not loop forever.
input=$(cat)
cd "${CLAUDE_PROJECT_DIR:-.}" || exit 0
session=$(echo "$input" | jq -r '.session_id // "unknown"')
counter="${TMPDIR:-/tmp}/nhl-edge-stop-blocks-$session"

python_changed() {
  git rev-parse --verify -q HEAD >/dev/null || return 0                    # no commits yet
  git diff --quiet HEAD -- '*.py' || return 0                              # tracked changes
  [ -n "$(git ls-files --others --exclude-standard -- '*.py')" ]           # new untracked files
}

block() {
  blocks=$(( $(cat "$counter" 2>/dev/null || echo 0) + 1 ))
  if [ "$blocks" -gt 2 ]; then
    rm -f "$counter"
    echo "stop-checks: still failing after 2 attempts; letting the turn end. Fix before committing." >&2
    exit 0
  fi
  echo "$blocks" > "$counter"
  echo "$1" | tail -30 >&2
  exit 2
}

python_changed || { rm -f "$counter"; exit 0; }                            # nothing changed
out=$(uv run pytest -m "not slow" -q -x 2>&1) || block "$out"
out=$(uv run pyright src 2>&1) || block "$out"
rm -f "$counter"
exit 0
