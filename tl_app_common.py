"""
Shared Streamlit module for the per-TL reward-time apps.

Each TL has a thin wrapper (tl_app_jess.py, tl_app_yasmin.py, tl_app_courtney.py)
that calls run_app(tl_name).

What the TL sees:
  - Only their own team members
  - Reward-time grid (throughput vs targets) — NOT the full rota
  - One tab per team member with hours / role splits / quality / timelines / request
  - "Submit team for Jo" button → DMs Jo's dry-run channel

State is shared with rota_app.py via reward_time.STATE_DIR (week_<friday>.json)
so Jo can review and approve in her main app.
"""
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests
import streamlit as st

import reward_time as rt
from reward_time import (
    REWARD_DAYS,
    SKIP_THRESHOLD_WEEKLY,
    SPLITTABLE_ROLES,
    STANDARD_SHIFT_HOURS,
    TL_TEAMS,
    add_override,
    adjust_shift_hours,
    build_week,
    calculate_eligibility,
    format_reward_hours,
    get_reward_friday,
    get_weekday_dates,
    load_week,
    pull_day_data,
    pull_skips,
    save_week,
    split_day,
    unsplit_day,
    update_day_actuals,
)

# Jo's dry-run channel for TL submissions
SLACK_DRY_RUN_CHANNEL = 'C0AUP24HQPP'  # #dry-run-testing-jo
SLACK_TOKEN_PATH = Path.home() / '.config/juno/claude-code/slack-token'  # legacy ref
from compat import get_slack_token

# ── Brand palette (mirrors setup_tl_view.py) ───────────────────────────────
JUNO_BLUE = '#0F5CB8'
JUNO_BLUE_SOFT = '#E6EEF8'
JUNO_GREEN = '#218B21'
JUNO_GREEN_SOFT = '#E8F6E8'
SOFT_RED = '#FADBD8'
SOFT_RED_TEXT = '#922B21'
SOFT_AMBER = '#FEF3D6'
SOFT_AMBER_TEXT = '#8C5C00'
NEUTRAL = '#F2F3F4'
NEUTRAL_TEXT = '#6B6B6B'


# ── Stylesheet ──────────────────────────────────────────────────────────────

_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Overpass:wght@400;500;600;700&display=swap');

html, body, [class*="css"], [data-testid="stAppViewContainer"] {
  font-family: 'Overpass', -apple-system, BlinkMacSystemFont, sans-serif;
}

/* Tighten the default top padding so the hero banner sits near the top */
[data-testid="stAppViewContainer"] > .main > div.block-container {
  padding-top: 1.5rem;
  padding-bottom: 4rem;
  max-width: 1200px;
}

