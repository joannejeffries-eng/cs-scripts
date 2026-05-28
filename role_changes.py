"""
Mid-day role-change capture.

When a TL types `Maisha to triage` in #dry-run-testing-jo (threaded under
the daily anchor — temporary while testing; eventually back to
#client-support-leads), the role_change_daemon captures it as a
structured move record. This module holds:

  - the Slack-message parser (parse_role_change_message)
  - persistence (load_moves / save_moves, local + Drive routed via drive_state)
  - mutation (apply_move — append or close)
  - queries (current_role_for, build_live_rota_df)

Move record shape:
    {
        'name':        'Maisha',
        'to_role':     'Triage only',
        'from_role':   'Case setup only',   # set when applied, from rota
        'start_time':  '10:30',             # 'HH:MM' or 'now' if implicit
        'end_time':    '12:00',             # null if still open
        'noted_by':    'U07Q2EEN3SL',       # slack user_id of the TL
        'noted_at':    '2026-05-26T10:32:15',
        'raw_text':    'Maisha to triage 10:30-12',
        'message_ts':  '1779984735.024381',
    }
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pandas as pd

from drive_state import read_json_either, write_json_either

# ── Config ──────────────────────────────────────────────────────────────────

STATE_DIR = Path.home() / '.claude/scheduled-tasks/role-changes'

# Role aliases: lowercase keyword → canonical role string (matches the rota's
# ROLE_TARGETS keys in reward_time.py).
ROLE_ALIASES = {
    'phones':            'Inbound phones',
    'inbound':           'Inbound phones',
    'inbound phones':    'Inbound phones',
    'triage':            'Triage only',
    'triage only':       'Triage only',
    'ics':               'Case setup only',
    'case setup':        'Case setup only',
    'case setup only':   'Case setup only',
    'casesetup':         'Case setup only',
    'cases':             'Case setup only',
    'chase':             'Chasing',
    'chasing':           'Chasing',
    'lender chasing':    'Triage + lender chasing',
    'lc':                'Triage + lender chasing',
    't+lc':              'Triage + lender chasing',
    'triage + lc':       'Triage + lender chasing',
    'triage lc':         'Triage + lender chasing',
    'triage+lc':         'Triage + lender chasing',
    'triage and lc':     'Triage + lender chasing',
    'tvc':               'Triage and Video Calls',
    't+vc':              'Triage and Video Calls',
    'vc':                'Triage and Video Calls',
    'video':             'Triage and Video Calls',
    'video calls':       'Triage and Video Calls',
    'triage vc':         'Triage and Video Calls',
    'triage + vc':       'Triage and Video Calls',
    'triage video':      'Triage and Video Calls',
    'triage and video':  'Triage and Video Calls',
    'triage and video calls': 'Triage and Video Calls',
    'webchat':           'Inbound phones',   # webchat sits under phones role
    # Non-role states — part-day training / leave run from Slack too.
    # All have 0/0 targets in ROLE_TARGETS, so the moved-to portion of the
    # day doesn't count against throughput. (Appointments stay in Daily Notes.)
    'training':          'Training',
    'train':             'Training',
    'leave':             'Part day AL',
    'al':                'Part day AL',
    'annual leave':      'Part day AL',
    'holiday':           'Part day AL',
    'hol':               'Part day AL',
    'off':               'Part day AL',
    'sick':              'Part day AL',
    'sickness':          'Part day AL',
}

# Default move duration when no end time is given AND no closing message arrives.
# 30 minutes — anything below the noise floor anyway.
DEFAULT_OPEN_DURATION_MIN = 30


# ── Name resolution ────────────────────────────────────────────────────────

def _all_names() -> list[str]:
    """Every first-name the team uses, lowercased."""
    try:
        from reward_time import TL_TEAMS, ALL_AGENTS
        names = set(ALL_AGENTS) | set(TL_TEAMS.keys())
        return [n for n in names]
    except Exception:
        return []


def _resolve_name(token: str) -> str | None:
    """Match a first-name token (case-insensitive) against the known team.
    Returns the canonical capitalised name or None."""
    if not token:
        return None
    target = token.strip().lower()
    for n in _all_names():
        if n.lower() == target:
            return n
    return None


def _resolve_role(token: str) -> str | None:
    """Map a free-text role token to the canonical role string. Returns None
    if the token doesn't look like a role at all."""
    if not token:
        return None
    cleaned = token.strip().lower().rstrip('.!?')
    return ROLE_ALIASES.get(cleaned)


