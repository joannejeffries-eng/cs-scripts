"""
Friday 13:00 — automated decision to send (or hold) the reward time post.

Replaces the `/personal-jo:reward-time-friday-check` scheduled task.

Flow each Friday:
  1. Jo (or whoever is covering) clicks **Authorise reward time** on the
     Reward Time page. This writes an auth_<friday>.json state file.
  2. This script fires hourly 13:00–17:00 via launchd
     (com.juno.cs-reward-friday-send.plist) and retries until it can post.
  3. Authorisation present + both queues ≤ 50 + the tracker sheet fully signed
     off → posts the team-grouped reward message to #reward-time-questions-cs
     and writes a `posted_<friday>` marker so later hours are no-ops. The TLs'
     Timeline/Quality sign-off is read from the tracker sheet (source of truth).
  4. Not triggered yet (either queue > 50) → stays silent and retries next hour
     (the reward-time-friday-check / hourly-poll tasks report queue status).
  5. Authorisation missing, or sign-off incomplete → notifies once (Jo DM, or
     in away mode the cover channel tagging the cover person) and keeps
     retrying that day.

Once posted, the auth state is cleared and the posted marker stops further
sends that week. Authorisation is one-shot per week (does not carry forward).
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

import populate_reward_sheet as prs
import reward_time as rt
from compat import get_postgres_url, get_slack_token


def _reward_friday() -> date:
    """The Friday that starts the just-completed reward week (Fri→Thu)."""
    return rt.get_reward_friday(date.today() - timedelta(days=1))

# ── Config ──────────────────────────────────────────────────────────────────

SLACK_API = "https://slack.com/api"
SLACK_CHANNEL_REWARD_QUESTIONS = "C0AS755PW49"  # #reward-time-questions-cs
SLACK_CHANNEL_DRY_RUN = "C0AUP24HQPP"           # #dry-run-testing-jo
JO_USER_ID = "U07KFSSCUNT"
COURTNEY_USER_ID = "U07030XA3NV"                # cover point-person while Jo's away

STATE_DIR = Path.home() / ".juno/scheduled-tasks/reward-time-friday"
LOG_FILE = STATE_DIR / "reward-friday-send.log"

# Optional "away mode" — while Jo is off, routine status DMs go silent and any
# PROBLEM (hold / failure) is escalated to #reward-time-questions-cs tagging the
# cover person instead of DM'ing Jo (who won't see it). Controlled by a JSON
# file so the window is data, not a code deploy:
#   ~/.juno/scheduled-tasks/reward-time-friday/away_mode.json
#   {"from": "2026-07-22", "to": "2026-08-05",
#    "escalate_channel": "C0AS755PW49", "tag_user_id": "U07030XA3NV"}
# Active on [from, to) (to = exclusive, i.e. the first day back). Missing file
# or malformed dates → normal Jo-DM behaviour.
AWAY_MODE_PATH = STATE_DIR / "away_mode.json"

# Both queues need to be at or below this for reward time to fire.
QUEUE_TRIGGER_MAX = 50

# Mirrors the SQL in the existing /reward-time-friday-check skill so the
# behaviour stays one-for-one when we swap over.
TRIAGE_SQL = """
SELECT COUNT(DISTINCT d.id)
FROM doable d
JOIN cases c ON d.case_id = c.id
JOIN doable_role_restriction drr ON d.id = drr.doable_id
JOIN staff_capability sc ON drr.required_capability_id = sc.id
WHERE d.db_doable_type = 'email'
  AND d.db_status = 'available'
  AND sc.name = 'Email (triage)';
"""

ICS_SQL = """
SELECT COUNT(DISTINCT d.id)
FROM doable d
JOIN cases c ON d.case_id = c.id
JOIN doable_role_restriction drr ON d.id = drr.doable_id
JOIN staff_capability sc ON drr.required_capability_id = sc.id
WHERE d.db_status = 'available'
  AND sc.name = 'Initial Case setup'
  AND c.status = 'instructed'
  AND NOT EXISTS (
    SELECT 1 FROM fields f
    WHERE f.case_id = c.id
      AND f.name = 'completion_date'
      AND f.raw_value IS NOT NULL
  );
