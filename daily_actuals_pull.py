"""
Daily auto-pull of reward-time actuals + skips.

Runs as a launchd cron at 06:00 weekdays. Mirrors Jess's pattern for
cloud-hosted Juno dashboards: schedule a daily pull, drop the data
somewhere the apps can read, send Jo a heads-up.

Three things happen on each run:
  1. `refresh_daemon.refresh_now()` pulls fresh actuals + skips from the
     Looker Postgres (private VPC — Jo's laptop has VPN access). Writes
     the updated week JSON to both local FS and Google Drive so the
     cloud rota app sees it on its next interaction.
  2. `write_daily_actuals_snapshot()` writes a flat human-readable
     snapshot to the 'Daily Actuals Snapshot' tab on the Reward Time
     Audit sheet — for Jo to glance at without opening the app.
  3. A DM goes to Jo confirming the run, or reporting the error.

If the Mac is asleep at 06:00, macOS launchd reruns this on next wake.

Logs to ~/.claude/scheduled-tasks/refresh-daemon/daemon.log (shared
with the on-demand daemon).
"""
from __future__ import annotations

import logging
import sys
import traceback
from datetime import date, datetime
from pathlib import Path

import requests

from compat import get_slack_token
import refresh_daemon as rd
import reward_time as rt

# Jo's Slack user ID — DM target. Posting to a user-ID channel auto-opens
# the DM, no need to call conversations.open.
JO_USER_ID = 'U07KFSSCUNT'

LOG_FILE = Path.home() / '.claude/scheduled-tasks/refresh-daemon/daemon.log'


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


def main():
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s daily-pull %(levelname)s %(message)s',
        handlers=[
            logging.FileHandler(LOG_FILE),
            logging.StreamHandler(sys.stdout),
        ],
    )
    started = datetime.now()
    logging.info("Daily actuals pull starting")

    # ── 1. Pull from Looker ───────────────────────────────────────────
    try:
        result = rd.refresh_now()
        if 'note' in result:
            logging.info(f"refresh_now no-op: {result['note']}")
            _dm_jo(f"ℹ️ Daily refresh — {result['note']}")
            return
        logging.info(f"refresh_now ok: {result['people']} people, "
                      f"{len(result['days'])} day(s)")
    except rt.CloudDBUnreachableError as e:
        # Shouldn't happen here (we're on Jo's laptop) but surface clearly
        logging.exception("DB unreachable")
        _dm_jo(f"❌ Daily refresh failed — DB unreachable. VPN connected?\n```{e}```")
        return
    except Exception as e:
        logging.exception("refresh_now failed")
        _dm_jo(
            f"❌ Daily refresh failed at {started.strftime('%a %d %b %H:%M')}.\n"
            f"```{type(e).__name__}: {e}\n\n"
            f"{traceback.format_exc()[-600:]}```"
        )
        return

    # ── 2. Fold Slack moves into the week before snapshotting ─────────
    # Apply moves used to be a manual button — now it runs on every refresh
    # so role switches (Clare to chasing, Maisha to phones, …) show on the
    # spreadsheet + app without needing a click.
    try:
        friday = result['friday']
        week_data = rt.load_week(friday)
        # Rebuild each day in layers, rota first:
        #   1. sync_rota_from_sheet — roles/absences from the canonical rota
        #      (headless 'Sync rota'; resets splits so a stale base can't survive)
        #   2. resync_from_daily_notes — reward-time / appointment slots
        #   3. apply_all_moves — captured Slack role moves
        # Each layer composes on the previous one.
        try:
            rt.sync_rota_from_sheet(week_data, friday)
        except Exception:
            logging.exception("sync_rota_from_sheet failed (continuing)")
        try:
            rt.resync_from_daily_notes(week_data, friday)
        except Exception:
            logging.exception("resync_from_daily_notes failed (continuing)")
        move_changes = rt.apply_all_moves(week_data, friday)
        rt.save_week(friday, week_data)
        logging.info(f"synced rota + notes + applied {len(move_changes)} move-derived day shape(s)")
    except Exception as e:
        logging.exception("rota/notes/moves sync failed (continuing)")

    # ── 2b. Mirror captured moves into the rota Daily Notes ───────────
    # Catch-up for any moves the role-change daemon missed (e.g. the laptop
    # was asleep when the move was posted). Idempotent — unchanged days
    # write nothing. Covers every reward-week weekday up to today.
    try:
        import generate_rota as gr
        today = date.today()
        gc = gr.get_gspread()
        for d in rt.get_weekday_dates(friday):
            if d <= today:
                res = gr.sync_moves_to_daily_notes(gc, d)
                if any(res[k] for k in ('added', 'updated', 'deleted')):
                    logging.info(f"daily-notes sync {d}: {res}")
    except Exception:
        logging.exception("daily-notes sync failed (continuing)")

    # ── 3. Write the human-readable sheet snapshot ────────────────────
    snapshot_ok = False
    try:
        week_data = rt.load_week(friday)
        rt.write_daily_actuals_snapshot(friday, week_data)
        snapshot_ok = True
        logging.info(f"snapshot written for w/c {friday}")
    except Exception as e:
        logging.exception("snapshot write failed")
        # Don't bail — the refresh itself succeeded.
        _dm_jo(
            f"⚠️ Daily refresh OK but the sheet snapshot failed.\n"
            f"```{type(e).__name__}: {e}```"
        )

    # ── 3. Heads-up DM on success ─────────────────────────────────────
    if snapshot_ok:
        days_str = ', '.join(d.strftime('%a %d/%m') for d in result['days'])
        _dm_jo(
            f"✅ Daily refresh at {started.strftime('%a %d %b %H:%M')}\n"
            f"• {result['people']} people across {len(result['days'])} day(s): {days_str}\n"
            f"• Snapshot tab updated on the Reward Time Audit sheet"
        )

    duration = (datetime.now() - started).total_seconds()
    logging.info(f"Daily actuals pull done in {duration:.1f}s")


if __name__ == '__main__':
    main()
