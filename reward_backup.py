"""
Reward Time — laptop-independent backup spreadsheet.

Why this exists
---------------
The Streamlit rota app pulls reward-time actuals from Looker's private
Postgres, which is only reachable from Jo's laptop on VPN. If that laptop
is off, Sam can't run reward time. This script rebuilds the same picture
from *internet-facing* sources instead, so it runs from anywhere:

  - Looker API (https://looker-api.juno.legal) for throughput + skips
  - Slack API for the day's role moves
  - Google Drive for the week's role/absence structure (read-only)

It reuses reward_time's structure, targets and eligibility logic so the
result matches the app. Quality/timelines are surfaced as "needs sign-off"
(best-effort archive-ratio hint only) rather than gating the headline
result — Sam confirms those by eye.

Run:  python3 reward_backup.py [--friday YYYY-MM-DD] [--share email]
"""
import os
import re
import sys
import json
import argparse
from datetime import date, datetime, timedelta
from collections import defaultdict
from pathlib import Path

import requests
import gspread

import reward_time as rt
import compat

# ── Config ───────────────────────────────────────────────────────────────────
ROLE_CHANGES_CHANNEL = 'C0AUP24HQPP'   # #dry-run-testing-jo today; → #client-support-leads (C093EAUT3HQ) from next week
REWARD_POST_CHANNEL = 'reward-time-tracker'   # C0B103KEJLU — where Sam pastes the post
STATE_DIR = Path.home() / '.juno/scheduled-tasks/reward-backup'
STATE_FILE = STATE_DIR / 'state.json'
SHEET_TITLE = 'CS Reward Time — Backup (Looker)'
TL_TAGS = '@Jess @Yasmin @Courtney'

LOOKER_MODEL = 'juno_staff_app'


# ── Looker REST ────────────────────────────────────────────────────────────────
class Looker:
    def __init__(self):
        self.base = os.environ['LOOKERSDK_BASE_URL'].rstrip('/')
        self.s = requests.Session()
        self.s.verify = os.environ.get('LOOKERSDK_VERIFY_SSL', 'true').lower() != 'false'
        r = self.s.post(f"{self.base}/api/4.0/login", data={
            'client_id': os.environ['LOOKERSDK_CLIENT_ID'],
            'client_secret': os.environ['LOOKERSDK_CLIENT_SECRET'],
        }, timeout=60)
        r.raise_for_status()
        self.token = r.json()['access_token']

    def run(self, view, fields, filters, sorts=None, limit=5000):
        body = {'model': LOOKER_MODEL, 'view': view, 'fields': fields,
                'filters': filters, 'limit': limit}
        if sorts:
            body['sorts'] = sorts
        r = self.s.post(f"{self.base}/api/4.0/queries/run/json",
                        headers={'Authorization': f'token {self.token}'},
                        json=body, timeout=120)
        r.raise_for_status()
        return r.json()


def _date_range_filter(dates):
    """Looker inclusive-start / exclusive-end range covering all `dates`."""
    lo = min(dates)
    hi = max(dates) + timedelta(days=1)
    return f"{lo.isoformat()} to {hi.isoformat()}"