"""

logger = logging.getLogger(__name__)


# ── Authorisation state ─────────────────────────────────────────────────────


def auth_state_path(reward_friday: date) -> Path:
    return STATE_DIR / f"auth_{reward_friday.isoformat()}.json"


def load_authorisation(reward_friday: date) -> dict | None:
    path = auth_state_path(reward_friday)
    if not path.exists():
        return None
    return json.loads(path.read_text())


def save_authorisation(reward_friday: date, authorised_by: str = "Jo") -> dict:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state = {
        "reward_friday": reward_friday.isoformat(),
        "authorised_at": datetime.now().isoformat(timespec="seconds"),
        "authorised_by": authorised_by,
    }
    auth_state_path(reward_friday).write_text(json.dumps(state, indent=2))
    return state


def clear_authorisation(reward_friday: date) -> None:
    path = auth_state_path(reward_friday)
    if path.exists():
        path.unlink()


# ── Per-week run state (idempotent hourly retries) ──────────────────────────
# The send fires hourly 13:00–17:00 on Fridays so a late TL sign-off still gets
# paid the same day. These markers make repeat runs safe: `posted` short-circuits
# once the reward has gone out, and `_notify_once` stops a persistent hold/
# failure from pinging every hour.


def _flag_path(reward_friday: date, key: str) -> Path:
    return STATE_DIR / f"{key}_{reward_friday.isoformat()}.json"


def _already_posted(reward_friday: date) -> bool:
    return _flag_path(reward_friday, "posted").exists()


def _mark_posted(reward_friday: date) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    _flag_path(reward_friday, "posted").write_text(
        datetime.now().isoformat(timespec="seconds")
    )


def _notify_once(reward_friday: date, key: str, text: str, problem: bool = True) -> None:
    """Send a notice at most once per (friday, key) — avoids hourly-retry spam.
    problem=True routes via _notify_problem (Jo DM / away-mode escalation);
    problem=False via _notify_status (silent in away mode)."""
    marker = _flag_path(reward_friday, f"notified-{key}")
    if marker.exists():
        return
    (_notify_problem if problem else _notify_status)(text)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    marker.write_text(datetime.now().isoformat(timespec="seconds"))


# ── Slack ───────────────────────────────────────────────────────────────────


def _slack_post(channel: str, text: str) -> None:
    r = requests.post(
        f"{SLACK_API}/chat.postMessage",
        headers={
            "Authorization": f"Bearer {get_slack_token()}",
            "Content-Type": "application/json",
        },
        json={"channel": channel, "text": text, "link_names": True},
        timeout=10,
    )
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Slack post failed: {data.get('error')}")


def _dm_jo(text: str) -> None:
    try:
        _slack_post(JO_USER_ID, text)
    except Exception:
        logger.exception("DM to Jo failed")


def _away_mode() -> dict | None:
    """Return the away-mode config if today is inside its [from, to) window."""
    try:
        cfg = json.loads(AWAY_MODE_PATH.read_text())
        start = date.fromisoformat(cfg["from"])
        end = date.fromisoformat(cfg["to"])
    except Exception:
        return None
    return cfg if start <= date.today() < end else None


def _notify_problem(text: str) -> None:
    """Route a hold/failure notice. Normally DMs Jo; in away mode it escalates
    to the cover channel tagging the cover person so ops don't silently stall."""
    away = _away_mode()
    if not away:
        _dm_jo(text)
        return
    channel = away.get("escalate_channel", SLACK_CHANNEL_REWARD_QUESTIONS)
    tag = away.get("tag_user_id", COURTNEY_USER_ID)
    try:
        _slack_post(channel, f"<@{tag}> {text}")
    except Exception:
        logger.exception("Away-mode escalation post failed; falling back to DM")
        _dm_jo(text)


