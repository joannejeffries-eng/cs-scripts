#!/usr/bin/env python3
"""
Auto-generate the CS weekly rota.

Usage:
    python3 generate_rota.py --new 2026-05-11          # Generate week starting Mon 11 May
    python3 generate_rota.py --recalc 2026-05-11       # Re-validate after manual edits
    python3 generate_rota.py --setup                   # First-time: create sheet + seed config

Writes to the "test rota" Google Sheet. Never touches the original rota.
"""
import sys
import os
import json
import argparse
from datetime import datetime, date, timedelta
from pathlib import Path
from collections import defaultdict
from dataclasses import dataclass, field

# ── Config ──────────────────────────────────────────────────────────────────
CREDS_PATH = Path.home() / '.config/juno/claude-code/google-credentials.json'
STATE_PATH = Path.home() / '.claude/scheduled-tasks/generate-rota/state.json'
EXISTING_ROTA_ID = '1CMSEZSb-4D4mO6iPb8tVSaAPsZT5KZst9VSXH4bpi0Y'
SKILLS_MATRIX_ID = '14zUquhC8pnnNnLDDbIeFO6vkgfO6CjnGYBrIZCo8iDE'

# Will be set after sheet creation / loaded from state
TEST_ROTA_ID = None

# ── Role strings (must match fill_tracker.py ROLE_MAP keys exactly) ─────────
ROLE_PHONES = 'Inbound phones'
ROLE_TRIAGE = 'Triage only'
ROLE_TRIAGE_LC = 'Triage + lender chasing'
ROLE_TRIAGE_VC = 'Triage and Video Calls'
ROLE_ICS = 'Case setup only'
ROLE_CHASING = 'Chasing'
ROLE_TL = 'Team lead'
ROLE_NWD = 'Non working day'
ROLE_AL = 'Annual leave'
ROLE_ABSENCE = 'Unplanned absence'
ROLE_TRAINING = 'Training'

ABSENCE_ROLES = {ROLE_AL, ROLE_ABSENCE, ROLE_NWD, ROLE_TRAINING}

# Phone compound roles
def phone_role(secondary=None):
    if secondary:
        return f"{ROLE_PHONES} + {secondary}"
    return ROLE_PHONES

# Secondaries
SEC_WEBCHAT = 'Webchat'
SEC_LMS = 'LMS/LE'
SEC_MISSED = 'Missed calls'
SEC_UNCAT = 'Uncat'
SEC_EMAIL = 'Email Health'

# ── Team data (from TL_CALC_TEAMS in setup_tl_view.py) ─────────────────────
DB_NAMES = {
    'Becky': 'Becky Smith', 'Elida': 'Elida Gizli', 'Fionn': 'Fionn Burrows',
    'Jade': 'Jade Regent', 'Kate': "Kate O'Neill", 'Kirsty': 'Kirsty Rowley',
    'Bella': 'Bella Brayford', 'Clare': 'Clare Brown', 'Cris': 'Cris Macagi',
    'Erika': 'Erika Frolova', 'Harriet': 'Harriet Clifton-Sprigg',
    'Lizzie': 'Lizzie Williamson', 'Lucy': 'Lucy Riordan',
    'Maisha': 'Maisha Begum', 'Noemi': 'Noemi Sip', 'Sophie': 'Sophie Maloney',
    'Tara': 'Tara Dunkley', 'Thea': 'Thea Willsmore',
}

TL_TEAMS = {
    'Courtney': ['Fionn', 'Kate', 'Becky', 'Jade', 'Elida', 'Harriet'],
    'Yasmin': ['Tara', 'Sophie', 'Noemi', 'Lizzie', 'Kirsty'],
    'Jess': ['Bella', 'Cris', 'Clare', 'Erika', 'Lucy', 'Maisha', 'Thea'],
}

ALL_TLS = ['Yasmin', 'Courtney', 'Jess']
ALL_AGENTS = sorted(DB_NAMES.keys())

# Day indices (Mon=0 through Fri=4)
DAY_NAMES = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri']


# ── Data classes ────────────────────────────────────────────────────────────
@dataclass
class PersonConfig:
    name: str
    full_name: str
    team_lead: str
    skills: set = field(default_factory=set)
    hours: dict = field(default_factory=dict)  # day_idx -> hours
    shift_start: float = 9.0   # hour (e.g. 8.5 = 8:30)
    shift_end: float = 18.0    # hour (e.g. 17.5 = 5:30)
    default_role: str = ''
    is_fixed: bool = False
    phone_secondary: str = ''
    status: str = 'active'  # active, training, excluded


@dataclass
class RoleTarget:
    role: str
    target: int
    minimum: int
    cover_required: bool
    rota_string: str
    is_compound: bool = False  # True for phone secondaries


@dataclass
class LunchConfig:
    name: str
    fixed_slot: str = ''  # e.g. '13:30-14:00'
    duration_mins: int = 60
    rotating: bool = True
    no_slot: bool = False  # Kate


# ── Default config data ────────────────────────────────────────────────────

DEFAULT_ROLE_TARGETS = [
    RoleTarget(ROLE_PHONES, 5, 4, True, ROLE_PHONES),
    RoleTarget(ROLE_TRIAGE, 5, 4, False, ROLE_TRIAGE),
    RoleTarget(ROLE_TRIAGE_LC, 1, 1, True, ROLE_TRIAGE_LC),
    RoleTarget(ROLE_CHASING, 3, 2, False, ROLE_CHASING),
    RoleTarget(ROLE_ICS, 2, 1, False, ROLE_ICS),
    RoleTarget(ROLE_TL, 1, 1, False, ROLE_TL),
    # Singleton roles fulfilled as phone secondaries or standalone:
    RoleTarget(SEC_WEBCHAT, 1, 1, False, SEC_WEBCHAT, is_compound=True),
    RoleTarget(SEC_EMAIL, 1, 0, False, SEC_EMAIL, is_compound=True),
    RoleTarget(SEC_MISSED, 1, 0, False, SEC_MISSED, is_compound=True),
    RoleTarget(SEC_LMS, 1, 0, False, SEC_LMS, is_compound=True),
    RoleTarget(SEC_UNCAT, 1, 0, False, SEC_UNCAT, is_compound=True),
]

# Skills matrix: person -> set of roles they can do
# Sourced from memory: project_skills_matrix.md
DEFAULT_SKILLS = {
    'Becky':   {'Inbound', 'Uncat', 'Call&Chase', 'LMS/LE', 'Email Health'},
    'Fionn':   {'Inbound', 'Uncat', 'Verify Addr'},
    'Jade':    {'Inbound', 'LMS/LE', 'Email Health'},
    'Kate':    {'Inbound', 'Uncat', 'Webchat', 'E-sign', 'LMS/LE'},
    'Elida':   {'Inbound', 'Uncat', 'Missed Calls'},
    'Harriet': {'Inbound', 'Webchat', 'LMS/LE', 'Missed Calls'},
    'Bella':   {'Triage', 'ICS'},
    'Cris':    {'Triage', 'Uncat', 'Lender Chase', 'Verify Addr', 'Call&Chase',
                'E-sign', 'LMS/LE', 'Video', 'ID Checks', 'ICS'},
    'Maisha':  {'Triage', 'Inbound', 'Lender Chase', 'Verify Addr', 'Call&Chase',
                'E-sign', 'ID Checks', 'ICS'},
    'Erika':   {'Triage', 'Inbound', 'Uncat', 'Webchat', 'Verify Addr', 'Call&Chase',
                'E-sign', 'LMS/LE', 'ID Checks'},
    'Thea':    {'Triage', 'Inbound', 'Uncat', 'Webchat', 'Lender Chase', 'Verify Addr',
                'Call&Chase', 'E-sign', 'LMS/LE', 'ID Checks', 'ICS'},
    'Clare':   {'Triage', 'Webchat', 'Lender Chase', 'Verify Addr',
                'Call&Chase', 'E-sign', 'LMS/LE', 'Video', 'ID Checks'},
    'Lucy':    set(),  # New starter — update as training completes
    'Noemi':   {'Triage'},
    'Tara':    {'Triage', 'Inbound', 'Uncat', 'Lender Chase', 'Verify Addr',
                'Call&Chase', 'E-sign', 'LMS/LE', 'Video', 'ID Checks', 'ICS'},
    'Sophie':  {'Triage', 'Inbound', 'Lender Chase', 'Verify Addr', 'Call&Chase',
                'E-sign', 'LMS/LE', 'Video', 'ID Checks'},
    'Kirsty':  {'Triage', 'LMS/LE'},
    'Lizzie':  {'Triage', 'Webchat', 'Lender Chase', 'Verify Addr', 'Call&Chase',
                'LMS/LE', 'Video', 'ID Checks'},
}

# Default working hours per day (Mon-Fri, in hours). 0 = non-working day.
DEFAULT_HOURS = {
    'Becky':   {0: 8, 1: 8, 2: 8, 3: 8, 4: 8},
    'Kate':    {0: 8, 1: 8, 2: 8, 3: 8, 4: 8},
    'Fionn':   {0: 8, 1: 8, 2: 8, 3: 8, 4: 8},
    'Jade':    {0: 8, 1: 8, 2: 8, 3: 8, 4: 8},
    'Elida':   {0: 8, 1: 8, 2: 8, 3: 8, 4: 8},
    'Harriet': {0: 8, 1: 8, 2: 8, 3: 8, 4: 8},
    'Bella':   {0: 7, 1: 7, 2: 7, 3: 7, 4: 7},
    'Cris':    {0: 8, 1: 8, 2: 8, 3: 8, 4: 8},
    'Clare':   {0: 0, 1: 8, 2: 8, 3: 8, 4: 8},
    'Erika':   {0: 8, 1: 8, 2: 8, 3: 8, 4: 8},
    'Lucy':    {0: 8, 1: 8, 2: 8, 3: 8, 4: 8},
    'Maisha':  {0: 8, 1: 8, 2: 8, 3: 8, 4: 8},
    'Thea':    {0: 8, 1: 8, 2: 8, 3: 8, 4: 8},
    'Noemi':   {0: 8, 1: 8, 2: 8, 3: 8, 4: 8},
    'Tara':    {0: 4.5, 1: 4.5, 2: 4.5, 3: 4.5, 4: 4},
    'Sophie':  {0: 6, 1: 6, 2: 6, 3: 6, 4: 6},
    'Kirsty':  {0: 8, 1: 8, 2: 8, 3: 8, 4: 8},
    'Lizzie':  {0: 6, 1: 6, 2: 6, 3: 6, 4: 6},
}

# Shift start/end times (hour as float, e.g. 9.5 = 9:30).
# Used to check phone coverage (9-17) and lunch eligibility.
# Format: {name: (start, end)} — applies to all working days unless overridden.
# Split shifts use earliest start / latest end for coverage calculation.
DEFAULT_SHIFTS = {
    'Becky':   (9, 17.5),    # 9-5:30 (or 8:30-5, varies)
    'Kate':    (9, 17.5),    # 9-5:30
    'Fionn':   (8, 17),      # 8-5
    'Jade':    (9, 18),      # 9-6
    'Elida':   (8, 17),      # 8-5
    'Harriet': (8, 17),      # 8-5
    'Bella':   (7, 16.5),    # 7-8:30, 9:15-3, 3:45-4:30 (split — gaps in middle)
    'Cris':    (8, 17),      # 8-5
    'Clare':   (8, 17),      # 8-5 (off Mon)
    'Erika':   (8, 17),      # 8-5
    'Lucy':    (9, 18),      # 9-6 (assumed standard new starter)
    'Maisha':  (9, 18),      # 9-6
    'Thea':    (9, 18),      # 9-6
    'Noemi':   (9, 18),      # 9-6
    'Tara':    (9, 13.5),    # ~4.5h (approx 9-1:30)
    'Sophie':  (7, 15),      # 7-8 + 9:30-3 (split — gap 8-9:30)
    'Kirsty':  (9, 18),      # 9-6
    'Lizzie':  (8, 14),      # 8-2
}

