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

# Password gate — blocks everything below until the user signs in
from auth import require_login
require_login()

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
    ROLE_PHONES, ROLE_TRIAGE, ROLE_TRIAGE_LC, ROLE_TRIAGE_VC, ROLE_CHASING, ROLE_ICS,
    ROLE_TL, ROLE_AL, ROLE_ABSENCE, ROLE_NWD, ROLE_TRAINING,
    base_role, build_people, generate_lunch_rota,
    suggest_cover, build_dashboard_data,
    get_gspread,
    read_original_rota, read_daily_notes, append_daily_notes_rows,
    read_working_hours, update_daily_notes_roles,
    DEFAULT_ROLE_TARGETS,
    DEFAULT_SKILLS, DEFAULT_SHIFTS, DEFAULT_HOURS,
)
import reward_time as rt
from reward_time import (
    get_reward_friday, get_weekday_dates, load_week, save_week,
    build_week, update_day_actuals, pull_day_data, calculate_eligibility, add_override,
    format_reward_hours,
    build_reward_slack_message, build_tl_messages, build_reward_message_by_team,
    build_daily_notes_draft,
    write_audit_entry, write_week_summary,
    REWARD_DAYS, ALL_AGENTS as RT_AGENTS, TL_TEAMS, ROLE_TARGETS as RT_TARGETS,
    WEEKLY_HOURS, SKIP_THRESHOLD_WEEKLY, STANDARD_SHIFT_HOURS, DAILY_HOURS,
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


@st.cache_data(ttl=600)
def _cached_working_hours():
    """Read & cache the Working Hours tab from the rota sheet for 10 min.
    Returns {name: (start_h, end_h)} fractional 24-hour values."""
    try:
        return read_working_hours(_cached_gspread())
    except Exception:
        return {}


CORE_PHONES = ['Becky', 'Elida', 'Fionn', 'Jade', 'Kate']
WIDER_TEAM = ['Clare', 'Cris', 'Erika', 'Harriet', 'Harry',
              'Kirsty', 'Lizzie', 'Lucy', 'Maisha', 'Noemi', 'Roseanne',
              'Sophie', 'Tara', 'Thea']


# ── Slack ──────────────────────────────────────────────────────────────────
# Token resolution goes through compat.get_slack_token(): st.secrets on cloud,
# ~/.config/juno/claude-code/slack-token locally.
from compat import get_slack_token
SLACK_TOKEN_PATH = Path.home() / '.config/juno/claude-code/slack-token'  # kept for backward refs
SLACK_CHANNEL_MORNING_MSG = 'C0AUP24HQPP'      # #dry-run-testing-jo
SLACK_CHANNEL_CS_MORNING = 'C02TP0FBM32'        # real CS channel — scheduled 07:30 post lands here
SLACK_CHANNEL_REWARD_QUESTIONS = 'C0AS755PW49' # #reward-time-questions-cs (live target for the team-grouped reward post)
JO_USER_ID = 'U07KFSSCUNT'                     # Jo Jeffries — Slack DM target for "Send test"

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


def _lunch_cover_filename(monday):
    return f"lunch_cover_{monday.isoformat()}.json"


def _lunch_cover_file(monday):
    return LUNCH_COVER_DIR / _lunch_cover_filename(monday)


def load_lunch_overrides(monday):
    """Return {day_idx: name} or {} if none saved. Local or Drive depending
    on whether we're running on Streamlit Cloud."""
    from drive_state import read_json_either
    raw = read_json_either(_lunch_cover_file(monday), _lunch_cover_filename(monday))
    if not raw:
        return {}
    try:
        return {int(k): v for k, v in raw.items()}
    except Exception:
        return {}


def save_lunch_overrides(monday, overrides):
    """Persist {day_idx: name} (local or Drive)."""
    from drive_state import write_json_either
    payload = {str(k): v for k, v in overrides.items() if v}
    write_json_either(_lunch_cover_file(monday), _lunch_cover_filename(monday), payload)


# ── Scheduled morning messages ──────────────────────────────────────────────
# State: {(monday, day_idx): {'scheduled_id', 'channel', 'post_at_ts',
#                              'post_at_iso', 'channel_label', 'updated_at'}}
SCHEDULED_DIR = Path.home() / '.claude/scheduled-tasks/morning-message'


def _scheduled_filename(monday):
    return f"scheduled_{monday.isoformat()}.json"


def _scheduled_file(monday):
    return SCHEDULED_DIR / _scheduled_filename(monday)


def load_scheduled_messages(monday):
    """Return {day_idx: scheduled_info_dict} or {} if none saved."""
    from drive_state import read_json_either
    raw = read_json_either(_scheduled_file(monday), _scheduled_filename(monday))
    if not raw:
        return {}
    try:
        return {int(k): v for k, v in raw.items()}
    except Exception:
        return {}


def save_scheduled_messages(monday, sched):
    """Persist scheduled-message state (local or Drive)."""
    from drive_state import write_json_either
    payload = {str(k): v for k, v in sched.items() if v}
    write_json_either(_scheduled_file(monday), _scheduled_filename(monday), payload)


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
    token = get_slack_token()
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
    token = get_slack_token()
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
    token = get_slack_token()
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
    'Becky': 'Becky Smith', 'Clare': 'Clare Brown',
    'Cris': 'Cris Macagi', 'Elida': 'Elida Gizli', 'Erika': 'Erika Frolova',
    'Fionn': 'Fionn Burrows', 'Harriet': 'Harriet Clifton-Sprigg',
    'Harry': 'Harry McNicholas', 'Roseanne': 'Roseanne Brooks-Brown',
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
    token = get_slack_token()
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
    token = get_slack_token()
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
    'Clare':    'C09728A769E',   # #pegasus-clare-j
    'Cris':     'C0925KLHDFD',   # #pegasus-cris-j
    'Elida':    'C0APEK7FFU0',   # #pegasus-elida-c
    'Erika':    'C09G2DF5EQP',   # #pegasus-erika-l
    'Fionn':    'C06L8394GAZ',   # #pegasus-fionn-c
    'Harriet':  'C09JG8XGVHA',   # #pegasus-harriet-l
    'Harry':    'C0B78KZUG1E',   # #pegasus-harry-c
    'Jade':     'C0AGJPAJWQ1',   # #pegasus-jade-c
    'Kate':     'C05FN07KF8B',   # #pegasus-kate-c
    'Kirsty':   'C0AMJTHL4SZ',   # #pegasus-kirsty-y
    'Lizzie':   'C072AQCBG15',   # #pegasus-lizzie-l
    'Lucy':     'C0AU24PSF39',   # #pegasus-lucy-j
    'Maisha':   'C09GN3P0JA0',   # #pegasus-maisha-j
    'Noemi':    'C09AJD6T7H7',   # #pegasus-noemi-y
    'Roseanne': 'C0B6G6NJ6DP',   # #pegasus-roseanne-y
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
    'Becky': '#pegasus-becky-c',
    'Clare': '#pegasus-clare-j',  'Cris': '#pegasus-cris-j',
    'Elida': '#pegasus-elida-c',  'Erika': '#pegasus-erika-l',
    'Fionn': '#pegasus-fionn-c',  'Harriet': '#pegasus-harriet-l',
    'Harry': '#pegasus-harry-c',
    'Jade': '#pegasus-jade-c',    'Kate': '#pegasus-kate-c',
    'Kirsty': '#pegasus-kirsty-y','Lizzie': '#pegasus-lizzie-l',
    'Lucy': '#pegasus-lucy-j',    'Maisha': '#pegasus-maisha-j',
    'Noemi': '#pegasus-noemi-y',  'Roseanne': '#pegasus-roseanne-y',
    'Sophie': '#pegasus-sophie-y',
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
REWARD_TIME_LABEL = "Reward time"
ROLE_CSS = {
    ROLE_PHONES:        "background-color: #cce4ff" + _DARK_TEXT,
    ROLE_TRIAGE:        "background-color: #d8ffd8" + _DARK_TEXT,
    ROLE_TRIAGE_LC:     "background-color: #f2f2b2" + _DARK_TEXT,
    ROLE_TRIAGE_VC:     "background-color: #d8f0e0" + _DARK_TEXT,
    ROLE_CHASING:       "background-color: #ffe5cc" + _DARK_TEXT,
    ROLE_ICS:           "background-color: #e5d8ff" + _DARK_TEXT,
    ROLE_TL:            "background-color: #d8d8d8" + _DARK_TEXT,
    ROLE_AL:            "background-color: #ffcccc" + _DARK_TEXT,
    ROLE_ABSENCE:       "background-color: #ffb2b2" + _DARK_TEXT,
    ROLE_NWD:           "background-color: #e5e5e5" + _DARK_TEXT,
    ROLE_TRAINING:      "background-color: #ccffff" + _DARK_TEXT,
    REWARD_TIME_LABEL:  "background-color: #fff2b3; font-weight: 600" + _DARK_TEXT,
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
    # Role headroom priority — higher = more headroom, easier to pull from.
    # Chasing is now target 1 / min 1, so there's no slack to pull from there;
    # left at the bottom of the ranking but it'll only get picked if nothing
    # else has headroom either.
    ROLE_HEADROOM = {
        ROLE_TRIAGE: 3,     # target 5, min 4 — most headroom
        ROLE_ICS: 2,        # target 2, min 1
        ROLE_CHASING: 1,    # target 1, min 1 — pulling someone breaks cover
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
        ("📹 Triage + Video Calls", ROLE_TRIAGE_VC, False),
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

    # Role + cover info intentionally omitted — those already appear in the
    # Cover section below, no need to duplicate.
    if reward_entries:
        lines.append("🏆 Reward time")
        for e in reward_entries:
            lines.append(f"{e['name']}: {_fmt_hours(e['hours'])}")
        lines.append("")

    if half_day_entries:
        lines.append("⏰ Half day")
        for e in half_day_entries:
            lines.append(f"{e['name']}: {_fmt_hours(e['hours'])}")
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

    page = st.radio(
        "Navigation",
        [
            "🗓️ Weekly Rota",
            "📡 Hourly view (who's where)",
            "📊 Coverage",
            "💬 Morning Message",
            "🍽️ Lunch Rota",
            "🔍 Role Lookup",
            "🏆 Reward Time",
            "⚙️ Pull Rota",
        ],
        captions=[
            "Read-only view of the planned week",
            "Read-only view with today's intraday moves overlaid",
            "Read-only view of cover gaps and suggestions",
            "Sends the 07:30 post and writes reward time into next week's rota",
            "Read-only view of the lunch cover plan",
            "Read-only lookup of historic roles",
            "Decides reward time and writes bookings into the rota",
            "Read-only loader, refreshes a week from the rota sheet",
        ],
        label_visibility="collapsed",
    )

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
    st.caption("**Linked sheets**")
    st.caption(
        "📋 [Rota Sheet](https://docs.google.com/spreadsheets/d/1CMSEZSb-4D4mO6iPb8tVSaAPsZT5KZst9VSXH4bpi0Y)"
    )
    # Reward Time audit sheet — sheet ID loaded from saved state so the
    # link auto-updates if it ever gets recreated.
    try:
        rt.load_reward_state()
        if rt.REWARD_SHEET_ID:
            st.caption(
                f"📒 [Reward Time Audit](https://docs.google.com/spreadsheets/d/{rt.REWARD_SHEET_ID})"
            )
    except Exception:
        pass

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


# ── Hourly-view helpers ────────────────────────────────────────────────────
# Used by the "Hourly view (who's where)" page to overlay reward-time blocks
# from Daily Notes onto the per-hour grid.

def _parse_hour_string(s: str):
    """'13:30' → 13.5, '2pm' → 14.0, '9' → 9.0. None if not parseable."""
    import re as _re
    if not s:
        return None
    raw = s.strip().lower()
    pm = raw.endswith("pm")
    am = raw.endswith("am")
    if pm or am:
        raw = raw[:-2].strip()
    m = _re.match(r"^(\d{1,2})(?::(\d{2}))?$", raw)
    if not m:
        return None
    h = int(m.group(1))
    mn = int(m.group(2)) if m.group(2) else 0
    if pm and h < 12:
        h += 12
    elif am and h == 12:
        h = 0
    return h + mn / 60.0


def _parse_time_range_start_end(s: str):
    """'13:00 - 17:00' → (13.0, 17.0). None if not parseable.

    Mirrors the meridian inference in reward_time.parse_time_range_hours.
    """
    import re as _re
    if not s:
        return None
    parts = _re.split(r"\s*[-–]\s*", s.strip(), maxsplit=1)
    if len(parts) != 2:
        return None
    raw_a, raw_b = parts[0].strip().lower(), parts[1].strip().lower()
    a = _parse_hour_string(raw_a)
    b = _parse_hour_string(raw_b)
    if a is None or b is None:
        return None
    a_mer = raw_a.endswith("am") or raw_a.endswith("pm")
    b_mer = raw_b.endswith("am") or raw_b.endswith("pm")
    if not a_mer and not b_mer:
        if a < 8 and b < 8:
            a += 12
            b += 12
        elif b < a:
            b += 12
    elif b_mer and not a_mer:
        if a < 12 and (a + 12) < b:
            a += 12
    if b <= a:
        return None
    return a, b


def _hhmm_to_float(s: str):
    """'10:30' → 10.5. None on bad input."""
    if not s:
        return None
    try:
        h, m = s.split(":")
        return int(h) + int(m) / 60
    except (ValueError, AttributeError):
        return None


WORKING_HOURS_SHEET_ID = "1MnzXWPOLHld8vliMVUDKfmTGOO9tZuwyQj4fM1IdyQY"


def _parse_sheet_time(s):
    """Parse a 'Usual Working Hours' cell into (start_h, end_h).

    Returns None for entries we can't make sense of (split shifts,
    hours-only, free text, blanks) — callers fall back to DEFAULT_SHIFTS.
    """
    if not s:
        return None
    import re as _re
    raw = s.strip().lower()
    if not raw:
        return None
    if "," in raw or " then " in raw:
        return None
    # First option for "9 - 5.30 or 8.30 - 5".
    raw = raw.split(" or ")[0].strip()
    # Strip parenthetical notes like "(off Mon)".
    raw = _re.sub(r"\(.*?\)", "", raw).strip()
    # UK dot-time → colon: '5.30' → '5:30'.
    raw = _re.sub(r"(\d+)\.(\d{2})\b", r"\1:\2", raw)
    # If non-time letters remain (e.g. 'mon-thurs'), bail.
    stripped = _re.sub(r"\b(am|pm)\b", "", raw)
    if _re.search(r"[a-z]", stripped):
        return None
    return _parse_time_range_start_end(raw)


@st.cache_data(ttl=600)
def load_working_hours_from_sheet() -> dict:
    """Read the 'Usual Working Hours' sheet → {name: {day_idx: (start, end)}}.

    Free-form rows (Tara's '4.5 Mon-Thurs', Sophie's split shift,
    Becky's 'A or B') drop out — DEFAULT_SHIFTS handles those.
    Cached for 10 minutes so we don't hit the sheet on every rerun.
    """
    import re as _re
    try:
        gc = _cached_gspread()
        ss = gc.open_by_key(WORKING_HOURS_SHEET_ID)
        ws = ss.worksheets()[0]
        rows = ws.get_all_values()
    except Exception:
        return {}

    out: dict[str, dict[int, tuple[float, float]]] = {}
    for row in rows:
        if not row or len(row) < 6:
            continue
        name = row[0].strip()
        if not name:
            continue
        # Strip annotations like '(Left CS 27 Apr)'.
        name = _re.sub(r"\s*\(.*?\)", "", name).strip()
        if name in ("", "Mon", "Tue", "Wed", "Thu", "Fri"):
            continue
        per_day: dict[int, tuple[float, float]] = {}
        for di, cell in enumerate(row[1:6]):
            parsed = _parse_sheet_time(cell)
            if parsed is not None:
                per_day[di] = parsed
        if per_day:
            out[name] = per_day
    return out


def effective_shift_window(name, picked_day, moves):
    """Effective (start_h, end_h) shift window for a person on picked_day.

    Priority:
      1. Bounded move whose duration matches DEFAULT_HOURS[name][day_idx]
         within ~30 min (a shift-defining move, e.g. Tara 07:00-11:30).
      2. Per-day entry in the Usual Working Hours sheet.
      3. Static DEFAULT_SHIFTS[name].
    """
    default = DEFAULT_SHIFTS.get(name, (8.0, 17.0))
    day_idx = picked_day.weekday()
    if day_idx > 4:
        return default

    # Sheet override takes precedence over the static default.
    try:
        sheet_shifts = load_working_hours_from_sheet()
        if name in sheet_shifts and day_idx in sheet_shifts[name]:
            default = sheet_shifts[name][day_idx]
    except Exception:
        pass

    # Move override (only when the move IS the shift, not a mid-day swap).
    daily_hours = (DEFAULT_HOURS.get(name) or {}).get(day_idx)
    if daily_hours:
        for m in moves:
            if m.get("name") != name or m.get("action") != "move":
                continue
            s = _hhmm_to_float(m.get("start_time"))
            e = _hhmm_to_float(m.get("end_time"))
            if s is None or e is None:
                continue
            if abs((e - s) - daily_hours) <= 0.5:
                return (s, e)
    return default


def reward_blocks_for_day(daily_notes_for_day):
    """Return {name: set(int_hours)} for every Daily Notes entry on the day
    that mentions 'reward time'. Hour buckets match the columns in
    build_live_rota_df (8..17 inclusive).
    """
    import math as _math
    if not daily_notes_for_day:
        return {}
    out: dict[str, set[int]] = {}
    for entry in daily_notes_for_day:
        name = entry.get("name", "").strip()
        if not name:
            continue
        haystack = (entry.get("role", "") + " " + entry.get("note", "")).lower()
        if "reward time" not in haystack:
            continue
        rng = _parse_time_range_start_end(entry.get("time", ""))
        if rng is None:
            continue
        start, end = rng
        # Slot 'HH:00' covers HH:00 → HH+1:00. A reward block 13:30–17:00 covers
        # slots 13, 14, 15, 16 (the partial 13 slot is still flagged for
        # visibility — better to overshow by a partial hour than miss it).
        for h in range(int(_math.floor(start)), int(_math.ceil(end))):
            if 8 <= h < 18:
                out.setdefault(name, set()).add(h)
    return out


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

    # Absences are tracked on the rota itself — no manual marker here.
    # Annual leave / unplanned absence flow in via Pull rota / 🔁 Sync rota.


elif page == "📡 Hourly view (who's where)":
    import role_changes as _rc
    st.header("📡 Hourly view (who's where)")
    st.caption(
        "Roles as actually happening — overlays mid-day moves captured "
        "from #dry-run-testing-jo on top of the planned rota "
        "(temporary while testing; will move back to #client-support-leads). "
        "Post `Maisha to triage` etc. in today's anchor thread."
    )

    picked_day = st.date_input(
        "Day", value=date.today(),
        help="Pick any working day. Defaults to today.",
        key="live_rota_day",
    )

    # Need the rota assignments for this day's week
    picked_monday = picked_day - timedelta(days=picked_day.weekday())
    week_key_live = picked_monday.isoformat()
    live_assignments = None
    if f"assignments_{week_key_live}" in st.session_state:
        live_assignments = st.session_state[f"assignments_{week_key_live}"]
    else:
        try:
            gc = _cached_gspread()
            live_assignments, _ = read_original_rota(gc, picked_monday)
            st.session_state[f"assignments_{week_key_live}"] = live_assignments
        except Exception as e:
            st.error(f"Couldn't fetch rota for w/c {picked_monday.strftime('%d %b')}: {e}")
            st.stop()

    moves = _rc.load_moves(picked_day)

    # Compute each working person's effective shift window today, then size
    # the hour grid to fit the earliest start and latest end (within 07-18).
    import math as _math
    shift_windows: dict[str, tuple[float, float]] = {}
    day_idx = picked_day.weekday()
    if day_idx <= 4 and live_assignments:
        for name, days in live_assignments.items():
            role = (days.get(day_idx) or "").strip()
            if not role or role in (ROLE_NWD, ROLE_AL, ROLE_ABSENCE):
                continue
            shift_windows[name] = effective_shift_window(name, picked_day, moves)
    if shift_windows:
        starts = [s for s, _ in shift_windows.values()]
        ends = [e for _, e in shift_windows.values()]
        display_start = max(7, int(_math.floor(min(starts))))
        display_end = min(19, int(_math.ceil(max(ends))))
    else:
        display_start, display_end = 8, 18
    hours_range = list(range(display_start, display_end))

    df = _rc.build_live_rota_df(
        picked_day, live_assignments, moves, hours=hours_range,
    )

    # Blank cells outside each person's shift window (so a part-timer
    # finishing at 11:30 doesn't appear to be on their rota role at 14:00).
    if not df.empty:
        for name in df.index:
            shift_s, shift_e = shift_windows.get(name, (display_start, display_end))
            for h in hours_range:
                # Slot 'HH:00' covers HH:00 → HH+1:00.
                # In-shift if any part of the slot overlaps [shift_s, shift_e).
                if (h + 1) <= shift_s or h >= shift_e:
                    col = f"{h:02d}:00"
                    if col in df.columns:
                        df.at[name, col] = ""

    # Overlay reward-time blocks from Daily Notes onto the hour grid (after
    # the off-shift masking — reward time is part of the shift).
    reward_blocks: dict[str, set[int]] = {}
    try:
        gc_dn = _cached_gspread()
        dn_week = read_daily_notes(gc_dn, picked_monday)
        if day_idx <= 4:
            reward_blocks = reward_blocks_for_day(dn_week.get(day_idx, []))
    except Exception:
        reward_blocks = {}

    if reward_blocks and not df.empty:
        for name, hours in reward_blocks.items():
            if name not in df.index:
                continue
            for h in hours:
                col = f"{h:02d}:00"
                if col in df.columns:
                    df.at[name, col] = REWARD_TIME_LABEL

    if df.empty:
        st.info("No working roles on this day.")
    else:
        st.dataframe(
            df.style.map(role_style),
            use_container_width=True,
            height=min(60 + 35 * len(df), 700),
        )
        if reward_blocks:
            ppl = ", ".join(sorted(reward_blocks.keys()))
            st.caption(f"🎁 On reward time today: {ppl}")

    if moves:
        st.subheader(f"Today's moves ({len(moves)})")
        for m in moves:
            from_role = m.get('from_role') or '?'
            to_role = m.get('to_role') or '?'
            start = m.get('start_time') or '?'
            end = m.get('end_time') or 'open'
            noted_by = m.get('noted_by', '')
            who = f"<@{noted_by}>" if noted_by else ''
            st.caption(
                f"• **{m.get('name')}**: {from_role} → {to_role}  "
                f"{start} – {end}  _logged by {who}_"
            )
    else:
        st.caption("No moves captured for this day yet.")


elif page == "📊 Coverage":
    st.header(f"Coverage — w/c {selected_monday.strftime('%d %b %Y')}")

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

            # ── Morning message actions ──
            action_cols = st.columns(2)
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

            # ── Cover notifications ──
            if cover_items:
                with st.container(border=True):
                    st.markdown(f"**📣 Cover notifications ({len(cover_items)})**")
                    st.caption(
                        "Posts a cover line into each TL's #pegasus channel for "
                        "the people picking up cover. Preview goes to "
                        "#dry-run-testing-jo with mentions stripped."
                    )
                    cover_cols = st.columns(2)
                    with cover_cols[0]:
                        if st.button("🧪 Preview to dry-run",
                                      key=f"cover_preview_{di}",
                                      use_container_width=True):
                            with st.spinner('Posting preview…'):
                                results = send_cover_notifications(
                                    cover_items, day_label, dry_run=True
                                )
                            for name, channel_label, ok, err in results:
                                if ok:
                                    st.success(f"✅ dry-run preview for {channel_label} — {name}")
                                else:
                                    st.error(f"❌ {name} ({channel_label}): {err}")
                    with cover_cols[1]:
                        if st.button("📤 Send to pegasus channels (LIVE)",
                                      key=f"cover_send_{di}",
                                      type='primary',
                                      use_container_width=True):
                            with st.spinner('Posting cover notifications…'):
                                results = send_cover_notifications(
                                    cover_items, day_label, dry_run=False
                                )
                            for name, channel_label, ok, err in results:
                                if ok:
                                    st.success(f"✅ posted to {channel_label} — {name}")
                                else:
                                    st.error(f"❌ {name} ({channel_label}): {err}")
            else:
                st.caption("📣 No cover needed for this day.")


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
    col_pick1, col_pick2, col_pick3, col_pick4, col_pick5, col_pick6 = st.columns([2, 1, 1, 1, 1, 1])
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
            "🔃 Resync from notes",
            use_container_width=True,
            help="Two-way sync with Daily Notes: reverts any autofilled splits "
                 "whose entry has moved or disappeared, then applies any new "
                 "reward-time / appointment notes. Manually applied splits are "
                 "left alone.",
        )
    with col_pick5:
        st.write("")
        apply_moves_clicked = st.button(
            "🧩 Apply moves",
            use_container_width=True,
            help="Apply captured mid-day role moves from #dry-run-testing-jo "
                 "(temporarily — was #client-support-leads). Splits each day "
                 "using moves ≥ 30 min.",
        )
    with col_pick6:
        st.write("")
        checks_clicked = st.button(
            "🔍 Run checks",
            use_container_width=True,
            help="Auto-suggest Quality + Timeline per person. Quality = triage "
                 "archive ratio ≥85% AND Cody edits fed back in "
                 "#cody-email-triage-feedback. Timeline = work-activity gaps "
                 "over 13 min (excluding standups/lunch/1:1s); a block ≥30 min "
                 "flags for review. Pre-fills the tick boxes — you can still "
                 "override any. Takes ~30s (pulls activity + Cody).",
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
                except rt.CloudDBUnreachableError as e:
                    st.warning(str(e))
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
        with st.spinner("Resyncing splits from Daily Notes…"):
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

                # Two-way sync: reverts stale autofilled splits + applies new ones.
                result = rt.resync_autofilled_splits_from_notes(week_data, notes_by_date)
                reverted = result['reverted']
                applied_results = result['applied']

                total_applied = (
                    [('reward time', r) for r in applied_results
                     if r['status'] == 'applied' and 'reward time' in r['reason']]
                    + [('appointment', r) for r in applied_results
                       if r['status'] == 'applied' and 'appointment' in r['reason']]
                )
                total_skipped = [r for r in applied_results if r['status'] == 'skipped']

                # Save if anything changed
                if reverted or total_applied:
                    save_week(reward_friday, week_data)
                    st.session_state[rw_key] = week_data

                if reverted:
                    st.warning(f"↩️ Reverted {len(reverted)} stale autofilled split(s):")
                    for r in reverted:
                        st.caption(f"  • {r['name']} {r['date'].strftime('%a %d/%m')} — {r['reason']}")

                if total_applied:
                    st.success(f"🍯 Applied {len(total_applied)} split(s):")
                    for kind, r in total_applied:
                        st.caption(
                            f"  • {r['name']} {r['date'].strftime('%a %d/%m')} ({kind}): {r['reason']}"
                        )

                if not reverted and not total_applied:
                    st.info("Nothing to sync — Daily Notes already match the state.")

                if total_skipped:
                    with st.expander(f"⏭️ {len(total_skipped)} skipped"):
                        for r in total_skipped:
                            hrs = f"{r['hours']:.2f}h" if r['hours'] is not None else "—"
                            st.caption(
                                f"  • {r['name']} {r['date'].strftime('%a %d/%m')} "
                                f"({hrs}): {r['reason']}"
                            )

                if reverted or total_applied:
                    st.rerun()
            except Exception as e:
                st.error(f"Resync failed: {e}")

    if apply_moves_clicked:
        import role_changes as _rc
        with st.spinner("Reading captured moves and applying splits…"):
            try:
                applied_summary = []
                reset_summary = []
                skipped_summary = []
                noise_skipped = 0
                for d in reward_dates:
                    moves = _rc.load_moves(d)
                    # Pre-count moves below noise floor for summary
                    for m in (moves or []):
                        if m.get('action') in (None, 'move'):
                            try:
                                sh, sm = (int(p) for p in m.get('start_time', '0:0').split(':'))
                                eh, em = (int(p) for p in m.get('end_time', '0:0').split(':'))
                                dur = (eh * 60 + em) - (sh * 60 + sm)
                                if 0 < dur < 30:
                                    noise_skipped += 1
                            except Exception:
                                pass
                    # Process everyone who has a move on this day OR was
                    # previously split by Apply moves (so clearing the moves
                    # collapses those days back to the planned rota).
                    day_tag = d.strftime('%a %d/%m')
                    names_with_moves = {m.get('name') for m in (moves or []) if m.get('name')}
                    names_prev_applied = {
                        name for name, pw in week_data.items()
                        if any((o.get('field') or '').startswith(f'apply_moves ({day_tag})')
                               for o in pw.overrides)
                    }
                    for name in sorted(names_with_moves | names_prev_applied):
                        pw = week_data.get(name)
                        if not pw:
                            continue
                        result = rt.apply_moves_to_day(pw, d, moves or [])
                        label = f"{name} {day_tag}"
                        if result['applied']:
                            if result['segments']:
                                segs = ', '.join(f"{r} {h:.1f}h" for r, h in result['segments'])
                                applied_summary.append((label, segs))
                            else:
                                reset_summary.append((label, result['reason']))
                        elif result['reason'] not in ('no qualifying moves',):
                            skipped_summary.append((label, result['reason']))
                changed = bool(applied_summary or reset_summary)
                if changed:
                    save_week(reward_friday, week_data)
                    st.session_state[rw_key] = week_data

                if applied_summary:
                    st.success(f"Applied {len(applied_summary)} split(s) from captured moves.")
                    with st.expander(f"✏️ {len(applied_summary)} day(s) split"):
                        for lbl, segs in applied_summary:
                            st.caption(f"  • {lbl}: {segs}")

                if reset_summary:
                    st.warning(f"↩️ Reset {len(reset_summary)} day(s) back to the planned "
                               f"rota (moves cleared):")
                    for lbl, reason in reset_summary:
                        st.caption(f"  • {lbl}")

                if not changed:
                    st.info("No changes — captured moves already match the state "
                            "(or none to apply).")

                if skipped_summary or noise_skipped:
                    parts = []
                    if skipped_summary:
                        parts.append(f"{len(skipped_summary)} day-person(s) skipped")
                    if noise_skipped:
                        parts.append(f"{noise_skipped} move(s) below 30-min noise floor")
                    with st.expander("⏭️ " + ' · '.join(parts)):
                        for lbl, reason in skipped_summary:
                            st.caption(f"  • {lbl}: {reason}")

                if changed:
                    st.rerun()
            except Exception as e:
                st.error(f"Apply moves failed: {e}")

    if checks_clicked:
        with st.spinner("Pulling activity timelines + computing quality/timeline suggestions…"):
            try:
                summary = rt.run_quality_timeline_checks(week_data, reward_friday)
                save_week(reward_friday, week_data)
                st.session_state[rw_key] = week_data
                n_q_review = sum(1 for s in summary if not s['quality'])
                n_t_review = sum(1 for s in summary if not s['timeline'])
                st.success(
                    f"Checked {len(summary)} people · "
                    f"Quality flagged {n_q_review} · Timeline flagged {n_t_review}. "
                    f"Suggestions pre-filled below — override any as needed."
                )
                flagged = [s for s in summary if not s['quality'] or not s['timeline']]
                if flagged:
                    with st.expander(f"⚠️ {len(flagged)} to review"):
                        for s in flagged:
                            bits = []
                            if not s['quality']:
                                bits.append(f"Quality — {s['q_reason']}")
                            if not s['timeline']:
                                bits.append(f"Timeline — {s['t_reason']}")
                            st.caption(f"  • **{s['name']}**: " + ' · '.join(bits))
                st.rerun()
            except rt.CloudDBUnreachableError as e:
                st.warning(str(e))
            except Exception as e:
                st.error(f"Checks failed: {e}")

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
                if pw.quality_suggested is not None:
                    icon = "✅" if pw.quality_suggested else "⚠️"
                    overridden = " · overridden" if pw.quality_ok != pw.quality_suggested else ""
                    st.caption(f"🤖 {icon} {pw.quality_reason}{overridden}")
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
                if pw.timeline_suggested is not None:
                    icon = "✅" if pw.timeline_suggested else "⚠️"
                    overridden = " · overridden" if pw.timeline_ok != pw.timeline_suggested else ""
                    st.caption(f"🤖 {icon} {pw.timeline_reason}{overridden}")
                    if pw.timeline_gaps:
                        with st.expander(f"🕳️ {len(pw.timeline_gaps)} gap(s)"):
                            for g in pw.timeline_gaps:
                                gday = date.fromisoformat(g['date']).strftime('%a %d/%m')
                                st.caption(f"  • {gday} {g['start']}–{g['end']} ({g['minutes']}m)")
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

            # ── Day shape (read-only) ──
            # Splits, training and part-day leave are all driven from the Slack
            # role-change message + 🧩 Apply moves; reward-time splits come from
            # Daily Notes + 🔃 Resync. This is a read-only view of the result —
            # no manual Split/Unsplit/hours editing here any more.
            st.caption("**Day shape** (set via Slack moves + Daily Notes):")
            for d in reward_dates:
                dr = pw.days.get(d)
                if not dr or not dr.is_working:
                    continue
                day_label = d.strftime('%a %d/%m')
                if dr.segments:
                    segs = ' · '.join(
                        f"{s.role} {s.minutes / 60:.2g}h ({s.actual}/{s.target_base})"
                        for s in dr.segments
                    )
                    st.caption(f"**{day_label}** — {segs}")
                else:
                    st.caption(f"**{day_label}** — {dr.role} ({dr.shift_hours:.2g}h)")

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

    # ── TL timeline-check status ──
    # The Friday 09:00 launchd posts 3 parent messages in
    # #reward-time-questions-cs, one per TL. Each per-person ✅ unblocks
    # this Reward Time post.
    import timeline_checks as tlc
    try:
        tlc_state = tlc.refresh_check_state(reward_friday)
    except Exception:
        tlc_state = tlc._load_state(reward_friday)
    timelines_signed_off = (
        tlc.all_teams_complete(tlc_state) if tlc_state else False
    )

    if tlc_state is None:
        st.warning(
            "⏳ TL timeline checks have not started for this week. "
            "The Friday 09:00 bot will post the per-team threads in "
            "#reward-time-questions-cs. The reward time post is gated until "
            "all three teams sign off."
        )
    else:
        progress = tlc.team_progress(tlc_state)
        parts = []
        for tl, p in progress.items():
            if p["complete"]:
                tag = f"✅ {tl}"
            else:
                tag = f"{tl} {p['done']}/{p['total']}"
            if p["flag_count"]:
                tag += f" ({p['flag_count']} flag{'s' if p['flag_count'] > 1 else ''})"
            parts.append(tag)
        summary = " · ".join(parts)
        if timelines_signed_off:
            st.success(f"✅ TL timeline checks complete — {summary}")
        else:
            pending_bits = [
                f"{tl}: {', '.join(p['pending'])}"
                for tl, p in progress.items()
                if p["pending"]
            ]
            st.warning(
                f"⏳ TL timeline checks in progress — {summary}.  \n"
                f"Pending: {' · '.join(pending_bits) if pending_bits else 'none'}.  \n"
                "Reward time post is gated until all three teams sign off."
            )

    qualified_count = sum(1 for name in RT_AGENTS if week_data.get(name) and calculate_eligibility(week_data[name])[0])
    total_count = sum(1 for name in RT_AGENTS if week_data.get(name) and week_data[name].days_worked > 0)
    st.metric("Qualified", f"{qualified_count} / {total_count}")

    col_summary, col_send_test = st.columns(2)
    with col_summary:
        if st.button("💾 Save week summary to sheet", use_container_width=True):
            try:
                write_week_summary(reward_friday, week_data)
                st.success("Week summary written to Google Sheet")
            except Exception as e:
                st.error(f"Failed to write summary: {e}")

    with col_send_test:
        if st.button("🧪 Send test (to my DM)", use_container_width=True,
                      help="Posts the team-grouped reward message to your Slack DM "
                           "so you can preview before sending live."):
            try:
                write_week_summary(reward_friday, week_data)
                msg = build_reward_message_by_team(reward_friday, week_data)
                send_slack_message(JO_USER_ID, msg)
                st.success("✅ Test sent to your DM")
            except Exception as e:
                st.error(f"Failed: {e}")

    # ── Authorise reward time (replaces direct send button) ──
    # Clicking Authorise saves a state file. At 13:00 a launchd job runs
    # reward_time_friday_send.py which checks: authorisation + TL timelines
    # ✅ + queue trigger (triage + ICS both ≤ 50). If everything aligns it
    # posts the reward message. If queues fail the auth is one-shot — Jo has
    # to re-authorise next week.
    st.markdown("### Authorise reward time")
    import reward_time_friday_send as rt_fri
    existing_auth = rt_fri.load_authorisation(reward_friday)

    if existing_auth is not None:
        st.success(
            f"✅ Authorised at **{existing_auth['authorised_at'][11:16]}** by "
            f"{existing_auth['authorised_by']}. The post will fire at 13:00 if "
            "both queues are ≤ 50. If they're over, the not-triggered message "
            "goes out and authorisation clears (one-shot per week)."
        )
        if st.button("Cancel authorisation", key=f"cancel_auth_{reward_friday}"):
            rt_fri.clear_authorisation(reward_friday)
            st.rerun()
    else:
        auth_help = (
            "Saves the week summary and authorises the 13:00 reward time post. "
            "The post only fires if both queues hit the trigger (≤ 50)."
            if timelines_signed_off
            else "Blocked: TL timeline checks not yet complete (see banner above)."
        )
        if st.button("✅ Authorise reward time",
                      type='primary', use_container_width=True,
                      disabled=not timelines_signed_off,
                      help=auth_help):
            try:
                write_week_summary(reward_friday, week_data)
                state = rt_fri.save_authorisation(reward_friday)
                st.success(
                    f"Authorised at {state['authorised_at'][11:16]}. "
                    "Will fire at 13:00 if the queue trigger is met."
                )
                st.rerun()
            except Exception as e:
                st.error(f"Authorise failed: {e}")

    # Preview message — DELIBERATELY no `key=` so Streamlit re-reads `value=`
    # on every rerun (otherwise the textarea stays stuck on the first-render
    # message even after Pull actuals refreshes the underlying data).
    with st.expander("Preview Slack message"):
        msg = build_reward_slack_message(reward_friday, week_data)
        st.text_area(
            "Message preview",
            value=msg,
            height=max(280, min(700, 24 + msg.count('\n') * 22)),
        )

    # ── Daily Notes draft ──
    st.divider()
    st.markdown("### 📝 Add reward time to Daily Notes")
    st.caption(
        "Reward time earned in this reward week is taken in the *next* rota "
        "week (reward days are Tue/Wed/Thu). Edit the draft below and click "
        "**Apply** to append the rows to the rota's Daily Notes tab."
    )

    # Reward earned this reward week (Fri–Thu) is TAKEN in the next reward
    # week. Reward days are Tue/Wed/Thu, all of which sit in the rota week
    # starting on the Monday 10 days after this reward week's Friday.
    # e.g. reward_friday = Fri 15 May → reward taken in rota week starting
    # Mon 25 May.
    next_monday = reward_friday + timedelta(days=10)

    # Build the draft. We need each person's role on their target_date — read
    # the next rota week's assignments.
    next_assignments = None
    try:
        gc = _cached_gspread()
        next_assignments, _ = read_original_rota(gc, next_monday)
    except Exception as e:
        st.warning(f"Couldn't read the rota for w/c {next_monday.strftime('%d %b %Y')}: {e}")

    # Look up each person's shift from the rota's Working Hours tab —
    # gives us per-person start/end times for the reward block.
    working_hours = _cached_working_hours()
    draft = build_daily_notes_draft(
        reward_friday, week_data, next_monday,
        shifts_by_name=working_hours,
    )

    # Fill in role + cover_needed from the next rota week
    DAY_IDX_BY_NAME = {'Mon': 0, 'Tue': 1, 'Wed': 2, 'Thu': 3, 'Fri': 4}
    for row in draft:
        if next_assignments:
            target_di = (row['date'] - next_monday).days
            row['role'] = next_assignments.get(row['name'], {}).get(target_di, '')
        # Cover required for phones / triage + lender chasing
        role = row['role'] or ''
        row['cover_needed'] = role.startswith('Inbound phones') or role == 'Triage + lender chasing'

    if not draft:
        st.info("No qualifying reward time blocks for this week.")
    else:
        st.caption(
            f"**{len(draft)}** reward block(s) drafted for w/c "
            f"{next_monday.strftime('%d %b %Y')}. Edit any cell, then Apply."
        )

        # Build a DataFrame for st.data_editor
        draft_df = pd.DataFrame([
            {
                'Date':        r['date'].strftime('%a %d/%m/%Y'),
                'Time':        r['time'],
                'Name':        r['name'],
                'Role':        r['role'],
                'Note':        r['note'],
                'Cover?':     bool(r['cover_needed']),
                "Who's covering": r['whos_covering'],
            }
            for r in draft
        ])

        edited_df = st.data_editor(
            draft_df,
            num_rows='dynamic',   # allows deleting (and adding) rows
            width='stretch',
            hide_index=True,
            column_config={
                'Date': st.column_config.TextColumn('Date', disabled=True),
                'Name': st.column_config.TextColumn('Name', disabled=True),
                'Role': st.column_config.TextColumn('Role', disabled=True),
                'Time': st.column_config.TextColumn('Time', help='e.g. 2 - 5 or 9 - 12:30'),
                'Note': st.column_config.TextColumn('Note'),
                'Cover?': st.column_config.CheckboxColumn('Cover?'),
                "Who's covering": st.column_config.TextColumn("Who's covering"),
            },
            key=f"daily_notes_draft_{reward_friday.isoformat()}",
        )
        st.caption(
            "Hover the leftmost edge of a row to get the row checkbox, then "
            "press the **Delete** key to remove a row before applying."
        )

        col_apply, _ = st.columns([1, 2])
        with col_apply:
            if st.button("📝 Apply to Daily Notes",
                          type='primary', use_container_width=True,
                          help="Appends each row above as a new line in the rota sheet's Daily Notes tab."):
                # Build rows_to_apply from the edited DataFrame. Match by
                # name + date string so deleted rows are skipped naturally,
                # and any added blank rows are also skipped.
                date_by_label = {
                    r['date'].strftime('%a %d/%m/%Y'): r['date'] for r in draft
                }
                rows_to_apply = []
                for _, er in edited_df.iterrows():
                    name = str(er.get('Name') or '').strip()
                    if not name:
                        continue   # skip blank / new empty rows
                    date_label = str(er.get('Date') or '').strip()
                    target_date = date_by_label.get(date_label)
                    if target_date is None:
                        continue   # date column was edited / corrupted — skip
                    rows_to_apply.append({
                        'date': target_date,
                        'time': str(er.get('Time') or '').strip(),
                        'name': name,
                        'role': str(er.get('Role') or '').strip(),
                        'note': str(er.get('Note') or '').strip(),
                        'cover_needed': bool(er.get('Cover?') or False),
                        'whos_covering': str(er.get("Who's covering") or '').strip(),
                    })
                if not rows_to_apply:
                    st.warning("No rows to apply — all rows were deleted or blank.")
                    st.stop()
                try:
                    gc = _cached_gspread()
                    n = append_daily_notes_rows(gc, rows_to_apply)
                    st.success(
                        f"Appended {n} row(s) to the Daily Notes tab for "
                        f"w/c {next_monday.strftime('%d %b %Y')}. "
                        f"Open the rota sheet to review."
                    )
                except Exception as e:
                    st.error(f"Failed to write Daily Notes rows: {e}")


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

                        # Sync Role + Cover Needed on any Daily Notes rows in
                        # this week so they reflect the freshly-pulled rota
                        try:
                            sync = update_daily_notes_roles(gc, selected_monday,
                                                              new_assignments)
                            if sync['updated']:
                                with st.expander(
                                    f"✏️ Updated {sync['updated']} Daily Notes row(s) "
                                    f"to match the new rota"
                                ):
                                    for line in sync['detail']:
                                        st.caption(f"  • {line}")
                            elif sync['unchanged']:
                                st.caption(
                                    f"Daily Notes already in sync "
                                    f"({sync['unchanged']} row(s) checked, no changes)."
                                )
                        except Exception as e:
                            st.warning(f"Couldn't sync Daily Notes roles: {e}")

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