def _notify_status(text: str) -> None:
    """Route a routine (non-problem) status note. Silent in away mode."""
    if _away_mode():
        logger.info(f"[away mode — status DM suppressed] {text}")
        return
    _dm_jo(text)


# ── Queue queries ───────────────────────────────────────────────────────────


def fetch_queue_counts() -> tuple[int, int]:
    """Return (triage_count, ics_count). Raises on DB failure."""
    conn = rt._connect_postgres(get_postgres_url())
    try:
        with conn.cursor() as cur:
            cur.execute(TRIAGE_SQL)
            row = cur.fetchone()
            triage = int(row[0]) if row and row[0] is not None else 0
            cur.execute(ICS_SQL)
            row = cur.fetchone()
            ics = int(row[0]) if row and row[0] is not None else 0
        return triage, ics
    finally:
        conn.close()


# ── Messages ────────────────────────────────────────────────────────────────


def render_not_triggered(triage: int, ics: int) -> str:
    return (
        ":hourglass_flowing_sand: *Reward time not triggered this week.*\n"
        "\n"
        f"• *Triage queue:* {triage}\n"
        f"• *Case setup queue:* {ics}\n"
        "\n"
        f"Trigger needs both queues ≤ {QUEUE_TRIGGER_MAX}."
    )


# ── Main ────────────────────────────────────────────────────────────────────


def run_friday_send(channel: str | None = None) -> None:
    """Friday 13:00 launchd entry point.

    `channel` overrides #reward-time-questions-cs — pass the dry-run channel
    when testing.
    """
    channel = channel or SLACK_CHANNEL_REWARD_QUESTIONS

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s reward-friday-send %(levelname)s %(message)s",
        handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stdout)],
    )

    friday = _reward_friday()
    logger.info(f"Reward Friday send check for reward week starting {friday}")

    # 0. Idempotency — the send fires hourly 13:00–17:00; once the reward has
    # posted (or the week was cleared), later runs are silent no-ops.
    if _already_posted(friday):
        logger.info("Reward already posted this week — nothing to do.")
        return

    # 1. Authorisation
    auth = load_authorisation(friday)
    if auth is None:
        logger.info("No authorisation found — holding, no post.")
        _notify_once(
            friday, "noauth",
            f":pause_button: *Reward time not authorised* — "
            f"nothing will post for week of {friday.strftime('%a %d %b')} until "
            "someone clicks *Authorise reward time* on the Reward Time page "
            "(one-shot; does not carry forward).",
        )
        return

    # (TL Timeline/Quality sign-off is no longer a Slack-reaction gate — it's
    # read from the tracker sheet after the trigger check, in step 5.)

    # 3. Queue queries
    try:
        triage, ics = fetch_queue_counts()
        logger.info(f"Queue counts — triage={triage}, ics={ics}")
    except Exception as e:
        logger.exception("Queue query failed")
        _notify_once(
            friday, "queuefail",
            f":warning: *Reward time check failed* — could not run the "
            f"queue query.\n```{type(e).__name__}: {e}```",
        )
        return

    # 4. Trigger logic — if not triggered yet, stay silent and let a later hour
    # retry (queues often drop through the afternoon). Authorisation is kept so
    # the next run can still fire. The separate reward-time-friday-check /
    # hourly-poll tasks already report queue status to the channel, so the send
    # doesn't post a not-triggered notice itself.
    triggered = triage <= QUEUE_TRIGGER_MAX and ics <= QUEUE_TRIGGER_MAX
    if not triggered:
        logger.info(f"Not triggered yet (triage {triage}, ICS {ics}) — will retry.")
        return

    # 5. Trigger met — post the team-grouped reward message
    week_data = rt.load_week(friday)
    if not week_data:
        logger.warning("No saved reward week — can't build the team message.")
        _notify_once(
            friday, "noweek",
            f":x: Reward time *triggered* (triage {triage}, ICS {ics}) but "
            "no saved reward week was found — the Reward Time sheet hasn't been "
            "saved for this week.",
        )
        return

    # Apply the TLs' manual Timeline/Quality sign-off from the tracker sheet —
    # the sheet is the source of truth (replaces the old Slack-reaction gate).
    # A person is only quality/timeline confirmed once a TL has picked ✅ Pass
    # (or ⏸️ N/A); ❌ Fail is a decision (reward denied, fine to post). A blank
    # cell means the TL hasn't signed off yet — that HOLDS the whole post so we
    # never publish a summary that wrongly zeroes someone out. Authorisation is
    # left intact so the hourly poll re-fires once the sheet is complete.
    signoff = prs.apply_tl_signoff_from_sheet(week_data, friday)
    rt.save_week(friday, week_data)  # persist gates so state matches the post
    if signoff["incomplete"]:
        names = ", ".join(sorted(signoff["incomplete"]))
        logger.warning(f"Incomplete TL sign-off, holding: {names}")
        _notify_once(
            friday, "signoff",
            ":pause_button: *Reward time authorised + triggered, but the "
            "tracker sheet isn't fully signed off* — holding. No Timeline/"
            f"Quality decision yet for: {names}. Fill the *TL View* Timeline "
            "& Quality columns and it posts automatically on the next hourly "
            "retry (through 17:00).",
        )
        return

    try:
        rt.write_week_summary(friday, week_data)
        msg = rt.build_reward_message_by_team(friday, week_data)
        _slack_post(channel, msg)
    except Exception as e:
        logger.exception("Reward post failed")
        _notify_once(
            friday, "postfail",
            f":x: Reward time triggered but the post failed.\n"
            f"```{type(e).__name__}: {e}```",
        )
        return

    _mark_posted(friday)
    clear_authorisation(friday)
    _notify_status(
        f":white_check_mark: Reward time posted to #reward-time-questions-cs "
        f"(triage {triage}, ICS {ics})."
    )