def pull_actuals(lk, dates):
    """Return {date: {first_name: {metric: value}}} from Looker — same shape
    as reward_time.pull_day_data, but for the whole week in four queries.

    All queries are filtered to the CS roster: without it the explores return
    the whole company and the row cap silently truncates the earliest days."""
    rng = _date_range_filter(dates)
    names = ', '.join(rt.DB_NAMES.values())
    out = defaultdict(lambda: defaultdict(dict))

    def first_of(full):
        return rt.FIRST_NAMES.get(full)

    # Inbound answered (phones) — match the app: answered_at IS NOT NULL
    # (Looker's is_answered is stricter and drops short answered calls)
    for row in lk.run('phone_activity',
                      ['phone_activity.user_name', 'phone_activity.started_date', 'phone_activity.count'],
                      {'phone_activity.direction': 'inbound', 'phone_activity.answered_date': 'NOT NULL',
                       'phone_activity.user_name': names, 'phone_activity.started_date': rng}):
        f = first_of(row['phone_activity.user_name'])
        if f and row['phone_activity.started_date']:
            d = date.fromisoformat(row['phone_activity.started_date'][:10])
            out[d][f]['inbound_calls'] = int(row['phone_activity.count'] or 0)

    # Outbound connected (chasing) — answered (answered_at NOT NULL) + duration > 25s
    for row in lk.run('phone_activity',
                      ['phone_activity.user_name', 'phone_activity.started_date', 'phone_activity.count'],
                      {'phone_activity.direction': 'outbound', 'phone_activity.answered_date': 'NOT NULL',
                       'phone_activity.duration': '>25', 'phone_activity.user_name': names,
                       'phone_activity.started_date': rng}):
        f = first_of(row['phone_activity.user_name'])
        if f and row['phone_activity.started_date']:
            d = date.fromisoformat(row['phone_activity.started_date'][:10])
            out[d][f]['outbound_calls'] = int(row['phone_activity.count'] or 0)

    # Things done by method (triage emails + ICS things)
    by_pd = defaultdict(lambda: defaultdict(int))  # (date, first) -> {method: count}
    for row in lk.run('pdt_things_done_by_person',
                      ['pdt_things_done_by_person.staff_member_full_name',
                       'pdt_things_done_by_person.completed_date',
                       'pdt_things_done_by_person.completed_method',
                       'pdt_things_done_by_person.count'],
                      {'pdt_things_done_by_person.completed_date': rng,
                       'pdt_things_done_by_person.staff_member_full_name': names}):
        f = first_of(row['pdt_things_done_by_person.staff_member_full_name'])
        ds = row['pdt_things_done_by_person.completed_date']
        if not f or not ds:
            continue
        d = date.fromisoformat(ds[:10])
        method = row['pdt_things_done_by_person.completed_method'] or ''
        by_pd[(d, f)][method] += int(row['pdt_things_done_by_person.count'] or 0)
    for (d, f), methods in by_pd.items():
        archived = methods.get('email archived', 0)
        escalated = methods.get('email escalated', 0)
        total_email = archived + escalated
        out[d][f]['emails_archived'] = archived
        out[d][f]['things_done'] = sum(methods.values())
        out[d][f]['archive_ratio'] = (archived / total_email) if total_email else 0.0

    return out


def pull_skips(lk, dates):
    """Return {first_name: weekly_skip_count} (db_completion_status='cant_do')."""
    rng = _date_range_filter(dates)
    names = ', '.join(rt.DB_NAMES.values())
    skips = {}
    for row in lk.run('case_allocation',
                      ['case_alloc_staff_member.full_name', 'work_allocation.count_distinct'],
                      {'work_allocation.db_completion_status': 'cant_do',
                       'work_allocation.completed_date': rng,
                       'case_alloc_staff_member.full_name': names}):
        f = rt.FIRST_NAMES.get(row['case_alloc_staff_member.full_name'])
        if f:
            skips[f] = int(row['work_allocation.count_distinct'] or 0)
    return skips


# ── Slack moves ────────────────────────────────────────────────────────────────
_CONFIRM_RE = re.compile(
    r'\*(?P<name>[^*]+)\*\s+to\s+\*(?P<role>[^*]+)\*\s+\(from\s+\*(?P<from>[^*]+)\*\)\s+'
    r'(?P<start>\d{1,2}:\d{2})(?:\s+to\s+(?P<end>\d{1,2}:\d{2}))?'
)


def _slack(method, params, token):
    r = requests.get(f"https://slack.com/api/{method}",
                     headers={'Authorization': f'Bearer {token}'},
                     params=params, timeout=30)
    return r.json()


def pull_moves(dates):
    """Read each day's 'Role changes for…' thread and parse the bot's
    confirmation lines. Returns ({date: [move,…]}, set of (first_name,date)
    that had at least one move)."""
    token = compat.get_slack_token()
    oldest = (min(dates) - timedelta(days=1))
    hist = _slack('conversations.history', {
        'channel': ROLE_CHANGES_CHANNEL, 'limit': 200,
        'oldest': str(int(datetime(oldest.year, oldest.month, oldest.day).timestamp())),
    }, token)
    anchors = {}  # date -> ts
    for m in hist.get('messages', []):
        txt = m.get('text', '')
        mt = re.search(r'Role changes for\s+\w+\s+(\d{1,2})\s+(\w+)\s+(\d{4})', txt)
        if mt and 'please post any moves' in txt:
            try:
                d = datetime.strptime(f"{mt.group(1)} {mt.group(2)} {mt.group(3)}", "%d %b %Y").date()
                anchors[d] = m['ts']
            except ValueError:
                pass

    moves_by_date = defaultdict(list)
    moved = set()
    for d in dates:
        ts = anchors.get(d)
        if not ts:
            continue
        rep = _slack('conversations.replies',
                     {'channel': ROLE_CHANGES_CHANNEL, 'ts': ts, 'limit': 200}, token)
        for m in rep.get('messages', []):
            cm = _CONFIRM_RE.search(m.get('text', ''))
            if not cm:
                continue
            name = cm.group('name').strip()
            mv = {'date': d, 'name': name, 'from_role': cm.group('from').strip(),
                  'to_role': cm.group('role').strip(),
                  'start': cm.group('start'), 'end': cm.group('end') or 'open'}
            moves_by_date[d].append(mv)
            if name in rt.DB_NAMES:  # first-name key
                moved.add((name, d))
    return moves_by_date, moved


