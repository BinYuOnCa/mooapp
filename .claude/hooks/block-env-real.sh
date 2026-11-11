#!/usr/bin/env bash
# PreToolUse hook (Bash): block any command that selects the REAL trading env.
# Matches: --env real | --env=real | --env  real | --env "real" | --env 'real'
# Exit 2 = block the tool call; stderr is shown to Claude as the reason.
set -u

cmd="$(jq -r '.tool_input.command // empty' 2>/dev/null)"
[ -z "$cmd" ] && exit 0

if printf '%s' "$cmd" | grep -Eq -- "--env[=[:space:]]+[\"']?real"; then
  cat >&2 <<'EOF'
BLOCKED by .claude/hooks/block-env-real.sh (configuration lock):
this command selects the REAL trading environment (--env real).
Claude Code must never start REAL trading. Run REAL yourself in an
interactive terminal, where the human lock (password + confirmation code) applies.
EOF
  exit 2
fi
exit 0