def _cli():
    import argparse

    p = argparse.ArgumentParser(
        description="Friday 13:00 reward-time send (replaces the skill)."
    )
    p.add_argument(
        "command",
        choices=["run", "run-dry-run", "authorise", "clear-auth", "status"],
        help=(
            "run: post live. run-dry-run: post to #dry-run-testing-jo. "
            "authorise / clear-auth: manage the per-week authorisation state "
            "(normally driven from the Streamlit). status: show current "
            "authorisation + TL sheet sign-off state."
        ),
    )
    p.add_argument(
        "--friday",
        help="Reward Friday (YYYY-MM-DD). Defaults to the last reward Friday.",
    )
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    friday = (
        date.fromisoformat(args.friday) if args.friday else _reward_friday()
    )
    print(f"Reward Friday: {friday}")

    if args.command == "run":
        run_friday_send()
    elif args.command == "run-dry-run":
        run_friday_send(channel=SLACK_CHANNEL_DRY_RUN)
    elif args.command == "authorise":
        s = save_authorisation(friday, authorised_by="cli")
        print(f"Authorised at {s['authorised_at']}")
    elif args.command == "clear-auth":
        clear_authorisation(friday)
        print("Cleared.")
    elif args.command == "status":
        auth = load_authorisation(friday)
        print(f"Authorisation: {auth or 'NONE'}")
        week_data = rt.load_week(friday)
        if not week_data:
            print("TL sign-off: no saved reward week")
        else:
            signoff = prs.apply_tl_signoff_from_sheet(week_data, friday)
            if signoff["incomplete"]:
                print(f"TL sign-off: incomplete — {', '.join(sorted(signoff['incomplete']))}")
            else:
                print("TL sign-off: ✅ complete")


if __name__ == "__main__":
    _cli()
