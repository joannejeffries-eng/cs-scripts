"""
Slack-triggered reward-time refresh daemon.

Runs on Jo's Mac as a launchd agent. Polls Slack for a trigger message.
When detected, pulls the latest Looker actuals + skip counts for the
current reward week, writes them to BOTH local state (so Jo's local
rota app sees them) and Google Drive (so the Streamlit Cloud rota app
sees them), then reacts ✅ to the trigger message.

## How Jo triggers it (from anywhere with Slack)

Post the trigger phrase in #dry-run-testing-jo:

    🔄 refresh

(or any message containing the phrase "refresh reward")

Within ~30 s the daemon picks it up, runs the pull, and posts a thread
reply confirming what got refreshed.

## Why this exists

The Looker Postgres DB sits inside Juno's VPC at a 10.x private IP.
Streamlit Cloud can't reach it. Jo's laptop (on VPN) can. This daemon
is the bridge: she triggers it from her phone / cloud app, it runs on
her always-on laptop, results flow back to the cloud via Drive.

## Setup

1. Save your Looker Postgres URL to a file the daemon can read:

       echo "$STAFF_APP_LOOKER_POSTGRES_URL" > \\
           ~/.config/juno/claude-code/looker-postgres-url
       chmod 600 ~/.config/juno/claude-code/looker-postgres-url

2. Copy `com.juno.cs-refresh-daemon.plist` to `~/Library/LaunchAgents/`

3. Load it:

       launchctl load ~/Library/LaunchAgents/com.juno.cs-refresh-daemon.plist

   It auto-starts at every login and restarts on crash.

4. Test by posting `🔄 refresh` in #dry-run-testing-jo. Expect ✅ reaction
   within 30 s and a thread reply summarising the pull.

Logs live at `~/.claude/scheduled-tasks/refresh-daemon/daemon.log`.
"""
from __future__ import annotations

import json
import logging
import sys
import time
from datetime import date
from pathlib import Path

import requests

from compat import get_slack_token
from drive_state import drive_write_json
import reward_time as rt

# ── Config ──────────────────────────────────────────────────────────────────
TRIGGER_CHANNEL = 'C0AUP24HQPP'   # #dry-run-testing-jo
TRIGGER_PHRASE = 'refresh reward'  # case-insensitive substring match
POLL_INTERVAL_SECONDS = 30

STATE_DIR = Path.home() / '.claude/scheduled-tasks/refresh-daemon'
LAST_SEEN_FILE = STATE_DIR / 'last_seen_ts'
LOG_FILE = STATE_DIR / 'daemon.log'


# ── Slack helpers ───────────────────────────────────────────────────────────

def _slack_get(method: str, params: dict) -> dict:
    """GET against api.slack.com."""
    r = requests.get(
        f'https://slack.com/api/{method}',
        headers={'Authorization': f'Bearer {get_slack_token()}'},
        params=params, timeout=15,
    )
    return r.json()


def _slack_post(method: str, payload: dict) -> dict:
    """POST against api.slack.com."""
    r = requests.post(
        f'https://slack.com/api/{method}',
        headers={'Authorization': f'Bearer {get_slack_token()}',
                 'Content-Type': 'application/json'},
        json=payload, timeout=15,
    )
    return r.json()


def _react(channel: str, ts: str, emoji: str) -> None:
    """Add an emoji reaction. Silent on duplicate / already-reacted errors."""
    resp = _slack_post('reactions.add', {
        'channel': channel, 'timestamp': ts, 'name': emoji,
    })
    if not resp.get('ok') and resp.get('error') not in ('already_reacted', 'message_not_found'):
        logging.warning(f"reactions.add failed: {resp.get('error')}")


def _thread_reply(channel: str, ts: str, text: str) -> None:
    """Post a threaded reply under the trigger message."""
    resp = _slack_post('chat.postMessage', {
        'channel': channel, 'thread_ts': ts, 'text': text,
    })
    if not resp.get('ok'):
        logging.warning(f"thread reply failed: {resp.get('error')}")


# ── Last-seen-timestamp persistence ────────────────────────────────────────

def _read_last_seen() -> str:
    if LAST_SEEN_FILE.exists():
        try:
            return LAST_SEEN_FILE.read_text().strip()
        except Exception:
            pass
    # First run — start from "now" so we don't refresh on historical messages
    return f"{time.time():.6f}"