# Phone coverage window (inbound phones must be staffed across this range)
PHONE_HOURS_START = 9   # 9am
PHONE_HOURS_END = 17    # 5pm

# TL rotation: day_idx -> TL name
TL_ROTATION = {0: 'Courtney', 1: 'Courtney', 2: 'Yasmin', 3: 'Jess', 4: 'Jess'}

# Default assignments (person -> default primary role, fixed, phone secondary)
DEFAULT_ASSIGNMENTS = {
    'Becky':   (ROLE_PHONES, True, 'LMS/LE'),
    'Kate':    (ROLE_PHONES, True, SEC_WEBCHAT),
    'Fionn':   (ROLE_PHONES, True, SEC_UNCAT),
    'Elida':   (ROLE_PHONES, True, SEC_MISSED),
    'Jade':    (ROLE_PHONES, True, SEC_EMAIL),
    'Noemi':   (ROLE_TRIAGE, True, ''),
    'Kirsty':  (ROLE_TRIAGE, True, ''),
    'Lizzie':  (ROLE_TRIAGE, True, ''),
}

# Lunch config
DEFAULT_LUNCH = {
    'Kate':  LunchConfig('Kate', no_slot=True, duration_mins=0, rotating=False),
    'Becky': LunchConfig('Becky', fixed_slot='13:30-14:00', duration_mins=30, rotating=False),
}
LUNCH_SLOTS = ['12:00-13:00', '13:00-14:00', '14:00-15:00']


# ── Google Sheets helpers ───────────────────────────────────────────────────

def get_creds():
    from compat import get_google_credentials
    return get_google_credentials()


def read_working_hours(gc):
    """Read the 'Working Hours' tab from the rota sheet and return
    {first_name: (start_hour, end_hour)} as fractional 24-hour values.

    Parses the human-readable 'Schedule' column with two simple rules:
      - START = first time mentioned in the first range
      - END   = last time mentioned in the last range, PM-shifted if it
                ends up < START in 24h

    Handles common formats: '8-5', '9-5:30', '9-6', '8-2', 'Off Mon, 8-5
    Tue-Fri', '9-5:30 or 8:30-5', '7-8:30, 9:15-3, 3:45-4:30 (split)'.

    Returns an empty dict if the Working Hours tab can't be read."""
    import re

    def _parse_t(s):
        s = s.strip().lower().replace(' ', '')
        is_pm = is_am = False
        if s.endswith('pm'):
            is_pm = True; s = s[:-2]
        elif s.endswith('am'):
            is_am = True; s = s[:-2]
        s = s.replace('.', ':')
        try:
            if ':' in s:
                h, m = s.split(':', 1)
                v = int(h) + int(m) / 60
            else:
                v = float(s)
        except ValueError:
            return None
        if is_pm and v < 12:
            v += 12
        elif is_am and v == 12:
            v = 0
        return v

    range_re = re.compile(
        r'(\d{1,2}(?::\d{2})?(?:\.\d+)?(?:\s*[ap]m)?)'
        r'\s*[-–]\s*'
        r'(\d{1,2}(?::\d{2})?(?:\.\d+)?(?:\s*[ap]m)?)',
        re.IGNORECASE,
    )

    try:
        ss = open_sheet(gc, EXISTING_ROTA_ID)
        ws = ss.worksheet("Working Hours")
        rows = ws.get_all_values()
    except Exception:
        return {}

    if not rows:
        return {}

    # Header is row 0: ['Agent', 'Mon Hrs', ..., 'Weekly Total', 'Schedule']
    header = [h.strip() for h in rows[0]]
    try:
        name_col = header.index('Agent')
        sched_col = header.index('Schedule')
    except ValueError:
        return {}

    result = {}
    for r in rows[1:]:
        if len(r) <= sched_col:
            continue
        name = r[name_col].strip()
        sched = r[sched_col].strip()
        if not name:
            continue
        matches = range_re.findall(sched)
        if not matches:
            continue
        start = _parse_t(matches[0][0])
        end = _parse_t(matches[-1][1])
        if start is None or end is None:
            continue
        if end < start:
            end += 12
        result[name] = (start, end)
    return result


def update_daily_notes_roles(gc, monday, assignments):
    """Update the **Scheduled role** and **Cover Needed?** columns of any
    existing Daily Notes rows whose date falls in the week of `monday`,
    based on the latest rota assignments.

    Does NOT add or delete rows. Does NOT touch Time, Note, or Who's
    covering — those are TL- or Jo-authored fields.

    Cover Needed rule: `Yes` for Inbound phones or Triage + lender
    chasing roles; `No` otherwise (per Jo's Daily Notes convention).

    Returns: dict {'updated': N, 'unchanged': M, 'skipped': K, 'detail': [...]}
    """
    from datetime import timedelta as _td
    ss = open_sheet(gc, EXISTING_ROTA_ID)
    ws = ss.worksheet("Daily Notes")
    rows = ws.get_all_values()

    # Daily Notes schema (matches read_daily_notes): data starts at row 5
    # (header is row 4, 1-indexed = row 3 0-indexed). Columns:
    # 0=Date  1=Time  2=Name  3=Role  4=Note  5=Cover Needed?  6=Who's covering?

    week_dates = {monday + _td(days=i): i for i in range(5)}

    cells = []
    detail = []
    updated = unchanged = skipped = 0

    for i, row in enumerate(rows):
        if i < 4:   # skip header rows
            continue
        if len(row) < 6:
            continue
        date_str = (row[0] or '').strip()
        name = (row[2] or '').strip()
        if not date_str or not name:
            continue
        d = _parse_uk_date(date_str)
        if d is None or d not in week_dates:
            continue
        di = week_dates[d]
        actual_role = assignments.get(name, {}).get(di, '')
        if not actual_role:
            skipped += 1
            continue

        cover_yes = (actual_role.startswith('Inbound phones')
                      or actual_role == 'Triage + lender chasing')
        cover_str = 'Yes' if cover_yes else 'No'

        cur_role = (row[3] or '').strip()
        cur_cover = (row[5] or '').strip()

        # ONLY touch a row when the role actually changed. If the role is
        # already correct but the cover differs from the role-based
        # default, leave it — that's a deliberate TL override (e.g.
        # Sophie's all-day video-call days have cover=Yes despite the
        # Triage-only role, because she's not on triage that day).
        if cur_role == actual_role:
            unchanged += 1
            continue

        from gspread.cell import Cell
        cells.append(Cell(row=i + 1, col=4, value=actual_role))   # col D = Role
        cells.append(Cell(row=i + 1, col=6, value=cover_str))     # col F = Cover Needed?
        updated += 1
        detail.append(f"{name} {d.strftime('%a %d/%m')}: "
                       f"{cur_role!r} → {actual_role!r} "
                       f"(cover {cur_cover or '∅'} → {cover_str})")

    if cells:
        ws.update_cells(cells, value_input_option='USER_ENTERED')

    return {'updated': updated, 'unchanged': unchanged,
             'skipped': skipped, 'detail': detail}


def append_daily_notes_rows(gc, rows):
    """Append the given rows to the Daily Notes tab of the rota sheet.

    Each row is a dict with keys: date (datetime.date), time, name, role,
    note, cover_needed (bool), whos_covering.

    The Daily Notes sheet has 7 columns:
        Date | Time | Name | Scheduled role for the day | Note | Cover Needed? | Who's covering?

    Date is written as dd/mm/yyyy. cover_needed becomes 'Yes' / 'No'.

    Returns the number of rows appended."""
    if not rows:
        return 0
    ss = open_sheet(gc, EXISTING_ROTA_ID)
    ws = ss.worksheet("Daily Notes")
    values = []
    for r in rows:
        d = r.get('date')
        date_str = d.strftime('%d/%m/%Y') if d else ''
        cover = 'Yes' if r.get('cover_needed') else 'No'
        values.append([
            date_str,
            r.get('time', ''),
            r.get('name', ''),
            r.get('role', ''),
            r.get('note', ''),
            cover,
            r.get('whos_covering', ''),
        ])
    ws.append_rows(values, value_input_option='USER_ENTERED')
    return len(values)


def get_gspread():
    import gspread
    return gspread.authorize(get_creds())


def get_sheets_service():
    from googleapiclient.discovery import build
    return build('sheets', 'v4', credentials=get_creds())


def get_drive_service():
    from googleapiclient.discovery import build
    return build('drive', 'v3', credentials=get_creds())


_ss_cache = {}

def open_sheet(gc, sheet_id):
    """Open a spreadsheet, caching by sheet_id to avoid repeated API calls."""
    if sheet_id not in _ss_cache:
        _ss_cache[sheet_id] = gc.open_by_key(sheet_id)
    return _ss_cache[sheet_id]


def clear_sheet_cache():
    """Clear cached spreadsheet objects (call after structural changes like adding tabs)."""
    _ss_cache.clear()


def load_state():
    global TEST_ROTA_ID
    if STATE_PATH.exists():
        data = json.loads(STATE_PATH.read_text())
        TEST_ROTA_ID = data.get('test_rota_id')
    return TEST_ROTA_ID


def save_state():
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps({'test_rota_id': TEST_ROTA_ID}, indent=2))


# ── Config readers (from the test rota sheet itself) ────────────────────────

def _find_header_row(rows, expected_first_col):
    """Find the header row by looking for the expected first column value."""
    for i, row in enumerate(rows):
        if row and row[0].strip() == expected_first_col:
            return i
    return 0


def _parse_uk_date(s):
    """Parse a UK date string. Accepts dd/mm/yy AND dd/mm/yyyy.

    The sheet contains a mix — some rows use the short form (13/5/26) and
    newer rows use the long form (21/05/2026). Returns a date or None on
    failure (caller should `continue`)."""
    if not s:
        return None
    parts = s.strip().split('/')
    if len(parts) != 3:
        return None
    try:
        day, month, year = int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        return None
    if year < 100:
        year += 2000
    try:
        return date(year, month, day)
    except ValueError:
        return None


def read_config_defaults(gc, sheet_id):
    """Read Config: Defaults tab. Returns dict of PersonConfig."""
    ss = open_sheet(gc, sheet_id)
    try:
        ws = ss.worksheet("Config: Defaults")
    except Exception:
        return None
    rows = ws.get_all_values()
    header_idx = _find_header_row(rows, 'Name')
    configs = {}
    for row in rows[header_idx + 1:]:
        if not row or not row[0].strip():
            continue
        name = row[0].strip()
        configs[name] = {
            'default_role': row[1].strip() if len(row) > 1 else '',
            'is_fixed': row[2].strip().upper() == 'Y' if len(row) > 2 else False,
            'phone_secondary': row[3].strip() if len(row) > 3 else '',
            'status': row[4].strip() if len(row) > 4 else 'active',
        }
    return configs


def read_config_skills(gc, sheet_id):
    """Read Config: Skills tab. Returns {name: set(skill_names)}."""
    ss = open_sheet(gc, sheet_id)
    try:
        ws = ss.worksheet("Config: Skills")
    except Exception:
        return None
    rows = ws.get_all_values()
    if not rows:
        return None
    header_idx = _find_header_row(rows, 'Name')
    headers = [h.strip() for h in rows[header_idx]]
    skills = {}
    for row in rows[header_idx + 1:]:
        if not row or not row[0].strip():
            continue
        name = row[0].strip()
        person_skills = set()
        for i, h in enumerate(headers[1:], 1):
            if i < len(row) and row[i].strip().upper() in ('TRUE', 'YES', '1', '✅'):
                person_skills.add(h)
        skills[name] = person_skills
    return skills