# ── Time parsing ────────────────────────────────────────────────────────────

_TIME_RE = re.compile(r'(\d{1,2})(?::(\d{2}))?')


def _parse_hhmm(s: str) -> str | None:
    """Parse '10', '10:30', '14:00' → 'HH:MM' canonical. Returns None on failure."""
    if not s:
        return None
    m = _TIME_RE.fullmatch(s.strip())
    if not m:
        return None
    h = int(m.group(1))
    mins = int(m.group(2)) if m.group(2) else 0
    if not (0 <= h <= 23 and 0 <= mins <= 59):
        return None
    return f"{h:02d}:{mins:02d}"


def _ts_to_hhmm(message_ts: str) -> str:
    """Convert a Slack ts (seconds since epoch) → local 'HH:MM' string."""
    try:
        dt = datetime.fromtimestamp(float(message_ts))
        return dt.strftime('%H:%M')
    except (ValueError, TypeError):
        return ''


# ── Parser ──────────────────────────────────────────────────────────────────

# Patterns the daemon understands. Always anchor to start; we'll strip
# Slack formatting noise (curly quotes, smart arrows) first.

_ARROW_RE = re.compile(r'[→⟶➡️]|->|=>')   # everything we treat as "to"
# "clear all" / "reset" / "clear moves" / "reset all moves" / "scrap moves" /
# "remove all moves" / "start over" — wipes today's captured moves.
_RESET_RE = re.compile(
    r'^\s*(?:clear|reset|scrap|remove|wipe|undo|cancel|start\s+over)'
    r'(?:\s+(?:all|the))?'
    r'(?:\s+moves?)?'
    r'(?:\s+(?:for\s+)?today)?\s*$',
    re.IGNORECASE,
)
_BACK_RE = re.compile(r'^\s*(?P<name>\w+)\s+back(?:\s+to\s+(?P<role>.+?))?\s*$',
                       re.IGNORECASE)
_MOVE_RE = re.compile(
    r'^\s*(?P<name>\w+)\s*'
    r'(?:→|⟶|➡️|->|=>|to|moved\s+to|on)\s+'
    r'(?P<role>[\w+ ]+?)'
    r'(?:\s+(?P<start>\d{1,2}(?::\d{2})?)\s*-\s*(?P<end>\d{1,2}(?::\d{2})?))?'
    r'\s*$',
    re.IGNORECASE,
)


