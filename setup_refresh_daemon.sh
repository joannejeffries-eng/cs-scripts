#!/bin/bash
# One-time setup for the CS launchd agents.
#
# Three agents get installed:
#   1. com.juno.cs-refresh-daemon       — pulls Looker actuals on demand (Slack-triggered)
#   2. com.juno.cs-role-change-daemon   — captures TL-posted role moves (Slack-triggered)
#   3. com.juno.cs-daily-actuals        — weekday auto-pull at 4 points per day
#
# Long-running daemons (1, 2) auto-start at every login and restart on crash.
# The auto-pull (3) fires Mon–Fri at 06:00, 11:00, 13:30 and 15:30
# (catches up on wake if missed).
#
# What this does:
#   - Writes your $STAFF_APP_LOOKER_POSTGRES_URL to a file the daemons
#     read (they don't inherit your shell env when launchd starts them).
#   - Copies the launchd plists into ~/Library/LaunchAgents/.
#   - Loads + starts each agent.
#
# Idempotent — re-running unloads + reloads each plist fresh.
#
# ⚠️  One-time macOS setup if you've never run a launchd daemon out of
#    ~/Documents before: grant Full Disk Access to /Library/Developer/
#    CommandLineTools/usr/bin/python3 in System Settings → Privacy &
#    Security → Full Disk Access. Without this, the daemons will crash
#    immediately with "Operation not permitted" when reading their .py
#    files from ~/Documents/GitHub/cs-scripts/.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
SECRETS_DIR="$HOME/.config/juno/claude-code"
URL_FILE="$SECRETS_DIR/looker-postgres-url"

DAEMONS=(
  "com.juno.cs-refresh-daemon"
  "com.juno.cs-role-change-daemon"
  "com.juno.cs-daily-actuals"
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

# ── 2b. Sanity check Full Disk Access — exits early if missing ────────────
PY="/Library/Developer/CommandLineTools/usr/bin/python3"
if [[ ! -x "$PY" ]]; then
  echo "❌ $PY missing — install Xcode Command Line Tools first." >&2
  exit 1
fi
# Try to import a module from the repo using the launchd-style invocation.
# If FDA is missing, this fails with Errno 1 'Operation not permitted'.
if ! "$PY" -c "import sys; sys.path.insert(0, '$REPO_DIR'); import compat" 2>/dev/null; then
  echo ""
  echo "⚠️  Python at $PY can't read this repo." >&2
  echo "   Likely Full Disk Access isn't granted." >&2
  echo ""
  echo "   Open: System Settings → Privacy & Security → Full Disk Access" >&2
  echo "   Click +  →  navigate to /Library/Developer/CommandLineTools/usr/bin/" >&2
  echo "             (press ⇧⌘. in the file picker to see hidden /Library)" >&2
  echo "             →  select python3  →  Open" >&2
  echo "   Then re-run this script." >&2
  exit 1
fi

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
echo "  • Refresh:       post '🔄 refresh' in #dry-run-testing-jo"
echo "  • Role change:   post 'Maisha to triage' in #client-support-leads"
echo "                   (under today's anchor message — daemon posts it on first weekday poll)"
echo "  • Auto-pull:     launchctl kickstart -k gui/\$(id -u)/com.juno.cs-daily-actuals"
echo "                   (forces an immediate run; otherwise fires Mon–Fri at"
echo "                   06:00, 11:00, 13:30 and 15:30)"
echo ""
echo "Logs:"
echo "  tail -f ~/.claude/scheduled-tasks/refresh-daemon/daemon.log"
echo "  tail -f ~/.claude/scheduled-tasks/role-changes/daemon.log"
echo ""
echo "To stop / uninstall later:"
for label in "${DAEMONS[@]}"; do
  echo "  launchctl unload ~/Library/LaunchAgents/${label}.plist"
done
