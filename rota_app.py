"""
CS Rota Manager — Streamlit web app.

Run:  streamlit run rota_app.py
"""
import sys
import streamlit as st
import pandas as pd
import requests
from datetime import date, timedelta, datetime
from pathlib import Path

st.set_page_config(page_title="CS Rota Manager", page_icon="📋", layout="wide")

# ── Shared brand polish (mirrors tl_app_common.py) ─────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Overpass:wght@400;500;600;700&display=swap');

html, body, [class*="css"], [data-testid="stAppViewContainer"] {
  font-family: 'Overpass', -apple-system, BlinkMacSystemFont, sans-serif;
}
[data-testid="stAppViewContainer"] > .main > div.block-container {
  padding-top: 1.5rem;
  padding-bottom: 4rem;
  max-width: 1280px;
}
.stButton > button {
  border-radius: 8px;
  font-weight: 500;
  font-family: 'Overpass', sans-serif;
}
[data-testid="stMetric"] {
  background: #F8FAFD;
  padding: 12px 16px;
  border-radius: 10px;
  border: 1px solid #E5EBF3;
}
[data-testid="stMetricLabel"] {
  color: #6B6B6B !important;
  font-size: 0.78rem !important;
  font-weight: 600 !important;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
[data-testid="stMetricValue"] {
  color: #0F5CB8 !important;
  font-weight: 700 !important;
}
.stTabs [data-baseweb="tab-list"] {
  gap: 4px;
  background: #F4F7FB;
  padding: 4px;
  border-radius: 8px;
}
.stTabs [data-baseweb="tab"] {
  border-radius: 6px;
  padding: 8px 14px;
  font-weight: 500;
}
.stTabs [aria-selected="true"] {
  background: #fff !important;
  box-shadow: 0 1px 2px rgba(0,0,0,0.06);
  color: #0F5CB8 !important;
}
[data-testid="stSidebar"] {
  background: #F4F7FB;
  border-right: 1px solid #E5EBF3;
}
h2, h3 {
  color: #1A1A1A;
  font-weight: 600;
}
.aq-card {
  background: #F8FAFD;
  border: 1px solid #E5EBF3;
  border-radius: 10px;
  padding: 14px 16px;
  margin-bottom: 10px;
}
.aq-status-green { background:#E8F6E8; color:#196F3D; padding:3px 10px; border-radius:999px; font-size:0.82rem; font-weight:500; }
.aq-status-red   { background:#FADBD8; color:#922B21; padding:3px 10px; border-radius:999px; font-size:0.82rem; font-weight:500; }
.aq-status-amber { background:#FEF3D6; color:#8C5C00; padding:3px 10px; border-radius:999px; font-size:0.82rem; font-weight:500; }
.aq-status-blue  { background:#E6EEF8; color:#0F5CB8; padding:3px 10px; border-radius:999px; font-size:0.82rem; font-weight:500; }
.aq-status-grey  { background:#F2F3F4; color:#6B6B6B; padding:3px 10px; border-radius:999px; font-size:0.82rem; font-weight:500; }
</style>
""", unsafe_allow_html=True)

# ── Import rota engine ─────────────────────────────────────────────────────
import generate_rota as gr
from generate_rota import (
    ALL_TLS, ALL_AGENTS, DAY_NAMES, ABSENCE_ROLES,
    ROLE_PHONES, ROLE_TRIAGE, ROLE_TRIAGE_LC, ROLE_CHASING, ROLE_ICS,
    ROLE_TL, ROLE_AL, ROLE_ABSENCE, ROLE_NWD, ROLE_TRAINING,
    base_role, build_people, generate_lunch_rota,
    suggest_cover, build_dashboard_data,
    get_gspread,
    read_original_rota, read_daily_notes,
    DEFAULT_ROLE_TARGETS,
    DEFAULT_SKILLS, DEFAULT_SHIFTS,
)
import reward_time as rt
from reward_time import (
    get_reward_friday, get_weekday_dates, load_week, save_week,
    build_week, update_day_actuals, pull_day_data, calculate_eligibility, add_override,
    format_reward_hours,
    build_reward_slack_message, build_tl_messages,
    write_audit_entry, write_week_summary,
    adjust_shift_hours, split_day, unsplit_day,
    REWARD_DAYS, ALL_AGENTS as RT_AGENTS, TL_TEAMS, ROLE_TARGETS as RT_TARGETS,
    WEEKLY_HOURS, SKIP_THRESHOLD_WEEKLY, STANDARD_SHIFT_HOURS, DAILY_HOURS,
    SPLITTABLE_ROLES, RoleSegment,
)

@st.cache_resource(ttl=300)
def _cached_gspread():
    """Return a gspread client, cached for 5 minutes to avoid API quota exhaustion."""
    return get_gspread()


@st.cache_data(ttl=600)
def _load_historic_rota():
    """Load all historic rota data from the original rota sheet. Cached 10 mins."""
    gc = _cached_gspread()
    ss = gc.open_by_key(gr.EXISTING_ROTA_ID)
    ws = ss.worksheet("Staff View")
    all_data = ws.get_all_values()

    header = all_data[4]
    agent_cols = {}
    for idx, cell in enumerate(header[2:24], start=2):
        name = cell.strip()
        if name:
            agent_cols[name] = idx

    records = {}
    for row in all_data[5:]:
        if len(row) < 3 or not row[1].strip():
            continue
        d = gr._parse_uk_date(row[1])
        if d is None:
            continue
        for name, col in agent_cols.items():
            val = row[col].strip() if col < len(row) else ''
            if val:
                records[(name, d)] = val

    names = sorted(agent_cols.keys())
    dates = sorted(set(d for (_, d) in records.keys()))
    return records, names, dates


CORE_PHONES = ['Becky', 'Elida', 'Fionn', 'Jade', 'Kate']
WIDER_TEAM = ['Bella', 'Clare', 'Cris', 'Erika', 'Harriet',
              'Kirsty', 'Lizzie', 'Lucy', 'Maisha', 'Noemi', 'Sophie', 'Tara', 'Thea']


# ── Slack ──────────────────────────────────────────────────────────────────
SLACK_TOKEN_PATH = Path.home() / '.config/juno/claude-code/slack-token'
SLACK_CHANNEL_MORNING_MSG = 'C0AUP24HQPP'      # #dry-run-testing-jo
SLACK_CHANNEL_CS_MORNING = 'C02TP0FBM32'        # real CS channel — scheduled 07:30 post lands here

# Subteam mention for @client-support-team. Looked up via Slack search 2026-05-20.
CS_TEAM_SUBTEAM_ID = 'S02TFJF9PMW'
CS_TEAM_MENTION = f'<!subteam^{CS_TEAM_SUBTEAM_ID}|client-support-team>'


def _sanitize_for_dry_run(text: str) -> str:
    """Strip live mentions so a dry-run post can't ping the whole team.

    Replaces <!subteam^…|handle> with @handle plain text, and any
    <@U…> mentions with @<name>-style placeholders. The scheduled / live
    post still uses the real syntax — only the dry-run channel gets the
    de-fanged version."""
    import re
    text = re.sub(r'<!subteam\^[A-Z0-9]+\|([^>]+)>', r'@\1', text)
    text = re.sub(r'<@([UW][A-Z0-9]+)>', r'`<@\1>`', text)  # backtick-wrap user mentions
    return text

# ── Manual lunch-cover overrides (per Monday) ─────────────────────────────
# Jo can pick the 12-2 phone cover per day; choices persist in a small JSON
# file so they survive a page reload.
LUNCH_COVER_DIR = Path.home() / '.claude/scheduled-tasks/morning-message'


def _lunch_cover_file(monday):
    return LUNCH_COVER_DIR / f"lunch_cover_{monday.isoformat()}.json"


def load_lunch_overrides(monday):
    """Return {day_idx: name} or {} if none saved."""
    import json
    p = _lunch_cover_file(monday)
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text())
        return {int(k): v for k, v in raw.items()}
    except Exception:
        return {}


def save_lunch_overrides(monday, overrides):
    """Persist {day_idx: name} to disk."""
    import json
    LUNCH_COVER_DIR.mkdir(parents=True, exist_ok=True)
    _lunch_cover_file(monday).write_text(
        json.dumps({str(k): v for k, v in overrides.items() if v}, indent=2)
    )


# ── Scheduled morning messages ──────────────────────────────────────────────
# State: {(monday, day_idx): {'scheduled_id', 'channel', 'post_at_ts',
#                              'post_at_iso', 'channel_label', 'updated_at'}}
SCHEDULED_DIR = Path.home() / '.claude/scheduled-tasks/morning-message'


def _scheduled_file(monday):
    return SCHEDULED_DIR / f"scheduled_{monday.isoformat()}.json"


def load_scheduled_messages(monday):
    """Return {day_idx: scheduled_info_dict} or {} if none saved."""
    import json
    p = _scheduled_file(monday)
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text())
        return {int(k): v for k, v in raw.items()}
    except Exception:
        return {}


def save_scheduled_messages(monday, sched):
    """Persist scheduled-message state."""
    import json
    SCHEDULED_DIR.mkdir(parents=True, exist_ok=True)
    _scheduled_file(monday).write_text(
        json.dumps({str(k): v for k, v in sched.items() if v}, indent=2)
    )


def _seven_thirty_uk_unix(day_date):
    """Unix timestamp for 07:30 Europe/London on day_date. Handles BST/GMT."""
    from datetime import datetime, time
    try:
        from zoneinfo import ZoneInfo
        uk = ZoneInfo('Europe/London')
    except ImportError:
        from backports.zoneinfo import ZoneInfo  # py<3.9 fallback
        uk = ZoneInfo('Europe/London')
    target = datetime.combine(day_date, time(7, 30), tzinfo=uk)
    return int(target.timestamp())


def schedule_slack_message(channel, text, post_at_ts):
    """Call chat.scheduleMessage. Returns scheduled_message_id."""
    token = SLACK_TOKEN_PATH.read_text().strip()
    resp = requests.post(
        'https://slack.com/api/chat.scheduleMessage',
        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
        json={'channel': channel, 'text': text, 'post_at': post_at_ts},
        timeout=10,
    )
    data = resp.json()
    if not data.get('ok'):
        raise RuntimeError(f"chat.scheduleMessage failed: {data.get('error')}")
    return data['scheduled_message_id']


def delete_scheduled_message(channel, scheduled_message_id):
    """Cancel a previously scheduled message. Idempotent — returns None on
    'invalid_scheduled_message_id' (e.g. already sent or already deleted)."""
    token = SLACK_TOKEN_PATH.read_text().strip()
    resp = requests.post(
        'https://slack.com/api/chat.deleteScheduledMessage',
        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
        json={'channel': channel, 'scheduled_message_id': scheduled_message_id},
        timeout=10,
    )
    data = resp.json()
    if not data.get('ok'):
        err = data.get('error', '')
        if err in ('invalid_scheduled_message_id', 'message_not_found'):
            return None  # already gone — safe to ignore
        raise RuntimeError(f"chat.deleteScheduledMessage failed: {err}")
    return data


def send_slack_message(channel, text, link_names=False):
    """Post to Slack. Pass link_names=True when the message contains mentions
    that must be resolved & pinged (e.g. <@U…> tagging the cover person)."""
    token = SLACK_TOKEN_PATH.read_text().strip()
    payload = {'channel': channel, 'text': text}
    if link_names:
        payload['link_names'] = True
    resp = requests.post(
        'https://slack.com/api/chat.postMessage',
        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
        json=payload,
        timeout=10,
    )
    data = resp.json()
    if not data.get('ok'):
        raise RuntimeError(f"Slack API error: {data.get('error', 'unknown')}")
    return data


# ── Slack user resolution (for DMing cover team) ───────────────────────────
# Short-name → "First Last" for everyone we might DM
FULL_NAMES = {
    'Becky': 'Becky Smith', 'Bella': 'Bella Brayford', 'Clare': 'Clare Brown',
    'Cris': 'Cris Macagi', 'Elida': 'Elida Gizli', 'Erika': 'Erika Frolova',
    'Fionn': 'Fionn Burrows', 'Harriet': 'Harriet Clifton-Sprigg',
    'Jade': 'Jade Regent', 'Kate': "Kate O'Neill", 'Kirsty': 'Kirsty Rowley',
    'Lizzie': 'Lizzie Williamson', 'Lucy': 'Lucy Riordan',
    'Maisha': 'Maisha Begum', 'Noemi': 'Noemi Sip', 'Sophie': 'Sophie Maloney',
    'Tara': 'Tara Dunkley', 'Thea': 'Thea Willsmore',
    'Jess': 'Jess Jackson', 'Yasmin': 'Yasmin Aly', 'Courtney': 'Courtney Elijah',
    'Jo': 'Joanne Jeffries',
}

# Slack workspace `name` overrides — where auto-generated firstname.lastname doesn't match.
# Source of truth: users.list real_name + workspace name field.
SLACK_WORKSPACE_NAMES = {
    'Harriet': 'harriet.clifton-sprig',   # Slack truncates the second 'g'
    'Lizzie':  'elizabeth.williamson',    # formal first name in Slack
    'Jo':      'joanne.jeffries',         # formal first name
}


@st.cache_data(ttl=86400)
def _slack_users_directory():
    """Fetch the full Slack workspace user list and index by workspace name.

    The token only has users:read (not users:read.email), so this paginates
    users.list and builds {workspace_name: user_id}. Workspace name is
    typically firstname.lastname — much more stable than real_name.
    """
    token = SLACK_TOKEN_PATH.read_text().strip()
    directory = {}
    cursor = None
    while True:
        params = {'limit': 200}
        if cursor:
            params['cursor'] = cursor
        resp = requests.get(
            'https://slack.com/api/users.list',
            headers={'Authorization': f'Bearer {token}'},
            params=params, timeout=15,
        )
        data = resp.json()
        if not data.get('ok'):
            return directory
        for u in data.get('members', []):
            if u.get('deleted') or u.get('is_bot'):
                continue
            name = (u.get('name') or '').lower()
            if name:
                directory[name] = u['id']
        cursor = data.get('response_metadata', {}).get('next_cursor')
        if not cursor:
            break
    return directory


def _resolve_slack_user_id(name: str):
    """Resolve a short or full name → Slack user ID.

    1. Check SLACK_WORKSPACE_NAMES override.
    2. Build candidate firstname.lastname workspace names and look up in the
       cached users.list directory.
    """
    directory = _slack_users_directory()
    override = SLACK_WORKSPACE_NAMES.get(name)
    if override and override in directory:
        return directory[override]
    full = FULL_NAMES.get(name, name)
    parts = full.replace("'", '').split()
    if len(parts) < 2:
        return None
    first = parts[0].lower()
    last_h = '-'.join(parts[1:]).lower()
    last_nh = last_h.replace('-', '')
    candidates = [f"{first}.{last_h}"]
    if last_nh != last_h:
        candidates.append(f"{first}.{last_nh}")
    # Try a "-sprig" style 14-char truncation in case Slack chopped the surname
    candidates.append(f"{first}.{last_h[:14]}")
    for cand in candidates:
        if cand in directory:
            return directory[cand]
    return None


def _open_dm_channel(user_id: str):
    """Open (or fetch) a 1-1 DM channel ID for a user."""
    token = SLACK_TOKEN_PATH.read_text().strip()
    resp = requests.post(
        'https://slack.com/api/conversations.open',
        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
        json={'users': user_id}, timeout=10,
    )
    data = resp.json()
    if not data.get('ok'):
        raise RuntimeError(f"conversations.open failed: {data.get('error')}")
    return data['channel']['id']


TL_PEGASUS_CHANNELS = {
    'Yasmin':   'C06Q759E8P6',   # #pegasus-yasmin
    'Jess':     'C07R3B17M0F',   # #pegasus-jess
    'Courtney': 'C070TN8CEQK',   # #pegasus-courtney
}

# Per-person pegasus channels. Suffix conventions in the workspace are mixed
# (-j / -y / -c / -l) so this is a hard-coded source of truth resolved from
# conversations.list on 2026-05-20.
PEGASUS_CHANNELS = {
    'Becky':    'C05FK5N5JR3',   # #pegasus-becky-c
    'Bella':    'C0907STFMNY',   # #pegasus-bella-j
    'Clare':    'C09728A769E',   # #pegasus-clare-j
    'Cris':     'C0925KLHDFD',   # #pegasus-cris-j
    'Elida':    'C0APEK7FFU0',   # #pegasus-elida-c
    'Erika':    'C09G2DF5EQP',   # #pegasus-erika-l
    'Fionn':    'C06L8394GAZ',   # #pegasus-fionn-c
    'Harriet':  'C09JG8XGVHA',   # #pegasus-harriet-l
    'Jade':     'C0AGJPAJWQ1',   # #pegasus-jade-c
    'Kate':     'C05FN07KF8B',   # #pegasus-kate-c
    'Kirsty':   'C0AMJTHL4SZ',   # #pegasus-kirsty-y
    'Lizzie':   'C072AQCBG15',   # #pegasus-lizzie-l
    'Lucy':     'C0AU24PSF39',   # #pegasus-lucy-j
    'Maisha':   'C09GN3P0JA0',   # #pegasus-maisha-j
    'Noemi':    'C09AJD6T7H7',   # #pegasus-noemi-y
    'Sophie':   'C095Y5UAFV4',   # #pegasus-sophie-y
    'Tara':     'C07TH1E24EP',   # #pegasus-tara-y
    'Thea':     'C09TASX0FLY',   # #pegasus-thea-l
    # TLs use their own channel
    'Courtney': 'C070TN8CEQK',
    'Jess':     'C07R3B17M0F',
    'Yasmin':   'C06Q759E8P6',
}

# Pretty labels for display (match the actual workspace channel names).
PEGASUS_LABELS = {
    'Becky': '#pegasus-becky-c',  'Bella': '#pegasus-bella-j',
    'Clare': '#pegasus-clare-j',  'Cris': '#pegasus-cris-j',
    'Elida': '#pegasus-elida-c',  'Erika': '#pegasus-erika-l',
    'Fionn': '#pegasus-fionn-c',  'Harriet': '#pegasus-harriet-l',
    'Jade': '#pegasus-jade-c',    'Kate': '#pegasus-kate-c',
    'Kirsty': '#pegasus-kirsty-y','Lizzie': '#pegasus-lizzie-l',
    'Lucy': '#pegasus-lucy-j',    'Maisha': '#pegasus-maisha-j',
    'Noemi': '#pegasus-noemi-y',  'Sophie': '#pegasus-sophie-y',
    'Tara': '#pegasus-tara-y',    'Thea': '#pegasus-thea-l',
    'Courtney': '#pegasus-courtney',
    'Jess': '#pegasus-jess',
    'Yasmin': '#pegasus-yasmin',
}


def _find_persons_pegasus(name: str):
    """Return (channel_label, channel_id) for a cover person's own pegasus channel."""
    channel_id = PEGASUS_CHANNELS.get(name)
    if not channel_id:
        return None, None
    return PEGASUS_LABELS.get(name, f"#pegasus-{name.lower()}"), channel_id


def build_cover_notifications(day_idx, day_label, phone_cover, daily_notes):
    """Return [(cover_person, target_or_purpose, time_str)] for the day.

    Each tuple is later turned into a Slack message and posted to that
    cover person's pegasus channel.
      - For lunch phone cover: target = 'phones (lunch)', time = '12:00–14:00'
      - For daily-notes cover: target = the person being covered, time = entry time
    Entries with no cover person (TBC) are skipped — nobody to ping.
    """
    items = []
    if phone_cover and day_idx in phone_cover:
        items.append((phone_cover[day_idx], 'phones (lunch)', '12:00–14:00'))
    if daily_notes and day_idx in daily_notes:
        for entry in daily_notes[day_idx]:
            if not entry.get('cover_needed'):
                continue
            who = entry.get('whos_covering')
            target = entry.get('name')
            if not who:
                continue  # TBC — nobody to notify
            items.append((who, target, entry.get('time') or ''))
    return items


# Kept as an alias so older callers don't break. New code should use
# build_cover_notifications / send_cover_notifications.
def build_cover_dms(day_idx, day_label, phone_cover, daily_notes):
    return build_cover_notifications(day_idx, day_label, phone_cover, daily_notes)


def send_cover_notifications(items, day_label, dry_run=True):
    """Post one message per cover person to their pegasus channel.

    Returns [(name, channel_label, ok, error_msg_or_None)].
    When dry_run is True every post is routed to #dry-run-testing-jo with a
    [DRY RUN — would post to #pegasus-<tl>] prefix so Jo can sanity-check.
    """
    results = []
    for cover_name, target, time_str in items:
        channel_label, channel = _find_persons_pegasus(cover_name)
        if not channel:
            results.append((cover_name, '(no pegasus channel)', False,
                            f"No pegasus channel mapped for {cover_name}"))
            continue

        user_id = _resolve_slack_user_id(cover_name)
        # Two mention forms:
        # - live_mention: real Slack tag → pings the user
        # - preview_mention: visible name only, no ping (for dry-run channel
        #   so test posts don't notify the real person)
        live_mention = f"<@{user_id}>" if user_id else f"*{cover_name}*"
        preview_mention = (
            f"*{cover_name}* _(would tag `<@{user_id}>`)_"
            if user_id else f"*{cover_name}*"
        )

        def _body(mention):
            if target == 'phones (lunch)':
                return f"Hi {mention} 👋 You're on phone cover during lunch on {day_label}, {time_str}. Thanks!"
            time_paren = f" ({time_str})" if time_str else ""
            return f"Hi {mention} 👋 You're covering {target} on {day_label}{time_paren}. Thanks!"

        if dry_run:
            preview = (
                f"[DRY RUN — would post to {channel_label} and tag "
                f"<@{user_id or '?'}>]\n\n{_body(preview_mention)}"
            )
            try:
                # No link_names in dry-run — preview mention is plain text and
                # won't ping anyone.
                send_slack_message(SLACK_CHANNEL_MORNING_MSG, preview)
                results.append((cover_name, channel_label, True, None))
            except Exception as e:
                results.append((cover_name, channel_label, False, str(e)))
        else:
            try:
                # link_names ensures Slack resolves and pings the mention even
                # if formatting is slightly off.
                send_slack_message(channel, _body(live_mention), link_names=True)
                results.append((cover_name, channel_label, True, None))
            except Exception as e:
                results.append((cover_name, channel_label, False, str(e)))
    return results


# Backwards-compat shim — call sites that previously used send_cover_dms.
def send_cover_dms(dms, dry_run=True):
    """Compatibility shim: maps legacy (name, message_string) tuples back
    through the new pegasus-channel sender. Not used by current callers."""
    return [(n, '(legacy)', False, 'send_cover_dms is deprecated — use send_cover_notifications')
            for n, _ in dms]


# ── Colours ────────────────────────────────────────────────────────────────
_DARK_TEXT = "; color: #1a1a1a"
ROLE_CSS = {
    ROLE_PHONES:    "background-color: #cce4ff" + _DARK_TEXT,
    ROLE_TRIAGE:    "background-color: #d8ffd8" + _DARK_TEXT,
    ROLE_TRIAGE_LC: "background-color: #f2f2b2" + _DARK_TEXT,
    ROLE_CHASING:   "background-color: #ffe5cc" + _DARK_TEXT,
    ROLE_ICS:       "background-color: #e5d8ff" + _DARK_TEXT,
    ROLE_TL:        "background-color: #d8d8d8" + _DARK_TEXT,
    ROLE_AL:        "background-color: #ffcccc" + _DARK_TEXT,
    ROLE_ABSENCE:   "background-color: #ffb2b2" + _DARK_TEXT,
    ROLE_NWD:       "background-color: #e5e5e5" + _DARK_TEXT,
    ROLE_TRAINING:  "background-color: #ccffff" + _DARK_TEXT,
}


def role_style(val):
    if not val or not isinstance(val, str):
        return ""
    if val.startswith(ROLE_PHONES):
        return ROLE_CSS[ROLE_PHONES]
    return ROLE_CSS.get(val, "")


def rag_style(val, target, minimum):
    if not isinstance(val, (int, float)):
        return ""
    if val >= target:
        return "background-color: #d8ffd8" + _DARK_TEXT
    if val >= minimum:
        return "background-color: #ffe5cc" + _DARK_TEXT
    return "background-color: #ffcccc" + _DARK_TEXT


# ── State helpers ──────────────────────────────────────────────────────────

def get_monday(d):
    return d - timedelta(days=d.weekday())


def load_config():
    """Load config (cached per session). Uses built-in defaults."""
    if "people" in st.session_state:
        return st.session_state["people"]

    people = build_people()  # Uses DEFAULT_SKILLS, DEFAULT_HOURS, etc.
    st.session_state["people"] = people
    return people


def build_rota_df(assignments):
    """Convert assignments dict to a styled DataFrame."""
    active_names = set(assignments.keys())
    col_order = [n for n in ALL_TLS + CORE_PHONES + WIDER_TEAM if n in active_names]

    rows = []
    for di in range(5):
        row = {"Day": DAY_NAMES[di]}
        for name in col_order:
            row[name] = assignments.get(name, {}).get(di, "")
        rows.append(row)

    df = pd.DataFrame(rows)
    df = df.set_index("Day")
    return df, col_order


def build_dashboard_df(dashboard_data):
    """Convert dashboard data to a DataFrame."""
    rows = []
    for d in dashboard_data:
        row = {"Role": d["role"]}
        for day in DAY_NAMES:
            row[day] = d.get(day, "")
        row["Target"] = d.get("target", "")
        row["Min"] = d.get("minimum", "")
        rows.append(row)
    df = pd.DataFrame(rows)
    df = df.set_index("Role")
    return df


def select_phone_cover(assignments):
    """Pick the best phone-cover person for 12–2 each day of the week.

    Selection rules:
    - Must have 'Inbound' in their skills
    - Must be working that day (not absent / NWD / training)
    - Must NOT already be on phones
    - Must NOT be on Triage + Lender Chasing (can't leave that role)
    - Shift must overlap 12–14
    - Prefer the person available most days (week-long consistency)
    - Tie-break: prefer roles with more headroom (triage > chasing > case setup)

    Returns {day_idx: name} for days 0–4.
    """
    # Role headroom priority — higher = more headroom, easier to pull from
    ROLE_HEADROOM = {
        ROLE_TRIAGE: 3,     # target 5, min 4 — most headroom
        ROLE_CHASING: 2,    # target 3, min 2
        ROLE_ICS: 1,        # target 2, min 1
    }

    # Step 1: build per-day eligible candidates with their current role
    eligible = {}  # {day_idx: [(name, role, headroom_score)]}
    for di in range(5):
        day_candidates = []
        # Count how many are on each role this day (for headroom calc)
        role_counts = {}
        for name in ALL_TLS + CORE_PHONES + WIDER_TEAM:
            val = assignments.get(name, {}).get(di, "")
            if val and val not in ABSENCE_ROLES:
                br = base_role(val)
                role_counts[br] = role_counts.get(br, 0) + 1

        for name in ALL_TLS + CORE_PHONES + WIDER_TEAM:
            val = assignments.get(name, {}).get(di, "")
            if not val or val in ABSENCE_ROLES:
                continue
            br = base_role(val)
            # Already on phones — skip
            if br == ROLE_PHONES:
                continue
            # On triage + lender chasing — can't leave
            if br == ROLE_TRIAGE_LC:
                continue
            # Must be trained on Inbound
            skills = DEFAULT_SKILLS.get(name, set())
            if 'Inbound' not in skills:
                continue
            # Shift must cover 12–14
            shift = DEFAULT_SHIFTS.get(name)
            if shift:
                start, end = shift
                if end <= 12 or start >= 14:
                    continue
            headroom = ROLE_HEADROOM.get(br, 0)
            day_candidates.append((name, br, headroom))
        eligible[di] = day_candidates

    # Step 2: score each candidate by how many days they're available
    availability = {}  # name -> count of days available
    for di, candidates in eligible.items():
        for name, _, _ in candidates:
            availability[name] = availability.get(name, 0) + 1

    # Step 3: pick the primary cover person — available most days, best headroom
    # Then for any day they can't do, pick the next best
    result = {}
    primary = None
    if availability:
        # Sort by: days available (desc), then max headroom across their days (desc), then name
        def _candidate_score(n):
            max_headroom = max(
                (h for di, cands in eligible.items() for nm, _, h in cands if nm == n),
                default=0,
            )
            return (-availability[n], -max_headroom, n)
        primary = min(availability.keys(), key=_candidate_score)

    for di in range(5):
        candidates = eligible.get(di, [])
        if not candidates:
            continue
        names_this_day = {c[0] for c in candidates}
        if primary and primary in names_this_day:
            result[di] = primary
        else:
            # Fallback: best headroom among today's candidates
            best = max(candidates, key=lambda c: (c[2], -ord(c[0][0])))
            result[di] = best[0]

    return result


def build_slack_message(assignments, day_idx, lunch_rota=None,
                        cover_suggestions=None, people=None,
                        phone_cover=None, daily_notes=None,
                        reward_status=None):
    """Build the morning Slack message for a given day.

    reward_status (optional): {day_idx: {'reward_time': [...], 'half_day': [...]}}
        from reward_time.find_reward_and_half_days — each entry has
        name / hours / role / cover_needed.
    """
    lines = [f"Good morning {CS_TEAM_MENTION} 📣", ""]

    # ── TL on lead (top of message) ──
    for name in ALL_TLS:
        val = assignments.get(name, {}).get(day_idx, "")
        if val == ROLE_TL:
            lines.append(f"💼 Team Lead: {name}")
            lines.append("")
            break

    # ── Absences ──
    absent = []
    nwd = []
    training = []
    for name in ALL_TLS + CORE_PHONES + WIDER_TEAM:
        val = assignments.get(name, {}).get(day_idx, "")
        if val in (ROLE_AL, ROLE_ABSENCE):
            absent.append(name)
        elif val == ROLE_TRAINING:
            training.append(name)
        elif val == ROLE_NWD:
            nwd.append(name)

    if absent or nwd:
        lines.append("🚫 Off today")
        lines.extend(absent)
        lines.extend(nwd)
        lines.append("")

    if training:
        lines.append("📚 Training")
        lines.extend(training)
        lines.append("")

    # ── Role assignments ──
    role_groups = [
        ("📞 Phones", ROLE_PHONES, True),
        ("📋 Triage", ROLE_TRIAGE, False),
        ("🔔 Triage + Lender Chasing", ROLE_TRIAGE_LC, False),
        ("📞 Chasing", ROLE_CHASING, False),
        ("📁 Case Setup", ROLE_ICS, False),
    ]

    for header, role, show_secondary in role_groups:
        lines.append(header)
        people_on = []
        for name in ALL_TLS + CORE_PHONES + WIDER_TEAM:
            val = assignments.get(name, {}).get(day_idx, "")
            if not val:
                continue
            br = base_role(val)
            if br == role:
                if show_secondary and val != role and " + " in val:
                    secondary = val.split(" + ", 1)[1]
                    people_on.append(f"{name}  + {secondary}")
                else:
                    people_on.append(name)
        if people_on:
            lines.extend(people_on)
        else:
            lines.append("No one assigned")
        lines.append("")

    # ── Lunch rota ──
    if lunch_rota and day_idx in lunch_rota and lunch_rota[day_idx]:
        lines.append("🍽️ Lunch")
        for time_slot, name in sorted(lunch_rota[day_idx], key=lambda x: x[0]):
            lines.append(f"{name}: {time_slot}")
        lines.append("")

    # ── Reward time ──
    rs = (reward_status or {}).get(day_idx) or {}
    reward_entries = rs.get('reward_time') or []
    half_day_entries = rs.get('half_day') or []

    def _fmt_hours(h):
        # 3.0 → "3h" · 3.5 → "3h 30m"
        whole = int(h)
        mins = int(round((h - whole) * 60))
        if mins == 0:
            return f"{whole}h"
        return f"{whole}h {mins}m"

    def _cover_tag(needed):
        return "⚠️ cover needed" if needed else "no cover needed"

    if reward_entries:
        lines.append("🏆 Reward time")
        for e in reward_entries:
            role_note = f" ({e['main_role']})" if e.get('main_role') else ""
            lines.append(
                f"{e['name']}: {_fmt_hours(e['hours'])}{role_note} — {_cover_tag(e['cover_needed'])}"
            )
        lines.append("")

    if half_day_entries:
        lines.append("⏰ Half day")
        for e in half_day_entries:
            role_note = f" — {e['role']}" if e.get('role') else ""
            lines.append(
                f"{e['name']}: {_fmt_hours(e['hours'])}{role_note} — {_cover_tag(e['cover_needed'])}"
            )
        lines.append("")

    # ── Cover (lunch hour phones + daily-notes cover) ──
    # No free-text notes — they can contain appointments / sensitive info.
    cover_lines = []
    if phone_cover and day_idx in phone_cover:
        cover_lines.append(f"{phone_cover[day_idx]} — phones (lunch 12:00–14:00)")
    if daily_notes and day_idx in daily_notes:
        for entry in daily_notes[day_idx]:
            if not entry.get('cover_needed'):
                continue
            who = entry.get('whos_covering')
            time = entry.get('time')
            target = entry.get('name')
            time_paren = f" ({time})" if time else ""
            if who:
                cover_lines.append(f"{who} — covering {target}{time_paren}")
            else:
                cover_lines.append(f"⚠️ {target}{time_paren} — cover TBC")
    if cover_lines:
        lines.append("🔄 Cover")
        lines.extend(cover_lines)
        lines.append("")

    lines.append("Have a wonderful day! 🌟")
    return "\n".join(lines)


def build_lunch_df(lunch_rota, monday):
    """Convert lunch rota to DataFrame."""
    slots = ["12:00-13:00", "13:00-14:00", "13:30-14:00", "14:00-15:00"]
    rows = []
    for di in range(5):
        day_date = monday + timedelta(days=di)
        row = {"Day": f"{DAY_NAMES[di]} {day_date.strftime('%d/%m')}"}
        slot_map = {s: "" for s in slots}
        for time_slot, name in lunch_rota.get(di, []):
            slot_map[time_slot] = name
        row.update(slot_map)
        rows.append(row)
    return pd.DataFrame(rows).set_index("Day")


# ── App ────────────────────────────────────────────────────────────────────

# Sidebar
with st.sidebar:
    st.title("📋 CS Rota Manager")
    st.divider()

    page = st.radio("Navigation", [
        "🗓️ Weekly Rota",
        "📊 Dashboard",
        "💬 Morning Message",
        "🍽️ Lunch Rota",
        "🔍 Role Lookup",
        "🏆 Reward Time",
        "⚙️ Pull Rota",
    ], label_visibility="collapsed")

    st.divider()
    today = date.today()
    this_monday = get_monday(today)
    selected_monday = st.date_input(
        "Week commencing",
        value=this_monday,
        help="Pick any Monday to view/generate that week",
    )
    if selected_monday.weekday() != 0:
        selected_monday = get_monday(selected_monday)
        st.caption(f"Adjusted to Monday: {selected_monday.strftime('%d %b %Y')}")

    st.divider()
    st.caption("**Cover notifications**")
    dms_live = st.toggle(
        "Post to pegasus channels for real",
        value=st.session_state.get('cover_dm_live', False),
        key='cover_dm_live',
        help="Off (default) = posts a preview to #dry-run-testing-jo. On = posts to each cover person's #pegasus-<tl> channel with an @mention.",
    )
    if dms_live:
        st.caption("⚠️ Live — posts go to #pegasus-jess / #pegasus-yasmin / #pegasus-courtney with @mentions.")
    else:
        st.caption("🧪 Dry-run — previews go to your dry-run channel.")

    st.divider()
    st.caption("Data source: [Rota Sheet](https://docs.google.com/spreadsheets/d/1CMSEZSb-4D4mO6iPb8tVSaAPsZT5KZst9VSXH4bpi0Y)")

# ── Load data ──
people = load_config()
if people is None:
    st.error("Could not load config. Check the generate_rota.py configuration.")
    st.stop()

# Check if we have assignments in session state for this week
week_key = selected_monday.isoformat()
if f"assignments_{week_key}" not in st.session_state:
    # Try to load from the original rota sheet
    try:
        gc = _cached_gspread()
        existing, pa = read_original_rota(gc, selected_monday)
        if any(any(days.values()) for days in existing.values()):
            st.session_state[f"assignments_{week_key}"] = existing
            st.session_state[f"phone_agents_{week_key}"] = pa
    except Exception:
        pass

assignments = st.session_state.get(f"assignments_{week_key}")
phone_agents = st.session_state.get(f"phone_agents_{week_key}")


# ═══════════════════════════════════════════════════════════════════════════
# PAGES
# ═══════════════════════════════════════════════════════════════════════════

if page == "🗓️ Weekly Rota":
    st.header(f"Weekly Rota — w/c {selected_monday.strftime('%d %b %Y')}")

    if assignments is None:
        st.info("No rota found for this week. Go to **Pull Rota** to load it from the spreadsheet.")
        st.stop()

    df, col_order = build_rota_df(assignments)

    # Apply role colours
    def style_rota(df):
        return df.style.map(role_style)

    st.dataframe(
        style_rota(df),
        use_container_width=True,
        height=220,
    )

    # Legend
    with st.expander("Colour legend"):
        legend_cols = st.columns(5)
        legend_items = [
            ("🔵 Phones", "#cce4ff"),
            ("🟢 Triage", "#d8ffd8"),
            ("🟡 T+LC", "#f2f2b2"),
            ("🟠 Chasing", "#ffe5cc"),
            ("🟣 Case Setup", "#e5d8ff"),
        ]
        for col, (label, colour) in zip(legend_cols, legend_items):
            col.markdown(f'<span style="background-color:{colour};padding:4px 8px;border-radius:4px">{label}</span>', unsafe_allow_html=True)

        legend_cols2 = st.columns(5)
        legend_items2 = [
            ("⬜ Team Lead", "#d8d8d8"),
            ("🔴 Annual Leave", "#ffcccc"),
            ("🔴 Absence", "#ffb2b2"),
            ("⬜ NWD", "#e5e5e5"),
            ("🔵 Training", "#ccffff"),
        ]
        for col, (label, colour) in zip(legend_cols2, legend_items2):
            col.markdown(f'<span style="background-color:{colour};padding:4px 8px;border-radius:4px">{label}</span>', unsafe_allow_html=True)

    # Quick absence marker
    st.subheader("Mark absence")
    abs_cols = st.columns([2, 2, 2, 1])
    active_names = sorted(set(assignments.keys()) - set(ALL_TLS))
    with abs_cols[0]:
        abs_name = st.selectbox("Person", [""] + active_names + ALL_TLS, key="abs_name")
    with abs_cols[1]:
        abs_day = st.selectbox("Day", DAY_NAMES, key="abs_day")
    with abs_cols[2]:
        abs_type = st.selectbox("Type", [ROLE_AL, ROLE_ABSENCE], key="abs_type")
    with abs_cols[3]:
        st.markdown("<br>", unsafe_allow_html=True)
        if st.button("Apply", use_container_width=True):
            if abs_name:
                di = DAY_NAMES.index(abs_day)
                assignments[abs_name][di] = abs_type
                st.session_state[f"assignments_{week_key}"] = assignments
                st.rerun()


elif page == "📊 Dashboard":
    st.header(f"Dashboard — w/c {selected_monday.strftime('%d %b %Y')}")

    if assignments is None:
        st.info("No rota found for this week. Go to **Pull Rota** to load it.")
        st.stop()

    dashboard_data = build_dashboard_data(assignments)
    cover_suggestions = suggest_cover(assignments, people)
    df = build_dashboard_df(dashboard_data)

    # Style with RAG colours
    def style_dashboard(styler):
        for row_data in dashboard_data:
            role = row_data["role"]
            target = row_data.get("target", "")
            minimum = row_data.get("minimum", "")
            if not target:
                continue
            for day in DAY_NAMES:
                actual = row_data.get(day, 0)
                bg = rag_style(actual, target, minimum)
                if bg:
                    styler = styler.map(
                        lambda v, bg=bg: bg if v == actual else "",
                        subset=(role, day)
                    )
        return styler

    def style_dashboard_cells(styler):
        for i, row_data in enumerate(dashboard_data):
            target = row_data.get("target", "")
            minimum = row_data.get("minimum", "")
            if not target:
                continue
            role = row_data["role"]
            for day in DAY_NAMES:
                actual = row_data.get(day, 0)
                css = rag_style(actual, target, minimum)
                if css:
                    styler = styler.map(lambda v, css=css: css,
                                        subset=(role, day))
        return styler

    st.dataframe(
        style_dashboard_cells(df.style),
        use_container_width=True,
        height=340,
    )

    # RAG legend
    legend_c = st.columns(3)
    legend_c[0].markdown('<span style="background-color:#d8ffd8;padding:4px 12px;border-radius:4px">🟢 At/above target</span>', unsafe_allow_html=True)
    legend_c[1].markdown('<span style="background-color:#ffe5cc;padding:4px 12px;border-radius:4px">🟡 Above minimum, below target</span>', unsafe_allow_html=True)
    legend_c[2].markdown('<span style="background-color:#ffcccc;padding:4px 12px;border-radius:4px">🔴 Below minimum</span>', unsafe_allow_html=True)

    # Cover suggestions
    if cover_suggestions:
        st.subheader("Cover suggestions")
        for sug in cover_suggestions:
            severity = "🔴" if sug["cover_required"] else "🟡"
            st.markdown(f"**{severity} {sug['day']}: {sug['role']}** — {sug['current']}/{sug['target']} (min {sug['minimum']})")
            if sug["suggestions"]:
                for c in sug["suggestions"][:3]:
                    st.markdown(f"&nbsp;&nbsp;&nbsp;&nbsp;→ Move **{c['name']}** from {c['current_role']} (headroom: {c['headroom']})")
            else:
                st.markdown("&nbsp;&nbsp;&nbsp;&nbsp;_No candidates available_")
    else:
        st.success("All roles at or above minimum. No cover needed.")


elif page == "💬 Morning Message":
    st.header(f"Morning Message — w/c {selected_monday.strftime('%d %b %Y')}")

    if assignments is None:
        st.info("No rota found for this week. Go to **Pull Rota** to load it.")
        st.stop()

    # Build lunch rota, phone cover, daily notes, and cover suggestions
    lunch_rota = None
    if phone_agents:
        lunch_rota = generate_lunch_rota(phone_agents, people, selected_monday)
    auto_phone_cover = select_phone_cover(assignments)
    lunch_overrides = load_lunch_overrides(selected_monday)

    def _effective_phone_cover():
        """Apply lunch overrides on top of the auto-pick. '__none__' = suppress."""
        out = {}
        for di in range(5):
            ov = lunch_overrides.get(di)
            if ov == '__none__':
                continue        # explicitly skip
            if ov:
                out[di] = ov     # manual pick
            elif di in auto_phone_cover:
                out[di] = auto_phone_cover[di]
        return out

    phone_cover = _effective_phone_cover()
    cover_suggestions = suggest_cover(assignments, people)

    # Pull daily notes from the original rota sheet
    daily_notes = None
    try:
        gc = _cached_gspread()
        daily_notes = read_daily_notes(gc, selected_monday)
    except Exception:
        pass

    # ── Reward time + half-day status from saved reward week state ──
    # A rota week's Mon-Thu sit in the previous Friday's reward week.
    # The rota week's Friday starts the next reward week.
    # Load both reward weeks and pick the right one per day.
    reward_status = {}
    week_caches = {}
    for di in range(5):
        day_date = selected_monday + timedelta(days=di)
        rw_fri = rt.get_reward_friday(day_date)
        if rw_fri not in week_caches:
            try:
                week_caches[rw_fri] = rt.load_week(rw_fri)
            except Exception:
                week_caches[rw_fri] = {}
        rw_data = week_caches[rw_fri]
        if rw_data:
            try:
                reward_status[di] = rt.find_reward_and_half_days(rw_data, day_date)
            except Exception:
                reward_status[di] = {'reward_time': [], 'half_day': []}

    scheduled = load_scheduled_messages(selected_monday)

    # Build the list of people working each day (for the lunch cover dropdown)
    def _people_working(day_idx):
        names = []
        for name in ALL_TLS + CORE_PHONES + WIDER_TEAM:
            val = assignments.get(name, {}).get(day_idx, "")
            if not val or val in (ROLE_AL, ROLE_ABSENCE, ROLE_NWD, ROLE_TRAINING):
                continue
            names.append(name)
        return sorted(set(names))

    day_tabs = st.tabs(DAY_NAMES)
    for di, tab in enumerate(day_tabs):
        with tab:
            day_date = selected_monday + timedelta(days=di)

            # ── Lunch cover picker ──
            auto_pick = auto_phone_cover.get(di) or "(no one)"
            override_raw = lunch_overrides.get(di) or ''
            options = ["(auto)", "(no one)"] + _people_working(di)
            # Map saved sentinel → display label
            if override_raw == '__none__':
                current = "(no one)"
            elif override_raw:
                current = override_raw
                if current not in options:
                    options.append(current)
            else:
                current = "(auto)"
            with st.container(border=True):
                col_lc1, col_lc2 = st.columns([3, 2])
                with col_lc1:
                    picked = st.selectbox(
                        f"🍽️ Lunch cover (12–2) — auto-suggests **{auto_pick}**",
                        options,
                        index=options.index(current) if current in options else 0,
                        key=f"lunch_pick_{selected_monday.isoformat()}_{di}",
                        help="Pick (auto) to use the auto-suggestion, (no one) to skip, or choose a name to override.",
                    )
                with col_lc2:
                    st.write("")
                    if picked == "(auto)":
                        new_override = ''
                    elif picked == "(no one)":
                        new_override = '__none__'
                    else:
                        new_override = picked
                    saved_marker = override_raw
                    if new_override != saved_marker:
                        if st.button("💾 Save lunch cover", key=f"lunch_save_{di}", type='primary'):
                            if new_override == '':
                                lunch_overrides.pop(di, None)
                            else:
                                lunch_overrides[di] = new_override
                            save_lunch_overrides(selected_monday, lunch_overrides)
                            st.rerun()
                    else:
                        st.caption("✓ Saved")

            auto_msg = build_slack_message(
                assignments, di,
                lunch_rota=lunch_rota,
                cover_suggestions=cover_suggestions,
                people=people,
                phone_cover=phone_cover,
                daily_notes=daily_notes,
                reward_status=reward_status,
            )

            # ── Editable message text area ──────────────────────────────────
            # Key persists edits across reruns. Auto-generated text is the
            # initial seed; the "🔄 Reset" button discards Jo's edits and
            # re-seeds with the freshly auto-generated text.
            msg_key = f"msg_text_{selected_monday.isoformat()}_{di}"
            auto_key = f"msg_auto_{selected_monday.isoformat()}_{di}"
            # Detect when the auto-generated text changes (e.g. lunch cover
            # override). If Jo hasn't manually edited, refresh the text.
            prev_auto = st.session_state.get(auto_key)
            user_edited = st.session_state.get(msg_key) is not None and \
                          st.session_state.get(msg_key) != prev_auto
            if not user_edited:
                st.session_state[msg_key] = auto_msg
            st.session_state[auto_key] = auto_msg

            st.text_area(
                f"Message for {DAY_NAMES[di]} {day_date.strftime('%d/%m')} — edit as needed",
                key=msg_key,
                height=500,
            )
            msg = st.session_state[msg_key]   # what we'll schedule/send
            if user_edited:
                st.caption("✏️ You've edited this — click 🔄 Reset to use the auto-generated text instead.")

            day_label = f"{DAY_NAMES[di]} {day_date.strftime('%d/%m')}"
            cover_items = build_cover_notifications(di, day_label, phone_cover, daily_notes)
            dms_live = st.session_state.get('cover_dm_live', False)

            # ── Schedule for 07:30 in #cs-zone ─────────────────────────────
            with st.container(border=True):
                st.markdown(f"**📅 Schedule for 07:30 — `#{SLACK_CHANNEL_CS_MORNING}`**")
                from datetime import datetime as _dt
                from time import time as _now
                target_ts = _seven_thirty_uk_unix(day_date)
                is_in_past = target_ts < _now()
                existing = scheduled.get(di)

                if existing:
                    st.success(
                        f"✅ Scheduled — fires at **{existing.get('post_at_iso')}** "
                        f"in `<#{existing.get('channel')}>`"
                    )
                    c_resched, c_cancel = st.columns(2)
                    with c_resched:
                        if st.button("🔄 Reschedule with current text",
                                      key=f"resched_{di}",
                                      disabled=is_in_past):
                            try:
                                delete_scheduled_message(existing['channel'],
                                                          existing['scheduled_id'])
                                sched_id = schedule_slack_message(
                                    SLACK_CHANNEL_CS_MORNING, msg, target_ts
                                )
                                scheduled[di] = {
                                    'scheduled_id': sched_id,
                                    'channel': SLACK_CHANNEL_CS_MORNING,
                                    'post_at_ts': target_ts,
                                    'post_at_iso': _dt.fromtimestamp(target_ts).strftime('%a %d %b %H:%M'),
                                    'updated_at': _dt.now().isoformat(timespec='seconds'),
                                }
                                save_scheduled_messages(selected_monday, scheduled)
                                st.rerun()
                            except Exception as e:
                                st.error(f"Reschedule failed: {e}")
                    with c_cancel:
                        if st.button("🗑️ Cancel schedule", key=f"cancel_sched_{di}"):
                            try:
                                delete_scheduled_message(existing['channel'],
                                                          existing['scheduled_id'])
                                scheduled.pop(di, None)
                                save_scheduled_messages(selected_monday, scheduled)
                                st.success("Schedule cancelled.")
                                st.rerun()
                            except Exception as e:
                                st.error(f"Cancel failed: {e}")
                else:
                    if is_in_past:
                        st.caption("⚠️ 07:30 for this day has already passed — can't schedule. "
                                    "Use Send now instead.")
                    elif st.button(f"📅 Schedule for {DAY_NAMES[di]} 07:30",
                                   key=f"sched_{di}", type='primary'):
                        try:
                            sched_id = schedule_slack_message(
                                SLACK_CHANNEL_CS_MORNING, msg, target_ts
                            )
                            scheduled[di] = {
                                'scheduled_id': sched_id,
                                'channel': SLACK_CHANNEL_CS_MORNING,
                                'post_at_ts': target_ts,
                                'post_at_iso': _dt.fromtimestamp(target_ts).strftime('%a %d %b %H:%M'),
                                'updated_at': _dt.now().isoformat(timespec='seconds'),
                            }
                            save_scheduled_messages(selected_monday, scheduled)
                            st.success(
                                f"Scheduled for {scheduled[di]['post_at_iso']} "
                                f"in <#{SLACK_CHANNEL_CS_MORNING}>"
                            )
                            st.rerun()
                        except Exception as e:
                            st.error(f"Schedule failed: {e}")

            # ── Other actions ──
            action_cols = st.columns(3)
            with action_cols[0]:
                if st.button(f"📤 Send now (dry-run)", key=f"send_{di}"):
                    try:
                        # De-fang mentions so the test send doesn't ping the team
                        send_slack_message(SLACK_CHANNEL_MORNING_MSG,
                                            _sanitize_for_dry_run(msg))
                        st.success("Sent to #dry-run-testing-jo (mentions stripped)")
                    except Exception as e:
                        st.error(f"Failed to send: {e}")
            with action_cols[1]:
                if st.button("🔄 Reset to auto-generated", key=f"reset_msg_{di}",
                              disabled=not user_edited):
                    # Drop both keys so the next rerun seeds fresh from auto_msg.
                    st.session_state.pop(msg_key, None)
                    st.session_state.pop(auto_key, None)
                    st.rerun()
            with action_cols[2]:
                if cover_items:
                    btn_label = (
                        f"📣 Notify cover ({len(cover_items)}) "
                        f"— {'LIVE' if dms_live else 'dry-run'}"
                    )
                    if st.button(btn_label, key=f"notify_cover_{di}",
                                 type='primary' if dms_live else 'secondary'):
                        with st.spinner('Posting cover notifications…'):
                            results = send_cover_notifications(
                                cover_items, day_label, dry_run=not dms_live
                            )
                        for name, channel_label, ok, err in results:
                            if ok:
                                tag = (f'✅ posted to {channel_label}' if dms_live
                                        else f'✅ dry-run preview for {channel_label}')
                                st.success(f"{tag} — {name}")
                            else:
                                st.error(f"❌ {name} ({channel_label}): {err}")
                else:
                    st.button("📣 No cover today", key=f"notify_cover_{di}", disabled=True)


elif page == "🍽️ Lunch Rota":
    st.header(f"Lunch Rota — w/c {selected_monday.strftime('%d %b %Y')}")

    if phone_agents is None:
        st.info("No rota found for this week. Go to **Pull Rota** to load it.")
        st.stop()

    lunch_rota = generate_lunch_rota(phone_agents, people, selected_monday)
    df = build_lunch_df(lunch_rota, selected_monday)
    st.dataframe(df, use_container_width=True, height=220)

    st.caption("Kate: two short breaks (no lunch slot). Becky: fixed at 13:30-14:00. Others rotate weekly. Minimum 4 phone agents active at all times.")


elif page == "🔍 Role Lookup":
    st.header("Role Lookup")
    st.caption("Look up any person's assigned role on any date, using historic data from the original rota.")

    with st.spinner("Loading historic rota..."):
        try:
            records, all_names, all_dates = _load_historic_rota()
        except Exception as e:
            st.error(f"Could not load original rota: {e}")
            st.stop()

    min_date = min(all_dates) if all_dates else date(2026, 1, 5)
    max_date = max(all_dates) if all_dates else date.today()

    col1, col2 = st.columns(2)
    with col1:
        lookup_name = st.selectbox("Person", all_names)
    with col2:
        lookup_date = st.date_input("Date", value=date.today(), min_value=min_date, max_value=max_date)

    if lookup_name and lookup_date:
        role = records.get((lookup_name, lookup_date))
        if role:
            css = role_style(role)
            st.markdown(
                f'<div style="padding: 16px; border-radius: 8px; font-size: 1.3em; margin: 16px 0; {css}">'
                f'<strong>{lookup_name}</strong> on {lookup_date.strftime("%A %d %b %Y")}: <strong>{role}</strong></div>',
                unsafe_allow_html=True,
            )
        else:
            st.info(f"No rota entry for {lookup_name} on {lookup_date.strftime('%A %d %b %Y')}.")

    st.divider()
    st.subheader("Week view")
    if lookup_name:
        lookup_monday = get_monday(lookup_date)
        week_dates = [lookup_monday + timedelta(days=i) for i in range(5)]
        week_data = []
        for d in week_dates:
            role = records.get((lookup_name, d), "—")
            week_data.append({"Day": d.strftime("%A"), "Date": d.strftime("%d %b"), "Role": role})
        week_df = pd.DataFrame(week_data)
        st.dataframe(
            week_df.style.map(role_style, subset=["Role"]),
            use_container_width=True,
            hide_index=True,
        )

    st.divider()
    st.subheader("Date range search")
    st.caption("See all roles for a person across a date range.")
    range_col1, range_col2 = st.columns(2)
    with range_col1:
        range_start = st.date_input("From", value=get_monday(lookup_date), min_value=min_date, max_value=max_date, key="range_start")
    with range_col2:
        range_end = st.date_input("To", value=get_monday(lookup_date) + timedelta(days=4), min_value=min_date, max_value=max_date, key="range_end")

    if lookup_name and range_start and range_end and range_start <= range_end:
        range_data = []
        d = range_start
        while d <= range_end:
            if d.weekday() < 5:
                role = records.get((lookup_name, d), "—")
                range_data.append({"Day": d.strftime("%A"), "Date": d.strftime("%d %b %Y"), "Role": role})
            d += timedelta(days=1)
        if range_data:
            range_df = pd.DataFrame(range_data)
            st.dataframe(
                range_df.style.map(role_style, subset=["Role"]),
                use_container_width=True,
                hide_index=True,
            )

            role_counts = {}
            for row in range_data:
                r = row["Role"]
                if r != "—":
                    role_counts[r] = role_counts.get(r, 0) + 1
            if role_counts:
                st.caption("**Role breakdown:** " + " · ".join(f"{r}: {c}d" for r, c in sorted(role_counts.items(), key=lambda x: -x[1])))


elif page == "🏆 Reward Time":
    st.header("Reward Time")

    # Reward weeks run Fri-Thu — calculate which reward week we're in
    reward_friday = get_reward_friday()
    reward_dates = get_weekday_dates(reward_friday)
    reward_end = reward_friday + timedelta(days=6)  # Thursday

    st.caption(f"Reward week: **{reward_friday.strftime('%d %b')} (Fri)** → **{reward_end.strftime('%d %b')} (Thu)**")

    # Week picker
    col_pick1, col_pick2, col_pick3, col_pick4 = st.columns([2, 1, 1, 1])
    with col_pick1:
        pick_date = st.date_input(
            "View reward week containing",
            value=reward_friday,
            key="reward_week_pick",
        )
        reward_friday = get_reward_friday(pick_date)
        reward_dates = get_weekday_dates(reward_friday)
        reward_end = reward_friday + timedelta(days=6)
    with col_pick2:
        st.write("")  # vertical alignment with the date input
        pull_clicked = st.button(
            "🔄 Pull actuals",
            use_container_width=True,
            help="Pulls Looker actuals for every elapsed working day in this reward week.",
        )
    with col_pick3:
        st.write("")
        sync_clicked = st.button(
            "🔁 Sync rota",
            use_container_width=True,
            help="Re-reads the rota and updates roles / absences (e.g. unplanned absence added after the week opened).",
        )
    with col_pick4:
        st.write("")
        autofill_clicked = st.button(
            "🍯 Autofill from notes",
            use_container_width=True,
            help="Reads Daily Notes and splits days with a 'Reward time' or 'appointment' note into role + segment.",
        )

    # Load or initialise week data
    # Reward week Fri-Thu spans two rota weeks:
    #   Friday = previous rota week (selected_monday - 7 days)
    #   Mon-Thu = current rota week (selected_monday)
    rw_key = f"reward_{reward_friday.isoformat()}"
    # Always reload from file to pick up data pulls and external changes
    existing = load_week(reward_friday)
    if existing:
        st.session_state[rw_key] = existing
    elif rw_key not in st.session_state:
        if assignments:
            # Try to load Friday's rota week from original rota
            fri_monday = reward_friday - timedelta(days=(reward_friday.weekday()))  # Monday of Friday's week
            fri_assignments = None
            if fri_monday != selected_monday:
                try:
                    gc = _cached_gspread()
                    fri_assignments, _ = read_original_rota(gc, fri_monday)
                    if not any(any(days.values()) for days in fri_assignments.values()):
                        fri_assignments = None
                except Exception:
                    pass
            st.session_state[rw_key] = build_week(
                reward_friday,
                assignments_fri=fri_assignments or assignments,
                assignments_mon_thu=assignments,
            )
        else:
            st.session_state[rw_key] = {}
    week_data = st.session_state[rw_key]

    if not week_data:
        st.info("No reward data for this week. Pull the rota first, then come back here.")
        st.stop()

    if pull_clicked:
        today = date.today()
        targets = [d for d in reward_dates if d <= today]
        if not targets:
            st.warning("No elapsed working days in this reward week yet.")
        else:
            with st.spinner(f"Pulling actuals for {len(targets)} day(s)..."):
                try:
                    for d in targets:
                        actuals = pull_day_data(d)
                        update_day_actuals(week_data, d, actuals)
                    # Skips are weekly — pull once per click and refresh every
                    # person's pw.skips against the DB.
                    skips = rt.pull_skips(reward_friday)
                    for name, pw in week_data.items():
                        pw.skips = skips.get(name, 0)
                    save_week(reward_friday, week_data)
                    st.session_state[rw_key] = week_data
                    st.success(
                        f"Pulled {len(targets)} day(s) + skips: "
                        f"{', '.join(d.strftime('%a %d/%m') for d in targets)}"
                    )
                    st.rerun()
                except Exception as e:
                    st.error(f"Pull failed: {e}")

    if sync_clicked:
        with st.spinner("Reading current rota and syncing roles…"):
            try:
                gc = _cached_gspread()
                fri_monday = reward_friday - timedelta(days=reward_friday.weekday())
                mon_monday = fri_monday + timedelta(days=7)
                fri_assignments, _ = read_original_rota(gc, fri_monday)
                mon_assignments, _ = read_original_rota(gc, mon_monday)
                changes = rt.sync_rota_into_week(
                    week_data, reward_friday,
                    assignments_fri=fri_assignments,
                    assignments_mon_thu=mon_assignments,
                )
                save_week(reward_friday, week_data)
                st.session_state[rw_key] = week_data
                if changes:
                    summary = ', '.join(
                        f"{n} {d.strftime('%a')} → {new}" for n, d, _, new in changes[:6]
                    )
                    extra = f' (+{len(changes) - 6} more)' if len(changes) > 6 else ''
                    st.success(f"Synced {len(changes)} role change(s): {summary}{extra}")
                else:
                    st.success("Rota matches saved state — nothing to update.")
                st.rerun()
            except Exception as e:
                st.error(f"Sync failed: {e}")

    if autofill_clicked:
        with st.spinner("Reading Daily Notes and autofilling splits…"):
            try:
                gc = _cached_gspread()
                # Reward week spans two rota weeks. Load Daily Notes from both
                # and project each entry onto the actual date.
                fri_monday = reward_friday - timedelta(days=reward_friday.weekday())
                mon_monday = fri_monday + timedelta(days=7)
                notes_by_date = {}
                for monday in (fri_monday, mon_monday):
                    week_notes = read_daily_notes(gc, monday)
                    for di, entries in week_notes.items():
                        target_date = monday + timedelta(days=di)
                        if target_date in reward_dates:
                            notes_by_date.setdefault(target_date, []).extend(entries)

                # Order matters: reward time first (typically larger blocks),
                # then appointments. If a day has both, the first one wins and
                # the second is skipped as 'already has segment'.
                reward_results = rt.autofill_reward_splits_from_notes(week_data, notes_by_date)
                appt_results   = rt.autofill_appointment_splits_from_notes(week_data, notes_by_date)

                total_applied = (
                    [('reward time', r) for r in reward_results if r['status'] == 'applied']
                    + [('appointment',  r) for r in appt_results if r['status'] == 'applied']
                )
                total_skipped = (
                    [('reward time', r) for r in reward_results if r['status'] == 'skipped']
                    + [('appointment',  r) for r in appt_results if r['status'] == 'skipped']
                )

                if total_applied:
                    save_week(reward_friday, week_data)
                    st.session_state[rw_key] = week_data
                    st.success(f"Autofilled {len(total_applied)} split(s):")
                    for kind, r in total_applied:
                        st.caption(
                            f"  • {r['name']} {r['date'].strftime('%a %d/%m')} ({kind}): {r['reason']}"
                        )
                else:
                    st.info("Nothing to autofill — no new reward-time or appointment notes.")

                if total_skipped:
                    with st.expander(f"⏭️ {len(total_skipped)} skipped"):
                        for kind, r in total_skipped:
                            hrs = f"{r['hours']:.2f}h" if r['hours'] is not None else "—"
                            st.caption(
                                f"  • {r['name']} {r['date'].strftime('%a %d/%m')} "
                                f"({kind}, {hrs}): {r['reason']}"
                            )

                if total_applied:
                    st.rerun()
            except Exception as e:
                st.error(f"Autofill failed: {e}")

    # ── Throughput grid ──
    st.subheader("Throughput vs Targets")
    day_labels = [d.strftime("%a %d/%m") for d in reward_dates]

    grid_rows = []
    for name in RT_AGENTS:
        pw = week_data.get(name)
        if not pw:
            continue
        row = {"Name": name, "Role": ""}
        roles_seen = set()
        all_base = True
        all_stretch = True
        for i, d in enumerate(reward_dates):
            dr = pw.days.get(d)
            if not dr or not dr.is_working:
                row[day_labels[i]] = dr.role if dr else "—"
            elif dr.segments:
                # Split-role day: show each segment
                parts = []
                for seg in dr.segments:
                    roles_seen.add(seg.role.split(' + ')[0] if ' + ' in seg.role else seg.role)
                    if seg.target_base > 0:
                        parts.append(f"{seg.actual}/{seg.target_base}")
                    else:
                        parts.append(seg.role)
                row[day_labels[i]] = " | ".join(parts)
                if not dr.met_base:
                    all_base = False
                    all_stretch = False
                elif not dr.met_stretch:
                    all_stretch = False
            else:
                roles_seen.add(dr.role.split(' + ')[0] if ' + ' in dr.role else dr.role)
                if dr.target_base > 0:
                    ratio_str = ""
                    if dr.role.startswith('Triage'):
                        ratio_str = f" ({dr.archive_ratio:.0%})"
                    # Show pro-rata indicator if not a full 8h day
                    prorata_str = f" [{dr.shift_hours}h]" if dr.shift_hours < STANDARD_SHIFT_HOURS else ""
                    row[day_labels[i]] = f"{dr.actual}/{dr.target_base}{ratio_str}{prorata_str}"
                    if not dr.met_base:
                        all_base = False
                        all_stretch = False
                    elif not dr.met_stretch:
                        all_stretch = False
                else:
                    row[day_labels[i]] = dr.role
                    all_base = False
                    all_stretch = False
        row["Role"] = ", ".join(sorted(roles_seen)) if roles_seen else "—"
        row["Skips"] = pw.skips
        eligible, level, hours, reason = calculate_eligibility(pw)
        if eligible:
            row["Status"] = f"{'⭐ Stretch' if level == 'stretch' else '✅ Base'} ({format_reward_hours(hours)})"
        else:
            row["Status"] = f"❌ {reason}"
        grid_rows.append(row)

    grid_df = pd.DataFrame(grid_rows)

    def _check_one_fraction(text):
        """Parse 'actual/target' (possibly with trailing ratio/tag) and return True if met."""
        if "/" not in text:
            return None
        parts = text.split("/")
        try:
            actual = int(parts[0].strip())
            # Strip trailing annotations like (85%), [4.5h], etc.
            target_str = parts[1].split("(")[0].split("[")[0].strip()
            target = int(target_str)
            return actual >= target
        except (ValueError, IndexError):
            return None

    def _throughput_style(val):
        if not isinstance(val, str):
            return ""
        if val.startswith("⭐"):
            return "background-color: #b8e6b8; color: #1a1a1a"
        if val.startswith("✅"):
            return "background-color: #d8ffd8; color: #1a1a1a"
        if val.startswith("❌"):
            return "background-color: #ffcccc; color: #1a1a1a"
        # Split-role cells: "40/36 | 55/65"
        if "|" in val:
            all_met = True
            for chunk in val.split("|"):
                result = _check_one_fraction(chunk.strip())
                if result is None:
                    continue
                if not result:
                    all_met = False
            if all_met:
                return "background-color: #d8ffd8; color: #1a1a1a"
            return "background-color: #ffcccc; color: #1a1a1a"
        # Single fraction: "92/72", "119/98 (85%)", "73/73 [4.5h]"
        result = _check_one_fraction(val)
        if result is not None:
            if result:
                return "background-color: #d8ffd8; color: #1a1a1a"
            return "background-color: #ffcccc; color: #1a1a1a"
        if val in ("Annual leave", "Non working day", "Unplanned absence", "Training"):
            return "background-color: #e5e5e5; color: #1a1a1a"
        return ""

    styled = grid_df.style.map(_throughput_style, subset=[c for c in grid_df.columns if c not in ("Name", "Role", "Skips")])
    st.dataframe(styled, use_container_width=True, hide_index=True, height=700)

    # ── Per-person quality / timeline / overrides ──
    st.divider()
    st.subheader("Quality, Timelines & Overrides")

    for name in RT_AGENTS:
        pw = week_data.get(name)
        if not pw or pw.days_worked == 0:
            continue

        eligible, level, hours, reason = calculate_eligibility(pw)
        status_icon = "⭐" if level == "stretch" else "✅" if eligible else "❌"

        with st.expander(f"{status_icon} {name} — {reason}"):
            col_q, col_t, col_o = st.columns(3)
            with col_q:
                new_quality = st.checkbox(
                    "Quality OK",
                    value=pw.quality_ok,
                    key=f"quality_{rw_key}_{name}",
                )
                if new_quality != pw.quality_ok:
                    if pw.quality_ok != new_quality:
                        add_override(pw, 'quality_ok', pw.quality_ok, new_quality, 'Toggled in app')
                        try:
                            write_audit_entry(reward_friday, name, 'quality_ok', pw.quality_ok, new_quality, 'Toggled in app')
                        except Exception:
                            pass  # Don't block UI if sheet write fails
                    pw.quality_ok = new_quality
                    save_week(reward_friday, week_data)
                    st.rerun()

            with col_t:
                new_timeline = st.checkbox(
                    "Timelines OK",
                    value=pw.timeline_ok,
                    key=f"timeline_{rw_key}_{name}",
                )
                if new_timeline != pw.timeline_ok:
                    if pw.timeline_ok != new_timeline:
                        add_override(pw, 'timeline_ok', pw.timeline_ok, new_timeline, 'Toggled in app')
                        try:
                            write_audit_entry(reward_friday, name, 'timeline_ok', pw.timeline_ok, new_timeline, 'Toggled in app')
                        except Exception:
                            pass
                    pw.timeline_ok = new_timeline
                    save_week(reward_friday, week_data)
                    st.rerun()

            with col_o:
                override_options = ["No override", "Grant base", "Grant stretch", "Deny"]
                label_for = {'': 'No override', 'base': 'Grant base', 'stretch': 'Grant stretch', 'deny': 'Deny'}
                value_for = {v: k for k, v in label_for.items()}
                override_val = st.selectbox(
                    "Override eligibility",
                    override_options,
                    index=override_options.index(label_for.get(pw.override_eligible, 'No override')),
                    key=f"override_{rw_key}_{name}",
                )
                new_override = value_for[override_val]
                override_reason = st.text_input(
                    "Reason",
                    key=f"override_reason_{rw_key}_{name}",
                    placeholder="Required if changing the override",
                )
                if st.button("Apply override", key=f"apply_override_{rw_key}_{name}"):
                    if new_override == pw.override_eligible:
                        st.info("No change — pick a different option to apply an override.")
                    else:
                        reason = override_reason.strip() or 'No reason given'
                        add_override(pw, 'override_eligible', pw.override_eligible, new_override, reason)
                        try:
                            write_audit_entry(reward_friday, name, 'override_eligible', pw.override_eligible, new_override, reason)
                        except Exception:
                            pass
                        pw.override_eligible = new_override
                        save_week(reward_friday, week_data)
                        st.rerun()

            # ── Day adjustments (partial days & split roles) ──
            st.caption("**Day adjustments:**")
            for d_idx, d in enumerate(reward_dates):
                dr = pw.days.get(d)
                if not dr or not dr.is_working:
                    continue
                day_label = d.strftime('%a %d/%m')
                adj_cols = st.columns([2, 2, 2, 1])

                with adj_cols[0]:
                    st.caption(f"**{day_label}** — {dr.role}")

                with adj_cols[1]:
                    new_hours = st.number_input(
                        "Hours", min_value=1.0, max_value=10.0,
                        value=float(dr.shift_hours), step=0.5,
                        key=f"shift_{rw_key}_{name}_{d_idx}",
                        label_visibility="collapsed",
                    )
                    if abs(new_hours - dr.shift_hours) > 0.01:
                        old_h = adjust_shift_hours(pw, d, new_hours)
                        try:
                            write_audit_entry(reward_friday, name, f'shift_hours ({day_label})', old_h, new_hours, 'Adjusted in app')
                        except Exception:
                            pass
                        add_override(pw, f'shift_hours ({day_label})', old_h, new_hours, 'Adjusted in app')
                        save_week(reward_friday, week_data)

                with adj_cols[2]:
                    if not dr.segments:
                        if st.button("✂️ Split", key=f"split_btn_{rw_key}_{name}_{d_idx}"):
                            st.session_state[f"splitting_{rw_key}_{name}_{d_idx}"] = True

                        if st.session_state.get(f"splitting_{rw_key}_{name}_{d_idx}"):
                            available_roles = [r for r in SPLITTABLE_ROLES if r != dr.role]
                            split_role_b = st.selectbox(
                                "Second role", available_roles,
                                key=f"split_role_b_{rw_key}_{name}_{d_idx}",
                                label_visibility="collapsed",
                            )
                            third_options = ['(none)'] + [r for r in SPLITTABLE_ROLES if r not in (dr.role, split_role_b)]
                            split_role_c = st.selectbox(
                                "Third role (optional)", third_options,
                                key=f"split_role_c_{rw_key}_{name}_{d_idx}",
                                label_visibility="collapsed",
                            )
                            three_way = split_role_c != '(none)'

                            if three_way:
                                third = dr.shift_hours / 3
                                hrs_a = st.number_input(
                                    f"Hours on {dr.role}",
                                    min_value=0.5, max_value=dr.shift_hours - 1.0,
                                    value=round(third, 1), step=0.5,
                                    key=f"split_hrs_a_{rw_key}_{name}_{d_idx}",
                                )
                                hrs_b = st.number_input(
                                    f"Hours on {split_role_b}",
                                    min_value=0.5, max_value=max(0.5, dr.shift_hours - hrs_a - 0.5),
                                    value=round(min(third, dr.shift_hours - hrs_a - 0.5), 1), step=0.5,
                                    key=f"split_hrs_b_{rw_key}_{name}_{d_idx}",
                                )
                                hrs_c = round(dr.shift_hours - hrs_a - hrs_b, 1)
                                st.caption(f"→ {hrs_c}h on {split_role_c}")
                            else:
                                half = dr.shift_hours / 2
                                hrs_a = st.number_input(
                                    f"Hours on {dr.role}",
                                    min_value=0.5, max_value=dr.shift_hours - 0.5,
                                    value=half, step=0.5,
                                    key=f"split_hrs_a_{rw_key}_{name}_{d_idx}",
                                )
                                hrs_b = round(dr.shift_hours - hrs_a, 1)
                                hrs_c = 0
                                st.caption(f"→ {hrs_b}h on {split_role_b}")

                            if st.button("Apply split", key=f"split_apply_{rw_key}_{name}_{d_idx}"):
                                orig_role = dr.role
                                if three_way:
                                    spec = [(orig_role, hrs_a), (split_role_b, hrs_b), (split_role_c, hrs_c)]
                                    reason = f"Split {hrs_a}h {orig_role} / {hrs_b}h {split_role_b} / {hrs_c}h {split_role_c}"
                                else:
                                    spec = [(orig_role, hrs_a), (split_role_b, hrs_b)]
                                    reason = f"Split {hrs_a}h {orig_role} / {hrs_b}h {split_role_b}"
                                split_day(pw, d, spec)
                                add_override(pw, f'split ({day_label})', orig_role, dr.role, reason)
                                try:
                                    write_audit_entry(reward_friday, name, f'split ({day_label})', orig_role, dr.role, reason)
                                except Exception:
                                    pass
                                save_week(reward_friday, week_data)
                                del st.session_state[f"splitting_{rw_key}_{name}_{d_idx}"]
                                st.rerun()
                    else:
                        # Already split — show segments and allow unsplit
                        for seg in dr.segments:
                            mins_h = seg.minutes / 60
                            st.caption(f"  {seg.role}: {mins_h:.1f}h — {seg.actual}/{seg.target_base}")
                        if st.button("↩️ Unsplit", key=f"unsplit_{rw_key}_{name}_{d_idx}"):
                            orig_role = dr.segments[0].role  # revert to first segment's role
                            unsplit_day(pw, d, orig_role)
                            add_override(pw, f'unsplit ({day_label})', dr.role, orig_role, 'Reverted split')
                            try:
                                write_audit_entry(reward_friday, name, f'unsplit ({day_label})', dr.role, orig_role, 'Reverted split')
                            except Exception:
                                pass
                            save_week(reward_friday, week_data)
                            st.rerun()

                with adj_cols[3]:
                    st.caption(f"{dr.shift_hours}h")

            # Audit trail
            if pw.overrides:
                st.caption("**Audit trail:**")
                for ov in pw.overrides:
                    st.caption(f"  {ov['timestamp'][:16]} — {ov['field']}: {ov['old']} → {ov['new']} — {ov['reason']}")

            # Reward day info
            rd = REWARD_DAYS.get(name)
            if rd:
                actual_hrs = sum(dr.shift_hours for dr in pw.days.values() if dr.is_working)
                st.caption(f"**Reward day:** {rd[0]} {rd[1]} | **Hours worked:** {actual_hrs:.1f}h / 40h | **Pro-rata:** {actual_hrs/40:.0%}")

    # ── Friday sign-off ──
    st.divider()
    st.subheader("Friday Sign-off")
    st.caption("Review quality and timelines above, then send the results to team leads.")

    qualified_count = sum(1 for name in RT_AGENTS if week_data.get(name) and calculate_eligibility(week_data[name])[0])
    total_count = sum(1 for name in RT_AGENTS if week_data.get(name) and week_data[name].days_worked > 0)
    st.metric("Qualified", f"{qualified_count} / {total_count}")

    # Write week summary to Google Sheet when signing off
    col_summary, col_send1, col_send2 = st.columns(3)
    with col_summary:
        if st.button("💾 Save week summary to sheet", use_container_width=True):
            try:
                write_week_summary(reward_friday, week_data)
                st.success("Week summary written to Google Sheet")
            except Exception as e:
                st.error(f"Failed to write summary: {e}")

    with col_send1:
        if st.button("📤 Send to #dry-run-testing-jo", type="primary", use_container_width=True):
            try:
                write_week_summary(reward_friday, week_data)
                msg = build_reward_slack_message(reward_friday, week_data)
                send_slack_message(SLACK_CHANNEL_MORNING_MSG, msg)
                st.success("Reward time summary sent to #dry-run-testing-jo")
            except Exception as e:
                st.error(f"Failed: {e}")

    # ── TL approval queue ──
    st.divider()
    st.markdown("### 📥 Approval Queue")
    st.caption("Each TL submits their team here. Approve, or ask a question — then post the decision to the reward time channel.")

    SLACK_REWARD_CHANNEL = 'C0AUP24HQPP'  # #dry-run-testing-jo (local/test); switch to C0B103KEJLU when live

    submissions_by_tl = {tl: [] for tl in TL_TEAMS}
    for tl, members in TL_TEAMS.items():
        for name in members:
            pw = week_data.get(name)
            if pw and pw.tl_submitted_at and pw.days_worked > 0:
                submissions_by_tl[tl].append(name)

    any_submissions = any(submissions_by_tl.values())
    if not any_submissions:
        st.info("No TL submissions yet for this reward week. Once a team lead hits **Submit team to Jo** in their app, their team will appear here.")
    else:
        # Top-level stats
        total_submitted = sum(len(v) for v in submissions_by_tl.values())
        total_approved = sum(1 for v in submissions_by_tl.values() for n in v if week_data[n].jo_decision == 'approved')
        total_pending = sum(1 for v in submissions_by_tl.values() for n in v if week_data[n].jo_decision == '')
        total_question = sum(1 for v in submissions_by_tl.values() for n in v if week_data[n].jo_decision == 'question')

        s1, s2, s3, s4 = st.columns(4)
        s1.metric('Submitted', total_submitted)
        s2.metric('Pending', total_pending)
        s3.metric('Approved', total_approved)
        s4.metric('Questions', total_question)

        # Tabs per TL — only show TLs who have something
        tls_with_subs = [tl for tl in submissions_by_tl if submissions_by_tl[tl]]
        tab_labels = []
        for tl in tls_with_subs:
            names = submissions_by_tl[tl]
            pending = sum(1 for n in names if week_data[n].jo_decision == '')
            badge = f" ({pending} pending)" if pending else ' ✓'
            tab_labels.append(f"{tl}{badge}")

        tl_tabs = st.tabs(tab_labels)
        for tab, tl in zip(tl_tabs, tls_with_subs):
            with tab:
                names = submissions_by_tl[tl]
                pending = sum(1 for n in names if week_data[n].jo_decision == '')
                approved = sum(1 for n in names if week_data[n].jo_decision == 'approved')
                questioned = sum(1 for n in names if week_data[n].jo_decision == 'question')
                st.caption(f"{len(names)} submitted · {pending} pending · {approved} approved · {questioned} ❓")

                for name in names:
                    pw = week_data[name]
                    eligible, level, hours, reason = calculate_eligibility(pw)
                    if pw.tl_request_level == 'deny':
                        ask = '❌ Deny'
                    elif pw.tl_request_level == 'base':
                        ask = '✅ Grant Base'
                    elif pw.tl_request_level == 'stretch':
                        ask = '⭐ Grant Stretch'
                    else:
                        ask = f"Auto → {'⭐ Stretch' if level == 'stretch' else '✅ Base' if eligible else '❌ None'}"

                    # Status pill
                    if pw.jo_decision == 'approved':
                        status_html = '<span class="aq-status-green">✅ Approved</span>'
                    elif pw.jo_decision == 'question':
                        status_html = '<span class="aq-status-amber">❓ Question raised</span>'
                    else:
                        status_html = '<span class="aq-status-blue">⏳ Pending</span>'

                    expander_label = f"{name}  —  TL asks: {ask}"
                    with st.expander(expander_label, expanded=(pw.jo_decision == '')):
                        st.markdown(status_html, unsafe_allow_html=True)
                        if pw.tl_notes:
                            st.markdown(f"**TL note:** _{pw.tl_notes}_")
                        st.caption(
                            f"Auto result: "
                            f"{'⭐ Stretch' if level == 'stretch' else '✅ Base' if eligible else '❌ None'}"
                            f"{' · ' + format_reward_hours(hours) if eligible else ''} — {reason}"
                        )
                        if pw.jo_decision == 'question':
                            st.warning(f"Your question: _{pw.jo_question_text}_")

                        c_app, c_q = st.columns([1, 2])
                        with c_app:
                            if st.button('✅ Approve', key=f"jo_approve_{rw_key}_{name}",
                                         type='primary', width='stretch'):
                                from datetime import datetime as _dt
                                pw.jo_decision = 'approved'
                                pw.jo_decision_at = _dt.now().isoformat(timespec='seconds')
                                pw.jo_question_text = ''
                                if pw.tl_request_level in ('base', 'stretch', 'deny'):
                                    add_override(pw, 'override_eligible', pw.override_eligible,
                                                 pw.tl_request_level,
                                                 f"Approved TL request: {pw.tl_notes or 'no reason'}")
                                    pw.override_eligible = pw.tl_request_level
                                save_week(reward_friday, week_data)
                                try:
                                    write_audit_entry(reward_friday, name, 'jo_decision', '', 'approved',
                                                      pw.tl_notes or 'TL request approved')
                                except Exception:
                                    pass
                                st.rerun()
                            if pw.jo_decision and st.button('↩️ Clear decision',
                                                              key=f"jo_clear_{rw_key}_{name}",
                                                              width='stretch'):
                                pw.jo_decision = ''
                                pw.jo_decision_at = ''
                                pw.jo_question_text = ''
                                save_week(reward_friday, week_data)
                                st.rerun()
                        with c_q:
                            q_text = st.text_input(
                                'Question for the TL', value=pw.jo_question_text,
                                key=f"jo_q_text_{rw_key}_{name}",
                                placeholder='e.g. why are you asking for stretch when Thu was below base?',
                            )
                            if st.button('❓ Ask question', key=f"jo_question_{rw_key}_{name}",
                                         width='stretch'):
                                if not q_text.strip():
                                    st.error('Type a question first.')
                                else:
                                    from datetime import datetime as _dt
                                    pw.jo_decision = 'question'
                                    pw.jo_decision_at = _dt.now().isoformat(timespec='seconds')
                                    pw.jo_question_text = q_text.strip()
                                    save_week(reward_friday, week_data)
                                    try:
                                        write_audit_entry(reward_friday, name, 'jo_decision',
                                                          '', 'question', q_text.strip())
                                    except Exception:
                                        pass
                                    st.rerun()

        # Build summary message for the reward time channel
        def _build_decision_message():
            lines = [f"🏆 *Reward Time Decisions — w/c {reward_friday.strftime('%d %b %Y')}*", '']
            had_anything = False
            for tl, names in submissions_by_tl.items():
                approved_list = [n for n in names if week_data[n].jo_decision == 'approved']
                question_list = [n for n in names if week_data[n].jo_decision == 'question']
                if not approved_list and not question_list:
                    continue
                had_anything = True
                lines.append(f"*{tl}'s team*")
                for n in approved_list:
                    pw = week_data[n]
                    eligible, level, hours, _ = calculate_eligibility(pw)
                    if eligible:
                        result = f"{'⭐ Stretch' if level == 'stretch' else '✅ Base'} — {format_reward_hours(hours)}"
                    else:
                        result = '❌ None'
                    lines.append(f"• ✅ *Jo has approved the reward time* for {n} — {result}")
                for n in question_list:
                    pw = week_data[n]
                    lines.append(f"• ❓ *Jo has a question about reward time* for {n} — {pw.jo_question_text}")
                lines.append('')
            return '\n'.join(lines) if had_anything else ''

        st.divider()
        decision_msg = _build_decision_message()
        col_post1, col_post2 = st.columns([2, 1])
        with col_post1:
            with st.expander('👀 Preview the channel post'):
                st.text_area('Preview', value=decision_msg or '(nothing decided yet)',
                             height=260, key=f"decision_preview_{rw_key}",
                             label_visibility='collapsed')
        with col_post2:
            st.write('')
            if st.button('📤 Post decisions to reward time channel',
                         type='primary', width='stretch',
                         disabled=not decision_msg):
                try:
                    send_slack_message(SLACK_REWARD_CHANNEL, decision_msg)
                    st.success('Posted to reward time channel (dry-run).')
                except Exception as e:
                    st.error(f'Failed to send: {e}')

    # TL_PEGASUS_CHANNELS now defined globally near build_cover_notifications.

    with col_send2:
        if st.button("📤 Send per-TL messages", use_container_width=True):
            try:
                write_week_summary(reward_friday, week_data)
                tl_messages = build_tl_messages(reward_friday, week_data)
                for tl, msg in tl_messages.items():
                    channel = TL_PEGASUS_CHANNELS.get(tl, SLACK_CHANNEL_MORNING_MSG)
                    send_slack_message(channel, msg)
                st.success(f"Sent {len(tl_messages)} TL messages to their pegasus channels")
            except Exception as e:
                st.error(f"Failed: {e}")

    # Preview message
    with st.expander("Preview Slack message"):
        msg = build_reward_slack_message(reward_friday, week_data)
        st.text_area("Message preview", value=msg, height=400, key="reward_msg_preview")


elif page == "⚙️ Pull Rota":
    st.header("Pull Rota")
    st.markdown(
        f"Pull the rota for **w/c {selected_monday.strftime('%d %b %Y')}** from the "
        "[original rota spreadsheet](https://docs.google.com/spreadsheets/d/1CMSEZSb-4D4mO6iPb8tVSaAPsZT5KZst9VSXH4bpi0Y)."
    )

    col1, col2 = st.columns(2)

    with col1:
        if st.button("📥 Pull from rota sheet", type="primary", use_container_width=True):
            with st.spinner("Reading from rota spreadsheet..."):
                try:
                    gc = _cached_gspread()
                    new_assignments, new_phone_agents = read_original_rota(gc, selected_monday)

                    if any(any(days.values()) for days in new_assignments.values()):
                        st.session_state[f"assignments_{week_key}"] = new_assignments
                        st.session_state[f"phone_agents_{week_key}"] = new_phone_agents
                        st.success(f"Rota loaded for w/c {selected_monday.strftime('%d %b %Y')}.")
                        st.rerun()
                    else:
                        st.warning("No data found for this week in the rota spreadsheet.")
                except Exception as e:
                    st.error(f"Failed to read rota: {e}")

    with col2:
        if st.button("🔄 Refresh (clear cache)", use_container_width=True):
            # Clear cached data for this week and force re-pull
            for key in list(st.session_state.keys()):
                if key in (f"assignments_{week_key}", f"phone_agents_{week_key}", "people"):
                    del st.session_state[key]
            st.rerun()

    if assignments:
        st.divider()
        # Show summary of what's loaded
        working_count = {}
        absent_count = {}
        for di in range(5):
            working = 0
            absent = 0
            for name, days in assignments.items():
                val = days.get(di, "")
                if not val or val == ROLE_NWD:
                    continue
                if val in (ROLE_AL, ROLE_ABSENCE, ROLE_TRAINING):
                    absent += 1
                else:
                    working += 1
            working_count[di] = working
            absent_count[di] = absent

        cols = st.columns(5)
        for di, col in enumerate(cols):
            day_date = selected_monday + timedelta(days=di)
            with col:
                st.metric(
                    f"{DAY_NAMES[di]} {day_date.strftime('%d/%m')}",
                    f"{working_count[di]} active",
                    delta=f"-{absent_count[di]} absent" if absent_count[di] else None,
                    delta_color="inverse",
                )
