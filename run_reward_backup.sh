#!/bin/bash
# Wrapper for reward_backup.py — sources env vars and refreshes the
# "CS Reward Time — Backup (Looker)" Google Sheet from Looker + Slack.
# Invoked by ~/Library/LaunchAgents/com.juno.reward-time-backup.plist
set -euo pipefail
source "$HOME/.juno/.juno-claude-skills-environment.sh"
cd "$(dirname "$0")"
exec /usr/bin/python3 reward_backup.py "$@"
