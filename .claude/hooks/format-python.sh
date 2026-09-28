#!/usr/bin/env bash
# .claude/hooks/format-python.sh
path=$(jq -r '.tool_input.file_path // empty')
[[ "$path" == *.py ]] || exit 0
cd "${CLAUDE_PROJECT_DIR:-.}" || exit 0
uv run ruff format "$path" >/dev/null
if ! out=$(uv run ruff check --fix "$path" 2>&1); then echo "$out" >&2; exit 2; fi
uv run ruff format "$path" >/dev/null                                      # tidy after fixes
exit 0