def parse_role_change_message(text: str, message_ts: str,
                                noted_by: str = '') -> dict | None:
    """Parse a Slack message into a move record, or return None.

    Two move shapes are emitted:
      - A 'move' record (new role assignment, possibly with explicit window)
      - A 'back' record (close the currently-open move for this person)

    On success returns a dict with keys: action ('move' or 'back'),
    name, to_role (move only), start_time, end_time (may be None),
    raw_text, message_ts, noted_by, noted_at.
    """
    if not text:
        return None

    # Normalise — strip Slack mention syntax, collapse whitespace
    clean = re.sub(r'<[^>]+>', '', text).strip()
    clean = re.sub(r'\s+', ' ', clean)
    clean = _ARROW_RE.sub(' → ', clean)
    clean = re.sub(r'\s+→\s+', ' → ', clean)

    noted_at = ''
    try:
        noted_at = datetime.fromtimestamp(float(message_ts)).isoformat(timespec='seconds')
    except (ValueError, TypeError):
        pass

    # "clear all" / "reset" / "scrap moves" etc. — wipe today's moves.
    if _RESET_RE.match(clean):
        return {
            'action': 'reset',
            'raw_text': text,
            'message_ts': message_ts,
            'noted_by': noted_by,
            'noted_at': noted_at,
        }

    # "Name back" / "Name back to X"
    m = _BACK_RE.match(clean)
    if m:
        name = _resolve_name(m.group('name'))
        if not name:
            return None
        role_token = m.group('role')
        to_role = _resolve_role(role_token) if role_token else None
        return {
            'action': 'back',
            'name': name,
            'to_role': to_role,    # may be None — apply_move will fall back to original
            'end_time': _ts_to_hhmm(message_ts),
            'raw_text': text,
            'message_ts': message_ts,
            'noted_by': noted_by,
            'noted_at': noted_at,
        }

    # "Name → role" / "Name to role" / "Name on role" with optional " HH:MM-HH:MM"
    # We also accept "Name → role" where role is the standardised arrow form.
    clean_for_move = re.sub(r'\s*→\s*', ' to ', clean, count=1)
    m = _MOVE_RE.match(clean_for_move)
    if m:
        name = _resolve_name(m.group('name'))
        # The role group might capture trailing words; try shrinking from the right
        # until we get an aliased role
        raw_role = (m.group('role') or '').strip()
        to_role = None
        words = raw_role.split()
        for take in range(len(words), 0, -1):
            candidate = ' '.join(words[:take])
            to_role = _resolve_role(candidate)
            if to_role:
                break
        if not (name and to_role):
            return None

        start = _parse_hhmm(m.group('start')) if m.group('start') else _ts_to_hhmm(message_ts)
        end = _parse_hhmm(m.group('end')) if m.group('end') else None
        return {
            'action': 'move',
            'name': name,
            'to_role': to_role,
            'start_time': start,
            'end_time': end,
            'raw_text': text,
            'message_ts': message_ts,
            'noted_by': noted_by,
            'noted_at': noted_at,
        }

    return None


# ── Persistence ────────────────────────────────────────────────────────────

def _local_file(d: date) -> Path:
    return STATE_DIR / f"moves_{d.isoformat()}.json"


def _drive_filename(d: date) -> str:
    return f"moves_{d.isoformat()}.json"


def load_moves(d: date) -> list[dict]:
    """Read moves_<date>.json. Returns [] when no file exists."""
    raw = read_json_either(_local_file(d), _drive_filename(d))
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict) and 'moves' in raw:
        return raw['moves']
    return []


def save_moves(d: date, moves: list[dict]) -> None:
    write_json_either(_local_file(d), _drive_filename(d), moves)