def read_config_hours(gc, sheet_id):
    """Read Config: Hours tab. Returns ({name: {day_idx: hours}}, {name: (start, end)})."""
    ss = open_sheet(gc, sheet_id)
    try:
        ws = ss.worksheet("Config: Hours")
    except Exception:
        return None, None
    rows = ws.get_all_values()
    if not rows:
        return None, None
    header_idx = _find_header_row(rows, 'Name')
    headers = [h.strip() for h in rows[header_idx]]
    start_col = headers.index('Shift Start') if 'Shift Start' in headers else None
    end_col = headers.index('Shift End') if 'Shift End' in headers else None

    hours = {}
    shifts = {}
    for row in rows[header_idx + 1:]:
        if not row or not row[0].strip():
            continue
        name = row[0].strip()
        per_day = {}
        for di in range(5):
            col = di + 1
            val = row[col].strip() if col < len(row) else '0'
            try:
                per_day[di] = float(val)
            except ValueError:
                per_day[di] = 0.0
        hours[name] = per_day

        if start_col is not None and end_col is not None:
            def parse_time(t):
                t = t.strip()
                if not t:
                    return None
                if ':' in t:
                    parts = t.split(':')
                    return int(parts[0]) + int(parts[1]) / 60
                try:
                    return float(t)
                except ValueError:
                    return None
            s = parse_time(row[start_col]) if start_col < len(row) else None
            e = parse_time(row[end_col]) if end_col < len(row) else None
            if s is not None and e is not None:
                shifts[name] = (s, e)

    return hours, shifts


def read_config_role_targets(gc, sheet_id):
    """Read Config: Role Targets tab. Returns list of RoleTarget."""
    ss = open_sheet(gc, sheet_id)
    try:
        ws = ss.worksheet("Config: Role Targets")
    except Exception:
        return None
    rows = ws.get_all_values()
    header_idx = _find_header_row(rows, 'Role')
    targets = []
    for row in rows[header_idx + 1:]:
        if not row or not row[0].strip():
            continue
        targets.append(RoleTarget(
            role=row[0].strip(),
            target=int(row[1]) if len(row) > 1 and row[1].strip() else 0,
            minimum=int(row[2]) if len(row) > 2 and row[2].strip() else 0,
            cover_required=row[3].strip().upper() == 'Y' if len(row) > 3 else False,
            rota_string=row[4].strip() if len(row) > 4 else row[0].strip(),
            is_compound=row[5].strip().upper() == 'Y' if len(row) > 5 else False,
        ))
    return targets


def read_config_tl_rotation(gc, sheet_id):
    """Read Config: TL Rotation tab. Returns {day_idx: tl_name}."""
    ss = open_sheet(gc, sheet_id)
    try:
        ws = ss.worksheet("Config: TL Rotation")
    except Exception:
        return None
    rows = ws.get_all_values()
    header_idx = _find_header_row(rows, 'Day')
    rotation = {}
    for row in rows[header_idx + 1:]:
        if not row or not row[0].strip():
            continue
        day_name = row[0].strip()
        tl_name = row[1].strip() if len(row) > 1 else ''
        if day_name in DAY_NAMES and tl_name:
            rotation[DAY_NAMES.index(day_name)] = tl_name
    return rotation


def read_original_rota(gc, monday):
    """Read the full rota from the original rota sheet for a given week.

    Returns (assignments, phone_agents) where:
        assignments = {name: {day_idx: role_string}}
        phone_agents = {day_idx: [names on phones]}
    """
    ss = open_sheet(gc, EXISTING_ROTA_ID)
    ws = ss.worksheet("Staff View")
    all_data = ws.get_all_values()

    header = all_data[4]
    agent_cols = {}
    for idx, cell in enumerate(header[2:24], start=2):
        name = cell.strip()
        if name:
            agent_cols[name] = idx

    week_dates = {monday + timedelta(days=i): i for i in range(5)}
    assignments = {name: {} for name in agent_cols}
    phone_agents = {di: [] for di in range(5)}

    for row in all_data[5:]:
        if len(row) < 3 or not row[1].strip():
            continue
        d = _parse_uk_date(row[1])
        if d is None:
            continue
        if d not in week_dates:
            continue
        di = week_dates[d]
        for name, col in agent_cols.items():
            val = row[col].strip() if col < len(row) else ''
            assignments[name][di] = val
            if val.startswith(ROLE_PHONES):
                phone_agents[di].append(name)

    found = sum(1 for n, days in assignments.items() if any(days.values()))
    print(f"  Read {found} people from original rota for w/c {monday}")
    return assignments, phone_agents


def read_daily_notes(gc, monday):
    """Read Daily Notes from the original rota sheet for a given week.

    Returns {day_idx: [entries]} where each entry is a dict:
        {name, time, role, note, cover_needed, whos_covering}
    """
    ss = open_sheet(gc, EXISTING_ROTA_ID)
    ws = ss.worksheet("Daily Notes")
    all_data = ws.get_all_values()

    week_dates = {}
    for i in range(5):
        d = monday + timedelta(days=i)
        week_dates[d] = i

    result = {di: [] for di in range(5)}

    for row in all_data[4:]:  # skip header rows
        if len(row) < 5 or not row[0].strip():
            continue
        d = _parse_uk_date(row[0])
        if d is None or d not in week_dates:
            continue
        di = week_dates[d]
        entry = {
            'name': row[2].strip() if len(row) > 2 else '',
            'time': row[1].strip() if len(row) > 1 else '',
            'role': row[3].strip() if len(row) > 3 else '',
            'note': row[4].strip() if len(row) > 4 else '',
            'cover_needed': (row[5].strip().lower() if len(row) > 5 else '') == 'yes',
            'whos_covering': row[6].strip() if len(row) > 6 else '',
        }
        if entry['name']:
            result[di].append(entry)

    return result


def read_absences_from_original(gc, monday):
    """Read annual leave / absences from the original rota for a given week.
    Returns {name: {day_idx: role_string}} for absence entries only."""
    ss = open_sheet(gc, EXISTING_ROTA_ID)
    ws = ss.worksheet("Staff View")
    all_data = ws.get_all_values()

    header = all_data[4]
    agent_cols = {}
    for idx, cell in enumerate(header[2:24], start=2):
        name = cell.strip()
        if name:
            agent_cols[name] = idx

    week_dates = {monday + timedelta(days=i): i for i in range(5)}
    absences = {}

    for row in all_data[5:]:
        if len(row) < 3 or not row[1].strip():
            continue
        d = _parse_uk_date(row[1])
        if d is None:
            continue
        if d not in week_dates:
            continue
        di = week_dates[d]
        for name, col in agent_cols.items():
            val = row[col].strip() if col < len(row) else ''
            if val in (ROLE_AL, ROLE_ABSENCE, ROLE_TRAINING):
                if name not in absences:
                    absences[name] = {}
                absences[name][di] = val

    if absences:
        total = sum(len(days) for days in absences.values())
        print(f"  Imported {total} absence entries from original rota")
    return absences


def read_staff_view(gc, sheet_id):
    """Read existing Staff View for --recalc mode.
    Returns {name: {day_idx: role_string}} and the list of dates."""
    ss = open_sheet(gc, sheet_id)
    ws = ss.worksheet("Staff View")
    all_data = ws.get_all_values()

    # Header at row 4 (0-indexed): Day, Date, agent names...
    header = all_data[4]
    agent_cols = {}
    all_names = ALL_TLS + ALL_AGENTS
    for idx, cell in enumerate(header):
        name = cell.strip()
        if name in all_names and name not in agent_cols:
            agent_cols[name] = idx

    assignments = {name: {} for name in all_names}
    dates_found = []
    for row in all_data[5:]:
        if not row or not row[1].strip():
            continue
        date_str = row[1].strip()
        d = _parse_uk_date(date_str)
        if d is None:
            continue
        day_idx = d.weekday()
        if day_idx > 4:
            continue
        dates_found.append(d)
        for name, col in agent_cols.items():
            val = row[col].strip() if col < len(row) else ''
            assignments[name][day_idx] = val

    return assignments, dates_found


# ── Constraint solver ───────────────────────────────────────────────────────

def build_people(config_defaults=None, config_skills=None, config_hours=None,
                  config_shifts=None):
    """Build PersonConfig list from config or defaults."""
    people = {}

    for name in ALL_AGENTS:
        tl = ''
        for tl_name, members in TL_TEAMS.items():
            if name in members:
                tl = tl_name
                break

        skills = (config_skills or DEFAULT_SKILLS).get(name, set())
        hours = (config_hours or DEFAULT_HOURS).get(name, {di: 8 for di in range(5)})

        defaults = (config_defaults or {}).get(name, {})
        if not defaults and name in DEFAULT_ASSIGNMENTS:
            drole, fixed, sec = DEFAULT_ASSIGNMENTS[name]
            defaults = {'default_role': drole, 'is_fixed': fixed,
                        'phone_secondary': sec, 'status': 'active'}
        elif not defaults:
            defaults = {'default_role': '', 'is_fixed': False,
                        'phone_secondary': '', 'status': 'active'}

        shift = (config_shifts or DEFAULT_SHIFTS).get(name, (9, 18))
        people[name] = PersonConfig(
            name=name,
            full_name=DB_NAMES.get(name, name),
            team_lead=tl,
            skills=skills,
            hours=hours,
            shift_start=shift[0],
            shift_end=shift[1],
            default_role=defaults.get('default_role', ''),
            is_fixed=defaults.get('is_fixed', False),
            phone_secondary=defaults.get('phone_secondary', ''),
            status=defaults.get('status', 'active'),
        )

    return people


def base_role(role_str):
    """Extract the primary role from a compound role string.
    Phone compounds ('Inbound phones + Webchat') -> 'Inbound phones'.
    'Triage + lender chasing' stays as-is (it's a single role, not a compound)."""
    if not role_str:
        return ''
    if role_str.startswith(ROLE_PHONES + ' + '):
        return ROLE_PHONES
    return role_str


def can_do_role(person, role):
    """Check if a person is trained for a given primary role."""
    if role == ROLE_TRIAGE:
        return 'Triage' in person.skills
    if role == ROLE_PHONES:
        return 'Inbound' in person.skills
    if role == ROLE_CHASING:
        return 'Call&Chase' in person.skills
    if role == ROLE_ICS:
        return 'ICS' in person.skills
    if role == ROLE_TRIAGE_LC:
        return 'Triage' in person.skills and 'Lender Chase' in person.skills
    if role == SEC_EMAIL:
        return 'Email Health' in person.skills
    return False


def can_do_secondary(person, secondary):
    """Check if a phone agent can handle a secondary role."""
    mapping = {
        SEC_WEBCHAT: 'Webchat',
        SEC_LMS: 'LMS/LE',
        SEC_MISSED: 'Missed Calls',
        SEC_UNCAT: 'Uncat',
        SEC_EMAIL: 'Email Health',
    }
    return mapping.get(secondary, secondary) in person.skills


def can_cover_phones(person):
    """Check if a person's shift covers the full phone window (9am-5pm).
    Returns False for part-timers/split shifts that end before PHONE_HOURS_END."""
    if person.hours.get(0, 0) == 0 and all(person.hours.get(di, 0) == 0 for di in range(5)):
        return False
    return person.shift_start <= PHONE_HOURS_START and person.shift_end >= PHONE_HOURS_END


def load_fairness_state():
    """Load role assignment history for fairness tracking."""
    fairness_path = STATE_PATH.parent / 'fairness.json'
    if fairness_path.exists():
        return json.loads(fairness_path.read_text())
    return {}


