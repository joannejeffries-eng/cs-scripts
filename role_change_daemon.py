"""
Role-change daemon — captures TL-posted mid-day role moves from Slack.

How it works
------------
- Each working morning the daemon posts an *anchor* message in
  #client-support-leads (C093EAUT3HQ):

      📋 Role changes for Mon 26 May — please post any moves in this thread.
      Format: `Maisha to triage` or `Maisha to triage 10:30-12` or `Maisha back`

  The thread ts is saved in `anchors.json` so subsequent polls scope to
  that thread via `conversations.replies`.

- Every 30 seconds the daemon polls today's anchor thread for new replies.
  Each new reply is parsed via `role_changes.parse_role_change_message`.

- Parseable: ✅ react + threaded reply confirming the capture, write to
  `moves_<date>.json` (local + Drive via drive_state).
  Unparseable: ❌ react + threaded reply with a brief format hint.

Setup
-----
Run `setup_refresh_daemon.sh` — it installs both this daemon AND the
existing refresh daemon as launchd agents.

Logs live at `~/.claude/scheduled-tasks/role-changes/daemon.log`.
"""
from __future__ import annotations

import json
import logging
import sys
import time
from datetime import date, datetime
from pathlib import Path

import requests

from compat import get_slack_token
from drive_state import read_json_either, write_json_either
import role_changes as rc

# ── Config ──────────────────────────────────────────────────────────────────

ROLE_CHANGES_CHANNEL = 'C093EAUT3HQ'   # #client-support-leads
POLL_INTERVAL_SECONDS = 30

STATE_DIR = Path.home() / '.claude/scheduled-tasks/role-changes'
ANCHORS_FILE = STATE_DIR / 'anchors.json'
LAST_SEEN_FILE = STATE_DIR / 'last_seen_ts'
LOG_FILE = STATE_DIR / 'daemon.log'

ANCHOR_HEADER = "📋 Role changes for {day_label} — please post any moves in this thread."
ANCHOR_HELP = ("Format: `Maisha to triage` or `Maisha to triage 10:30-12` "
                "or `Maisha back`")


# ── Slack helpers ───────────────────────────────────────────────────────────

def _slack_get(method: str, params: dict) -> dict:
    r = requests.get(
        f'https://slack.com/api/{method}',
        headers={'Authorization': f'Bearer {get_slack_token()}'},
        params=params, timeout=15,
    )
    return r.json()


def _slack_post(method: str, payload: dict) -> dict:
    r = requests.post(
        f'https://slack.com/api/{method}',
        headers={'Authorization': f'Bearer {get_slack_token()}',
                 'Content-Type': 'application/json'},
        json=payload, timeout=15,
    )
    return r.json()


def _react(channel: str, ts: str, emoji: str) -> None:
    resp = _slack_post('reactions.add',
                        {'channel': channel, 'timestamp': ts, 'name': emoji})
    if not resp.get('ok') and resp.get('error') not in (
            'already_reacted', 'message_not_found'):
        logging.warning(f"reactions.add failed: {resp.get('error')}")


def _thread_reply(channel: str, ts: str, text: str) -> None:
    resp = _slack_post('chat.postMessage',
                        {'channel': channel, 'thread_ts': ts, 'text': text})
    if not resp.get('ok'):
        logging.warning(f"thread reply failed: {resp.get('error')}")


# ── State persistence ──────────────────────────────────────────────────────

def _load_anchors() -> dict:
    if not ANCHORS_FILE.exists():
        return {}
    try:
        return json.loads(ANCHORS_FILE.read_text())
    except Exception:
        return {}


