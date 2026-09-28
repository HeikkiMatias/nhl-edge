#!/usr/bin/env bash
# .claude/hooks/readonly-bash.sh
# Bash allowlist for the read-only reviewer subagents, so a reviewer cannot change what it reviews.
cmd=$(jq -r '.tool_input.command // empty')
deny() {
  echo "Blocked for read-only reviewers: $1. Allowed: reading files, git history, gh pr view/diff," \
       "uv run pytest, uv run pyright, uv run ruff check." >&2
  exit 2
}

# No command substitution, and no redirects except 2>&1 and >/dev/null
rest=$(echo "$cmd" | sed -E 's/[0-9]?>&[0-9]//g; s#[0-9]?>[[:space:]]*/dev/null##g')
echo "$rest" | grep -q '>' && deny "output redirect"
echo "$cmd" | grep -qE '\$\(|`' && deny "command substitution"

# Every command in a chain or pipeline must be on the allowlist
chain=${rest//&&/$'\n'}; chain=${chain//||/$'\n'}; chain=${chain//&/$'\n'}
chain=${chain//;/$'\n'}; chain=${chain//|/$'\n'}
set -f
while IFS= read -r part; do
  # shellcheck disable=SC2086
  set -- $part
  case "$1" in
    ''|cd|pwd|ls|cat|head|tail|wc|grep|rg|jq|sort|uniq|cut|diff|stat|file|tree|echo|printf) ;;
    basename|dirname|date|true) ;;
    find) echo "$part" | grep -qE -- '-delete|-exec|-ok|-fprint' && deny "find with actions" ;;
    git)
      case "$2" in
        diff|log|show|status|ls-files|rev-parse|blame|grep|merge-base) ;;
        *) deny "git $2" ;;
      esac ;;
    gh)
      [ "$2" = pr ] || deny "gh $2"
      case "$3" in view|diff|checks) ;; *) deny "gh pr $3" ;; esac ;;
    uv)
      [ "$2" = run ] || deny "uv $2"
      case "$3" in
        pytest|pyright) ;;
        ruff)
          [ "$4" = check ] || deny "ruff $4"
          echo "$part" | grep -q -- '--fix' && deny "ruff --fix" ;;
        *) deny "uv run $3" ;;
      esac ;;
    *) deny "$1" ;;
  esac
done <<< "$chain"
exit 0