def save_fairness_state(history):
    fairness_path = STATE_PATH.parent / 'fairness.json'
    fairness_path.parent.mkdir(parents=True, exist_ok=True)
    fairness_path.write_text(json.dumps(history, indent=2))


def update_fairness(history, assignments, monday):
    """Update fairness counters with this week's assignments."""
    week_key = monday.isoformat()
    week_data = {}
    for name, days in assignments.items():
        for day_idx, role in days.items():
            if role and role not in ABSENCE_ROLES:
                br = base_role(role)
                week_data.setdefault(name, {})
                week_data[name][br] = week_data[name].get(br, 0) + 1
    history[week_key] = week_data
    # Keep last 8 weeks
    keys = sorted(history.keys())
    while len(keys) > 8:
        del history[keys.pop(0)]
    return history


def get_role_counts(history, name, role, weeks_back=4):
    """Count how many times person was on role in recent weeks."""
    count = 0
    week_keys = sorted(k for k in history.keys() if not k.startswith('_'))[-weeks_back:]
    for wk in week_keys:
        week_data = history.get(wk, {})
        if isinstance(week_data, dict):
            person_data = week_data.get(name, {})
            if isinstance(person_data, dict):
                count += person_data.get(role, 0)
    return count


def generate_week(monday, people, role_targets=None, tl_rotation=None,
                  known_absences=None, fairness_history=None):
    """Generate role assignments for Mon-Fri using week-long blocks.

    Each person is assigned the same primary role for the whole week where
    possible (better for auditing). Exceptions: TL management days, bank
    holidays, absences, non-working days.

    Args:
        monday: date object for Monday of the week
        people: dict of PersonConfig
        role_targets: list of RoleTarget (or None for defaults)
        tl_rotation: dict day_idx -> TL name (or None for default)
        known_absences: dict {name: {day_idx: role_string}} for pre-marked absences
        fairness_history: dict from load_fairness_state()

    Returns:
        assignments: {name: {day_idx: role_string}} for ALL people (TLs + agents)
        phone_agents: {day_idx: [list of names on phones]}
    """
    if role_targets is None:
        role_targets = DEFAULT_ROLE_TARGETS
    if tl_rotation is None:
        tl_rotation = TL_ROTATION
    if known_absences is None:
        known_absences = {}
    if fairness_history is None:
        fairness_history = {}

    primary_targets = [t for t in role_targets if not t.is_compound]
    secondary_targets = [t for t in role_targets if t.is_compound]

    # UK bank holidays for 2026
    bank_holidays = {
        date(2026, 1, 1), date(2026, 4, 3), date(2026, 4, 6),
        date(2026, 5, 4), date(2026, 5, 25), date(2026, 8, 31),
        date(2026, 12, 25), date(2026, 12, 28),
    }

    assignments = {name: {} for name in list(people.keys()) + ALL_TLS}
    phone_agents_by_day = {di: [] for di in range(5)}

    # Which days are working days this week?
    working_days = []
    for di in range(5):
        day_date = monday + timedelta(days=di)
        if day_date in bank_holidays:
            for name in list(people.keys()) + ALL_TLS:
                assignments[name][di] = ROLE_AL
        else:
            working_days.append(di)

    if not working_days:
        return assignments, phone_agents_by_day

    # ── Pre-fill: absences, NWD, training ──
    for name, abs_days in known_absences.items():
        for di, role_str in abs_days.items():
            if di in working_days:
                assignments[name][di] = role_str

    for name, person in people.items():
        for di in working_days:
            if di in assignments[name]:
                continue
            if person.hours.get(di, 0) == 0:
                assignments[name][di] = ROLE_NWD
            elif person.status in ('training', 'excluded'):
                assignments[name][di] = ROLE_TRAINING

    # ── TL assignments ──
    # Management day: scheduled TL. If absent, another TL covers.
    # Non-management days: TLs get a week-long non-lead role (triage/ICS/chasing),
    # rotated by fairness across weeks.
    TL_NON_LEAD_ROLES = [ROLE_TRIAGE, ROLE_CHASING, ROLE_ICS]

    for di in working_days:
        scheduled_tl = tl_rotation.get(di, '')
        if scheduled_tl and di not in assignments.get(scheduled_tl, {}):
            assignments[scheduled_tl][di] = ROLE_TL
        elif scheduled_tl and assignments.get(scheduled_tl, {}).get(di, '') in ABSENCE_ROLES:
            # Scheduled TL is absent — find a cover TL
            for backup_tl in ALL_TLS:
                if backup_tl == scheduled_tl:
                    continue
                if assignments.get(backup_tl, {}).get(di, '') not in ABSENCE_ROLES:
                    assignments[backup_tl][di] = ROLE_TL
                    break

    # Assign week-long non-lead role to each TL
    for tl in ALL_TLS:
        # Pick a non-lead role for this TL based on fairness
        tl_role_counts = [(r, get_role_counts(fairness_history, tl, r))
                          for r in TL_NON_LEAD_ROLES]
        tl_role_counts.sort(key=lambda x: x[1])
        non_lead_role = tl_role_counts[0][0]

        for di in working_days:
            if di not in assignments.get(tl, {}):
                assignments[tl][di] = non_lead_role

    # ── Fixed role agents (week-long) ──
    for name, person in people.items():
        if person.is_fixed and person.default_role:
            role = person.default_role
            if role == ROLE_PHONES and not can_cover_phones(person):
                role = ROLE_TRIAGE  # fallback if shift doesn't cover phone hours
            for di in working_days:
                if di not in assignments[name]:
                    assignments[name][di] = role

    # ── Week-long block assignment for flex agents ──
    # Determine how many flex people we need per role (across the week).
    # Then assign each flex person to one role for the whole week.

    def count_role_day(role, day):
        count = 0
        for n in list(people.keys()) + ALL_TLS:
            val = assignments[n].get(day, '')
            if val and base_role(val) == role:
                count += 1
        return count

    # Flex agents: not yet assigned on any working day, active
    flex_agents = [name for name in people
                   if people[name].status == 'active'
                   and any(di not in assignments[name] for di in working_days)]

    # Roles that need filling (excluding compound/secondaries)
    # Use the first working day as representative for target gaps
    ref_day = working_days[0]
    fill_order = [
        ROLE_TRIAGE_LC,
        ROLE_PHONES,
        ROLE_ICS,
        ROLE_CHASING,
        ROLE_TRIAGE,  # Catch-all: overflow stays in triage
    ]

    assigned_flex = set()

    for role in fill_order:
        target_obj = next((t for t in primary_targets if t.rota_string == role), None)
        if not target_obj:
            continue
        current = count_role_day(role, ref_day)
        needed = target_obj.target - current

        if needed <= 0:
            if role == ROLE_TRIAGE:
                # Triage is the catch-all — assign everyone remaining
                remaining = [n for n in flex_agents if n not in assigned_flex]
                for name in remaining:
                    assigned_flex.add(name)
                    for di in working_days:
                        if di not in assignments[name]:
                            assignments[name][di] = ROLE_TRIAGE
            continue

        candidates = [n for n in flex_agents
                      if n not in assigned_flex
                      and can_do_role(people[n], role)
                      and (role != ROLE_PHONES or can_cover_phones(people[n]))]
        candidates.sort(key=lambda n: get_role_counts(fairness_history, n, role))

        for c in candidates[:needed]:
            assigned_flex.add(c)
            for di in working_days:
                if di not in assignments[c]:
                    assignments[c][di] = role

    # Anyone still unassigned → triage (shouldn't happen but safety net)
    for name in flex_agents:
        if name not in assigned_flex:
            assigned_flex.add(name)
            for di in working_days:
                if di not in assignments[name]:
                    assignments[name][di] = ROLE_TRIAGE

    # ── Day-specific backfill for cover-required roles ──
    # Week-long blocks can leave per-day gaps when someone is NWD on specific days.
    # Swap triage people in to cover shortfalls on individual days.
    cover_roles = [t for t in primary_targets if t.cover_required]
    for target_obj in cover_roles:
        for di in working_days:
            current = count_role_day(target_obj.rota_string, di)
            shortfall = target_obj.target - current
            if shortfall <= 0:
                continue
            triage_pool = [n for n in list(people.keys()) + ALL_TLS
                           if assignments[n].get(di) == ROLE_TRIAGE
                           and n in people
                           and can_do_role(people[n], target_obj.rota_string)]
            if target_obj.rota_string == ROLE_PHONES:
                triage_pool = [n for n in triage_pool if can_cover_phones(people[n])]
            triage_pool.sort(key=lambda n: get_role_counts(
                fairness_history, n, target_obj.rota_string))
            for fill_name in triage_pool[:shortfall]:
                assignments[fill_name][di] = target_obj.rota_string

    # ── Phone secondaries (week-long per agent) ──
    # Kate = Webchat (fixed). Others rotate weekly by fairness.
    FIXED_SECONDARIES = {'Kate': SEC_WEBCHAT}

    # Identify phone agents (same all week due to block assignment)
    phone_names = set()
    for di in working_days:
        for name in list(people.keys()) + ALL_TLS:
            val = assignments[name].get(di, '')
            if val == ROLE_PHONES or val.startswith(ROLE_PHONES):
                phone_names.add(name)
                phone_agents_by_day[di].append(name)

    assigned_secondaries = set()

    # Fixed secondaries
    for name, sec in FIXED_SECONDARIES.items():
        if name in phone_names:
            person = people.get(name)
            if person and can_do_secondary(person, sec):
                for di in working_days:
                    if assignments[name].get(di, '') in (ROLE_PHONES,):
                        assignments[name][di] = phone_role(sec)
                assigned_secondaries.add(sec)

    # Rotating secondaries — assign one secondary per phone agent for the week
    needed_secs = [t.role for t in secondary_targets if t.role not in assigned_secondaries]
    remaining_phone = [n for n in phone_names
                       if n not in FIXED_SECONDARIES
                       and any(assignments[n].get(di) == ROLE_PHONES for di in working_days)]

    for sec in needed_secs:
        if not remaining_phone:
            break
        eligible = [(n, get_role_counts(fairness_history, n, sec))
                    for n in remaining_phone
                    if people.get(n) and can_do_secondary(people[n], sec)]
        if not eligible:
            continue
        eligible.sort(key=lambda x: (x[1], x[0]))
        chosen = eligible[0][0]
        for di in working_days:
            if assignments[chosen].get(di) == ROLE_PHONES:
                assignments[chosen][di] = phone_role(sec)
        assigned_secondaries.add(sec)
        remaining_phone.remove(chosen)

    # Second pass: any phone agent still on plain "Inbound phones" gets a secondary.
    # Prefer secondaries not yet covered on the agent's phone days.
    still_plain = [n for n in phone_names
                   if n not in FIXED_SECONDARIES
                   and any(assignments[n].get(di) == ROLE_PHONES for di in working_days)]
    all_secs = [t.role for t in secondary_targets]
    for name in still_plain:
        person = people.get(name)
        if not person:
            continue
        trainable = [s for s in all_secs if can_do_secondary(person, s)]
        if not trainable:
            continue
        phone_days = [di for di in working_days if assignments[name].get(di) == ROLE_PHONES]
        # Find which secondaries are already covered on this agent's phone days
        covered_on_days = set()
        for other in phone_names:
            if other == name:
                continue
            for di in phone_days:
                val = assignments[other].get(di, '')
                if ' + ' in val:
                    covered_on_days.add(val.split(' + ', 1)[1])
        # Prefer uncovered secondaries, then by fairness
        uncovered = [s for s in trainable if s not in covered_on_days]
        pool = uncovered if uncovered else trainable
        pool.sort(key=lambda s: get_role_counts(fairness_history, name, s))
        sec = pool[0]
        for di in phone_days:
            assignments[name][di] = phone_role(sec)

    return assignments, phone_agents_by_day