/* Hero banner */
.tl-hero {
  background: linear-gradient(135deg, #0F5CB8 0%, #1973D6 100%);
  color: #fff;
  padding: 24px 28px;
  border-radius: 12px;
  margin-bottom: 20px;
  box-shadow: 0 1px 3px rgba(15, 92, 184, 0.15);
}
.tl-hero h1 {
  color: #fff !important;
  margin: 0 0 4px 0;
  font-weight: 700;
  font-size: 1.6rem;
}
.tl-hero .tl-hero-sub {
  color: rgba(255,255,255,0.92);
  font-size: 0.95rem;
  font-weight: 400;
}

/* Pill */
.tl-pill {
  display: inline-block;
  padding: 3px 10px;
  border-radius: 999px;
  font-size: 0.8rem;
  font-weight: 500;
  margin-right: 6px;
}
.tl-pill-blue   { background:#E6EEF8; color:#0F5CB8; }
.tl-pill-green  { background:#E8F6E8; color:#218B21; }
.tl-pill-red    { background:#FADBD8; color:#922B21; }
.tl-pill-amber  { background:#FEF3D6; color:#8C5C00; }
.tl-pill-grey   { background:#F2F3F4; color:#6B6B6B; }

/* Status banner inside a person tab */
.tl-status {
  padding: 12px 16px;
  border-radius: 10px;
  margin-bottom: 16px;
  font-size: 0.95rem;
  font-weight: 500;
}
.tl-status-green { background:#E8F6E8; color:#196F3D; border-left: 4px solid #218B21; }
.tl-status-red   { background:#FADBD8; color:#922B21; border-left: 4px solid #C0392B; }
.tl-status-amber { background:#FEF3D6; color:#8C5C00; border-left: 4px solid #E0A800; }
.tl-status-blue  { background:#E6EEF8; color:#0F5CB8; border-left: 4px solid #0F5CB8; }
.tl-status-grey  { background:#F2F3F4; color:#6B6B6B; border-left: 4px solid #B0B0B0; }

/* Section headings */
.tl-section {
  font-size: 0.78rem;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: #6B6B6B;
  margin: 18px 0 8px 0;
}

/* Day-row card */
.tl-day-row {
  background: #F8FAFD;
  border: 1px solid #E5EBF3;
  border-radius: 8px;
  padding: 8px 12px;
  margin-bottom: 6px;
}

/* Streamlit button polish */
.stButton > button {
  border-radius: 8px;
  font-weight: 500;
  font-family: 'Overpass', sans-serif;
  padding: 6px 14px;
  transition: all 0.15s ease;
}
.stButton > button:hover {
  transform: translateY(-1px);
}

/* Metric cards */
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

/* Tabs */
.stTabs [data-baseweb="tab-list"] {
  gap: 4px;
  background: #F4F7FB;
  padding: 4px;
  border-radius: 8px;
}
.stTabs [data-baseweb="tab"] {
  background: transparent;
  border-radius: 6px;
  padding: 8px 14px;
  font-weight: 500;
}
.stTabs [aria-selected="true"] {
  background: #fff !important;
  box-shadow: 0 1px 2px rgba(0,0,0,0.06);
  color: #0F5CB8 !important;
}

/* Sidebar */
[data-testid="stSidebar"] {
  background: #F4F7FB;
  border-right: 1px solid #E5EBF3;
}
[data-testid="stSidebar"] .stMarkdown h1 {
  color: #0F5CB8;
  font-size: 1.3rem;
}

/* Sub-headers ("Submit to Jo" etc.) */
h2, h3 {
  color: #1A1A1A;
  font-weight: 600;
}

/* Captions a bit clearer */
[data-testid="stCaptionContainer"] {
  color: #6B6B6B;
  font-size: 0.85rem;
}

/* Submit-to-Jo call-out card */
.tl-submit-card {
  background: linear-gradient(135deg, #E6EEF8 0%, #F4F7FB 100%);
  border: 1px solid #C8D8EE;
  border-radius: 12px;
  padding: 20px 24px;
  margin-top: 8px;
}
</style>
"""


def _inject_css() -> None:
    """Apply the global stylesheet. Called once at the top of run_app()."""
    st.markdown(_CSS, unsafe_allow_html=True)


def _hero(tl_name: str, reward_friday: date, reward_end: date) -> None:
    """Top branded banner."""
    st.markdown(
        f"""
        <div class="tl-hero">
          <h1>🏆 Reward Time — {tl_name}'s team</h1>
          <div class="tl-hero-sub">
            Reward week <strong>{reward_friday.strftime('%a %d %b')}</strong>
            → <strong>{reward_end.strftime('%a %d %b %Y')}</strong>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _send_slack(channel: str, text: str) -> None:
    """Post a message to Slack. Raises on API failure."""
    token = get_slack_token()
    resp = requests.post(
        'https://slack.com/api/chat.postMessage',
        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
        json={'channel': channel, 'text': text},
        timeout=10,
    )
    data = resp.json()
    if not data.get('ok'):
        raise RuntimeError(f"Slack API error: {data.get('error', 'unknown')}")


def _ensure_week_loaded(reward_friday: date) -> dict:
    """Load (or initialise) the reward-time week. Returns {name: PersonWeek}."""
    rw_key = f"tl_reward_{reward_friday.isoformat()}"
    existing = load_week(reward_friday)
    if existing:
        st.session_state[rw_key] = existing
    elif rw_key not in st.session_state:
        st.session_state[rw_key] = build_week(reward_friday)
    return st.session_state[rw_key]


def _refresh_from_rota(reward_friday: date) -> None:
    """Re-read the rota and overlay it onto the saved state for this week.

    Picks up unplanned absences and role changes added after the week was
    first built. Preserves all TL inputs (quality, timelines, skips, notes,
    submission state).
    """
    import generate_rota as gr
    from generate_rota import read_original_rota, get_gspread

    gc = get_gspread()
    fri_monday = reward_friday - timedelta(days=reward_friday.weekday())
    mon_monday = fri_monday + timedelta(days=7)
    fri_assignments, _ = read_original_rota(gc, fri_monday)
    mon_assignments, _ = read_original_rota(gc, mon_monday)

    week_data = load_week(reward_friday)
    if not week_data:
        week_data = build_week(
            reward_friday,
            assignments_fri=fri_assignments,
            assignments_mon_thu=mon_assignments,
        )
    changes = rt.sync_rota_into_week(
        week_data, reward_friday,
        assignments_fri=fri_assignments,
        assignments_mon_thu=mon_assignments,
    )
    save_week(reward_friday, week_data)
    rw_key = f"tl_reward_{reward_friday.isoformat()}"
    st.session_state[rw_key] = week_data
    if changes:
        summary = ', '.join(
            f"{n} {d.strftime('%a')} → {new}" for n, d, _, new in changes[:5]
        )
        extra = f' (+{len(changes) - 5} more)' if len(changes) > 5 else ''
        st.success(f"Synced {len(changes)} change(s): {summary}{extra}")
    else:
        st.info("Rota matches saved state — nothing changed.")


def _pull_current_figures(reward_friday: date, reward_dates: list[date]) -> None:
    """Pull live Looker actuals for every elapsed working day in this week.

    Mirrors the 'Pull actuals' button in rota_app. Updates the shared state
    file so the change shows immediately in Jo's app too.
    """
    today = date.today()
    targets = [d for d in reward_dates if d <= today]
    if not targets:
        st.warning("No elapsed working days in this reward week yet.")
        return

    week_data = load_week(reward_friday)
    if not week_data:
        st.warning("No state for this week yet — try Refresh from rota first.")
        return

    try:
        for d in targets:
            actuals = pull_day_data(d)
            update_day_actuals(week_data, d, actuals)
        # Skips are a weekly figure — refresh once per click.
        skips = pull_skips(reward_friday)
        for name, pw in week_data.items():
            pw.skips = skips.get(name, 0)
        save_week(reward_friday, week_data)
        rw_key = f"tl_reward_{reward_friday.isoformat()}"
        st.session_state[rw_key] = week_data
        st.success(
            f"Pulled current figures + skips for {len(targets)} day(s): "
            f"{', '.join(d.strftime('%a %d/%m') for d in targets)}"
        )
    except rt.CloudDBUnreachableError as e:
        st.warning(str(e))
    except Exception as e:
        st.error(f"Pull failed: {e}")


def _status_chip(pw) -> tuple[str, str]:
    """Return (chip_html, banner_class) describing where this person is."""
    if pw.jo_decision == 'approved':
        return ('✅ Jo approved', 'tl-status-green')
    if pw.jo_decision == 'question':
        return ('❓ Jo has a question', 'tl-status-amber')
    if pw.tl_submitted_at:
        return ('📤 Sent to Jo', 'tl-status-blue')
    return ('✏️ In progress', 'tl-status-grey')


def _throughput_text(dr) -> str:
    """Build a compact 'actual/target' string for a day cell."""
    if not dr or not dr.is_working:
        return dr.role if dr else '—'
    if dr.segments:
        parts = []
        for seg in dr.segments:
            if seg.target_base > 0:
                parts.append(f"{seg.actual}/{seg.target_base}")
            else:
                parts.append(rt._short_role(seg.role))
        return ' | '.join(parts)
    if dr.target_base > 0:
        ratio_str = ''
        if dr.role.startswith('Triage'):
            ratio_str = f" ({dr.archive_ratio:.0%})"
        prorata_str = f" [{dr.shift_hours}h]" if dr.shift_hours < STANDARD_SHIFT_HOURS else ''
        return f"{dr.actual}/{dr.target_base}{ratio_str}{prorata_str}"
    return dr.role


def _throughput_style(val):
    """Soft green / soft red colour for throughput cells."""
    if not isinstance(val, str):
        return ''
    if '/' not in val:
        if val in ('Annual leave', 'Non working day', 'Unplanned absence', 'Training'):
            return f'background-color: {NEUTRAL}; color: {NEUTRAL_TEXT}'
        return ''
    all_met = True
    saw_fraction = False
    for chunk in val.split('|'):
        chunk = chunk.strip()
        if '/' not in chunk:
            continue
        try:
            actual = int(chunk.split('/')[0].strip())
            target_str = chunk.split('/')[1].split('(')[0].split('[')[0].strip()
            target = int(target_str)
            saw_fraction = True
            if actual < target:
                all_met = False
                break
        except (ValueError, IndexError):
            continue
    if not saw_fraction:
        return ''
    return (
        f'background-color: {JUNO_GREEN_SOFT}; color: {JUNO_GREEN}'
        if all_met else
        f'background-color: {SOFT_RED}; color: {SOFT_RED_TEXT}'
    )


def _render_team_grid(team_members: list[str], week_data: dict, reward_dates: list[date]) -> None:
    """Read-only summary grid: one row per team member, days as columns."""
    day_labels = [d.strftime('%a %d/%m') for d in reward_dates]
    rows = []
    for name in team_members:
        pw = week_data.get(name)
        if not pw:
            continue
        row = {'Name': name}
        for i, d in enumerate(reward_dates):
            row[day_labels[i]] = _throughput_text(pw.days.get(d))
        row['Skips'] = pw.skips
        chip, _ = _status_chip(pw)
        row['Status'] = chip
        eligible, level, hours, _ = calculate_eligibility(pw)
        if eligible:
            row['Reward'] = f"{'⭐ Stretch' if level == 'stretch' else '✅ Base'} · {format_reward_hours(hours)}"
        else:
            row['Reward'] = '—'
        rows.append(row)

    if not rows:
        st.info('No team members loaded for this week yet.')
        return

    df = pd.DataFrame(rows)
    styled = df.style.map(_throughput_style, subset=day_labels)
    st.dataframe(styled, width='stretch', hide_index=True, height=min(60 + 35 * len(rows), 420))


def _render_person_tab(
    name: str,
    pw,
    reward_dates: list[date],
    week_data: dict,
    reward_friday: date,
    rw_key: str,
) -> None:
    """Editing UI for one team member, rendered inside a tab."""
    eligible, level, hours, reason = calculate_eligibility(pw)
    chip_text, banner_class = _status_chip(pw)

    # Status banner — colour-coded depending on stage
    if pw.jo_decision == 'question':
        banner_body = f"❓ <strong>Jo asked:</strong> {pw.jo_question_text}"
    elif pw.jo_decision == 'approved':
        banner_body = f"✅ <strong>Jo approved.</strong> Result: {'⭐ Stretch' if level == 'stretch' else '✅ Base' if eligible else '❌ None'} · {format_reward_hours(hours) if eligible else '—'}"
    elif pw.tl_submitted_at:
        banner_body = (
            f"📤 Submitted to Jo at {pw.tl_submitted_at[:16].replace('T', ' ')}. "
            "You can still edit and re-submit if needed."
        )
    else:
        auto_result = '⭐ Stretch' if level == 'stretch' else '✅ Base' if eligible else '❌ None'
        banner_body = (
            f"<strong>Auto result from the data:</strong> {auto_result} "
            f"{'· ' + format_reward_hours(hours) if eligible else ''} — <em>{reason}</em>"
        )
    st.markdown(f'<div class="tl-status {banner_class}">{banner_body}</div>',
                unsafe_allow_html=True)

    # ── Their week (mini grid) ────────────────────────────────────
    st.markdown('<div class="tl-section">Their week</div>', unsafe_allow_html=True)
    week_row = {}
    for d in reward_dates:
        week_row[d.strftime('%a %d/%m')] = _throughput_text(pw.days.get(d))
    week_df = pd.DataFrame([week_row])
    styled = week_df.style.map(_throughput_style)
    st.dataframe(styled, width='stretch', hide_index=True, height=70)

    # ── Day editor ────────────────────────────────────────────────
    st.markdown('<div class="tl-section">Edit hours &amp; split roles</div>',
                unsafe_allow_html=True)

    for d_idx, d in enumerate(reward_dates):
        dr = pw.days.get(d)
        if not dr:
            continue
        day_label = d.strftime('%a %d/%m')

        if not dr.is_working:
            st.markdown(
                f'<div class="tl-day-row" style="opacity:0.65"><strong>{day_label}</strong> '
                f'— <span style="color:#6B6B6B">{dr.role or "Not working"}</span></div>',
                unsafe_allow_html=True,
            )
            continue

        with st.container(border=False):
            cols = st.columns([2.2, 1.2, 1.6, 1])
            with cols[0]:
                st.markdown(
                    f'<div style="padding-top:6px"><strong>{day_label}</strong>'
                    f'<br><span style="color:#6B6B6B;font-size:0.85rem">{dr.role}</span></div>',
                    unsafe_allow_html=True,
                )
            with cols[1]:
                new_hours = st.number_input(
                    'Hours',
                    min_value=1.0, max_value=10.0,
                    value=float(dr.shift_hours), step=0.5,
                    key=f"tl_hours_{rw_key}_{name}_{d_idx}",
                )
                if abs(new_hours - dr.shift_hours) > 0.01:
                    old_h = adjust_shift_hours(pw, d, new_hours)
                    add_override(pw, f'shift_hours ({day_label})', old_h, new_hours,
                                 'TL set in TL app')
                    save_week(reward_friday, week_data)
                    st.rerun()
            with cols[2]:
                if dr.segments:
                    seg_lines = []
                    for seg in dr.segments:
                        seg_lines.append(
                            f"<span style='color:#6B6B6B;font-size:0.85rem'>"
                            f"{rt._short_role(seg.role)}: {seg.minutes/60:.1f}h "
                            f"({seg.actual}/{seg.target_base})</span>"
                        )
                    st.markdown('<br>'.join(seg_lines), unsafe_allow_html=True)
                    if st.button('↩️ Unsplit', key=f"tl_unsplit_{rw_key}_{name}_{d_idx}"):
                        orig_role = dr.segments[0].role
                        unsplit_day(pw, d, orig_role)
                        add_override(pw, f'unsplit ({day_label})', dr.role, orig_role,
                                     'TL reverted split')
                        save_week(reward_friday, week_data)
                        st.rerun()
                else:
                    split_key = f"tl_splitting_{rw_key}_{name}_{d_idx}"
                    if st.session_state.get(split_key):
                        st.caption('Splitting…')
                    else:
                        st.caption('Single role')
            with cols[3]:
                if not dr.segments:
                    split_key = f"tl_splitting_{rw_key}_{name}_{d_idx}"
                    if st.button('✂️ Split', key=f"tl_split_btn_{rw_key}_{name}_{d_idx}"):
                        st.session_state[split_key] = True
                        st.rerun()

            # Split panel expands beneath the row when active
            split_key = f"tl_splitting_{rw_key}_{name}_{d_idx}"
            if st.session_state.get(split_key) and not dr.segments:
                with st.container(border=True):
                    st.markdown(f"**Split {day_label} into 2 or 3 roles**")
                    _render_split_controls(pw, dr, d, day_label, rw_key, name, d_idx,
                                           week_data, reward_friday)

    # ── Weekly checks ─────────────────────────────────────────────
    st.markdown('<div class="tl-section">Weekly checks</div>', unsafe_allow_html=True)
    with st.container(border=True):
        c_q, c_t, c_s = st.columns([1, 1, 1.2])
        with c_q:
            new_quality = st.checkbox('✅ Quality OK', value=pw.quality_ok,
                                       key=f"tl_q_{rw_key}_{name}",
                                       help='Tick once you’ve checked their work quality this week.')
            if new_quality != pw.quality_ok:
                add_override(pw, 'quality_ok', pw.quality_ok, new_quality, 'TL ticked')
                pw.quality_ok = new_quality
                save_week(reward_friday, week_data)
                st.rerun()
        with c_t:
            new_timeline = st.checkbox('⏱️ Timelines OK', value=pw.timeline_ok,
                                        key=f"tl_t_{rw_key}_{name}",
                                        help='Tick once you’ve checked timelines are met.')
            if new_timeline != pw.timeline_ok:
                add_override(pw, 'timeline_ok', pw.timeline_ok, new_timeline, 'TL ticked')
                pw.timeline_ok = new_timeline
                save_week(reward_friday, week_data)
                st.rerun()
        with c_s:
            skip_limit = int(SKIP_THRESHOLD_WEEKLY * (pw.days_worked / 5.0 if pw.days_worked else 1))
            new_skips = st.number_input(
                f'Skips (limit {skip_limit})',
                min_value=0, max_value=999, value=int(pw.skips), step=1,
                key=f"tl_skips_{rw_key}_{name}",
            )
            if new_skips != pw.skips:
                add_override(pw, 'skips', pw.skips, new_skips, 'TL adjusted')
                pw.skips = new_skips
                save_week(reward_friday, week_data)
                st.rerun()

    # ── Request to Jo ─────────────────────────────────────────────
    st.markdown('<div class="tl-section">What you’re asking Jo to do</div>',
                unsafe_allow_html=True)
    with st.container(border=True):
        c_req, c_reason = st.columns([1, 2])
        with c_req:
            req_labels = {
                '': 'Auto (from data)',
                'base': '✅ Grant Base',
                'stretch': '⭐ Grant Stretch',
                'deny': '❌ Deny',
            }
            label_to_val = {v: k for k, v in req_labels.items()}
            opts = list(req_labels.values())
            current = req_labels.get(pw.tl_request_level, 'Auto (from data)')
            sel = st.selectbox(
                'Request',
                opts, index=opts.index(current),
                key=f"tl_req_{rw_key}_{name}",
                help='“Auto” = use whatever the data says. Pick Grant/Deny to override.',
            )
            new_req = label_to_val[sel]
        with c_reason:
            new_notes = st.text_area(
                'Reason for Jo (required if you’re overriding)',
                value=pw.tl_notes, height=80,
                key=f"tl_notes_{rw_key}_{name}",
                placeholder='e.g. near baseline on Wed but had system issues — please grant Base.',
            )

        unsaved = (new_req != pw.tl_request_level) or (new_notes != pw.tl_notes)
        if unsaved:
            if st.button('💾 Save', key=f"tl_save_{rw_key}_{name}", type='primary'):
                if new_req in ('base', 'stretch', 'deny') and not new_notes.strip():
                    st.error('Add a reason — you’re overriding the automatic result.')
                else:
                    if new_req != pw.tl_request_level:
                        add_override(pw, 'tl_request_level',
                                     pw.tl_request_level, new_req,
                                     new_notes.strip() or 'No reason given')
                    pw.tl_request_level = new_req
                    pw.tl_notes = new_notes
                    save_week(reward_friday, week_data)
                    st.rerun()
        else:
            st.caption('✓ Saved')


def _render_split_controls(pw, dr, d, day_label, rw_key, name, d_idx,
                            week_data, reward_friday):
    """Inline 2- or 3-role split UI."""
    available = [r for r in SPLITTABLE_ROLES if r != dr.role]
    role_b = st.selectbox(
        'Second role',
        available,
        key=f"tl_split_b_{rw_key}_{name}_{d_idx}",
    )
    third_options = ['(none)'] + [r for r in SPLITTABLE_ROLES
                                    if r not in (dr.role, role_b)]
    role_c = st.selectbox(
        'Third role (optional)',
        third_options,
        key=f"tl_split_c_{rw_key}_{name}_{d_idx}",
    )
    three_way = role_c != '(none)'

    if three_way:
        third = dr.shift_hours / 3
        hrs_a = st.number_input(
            f'Hours on {dr.role}',
            min_value=0.5, max_value=dr.shift_hours - 1.0,
            value=round(third, 1), step=0.5,
            key=f"tl_split_hra_{rw_key}_{name}_{d_idx}",
        )
        hrs_b = st.number_input(
            f'Hours on {role_b}',
            min_value=0.5, max_value=max(0.5, dr.shift_hours - hrs_a - 0.5),
            value=round(min(third, dr.shift_hours - hrs_a - 0.5), 1), step=0.5,
            key=f"tl_split_hrb_{rw_key}_{name}_{d_idx}",
        )
        hrs_c = round(dr.shift_hours - hrs_a - hrs_b, 1)
        st.caption(f"→ {hrs_c}h on {role_c}")
    else:
        half = dr.shift_hours / 2
        hrs_a = st.number_input(
            f'Hours on {dr.role}',
            min_value=0.5, max_value=dr.shift_hours - 0.5,
            value=half, step=0.5,
            key=f"tl_split_hra_{rw_key}_{name}_{d_idx}",
        )
        hrs_b = round(dr.shift_hours - hrs_a, 1)
        hrs_c = 0
        st.caption(f"→ {hrs_b}h on {role_b}")

    c_apply, c_cancel = st.columns(2)
    with c_apply:
        if st.button('Apply split',
                      key=f"tl_split_apply_{rw_key}_{name}_{d_idx}",
                      type='primary'):
            orig_role = dr.role
            if three_way:
                spec = [(orig_role, hrs_a), (role_b, hrs_b), (role_c, hrs_c)]
                reason = (f"TL split {hrs_a}h {orig_role} / "
                           f"{hrs_b}h {role_b} / {hrs_c}h {role_c}")
            else:
                spec = [(orig_role, hrs_a), (role_b, hrs_b)]
                reason = f"TL split {hrs_a}h {orig_role} / {hrs_b}h {role_b}"
            split_day(pw, d, spec)
            add_override(pw, f'split ({day_label})', orig_role, dr.role, reason)
            save_week(reward_friday, week_data)
            del st.session_state[f"tl_splitting_{rw_key}_{name}_{d_idx}"]
            st.rerun()
    with c_cancel:
        if st.button('Cancel', key=f"tl_split_cancel_{rw_key}_{name}_{d_idx}"):
            del st.session_state[f"tl_splitting_{rw_key}_{name}_{d_idx}"]
            st.rerun()


def _build_submission_message(tl_name: str, reward_friday: date,
                              team_members: list[str], week_data: dict) -> str:
    """Build the DM body for Jo summarising what the TL is requesting."""
    lines = [
        f"📋 *{tl_name} has submitted reward time — w/c {reward_friday.strftime('%d %b %Y')}*",
        '',
    ]
    for name in team_members:
        pw = week_data.get(name)
        if not pw or pw.days_worked == 0:
            continue
        eligible, level, hours, _ = calculate_eligibility(pw)
        if pw.tl_request_level == 'deny':
            ask = '❌ Deny'
        elif pw.tl_request_level == 'base':
            ask = '✅ Grant Base'
        elif pw.tl_request_level == 'stretch':
            ask = '⭐ Grant Stretch'
        else:
            auto_word = '⭐ Stretch' if level == 'stretch' else '✅ Base' if eligible else '❌ None'
            ask = f"Auto → {auto_word}"
        line = f"• *{name}* — {ask}"
        if eligible:
            line += f" ({format_reward_hours(hours)})"
        if pw.tl_notes:
            line += f"\n   _Note: {pw.tl_notes}_"
        lines.append(line)
    lines.append('')
    lines.append('Please approve or ask a question in the rota app → Reward Time → Approval Queue.')
    return '\n'.join(lines)


def run_app(tl_name: str) -> None:
    """Run the Streamlit app for a single TL."""
    if tl_name not in TL_TEAMS:
        st.error(f"Unknown TL: {tl_name}. Must be one of {list(TL_TEAMS)}.")
        st.stop()

    st.set_page_config(
        page_title=f"Reward Time — {tl_name}",
        page_icon='🏆',
        layout='wide',
    )

    # Password gate — blocks everything below until the user signs in
    from auth import require_login
    require_login()

    _inject_css()

    team_members = TL_TEAMS[tl_name]

    # ── Sidebar ────────────────────────────────────────────────────
    with st.sidebar:
        st.markdown(f"### 👋 Hi, {tl_name}")
        st.caption('You can only see your own team.')
        st.divider()
        today = date.today()
        default_friday = get_reward_friday(today)
        pick_date = st.date_input(
            'Reward week containing',
            value=default_friday,
            help='Pick any date in the week — the app jumps to that Fri–Thu reward week.',
        )
        reward_friday = get_reward_friday(pick_date)
        reward_dates = get_weekday_dates(reward_friday)
        reward_end = reward_friday + timedelta(days=6)

        st.divider()
        st.caption('**Bring data up to date**')
        if st.button('📊 Pull current figures', width='stretch', type='primary',
                      help='Pulls live throughput from Looker for every elapsed working day this week.'):
            with st.spinner('Pulling current figures from Looker…'):
                _pull_current_figures(reward_friday, reward_dates)
            st.rerun()
        if st.button('🔁 Refresh from rota', width='stretch',
                      help='Re-reads the latest rota — picks up unplanned absences, role changes etc.'):
            _refresh_from_rota(reward_friday)
            st.rerun()

        st.divider()
        st.caption('**Your team this week**')
        for n in team_members:
            st.markdown(f"• {n}")

    # ── Hero header ─────────────────────────────────────────────────
    _hero(tl_name, reward_friday, reward_end)

    rw_key = f"tl_reward_{reward_friday.isoformat()}"
    week_data = _ensure_week_loaded(reward_friday)

    # ── Status metrics ─────────────────────────────────────────────
    submitted = sum(1 for n in team_members
                     if week_data.get(n) and week_data[n].tl_submitted_at)
    approved = sum(1 for n in team_members
                    if week_data.get(n) and week_data[n].jo_decision == 'approved')
    questioned = sum(1 for n in team_members
                      if week_data.get(n) and week_data[n].jo_decision == 'question')
    total = sum(1 for n in team_members
                 if week_data.get(n) and week_data[n].days_worked > 0)

    m1, m2, m3, m4 = st.columns(4)
    m1.metric('Team this week', total)
    m2.metric('Submitted', submitted)
    m3.metric('Jo approved', approved)
    m4.metric('Jo has questions', questioned)

    # ── Team summary grid ─────────────────────────────────────────
    st.markdown('### Your team’s reward time week')
    st.caption('Each cell shows actual ÷ target for that day. Green = hit · red = miss.')
    _render_team_grid(team_members, week_data, reward_dates)

    # ── Per-person tabs ───────────────────────────────────────────
    st.markdown('### Review each team member')
    st.caption('Click a name to set their hours, splits, ticks, and what you want Jo to grant.')

    # Build tab labels with a status emoji so TLs can see progress at a glance
    tab_labels = []
    for n in team_members:
        pw = week_data.get(n)
        if not pw or pw.days_worked == 0:
            tab_labels.append(f"○ {n}")
        elif pw.jo_decision == 'approved':
            tab_labels.append(f"✅ {n}")
        elif pw.jo_decision == 'question':
            tab_labels.append(f"❓ {n}")
        elif pw.tl_submitted_at:
            tab_labels.append(f"📤 {n}")
        elif (pw.quality_ok or pw.timeline_ok or pw.tl_request_level):
            tab_labels.append(f"✏️ {n}")
        else:
            tab_labels.append(f"○ {n}")

    tabs = st.tabs(tab_labels)
    for tab, name in zip(tabs, team_members):
        with tab:
            pw = week_data.get(name)
            if not pw:
                st.info('No data for this person this week.')
                continue
            _render_person_tab(name, pw, reward_dates, week_data, reward_friday, rw_key)

    # ── Submit to Jo ──────────────────────────────────────────────
    st.markdown('### Send to Jo')

    missing = []
    nothing_to_submit = True
    for name in team_members:
        pw = week_data.get(name)
        if not pw or pw.days_worked == 0:
            continue
        nothing_to_submit = False
        if pw.tl_request_level in ('base', 'stretch', 'deny') and not pw.tl_notes.strip():
            missing.append(f"{name} — add a reason for your override request")

    with st.container(border=True):
        st.markdown('<div class="tl-submit-card">', unsafe_allow_html=True)
        if nothing_to_submit:
            st.info('Nobody on your team has working days this week yet — nothing to submit.')
        else:
            if missing:
                st.warning(
                    '**Fix these before sending:**\n'
                    + '\n'.join(f"• {m}" for m in missing)
                )
            else:
                st.success(
                    "You're ready. Hit **Submit team to Jo** to send the summary to her dry-run channel."
                )

            preview = _build_submission_message(tl_name, reward_friday,
                                                  team_members, week_data)
            with st.expander('👀 Preview what Jo will see'):
                st.text_area('Preview', value=preview, height=300,
                              key=f"tl_preview_text_{rw_key}",
                              label_visibility='collapsed')

            c1, c2 = st.columns([1, 1])
            with c2:
                if st.button('📤 Submit team to Jo',
                              type='primary', width='stretch',
                              disabled=bool(missing)):
                    try:
                        _send_slack(SLACK_DRY_RUN_CHANNEL, preview)
                        now = datetime.now().isoformat(timespec='seconds')
                        for name in team_members:
                            pw = week_data.get(name)
                            if pw and pw.days_worked > 0:
                                pw.tl_submitted_at = now
                        save_week(reward_friday, week_data)
                        st.success("Sent! Jo will review and post the decision.")
                        st.rerun()
                    except Exception as e:
                        st.error(f'Failed to send: {e}')
        st.markdown('</div>', unsafe_allow_html=True)
