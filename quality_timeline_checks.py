"""
Thursday-evening auto-run of the reward-week quality + timeline checks.

The reward week runs Friday → Thursday, so by Thursday evening the week's
work is complete. This script:

  1. refresh_now() — pulls fresh Looker actuals + skips for the week so the
     archive-ratio quality signal reflects the full week (the 15:30 daily
     pull misses late-afternoon triage).
  2. run_quality_timeline_checks() — computes the quality + timeline
     SUGGESTIONS per person (archive ratio + Cody compliance; work-activity
     gaps).
  3. Pre-fills those suggestions into the Reward Time tracker sheet's TL View
     (Timeline + Quality columns), only filling blank cells so a TL's own
     entry is never clobbered. The sheet is the only reward-time interface now
     the Streamlit app is retired.
  4. DMs Jo a summary of who's flagged, ready for Friday review.

Suggestions are also saved to local FS + Google Drive (via save_week's
dual-write) so the Python eligibility gate + Friday send stay in sync.

Runs as a launchd cron (com.juno.cs-quality-timeline-checks) Thu evening.
Logs to ~/.claude/scheduled-tasks/quality-timeline/checks.log.
"""
from __future__ import annotations

import logging
import sys
import traceback
from datetime import date, datetime
from pathlib import Path

import requests

from compat import get_slack_token
import populate_reward_sheet as prs
import refresh_daemon as rd
import reward_time as rt

JO_USER_ID = 'U07KFSSCUNT'
LOG_FILE = Path.home() / '.claude/scheduled-tasks/quality-timeline/checks.log'


def _dm_jo(text: str) -> None:
    """Best-effort DM. Logs the failure but doesn't raise."""
    try:
        r = requests.post(
            'https://slack.com/api/chat.postMessage',
            headers={'Authorization': f'Bearer {get_slack_token()}',
                     'Content-Type': 'application/json'},
            json={'channel': JO_USER_ID, 'text': text},
            timeout=10,
        )
        data = r.json()
        if not data.get('ok'):
            logging.warning(f"DM to Jo failed: {data.get('error')}")
    except Exception as e:
        logging.warning(f"DM to Jo blew up: {e}")


def _summary_dm(friday, summary, sheet_link="") -> str:
    q_flagged = [s for s in summary if not s['quality']]
    t_flagged = [s for s in summary if not s['timeline']]
    lines = [
        f"🔍 *Thursday reward checks* — w/c {friday.strftime('%d %b')} (Fri→Thu)",
        f"Checked {len(summary)} people · Quality flagged {len(q_flagged)} · "
        f"Timeline flagged {len(t_flagged)}",
    ]
    if q_flagged:
        lines.append("\n*Quality to review:*")
        for s in q_flagged:
            lines.append(f"• {s['name']} — {s['q_reason']}")
    if t_flagged:
        lines.append("\n*Timeline to review:*")
        for s in t_flagged:
            lines.append(f"• {s['name']} — {s['t_reason']}")
    tail = ("\nThese are heads-up suggestions only — the Timeline/Quality "
            "columns in the Reward Time tracker are left blank for the Team "
            "Leads to complete manually before Friday.")
    if sheet_link:
        tail += f"\n{sheet_link}"
    lines.append(tail)
    return '\n'.join(lines)


def main():
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s quality-checks %(levelname)s %(message)s',
        handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stdout)],
    )
    started = datetime.now()
    friday = rt.get_reward_friday()
    logging.info(f"Thursday checks starting for reward week w/c {friday}")

    # ── 1. Fresh actuals + skips so archive ratio reflects the full week ──
    try:
        result = rd.refresh_now()
        if 'note' in result:
            logging.info(f"refresh_now no-op: {result['note']}")
        else:
            logging.info(f"refresh_now ok: {result['people']} people")
    except rt.CloudDBUnreachableError:
        logging.exception("DB unreachable during refresh")
        _dm_jo("❌ Thursday reward checks failed — DB unreachable. VPN connected?")
        return
    except Exception as e:
        logging.exception("refresh_now failed")
        _dm_jo(f"❌ Thursday reward checks — actuals refresh failed.\n"
               f"```{type(e).__name__}: {e}```")
        return

    # ── 2. Compute quality + timeline suggestions ──
    week_data = rt.load_week(friday)
    if not week_data:
        _dm_jo("ℹ️ Thursday reward checks — no saved reward week to check yet.")
        return
    # Rebuild day shapes rota-first so the checks see reality: roles/absences
    # from the canonical rota, then reward-time/appointment slots, then the
    # week's Slack moves.
    try:
        rt.sync_rota_from_sheet(week_data, friday)
    except Exception:
        logging.exception("sync_rota_from_sheet failed (continuing)")
    try:
        rt.resync_from_daily_notes(week_data, friday)
    except Exception:
        logging.exception("resync_from_daily_notes failed (continuing)")
    try:
        rt.apply_all_moves(week_data, friday)
    except Exception:
        logging.exception("apply_all_moves failed (continuing with planned rota)")
    try:
        summary = rt.run_quality_timeline_checks(week_data, friday)
        rt.save_week(friday, week_data)   # dual-writes local + Drive
    except rt.CloudDBUnreachableError:
        logging.exception("DB unreachable during checks")
        _dm_jo("❌ Thursday reward checks failed — DB unreachable. VPN connected?")
        return
    except Exception as e:
        logging.exception("run_quality_timeline_checks failed")
        _dm_jo(f"❌ Thursday reward checks failed.\n"
               f"```{type(e).__name__}: {e}\n\n{traceback.format_exc()[-500:]}```")
        return

    # ── 3. Resolve the sheet link for the heads-up DM ──
    # We deliberately do NOT pre-fill the TL View Timeline/Quality cells: the
    # Team Leads complete those sign-offs manually. The suggestions computed
    # above are still saved to state and surfaced in Jo's DM as a heads-up.
    # find_or_copy_sheet also ensures the TLs have writer access.
    sheet_link = ""
    try:
        sheet_id = prs.find_or_copy_sheet(friday)
        sheet_link = f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"
    except Exception:
        logging.exception("Could not resolve sheet link (suggestions still saved to state)")

    # ── 4. Heads-up DM ──
    _dm_jo(_summary_dm(friday, summary, sheet_link))
    duration = (datetime.now() - started).total_seconds()
    logging.info(f"Thursday checks done in {duration:.1f}s — "
                 f"{len(summary)} people")


if __name__ == '__main__':
    main()