# ── Lunch rota generator ───────────────────────────────────────────────────

def generate_lunch_rota(phone_agents_by_day, people, monday):
    """Generate lunch schedule for the phone team.

    Returns: {day_idx: [(time_slot, person_name), ...]}
    """
    lunch_rota = {}
    # Load rotation state
    fairness = load_fairness_state()
    lunch_key = '_lunch_slot_idx'

    for day_idx in range(5):
        phone_today = phone_agents_by_day.get(day_idx, [])
        day_schedule = []

        # Kate: no slot
        # Becky: fixed 13:30-14:00
        kate_present = 'Kate' in phone_today
        becky_present = 'Becky' in phone_today

        if becky_present:
            day_schedule.append(('13:30-14:00', 'Becky'))

        # Remaining phone agents who need lunch slots
        needs_lunch = [n for n in phone_today
                       if n != 'Kate' and n != 'Becky']

        # Assign to LUNCH_SLOTS in round-robin order
        slot_idx = fairness.get(lunch_key, 0)
        for i, name in enumerate(sorted(needs_lunch)):
            slot = LUNCH_SLOTS[(slot_idx + i) % len(LUNCH_SLOTS)]
            day_schedule.append((slot, name))

        day_schedule.sort(key=lambda x: x[0])
        lunch_rota[day_idx] = day_schedule

    # Advance rotation state
    fairness[lunch_key] = (fairness.get(lunch_key, 0) + 1) % len(LUNCH_SLOTS)
    save_fairness_state(fairness)

    return lunch_rota


# ── Cover suggestion engine ─────────────────────────────────────────────────

def suggest_cover(assignments, people, role_targets=None):
    """Analyse current assignments and suggest cover for understaffed roles.

    Returns list of {day_idx, role, current, target, minimum, gap,
                     suggestion: {name, current_role, headroom}}
    """
    if role_targets is None:
        role_targets = DEFAULT_ROLE_TARGETS

    primary_targets = [t for t in role_targets if not t.is_compound]
    suggestions = []

    for day_idx in range(5):
        # Count per role
        role_counts = defaultdict(int)
        person_roles = {}
        all_absent = True
        for name, days in assignments.items():
            role = days.get(day_idx, '')
            if not role or role in ABSENCE_ROLES:
                continue
            all_absent = False
            base = base_role(role)
            role_counts[base] += 1
            person_roles[name] = base

        if all_absent:
            continue  # Bank holiday — no cover needed

        for target in primary_targets:
            current = role_counts.get(target.rota_string, 0)
            if current >= target.minimum:
                continue
            gap = target.target - current

            # Find candidates: trained + on a role with headroom
            candidates = []
            for name, their_role in person_roles.items():
                if name in ALL_TLS:
                    continue
                person = people.get(name)
                if not person or not can_do_role(person, target.rota_string):
                    continue
                # Check headroom in their current role
                their_target = next((t for t in primary_targets
                                     if t.rota_string == their_role), None)
                if their_target and role_counts[their_role] > their_target.minimum:
                    headroom = role_counts[their_role] - their_target.minimum
                    candidates.append({
                        'name': name,
                        'current_role': their_role,
                        'headroom': headroom,
                    })

            candidates.sort(key=lambda c: -c['headroom'])

            suggestions.append({
                'day': DAY_NAMES[day_idx],
                'day_idx': day_idx,
                'role': target.rota_string,
                'current': current,
                'target': target.target,
                'minimum': target.minimum,
                'gap': gap,
                'cover_required': target.cover_required,
                'suggestions': candidates[:3],
            })

    return suggestions


# ── Dashboard builder ───────────────────────────────────────────────────────

def build_dashboard_data(assignments, role_targets=None):
    """Build headcount data for dashboard.

    Returns list of dicts: {role, mon, tue, wed, thu, fri, target, minimum}
    """
    if role_targets is None:
        role_targets = DEFAULT_ROLE_TARGETS

    primary_targets = [t for t in role_targets if not t.is_compound]
    dashboard = []

    for target in primary_targets:
        row = {
            'role': target.rota_string,
            'target': target.target,
            'minimum': target.minimum,
            'cover_required': target.cover_required,
        }
        for day_idx in range(5):
            count = 0
            for name, days in assignments.items():
                role = days.get(day_idx, '')
                if not role:
                    continue
                base = base_role(role)
                if base == target.rota_string:
                    count += 1
            row[DAY_NAMES[day_idx]] = count
        dashboard.append(row)

    # Totals row
    totals = {'role': 'Total Active', 'target': '', 'minimum': '', 'cover_required': ''}
    absent = {'role': 'Absent/Off', 'target': '', 'minimum': '', 'cover_required': ''}
    for day_idx in range(5):
        active = 0
        off = 0
        for name, days in assignments.items():
            role = days.get(day_idx, '')
            if role in ABSENCE_ROLES:
                off += 1
            elif role:
                active += 1
        totals[DAY_NAMES[day_idx]] = active
        absent[DAY_NAMES[day_idx]] = off
    dashboard.append(totals)
    dashboard.append(absent)

    return dashboard


# ── Sheet writers ───────────────────────────────────────────────────────────

def create_test_rota_sheet():
    """Create a new Google Sheet called 'test rota'. Returns sheet ID."""
    drive = get_drive_service()
    body = {'name': 'test rota', 'mimeType': 'application/vnd.google-apps.spreadsheet'}
    result = drive.files().create(body=body, fields='id').execute()
    sheet_id = result['id']
    print(f"Created 'test rota' sheet: {sheet_id}")
    return sheet_id


def write_config_tabs(gc, sheet_id, people):
    """Write all config tabs to the sheet."""
    ss = open_sheet(gc, sheet_id)

    # ── Config: Defaults ──
    try:
        ws = ss.worksheet("Config: Defaults")
    except Exception:
        ws = ss.add_worksheet("Config: Defaults", rows=35, cols=6)
    data = [
        ['CONFIG: DEFAULTS — Edit this tab when the team changes'],
        ['Update when: someone joins/leaves, changes default role, becomes fixed/flex on a role, or changes status (active/training/excluded).'],
        ['Fixed (Y) = always assigned this role. Status: active / training / excluded. Phone Secondary only applies to phone agents.'],
        ['Name', 'Default Role', 'Fixed (Y/N)', 'Phone Secondary', 'Status'],
    ]
    for name in ALL_TLS:
        data.append([name, ROLE_TL, 'N', '', 'tl'])
    for name in sorted(people.keys()):
        p = people[name]
        data.append([name, p.default_role, 'Y' if p.is_fixed else 'N',
                      p.phone_secondary, p.status])
    ws.clear()
    ws.update(range_name='A1', values=data)
    print(f"  Wrote Config: Defaults ({len(data)-4} people)")

    # ── Config: Skills ──
    try:
        ws = ss.worksheet("Config: Skills")
    except Exception:
        ws = ss.add_worksheet("Config: Skills", rows=35, cols=20)
    skill_cols = ['Triage', 'Inbound', 'Uncat', 'Webchat', 'Lender Chase',
                  'Verify Addr', 'Call&Chase', 'E-sign', 'LMS/LE', 'Video',
                  'ID Checks', 'ICS', 'Email Health', 'Missed Calls']
    data = [
        ['CONFIG: SKILLS — Edit this tab when someone completes training'],
        ['Update when: someone finishes training on a new skill, or is temporarily/permanently removed from a skill.'],
        ['TRUE = trained and available for this role. FALSE = not trained. The script uses this to decide who can fill which roles.'],
        ['Name'] + skill_cols,
    ]
    for name in sorted(people.keys()):
        p = people[name]
        row = [name]
        for sk in skill_cols:
            row.append('TRUE' if sk in p.skills else 'FALSE')
        data.append(row)
    ws.clear()
    ws.update(range_name='A1', values=data)
    print(f"  Wrote Config: Skills ({len(data)-4} people x {len(skill_cols)} skills)")

    # ── Config: Hours ──
    try:
        ws = ss.worksheet("Config: Hours")
    except Exception:
        ws = ss.add_worksheet("Config: Hours", rows=35, cols=10)
    def fmt_time(h):
        hh = int(h)
        mm = int((h - hh) * 60)
        return f"{hh}:{mm:02d}"
    data = [
        ['CONFIG: HOURS — Edit this tab when contracted hours or shifts change'],
        ['Update when: someone changes their working pattern, start/end time, or non-working days (set hours to 0).'],
        ['Shift Start/End are used to check phone coverage (9am-5pm). Anyone whose shift does not cover 9-5 will not be assigned to phones.'],
        ['Name', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Weekly Total', 'Schedule',
         'Shift Start', 'Shift End'],
    ]
    for name in sorted(people.keys()):
        p = people[name]
        weekly = sum(p.hours.get(di, 0) for di in range(5))
        data.append([name] + [str(p.hours.get(di, 0)) for di in range(5)] +
                     [str(weekly), '', fmt_time(p.shift_start), fmt_time(p.shift_end)])
    ws.clear()
    ws.update(range_name='A1', values=data)
    print(f"  Wrote Config: Hours ({len(data)-4} people)")

    # ── Config: Role Targets ──
    try:
        ws = ss.worksheet("Config: Role Targets")
    except Exception:
        ws = ss.add_worksheet("Config: Role Targets", rows=25, cols=7)
    data = [
        ['CONFIG: ROLE TARGETS — Edit this tab if staffing model changes'],
        ['Update when: adding/removing a role, or changing headcount targets/minimums. "Cover Required" = the script will suggest cover if below minimum.'],
        ['"Is Compound" = this role is fulfilled as a phone secondary (e.g. Webchat, LMS/LE), not a standalone assignment.'],
        ['Role', 'Target', 'Minimum', 'Cover Required', 'Rota String', 'Is Compound'],
    ]
    for t in DEFAULT_ROLE_TARGETS:
        data.append([t.role, str(t.target), str(t.minimum),
                     'Y' if t.cover_required else 'N', t.rota_string,
                     'Y' if t.is_compound else 'N'])
    ws.clear()
    ws.update(range_name='A1', values=data)
    print(f"  Wrote Config: Role Targets ({len(data)-4} roles)")

    # ── Config: TL Rotation ──
    try:
        ws = ss.worksheet("Config: TL Rotation")
    except Exception:
        ws = ss.add_worksheet("Config: TL Rotation", rows=12, cols=3)
    data = [
        ['CONFIG: TL ROTATION — Edit this tab if management day assignments change'],
        ['Which TL is on "Team lead" duty each day. If the scheduled TL is absent, the script auto-assigns a backup.'],
        ['Day', 'Team Lead', 'Notes'],
    ]
    for di in range(5):
        data.append([DAY_NAMES[di], TL_ROTATION[di], ''])
    ws.clear()
    ws.update(range_name='A1', values=data)
    print(f"  Wrote Config: TL Rotation")

    # ── Config: Lunch ──
    try:
        ws = ss.worksheet("Config: Lunch")
    except Exception:
        ws = ss.add_worksheet("Config: Lunch", rows=20, cols=5)
    data = [
        ['CONFIG: LUNCH — Edit this tab when lunch arrangements change'],
        ['Fixed Slot = always this time (e.g. Becky 13:30). Rotating = Y means the script rotates their slot weekly. No Slot = Y for people who take short breaks instead (e.g. Kate).'],
        ['Only list people who go on inbound phones. Duration is in minutes. Leave Fixed Slot blank for rotating agents.'],
        ['Name', 'Fixed Slot', 'Duration (mins)', 'Rotating', 'No Slot'],
    ]
    data.append(['Kate', '', '0', 'N', 'Y'])
    data.append(['Becky', '13:30-14:00', '30', 'N', 'N'])
    for name in sorted(people.keys()):
        if name in ('Kate', 'Becky'):
            continue
        if 'Inbound' in people[name].skills:
            data.append([name, '', '60', 'Y', 'N'])
    ws.clear()
    ws.update(range_name='A1', values=data)
    print(f"  Wrote Config: Lunch ({len(data)-4} entries)")


