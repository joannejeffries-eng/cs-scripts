"""
Reward Time tracker — pulls Looker data, calculates eligibility, stores results.

Separate from fill_tracker.py. Uses the same Postgres/Looker DB but own logic.

Reward weeks run Friday to Thursday (7 days).
Targets from the CS Baselines & Reward Time Plan doc.
"""
import os
import json
import logging
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from collections import defaultdict
from dataclasses import dataclass, field

# ── Config ──────────────────────────────────────────────────────────────────
CREDS_PATH = Path.home() / '.config/juno/claude-code/google-credentials.json'
STATE_DIR = Path.home() / '.claude/scheduled-tasks/reward-time'
STATE_FILE = STATE_DIR / 'state.json'
DB_URL_KEY = 'STAFF_APP_LOOKER_POSTGRES_URL'
REWARD_SHEET_ID = None  # Set after creation / loaded from state

FULL_TIME_HOURS = 40  # Weekly hours for full-time

# ── Name mappings (first name -> DB full name) ──────────────────────────────
DB_NAMES = {
    'Becky': 'Becky Smith', 'Elida': 'Elida Gizli', 'Fionn': 'Fionn Burrows',
    'Jade': 'Jade Regent', 'Kate': "Kate O'Neill", 'Kirsty': 'Kirsty Rowley',
    'Clare': 'Clare Brown', 'Cris': 'Cris Macagi',
    'Erika': 'Erika Frolova', 'Harriet': 'Harriet Clifton-Sprigg',
    'Lizzie': 'Lizzie Williamson', 'Lucy': 'Lucy Riordan',
    'Maisha': 'Maisha Begum', 'Noemi': 'Noemi Sip', 'Sophie': 'Sophie Maloney',
    'Tara': 'Tara Dunkley',
    'Harry': 'Harry McNicholas', 'Roseanne': 'Roseanne Brooks-Brown',
}
FIRST_NAMES = {v: k for k, v in DB_NAMES.items()}

TL_TEAMS = {
    'Courtney': ['Fionn', 'Kate', 'Becky', 'Jade', 'Elida', 'Harriet', 'Harry'],
    'Yasmin': ['Tara', 'Sophie', 'Noemi', 'Lizzie', 'Kirsty', 'Roseanne'],
    'Jess': ['Cris', 'Clare', 'Erika', 'Lucy', 'Maisha'],
}
ALL_AGENTS = sorted(DB_NAMES.keys())

# ── Weekly hours per person (for pro-rata) ──────────────────────────────────
WEEKLY_HOURS = {
    'Becky': 40, 'Kate': 40, 'Fionn': 40, 'Jade': 40, 'Elida': 40,
    'Harriet': 40, 'Cris': 40, 'Clare': 32, 'Erika': 40,
    'Lucy': 40, 'Maisha': 40, 'Noemi': 40, 'Tara': 22,
    'Sophie': 30, 'Kirsty': 40, 'Lizzie': 30,
    'Harry': 40, 'Roseanne': 40,
}

# ── Targets ─────────────────────────────────────────────────────────────────
# Role string from rota -> (base_target, stretch_target, metric_name)
ROLE_TARGETS = {
    'Inbound phones':           (72, 82, 'inbound_calls'),
    'Triage only':              (130, 180, 'emails_archived'),
    'Triage + lender chasing':  (100, 140, 'emails_archived'),  # lower than triage-only — lender chasing takes time off the triage queue
    'Triage and Video Calls':   (100, 140, 'emails_archived'),  # same as +lender chasing — video calls take time off the triage queue too
    'Chasing':                  (95, 110, 'outbound_calls'),
    'Case setup only':          (275, 305, 'things_done'),
    'Training':                 (0, 0, 'training_minutes'),    # split-role only; auto-meets base+stretch
    'Appointment':              (0, 0, 'appointment_minutes'),  # split-role only; auto-meets base+stretch
    'Reward time (prev week)':  (0, 0, 'reward_minutes'),       # split-role only; auto-meets base+stretch
    'Part day AL':              (0, 0, 'al_minutes'),           # split-role only; auto-meets base+stretch
}

# Archive ratio thresholds (triage only)
TRIAGE_ARCHIVE_RATIO_BASE = 0.85
TRIAGE_ARCHIVE_RATIO_STRETCH = 0.87


class CloudDBUnreachableError(RuntimeError):
    """Raised when pull_day_data / pull_skips can't reach the Postgres DB
    because we're on Streamlit Cloud and the DB is on Juno's private VPC.

    The UI catches this specifically and shows a clear "ask Jo to pull from
    her local app" message instead of the raw psycopg2 timeout.
    """


def _connect_postgres(db_url):
    """Wrap psycopg2.connect with a friendly error when running on cloud and
    the DB can't be reached.

    The Looker Postgres lives at a 10.x private IP inside Juno's VPC. Jo's
    laptop reaches it via VPN. Streamlit Cloud cannot — those servers are
    on AWS US-East and have no path into Juno's network. Rather than
    surfacing a 30-second psycopg2 timeout, raise a clear error pointing
    Jo (and her TLs) at the local-app workflow.
    """
    import psycopg2
    from compat import running_on_cloud
    try:
        return psycopg2.connect(db_url, connect_timeout=10)
    except psycopg2.OperationalError as e:
        if running_on_cloud():
            raise CloudDBUnreachableError(
                "Can't reach the Looker database — it's on Juno's private "
                "network and only Jo's laptop (on VPN) can connect.\n\n"
                "👉 Reward-time actuals are pulled by the local daily cron "
                "(com.juno.cs-daily-actuals) and written to the tracker sheet "
                "+ Drive state. If figures look stale, run "
                "`python3 daily_actuals_pull.py` on Jo's laptop (VPN on)."
            ) from e
        raise


def _lookup_targets(role):
    """Look up (base, stretch, metric) for a role string.
    Handles compound roles like 'Inbound phones + Webchat'."""
    if role in ROLE_TARGETS:
        return ROLE_TARGETS[role]
    # Phone compound: "Inbound phones + ..." -> "Inbound phones"
    if role.startswith('Inbound phones'):
        return ROLE_TARGETS.get('Inbound phones')
    return None

# Skip threshold (weekly, pro-rata for days worked)
SKIP_THRESHOLD_WEEKLY = 50

# Reward time amounts (hours)
BASE_REWARD_HOURS = 3.0
STRETCH_BONUS_HOURS = 1.0

# ── Reward day assignments ──────────────────────────────────────────────────
# {name: (day_name, block)} — day_name is 'Tue','Wed','Thu'; block is 'AM' or 'PM'
REWARD_DAYS = {
    'Jade':     ('Tue', 'AM'),
    'Maisha':   ('Tue', 'AM'),
    'Becky':    ('Tue', 'PM'),
    'Lucy':     ('Tue', 'PM'),
    'Sophie':   ('Tue', 'PM'),
    'Fionn':    ('Wed', 'AM'),
    'Noemi':    ('Wed', 'AM'),
    'Cris':     ('Wed', 'AM'),
    'Kate':     ('Wed', 'PM'),
    'Lizzie':   ('Wed', 'PM'),
    'Clare':    ('Wed', 'PM'),
    'Kirsty':   ('Wed', 'PM'),
    'Tara':     ('Thu', 'AM'),
    'Harry':    ('Thu', 'AM'),
    'Elida':    ('Thu', 'PM'),
    'Erika':    ('Thu', 'PM'),
    'Roseanne': ('Thu', 'PM'),
    'Harriet':  ('Fri', 'PM'),
}
# Harry starts 2026-06-01, Roseanne 2026-06-10 — blocks assigned now; they won't
# appear in the weekly reward message until they have worked days (days_worked > 0).
# Phones team: Jade, Becky, Kate, Fionn, Kirsty, Elida
# Phones can only swap with phones

DAY_NAME_TO_IDX = {'Mon': 0, 'Tue': 1, 'Wed': 2, 'Thu': 3, 'Fri': 4}


# ── Data structures ─────────────────────────────────────────────────────────

STANDARD_SHIFT_HOURS = 8.0  # Full day = 8 productive hours; targets are calibrated to this

# Per-person daily hours (mirrors generate_rota DEFAULT_HOURS)
DAILY_HOURS = {
    'Becky': 8, 'Kate': 8, 'Fionn': 8, 'Jade': 8, 'Elida': 8,
    'Harriet': 8, 'Cris': 8, 'Clare': 8, 'Erika': 8, 'Lucy': 8,
    'Maisha': 8, 'Noemi': 8, 'Kirsty': 8,
    'Tara': 4.5, 'Sophie': 6, 'Lizzie': 6,
    'Harry': 8, 'Roseanne': 8,
}


@dataclass
class RoleSegment:
    """One portion of a day spent on a single role (for split-role days)."""
    role: str = ''
    minutes: int = 480      # how long on this role
    target_base: int = 0
    target_stretch: int = 0
    actual: int = 0
    met_base: bool = False
    met_stretch: bool = False
    archive_ratio: float = 0.0


@dataclass
class DayResult:
    role: str = ''                  # primary role (or "Role A / Role B" for splits)
    target_base: int = 0
    target_stretch: int = 0
    actual: int = 0
    met_base: bool = False
    met_stretch: bool = False
    is_working: bool = True         # False for NWD/leave/absence
    archive_ratio: float = 0.0     # triage only
    shift_hours: float = 8.0       # actual hours worked (for pro-rata)
    segments: list = field(default_factory=list)  # [RoleSegment] — non-empty = split-role day
    metrics: dict = field(default_factory=dict)   # all raw metrics {metric_name: value} for splits


@dataclass
class PersonWeek:
    name: str
    days: dict = field(default_factory=dict)  # {date: DayResult}
    skips: int = 0
    quality_ok: bool = False
    timeline_ok: bool = False
    # Automated suggestions (set by run_quality_timeline_checks; Jo can override).
    # *_suggested is None until checks have been run for this week.
    quality_suggested: object = None      # bool | None
    quality_reason: str = ''
    timeline_suggested: object = None     # bool | None
    timeline_reason: str = ''
    timeline_gaps: list = field(default_factory=list)  # [{date, start, end, minutes}]
    overrides: list = field(default_factory=list)  # [{date, field, old, new, reason, timestamp}]
    override_eligible: str = ''  # '', 'base', 'stretch' — manual override of final eligibility
    weekly_hours: float = 40.0
    days_worked: int = 0
    days_absent: int = 0          # AL + sick (excludes NWD and bank holidays)
    expected_days: int = 5         # days they should have worked (excludes NWD + bank hols)
    # TL submission + Jo approval (TL app workflow)
    tl_submitted_at: str = ''        # ISO timestamp when this TL submitted this person
    tl_request_level: str = ''       # '', 'base', 'stretch', 'deny' — what TL is asking Jo to grant
    tl_notes: str = ''               # TL's reason / context for Jo
    jo_decision: str = ''            # '', 'approved', 'question'
    jo_decision_at: str = ''         # ISO timestamp
    jo_question_text: str = ''       # if jo_decision == 'question'


# ── Bank holidays (UK) ────────────────────────────────────────────────────
# Used to exclude bank holidays from absence pro-rata.
# Update annually or when dates are confirmed.
UK_BANK_HOLIDAYS = {
    # 2026
    date(2026, 1, 1),   # New Year
    date(2026, 4, 3),   # Good Friday
    date(2026, 4, 6),   # Easter Monday
    date(2026, 5, 4),   # Early May
    date(2026, 5, 25),  # Spring
    date(2026, 8, 31),  # Summer
    date(2026, 12, 25), # Christmas
    date(2026, 12, 28), # Boxing Day (substitute)
}


# ── Week helpers ────────────────────────────────────────────────────────────

def get_reward_friday(d=None):
    """Get the Friday that starts the current reward week."""
    if d is None:
        d = date.today()
    days_since_friday = (d.weekday() - 4) % 7
    return d - timedelta(days=days_since_friday)


def get_reward_week_dates(friday):
    """Return list of 7 dates Fri-Thu for a reward week.
    Only weekdays (Fri, Mon-Thu) — skip Sat/Sun."""
    dates = [friday]  # Friday
    # Sat/Sun skipped
    for offset in range(3, 8):  # Mon=3, Tue=4, Wed=5, Thu=6
        dates.append(friday + timedelta(days=offset))
    return dates  # [Fri, Mon, Tue, Wed, Thu]


def get_weekday_dates(friday):
    """Return the 5 working days in a reward week: [Fri, Mon, Tue, Wed, Thu]."""
    return [
        friday,
        friday + timedelta(days=3),  # Mon
        friday + timedelta(days=4),  # Tue
        friday + timedelta(days=5),  # Wed
        friday + timedelta(days=6),  # Thu
    ]


# ── Database queries ────────────────────────────────────────────────────────

# Cache the PDT table lookup per Python process. The freshness scan over
# 4+ candidate tables (full MAX(date)) was costing ~9 s per pull_day_data
# call — multiply by 5 days and a full-week refresh was 45 s of overhead
# before any real querying.
_PDT_TABLE_CACHE = {'name': None}