# ── Build the picture ──────────────────────────────────────────────────────────
def build(friday, lk):
    dates = rt.get_weekday_dates(friday)
    week = rt.load_week(friday)
    if not week:
        raise SystemExit(
            f"No reward-week structure found on Drive for w/c {friday}. "
            "Open the rota app's Reward Time page once to initialise the week, "
            "then this backup can refresh it from Looker.")

    actuals = pull_actuals(lk, dates)
    for d in dates:
        rt.update_day_actuals(week, d, actuals.get(d, {}))

    skips = pull_skips(lk, dates)
    for name, pw in week.items():
        if name in skips:
            pw.skips = skips[name]

    moves_by_date, moved = pull_moves(dates)
    return dates, week, moves_by_date, moved


def archive_quality(pw):
    """Best-effort quality hint from triage archive ratio. Returns (pct_str, ok)."""
    ratios = [dr.archive_ratio for dr in pw.days.values()
              if dr.is_working and dr.role.startswith('Triage') and dr.archive_ratio]
    if not ratios:
        return '—', True
    avg = sum(ratios) / len(ratios)
    return f"{avg*100:.0f}%", avg >= rt.TRIAGE_ARCHIVE_RATIO_BASE


# ── Sheet writing ──────────────────────────────────────────────────────────────
def _get_or_create_sheet(gc, share_with=None):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    sid = state.get('sheet_id')
    if sid:
        try:
            return gc.open_by_key(sid)
        except Exception:
            pass
    sh = gc.create(SHEET_TITLE)
    state['sheet_id'] = sh.id
    STATE_FILE.write_text(json.dumps(state, indent=2))
    if share_with:
        sh.share(share_with, perm_type='user', role='writer', notify=False)
    return sh


def _abbrev(role):
    if role.startswith('Inbound phones'):
        return 'Phones'
    if role.startswith('Triage + lender') or role.startswith('Triage+LC'):
        return 'T+LC'
    if role.startswith('Triage and Video'):
        return 'T+VC'
    if role.startswith('Triage'):
        return 'Triage'
    if role.startswith('Case setup'):
        return 'ICS'
    if role.startswith('Chasing'):
        return 'Chasing'
    return role


def _work_parts(dr):
    """(abbrev, actual, target_base, mark) for each working portion of a day."""
    parts = dr.segments if dr.segments else [dr]
    out = []
    for s in parts:
        if s.target_base <= 0:
            continue
        mark = '⭐' if s.met_stretch else ('✅' if s.met_base else '❌')
        out.append((_abbrev(s.role), s.actual, s.target_base, mark))
    return out


def _day_text(dr):
    if dr is None or not dr.is_working:
        return 'AL' if (dr and dr.role == 'Part day AL') else '—'
    parts = _work_parts(dr)
    if not parts:
        return '—'
    return '\n'.join(f"{a} {act}/{tb} {mk}" for a, act, tb, mk in parts)


def _day_status(dr):
    """'g' (met base), 'r' (missed), or None (not a target day)."""
    if dr is None or not dr.is_working or not _work_parts(dr):
        return None
    return 'g' if dr.met_base else 'r'


# Reward Week column layout (0-based)
COL_DAYS_START = 2            # Fri..Thu occupy 2..6
N_DAYS = 5
COL_SKIPS = 7
COL_ARCHIVE = 8
COL_QUALITY = 9
COL_TIMELINES = 10
COL_RESULT = 11
COL_SIGNOFF = 12
HEADER_ROW = 4               # 0-based row of the column header

