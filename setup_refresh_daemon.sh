#!/bin/bash
# One-time setup for the Slack-triggered refresh daemon.
#
# What this does:
#   1. Writes your current $STAFF_APP_LOOKER_POSTGRES_URL to a file the
#      daemon reads (it doesn't inherit your shell env when launchd
#      starts it).
#   2. Copies the launchd plist into ~/Library/LaunchAgents/.
#   3. Loads + starts the daemon.
#
# Run once. To stop/uninstall later see the comment at the bottom.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
SECRETS_DIR="$HOME/.config/juno/claude-code"
URL_FILE="$SECRETS_DIR/looker-postgres-url"
PLIST_SRC="$REPO_DIR/com.juno.cs-refresh-daemon.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.juno.cs-refresh-daemon.plist"
LOG_DIR="$HOME/.claude/scheduled-tasks/refresh-daemon"

# ── 1. Save Postgres URL to a file the daemon can read ────────────────────
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

# ── 2. Ensure log directory exists ────────────────────────────────────────
mkdir -p "$LOG_DIR"

# ── 3. Install plist ──────────────────────────────────────────────────────
mkdir -p "$HOME/Library/LaunchAgents"
cp "$PLIST_SRC" "$PLIST_DST"
echo "✅ Installed plist → $PLIST_DST"

# ── 4. Load / start (idempotent — unload first if already loaded) ─────────
if launchctl list | grep -q com.juno.cs-refresh-daemon; then
  echo "↺ Unloading existing daemon to reload fresh…"
  launchctl unload "$PLIST_DST" 2>/dev/null || true
fi
launchctl load "$PLIST_DST"
echo "✅ Daemon loaded and running."
echo ""
echo "Test it:"
echo "  1. Post '🔄 refresh' in #dry-run-testing-jo"
echo "  2. Within ~30 s the daemon should react ✅ and post a thread reply"
echo ""
echo "Logs:"
echo "  tail -f $LOG_DIR/daemon.log"
echo ""
echo "To stop / uninstall later:"
echo "  launchctl unload $PLIST_DST"
echo "  rm $PLIST_DST"