def _find_pdt_table(cur):
    """Find the freshest pdt_things_done_by_person table in looker_scratch.

    Cached for the lifetime of the Python process — but PDT tables rotate
    (Looker drops the old one and rebuilds), so when we have a cached name
    we probe it first with a no-op SELECT. If it's been dropped
    (UndefinedTable) we roll back the failed transaction, invalidate the
    cache, and re-resolve. Avoids needing a manual app restart every time
    Looker rebuilds the scratch table.
    """
    import psycopg2
    cached = _PDT_TABLE_CACHE.get('name')
    if cached:
        try:
            cur.execute(f"SELECT 1 FROM {cached} LIMIT 0")
            return cached
        except psycopg2.errors.UndefinedTable:
            cur.connection.rollback()
            _PDT_TABLE_CACHE['name'] = None
            logging.info(f"PDT cache {cached!r} stale (table dropped); re-resolving")
        except Exception:
            cur.connection.rollback()
            _PDT_TABLE_CACHE['name'] = None

    cur.execute("""
        SELECT table_schema || '.' || table_name
        FROM information_schema.tables
        WHERE table_name LIKE '%pdt_things_done_by_person'
    """)
    candidates = [r[0] for r in cur.fetchall()]
    if not candidates:
        raise RuntimeError("Could not find pdt_things_done_by_person table")

    best_table = None
    best_date = None
    for tbl in candidates:
        cur.execute(f"SELECT MAX(DATE(doable_or_enquiry_action_completed_at)) FROM {tbl}")
        max_date = cur.fetchone()[0]
        if max_date is not None and (best_date is None or max_date > best_date):
            best_date = max_date
            best_table = tbl

    if not best_table:
        raise RuntimeError("All pdt_things_done_by_person tables are empty")
    _PDT_TABLE_CACHE['name'] = best_table
    return best_table


def pull_day_data(target_date):
    """Pull throughput data for a single day from Looker/Postgres.
    Returns {first_name: {metric: value}}."""
    import psycopg2

    from compat import get_postgres_url
    db_url = get_postgres_url()

    conn = _connect_postgres(db_url)
    cur = conn.cursor()
    pdt = _find_pdt_table(cur)

    start = target_date
    end = target_date + timedelta(days=1)
    results = defaultdict(dict)

    # Phone calls (inbound)
    cur.execute("""
        SELECT user_name,
               COUNT(*) FILTER (WHERE answered_at IS NOT NULL) as answered
        FROM public.phone_activity
        WHERE DATE(started_at) = %s
          AND direction = 'inbound'
          AND user_name IS NOT NULL
        GROUP BY 1
    """, (start,))
    for user_name, count in cur.fetchall():
        first = FIRST_NAMES.get(user_name)
        if first:
            results[first]['inbound_calls'] = count

    # Phone calls (outbound — for chasing)
    cur.execute("""
        SELECT user_name,
               COUNT(*) FILTER (WHERE answered_at IS NOT NULL AND duration > 25) as answered
        FROM public.phone_activity
        WHERE DATE(started_at) = %s
          AND direction = 'outbound'
          AND user_name IS NOT NULL
        GROUP BY 1
    """, (start,))
    for user_name, count in cur.fetchall():
        first = FIRST_NAMES.get(user_name)
        if first:
            results[first]['outbound_calls'] = count

    # Things done (triage emails archived, total things done for ICS)
    cur.execute(f"""
        SELECT staff_member_full_name, completed_method, COUNT(*) as cnt
        FROM {pdt}
        WHERE DATE(doable_or_enquiry_action_completed_at) = %s
          AND staff_member_full_name IS NOT NULL
        GROUP BY 1, 2
    """, (start,))
    things_by_person = defaultdict(lambda: defaultdict(int))
    for full_name, method, cnt in cur.fetchall():
        first = FIRST_NAMES.get(full_name)
        if first:
            things_by_person[first][method] = cnt

    for first, methods in things_by_person.items():
        results[first]['emails_archived'] = methods.get('email archived', 0)
        results[first]['things_done'] = sum(methods.values())
        archived = methods.get('email archived', 0)
        escalated = methods.get('email escalated', 0)
        email_total = archived + escalated
        results[first]['archive_ratio'] = archived / email_total if email_total > 0 else 0.0

    conn.close()
    return dict(results)


def pull_skips(friday):
    """Pull weekly skip counts for the reward week starting on friday.
    Returns {first_name: skip_count}."""
    import psycopg2

    from compat import get_postgres_url
    db_url = get_postgres_url()

    dates = get_weekday_dates(friday)
    start = min(dates)
    end = max(dates) + timedelta(days=1)

    conn = _connect_postgres(db_url)
    cur = conn.cursor()
    pdt = _find_pdt_table(cur)

    cur.execute(f"""
        WITH staff_map AS (
            SELECT DISTINCT staff_member_id, staff_member_full_name
            FROM {pdt}
            WHERE staff_member_full_name IS NOT NULL
        )
        SELECT sm.staff_member_full_name, COUNT(DISTINCT wa.id) as skips
        FROM public.work_allocation wa
        JOIN public.case_allocation ca ON wa.case_allocation_id = ca.id
        JOIN staff_map sm ON ca.staff_member_id = sm.staff_member_id
        WHERE wa.db_completion_status = 'cant_do'
          AND wa.completed_at >= %s AND wa.completed_at < %s
        GROUP BY 1
    """, (start, end))

    skips = {}
    for full_name, skip_count in cur.fetchall():
        first = FIRST_NAMES.get(full_name)
        if first:
            skips[first] = skip_count

    conn.close()
    return skips


# ── Timeline gap analysis ───────────────────────────────────────────────────
# Recurring meetings that legitimately interrupt work — excluded from gap
# flagging. Keyed by weekday (Mon=0 … Fri=4); values are (start, end) as
# "HH:MM". These mirror the gap_analysis Looker explore's intent: a gap that
# falls inside one of these isn't an idle gap. Lunch + 1:1s are supplied
# per-person by the caller (they vary by person/day).
STANDUP_WINDOWS = {
    1: [('09:00', '09:45')],   # Tuesday standups (two half-team slots 9:00/9:30)
    2: [('17:00', '18:00')],   # Wednesday team meeting 5–6pm
}

# CS lunch-cover band — applied every weekday. A gap that sits inside this
# window reads as lunch, not idle time. Wide on purpose: lunch start varies
# per person and we'd rather not false-flag it.
LUNCH_BAND = ('12:00', '14:00')

# 1:1 slots from the CS TL sample diary (V6 May 2026). 1:1s are stacked 9am
# starts on a 4-week rota; each person occupies a FIXED day+time slot whether
# or not they have a 1:1 that particular week, so we exclude that window every
# week (over-exclusion risk = one 1-hour window on one weekday — negligible).
# (weekday Mon=0…Fri=4, start 'HH:MM', end 'HH:MM').
ONE_TO_ONE_SLOTS = {
    # Courtney's team — Wednesday
    'Elida':   (2, '09:00', '10:00'), 'Jade': (2, '09:00', '10:00'),
    'Harry':   (2, '09:00', '10:00'),
    'Harriet': (2, '10:30', '11:30'), 'Becky': (2, '10:30', '11:30'),
    'Fionn':   (2, '10:30', '11:30'), 'Kate':  (2, '10:30', '11:30'),
    # Yasmin's team — Thursday
    'Kirsty':  (3, '09:00', '10:00'), 'Noemi': (3, '09:00', '10:00'),
    'Sophie':  (3, '09:00', '10:00'),
    'Tara':    (3, '10:30', '11:30'), 'Roseanne': (3, '10:30', '11:30'),
    'Lizzie':  (3, '10:30', '11:30'),
    # Jess's team — Wednesday
    'Lucy':    (2, '09:00', '10:00'), 'Erika': (2, '09:00', '10:00'),
    'Cris':    (2, '10:30', '11:30'), 'Maisha': (2, '10:30', '11:30'),
    'Clare':   (2, '10:30', '11:30'),
}

# A gap shorter than this (minutes) is normal task-switching — not even listed.
# 13 min, not 10: 10–12 min gaps are routine and listing them flagged nearly
# everyone, drowning out the meaningful ones.
TIMELINE_GAP_THRESHOLD_MIN = 13
# A single gap this long (minutes) is a substantial unexplained block and
# trips the Timeline suggestion to REVIEW. Shorter gaps are still listed for
# Jo to eyeball, but don't on their own flag the person.
TIMELINE_REVIEW_GAP_MIN = 30


def _to_local_naive(ts):
    """Normalise a DB timestamp to naive UK-local time so it compares cleanly
    with the naive standup/lunch windows. tz-aware → Europe/London → drop tz."""
    if ts is None:
        return None
    if ts.tzinfo is None:
        return ts
    try:
        from zoneinfo import ZoneInfo
        return ts.astimezone(ZoneInfo('Europe/London')).replace(tzinfo=None)
    except Exception:
        # Fallback: treat as UTC and drop tz (rare — zoneinfo missing)
        return ts.replace(tzinfo=None)


def pull_activity_events(friday, include_history=False):
    """Pull every timestamped work-activity event per person for the reward week.

    Mirrors the `gap_analysis` Looker explore (dashboards 356/359/360), which
    is a UNION of four Postgres sources. We query them directly so same-day
    (Friday) data is included — the Looker PDT only rebuilds overnight.

    Sources:
      1. pdt_things_done_by_person  — doable/enquiry completions
      2. public.phone_activity      — inbound/outbound call start + end
      3. public.case_allocation     — case claimed + released
      4. public.history             — system events (status changes etc.) —
         only when include_history=True. This table is huge (~2 min scan), and
         sources 1–3 already give hundreds of events/person/day, plenty to
         detect idle gaps. Off by default to keep the button responsive.

    Timestamps are normalised to naive UK-local time. Each source is queried
    defensively; if one fails the others still return.
    Returns {first_name: [(datetime, event_type), …]} sorted by timestamp.
    """
    from compat import get_postgres_url
    db_url = get_postgres_url()

    dates = get_weekday_dates(friday)
    start = min(dates)
    end = max(dates) + timedelta(days=1)

    conn = _connect_postgres(db_url)
    cur = conn.cursor()
    pdt = _find_pdt_table(cur)

    events = defaultdict(list)  # first_name -> [(ts, event_type)]

    def add(full_name, ts, etype):
        if not full_name or not ts:
            return
        first = FIRST_NAMES.get(full_name)
        if first:
            events[first].append((_to_local_naive(ts), etype))

    # staff_member_id -> full_name (same source as pull_skips)
    id_to_name = {}
    try:
        cur.execute(f"""
            SELECT DISTINCT staff_member_id, staff_member_full_name
            FROM {pdt}
            WHERE staff_member_full_name IS NOT NULL
        """)
        id_to_name = {sid: name for sid, name in cur.fetchall()}
    except Exception as e:
        logging.warning(f"pull_activity_events: staff map failed: {e}")

    # 1. Things done (pdt)
    try:
        cur.execute(f"""
            SELECT staff_member_full_name, completed_method,
                   doable_or_enquiry_action_completed_at
            FROM {pdt}
            WHERE doable_or_enquiry_action_completed_at >= %s
              AND doable_or_enquiry_action_completed_at < %s
              AND staff_member_full_name IS NOT NULL
        """, (start, end))
        for full_name, method, ts in cur.fetchall():
            add(full_name, ts, method or 'thing_done')
    except Exception as e:
        logging.warning(f"pull_activity_events: pdt events failed: {e}")

    # 2. Phone activity (start + end so long calls don't read as gaps)
    try:
        cur.execute("""
            SELECT user_name, direction, started_at, ended_at
            FROM public.phone_activity
            WHERE started_at >= %s AND started_at < %s
              AND user_name IS NOT NULL
        """, (start, end))
        for user_name, direction, started, ended in cur.fetchall():
            add(user_name, started, f'{direction}_call')
            if ended:
                add(user_name, ended, f'{direction}_call_end')
    except Exception as e:
        logging.warning(f"pull_activity_events: phone events failed: {e}")

    # 3. Case allocation (claimed + released)
    try:
        cur.execute("""
            SELECT staff_member_id, created_at, released_at
            FROM public.case_allocation
            WHERE (created_at >= %s AND created_at < %s)
               OR (released_at >= %s AND released_at < %s)
        """, (start, end, start, end))
        for sid, created, released in cur.fetchall():
            name = id_to_name.get(sid)
            if not name:
                continue
            if created and start <= created.date() <= max(dates):
                add(name, created, 'case_claimed')
            if released and start <= released.date() <= max(dates):
                add(name, released, 'case_released')
    except Exception as e:
        logging.warning(f"pull_activity_events: case_allocation events failed: {e}")

    # 4. History (system events) — huge table, opt-in only.
    if include_history:
        try:
            cur.execute("""
                SELECT logged_in_staff_member_id, event_type, created_at
                FROM public.history
                WHERE created_at >= %s AND created_at < %s
                  AND logged_in_staff_member_id IS NOT NULL
            """, (start, end))
            for sid, etype, ts in cur.fetchall():
                add(id_to_name.get(sid), ts, etype or 'history')
        except Exception as e:
            logging.warning(f"pull_activity_events: history events failed: {e}")

    conn.close()

    for first in events:
        events[first].sort(key=lambda x: x[0])
    return dict(events)


def _overlap_minutes(a_start, a_end, b_start, b_end):
    """Minutes of overlap between [a_start,a_end] and [b_start,b_end]."""
    lo = max(a_start, b_start)
    hi = min(a_end, b_end)
    if hi <= lo:
        return 0.0
    return (hi - lo).total_seconds() / 60.0