def _save_anchors(anchors: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    ANCHORS_FILE.write_text(json.dumps(anchors, indent=2))


def _read_last_seen() -> str:
    if LAST_SEEN_FILE.exists():
        try:
            return LAST_SEEN_FILE.read_text().strip()
        except Exception:
            pass
    return '0'


def _write_last_seen(ts: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LAST_SEEN_FILE.write_text(ts)


# ── Anchor management ──────────────────────────────────────────────────────

def _get_or_create_anchor(today: date) -> str | None:
    """Return today's anchor ts. Posts one if missing.
    Returns None on Slack errors."""
    anchors = _load_anchors()
    key = today.isoformat()
    if key in anchors:
        return anchors[key]

    text = (ANCHOR_HEADER.format(day_label=today.strftime('%a %d %b %Y'))
            + '\n' + ANCHOR_HELP)
    resp = _slack_post('chat.postMessage',
                        {'channel': ROLE_CHANGES_CHANNEL, 'text': text})
    if not resp.get('ok'):
        logging.error(f"failed to post anchor: {resp.get('error')}")
        return None
    ts = resp.get('ts')
    anchors[key] = ts
    _save_anchors(anchors)
    logging.info(f"posted anchor for {key}: ts={ts}")
    return ts


# ── Rota lookup (for from_role) ────────────────────────────────────────────

def _todays_rota_assignments(today: date) -> dict:
    """Get the rota assignments for today's week (for from_role)."""
    from datetime import timedelta
    monday = today - timedelta(days=today.weekday())
    try:
        from generate_rota import get_gspread, read_original_rota
        assignments, _ = read_original_rota(get_gspread(), monday)
        return assignments
    except Exception as e:
        logging.warning(f"couldn't fetch today's rota: {e}")
        return {}


# ── Main loop ──────────────────────────────────────────────────────────────

def _format_confirmation(applied: dict) -> str:
    """Friendly reply text for a captured move."""
    if applied.get('action') == 'back-noop':
        return (f"ℹ️ Couldn't find an open move for *{applied['name']}* — "
                f"if you meant to log an explicit role, try `{applied['name']} to triage`.")
    if applied.get('action') == 'back':
        return (f"↩️ *{applied['name']}* closed at {applied.get('end_time', '?')}"
                f"{' back to *' + applied['to_role'] + '*' if applied.get('to_role') else ''}")
    parts = [f"✅ *{applied['name']}* to *{applied.get('to_role', '?')}*"]
    if applied.get('from_role'):
        parts.append(f"(from *{applied['from_role']}*)")
    if applied.get('start_time') or applied.get('end_time'):
        parts.append(f"{applied.get('start_time', '?')} to "
                      f"{applied.get('end_time') or 'end of shift'}")
    return ' '.join(parts)


def poll_once(today: date, last_seen: str, anchor_ts: str) -> str:
    """One poll cycle. Returns updated last_seen."""
    resp = _slack_get('conversations.replies', {
        'channel': ROLE_CHANGES_CHANNEL,
        'ts': anchor_ts,
        'oldest': last_seen,
        'limit': 100,
    })
    if not resp.get('ok'):
        logging.warning(f"conversations.replies failed: {resp.get('error')}")
        return last_seen

    rota = None   # lazy — only fetch if we need it

    for msg in resp.get('messages', []):
        ts = msg.get('ts', '')
        if not ts or ts == anchor_ts:
            continue
        try:
            if float(ts) <= float(last_seen):
                continue
        except ValueError:
            continue
        # Skip our own thread replies
        if msg.get('user') == resp.get('messages', [{}])[0].get('user'):
            # The first message in the thread is the anchor (ours).
            # Subsequent ones with the same user could be our reaction
            # confirmations — but those go via thread_ts and don't show
            # up in conversations.replies as separate messages. Just be
            # safe: skip messages whose text looks like our confirmation.
            if (msg.get('text', '').startswith(('✅ ', '↩️ ', 'ℹ️ ', '❌ '))):
                last_seen = ts
                _write_last_seen(last_seen)
                continue

        text = msg.get('text', '')
        noted_by = msg.get('user', '')
        logging.info(f"new reply ts={ts} from={noted_by}: {text[:80]!r}")

        parsed = rc.parse_role_change_message(text, ts, noted_by=noted_by)
        if parsed is None:
            _react(ROLE_CHANGES_CHANNEL, ts, 'x')
            _thread_reply(
                ROLE_CHANGES_CHANNEL, anchor_ts,
                f"❌ Couldn't parse <@{noted_by}>'s message. "
                f"Try `Name to role` or `Name back`. "
                f"Roles: phones, triage, ics, chasing, t+lc."
            )
            last_seen = ts
            _write_last_seen(last_seen)
            continue

        try:
            if rota is None:
                rota = _todays_rota_assignments(today)
            applied = rc.apply_move(today, parsed, rota)
            _react(ROLE_CHANGES_CHANNEL, ts, 'white_check_mark')
            _thread_reply(ROLE_CHANGES_CHANNEL, anchor_ts,
                           _format_confirmation(applied))
        except Exception as e:
            logging.exception("apply_move failed")
            _react(ROLE_CHANGES_CHANNEL, ts, 'x')
            _thread_reply(ROLE_CHANGES_CHANNEL, anchor_ts,
                           f"❌ Captured but couldn't save: {e}")

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
        f"Role-change daemon starting. channel={ROLE_CHANGES_CHANNEL} "
        f"poll={POLL_INTERVAL_SECONDS}s"
    )
    last_seen = _read_last_seen()
    while True:
        try:
            today = date.today()
            # Only operate on weekdays — Sat/Sun no-op
            if today.weekday() <= 4:
                anchor_ts = _get_or_create_anchor(today)
                if anchor_ts:
                    last_seen = poll_once(today, last_seen, anchor_ts)
        except Exception as e:
            logging.exception(f"poll loop error: {e}")
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == '__main__':
    main()