GREEN = {'red': 0.84, 'green': 0.93, 'blue': 0.82}
RED = {'red': 0.96, 'green': 0.80, 'blue': 0.80}
TEAMGREY = {'red': 0.90, 'green': 0.90, 'blue': 0.92}
HDRGREY = {'red': 0.83, 'green': 0.86, 'blue': 0.91}

QUALITY_OPTS = ['✅ OK', '⚠ check', '❌ not OK', 'n/a']
TIMELINE_OPTS = ['✅ OK', '⚠ check']
SIGNOFF_OPTS = ['', '✅ Approve', 'Grant base', 'Grant stretch', 'Query TL', 'Deny']


def write_sheet(sh, friday, dates, week, moves_by_date, moved):
    refreshed = datetime.now().strftime('%a %d %b %Y %H:%M')
    span = f"{dates[0].strftime('%a %d %b')} → {dates[-1].strftime('%a %d %b %Y')}"
    day_hdrs = [d.strftime('%a %d %b') for d in dates]

    ws = sh.sheet1
    ws.update_title('Reward Week')
    rows = [
        [f"CS Reward Time — Backup (week {span})"],
        [f"Refreshed live from Looker + Slack: {refreshed}  ·  no laptop / VPN needed"],
        ["Result is the throughput verdict. Sign off Quality + Timelines and make the call in the dropdowns →"],
        [],
        (['Name', 'Reward slot'] + day_hdrs +
         ['Skips', 'Archive %', 'Quality ✓', 'Timelines ✓', 'Result (data)', 'Sign-off']),
    ]

    day_status = []   # (row_idx, [status per day], result_eligible)
    team_rows = []
    member_rows = []
    tl_order = ['Jess', 'Yasmin', 'Courtney']
    for tl in tl_order:
        members = [m for m in rt.TL_TEAMS.get(tl, []) if week.get(m) and week[m].days_worked > 0]
        if not members:
            continue
        team_rows.append(len(rows))
        rows.append([f"{tl}'s team"])
        for name in members:
            pw = week[name]
            pw.quality_ok = True
            pw.timeline_ok = True
            eligible, level, hours, reason = rt.calculate_eligibility(pw)
            rd = rt.REWARD_DAYS.get(name)
            slot = f"{rd[0]} {rd[1]}" if rd else 'TBC'
            statuses = []
            day_vals = []
            for d in dates:
                dr = pw.days.get(d)
                txt = _day_text(dr)
                if (name, d) in moved and (dr is None or not dr.segments):
                    txt += ' ↻?'
                day_vals.append(txt)
                statuses.append(_day_status(dr))
            pct, qok = archive_quality(pw)
            if eligible:
                badge = '⭐' if level == 'stretch' else '✅'
                result = f"{badge} {level.title()} — {rt.format_reward_hours(hours)}"
            else:
                result = f"❌ {reason}"
            member_rows.append(len(rows))
            day_status.append((len(rows), statuses, eligible))
            rows.append([name, slot] + day_vals +
                        [pw.skips, pct,
                         '✅ OK' if qok else '⚠ check',
                         '⚠ check', result, ''])
        rows.append([])

    ws.clear()
    ws.update(rows, value_input_option='USER_ENTERED')

    # ---- formatting + dropdowns in one batch ----
    gid = ws.id
    reqs = []

    def cell_bg(r, c, color):
        reqs.append({'repeatCell': {
            'range': {'sheetId': gid, 'startRowIndex': r, 'endRowIndex': r + 1,
                      'startColumnIndex': c, 'endColumnIndex': c + 1},
            'cell': {'userEnteredFormat': {'backgroundColor': color}},
            'fields': 'userEnteredFormat.backgroundColor'}})

    def validation(rows_idx, col, opts):
        for r in rows_idx:
            reqs.append({'setDataValidation': {
                'range': {'sheetId': gid, 'startRowIndex': r, 'endRowIndex': r + 1,
                          'startColumnIndex': col, 'endColumnIndex': col + 1},
                'rule': {'condition': {'type': 'ONE_OF_LIST',
                                       'values': [{'userEnteredValue': v} for v in opts]},
                         'showCustomUi': True, 'strict': False}}})

    # title / header styling
    reqs.append({'repeatCell': {
        'range': {'sheetId': gid, 'startRowIndex': 0, 'endRowIndex': 1, 'startColumnIndex': 0, 'endColumnIndex': 1},
        'cell': {'userEnteredFormat': {'textFormat': {'bold': True, 'fontSize': 13}}},
        'fields': 'userEnteredFormat.textFormat'}})
    reqs.append({'repeatCell': {
        'range': {'sheetId': gid, 'startRowIndex': HEADER_ROW, 'endRowIndex': HEADER_ROW + 1,
                  'startColumnIndex': 0, 'endColumnIndex': COL_SIGNOFF + 1},
        'cell': {'userEnteredFormat': {'textFormat': {'bold': True}, 'backgroundColor': HDRGREY,
                                       'wrapStrategy': 'WRAP', 'verticalAlignment': 'MIDDLE'}},
        'fields': 'userEnteredFormat(textFormat,backgroundColor,wrapStrategy,verticalAlignment)'}})
    # freeze header + name column
    reqs.append({'updateSheetProperties': {
        'properties': {'sheetId': gid, 'gridProperties': {'frozenRowCount': HEADER_ROW + 1, 'frozenColumnCount': 1}},
        'fields': 'gridProperties.frozenRowCount,gridProperties.frozenColumnCount'}})
    # wrap day cells (multi-role) + top align across member rows
    if member_rows:
        reqs.append({'repeatCell': {
            'range': {'sheetId': gid, 'startRowIndex': min(member_rows), 'endRowIndex': max(member_rows) + 1,
                      'startColumnIndex': COL_DAYS_START, 'endColumnIndex': COL_DAYS_START + N_DAYS},
            'cell': {'userEnteredFormat': {'wrapStrategy': 'WRAP', 'verticalAlignment': 'TOP'}},
            'fields': 'userEnteredFormat(wrapStrategy,verticalAlignment)'}})
    # team header rows shaded + bold
    for r in team_rows:
        reqs.append({'repeatCell': {
            'range': {'sheetId': gid, 'startRowIndex': r, 'endRowIndex': r + 1, 'startColumnIndex': 0, 'endColumnIndex': COL_SIGNOFF + 1},
            'cell': {'userEnteredFormat': {'backgroundColor': TEAMGREY, 'textFormat': {'bold': True}}},
            'fields': 'userEnteredFormat(backgroundColor,textFormat)'}})
    # RAG day cells + result
    for r, statuses, eligible in day_status:
        for i, stt in enumerate(statuses):
            if stt == 'g':
                cell_bg(r, COL_DAYS_START + i, GREEN)
            elif stt == 'r':
                cell_bg(r, COL_DAYS_START + i, RED)
        cell_bg(r, COL_RESULT, GREEN if eligible else RED)
    # dropdowns
    validation(member_rows, COL_QUALITY, QUALITY_OPTS)
    validation(member_rows, COL_TIMELINES, TIMELINE_OPTS)
    validation(member_rows, COL_SIGNOFF, SIGNOFF_OPTS)
    # column widths
    for col, w in [(0, 90), (1, 80), (7, 50), (8, 70), (9, 90), (10, 95), (11, 230), (12, 120)]:
        reqs.append({'updateDimensionProperties': {
            'range': {'sheetId': gid, 'dimension': 'COLUMNS', 'startIndex': col, 'endIndex': col + 1},
            'properties': {'pixelSize': w}, 'fields': 'pixelSize'}})
    reqs.append({'updateDimensionProperties': {
        'range': {'sheetId': gid, 'dimension': 'COLUMNS', 'startIndex': COL_DAYS_START, 'endIndex': COL_DAYS_START + N_DAYS},
        'properties': {'pixelSize': 115}, 'fields': 'pixelSize'}})

    sh.batch_update({'requests': reqs})

    # ---- Role Moves tab ----
    try:
        mw = sh.worksheet('Role Moves')
    except gspread.WorksheetNotFound:
        mw = sh.add_worksheet('Role Moves', rows=200, cols=8)
    mrows = [[f"Role moves logged in Slack — week {span}"], [],
             ['Date', 'Person', 'From role', 'To role', 'Start', 'End']]
    any_move = False
    for d in dates:
        for mv in moves_by_date.get(d, []):
            any_move = True
            mrows.append([d.strftime('%a %d %b'), mv['name'], mv['from_role'],
                          mv['to_role'], mv['start'], mv['end']])
    if not any_move:
        mrows.append(['(no moves logged this week)'])
    mw.clear()
    mw.update(mrows, value_input_option='USER_ENTERED')
    mw.format('A1', {'textFormat': {'bold': True, 'fontSize': 12}})
    mw.format('A3:F3', {'textFormat': {'bold': True}})

    # ---- Slack post tab ----
    try:
        pw_ws = sh.worksheet('Slack post')
    except gspread.WorksheetNotFound:
        pw_ws = sh.add_worksheet('Slack post', rows=60, cols=2)
    msg = rt.build_reward_message_by_team(friday, week)
    post = f"{TL_TAGS}\n\n{msg}"
    pw_ws.clear()
    pw_ws.update([
        [f"Copy the cell below into #reward-time-tracker and tag the three TLs."],
        ["(@names need a quick re-select in Slack to become live mentions.)"],
        [post],
    ], value_input_option='RAW')
    pw_ws.format('A1', {'textFormat': {'bold': True, 'fontSize': 12}})

    # ---- Notes tab ----
    try:
        nw = sh.worksheet('Notes')
    except gspread.WorksheetNotFound:
        nw = sh.add_worksheet('Notes', rows=40, cols=1)
    nw.clear()
    nw.update([
        ["How this backup works"],
        ["• Throughput + skips are pulled live from Looker (internet — no laptop/VPN needed)."],
        ["• Role moves are read from the daily Slack thread and listed on the Role Moves tab."],
        ["• Roles / absences come from the last rota sync (the week must have been opened in the app once)."],
        ["• Each day cell shows every role worked that day, as 'Role actual/base' — green = hit base, red = missed."],
        ["• 'Reward slot' is when they'd take the time (from the reward-day plan)."],
        ["• 'Result (data)' is the throughput verdict only: hit base/stretch every working day, skips under limit."],
        ["• Quality ✓ / Timelines ✓ are dropdowns for you to sign off. Quality is pre-set from the triage archive % (Archive % column); timelines aren't in the data, so they default to 'check'."],
        ["• Cody-feedback quality and idle-time timelines are NOT in this backup — confirm those by eye."],
        ["• Sign-off dropdown: Approve / Grant base / Grant stretch / Query TL / Deny — your final call per person."],
        ["• ↻? next to a day = a move was logged in Slack but the rota structure wasn't split for it; check that person."],
        ["• The app may grant the odd 'within 1 of stretch' or ID-skip case at sign-off; the backup shows the strict data verdict, so you apply those judgement calls."],
        ["• Stretch = hit the stretch target every day (4h pro-rata); Base = hit base every day (3h pro-rata)."],
    ], value_input_option='RAW')
    nw.format('A1', {'textFormat': {'bold': True, 'fontSize': 12}})

    return sh.url