def detect_timeline_gaps(events_by_person, friday, exclusions_by_person=None,
                          threshold_min=TIMELINE_GAP_THRESHOLD_MIN):
    """Find idle gaps > threshold_min between consecutive work events.

    For each person/day, sorts their events and measures the gap between each
    consecutive pair. A gap is reported unless it's explained by a meeting:
      - STANDUP_WINDOWS (global, by weekday)
      - exclusions_by_person[first_name] = [(start_dt, end_dt), …] — lunch /
        1:1s supplied by the caller (vary per person/day)
    A gap is "explained" when the meeting windows cover all but < threshold_min
    of it (windows padded 5 min each side).

    Gaps before the first event / after the last event are NOT counted (we
    only measure inter-event idle time within the working span).

    Returns {first_name: [{'date','start','end','minutes'}, …]} — only people
    with at least one unexplained gap appear.
    """
    exclusions_by_person = exclusions_by_person or {}
    dates = get_weekday_dates(friday)
    pad = timedelta(minutes=5)
    out = {}

    for first, evs in events_by_person.items():
        person_excl = exclusions_by_person.get(first, [])
        gaps = []
        # Bucket events by date
        by_day = defaultdict(list)
        for ts, etype in evs:
            by_day[ts.date()].append(ts)
        for d in dates:
            day_ts = sorted(by_day.get(d, []))
            if len(day_ts) < 2:
                continue
            # Build exclusion windows for this weekday: standups (by weekday)
            # + the daily lunch band + this person's 1:1 slot (if it's today)
            # + any caller-supplied per-person windows.
            day_windows = list(STANDUP_WINDOWS.get(d.weekday(), [])) + [LUNCH_BAND]
            one_to_one = ONE_TO_ONE_SLOTS.get(first)
            if one_to_one and one_to_one[0] == d.weekday():
                day_windows.append((one_to_one[1], one_to_one[2]))
            windows = []
            for s_str, e_str in day_windows:
                sh, sm = (int(p) for p in s_str.split(':'))
                eh, em = (int(p) for p in e_str.split(':'))
                windows.append((datetime.combine(d, dtime(sh, sm)) - pad,
                                datetime.combine(d, dtime(eh, em)) + pad))
            for ws, we in person_excl:
                if ws.date() == d:
                    windows.append((ws - pad, we + pad))

            for prev, nxt in zip(day_ts, day_ts[1:]):
                gap_min = (nxt - prev).total_seconds() / 60.0
                if gap_min <= threshold_min:
                    continue
                covered = sum(_overlap_minutes(prev, nxt, ws, we)
                              for ws, we in windows)
                if (gap_min - covered) <= threshold_min:
                    continue  # explained by meeting/lunch
                gaps.append({
                    'date': d.isoformat(),
                    'start': prev.strftime('%H:%M'),
                    'end': nxt.strftime('%H:%M'),
                    'minutes': int(round(gap_min)),
                })
        if gaps:
            out[first] = gaps
    return out


# ── Cody edit compliance ────────────────────────────────────────────────────
# Every Cody email edit must be fed back in #cody-email-triage-feedback — a
# CS-wide rule (feedback_cody_feedback_mandatory). The quality check flags
# anyone who made substantive edits but posted no feedback that week.
CODY_FEEDBACK_CHANNEL = 'C09LDEKS2SE'   # #cody-email-triage-feedback

# difflib ratio bands (normalised draft vs sent):
#   ≥ 0.95     → sent as-is (no edit)
#   0.55–0.95  → substantive edit
#   < 0.55     → wrote their own
_CODY_ASIS_RATIO = 0.95
_CODY_EDIT_FLOOR = 0.55


def _normalise_email(text):
    """Strip greeting, sign-off/signature and quoted reply, lowercase, collapse
    whitespace — so a difflib compare reflects substance, not boilerplate."""
    import re
    if not text:
        return ''
    greeting_re = re.compile(
        r'^(hi|hello|hey|dear|good (?:morning|afternoon|evening))\b[^.!?]{0,40}?[,:]\s*',
        re.I)
    out = []
    for ln in text.replace('\r\n', '\n').split('\n'):
        s = ln.strip()
        if not s:
            continue
        low = s.lower()
        if s.startswith('>'):
            continue
        if low.startswith('on ') and 'wrote:' in low:
            break  # quoted thread
        if low.rstrip('.,!') in (
            'best wishes', 'kind regards', 'kindest regards', 'many thanks',
            'thanks', 'thank you', 'regards', 'best', 'warm regards',
            'all the best', 'speak soon', 'cheers',
        ):
            break  # sign-off / signature
        if not out:
            # strip a leading greeting *prefix* (keep the rest of the line,
            # since drafts often put greeting + body on one line)
            g = greeting_re.match(s)
            if g:
                s = s[g.end():].strip()
                if not s:
                    continue
        out.append(s)
    return re.sub(r'\s+', ' ', ' '.join(out)).strip().lower()


def _classify_cody_edit(draft, sent):
    """Return 'as_is' | 'edited' | 'wrote_own' for a draft/sent pair."""
    import difflib
    nd, ns = _normalise_email(draft), _normalise_email(sent)
    if not nd or not ns:
        return 'wrote_own'
    ratio = difflib.SequenceMatcher(None, nd, ns).ratio()
    if ratio >= _CODY_ASIS_RATIO:
        return 'as_is'
    if ratio >= _CODY_EDIT_FLOOR:
        return 'edited'
    return 'wrote_own'


# Verified query (built + run live by the data-analyst): one row per email where
# Cody drafted a reply and the CS person sent one. Sent body is the matching
# outbound inbox_activity row within ±5 min of first_replied_at.
_CODY_PAIRS_SQL = """
WITH staff AS (
  SELECT DISTINCT ON (front_id) front_id, first_name, last_name
  FROM staff_member WHERE front_id IS NOT NULL
  ORDER BY front_id, (LENGTH(username) - LENGTH(REPLACE(username, '.', ''))) ASC
),
cody AS (
  SELECT ia.message_id, ia.conversation_id, ia.first_replied_at, ia.first_replied_by,
    (etr.created_by_integration_event_details::jsonb
       -> 'payload' -> 'agent_response_data' ->> 'response_to_email') AS draft_text
  FROM email_triage_result etr
  JOIN inbox_activity ia ON etr.inbox_activity_id = ia.message_id
  WHERE etr.is_main_agent IS TRUE AND NOT etr.is_error
    AND (etr.created_by_integration_event_details::jsonb
         -> 'payload' -> 'agent_response_data' ->> 'action_to_take') = 'reply_to_email'
    AND COALESCE(etr.created_by_integration_event_details::jsonb
         -> 'payload' -> 'agent_response_data' ->> 'response_to_email', '') <> ''
    AND ia.is_inbound IS TRUE
    AND ia.first_replied_at >= %s AND ia.first_replied_at < %s
),
sent AS (
  SELECT DISTINCT ON (cody.message_id) cody.message_id, o.text AS sent_text
  FROM cody
  JOIN inbox_activity o ON o.conversation_id = cody.conversation_id
    AND o.is_inbound IS FALSE
    AND ABS(EXTRACT(EPOCH FROM (o.received_at - cody.first_replied_at))) <= 300
  ORDER BY cody.message_id,
           ABS(EXTRACT(EPOCH FROM (o.received_at - cody.first_replied_at))) ASC
)
SELECT s.first_name, s.last_name, cody.draft_text, sent.sent_text
FROM cody
JOIN sent ON sent.message_id = cody.message_id
LEFT JOIN staff s ON s.front_id = cody.first_replied_by
WHERE sent.sent_text IS NOT NULL
"""


def _resolve_slack_user_first_name(user_id, token, cache):
    """Slack user ID → CS first name (via users.info, cached). None if not CS."""
    if user_id in cache:
        return cache[user_id]
    import requests
    first = None
    try:
        r = requests.get('https://slack.com/api/users.info',
                         headers={'Authorization': f'Bearer {token}'},
                         params={'user': user_id}, timeout=15)
        data = r.json()
        if data.get('ok'):
            prof = data['user'].get('profile', {})
            cand = prof.get('first_name') or (data['user'].get('real_name') or '').split(' ')[0]
            cand = (cand or '').strip().capitalize()
            if cand in DB_NAMES:
                first = cand
    except Exception as e:
        logging.warning(f"users.info failed for {user_id}: {e}")
    cache[user_id] = first
    return first


def _count_cody_feedback_posts(friday):
    """Count #cody-email-triage-feedback posts per CS first name for the week.

    The feedback bot posts end with `Feedback from <@U…>` (a bare Slack user
    ID — no handle), so we extract the ID and resolve it to a name via
    users.info."""
    import re
    import requests
    from compat import get_slack_token

    dates = get_weekday_dates(friday)
    oldest = datetime.combine(min(dates), dtime(0, 0)).timestamp()
    latest = datetime.combine(max(dates) + timedelta(days=1), dtime(0, 0)).timestamp()

    counts = defaultdict(int)
    user_re = re.compile(r'feedback from <@([UW][A-Z0-9]+)(?:\|[^>]+)?>', re.I)
    token = get_slack_token()
    name_cache = {}
    cursor = None
    try:
        for _ in range(20):  # page-cap safety
            params = {'channel': CODY_FEEDBACK_CHANNEL, 'oldest': oldest,
                      'latest': latest, 'limit': 200}
            if cursor:
                params['cursor'] = cursor
            r = requests.get('https://slack.com/api/conversations.history',
                             headers={'Authorization': f'Bearer {token}'},
                             params=params, timeout=20)
            data = r.json()
            if not data.get('ok'):
                logging.warning(f"cody feedback history failed: {data.get('error')}")
                break
            for msg in data.get('messages', []):
                m = user_re.search(msg.get('text', '') or '')
                if not m:
                    continue
                first = _resolve_slack_user_first_name(m.group(1), token, name_cache)
                if first:
                    counts[first] += 1
            cursor = data.get('response_metadata', {}).get('next_cursor')
            if not cursor:
                break
    except Exception as e:
        logging.warning(f"cody feedback count failed: {e}")
    return dict(counts)


def pull_cody_compliance(friday):
    """Per-person Cody edit compliance for the reward week.

    Fetches every email where Cody drafted a reply and the person sent one,
    classifies each draft/sent pair (as-is / edited / wrote-own) via difflib,
    and counts the person's posts in #cody-email-triage-feedback.

    Returns {first_name: {'edits': n, 'as_is': n, 'wrote_own': n, 'posts': n}}.
    """
    from compat import get_postgres_url

    dates = get_weekday_dates(friday)
    start = min(dates)
    end = max(dates) + timedelta(days=1)

    out = defaultdict(lambda: {'edits': 0, 'as_is': 0, 'wrote_own': 0, 'posts': 0})

    try:
        conn = _connect_postgres(get_postgres_url())
        cur = conn.cursor()
        cur.execute(_CODY_PAIRS_SQL, (start, end))
        for first_name, last_name, draft, sent in cur.fetchall():
            full = f"{first_name} {last_name}" if first_name and last_name else ''
            first = FIRST_NAMES.get(full)
            if not first:
                continue  # non-CS / unmapped replier
            kind = _classify_cody_edit(draft, sent)
            key = 'as_is' if kind == 'as_is' else 'wrote_own' if kind == 'wrote_own' else 'edits'
            out[first][key] += 1
        conn.close()
    except CloudDBUnreachableError:
        raise
    except Exception as e:
        logging.warning(f"pull_cody_compliance query failed: {e}")

    for first, n in _count_cody_feedback_posts(friday).items():
        out[first]['posts'] = n

    return dict(out)


def suggest_quality_timeline(pw, gaps=None, cody=None):
    """Compute suggested quality_ok / timeline_ok + human-readable reasons.

    Quality = two signals, both must pass:
      1. Archive ratio — every single-role triage day must hit the base ratio
         (0.85). Split triage days are skipped (ratio not cleanly attributable).
      2. Cody compliance — if `cody` is given ({'edits','as_is','wrote_own',
         'posts'}), flag when the person made substantive Cody edits but posted
         no feedback in #cody-email-triage-feedback (mandatory CS rule).

    Timeline: pass if no unexplained gaps; otherwise flag for review with a
    short sample of the gaps.

    Returns (quality_suggested, quality_reason, timeline_suggested, timeline_reason).
    """
    gaps = gaps or []

    # ── Quality signal 1: archive ratio on assessable (single-role) triage days ──
    triage_days = [
        (d, dr) for d, dr in sorted(pw.days.items())
        if dr.is_working and not dr.segments
        and dr.role.startswith('Triage') and dr.archive_ratio > 0
    ]
    below = [(d, dr) for d, dr in triage_days
             if dr.archive_ratio < TRIAGE_ARCHIVE_RATIO_BASE]
    if not triage_days:
        archive_ok, archive_msg = True, 'no triage days'
    elif below:
        days_str = ', '.join(d.strftime('%a') for d, _ in below)
        lowest = min(dr.archive_ratio for _, dr in below)
        archive_ok = False
        archive_msg = f'archive <85% on {days_str} (low {lowest * 100:.0f}%)'
    else:
        avg = sum(dr.archive_ratio for _, dr in triage_days) / len(triage_days)
        archive_ok = True
        archive_msg = f'archive OK ({avg * 100:.0f}%)'

    # ── Quality signal 2: Cody edit-feedback compliance ──
    cody_ok, cody_msg = True, ''
    if cody is not None:
        edits = cody.get('edits', 0)
        posts = cody.get('posts', 0)
        if edits > 0 and posts == 0:
            cody_ok = False
            cody_msg = f'{edits} Cody edit(s), 0 feedback posts'
        elif edits > 0:
            cody_msg = f'Cody {posts} post(s) for {edits} edit(s)'
        else:
            cody_msg = 'no Cody edits'

    q_suggested = archive_ok and cody_ok
    parts = [archive_msg] + ([cody_msg] if cody_msg else [])
    q_reason = ' · '.join(p for p in parts if p)

    # ── Timeline: unexplained gaps ──
    # All gaps > 10 min are listed for review; only a substantial block
    # (≥ TIMELINE_REVIEW_GAP_MIN) trips the suggestion to REVIEW, so people
    # with only normal short gaps don't all get flagged.
    notable = sorted((g for g in gaps if g['minutes'] >= TIMELINE_REVIEW_GAP_MIN),
                     key=lambda g: -g['minutes'])
    if not gaps:
        t_suggested = True
        t_reason = f'No unexplained gaps over {TIMELINE_GAP_THRESHOLD_MIN} min'
    elif not notable:
        t_suggested = True
        t_reason = f'{len(gaps)} minor gap(s) only (all < {TIMELINE_REVIEW_GAP_MIN}m)'
    else:
        def _fmt(g):
            day = date.fromisoformat(g['date']).strftime('%a')
            return f"{day} {g['start']}–{g['end']} ({g['minutes']}m)"
        sample = '; '.join(_fmt(g) for g in notable[:3])
        more = f' +{len(notable) - 3} more' if len(notable) > 3 else ''
        t_suggested = False
        t_reason = (f'{len(notable)} gap(s) ≥ {TIMELINE_REVIEW_GAP_MIN}m to review: '
                    f'{sample}{more}')

    return q_suggested, q_reason, t_suggested, t_reason


