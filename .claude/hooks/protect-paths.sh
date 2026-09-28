#!/usr/bin/env bash
# .claude/hooks/protect-paths.sh
path=$(jq -r '.tool_input.file_path // empty')
case "$path" in
  */.env.example|.env.example) exit 0 ;;                                   # the committed template
  */data/raw/*|data/raw/*|*/tests/golden/*|tests/golden/*|*/.env|.env|*/.env.*|.env.*)
    echo "Blocked: $path is protected. Change raw data and golden fixtures by hand only." >&2
    exit 2 ;;
esac
exit 0
