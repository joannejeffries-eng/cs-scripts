#!/bin/bash
# One-time setup for the CS launchd agents.
#
# Five agents get installed:
#   1. com.juno.cs-refresh-daemon          — pulls Looker actuals on demand (Slack-triggered)
#   2. com.juno.cs-role-change-daemon      — captures TL-posted role moves (Slack-triggered)
#   3. com.juno.cs-daily-actuals           — weekday auto-pull at 4 points per day
#   4. com.juno.cs-rota-app                — Streamlit rota app on http://localhost:8501
#   5. com.juno.cs-quality-timeline-checks — Thu 18:30 reward quality+timeline checks
#
# Long-running services (1, 2, 4) auto-start at every login and restart on crash.
# The auto-pull (3) fires Mon–Fri at 06:00, 11:00, 13:30 and 15:30.
# The Thu checks (5) fire Thursday 18:30 (end of the Fri→Thu reward week).
# Single-shot crons (3, 5) catch up on wake if missed.
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
  "com.juno.cs-rota-app"
  "com.juno.cs-quality-timeline-checks"
)

LOG_DIRS=(
  "$HOME/.claude/scheduled-tasks/refresh-daemon"
  "$HOME/.claude/scheduled-tasks/role-changes"
  "$HOME/.claude/scheduled-tasks/rota-app"
  "$HOME/.claude/scheduled-tasks/quality-timeline"
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
echo "  • Rota app:      open http://localhost:8501 in your browser"
echo "                   (auto-starts on login, restarts on crash)"
echo "  • Refresh:       post '🔄 refresh' in #dry-run-testing-jo"
echo "  • Role change:   post 'Maisha to triage' in #dry-run-testing-jo"
echo "                   (under today's anchor message — daemon posts it on first weekday poll;"
echo "                   currently routed to #dry-run-testing-jo while testing —"
echo "                   will move back to #client-support-leads)"
echo "  • Auto-pull:     launchctl kickstart -k gui/\$(id -u)/com.juno.cs-daily-actuals"
echo "                   (forces an immediate run; otherwise fires Mon–Fri at"
echo "                   06:00, 11:00, 13:30 and 15:30)"
echo "  • Thu checks:    launchctl kickstart -k gui/\$(id -u)/com.juno.cs-quality-timeline-checks"
echo "                   (forces an immediate run; otherwise fires Thursday 18:30 —"
echo "                   DMs Jo the quality+timeline review summary)"
echo ""
echo "Logs:"
echo "  tail -f ~/.claude/scheduled-tasks/refresh-daemon/daemon.log"
echo "  tail -f ~/.claude/scheduled-tasks/role-changes/daemon.log"
echo "  tail -f ~/.claude/scheduled-tasks/quality-timeline/checks.log"
echo ""
echo "To stop / uninstall later:"
for label in "${DAEMONS[@]}"; do
  echo "  launchctl unload ~/Library/LaunchAgents/${label}.plist"
done
