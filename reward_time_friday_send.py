"""
Friday 13:00 — automated decision to send (or hold) the reward time post.

Replaces the `/personal-jo:reward-time-friday-check` scheduled task.

Flow each Friday:
  1. Jo (or whoever is covering) clicks **Authorise reward time** on the
     Reward Time page sometime between 11:00 (TL timeline-check cutoff) and
     13:00. This writes an auth_<friday>.json state file.
  2. This script fires at 13:00 via launchd
     (com.juno.cs-reward-friday-send.plist).
  3. Authorisation present + TL timeline checks all ✅ + both queues ≤ 50
     → posts the team-grouped reward message to #reward-time-questions-cs.
  4. Trigger not met (either queue > 50) → posts the not-triggered message
     in the same channel.
  5. Authorisation missing or timelines incomplete → DMs Jo and posts
     nothing (one-shot; she has to re-authorise next week).

After a send (either branch) the auth state is cleared so it can't fire
again in the same week.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path

import requests

import populate_reward_sheet as prs
import reward_time as rt
import timeline_checks as tlc
from compat import get_postgres_url, get_slack_token

# ── Config ──────────────────────────────────────────────────────────────────

SLACK_API = "https://slack.com/api"
SLACK_CHANNEL_REWARD_QUESTIONS = "C0AS755PW49"  # #reward-time-questions-cs
SLACK_CHANNEL_DRY_RUN = "C0AUP24HQPP"           # #dry-run-testing-jo
JO_USER_ID = "U07KFSSCUNT"

STATE_DIR = Path.home() / ".juno/scheduled-tasks/reward-time-friday"
LOG_FILE = STATE_DIR / "reward-friday-send.log"

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

    friday = tlc.get_check_friday()
    logger.info(f"Reward Friday send check for reward week starting {friday}")

    # 1. Authorisation
    auth = load_authorisation(friday)
    if auth is None:
        logger.info("No authorisation found — holding, no post.")
        _dm_jo(
            f":pause_button: *Reward time not authorised* — "
            f"no post fired at 13:00 for week of {friday.strftime('%a %d %b')}.\n"
            "Open the Reward Time page and click *Authorise reward time* if you "
            "still want it to go this week (one-shot; authorisation does not "
            "carry forward)."
        )
        return

    # 2. Timeline checks
    tl_state = tlc._load_state(friday)
    if tl_state is None or not tlc.all_teams_complete(tl_state):
        progress = tlc.team_progress(tl_state) if tl_state else {}
        missing = [
            f"{tl}: {p['done']}/{p['total']}"
            for tl, p in progress.items()
            if not p["complete"]
        ]
        logger.warning(f"Timelines not all ✅: {missing or 'no state file'}")
        _dm_jo(
            ":pause_button: *Reward time authorised but timelines not all complete* — "
            f"holding. Outstanding at 13:00: "
            f"{', '.join(missing) if missing else 'no timeline state file'}."
        )
        return

    # 3. Queue queries
    try:
        triage, ics = fetch_queue_counts()
        logger.info(f"Queue counts — triage={triage}, ics={ics}")
    except Exception as e:
        logger.exception("Queue query failed")
        _dm_jo(
            f":warning: *Reward time check failed* — could not run the "
            f"queue query.\n```{type(e).__name__}: {e}```"
        )
        try:
            _slack_post(
                channel,
                ":warning: Reward time check failed — could not run queue query.",
            )
        except Exception:
            logger.exception("Failed posting failure note to channel")
        return

    # 4. Trigger logic
    triggered = triage <= QUEUE_TRIGGER_MAX and ics <= QUEUE_TRIGGER_MAX

    if not triggered:
        _slack_post(channel, render_not_triggered(triage, ics))
        _dm_jo(
            f":hourglass_flowing_sand: Reward time *not triggered* — "
            f"triage {triage}, ICS {ics}. Posted the not-triggered message "
            "to #reward-time-questions-cs. Authorisation cleared; re-authorise "
            "next week."
        )
        clear_authorisation(friday)
        return

    # 5. Trigger met — post the team-grouped reward message
    week_data = rt.load_week(friday)
    if not week_data:
        logger.warning("No saved reward week — can't build the team message.")
        _dm_jo(
            f":x: Reward time *triggered* (triage {triage}, ICS {ics}) but "
            "no saved reward week was found. Open the Reward Time page and "
            "click *Save week summary* first, then re-authorise next week."
        )
        return

    # Apply the TLs' manual Timeline/Quality sign-off from the tracker sheet —
    # the sheet is the source of truth. A person is only quality/timeline
    # confirmed once a TL has picked ✅ Pass (or ⏸️ N/A); Fail and blank both
    # leave the gate closed. Anyone the TLs haven't signed off is flagged to Jo
    # so a missing sign-off doesn't silently withhold someone's reward.
    try:
        signoff = prs.apply_tl_signoff_from_sheet(week_data, friday)
        rt.save_week(friday, week_data)  # persist gates so state matches the post
        if signoff["incomplete"]:
            names = ", ".join(sorted(signoff["incomplete"]))
            logger.warning(f"Incomplete TL sign-off: {names}")
            _dm_jo(
                ":warning: Posting reward time, but these people have *no "
                f"Timeline/Quality sign-off* in the tracker yet: {names}. "
                "They'll show as not-confirmed until a TL fills the sheet — "
                "nudge the TLs, then I can re-post."
            )
    except Exception:
        logger.exception("TL sign-off read-back failed; using saved gate values")

    try:
        rt.write_week_summary(friday, week_data)
        msg = rt.build_reward_message_by_team(friday, week_data)
        _slack_post(channel, msg)
    except Exception as e:
        logger.exception("Reward post failed")
        _dm_jo(
            f":x: Reward time triggered but the post failed.\n"
            f"```{type(e).__name__}: {e}```"
        )
        return

    _dm_jo(
        f":white_check_mark: Reward time posted to #reward-time-questions-cs "
        f"(triage {triage}, ICS {ics})."
    )
    clear_authorisation(friday)


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
            "authorisation + timeline state."
        ),
    )
    p.add_argument(
        "--friday",
        help="Reward Friday (YYYY-MM-DD). Defaults to the last reward Friday.",
    )
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    friday = (
        date.fromisoformat(args.friday) if args.friday else tlc.get_check_friday()
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
        tl_state = tlc._load_state(friday)
        if tl_state is None:
            print("Timeline checks: no state file")
        else:
            for tl, p in tlc.team_progress(tl_state).items():
                tag = "✅" if p["complete"] else f"{p['done']}/{p['total']}"
                print(f"  {tl}: {tag}")


if __name__ == "__main__":
    _cli()
