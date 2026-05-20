#!/bin/bash
# Wrapper for pull_reward_actuals.py — sources env vars and runs the pull.
# Invoked by ~/Library/LaunchAgents/com.juno.reward-time-daily-pull.plist
set -euo pipefail
source "$HOME/.juno/.juno-claude-skills-environment.sh"
cd "$(dirname "$0")"
exec /usr/bin/env python3 pull_reward_actuals.py "$@"