def run_quality_timeline_checks(week_data, friday, exclusions_by_person=None):
    """Pull activity, detect gaps, and set quality/timeline suggestions on each PW.

    Suggestions are advisory. `*_suggested` / `*_reason` / `timeline_gaps` are
    always refreshed. The live `quality_ok` / `timeline_ok` gates are set to the
    suggestion ONLY when Jo hasn't manually overridden the previous suggestion
    (current value still equals the prior suggestion, or none existed yet) — so
    re-running never clobbers a manual decision.

    `exclusions_by_person`: optional {first_name: [(start_dt, end_dt), …]} of
    lunch / 1:1 windows to exclude (standups are excluded globally).

    Returns a summary list of dicts (name, quality, timeline, gaps, reasons).
    """
    events = pull_activity_events(friday)
    gaps_by_person = detect_timeline_gaps(events, friday, exclusions_by_person)

    # Cody compliance is a best-effort second quality signal — don't let a
    # failure (Slack/DB hiccup) block the timeline + archive-ratio checks.
    try:
        cody_by_person = pull_cody_compliance(friday)
    except CloudDBUnreachableError:
        raise
    except Exception as e:
        logging.warning(f"cody compliance skipped: {e}")
        cody_by_person = {}

    summary = []
    for name, pw in week_data.items():
        if pw.days_worked == 0:
            continue
        gaps = gaps_by_person.get(name, [])
        cody = cody_by_person.get(name)
        q_sug, q_reason, t_sug, t_reason = suggest_quality_timeline(pw, gaps, cody)

        # Apply without clobbering a manual override.
        if pw.quality_suggested is None or pw.quality_ok == pw.quality_suggested:
            pw.quality_ok = q_sug
        if pw.timeline_suggested is None or pw.timeline_ok == pw.timeline_suggested:
            pw.timeline_ok = t_sug

        pw.quality_suggested = q_sug
        pw.quality_reason = q_reason
        pw.timeline_suggested = t_sug
        pw.timeline_reason = t_reason
        pw.timeline_gaps = gaps

        summary.append({
            'name': name, 'quality': q_sug, 'timeline': t_sug,
            'gaps': len(gaps), 'q_reason': q_reason, 't_reason': t_reason,
        })
    return summary


# ── State persistence ───────────────────────────────────────────────────────
# Local: files under STATE_DIR (~/.claude/scheduled-tasks/reward-time/).
# Cloud: files inside the cs-scripts-state Google Drive folder.
# drive_state.{read,write}_json_either handles the routing.

def _ensure_state_dir():
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def _week_filename(friday):
    """Bare filename for a reward week — used both locally and on Drive."""
    return f"week_{friday.isoformat()}.json"


def _week_file(friday):
    """Local Path for a reward week's JSON file."""
    return STATE_DIR / _week_filename(friday)


def save_week(friday, week_data):
    """Save a week's reward data to JSON.
    week_data: {name: PersonWeek}"""
    data = {}
    for name, pw in week_data.items():
        days_ser = {}
        for d, dr in pw.days.items():
            day_dict = {
                'role': dr.role, 'target_base': dr.target_base,
                'target_stretch': dr.target_stretch, 'actual': dr.actual,
                'met_base': dr.met_base, 'met_stretch': dr.met_stretch,
                'is_working': dr.is_working, 'archive_ratio': dr.archive_ratio,
                'shift_hours': dr.shift_hours, 'metrics': dr.metrics,
            }
            if dr.segments:
                day_dict['segments'] = [
                    {'role': s.role, 'minutes': s.minutes,
                     'target_base': s.target_base, 'target_stretch': s.target_stretch,
                     'actual': s.actual, 'met_base': s.met_base, 'met_stretch': s.met_stretch,
                     'archive_ratio': s.archive_ratio}
                    for s in dr.segments
                ]
            days_ser[d.isoformat()] = day_dict
        data[name] = {
            'days': days_ser, 'skips': pw.skips,
            'quality_ok': pw.quality_ok, 'timeline_ok': pw.timeline_ok,
            'quality_suggested': pw.quality_suggested,
            'quality_reason': pw.quality_reason,
            'timeline_suggested': pw.timeline_suggested,
            'timeline_reason': pw.timeline_reason,
            'timeline_gaps': pw.timeline_gaps,
            'overrides': pw.overrides, 'override_eligible': pw.override_eligible,
            'weekly_hours': pw.weekly_hours, 'days_worked': pw.days_worked,
            'days_absent': pw.days_absent, 'expected_days': pw.expected_days,
            'tl_submitted_at': pw.tl_submitted_at,
            'tl_request_level': pw.tl_request_level,
            'tl_notes': pw.tl_notes,
            'jo_decision': pw.jo_decision,
            'jo_decision_at': pw.jo_decision_at,
            'jo_question_text': pw.jo_question_text,
        }
    from drive_state import write_json_either
    write_json_either(_week_file(friday), _week_filename(friday), data)


def load_week(friday):
    """Load a week's reward data from JSON. Returns {name: PersonWeek} or empty dict."""
    from drive_state import read_json_either
    raw = read_json_either(_week_file(friday), _week_filename(friday))
    if not raw:
        return {}
    week_data = {}
    for name, d in raw.items():
        pw = PersonWeek(name=name)
        pw.skips = d.get('skips', 0)
        pw.quality_ok = d.get('quality_ok', False)
        pw.timeline_ok = d.get('timeline_ok', False)
        pw.quality_suggested = d.get('quality_suggested', None)
        pw.quality_reason = d.get('quality_reason', '')
        pw.timeline_suggested = d.get('timeline_suggested', None)
        pw.timeline_reason = d.get('timeline_reason', '')
        pw.timeline_gaps = d.get('timeline_gaps', [])
        pw.overrides = d.get('overrides', [])
        pw.override_eligible = d.get('override_eligible', '')
        pw.weekly_hours = d.get('weekly_hours', WEEKLY_HOURS.get(name, 40))
        pw.days_worked = d.get('days_worked', 0)
        pw.days_absent = d.get('days_absent', 0)
        pw.expected_days = d.get('expected_days', pw.days_worked + pw.days_absent)
        pw.tl_submitted_at = d.get('tl_submitted_at', '')
        pw.tl_request_level = d.get('tl_request_level', '')
        pw.tl_notes = d.get('tl_notes', '')
        pw.jo_decision = d.get('jo_decision', '')
        pw.jo_decision_at = d.get('jo_decision_at', '')
        pw.jo_question_text = d.get('jo_question_text', '')
        for date_str, dr_raw in d.get('days', {}).items():
            day_date = date.fromisoformat(date_str)
            dr = DayResult(
                role=dr_raw.get('role', ''),
                target_base=dr_raw.get('target_base', 0),
                target_stretch=dr_raw.get('target_stretch', 0),
                actual=dr_raw.get('actual', 0),
                met_base=dr_raw.get('met_base', False),
                met_stretch=dr_raw.get('met_stretch', False),
                is_working=dr_raw.get('is_working', True),
                archive_ratio=dr_raw.get('archive_ratio', 0.0),
                shift_hours=dr_raw.get('shift_hours', DAILY_HOURS.get(name, STANDARD_SHIFT_HOURS)),
                metrics=dr_raw.get('metrics', {}),
            )
            # Deserialize segments if present
            for seg_raw in dr_raw.get('segments', []):
                dr.segments.append(RoleSegment(
                    role=seg_raw.get('role', ''),
                    minutes=seg_raw.get('minutes', 240),
                    target_base=seg_raw.get('target_base', 0),
                    target_stretch=seg_raw.get('target_stretch', 0),
                    actual=seg_raw.get('actual', 0),
                    met_base=seg_raw.get('met_base', False),
                    met_stretch=seg_raw.get('met_stretch', False),
                    archive_ratio=seg_raw.get('archive_ratio', 0.0),
                ))
            pw.days[day_date] = dr
        week_data[name] = pw
    return week_data


# ── Build / update week from rota + actuals ─────────────────────────────────

def build_week(friday, assignments_fri=None, assignments_mon_thu=None,
               actuals_by_date=None, skips=None):
    """Build PersonWeek records from rota assignments and optional actuals.

    A reward week (Fri-Thu) spans two rota weeks:
    - Friday comes from the previous rota week (assignments_fri, day_idx=4)
    - Mon-Thu come from the current rota week (assignments_mon_thu, day_idx=0-3)

    If only one assignments dict is provided via assignments_fri, it's used for all days.

    Returns: {name: PersonWeek}
    """
    if actuals_by_date is None:
        actuals_by_date = {}
    if skips is None:
        skips = {}
    if assignments_mon_thu is None:
        assignments_mon_thu = assignments_fri or {}
    if assignments_fri is None:
        assignments_fri = assignments_mon_thu

    week_dates = get_weekday_dates(friday)
    date_to_rota_idx = {}
    for d in week_dates:
        date_to_rota_idx[d] = d.weekday()

    absence_roles = {'Annual leave', 'Unplanned absence', 'Non working day', 'Training'}
    week_data = {}

    for name in ALL_AGENTS:
        pw = PersonWeek(name=name)
        pw.weekly_hours = WEEKLY_HOURS.get(name, 40)
        pw.skips = skips.get(name, 0)

        fri_roles = assignments_fri.get(name, {})
        mon_thu_roles = assignments_mon_thu.get(name, {})
        days_worked = 0
        days_absent = 0      # AL + sick only (not NWD, not bank hols)
        expected_days = 0     # days they should have worked

        person_daily_hours = DAILY_HOURS.get(name, STANDARD_SHIFT_HOURS)

        for d in week_dates:
            di = date_to_rota_idx[d]
            source = fri_roles if di == 4 else mon_thu_roles
            role = source.get(di, '')

            dr = DayResult()
            dr.role = role
            dr.shift_hours = person_daily_hours

            is_bank_hol = d in UK_BANK_HOLIDAYS
            is_nwd = (role == 'Non working day') or not role

            if role in absence_roles or not role:
                dr.is_working = False
                if not is_bank_hol and not is_nwd:
                    # Genuine absence (AL or sick on a day they should have worked)
                    days_absent += 1
                    expected_days += 1
                # NWD and bank holidays don't count toward expected or absent
            else:
                dr.is_working = True
                days_worked += 1
                expected_days += 1

                targets = _lookup_targets(role)
                if targets:
                    # Pro-rate targets by shift hours
                    ratio = dr.shift_hours / STANDARD_SHIFT_HOURS
                    dr.target_base = round(targets[0] * ratio)
                    dr.target_stretch = round(targets[1] * ratio)
                    metric = targets[2]

                    day_actuals = actuals_by_date.get(d, {}).get(name, {})
                    dr.metrics = dict(day_actuals)  # store all raw metrics for splits
                    dr.actual = day_actuals.get(metric, 0)

                    if role.startswith('Triage'):
                        dr.archive_ratio = day_actuals.get('archive_ratio', 0.0)

                    dr.met_base = dr.actual >= dr.target_base
                    dr.met_stretch = dr.actual >= dr.target_stretch

                    if role.startswith('Triage'):
                        dr.met_base = dr.met_base and dr.archive_ratio >= TRIAGE_ARCHIVE_RATIO_BASE
                        dr.met_stretch = dr.met_stretch and dr.archive_ratio >= TRIAGE_ARCHIVE_RATIO_STRETCH

            pw.days[d] = dr

        pw.days_worked = days_worked
        pw.days_absent = days_absent
        pw.expected_days = expected_days
        week_data[name] = pw

    return week_data


