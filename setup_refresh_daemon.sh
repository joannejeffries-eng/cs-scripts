#!/bin/bash
# One-time setup for the CS Slack-triggered daemons.
#
# Two daemons get installed as launchd agents:
#   1. com.juno.cs-refresh-daemon       — pulls Looker actuals on demand
#   2. com.juno.cs-role-change-daemon   — captures TL-posted role moves
#
# Both auto-start at every login and restart on crash.
#
# What this does:
#   - Writes your $STAFF_APP_LOOKER_POSTGRES_URL to a file the refresh
#     daemon reads (it doesn't inherit your shell env when launchd
#     starts it).
#   - Copies both launchd plists into ~/Library/LaunchAgents/.
#   - Loads + starts both daemons.
#
# Idempotent — re-running unloads + reloads each plist fresh.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
SECRETS_DIR="$HOME/.config/juno/claude-code"
URL_FILE="$SECRETS_DIR/looker-postgres-url"

DAEMONS=(
  "com.juno.cs-refresh-daemon"
  "com.juno.cs-role-change-daemon"
)

LOG_DIRS=(
  "$HOME/.claude/scheduled-tasks/refresh-daemon"
  "$HOME/.claude/scheduled-tasks/role-changes"
)

# ── 1. Save Postgres URL to a file the refresh daemon can read ────────────
if [[ ! -f "$URL_FILE" ]]; then
  if [[ -z "${STAFF_APP_LOOKER_POSTGRES_URL:-}" ]]; then
    echo "❌ STAFF_APP_LOOKER_POSTGRES_URL isn't set in this shell." >&2
    echo "   Open a fresh terminal where the env var is loaded, then re-run." >&2
    exit 1
  fi
  mkdir -p "$SECRETS_DIR"
  printf '%s' "$STAFF_APP_LOOKER_POSTGRES_URL" > "$URL_FILE"
  chmod 600 "$URL_FILE"
  echo "✅ Wrote $URL_FILE (chmod 600)"
else
  echo "✓ $URL_FILE already exists — leaving alone"
fi

# ── 2. Ensure log directories exist ───────────────────────────────────────
for d in "${LOG_DIRS[@]}"; do
  mkdir -p "$d"
done

# ── 3. Install + (re)load each daemon ─────────────────────────────────────
mkdir -p "$HOME/Library/LaunchAgents"

for label in "${DAEMONS[@]}"; do
  src="$REPO_DIR/${label}.plist"
  dst="$HOME/Library/LaunchAgents/${label}.plist"

  if [[ ! -f "$src" ]]; then
    echo "⚠️  Missing plist $src — skipping $label"
    continue
  fi

  cp "$src" "$dst"
  echo "✅ Installed $dst"

  if launchctl list | grep -q "$label"; then
    echo "  ↺ Unloading existing $label to reload fresh…"
    launchctl unload "$dst" 2>/dev/null || true
  fi
  launchctl load "$dst"
  echo "  ▶️  Loaded $label"
done

echo ""
echo "Test them:"
echo "  • Refresh:      post '🔄 refresh' in #dry-run-testing-jo"
echo "  • Role change:  post 'Maisha → triage' in #client-support-leads"
echo "                  (under today's anchor message — daemon posts it on first weekday poll)"
echo ""
echo "Logs:"
echo "  tail -f ~/.claude/scheduled-tasks/refresh-daemon/daemon.log"
echo "  tail -f ~/.claude/scheduled-tasks/role-changes/daemon.log"
echo ""
echo "To stop / uninstall later:"
for label in "${DAEMONS[@]}"; do
  echo "  launchctl unload ~/Library/LaunchAgents/${label}.plist"
done