def apply_move(d: date, move: dict, rota_assignments: dict | None = None) -> dict:
    """Append a new move or close the open one for the same person.

    `rota_assignments` (optional): {name: {day_idx: role}} for the day's
    rota week — used to fill `from_role` so a move record always knows
    what the person was rota'd to.

    Returns the resulting move dict (the new entry, or the updated one).
    """
    moves = load_moves(d)
    name = move['name']

    if move.get('action') == 'back':
        # Close the most recent open move for this person
        target = None
        for m in reversed(moves):
            if m.get('name') == name and not m.get('end_time'):
                target = m
                break
        if target is None:
            # No open move — record the "back" as a no-op marker so the
            # daemon can still ack the message helpfully
            return {
                'name': name, 'action': 'back-noop', 'message_ts': move['message_ts'],
                'noted_at': move.get('noted_at', ''),
            }
        target['end_time'] = move['end_time']
        target['closed_by'] = move.get('noted_by', '')
        target['closed_message_ts'] = move['message_ts']
        # If "back to X" — also update the move's "to_role" follow-on intent
        # by appending a new move record for the returning role (so we can
        # render it on the live rota).
        next_role = move.get('to_role')
        if next_role:
            moves.append({
                'action': 'move',
                'name': name,
                'to_role': next_role,
                'from_role': target['to_role'],
                'start_time': move['end_time'],
                'end_time': None,
                'raw_text': move['raw_text'],
                'message_ts': move['message_ts'],
                'noted_by': move.get('noted_by', ''),
                'noted_at': move.get('noted_at', ''),
            })
        save_moves(d, moves)
        return target

    # 'move' — append, but auto-supersede an existing move with the same
    # person + same start_time + same end_time. This handles the "I typed
    # the wrong role, let me re-log it" workflow without forcing the user
    # to post 'back' first (e.g. an early "Tara to T+VC 7-11:30" followed
    # by "Tara to ICS 7-11:30" should replace, not double up).
    from_role = ''
    if rota_assignments:
        day_idx = d.weekday()
        from_role = rota_assignments.get(name, {}).get(day_idx, '') or ''
    move['from_role'] = from_role

    new_start = move.get('start_time')
    new_end = move.get('end_time')
    supersede_idx = None
    for i, existing in enumerate(moves):
        if existing.get('action') != 'move':
            continue
        if existing.get('name') != name:
            continue
        if existing.get('start_time') != new_start:
            continue
        if existing.get('end_time') != new_end:
            continue
        supersede_idx = i
        break

    if supersede_idx is not None:
        move['supersedes_message_ts'] = moves[supersede_idx].get('message_ts')
        moves[supersede_idx] = move
    else:
        moves.append(move)

    save_moves(d, moves)
    return move


# ── Queries ─────────────────────────────────────────────────────────────────

def _hhmm_to_float(s: str) -> float | None:
    """'10:30' → 10.5. None on bad input."""
    if not s:
        return None
    try:
        h, m = s.split(':')
        return int(h) + int(m) / 60
    except (ValueError, AttributeError):
        return None


def current_role_for(name: str, d: date, at_hhmm: str,
                       rota_role: str, moves: list[dict]) -> str:
    """Return the role this person is on at the given time.

    rota_role: the rota-planned role for the day.
    moves:     the day's moves (typically from load_moves).
    """
    t = _hhmm_to_float(at_hhmm)
    if t is None:
        return rota_role
    active = None
    latest_start = -1.0
    for m in moves:
        if m.get('name') != name or m.get('action') != 'move':
            continue
        s = _hhmm_to_float(m.get('start_time'))
        e = _hhmm_to_float(m.get('end_time')) if m.get('end_time') else None
        if s is None:
            continue
        if s > t:
            continue
        if e is not None and t >= e:
            continue
        if s > latest_start:
            latest_start = s
            active = m['to_role']
    return active or rota_role


def build_live_rota_df(d: date, rota: dict, moves: list[dict],
                         hours: list[int] | None = None) -> pd.DataFrame:
    """One column per hour-slot; one row per person. Each cell shows the
    role the person is actually on during that hour.

    rota: {name: {day_idx: role}} — full week, we pick the right day_idx
    automatically.
    hours: optional list of hour ints (e.g. [7,8,9,10,11]). Defaults to
    8 through 17 inclusive (the 17:00–18:00 slot is the last).
    """
    day_idx = d.weekday()
    if day_idx > 4:   # weekend
        return pd.DataFrame()

    # Hour slots — 8 through 17 (inclusive of 17:00–18:00) by default
    if hours is None:
        hours = list(range(8, 18))
    rows = []
    names = sorted(rota.keys())
    for name in names:
        rota_role = (rota.get(name, {}).get(day_idx) or '').strip()
        if not rota_role:
            continue
        row = {'Name': name}
        for h in hours:
            t = f"{h:02d}:00"
            row[t] = current_role_for(name, d, t, rota_role, moves)
        rows.append(row)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).set_index('Name')
    return df
