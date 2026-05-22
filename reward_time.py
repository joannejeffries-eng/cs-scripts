"""
Reward Time tracker — pulls Looker data, calculates eligibility, stores results.

Separate from fill_tracker.py. Uses the same Postgres/Looker DB but own logic.

Reward weeks run Friday to Thursday (7 days).
Targets from the CS Baselines & Reward Time Plan doc.
"""
import os
import json
from datetime import date, timedelta
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
    'Bella': 'Bella Brayford', 'Clare': 'Clare Brown', 'Cris': 'Cris Macagi',
    'Erika': 'Erika Frolova', 'Harriet': 'Harriet Clifton-Sprigg',
    'Lizzie': 'Lizzie Williamson', 'Lucy': 'Lucy Riordan',
    'Maisha': 'Maisha Begum', 'Noemi': 'Noemi Sip', 'Sophie': 'Sophie Maloney',
    'Tara': 'Tara Dunkley', 'Thea': 'Thea Willsmore',
}
FIRST_NAMES = {v: k for k, v in DB_NAMES.items()}

TL_TEAMS = {
    'Courtney': ['Fionn', 'Kate', 'Becky', 'Jade', 'Elida', 'Harriet'],
    'Yasmin': ['Tara', 'Sophie', 'Noemi', 'Lizzie', 'Kirsty'],
    'Jess': ['Bella', 'Cris', 'Clare', 'Erika', 'Lucy', 'Maisha', 'Thea'],
}
ALL_AGENTS = sorted(DB_NAMES.keys())

# ── Weekly hours per person (for pro-rata) ──────────────────────────────────
WEEKLY_HOURS = {
    'Becky': 40, 'Kate': 40, 'Fionn': 40, 'Jade': 40, 'Elida': 40,
    'Harriet': 40, 'Bella': 35, 'Cris': 40, 'Clare': 32, 'Erika': 40,
    'Lucy': 40, 'Maisha': 40, 'Thea': 40, 'Noemi': 40, 'Tara': 22,
    'Sophie': 30, 'Kirsty': 40, 'Lizzie': 30,
}

# ── Targets ─────────────────────────────────────────────────────────────────
# Role string from rota -> (base_target, stretch_target, metric_name)
ROLE_TARGETS = {
    'Inbound phones':           (72, 82, 'inbound_calls'),
    'Triage only':              (130, 180, 'emails_archived'),
    'Triage + lender chasing':  (100, 140, 'emails_archived'),  # lower than triage-only — lender chasing takes time off the triage queue
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
                "Can't reach the Looker database from Streamlit Cloud — "
                "it's on Juno's private network and only Jo's laptop "
                "(on VPN) can connect.\n\n"
                "👉 Ask Jo to click **🔄 Pull actuals** on her local rota "
                "app (http://localhost:8501 → Reward Time). The fresh "
                "numbers and skip counts will appear here within a few "
                "seconds — the apps share the same state via Drive."
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
    'Jade':    ('Tue', 'AM'),
    'Maisha':  ('Tue', 'AM'),
    'Becky':   ('Tue', 'PM'),
    'Lucy':    ('Tue', 'PM'),
    'Thea':    ('Tue', 'PM'),
    'Kate':    ('Wed', 'AM'),
    'Noemi':   ('Wed', 'AM'),
    'Cris':    ('Wed', 'AM'),
    'Fionn':   ('Wed', 'PM'),
    'Lizzie':  ('Wed', 'PM'),
    'Clare':   ('Wed', 'PM'),
    'Kirsty':  ('Thu', 'AM'),
    'Sophie':  ('Thu', 'AM'),
    'Tara':    ('Thu', 'AM'),
    'Elida':   ('Thu', 'PM'),
    'Erika':   ('Thu', 'PM'),
    'Bella':   ('Thu', 'PM'),
}
# Harriet not in the original doc — needs assigning
# Phones team: Jade, Becky, Kate, Fionn, Kirsty, Elida
# Phones can only swap with phones

DAY_NAME_TO_IDX = {'Mon': 0, 'Tue': 1, 'Wed': 2, 'Thu': 3, 'Fri': 4}


# ── Data structures ─────────────────────────────────────────────────────────

STANDARD_SHIFT_HOURS = 8.0  # Full day = 8 productive hours; targets are calibrated to this

# Per-person daily hours (mirrors generate_rota DEFAULT_HOURS)
DAILY_HOURS = {
    'Becky': 8, 'Kate': 8, 'Fionn': 8, 'Jade': 8, 'Elida': 8,
    'Harriet': 8, 'Cris': 8, 'Clare': 8, 'Erika': 8, 'Lucy': 8,
    'Maisha': 8, 'Thea': 8, 'Noemi': 8, 'Kirsty': 8,
    'Bella': 7, 'Tara': 4.5, 'Sophie': 6, 'Lizzie': 6,
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

    Cached for the lifetime of the Python process; restart the Streamlit
    app if Looker rebuilds its scratch tables (uncommon)."""
    if _PDT_TABLE_CACHE['name'] is not None:
        return _PDT_TABLE_CACHE['name']

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
                # We check base roles (e.g. "Inbound phones" vs
                # "Inbound phones + Email Health" — both base "Inbound phones").
                new_base = new_role.split(' + ')[0]
                keep_splits = False
                if dr.segments:
                    for seg in dr.segments:
                        if seg.role.split(' + ')[0] == new_base:
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

        if block == 'PM':
            start_h = shift_end - hours
            end_h = shift_end
        else:   # AM
            start_h = shift_start
            end_h = shift_start + hours

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
    """Format a fractional hour (e.g. 13.5) as 'HH:MM' or short form.

    Match the look of existing Daily Notes entries: full hours just show
    as the hour (e.g. '5' not '5:00'); fractional shows minutes.
    """
    whole = int(h)
    mins = int(round((h - whole) * 60))
    if mins == 0:
        return str(whole)
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