def _role_category(role):
    """Reduce a role string to its primary category, for matching during rota sync.

    Used to recognise that splits the user has set up are still compatible
    with a new rota role string — e.g. a 'Triage only' segment should
    survive the rota changing to 'Triage and Video Calls', because both are
    the same underlying role category.

    Examples:
      'Triage only'                  → 'Triage'
      'Triage and Video Calls'       → 'Triage'
      'Triage + lender chasing'      → 'Triage'
      'Inbound phones'               → 'Inbound phones'
      'Inbound phones + Email Health'→ 'Inbound phones'
      'Case setup only'              → 'Case setup'
      'Lender chasing'               → 'Lender chasing'
      'Reward time (prev week)'      → 'Reward time'
    """
    s = (role or '').strip()
    if '(' in s:
        s = s.split('(', 1)[0].strip()
    # Order matters: ' + ' first (keeps "Inbound phones" head), then
    # narrower separators that should strip suffixes.
    for sep in (' + ', ' and ', ', ', ' only'):
        if sep in s:
            s = s.split(sep, 1)[0].strip()
    return s


def sync_rota_into_week(week_data, friday, assignments_fri=None, assignments_mon_thu=None):
    """Overlay the current rota onto an existing week without losing TL inputs.

    Updates each day's role / is_working / shift_hours / targets to match the
    rota. Preserves: dr.metrics (actuals), pw.quality_ok, pw.timeline_ok,
    pw.skips, pw.overrides, pw.override_eligible, all TL submission fields,
    and pw.jo_decision.

    When a day's role changes:
      - If the day was previously split AND any segment's base role still
        matches the new rota role's base, the split is preserved (the user
        intentionally split it; minor rota tweaks like adding "+ Webchat"
        should not wipe their work). The synthetic role label and aggregate
        targets are refreshed.
      - Otherwise split segments are cleared and targets are recalculated
        from the new single role.
      - met_base / met_stretch are re-evaluated against existing actuals.

    When a day flips to absence/NWD: is_working=False, role updated, targets=0,
    segments cleared, actual=0.

    Returns a list of (name, date, old_role, new_role) tuples describing changes.
    """
    if assignments_mon_thu is None:
        assignments_mon_thu = assignments_fri or {}
    if assignments_fri is None:
        assignments_fri = assignments_mon_thu

    week_dates = get_weekday_dates(friday)
    absence_roles = {'Annual leave', 'Unplanned absence', 'Non working day', 'Training'}
    changes = []

    for name, pw in week_data.items():
        fri_roles = assignments_fri.get(name, {})
        mon_thu_roles = assignments_mon_thu.get(name, {})
        days_worked = 0
        days_absent = 0
        expected_days = 0
        person_daily_hours = DAILY_HOURS.get(name, STANDARD_SHIFT_HOURS)

        for d in week_dates:
            di = d.weekday()
            source = fri_roles if di == 4 else mon_thu_roles
            new_role = source.get(di, '')

            # Ensure the day exists in pw.days
            if d not in pw.days:
                pw.days[d] = DayResult(role='', shift_hours=person_daily_hours)
            dr = pw.days[d]
            old_role = dr.role

            is_bank_hol = d in UK_BANK_HOLIDAYS
            is_nwd = (new_role == 'Non working day') or not new_role
            role_changed = (old_role != new_role)

            if new_role in absence_roles or not new_role:
                # Flipping to absence — clear working state
                dr.is_working = False
                dr.role = new_role
                dr.target_base = 0
                dr.target_stretch = 0
                dr.actual = 0
                dr.met_base = False
                dr.met_stretch = False
                dr.archive_ratio = 0.0
                dr.segments = []
                if not is_bank_hol and not is_nwd:
                    days_absent += 1
                    expected_days += 1
            else:
                dr.is_working = True
                days_worked += 1
                expected_days += 1

                # Does the existing split still match the new rota role?
                # We compare role categories (e.g. 'Triage only',
                # 'Triage and Video Calls' and 'Triage + lender chasing'
                # all collapse to 'Triage').
                new_cat = _role_category(new_role)
                keep_splits = False
                if dr.segments:
                    for seg in dr.segments:
                        if _role_category(seg.role) == new_cat:
                            keep_splits = True
                            break

                if keep_splits:
                    # Preserve user-intentioned split.
                    # Refresh the synthetic role label + aggregate targets,
                    # and re-apply actuals to each segment.
                    dr.role = ' / '.join(_short_role(s.role) for s in dr.segments)
                    dr.target_base = sum(s.target_base for s in dr.segments)
                    dr.target_stretch = sum(s.target_stretch for s in dr.segments)
                    if dr.metrics:
                        _update_segments_actuals(dr, dr.metrics)
                    else:
                        _refresh_met_from_segments(dr)
                    # Not treated as a change for the purposes of the
                    # "synced N role changes" summary, even if the synthetic
                    # label differs from old_role — the split is intact.
                    role_changed = False
                else:
                    # No splits (or splits no longer compatible) — wipe segments
                    # and apply the new single role directly.
                    if dr.segments:
                        dr.segments = []
                    dr.role = new_role
                    targets = _lookup_targets(new_role)
                    if targets:
                        ratio = dr.shift_hours / STANDARD_SHIFT_HOURS if dr.shift_hours else 1.0
                        dr.target_base = round(targets[0] * ratio)
                        dr.target_stretch = round(targets[1] * ratio)
                        metric = targets[2]
                        if dr.metrics:
                            dr.actual = dr.metrics.get(metric, 0)
                            if new_role.startswith('Triage'):
                                dr.archive_ratio = dr.metrics.get('archive_ratio', 0.0)
                            dr.met_base = dr.actual >= dr.target_base
                            dr.met_stretch = dr.actual >= dr.target_stretch
                            if new_role.startswith('Triage'):
                                dr.met_base = dr.met_base and dr.archive_ratio >= TRIAGE_ARCHIVE_RATIO_BASE
                                dr.met_stretch = dr.met_stretch and dr.archive_ratio >= TRIAGE_ARCHIVE_RATIO_STRETCH

            if role_changed:
                changes.append((name, d, old_role, new_role))

        pw.days_worked = days_worked
        pw.days_absent = days_absent
        pw.expected_days = expected_days

    return changes


def _parse_time_to_hours(s: str):
    """'1:30pm' / '17:30' / '4' / '8.30' → fractional hours 0-24 (or None)."""
    s = s.strip().lower().replace(' ', '')
    is_pm = is_am = False
    if s.endswith('pm'):
        is_pm = True; s = s[:-2]
    elif s.endswith('am'):
        is_am = True; s = s[:-2]
    s = s.replace('.', ':')
    if ':' in s:
        try:
            h, m = s.split(':', 1)
            hours = int(h) + int(m) / 60
        except ValueError:
            return None
    else:
        try:
            hours = float(s)
        except ValueError:
            return None
    if is_pm and hours < 12:
        hours += 12
    elif is_am and hours == 12:
        hours = 0
    return hours


def parse_time_range_hours(s: str):
    """Parse a Daily Notes time range → duration in fractional hours.

    Handles all the formats Jo's team uses:
      '2 - 5'           → 3.0    (afternoon — both < 8 → both PM)
      '1:30 - 5'        → 3.5    (afternoon)
      '11:45-2'         → 2.25   (crosses noon)
      '8-10:30'         → 2.5    (morning — both ≥ 8)
      '4-5pm'           → 1.0    (PM marker on end → start also PM)
      '9:00 - 5pm'      → 8.0    (PM end, start at 9 stays AM)
      '13:30–14:00'     → 0.5    (en-dash, 24h)

    Returns None if unparseable or duration ≤ 0.
    """
    import re as _re
    if not s:
        return None
    parts = _re.split(r'\s*[-–]\s*', s.strip(), maxsplit=1)
    if len(parts) != 2:
        return None
    raw_a, raw_b = parts[0].strip().lower(), parts[1].strip().lower()
    a = _parse_time_to_hours(raw_a)
    b = _parse_time_to_hours(raw_b)
    if a is None or b is None:
        return None
    a_meridian = raw_a.endswith('am') or raw_a.endswith('pm')
    b_meridian = raw_b.endswith('am') or raw_b.endswith('pm')
    if not a_meridian and not b_meridian:
        if a < 8 and b < 8:
            a += 12; b += 12
        elif b < a:
            b += 12
    elif b_meridian and not a_meridian:
        if a < 12 and (a + 12) < b:
            a += 12
    duration = b - a
    return duration if duration > 0 else None


def autofill_splits_from_notes(week_data, notes_by_date, *,
                                 note_keywords, segment_role,
                                 segment_label=None):
    """Generic autofill — splits days based on a keyword found in Daily Notes.

    Idempotent:
      - skips days that already have the same segment_role
      - skips days where the person isn't working (absence / NWD)
      - skips entries whose time range can't be parsed
      - skips split hours that would leave less than 30 min for the main role

    Args:
        week_data: {name: PersonWeek}
        notes_by_date: {date: [entry_dict]} where entry has 'name', 'time', 'note'
        note_keywords: list of lowercase substrings to match against the note
            (entry matches if ANY keyword is a substring)
        segment_role: full role string for the new segment (must be in ROLE_TARGETS)
        segment_label: short label for messages (defaults to segment_role)

    Returns: [{'name', 'date', 'hours', 'status': 'applied'|'skipped', 'reason'}]
    """
    if segment_label is None:
        segment_label = segment_role
    keywords = [k.lower() for k in note_keywords]
    results = []
    for d, entries in notes_by_date.items():
        for entry in entries:
            note = (entry.get('note') or '').strip().lower()
            if not any(k in note for k in keywords):
                continue
            name = (entry.get('name') or '').strip()
            time_str = entry.get('time') or ''
            hours = parse_time_range_hours(time_str)
            if hours is None or hours <= 0:
                results.append({'name': name, 'date': d, 'hours': None,
                                'status': 'skipped',
                                'reason': f"couldn't parse time '{time_str}'"})
                continue
            pw = week_data.get(name)
            if not pw:
                results.append({'name': name, 'date': d, 'hours': hours,
                                'status': 'skipped',
                                'reason': 'person not in week_data'})
                continue
            dr = pw.days.get(d)
            if not dr or not dr.is_working:
                results.append({'name': name, 'date': d, 'hours': hours,
                                'status': 'skipped',
                                'reason': 'day not working / not in week'})
                continue
            # Already split with this segment role?
            if dr.segments and any(s.role == segment_role for s in dr.segments):
                results.append({'name': name, 'date': d, 'hours': hours,
                                'status': 'skipped',
                                'reason': f'already has {segment_label} segment'})
                continue
            if hours >= dr.shift_hours - 0.4:
                results.append({'name': name, 'date': d, 'hours': hours,
                                'status': 'skipped',
                                'reason': f"{segment_label} {hours:.2f}h ≥ shift {dr.shift_hours}h"})
                continue
            main_hours = round(dr.shift_hours - hours, 2)
            spec = [(dr.role, main_hours), (segment_role, round(hours, 2))]
            ok = split_day(pw, d, spec)
            if not ok:
                results.append({'name': name, 'date': d, 'hours': hours,
                                'status': 'skipped',
                                'reason': 'split_day returned False'})
                continue
            add_override(pw, f'split ({d.strftime("%a %d/%m")})',
                          'single', dr.role,
                          f"Autofilled from Daily Notes: {hours:.2f}h {segment_label}")
            results.append({'name': name, 'date': d, 'hours': hours,
                            'status': 'applied',
                            'reason': f"split {main_hours}h main / {hours}h {segment_label}"})
    return results


def autofill_reward_splits_from_notes(week_data, notes_by_date):
    """Convenience wrapper: autofills reward-time splits from Daily Notes."""
    return autofill_splits_from_notes(
        week_data, notes_by_date,
        note_keywords=['reward time'],
        segment_role='Reward time (prev week)',
        segment_label='reward time',
    )


def autofill_appointment_splits_from_notes(week_data, notes_by_date):
    """Convenience wrapper: autofills appointment splits from Daily Notes.
    Matches both 'appointment' (Medical appointment, School appointment) and
    'appt' (Dentist appt, School appt)."""
    return autofill_splits_from_notes(
        week_data, notes_by_date,
        note_keywords=['appointment', 'appt'],
        segment_role='Appointment',
        segment_label='appointment',
    )