# ── CLI ──────────────────────────────────────────────────────────────────────
def current_reward_friday(today=None):
    """Friday that starts the reward week to show.
    Mon–Thu: the in-progress week (most recent Friday).
    Fri: the week that just ended yesterday (previous Friday), for sign-off."""
    today = today or date.today()
    most_recent_fri = today - timedelta(days=(today.weekday() - 4) % 7)
    if today.weekday() == 4:  # Friday — review the just-ended week
        return most_recent_fri - timedelta(days=7)
    return most_recent_fri


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--friday', help='reward-week Friday YYYY-MM-DD (default: current)')
    ap.add_argument('--share', help='email to share the sheet with (writer)')
    ap.add_argument('--print-only', action='store_true', help='print result, do not write sheet')
    args = ap.parse_args()

    friday = date.fromisoformat(args.friday) if args.friday else current_reward_friday()
    lk = Looker()
    dates, week, moves_by_date, moved = build(friday, lk)

    # console summary (parity check)
    print(f"Reward week Fri {friday} → Thu {dates[-1]}")
    for name in sorted(week):
        pw = week[name]
        if pw.days_worked == 0:
            continue
        pw.quality_ok = pw.timeline_ok = True
        elig, level, hours, reason = rt.calculate_eligibility(pw)
        verdict = f"{level} {rt.format_reward_hours(hours)}" if elig else f"NO ({reason})"
        print(f"  {name:10} skips={pw.skips:<3} {verdict}")

    if args.print_only:
        return

    gc = gspread.authorize(compat.get_google_credentials())
    sh = _get_or_create_sheet(gc, share_with=args.share)
    url = write_sheet(sh, friday, dates, week, moves_by_date, moved)
    print("Sheet:", url)


if __name__ == '__main__':
    main()