def _write_last_seen(ts: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LAST_SEEN_FILE.write_text(ts)


# ── Pull + publish ─────────────────────────────────────────────────────────

def _push_week_to_drive(friday: date) -> None:
    """Re-read the just-saved local week JSON and upload to Drive.

    We do this instead of duplicating the serialization logic so the
    on-disk and on-Drive copies are byte-identical."""
    local_file = rt._week_file(friday)
    if not local_file.exists():
        return
    data = json.loads(local_file.read_text())
    drive_write_json(rt._week_filename(friday), data)


def refresh_now() -> dict:
    """Run pull_day_data + pull_skips for the current reward week.

    Writes the updated state to both local FS (via save_week) AND Google
    Drive (so the cloud rota app sees it).

    Returns a small dict summarising what was refreshed — used to build
    the thread-reply text."""
    today = date.today()
    friday = rt.get_reward_friday(today)
    all_dates = rt.get_weekday_dates(friday)
    targets = [d for d in all_dates if d <= today]

    if not targets:
        return {'friday': friday, 'days': [], 'people': 0,
                 'note': 'No elapsed working days in this reward week yet.'}

    week_data = rt.load_week(friday)
    if not week_data:
        return {'friday': friday, 'days': [], 'people': 0,
                 'note': "No saved state for this reward week — open the "
                          "rota app once first to initialise it."}

    for d in targets:
        actuals = rt.pull_day_data(d)
        rt.update_day_actuals(week_data, d, actuals)

    skips = rt.pull_skips(friday)
    for name, pw in week_data.items():
        pw.skips = skips.get(name, 0)

    rt.save_week(friday, week_data)   # local
    _push_week_to_drive(friday)        # Drive

    return {'friday': friday, 'days': targets, 'people': len(week_data)}


def _format_summary(result: dict) -> str:
    if 'note' in result:
        return f"ℹ️ {result['note']}"
    days_str = ', '.join(d.strftime('%a %d/%m') for d in result['days'])
    return (
        f"✅ Refreshed actuals + skips for *{result['people']}* people "
        f"across {len(result['days'])} day(s): {days_str}\n"
        f"_Week starting Fri {result['friday'].strftime('%d %b %Y')}._"
    )


# ── Main loop ──────────────────────────────────────────────────────────────

def poll_once(last_seen: str) -> str:
    """One poll cycle. Returns the new last_seen timestamp."""
    resp = _slack_get('conversations.history', {
        'channel': TRIGGER_CHANNEL, 'oldest': last_seen, 'limit': 50,
    })
    if not resp.get('ok'):
        logging.warning(f"conversations.history failed: {resp.get('error')}")
        return last_seen

    # API returns newest first; process oldest first so we keep ts monotonic
    for msg in reversed(resp.get('messages', [])):
        text = msg.get('text', '')
        ts = msg.get('ts', '')
        try:
            if float(ts) <= float(last_seen):
                continue
        except ValueError:
            continue

        if TRIGGER_PHRASE.lower() in text.lower():
            logging.info(f"Trigger at ts={ts}: {text[:80]!r}")
            _react(TRIGGER_CHANNEL, ts, 'hourglass_flowing_sand')
            try:
                result = refresh_now()
                _react(TRIGGER_CHANNEL, ts, 'white_check_mark')
                _thread_reply(TRIGGER_CHANNEL, ts, _format_summary(result))
                logging.info(f"Refresh done: {result}")
            except rt.CloudDBUnreachableError as e:
                # Shouldn't happen here — daemon runs on Jo's laptop — but
                # surface it cleanly if VPN is off.
                _react(TRIGGER_CHANNEL, ts, 'x')
                _thread_reply(TRIGGER_CHANNEL, ts,
                              f"❌ Can't reach the DB — is your VPN connected?\n```{e}```")
                logging.exception("DB unreachable")
            except Exception as e:
                _react(TRIGGER_CHANNEL, ts, 'x')
                _thread_reply(TRIGGER_CHANNEL, ts,
                              f"❌ Refresh failed:\n```{type(e).__name__}: {e}```")
                logging.exception("refresh failed")

        last_seen = ts
        _write_last_seen(last_seen)

    return last_seen


def main():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(message)s',
        handlers=[
            logging.FileHandler(LOG_FILE),
            logging.StreamHandler(sys.stdout),
        ],
    )
    logging.info(
        f"Refresh daemon starting. channel={TRIGGER_CHANNEL} "
        f"trigger=({TRIGGER_PHRASE!r}) poll={POLL_INTERVAL_SECONDS}s"
    )
    last_seen = _read_last_seen()
    while True:
        try:
            last_seen = poll_once(last_seen)
        except Exception as e:
            # Network blip, Slack 5xx, whatever — sleep and continue
            logging.exception(f"poll loop error: {e}")
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == '__main__':
    main()