def write_staff_view(gc, sheet_id, assignments, monday, people=None):
    """Write the Staff View tab in fill_tracker.py-compatible format.

    Format:
      Row 0: 'CS Weekly Rota' title
      Row 1: 'Week commencing: DD/MM/YYYY'
      Row 2-3: instructions
      Row 4: header — 'Day', 'Date', then agent first names
      Row 5+: data — day name, D/M/YY, role strings
    """
    ss = open_sheet(gc, sheet_id)
    try:
        ws = ss.worksheet("Staff View")
    except Exception:
        ws = ss.add_worksheet("Staff View", rows=15, cols=30)

    # Column order: TLs first, then agents in same order as fill_tracker
    from fill_tracker import CORE_PHONES, WIDER_TEAM
    if people:
        active_names = set(list(people.keys()) + ALL_TLS)
        col_order = [n for n in ALL_TLS + CORE_PHONES + WIDER_TEAM if n in active_names]
    else:
        col_order = ALL_TLS + CORE_PHONES + WIDER_TEAM

    data = []
    # Rows 0-3: title + instructions
    data.append([f'CS Weekly Rota — Test'])
    data.append([f'Week commencing: {monday.strftime("%d/%m/%Y")}'])
    data.append(['To mark an absence: change the cell to "Annual leave" or "Unplanned absence", then run --recalc to update Dashboard and cover suggestions.'])
    data.append(['Generated by generate_rota.py. Roles are assigned in week-long blocks. Do not rearrange columns — fill_tracker.py depends on the column order.'])
    # Row 4: header
    header = ['Day', 'Date'] + col_order
    data.append(header)
    # Rows 5+: Mon-Fri
    for day_idx in range(5):
        day_date = monday + timedelta(days=day_idx)
        date_str = f"{day_date.day}/{day_date.month}/{str(day_date.year)[-2:]}"
        row = [DAY_NAMES[day_idx], date_str]
        for name in col_order:
            row.append(assignments.get(name, {}).get(day_idx, ''))
        data.append(row)

    ws.clear()
    ws.update(range_name='A1', values=data)
    print(f"  Wrote Staff View ({len(col_order)} columns × 5 days)")