def resync_autofilled_splits_from_notes(week_data, notes_by_date):
    """Two-way sync: revert stale autofilled splits + apply current ones.

    Use case: a user moves a 'Reward time' entry in Daily Notes from
    Tuesday to Thursday. Without this function:
      - the Tuesday split stays in state (preserved by sync_rota_into_week)
      - the Thursday split gets added on the next autofill
      - the person ends up with TWO splits, one stale

    With this function:
      - the Tuesday split is detected as 'autofilled but Daily Notes no
        longer agrees' and reverted via unsplit_day back to the rota role
      - autofill then runs as normal and applies the Thursday split

    Splits applied manually via the UI are NOT touched — only overrides
    whose reason starts with 'Autofilled from Daily Notes' are eligible
    for revert. The override's reason is annotated with '[reverted — …]'
    so a later resync doesn't try to revert it again.

    Args:
        week_data: {name: PersonWeek}
        notes_by_date: {date: [entry_dict]} where entry has 'name',
            'time', 'note' — same shape autofill_*_from_notes expects.

    Returns:
        {
          'reverted': [{'name', 'date', 'reason'}, ...],
          'applied':  [{'name', 'date', 'hours', 'status', 'reason'}, ...]
                       — same shape autofill_*_from_notes returns,
                       covering both reward-time and appointment runs
        }
    """
    import re
    from datetime import datetime, date as _date

    reverted = []

    # Build (name, date) → list of lowercased note text for matching
    notes_index: dict = {}
    for d, entries in notes_by_date.items():
        for e in entries:
            n = (e.get('name') or '').strip()
            note_text = (e.get('note') or '').strip().lower()
            notes_index.setdefault((n, d), []).append(note_text)

    fallback_year = (next(iter(notes_by_date.keys())).year
                     if notes_by_date else _date.today().year)

    field_re = re.compile(r'^split\s+\(\w+\s+(\d{1,2})/(\d{1,2})\)$')

    for name, pw in week_data.items():
        for ov in pw.overrides:
            reason = ov.get('reason') or ''
            if not reason.startswith('Autofilled from Daily Notes:'):
                continue
            if '[reverted' in reason:
                continue   # already reverted in a previous resync

            field = (ov.get('field') or '').strip()
            m = field_re.match(field)
            if not m:
                continue
            dd, mm = int(m.group(1)), int(m.group(2))
            try:
                d = _date(fallback_year, mm, dd)
            except ValueError:
                continue

            # What kind of autofill was this?
            lower = reason.lower()
            if 'reward time' in lower:
                keyword = 'reward time'
            elif 'appointment' in lower or 'appt' in lower:
                keyword = 'appointment'
            else:
                continue   # unknown — leave alone

            dr = pw.days.get(d)
            if not dr or not dr.segments:
                continue   # split already gone

            # Does Daily Notes still have a matching entry?
            # For appointment matching we accept either 'appointment' or 'appt'.
            day_notes = notes_index.get((name, d), [])
            if keyword == 'reward time':
                still_present = any('reward time' in n for n in day_notes)
            else:
                still_present = any(('appointment' in n) or ('appt' in n)
                                    for n in day_notes)
            if still_present:
                continue

            # Revert. Find the "main" non-suffix role from the segments.
            main_role = None
            suffix_prefixes = (
                'Reward time', 'Appointment', 'Part day AL', 'Training',
            )
            for seg in dr.segments:
                if not seg.role.startswith(suffix_prefixes):
                    main_role = seg.role
                    break
            if not main_role:
                continue   # safety: can't determine main role

            unsplit_day(pw, d, main_role)
            ov['reverted_at'] = datetime.now().isoformat()
            ov['reason'] = (reason
                            + ' [reverted — Daily Notes no longer has '
                              'a matching entry on this day]')

            reverted.append({
                'name': name,
                'date': d,
                'reason': f"Daily Notes no longer has a {keyword} "
                          f"entry on {d.strftime('%a %d/%m')}",
            })

    # Now apply current Daily Notes (idempotent — only adds missing splits)
    reward_results = autofill_reward_splits_from_notes(week_data, notes_by_date)
    appt_results = autofill_appointment_splits_from_notes(week_data, notes_by_date)

    return {
        'reverted': reverted,
        'applied': reward_results + appt_results,
    }


def _role_needs_cover(role: str) -> bool:
    """Cover is only required for Inbound phones or Triage + lender chasing.

    Per Jo's Daily Notes rule: triage / chasing / ICS do not need cover for
    short absences; only the customer-facing phone slots do.
    """
    if not role:
        return False
    if role.startswith('Inbound phones'):
        return True
    if role == 'Triage + lender chasing':
        return True
    return False


def find_reward_and_half_days(week_data, the_date):
    """For a given date, return who is taking reward time and who is on a half day.

    Returns:
      {
        'reward_time': [{'name', 'hours', 'main_role', 'cover_needed'}, ...],
        'half_day':    [{'name', 'hours', 'role', 'cover_needed'}, ...],
      }

    Reward time today =
        a 'Reward time (prev week)' segment on the day, or the whole day's role.
    Half day today =
        is_working AND shift_hours < their normal daily hours AND not taking reward time.
    """
    reward_time = []
    half_day = []

    for name, pw in week_data.items():
        dr = pw.days.get(the_date)
        if not dr:
            continue

        normal_hours = DAILY_HOURS.get(name, STANDARD_SHIFT_HOURS)

        # Reward time taken — full or partial day
        reward_hours = 0.0
        main_role = dr.role
        if dr.segments:
            for seg in dr.segments:
                if seg.role.startswith('Reward time'):
                    reward_hours += seg.minutes / 60
                else:
                    main_role = seg.role  # non-reward segment is "main" today
        elif dr.role.startswith('Reward time'):
            reward_hours = dr.shift_hours

        if reward_hours > 0:
            reward_time.append({
                'name': name,
                'hours': round(reward_hours, 2),
                'main_role': main_role,
                'cover_needed': _role_needs_cover(main_role),
            })
            continue  # don't double-count as half day

        # Half day — working but fewer hours than usual
        if dr.is_working and dr.shift_hours < normal_hours:
            half_day.append({
                'name': name,
                'hours': float(dr.shift_hours),
                'role': dr.role,
                'cover_needed': _role_needs_cover(dr.role),
            })

    return {'reward_time': reward_time, 'half_day': half_day}


def update_day_actuals(week_data, target_date, actuals):
    """Update a single day's actuals in existing week data.
    actuals: {name: {metric: value}} from pull_day_data."""
    for name, pw in week_data.items():
        if target_date not in pw.days:
            continue
        dr = pw.days[target_date]
        if not dr.is_working:
            continue

        person_actuals = actuals.get(name, {})

        # Split-role day: update each segment independently
        if dr.segments:
            _update_segments_actuals(dr, person_actuals)
            continue

        # Single-role day
        dr.metrics = dict(person_actuals)  # store all raw metrics for splits
        role = dr.role
        targets = _lookup_targets(role)
        if not targets:
            continue

        metric = targets[2]
        dr.actual = person_actuals.get(metric, 0)

        if role.startswith('Triage'):
            dr.archive_ratio = person_actuals.get('archive_ratio', 0.0)

        dr.met_base = dr.actual >= dr.target_base
        dr.met_stretch = dr.actual >= dr.target_stretch

        if role.startswith('Triage'):
            dr.met_base = dr.met_base and dr.archive_ratio >= TRIAGE_ARCHIVE_RATIO_BASE
            dr.met_stretch = dr.met_stretch and dr.archive_ratio >= TRIAGE_ARCHIVE_RATIO_STRETCH


def _update_segments_actuals(dr, person_actuals):
    """Update actuals for a split-role DayResult from its segments.
    Note: archive ratio is NOT checked on split days because the ratio
    covers all work (both roles), making it artificially low for triage.
    Jo reviews quality manually via the quality tick."""
    if person_actuals:
        dr.metrics = dict(person_actuals)  # store raw metrics
    all_base = True
    all_stretch = True
    for seg in dr.segments:
        targets = _lookup_targets(seg.role)
        if not targets:
            continue
        metric = targets[2]
        seg.actual = person_actuals.get(metric, 0)
        if seg.role.startswith('Triage'):
            seg.archive_ratio = person_actuals.get('archive_ratio', 0.0)
        # On split days, only check the count target — archive ratio can't be
        # cleanly attributed to just the triage portion
        seg.met_base = seg.actual >= seg.target_base
        seg.met_stretch = seg.actual >= seg.target_stretch
        if not seg.met_base:
            all_base = False
            all_stretch = False
        elif not seg.met_stretch:
            all_stretch = False
    # Aggregate productive throughput across segments. Reward time / Appointment /
    # Training segments have target_base=0 and their seg.actual is in different
    # units (minutes), so exclude them — otherwise dr.actual would mix calls + minutes.
    dr.actual = sum(s.actual for s in dr.segments if s.target_base > 0)
    dr.met_base = all_base
    dr.met_stretch = all_stretch


# ── Day adjustment helpers ─────────────────────────────────────────────────

def adjust_shift_hours(pw, target_date, new_hours, actuals=None):
    """Change shift hours for a day (partial day pro-rata).
    Recalculates targets and met_base/met_stretch.
    actuals: optional {metric: value} to refresh from."""
    dr = pw.days.get(target_date)
    if not dr or not dr.is_working:
        return
    old_hours = dr.shift_hours
    dr.shift_hours = new_hours

    if dr.segments:
        # Redistribute segment minutes proportionally
        total_old_mins = sum(s.minutes for s in dr.segments)
        total_new_mins = int(new_hours * 60)
        for seg in dr.segments:
            seg.minutes = round(seg.minutes / total_old_mins * total_new_mins) if total_old_mins else total_new_mins // len(dr.segments)
            _recalc_segment_targets(seg)
        _update_segments_actuals(dr, actuals or {}) if actuals else _refresh_met_from_segments(dr)
    else:
        # Single-role day: recalculate targets
        targets = _lookup_targets(dr.role)
        if targets:
            ratio = new_hours / STANDARD_SHIFT_HOURS
            dr.target_base = round(targets[0] * ratio)
            dr.target_stretch = round(targets[1] * ratio)
            dr.met_base = dr.actual >= dr.target_base
            dr.met_stretch = dr.actual >= dr.target_stretch
            if dr.role.startswith('Triage'):
                dr.met_base = dr.met_base and dr.archive_ratio >= TRIAGE_ARCHIVE_RATIO_BASE
                dr.met_stretch = dr.met_stretch and dr.archive_ratio >= TRIAGE_ARCHIVE_RATIO_STRETCH
    return old_hours


def split_day(pw, target_date, segments_spec, actuals=None):
    """Split a working day into 2 or 3 role segments.

    segments_spec: list of (role, hours) tuples — at least 2.
      Hours may be fractional; the LAST segment absorbs any rounding remainder
      so segment minutes always sum to the day's full shift.
    actuals: optional {metric: value} to apply.
    Returns True on success."""
    dr = pw.days.get(target_date)
    if not dr or not dr.is_working:
        return False
    if len(segments_spec) < 2:
        return False

    total_mins = int(dr.shift_hours * 60)
    minutes_per_seg = [int(round(h * 60)) for _, h in segments_spec[:-1]]
    minutes_per_seg.append(total_mins - sum(minutes_per_seg))

    new_segments = []
    for (role, _), mins in zip(segments_spec, minutes_per_seg):
        seg = RoleSegment(role=role, minutes=mins)
        _recalc_segment_targets(seg)
        new_segments.append(seg)

    dr.role = ' / '.join(_short_role(s.role) for s in new_segments)
    dr.segments = new_segments
    dr.target_base = sum(s.target_base for s in new_segments)
    dr.target_stretch = sum(s.target_stretch for s in new_segments)

    metrics = actuals or dr.metrics
    if metrics:
        _update_segments_actuals(dr, metrics)
    else:
        _refresh_met_from_segments(dr)
    return True


def unsplit_day(pw, target_date, role, actuals=None):
    """Remove a split and revert to a single role for the day."""
    dr = pw.days.get(target_date)
    if not dr or not dr.is_working:
        return False
    dr.segments = []
    dr.role = role
    targets = _lookup_targets(role)
    if targets:
        ratio = dr.shift_hours / STANDARD_SHIFT_HOURS
        dr.target_base = round(targets[0] * ratio)
        dr.target_stretch = round(targets[1] * ratio)
    if actuals:
        metric = targets[2] if targets else None
        if metric:
            dr.actual = actuals.get(metric, 0)
            if role.startswith('Triage'):
                dr.archive_ratio = actuals.get('archive_ratio', 0.0)
            dr.met_base = dr.actual >= dr.target_base
            dr.met_stretch = dr.actual >= dr.target_stretch
            if role.startswith('Triage'):
                dr.met_base = dr.met_base and dr.archive_ratio >= TRIAGE_ARCHIVE_RATIO_BASE
                dr.met_stretch = dr.met_stretch and dr.archive_ratio >= TRIAGE_ARCHIVE_RATIO_STRETCH
    return True


def _recalc_segment_targets(seg):
    """Set a segment's targets based on its role and minutes."""
    targets = _lookup_targets(seg.role)
    if targets:
        ratio = seg.minutes / (STANDARD_SHIFT_HOURS * 60)
        seg.target_base = round(targets[0] * ratio)
        seg.target_stretch = round(targets[1] * ratio)


def _refresh_met_from_segments(dr):
    """Recalculate dr.met_base/met_stretch from segment state."""
    all_base = True
    all_stretch = True
    for seg in dr.segments:
        if not seg.met_base:
            all_base = False
            all_stretch = False
        elif not seg.met_stretch:
            all_stretch = False
    dr.actual = sum(s.actual for s in dr.segments if s.target_base > 0)
    dr.met_base = all_base
    dr.met_stretch = all_stretch


def _short_role(role):
    """Shorten role name for display in split labels."""
    shorts = {
        'Inbound phones': 'Phones', 'Triage only': 'Triage',
        'Triage + lender chasing': 'Triage+LC', 'Chasing': 'Chasing',
        'Case setup only': 'ICS',
    }
    for full, short in shorts.items():
        if role.startswith(full):
            return short
    return role


SPLITTABLE_ROLES = [
    'Inbound phones', 'Triage only', 'Triage + lender chasing',
    'Chasing', 'Case setup only',
    'Training', 'Appointment', 'Reward time (prev week)', 'Part day AL',
]


# Segment roles that come from Daily Notes (reward time + appointments), NOT
# from Slack moves. apply_moves_to_day preserves these when it re-derives a
# day from the move list. Training and part-day leave are now Slack moves, so
# they're deliberately NOT protected — clearing the move clears them, same as
# a role switch.
_PROTECTED_SEGMENT_PREFIXES = ('Reward time', 'Appointment')