def write_dashboard(gc, sheet_id, dashboard_data, cover_suggestions):
    """Write the Dashboard tab with headcount validation. Returns header row index."""
    ss = open_sheet(gc, sheet_id)
    try:
        ws = ss.worksheet("Dashboard")
    except Exception:
        ws = ss.add_worksheet("Dashboard", rows=30, cols=10)

    data = [
        ['DASHBOARD — Do not edit (auto-generated)'],
        ['Shows actual vs target headcount per role per day. Green = at/above target. Amber = above minimum but below target. Red = below minimum.'],
        ['Run --recalc after making changes to Staff View to refresh this dashboard and update cover suggestions.'],
        [],
        ['Role', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Target', 'Min'],
    ]
    for row_data in dashboard_data:
        row = [row_data['role']]
        for day in DAY_NAMES:
            val = row_data.get(day, '')
            target = row_data.get('target', '')
            if target and val != '':
                row.append(f"{val}/{target}")
            else:
                row.append(str(val))
        row.append(str(row_data.get('target', '')))
        row.append(str(row_data.get('minimum', '')))
        data.append(row)

    # Blank row then cover suggestions
    if cover_suggestions:
        data.append([])
        data.append(['COVER SUGGESTIONS'])
        data.append(['Day', 'Role', 'Gap', 'Cover Required?',
                     'Suggested Person', 'Their Current Role', 'Headroom'])
        for sug in cover_suggestions:
            if not sug['suggestions']:
                data.append([sug['day'], sug['role'], str(sug['gap']),
                             'YES' if sug['cover_required'] else 'No',
                             '(no candidates)', '', ''])
            else:
                for i, cand in enumerate(sug['suggestions']):
                    data.append([
                        sug['day'] if i == 0 else '',
                        sug['role'] if i == 0 else '',
                        str(sug['gap']) if i == 0 else '',
                        ('YES' if sug['cover_required'] else 'No') if i == 0 else '',
                        cand['name'],
                        cand['current_role'],
                        str(cand['headroom']),
                    ])

    ws.clear()
    ws.update(range_name='A1', values=data)
    print(f"  Wrote Dashboard ({len(data)} rows)")
    return 4  # header is at row index 4 (0-indexed)


def write_lunch_rota(gc, sheet_id, lunch_rota, monday):
    """Write the Lunch Rota tab."""
    ss = open_sheet(gc, sheet_id)
    try:
        ws = ss.worksheet("Lunch Rota")
    except Exception:
        ws = ss.add_worksheet("Lunch Rota", rows=20, cols=8)

    data = [
        ['LUNCH ROTA — Do not edit (auto-generated from Config: Lunch)'],
        ['Phone team lunch slots. Minimum 4 agents on phones at all times. Kate takes two short breaks (no slot). Becky is fixed at 13:30. Others rotate weekly.'],
        ['Day', 'Date', '12:00-13:00', '13:00-14:00', '13:30-14:00', '14:00-15:00'],
    ]
    for day_idx in range(5):
        day_date = monday + timedelta(days=day_idx)
        row = [DAY_NAMES[day_idx], day_date.strftime('%d/%m/%Y')]
        slots = {s: '' for s in ['12:00-13:00', '13:00-14:00', '13:30-14:00', '14:00-15:00']}
        for time_slot, name in lunch_rota.get(day_idx, []):
            slots[time_slot] = name
        row.extend([slots['12:00-13:00'], slots['13:00-14:00'],
                     slots['13:30-14:00'], slots['14:00-15:00']])
        data.append(row)

    ws.clear()
    ws.update(range_name='A1', values=data)
    print(f"  Wrote Lunch Rota")


def write_daily_notes(gc, sheet_id, monday):
    """Write empty Daily Notes tab in fill_tracker.py-compatible format."""
    ss = open_sheet(gc, sheet_id)
    try:
        ws = ss.worksheet("Daily Notes")
    except Exception:
        ws = ss.add_worksheet("Daily Notes", rows=50, cols=8)

    data = [
        ['DAILY NOTES — Edit this tab to record absences, appointments and cover during the week'],
        [f'Week commencing: {monday.strftime("%d/%m/%Y")}'],
        ['Add a row for each absence or appointment. "Cover Needed?" triggers only for Inbound phones or lender chasing roles. This tab feeds into the reward tracker for pro-rata calculations.'],
        ['Date', 'Time', 'Name', 'Scheduled role for the day', 'Note',
         'Cover Needed?', "Who's covering?"],
    ]
    # Pre-fill date rows for each day
    for day_idx in range(5):
        day_date = monday + timedelta(days=day_idx)
        date_str = f"{day_date.day}/{day_date.month}/{str(day_date.year)[-2:]}"
        data.append([date_str, '', '', '', '', '', ''])

    ws.clear()
    ws.update(range_name='A1', values=data)
    print(f"  Wrote Daily Notes (empty template)")


def write_overrides(gc, sheet_id):
    """Write Overrides tab in fill_tracker.py-compatible format."""
    ss = open_sheet(gc, sheet_id)
    try:
        ws = ss.worksheet("Overrides")
    except Exception:
        ws = ss.add_worksheet("Overrides", rows=30, cols=7)

    data = [
        ['OVERRIDES — Edit this tab to manage reward tracker overrides and rules'],
        ['This tab is read by fill_tracker.py. Add individual target overrides, standing rules, and pro-rata keywords as needed.'],
        [],
        ['INDIVIDUAL TARGET OVERRIDES'],
        ['Name', 'Role', 'Baseline', 'Stretch', 'Reason', 'Active'],
        ['Bella', 'Triage', '98', '', 'Webchat morning split — confirmed Jess 24 Apr', 'Y'],
        [],
        ['STANDING RULES (applied automatically by the script)'],
        ['Rule', 'Description', 'Active'],
        ['within_1_of_stretch', 'If actual = stretch - 1, grant stretch', 'Y'],
        ['id_skips_below_baseline', 'If ID skips reduced actual below baseline, grant base', 'Y'],
        [],
        ['DAILY NOTES PRO-RATA KEYWORDS'],
        ['Keyword'],
        ['half day'], ['annual leave'], ['al'], ['medical'], ['dentist'],
        ['doctor'], ['appointment'], ['appt'], ['early finish'], ['late start'],
        ['school'], ['blood test'], ['hospital'], ['amended hours'], ['reward time'],
    ]

    ws.clear()
    ws.update(range_name='A1', values=data)
    print(f"  Wrote Overrides tab")


def write_working_hours(gc, sheet_id, people):
    """Write Working Hours tab in fill_tracker.py-compatible format.
    This is separate from Config: Hours — it's the fill_tracker-compatible format."""
    ss = open_sheet(gc, sheet_id)
    try:
        ws = ss.worksheet("Working Hours")
    except Exception:
        ws = ss.add_worksheet("Working Hours", rows=30, cols=8)

    from fill_tracker import CORE_PHONES, WIDER_TEAM, ALL_AGENTS as FT_AGENTS

    data = [
        ['WORKING HOURS — Do not edit (auto-generated from Config: Hours)'],
        ['This tab is in fill_tracker.py format. To change hours, edit Config: Hours instead and re-run --new.'],
        ['Agent', 'Mon Hrs', 'Tue Hrs', 'Wed Hrs', 'Thu Hrs', 'Fri Hrs',
         'Weekly Total', 'Schedule'],
    ]
    for name in FT_AGENTS:
        p = people.get(name)
        if not p:
            continue
        hrs = [p.hours.get(di, 0) for di in range(5)]
        weekly = sum(hrs)
        data.append([name] + [str(h) for h in hrs] + [str(weekly), ''])

    ws.clear()
    ws.update(range_name='A1', values=data)
    print(f"  Wrote Working Hours ({len(data)-1} agents)")


def _col_letter(n):
    """Convert 0-based column index to A1 notation letter(s)."""
    result = ''
    while True:
        result = chr(ord('A') + n % 26) + result
        n = n // 26 - 1
        if n < 0:
            break
    return result


def write_slack_message(gc, sheet_id, monday, num_agent_cols):
    """Write the Slack Message tab with formulas reading from Staff View."""
    ss = open_sheet(gc, sheet_id)
    try:
        ws = ss.worksheet("Slack Message")
    except Exception:
        ws = ss.add_worksheet("Slack Message", rows=30, cols=3)

    last_col = _col_letter(1 + num_agent_cols)  # cols 0=Day, 1=Date, 2..N = agents
    name_range = f"'Staff View'!$C$5:${last_col}$5"
    data_range = f"'Staff View'!$C$6:${last_col}$300"

    # B3 will hold today's date (or a selected date)
    today_str = monday.strftime('%d/%m/%Y')

    # Row references for the FULL MESSAGE formula
    data = [
        # Row 0: title
        ['SLACK MESSAGE — Auto-generated from Staff View', ''],
        # Row 1: instructions
        ['Change the date in B3 to preview a different day. Copy the full message from A22.', ''],
        # Row 2: date selector
        ['Select date:', today_str],
        # Row 3: blank
        ['', ''],
        # Row 4: message header
        ['SLACK MESSAGE (copy below)', ''],
        # Row 5: divider
        ['─────────────────────────────', ''],
        # Row 6: greeting
        ['="Good morning all 📣"', ''],
        # Row 7: blank
        ['', ''],
        # Row 8: phones header
        ['="📞 Phones"', ''],
        # Row 9: phones list (with secondaries)
        [f'=LET(row_num,MATCH(TEXT($B$3,"d/m/yy"),\'Staff View\'!$B$6:$B$300,0),'
         f'names,{name_range},'
         f'roles,INDEX({data_range},row_num,0),'
         f'is_phones,ARRAYFORMULA(LEFT(roles,14)="Inbound phones"),'
         f'side_task,ARRAYFORMULA(IF(LEN(roles)>14,MID(roles,18,100),"")),'
         f'IFERROR(TEXTJOIN(CHAR(10),TRUE,FILTER(names&ARRAYFORMULA(IF(side_task<>"","  + "&side_task,"")),'
         f'is_phones)),"No one assigned"))', ''],
        # Row 10: triage header
        ['="📋 Triage"', ''],
        # Row 11: triage list
        [f'=IFERROR(TEXTJOIN(CHAR(10),TRUE,FILTER({name_range},'
         f'INDEX({data_range},MATCH(TEXT($B$3,"d/m/yy"),\'Staff View\'!$B$6:$B$300,0),0)="Triage only")),'
         f'"No one assigned")', ''],
        # Row 12: T+LC header
        ['="🔔 Triage + Lender Chasing"', ''],
        # Row 13: T+LC list
        [f'=IFERROR(TEXTJOIN(CHAR(10),TRUE,FILTER({name_range},'
         f'INDEX({data_range},MATCH(TEXT($B$3,"d/m/yy"),\'Staff View\'!$B$6:$B$300,0),0)="Triage + lender chasing")),'
         f'"No one assigned")', ''],
        # Row 14: chasing header
        ['="📞 Chasing"', ''],
        # Row 15: chasing list
        [f'=IFERROR(TEXTJOIN(CHAR(10),TRUE,FILTER({name_range},'
         f'INDEX({data_range},MATCH(TEXT($B$3,"d/m/yy"),\'Staff View\'!$B$6:$B$300,0),0)="Chasing")),'
         f'"No one assigned")', ''],
        # Row 16: case setup header
        ['="📁 Case Setup"', ''],
        # Row 17: case setup list
        [f'=IFERROR(TEXTJOIN(CHAR(10),TRUE,FILTER({name_range},'
         f'INDEX({data_range},MATCH(TEXT($B$3,"d/m/yy"),\'Staff View\'!$B$6:$B$300,0),0)="Case setup only")),'
         f'"No one assigned")', ''],
        # Row 18: blank
        ['', ''],
        # Row 19: closing
        ['="Have a wonderful day! 🌟"', ''],
        # Row 20: blank
        ['', ''],
        # Row 21: full message label
        ['FULL MESSAGE (copy from A23):', ''],
        # Row 22: full concatenated message
        ['=A7&CHAR(10)&CHAR(10)'
         '&A9&CHAR(10)&A10&CHAR(10)&CHAR(10)'
         '&A11&CHAR(10)&A12&CHAR(10)&CHAR(10)'
         '&A13&CHAR(10)&A14&CHAR(10)&CHAR(10)'
         '&A15&CHAR(10)&A16&CHAR(10)&CHAR(10)'
         '&A17&CHAR(10)&A18&CHAR(10)&CHAR(10)'
         '&A20', ''],
    ]

    ws.clear()
    ws.update(range_name='A1', values=data, value_input_option='USER_ENTERED')
    print(f"  Wrote Slack Message tab")


def apply_all_formatting(sheet_id, dashboard_data, dashboard_header_row,
                         assignments=None):
    """Apply formatting to all tabs: bold headers, instruction backgrounds,
    freeze panes, column widths, RAG colours on Dashboard, and role colours
    on Staff View."""
    service = get_sheets_service()
    ss_meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()

    sheet_ids = {}
    for s in ss_meta['sheets']:
        sheet_ids[s['properties']['title']] = s['properties']['sheetId']

    requests = []

    # Colours
    INST_BG = {'red': 1.0, 'green': 0.98, 'blue': 0.9}       # Pale cream for instructions
    HEADER_BG = {'red': 0.85, 'green': 0.88, 'blue': 0.95}    # Pale blue-grey for headers
    WARN_BG = {'red': 1.0, 'green': 0.93, 'blue': 0.93}       # Pale red for "don't edit"
    EDIT_BG = {'red': 0.9, 'green': 0.97, 'blue': 0.9}        # Pale green for "edit this"

    def add_row_format(sid, row_start, row_end, bg, bold=False, wrap=True):
        cell_fmt = {'backgroundColor': bg}
        fields = 'userEnteredFormat.backgroundColor'
        if bold:
            cell_fmt['textFormat'] = {'bold': True}
            fields += ',userEnteredFormat.textFormat.bold'
        if wrap:
            cell_fmt['wrapStrategy'] = 'WRAP'
            fields += ',userEnteredFormat.wrapStrategy'
        requests.append({
            'repeatCell': {
                'range': {'sheetId': sid, 'startRowIndex': row_start, 'endRowIndex': row_end},
                'cell': {'userEnteredFormat': cell_fmt},
                'fields': fields,
            }
        })

    def add_freeze(sid, rows=0, cols=0):
        props = {}
        if rows:
            props['frozenRowCount'] = rows
        if cols:
            props['frozenColumnCount'] = cols
        requests.append({
            'updateSheetProperties': {
                'properties': {'sheetId': sid, 'gridProperties': props},
                'fields': ','.join(f'gridProperties.{k}' for k in props),
            }
        })

    def add_col_width(sid, col_start, col_end, width):
        requests.append({
            'updateDimensionProperties': {
                'range': {'sheetId': sid, 'dimension': 'COLUMNS',
                          'startIndex': col_start, 'endIndex': col_end},
                'properties': {'pixelSize': width},
                'fields': 'pixelSize',
            }
        })

    # ── Tab layout: {tab_name: (instruction_rows, header_row, freeze_row, freeze_col, editable)} ──
    TAB_LAYOUT = {
        'Config: Defaults':    (3, 3, 4, 1, True),
        'Config: Skills':      (3, 3, 4, 1, True),
        'Config: Hours':       (3, 3, 4, 1, True),
        'Config: Role Targets':(3, 3, 4, 1, True),
        'Config: TL Rotation': (2, 2, 3, 1, True),
        'Config: Lunch':       (3, 3, 4, 1, True),
        'Staff View':          (0, None, None, None, False),  # Special — has its own layout
        'Dashboard':           (0, None, None, None, False),  # Special
        'Lunch Rota':          (2, 2, 3, 0, False),
        'Daily Notes':         (0, None, None, None, True),   # Special
        'Overrides':           (0, None, None, None, True),   # Special
        'Working Hours':       (2, 2, 3, 1, False),
    }

    for tab_name, (inst_rows, header_row, freeze_row, freeze_col, editable) in TAB_LAYOUT.items():
        sid = sheet_ids.get(tab_name)
        if sid is None:
            continue

        if inst_rows > 0:
            # Instruction rows: cream background
            add_row_format(sid, 0, inst_rows, INST_BG, bold=False)
            # First instruction row bold
            add_row_format(sid, 0, 1, EDIT_BG if editable else WARN_BG, bold=True)

        if header_row is not None:
            # Header row: blue-grey bold
            add_row_format(sid, header_row, header_row + 1, HEADER_BG, bold=True)

        if freeze_row is not None:
            add_freeze(sid, rows=freeze_row, cols=freeze_col or 0)

    # ── Role colour map (matching the original rota) ──
    ROLE_COLOURS = {
        ROLE_PHONES:     {'red': 0.8, 'green': 0.898, 'blue': 1.0},      # Light blue
        ROLE_TRIAGE:     {'red': 0.847, 'green': 1.0, 'blue': 0.847},    # Light green
        ROLE_TRIAGE_LC:  {'red': 0.949, 'green': 0.949, 'blue': 0.698},  # Pale yellow
        ROLE_CHASING:    {'red': 1.0, 'green': 0.898, 'blue': 0.8},      # Light orange
        ROLE_ICS:        {'red': 0.898, 'green': 0.847, 'blue': 1.0},    # Light purple
        ROLE_TL:         {'red': 0.847, 'green': 0.847, 'blue': 0.847},  # Medium grey
        ROLE_AL:         {'red': 1.0, 'green': 0.8, 'blue': 0.8},        # Light red/pink
        ROLE_ABSENCE:    {'red': 1.0, 'green': 0.698, 'blue': 0.698},    # Stronger red
        ROLE_NWD:        {'red': 0.898, 'green': 0.898, 'blue': 0.898},  # Light grey
        ROLE_TRAINING:   {'red': 0.8, 'green': 1.0, 'blue': 1.0},        # Light cyan
    }

    def get_role_colour(role_str):
        if not role_str:
            return None
        if role_str.startswith(ROLE_PHONES):
            return ROLE_COLOURS[ROLE_PHONES]
        return ROLE_COLOURS.get(role_str)

    # ── Staff View: bold title + header, freeze, role colours ──
    sv_sid = sheet_ids.get('Staff View')
    if sv_sid is not None:
        add_row_format(sv_sid, 0, 1, WARN_BG, bold=True)   # Title
        add_row_format(sv_sid, 1, 2, WARN_BG, bold=False)   # Subtitle
        add_row_format(sv_sid, 2, 4, INST_BG, bold=False)   # Instructions
        add_row_format(sv_sid, 4, 5, HEADER_BG, bold=True)  # Column header
        add_freeze(sv_sid, rows=5, cols=2)

        # Role-based cell colours in Staff View (rows 5-9, cols 2+)
        if assignments:
            from fill_tracker import CORE_PHONES, WIDER_TEAM
            active = set(list(assignments.keys()))
            col_order = [n for n in ALL_TLS + CORE_PHONES + WIDER_TEAM if n in active]
            for day_idx in range(5):
                row_in_sheet = 5 + day_idx  # data starts at row 5
                for col_offset, name in enumerate(col_order):
                    col_in_sheet = 2 + col_offset
                    role_str = assignments.get(name, {}).get(day_idx, '')
                    bg = get_role_colour(role_str)
                    if bg:
                        requests.append({
                            'repeatCell': {
                                'range': {
                                    'sheetId': sv_sid,
                                    'startRowIndex': row_in_sheet,
                                    'endRowIndex': row_in_sheet + 1,
                                    'startColumnIndex': col_in_sheet,
                                    'endColumnIndex': col_in_sheet + 1,
                                },
                                'cell': {'userEnteredFormat': {'backgroundColor': bg}},
                                'fields': 'userEnteredFormat.backgroundColor',
                            }
                        })

    # ── Dashboard: bold header, freeze ──
    db_sid = sheet_ids.get('Dashboard')
    if db_sid is not None:
        add_row_format(db_sid, 0, 1, WARN_BG, bold=True)    # "Don't edit" warning
        add_row_format(db_sid, 1, 3, INST_BG, bold=False)   # Instructions
        add_row_format(db_sid, dashboard_header_row, dashboard_header_row + 1,
                       HEADER_BG, bold=True)
        add_freeze(db_sid, rows=dashboard_header_row + 1, cols=1)
        add_col_width(db_sid, 0, 1, 200)  # Role column wider

        # RAG colouring
        num_roles = len([d for d in dashboard_data if d.get('target', '')])
        data_start = dashboard_header_row + 1
        for row_idx in range(num_roles):
            for col_idx in range(1, 6):
                target = dashboard_data[row_idx].get('target', 0)
                minimum = dashboard_data[row_idx].get('minimum', 0)
                if not target:
                    continue
                day_name = DAY_NAMES[col_idx - 1]
                actual = dashboard_data[row_idx].get(day_name, 0)

                if actual >= target:
                    bg = {'red': 0.85, 'green': 0.95, 'blue': 0.85}
                elif actual >= minimum:
                    bg = {'red': 1.0, 'green': 0.95, 'blue': 0.8}
                else:
                    bg = {'red': 0.98, 'green': 0.8, 'blue': 0.8}

                requests.append({
                    'repeatCell': {
                        'range': {
                            'sheetId': db_sid,
                            'startRowIndex': data_start + row_idx,
                            'endRowIndex': data_start + row_idx + 1,
                            'startColumnIndex': col_idx,
                            'endColumnIndex': col_idx + 1,
                        },
                        'cell': {'userEnteredFormat': {'backgroundColor': bg}},
                        'fields': 'userEnteredFormat.backgroundColor',
                    }
                })

    # ── Daily Notes: special formatting ──
    dn_sid = sheet_ids.get('Daily Notes')
    if dn_sid is not None:
        add_row_format(dn_sid, 0, 1, EDIT_BG, bold=True)    # Title
        add_row_format(dn_sid, 1, 3, INST_BG, bold=False)   # Week + instructions
        add_row_format(dn_sid, 3, 4, HEADER_BG, bold=True)  # Column header
        add_freeze(dn_sid, rows=4, cols=0)

    # ── Overrides: special formatting ──
    ov_sid = sheet_ids.get('Overrides')
    if ov_sid is not None:
        add_row_format(ov_sid, 0, 1, EDIT_BG, bold=True)    # Instructions
        add_row_format(ov_sid, 1, 2, INST_BG, bold=False)

    # ── Working Hours ──
    wh_sid = sheet_ids.get('Working Hours')
    if wh_sid is not None:
        add_col_width(wh_sid, 0, 1, 140)

    # ── Slack Message ──
    sm_sid = sheet_ids.get('Slack Message')
    if sm_sid is not None:
        add_row_format(sm_sid, 0, 1, WARN_BG, bold=True)    # Title
        add_row_format(sm_sid, 1, 2, INST_BG, bold=False)   # Instructions
        add_row_format(sm_sid, 4, 5, HEADER_BG, bold=True)  # "SLACK MESSAGE" label
        add_col_width(sm_sid, 0, 1, 500)

    # ── Config tabs: widen first column ──
    for cfg_tab in ['Config: Defaults', 'Config: Skills', 'Config: Hours',
                    'Config: Role Targets', 'Config: TL Rotation', 'Config: Lunch']:
        cfg_sid = sheet_ids.get(cfg_tab)
        if cfg_sid is not None:
            add_col_width(cfg_sid, 0, 1, 140)

    if requests:
        service.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={'requests': requests}
        ).execute()
        print(f"  Applied formatting ({len(requests)} requests across {len(sheet_ids)} tabs)")