def apply_moves_to_day(pw, target_date, moves, *, noise_floor_min=30):
    """Re-derive a person's day from the *current* captured role moves.

    Idempotent: each call rebuilds the day from scratch — the planned rota
    role for the working portion, plus a segment for each moved-to role,
    plus any protected segments (reward time / appointment / part-day leave
    / training) carried over from Daily Notes. This means clearing the moves
    in Slack and re-applying always reflects exactly the current move list,
    with no stale leftovers — no separate 'Sync rota' step needed.

    For each move on this person ≥ `noise_floor_min`:
        - Aggregates total time on each moved-to role
        - Rebuilds segments: rota remainder + moved roles + protected
        - Calls split_day() (which overwrites segments wholesale)

    Behaviour:
      - No segments + moves        → split fresh
      - Prior apply-moves split    → re-derived from the new move list
      - Reward/appt-only split     → moves layered on, reward/appt preserved
      - No moves + prior apply      → collapses back to planned rota (+protected)
      - Manual TL split (non-protected segments, no apply-moves override)
                                   → left untouched

    Skips: non-working days, moves with no start/end window, moves shorter
    than noise_floor_min.

    Returns {'applied': bool, 'reason': str, 'segments': [(role, hours), …]}.
    """
    dr = pw.days.get(target_date)
    if not dr or not dr.is_working:
        return {'applied': False, 'reason': 'not working', 'segments': []}

    day_tag = target_date.strftime('%a %d/%m')
    had_prior_apply = any(
        (o.get('field') or '').startswith(f'apply_moves ({day_tag})')
        for o in pw.overrides
    )

    # Split the current segments into protected (Daily-Notes-derived) and
    # move-derived (everything else).
    protected_segs = [(s.role, s.minutes / 60.0)
                      for s in dr.segments
                      if s.role.startswith(_PROTECTED_SEGMENT_PREFIXES)]
    protected_hours = sum(h for _, h in protected_segs)
    non_protected = [s for s in dr.segments
                     if not s.role.startswith(_PROTECTED_SEGMENT_PREFIXES)]

    # Guard: a multi-segment non-protected split that we didn't create is a
    # manual TL split — leave it alone.
    if len(non_protected) > 1 and not had_prior_apply:
        return {'applied': False, 'reason': 'manually split — left alone',
                'segments': []}

    # The planned rota role for the working portion. If the day is currently
    # split, the (single) non-protected segment carries it; else dr.role —
    # but dr.role may be a synthetic 'A / B' label, so take its head.
    if non_protected:
        rota_role = non_protected[0].role
    else:
        rota_role = dr.role.split(' / ')[0] if ' / ' in dr.role else dr.role

    old_label = dr.role

    # Gather this person's qualifying moves.
    person_moves = []
    for m in moves:
        if m.get('name') != pw.name:
            continue
        if m.get('action') not in (None, 'move'):
            continue
        start = m.get('start_time') or ''
        end = m.get('end_time') or ''
        if ':' not in start or ':' not in end:
            continue
        try:
            sh, sm = (int(p) for p in start.split(':'))
            eh, em = (int(p) for p in end.split(':'))
        except ValueError:
            continue
        dur_min = (eh * 60 + em) - (sh * 60 + sm)
        if dur_min < noise_floor_min:
            continue
        person_moves.append({'to_role': m.get('to_role'), 'duration_min': dur_min})

    # No qualifying moves now.
    if not person_moves:
        if had_prior_apply and dr.segments:
            # A previous apply split this day but the moves are gone — collapse
            # back to the planned rota (keeping any protected segments).
            if protected_segs:
                rota_hours = round(dr.shift_hours - protected_hours, 2)
                spec = [(rota_role, rota_hours)] + \
                       [(r, round(h, 2)) for r, h in protected_segs]
                split_day(pw, target_date, spec)
            else:
                unsplit_day(pw, target_date, rota_role)
            add_override(pw, f'apply_moves ({day_tag})', old_label, dr.role,
                          'Re-derived from moves: none now — reset to planned rota')
            return {'applied': True, 'reason': 'reset to planned rota (no moves)',
                    'segments': []}
        return {'applied': False, 'reason': 'no qualifying moves', 'segments': []}

    # Aggregate moved hours by destination role.
    by_role = {}
    for m in person_moves:
        r = m['to_role']
        by_role[r] = by_role.get(r, 0.0) + (m['duration_min'] / 60.0)

    moved_hours = sum(by_role.values())
    rota_hours = round(dr.shift_hours - protected_hours - moved_hours, 2)

    if rota_hours < 0.5 and not protected_segs:
        return {
            'applied': False,
            'reason': f'whole-day move ({rota_role} → {list(by_role)[0]}) — '
                       f'consider updating the rota instead',
            'segments': [],
        }
    if rota_hours < 0:
        rota_hours = 0.0  # moves + protected exceed the shift; clamp

    segments_spec = []
    if rota_hours >= 0.25:
        segments_spec.append((rota_role, rota_hours))
    for role, hrs in by_role.items():
        segments_spec.append((role, round(hrs, 2)))
    for role, hrs in protected_segs:
        segments_spec.append((role, round(hrs, 2)))

    if len(segments_spec) < 2:
        return {'applied': False, 'reason': 'nothing to split', 'segments': []}

    ok = split_day(pw, target_date, segments_spec)
    if not ok:
        return {'applied': False, 'reason': 'split_day rejected the spec',
                'segments': []}

    add_override(
        pw, f'apply_moves ({day_tag})',
        old_label, dr.role,
        'Re-derived from role-change moves: '
        + ', '.join(f"{r} {h:.1f}h" for r, h in segments_spec),
    )
    return {'applied': True, 'reason': 'split applied',
            'segments': segments_spec}


def apply_all_moves(week_data, friday):
    """Fold every captured Slack role-move into the reward week's day shapes.

    For each day in the reward week, loads its moves and runs
    apply_moves_to_day on (a) everyone with a move that day and (b) anyone
    who had a prior apply-moves split (so cleared moves collapse the day
    back to the planned rota). Idempotent — re-runs reflect the current
    move list without clobbering manual overrides.

    Wired into the daily-actuals cron + Thursday checks so the app + audit
    sheet always reflect today's moves without needing a manual click.

    Returns a summary list of (name, date, segments) for changed days.
    """
    import role_changes as rc
    changes = []
    for d in get_weekday_dates(friday):
        moves = rc.load_moves(d) or []
        day_tag = d.strftime('%a %d/%m')
        names_with_moves = {m.get('name') for m in moves if m.get('name')}
        names_prev_applied = {
            n for n, pw in week_data.items()
            if any((o.get('field') or '').startswith(f'apply_moves ({day_tag})')
                    for o in pw.overrides)
        }
        for name in sorted(names_with_moves | names_prev_applied):
            pw = week_data.get(name)
            if not pw:
                continue
            try:
                r = apply_moves_to_day(pw, d, moves)
            except Exception as e:
                logging.warning(f"apply_all_moves: {name} {d} failed: {e}")
                continue
            if r.get('applied'):
                changes.append((name, d, r.get('segments') or []))
    return changes


# ── Eligibility calculation ─────────────────────────────────────────────────

def calculate_eligibility(pw):
    """Calculate reward time eligibility for a PersonWeek.
    Returns (eligible, level, hours, reason).
    level: '', 'base', 'stretch'
    """
    if pw.override_eligible == 'deny':
        return False, '', 0, 'Manually denied'
    if pw.override_eligible in ('base', 'stretch'):
        level = pw.override_eligible
        hrs = _reward_hours(pw, level)
        return True, level, hrs, 'Manual override'

    working_days = [d for d, dr in pw.days.items() if dr.is_working]
    if not working_days:
        return False, '', 0, 'No working days'

    # Check skip threshold (pro-rata by qualifying days — training counts
    # as a full day per Jo's rule)
    qd = _qualifying_days(pw)
    pro_rata_factor = qd / 5.0 if qd > 0 else 1.0
    skip_limit = SKIP_THRESHOLD_WEEKLY * pro_rata_factor
    if pw.skips > skip_limit:
        return False, '', 0, f'Skips {pw.skips} > limit {skip_limit:.0f}'

    if not pw.quality_ok:
        return False, '', 0, 'Quality not confirmed'

    if not pw.timeline_ok:
        return False, '', 0, 'Timeline not confirmed'

    # Check daily targets
    all_base = True
    all_stretch = True
    for d in working_days:
        dr = pw.days[d]
        if dr.target_base > 0:
            if not dr.met_base:
                all_base = False
                all_stretch = False
            elif not dr.met_stretch:
                all_stretch = False

    if not all_base:
        return False, '', 0, 'Missed base target on one or more days'

    if all_stretch:
        hrs = _reward_hours(pw, 'stretch')
        return True, 'stretch', hrs, 'Hit stretch every day'

    hrs = _reward_hours(pw, 'base')
    return True, 'base', hrs, 'Hit base every day'


def _qualifying_days(pw):
    """Days that count toward reward-time pro-rata.

    Per Jo's rule: pro-rate only for sickness / absence — training days
    count as full days. So a person who worked 4 days and trained 1 day
    is treated as a full 5-day week for both reward hours and the skip
    threshold."""
    return sum(
        1 for dr in pw.days.values()
        if dr.is_working or dr.role == 'Training'
    )


def _actual_hours_worked(pw):
    """Hours that count toward the reward-time pro-rata numerator.

    Includes:
      - Days where dr.is_working (productive work hours)
      - Days where dr.role == 'Training' (training counts as a full day)

    Excludes:
      - Annual leave / unplanned absence (legitimately reduce pro-rata)
      - Non-working days / bank holidays (not part of the contract that week)
    """
    total = 0.0
    for d, dr in pw.days.items():
        if dr.is_working or dr.role == 'Training':
            total += dr.shift_hours
    return total


def _reward_hours(pw, level):
    """Calculate pro-rata reward hours.

    Pro-rated by actual hours worked / 40. Mirrors Sam's TL Calculator,
    where the 'Weekly Hours' cell is manually set to actual hours worked
    that week (full contract by default; reduced for absence/partial days).
    Bank holidays and NWDs naturally excluded (is_working=False)."""
    actual = _actual_hours_worked(pw)
    ratio = actual / FULL_TIME_HOURS
    if level == 'stretch':
        return round((BASE_REWARD_HOURS + STRETCH_BONUS_HOURS) * ratio, 2)
    return round(BASE_REWARD_HOURS * ratio, 2)


def format_reward_hours(hours):
    """Format decimal hours as 'Xh Ym' with minutes rounded UP to nearest 15.

    Mirrors Sam's TL Calculator formula:
      0h 36m → 0h 45m, 2h 24m → 2h 30m, 0h 48m → 1h 0m.
    Returns '—' for zero/negative."""
    import math
    if hours is None or hours <= 0:
        return '—'
    whole = int(hours)
    frac_mins = (hours - whole) * 60
    rounded_mins = int(math.ceil(frac_mins / 15) * 15)
    if rounded_mins == 60:
        return f"{whole + 1}h 0m"
    return f"{whole}h {rounded_mins}m"


# ── Override helpers ────────────────────────────────────────────────────────

def add_override(pw, field, old_value, new_value, reason):
    """Record an override with audit trail."""
    from datetime import datetime
    pw.overrides.append({
        'timestamp': datetime.now().isoformat(),
        'field': field,
        'old': str(old_value),
        'new': str(new_value),
        'reason': reason,
    })


# ── Slack message builder ──────────────────────────────────────────────────

def build_reward_slack_message(friday, week_data):
    """Build the Friday Slack message for TLs listing who qualified."""
    lines = [f"🎉 *Reward Time — w/c {friday.strftime('%d %b %Y')}*", ""]

    qualified = []
    not_qualified = []

    for name in ALL_AGENTS:
        pw = week_data.get(name)
        if not pw:
            continue
        eligible, level, hours, reason = calculate_eligibility(pw)
        if eligible:
            reward_day_info = REWARD_DAYS.get(name)
            day_block = f"{reward_day_info[0]} {reward_day_info[1]}" if reward_day_info else "TBC"
            if level == 'stretch':
                qualified.append(f"⭐ {name} — *Stretch* ({format_reward_hours(hours)}) — {day_block}")
            else:
                qualified.append(f"✅ {name} — *Base* ({format_reward_hours(hours)}) — {day_block}")
        else:
            not_qualified.append(f"❌ {name} — {reason}")

    if qualified:
        lines.append("*Qualified:*")
        lines.extend(qualified)
        lines.append("")

    if not_qualified:
        lines.append("*Did not qualify:*")
        lines.extend(not_qualified)
        lines.append("")

    lines.append("Please let your team members know and confirm their reward day slots.")
    return "\n".join(lines)


def build_reward_message_by_team(friday, week_data):
    """One consolidated Slack message for #reward-time-questions-cs.

    Grouped by TL's team. For each team:
      ⭐/✅ Name — Stretch/Base — Xh Ym — Day AM/PM   (qualifiers)
      _Didn't qualify: Name, Name_                    (italic, names only — no reasons)

    Reasons are deliberately omitted — Jo doesn't want sensitive reasons
    (skips over limit, missed base, etc.) broadcast to the team channel.
    Anyone curious can ping Jo."""
    week_label = friday.strftime('%d %b %Y')
    lines = [f"🎉 *Reward Time — week of {week_label}*", '']

    tl_order = ['Jess', 'Yasmin', 'Courtney']

    for tl in tl_order:
        members = TL_TEAMS.get(tl, [])
        qualified_lines = []
        misses = []
        for name in members:
            pw = week_data.get(name)
            if not pw or pw.days_worked == 0:
                continue
            eligible, level, hours, _ = calculate_eligibility(pw)
            if not eligible:
                misses.append(name)
                continue
            rd = REWARD_DAYS.get(name)
            slot = f"{rd[0]} {rd[1]}" if rd else "TBC"
            badge = '⭐' if level == 'stretch' else '✅'
            level_word = 'Stretch' if level == 'stretch' else 'Base'
            qualified_lines.append(
                f"{badge} *{name}* — {level_word} — {format_reward_hours(hours)} — {slot}"
            )

        if qualified_lines or misses:
            lines.append(f"*{tl}'s team*")
            if qualified_lines:
                lines.extend(qualified_lines)
            if misses:
                lines.append(f"_Didn't qualify: {', '.join(misses)}_")
            lines.append('')

    return '\n'.join(lines).rstrip() + '\n'


def build_daily_notes_draft(friday, week_data, next_week_monday,
                              shifts_by_name=None):
    """Build a list of draft Daily Notes rows for each qualifying person's
    reward block, taken in the rota week starting `next_week_monday`.

    Each row is a dict matching the Daily Notes sheet columns:
        date, time, name, role, note, cover_needed, whos_covering

    Time strings come from each person's shift bounds + reward hours:
        PM block: '(shift_end - hours):MM - shift_end:MM'
        AM block: 'shift_start:MM - (shift_start + hours):MM'

    `shifts_by_name` (optional): {name: (start_h, end_h)} read from the
    rota sheet's Working Hours tab via generate_rota.read_working_hours.
    Falls back to generate_rota.DEFAULT_SHIFTS for anyone not present.

    Cover-needed and Role are filled in by the caller from the next
    week's rota assignments."""
    from datetime import timedelta as _td

    # Map Tue/Wed/Thu → day_idx (Mon=0 .. Sun=6)
    day_idx_by_name = {'Mon': 0, 'Tue': 1, 'Wed': 2, 'Thu': 3, 'Fri': 4}

    # Lazy import to avoid circular dep at module load
    try:
        from generate_rota import DEFAULT_SHIFTS as _DEFAULT_SHIFTS
    except Exception:
        _DEFAULT_SHIFTS = {}

    shifts_by_name = shifts_by_name or {}

    rows = []
    for name in ALL_AGENTS:
        pw = week_data.get(name)
        if not pw or pw.days_worked == 0:
            continue
        eligible, level, hours, _ = calculate_eligibility(pw)
        if not eligible:
            continue
        rd = REWARD_DAYS.get(name)
        if not rd:
            continue
        day_name, block = rd
        day_idx = day_idx_by_name.get(day_name)
        if day_idx is None:
            continue
        target_date = next_week_monday + _td(days=day_idx)

        # Look up shift: prefer Working Hours sheet, fall back to DEFAULT_SHIFTS
        shift = shifts_by_name.get(name) or _DEFAULT_SHIFTS.get(name, (9, 17))
        shift_start, shift_end = shift

        # Round the reward block to the nearest 15-minute increment so the
        # time string matches what's shown in the message (format_reward_hours
        # rounds UP to nearest 15 min). E.g. raw 3.2h → 3.25h → 13:45 not 13:48.
        import math
        rounded_hours = math.ceil(hours * 4) / 4

        if block == 'PM':
            start_h = shift_end - rounded_hours
            end_h = shift_end
        else:   # AM
            start_h = shift_start
            end_h = shift_start + rounded_hours

        time_str = _fmt_hour(start_h) + ' - ' + _fmt_hour(end_h)

        rows.append({
            'date': target_date,
            'time': time_str,
            'name': name,
            'role': '',   # filled in by caller using the new week's rota
            'note': f"Reward time ({level} — {format_reward_hours(hours)})",
            'cover_needed': False,   # set by caller based on role
            'whos_covering': '',
        })
    return rows


def _fmt_hour(h: float) -> str:
    """Format a fractional hour as 'HH:MM' (always with minutes).

    Matches Jo's preferred Daily Notes time format:
        13.75 → '13:45'
        17.0  → '17:00'   (always show :00 for whole hours, not bare '17')
    """
    whole = int(h)
    mins = int(round((h - whole) * 60))
    if mins == 60:
        whole += 1
        mins = 0
    return f"{whole}:{mins:02d}"


def build_tl_messages(friday, week_data):
    """Build per-TL Slack messages. Returns {tl_name: message}."""
    messages = {}
    for tl, members in TL_TEAMS.items():
        lines = [f"🎉 *Reward Time — w/c {friday.strftime('%d %b %Y')}*", ""]
        has_qualified = False
        for name in members:
            pw = week_data.get(name)
            if not pw:
                continue
            eligible, level, hours, reason = calculate_eligibility(pw)
            if eligible:
                has_qualified = True
                reward_day_info = REWARD_DAYS.get(name)
                day_block = f"{reward_day_info[0]} {reward_day_info[1]}" if reward_day_info else "TBC"
                if level == 'stretch':
                    lines.append(f"⭐ {name} — *Stretch* ({format_reward_hours(hours)}) — {day_block}")
                else:
                    lines.append(f"✅ {name} — *Base* ({format_reward_hours(hours)}) — {day_block}")
            else:
                lines.append(f"❌ {name} — {reason}")

        if has_qualified:
            lines.append("")
            lines.append("Please let them know and confirm their reward day slots.")
        messages[tl] = "\n".join(lines)
    return messages


# ── Google Sheets integration ───────────────────────────────────────────────

def _get_creds():
    from compat import get_google_credentials
    return get_google_credentials()


def _get_gspread():
    import gspread
    return gspread.authorize(_get_creds())


def _get_drive_service():
    from googleapiclient.discovery import build
    return build('drive', 'v3', credentials=_get_creds())


def load_reward_state():
    """Load reward sheet ID from state file (local or Drive)."""
    global REWARD_SHEET_ID
    from drive_state import read_json_either
    data = read_json_either(STATE_FILE, 'state.json') or {}
    REWARD_SHEET_ID = data.get('reward_sheet_id') or REWARD_SHEET_ID
    return REWARD_SHEET_ID


def save_reward_state():
    """Save reward sheet ID to state file (local or Drive)."""
    from drive_state import read_json_either, write_json_either
    data = read_json_either(STATE_FILE, 'state.json') or {}
    data['reward_sheet_id'] = REWARD_SHEET_ID
    write_json_either(STATE_FILE, 'state.json', data)


def create_reward_sheet():
    """Create the Reward Time Google Sheet with Audit Log and Week Summary tabs."""
    global REWARD_SHEET_ID
    gc = _get_gspread()
    ss = gc.create("CS Reward Time Tracker")
    REWARD_SHEET_ID = ss.id
    save_reward_state()

    # Share with Jo — may fail due to Juno domain policy; sheet is accessible
    # via the service account that created it
    try:
        drive = _get_drive_service()
        drive.permissions().create(
            fileId=REWARD_SHEET_ID,
            body={'type': 'user', 'role': 'writer', 'emailAddress': 'joanne.jeffries@juno.legal'},
            sendNotificationEmail=False,
        ).execute()
    except Exception:
        # Domain policy blocks cross-domain sharing; share with 'anyone with link' instead
        try:
            drive.permissions().create(
                fileId=REWARD_SHEET_ID,
                body={'type': 'anyone', 'role': 'writer'},
            ).execute()
        except Exception:
            pass  # Sheet still accessible via authenticated gspread client

    # Create Audit Log tab
    ws = ss.sheet1
    ws.update_title('Audit Log')
    ws.update('A1:G1', [['Timestamp', 'Reward Week', 'Person', 'Field', 'Old Value', 'New Value', 'Reason']])
    ws.format('A1:G1', {'textFormat': {'bold': True}, 'backgroundColor': {'red': 0.9, 'green': 0.9, 'blue': 0.9}})

    # Create Week Summary tab
    ws2 = ss.add_worksheet('Week Summary', rows=50, cols=15)
    ws2.update('A1:I1', [['Reward Week', 'Person', 'Role', 'Days Worked', 'Skips',
                           'Quality', 'Timelines', 'Result', 'Reward Hours']])
    ws2.format('A1:I1', {'textFormat': {'bold': True}, 'backgroundColor': {'red': 0.9, 'green': 0.9, 'blue': 0.9}})

    print(f"Created Reward Time sheet: https://docs.google.com/spreadsheets/d/{REWARD_SHEET_ID}")
    return REWARD_SHEET_ID


def _get_reward_sheet():
    """Get the reward time spreadsheet, creating if needed."""
    global REWARD_SHEET_ID
    if not REWARD_SHEET_ID:
        load_reward_state()
    if not REWARD_SHEET_ID:
        create_reward_sheet()
    gc = _get_gspread()
    return gc.open_by_key(REWARD_SHEET_ID)


def write_audit_entry(friday, name, field_name, old_value, new_value, reason):
    """Append a row to the Audit Log tab in Google Sheets."""
    from datetime import datetime
    ss = _get_reward_sheet()
    ws = ss.worksheet('Audit Log')
    row = [
        datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        friday.strftime('%d %b %Y'),
        name,
        field_name,
        str(old_value),
        str(new_value),
        reason,
    ]
    ws.append_row(row, value_input_option='USER_ENTERED')


def write_week_summary(friday, week_data):
    """Write/update the Week Summary tab for a given reward week."""
    ss = _get_reward_sheet()
    ws = ss.worksheet('Week Summary')

    # Build rows for this week
    rows = []
    week_label = friday.strftime('%d %b %Y')
    week_dates = get_weekday_dates(friday)

    for name in ALL_AGENTS:
        pw = week_data.get(name)
        if not pw or pw.days_worked == 0:
            continue

        roles = set()
        for d in week_dates:
            dr = pw.days.get(d)
            if dr and dr.is_working and dr.role:
                base = dr.role.split(' + ')[0] if dr.role.startswith('Inbound phones') else dr.role
                roles.add(base)

        eligible, level, hours, reason = calculate_eligibility(pw)
        result = f"{'Stretch' if level == 'stretch' else 'Base'} — {reason}" if eligible else f"No — {reason}"

        rows.append([
            week_label, name, ', '.join(sorted(roles)),
            pw.days_worked, pw.skips,
            'Yes' if pw.quality_ok else 'No',
            'Yes' if pw.timeline_ok else 'No',
            result, format_reward_hours(hours) if eligible else '—',
        ])

    # Remove existing rows for this week (if re-running)
    existing = ws.get_all_values()
    keep = [existing[0]]  # header
    for row in existing[1:]:
        if row and row[0] != week_label:
            keep.append(row)
    keep.extend(rows)

    ws.clear()
    if keep:
        ws.update(f'A1:I{len(keep)}', keep, value_input_option='USER_ENTERED')

    # Re-format header
    ws.format('A1:I1', {'textFormat': {'bold': True}, 'backgroundColor': {'red': 0.9, 'green': 0.9, 'blue': 0.9}})
    print(f"  Wrote week summary for {week_label} ({len(rows)} people)")


def write_daily_actuals_snapshot(friday, week_data):
    """Write/update a flat snapshot table on the 'Daily Actuals Snapshot' tab.

    One row per person showing the current reward week's actuals + skips
    + total hours worked. Rewritten in full each call so the timestamp
    in cell A2 always reflects the latest refresh.

    Layout:
        A: Last refreshed (only filled on row 2)
        B: Name
        C: Skips
        D-H: Fri / Mon / Tue / Wed / Thu actual (against rota'd metric)
        I: Hours worked this week

    Designed for the daily-auto-pull cron — gives Jo a glance-able view
    of fresh numbers without opening the rota app."""
    from datetime import datetime as _dt

    ss = _get_reward_sheet()
    title = 'Daily Actuals Snapshot'
    try:
        ws = ss.worksheet(title)
    except Exception:
        ws = ss.add_worksheet(title, rows=30, cols=10)

    week_dates = get_weekday_dates(friday)  # [Fri, Mon, Tue, Wed, Thu]
    refreshed_at = _dt.now().strftime('%a %d %b %Y %H:%M')

    header = ['Last refreshed', 'Name', 'Skips',
              'Fri actual', 'Mon actual', 'Tue actual', 'Wed actual', 'Thu actual',
              'Hours worked']

    rows = [header]
    first_data_row = True
    for name in ALL_AGENTS:
        pw = week_data.get(name)
        if not pw or pw.days_worked == 0:
            continue
        row = [refreshed_at if first_data_row else '']
        first_data_row = False
        row.append(name)
        row.append(pw.skips)
        for d in week_dates:
            dr = pw.days.get(d)
            if not dr or not dr.is_working:
                row.append('—')
            elif dr.segments:
                row.append(' | '.join(str(s.actual) for s in dr.segments))
            else:
                row.append(dr.actual)
        # Total hours actually worked (includes training, per Jo's rule)
        hours = sum(
            dr.shift_hours for dr in pw.days.values()
            if dr.is_working or dr.role == 'Training'
        )
        row.append(f"{hours:.1f}")
        rows.append(row)

    ws.clear()
    ws.update(f'A1:I{len(rows)}', rows, value_input_option='USER_ENTERED')
    ws.format('A1:I1', {
        'textFormat': {'bold': True},
        'backgroundColor': {'red': 0.9, 'green': 0.9, 'blue': 0.9},
    })
    print(f"  Wrote daily actuals snapshot ({len(rows) - 1} people)")