def cleanup_default_sheet(gc, sheet_id):
    """Remove the default 'Sheet1' tab if it exists."""
    ss = open_sheet(gc, sheet_id)
    try:
        ws = ss.worksheet("Sheet1")
        ss.del_worksheet(ws)
    except Exception:
        pass


# ── Main commands ───────────────────────────────────────────────────────────

def cmd_setup():
    """First-time setup: create test rota sheet and seed config tabs."""
    global TEST_ROTA_ID

    print("=== Setting up test rota ===")
    TEST_ROTA_ID = create_test_rota_sheet()
    save_state()

    gc = get_gspread()
    people = build_people()
    write_config_tabs(gc, TEST_ROTA_ID, people)
    cleanup_default_sheet(gc, TEST_ROTA_ID)

    print(f"\nTest rota created: https://docs.google.com/spreadsheets/d/{TEST_ROTA_ID}")
    print("Edit the config tabs if needed, then run --new <monday-date> to generate a week.")
    return TEST_ROTA_ID


def cmd_new(monday_str):
    """Generate a new week's rota."""
    global TEST_ROTA_ID
    if not TEST_ROTA_ID:
        load_state()
    if not TEST_ROTA_ID:
        print("No test rota sheet found. Run --setup first.")
        sys.exit(1)

    monday = datetime.strptime(monday_str, '%Y-%m-%d').date()
    if monday.weekday() != 0:
        print(f"WARNING: {monday_str} is a {monday.strftime('%A')}, not a Monday!")

    gc = get_gspread()

    # Read config from sheet (falls back to defaults if tabs missing)
    print("Reading config tabs...")
    config_defaults = read_config_defaults(gc, TEST_ROTA_ID)
    config_skills = read_config_skills(gc, TEST_ROTA_ID)
    config_hours, config_shifts = read_config_hours(gc, TEST_ROTA_ID)
    tl_rotation = read_config_tl_rotation(gc, TEST_ROTA_ID) or TL_ROTATION

    people = build_people(config_defaults, config_skills, config_hours, config_shifts)
    fairness_history = load_fairness_state()

    # Import leave/absences from the original rota
    print("Checking original rota for leave...")
    known_absences = read_absences_from_original(gc, monday)

    print(f"\nGenerating rota for week of {monday.strftime('%d %b %Y')}...")
    assignments, phone_agents = generate_week(
        monday, people, tl_rotation=tl_rotation,
        known_absences=known_absences,
        fairness_history=fairness_history,
    )

    # Update fairness tracking
    fairness_history = update_fairness(fairness_history, assignments, monday)
    save_fairness_state(fairness_history)

    # Build dashboard and cover suggestions
    dashboard_data = build_dashboard_data(assignments)
    cover_suggestions = suggest_cover(assignments, people)

    # Generate lunch rota
    lunch_rota = generate_lunch_rota(phone_agents, people, monday)

    # Write everything
    print("\nWriting to test rota sheet...")
    write_staff_view(gc, TEST_ROTA_ID, assignments, monday, people)
    db_header_row = write_dashboard(gc, TEST_ROTA_ID, dashboard_data, cover_suggestions)
    write_lunch_rota(gc, TEST_ROTA_ID, lunch_rota, monday)
    write_daily_notes(gc, TEST_ROTA_ID, monday)
    write_overrides(gc, TEST_ROTA_ID)
    write_working_hours(gc, TEST_ROTA_ID, people)

    # Slack Message tab (formula-based, reads from Staff View)
    from fill_tracker import CORE_PHONES, WIDER_TEAM
    active_names = set(list(people.keys()) + ALL_TLS)
    num_agent_cols = len([n for n in ALL_TLS + CORE_PHONES + WIDER_TEAM if n in active_names])
    write_slack_message(gc, TEST_ROTA_ID, monday, num_agent_cols)

    # Apply formatting
    apply_all_formatting(TEST_ROTA_ID, dashboard_data, db_header_row, assignments)

    print(f"\nDone! View: https://docs.google.com/spreadsheets/d/{TEST_ROTA_ID}")

    # Print summary
    print("\n── Dashboard Summary ──")
    print(f"{'Role':<25} {'Mon':>5} {'Tue':>5} {'Wed':>5} {'Thu':>5} {'Fri':>5} {'Target':>7}")
    for row in dashboard_data:
        if not row.get('target'):
            continue
        print(f"{row['role']:<25} "
              f"{row.get('Mon', ''):>5} {row.get('Tue', ''):>5} "
              f"{row.get('Wed', ''):>5} {row.get('Thu', ''):>5} "
              f"{row.get('Fri', ''):>5} {str(row.get('target', '')):>7}")

    if cover_suggestions:
        print("\n── Cover Needed ──")
        for sug in cover_suggestions:
            status = "🔴 COVER REQUIRED" if sug['cover_required'] else "🟡 below target"
            print(f"  {sug['day']}: {sug['role']} at {sug['current']}/{sug['target']} — {status}")
            for c in sug['suggestions'][:2]:
                print(f"    → Move {c['name']} from {c['current_role']} (headroom: {c['headroom']})")


def cmd_recalc(monday_str):
    """Re-validate after manual edits to Staff View."""
    global TEST_ROTA_ID
    if not TEST_ROTA_ID:
        load_state()
    if not TEST_ROTA_ID:
        print("No test rota sheet found. Run --setup first.")
        sys.exit(1)

    monday = datetime.strptime(monday_str, '%Y-%m-%d').date()
    gc = get_gspread()

    # Read config
    config_defaults = read_config_defaults(gc, TEST_ROTA_ID)
    config_skills = read_config_skills(gc, TEST_ROTA_ID)
    config_hours, config_shifts = read_config_hours(gc, TEST_ROTA_ID)
    people = build_people(config_defaults, config_skills, config_hours, config_shifts)

    # Read current Staff View
    print("Reading current Staff View...")
    assignments, dates = read_staff_view(gc, TEST_ROTA_ID)

    # Rebuild dashboard and cover suggestions
    dashboard_data = build_dashboard_data(assignments)
    cover_suggestions = suggest_cover(assignments, people)

    # Rebuild lunch rota from current phone assignments
    phone_agents_by_day = {di: [] for di in range(5)}
    for name, days in assignments.items():
        for di, role in days.items():
            if isinstance(di, int) and role.startswith(ROLE_PHONES):
                phone_agents_by_day[di].append(name)
    lunch_rota = generate_lunch_rota(phone_agents_by_day, people, monday)

    # Rewrite dashboard and lunch rota
    print("\nUpdating Dashboard and Lunch Rota...")
    db_header_row = write_dashboard(gc, TEST_ROTA_ID, dashboard_data, cover_suggestions)
    write_lunch_rota(gc, TEST_ROTA_ID, lunch_rota, monday)
    apply_all_formatting(TEST_ROTA_ID, dashboard_data, db_header_row, assignments)

    print(f"\nDone! View: https://docs.google.com/spreadsheets/d/{TEST_ROTA_ID}")

    # Print summary
    print("\n── Dashboard Summary ──")
    print(f"{'Role':<25} {'Mon':>5} {'Tue':>5} {'Wed':>5} {'Thu':>5} {'Fri':>5} {'Target':>7}")
    for row in dashboard_data:
        if not row.get('target'):
            continue
        print(f"{row['role']:<25} "
              f"{row.get('Mon', ''):>5} {row.get('Tue', ''):>5} "
              f"{row.get('Wed', ''):>5} {row.get('Thu', ''):>5} "
              f"{row.get('Fri', ''):>5} {str(row.get('target', '')):>7}")

    if cover_suggestions:
        print("\n── Cover Needed ──")
        for sug in cover_suggestions:
            status = "🔴 COVER REQUIRED" if sug['cover_required'] else "🟡 below target"
            print(f"  {sug['day']}: {sug['role']} at {sug['current']}/{sug['target']} — {status}")
            for c in sug['suggestions'][:2]:
                print(f"    → Move {c['name']} from {c['current_role']} (headroom: {c['headroom']})")


# ── CLI ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Generate CS weekly rota')
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--setup', action='store_true',
                       help='First-time setup: create test rota sheet')
    group.add_argument('--new', metavar='MONDAY',
                       help='Generate new week (YYYY-MM-DD Monday)')
    group.add_argument('--recalc', metavar='MONDAY',
                       help='Recalculate dashboard after manual edits')
    parser.add_argument('--sheet-id', metavar='ID',
                        help='Override test rota sheet ID')

    args = parser.parse_args()

    if args.sheet_id:
        global TEST_ROTA_ID
        TEST_ROTA_ID = args.sheet_id

    if args.setup:
        cmd_setup()
    elif args.new:
        cmd_new(args.new)
    elif args.recalc:
        cmd_recalc(args.recalc)


if __name__ == '__main__':
    main()
