"""
Friday-morning TL timeline-check sign-off flow.

At 09:00 each Friday this module posts three parent messages in
#reward-time-questions-cs, one per TL, each with a thread reply per team
member. TLs eyeball Looker DB 334 and react ✅ on each person's thread
message once checked. The Reward Time page in the Streamlit reads the
reactions and won't let reward time go out until every team is signed off.

State is persisted to ~/.juno/scheduled-tasks/timeline-checks/week_<friday>.json
so re-runs / daemon polls find the right messages to update.

This file owns the data model and the Slack interactions. The live-update
daemon and the Streamlit banner read state via read_check_state().
"""
from __future__ import annotations

import json
import logging
import time as _time
from datetime import date, datetime, time, timedelta
from pathlib import Path

import requests

import reward_time as rt
from compat import get_slack_token

# ── Config ──────────────────────────────────────────────────────────────────

SLACK_CHANNEL_REWARD_QUESTIONS = "C0AS755PW49"  # #reward-time-questions-cs
SLACK_CHANNEL_DRY_RUN = "C0AUP24HQPP"           # #dry-run-testing-jo

LOOKER_DB_334_URL = "https://juno.cloud.looker.com/dashboards/334"

JO_USER_ID = "U07KFSSCUNT"

# Friday loop runs from the 09:00 launchd fire until this time, then exits.
# The reward time cutoff is 11:00; we keep watching for 30 min after so any
# late ticks or flag replies show up before reward time runs.
FRIDAY_LOOP_END = time(11, 30)
FRIDAY_LOOP_INTERVAL_SEC = 60

# Team-to-TL mapping. Hard-coded for now; update when people join or leave.
TEAMS: dict[str, dict] = {
    "Jess": {
        "user_id": "U07Q2EEN3SL",
        "members": [
            "Cris J",
            "Maisha J",
            "Erika",
            "Clare Brown",
            "Lucy Riordan",
        ],
    },
    "Yasmin": {
        "user_id": "U06MH8MRVM0",
        "members": ["Noemi", "Tara", "Sophie", "Kirsty", "Lizzie"],
    },
    "Courtney": {
        "user_id": "U07030XA3NV",
        "members": [
            "Harriet",
            "Kate O'Neill",
            "Jade",
            "Becky",
            "Fionn",
            "Elida",
        ],
    },
}

STATE_DIR = Path.home() / ".juno/scheduled-tasks/timeline-checks"
SLACK_API = "https://slack.com/api"
TICK_EMOJI = "white_check_mark"

logger = logging.getLogger(__name__)


# ── Helpers ─────────────────────────────────────────────────────────────────


def get_check_friday(today: date | None = None) -> date:
    """Friday that started the reward week being checked.

    Called on a Friday morning, this returns the Friday from 7 days ago
    (the start of the just-completed reward week).
    """
    if today is None:
        today = date.today()
    return rt.get_reward_friday(today - timedelta(days=1))


def _state_path(reward_friday: date) -> Path:
    return STATE_DIR / f"week_{reward_friday.isoformat()}.json"


def _load_state(reward_friday: date) -> dict | None:
    path = _state_path(reward_friday)
    if not path.exists():
        return None
    return json.loads(path.read_text())


def _save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = _state_path(date.fromisoformat(state["reward_friday"]))
    path.write_text(json.dumps(state, indent=2))


def _week_label(reward_friday: date) -> str:
    end = reward_friday + timedelta(days=6)
    return f"week {reward_friday.strftime('%a %d %b')} → {end.strftime('%a %d %b')}"


def _slack_post(channel: str, text: str, thread_ts: str | None = None) -> str:
    body = {"channel": channel, "text": text, "link_names": True}
    if thread_ts:
        body["thread_ts"] = thread_ts
    r = requests.post(
        f"{SLACK_API}/chat.postMessage",
        headers={
            "Authorization": f"Bearer {get_slack_token()}",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=10,
    )
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Slack post failed: {data.get('error')}")
    return data["ts"]


def _slack_update(channel: str, ts: str, text: str) -> None:
    r = requests.post(
        f"{SLACK_API}/chat.update",
        headers={
            "Authorization": f"Bearer {get_slack_token()}",
            "Content-Type": "application/json",
        },
        json={"channel": channel, "ts": ts, "text": text, "link_names": True},
        timeout=10,
    )
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Slack update failed: {data.get('error')}")


def _fetch_thread(channel: str, parent_ts: str) -> list[dict]:
    """Return the parent + every reply in a thread, including reactions inline.

    conversations.replies bundles each message's `reactions` on the same
    payload, so one call per parent gives us tick state for every per-person
    reply without needing the reactions:read scope.
    """
    r = requests.get(
        f"{SLACK_API}/conversations.replies",
        headers={"Authorization": f"Bearer {get_slack_token()}"},
        params={"channel": channel, "ts": parent_ts, "limit": 200},
        timeout=10,
    )
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        return []
    return data.get("messages", [])  # [0] is the parent


# ── Rendering ───────────────────────────────────────────────────────────────


def _render_parent(
    week_label: str,
    tl_name: str,
    tl_user_id: str,
    members: list[str],
    member_status: dict[str, dict],
    flag_count: int = 0,
) -> str:
    """Render the parent message body with current per-member status.

    member_status[name] = {'ticked': bool}
    flag_count = number of extra thread replies beyond the bot's per-person ones.
    """
    n = len(members)
    done = [m for m in members if member_status.get(m, {}).get("ticked")]
    pending = [m for m in members if not member_status.get(m, {}).get("ticked")]

    lines = [
        f"🕒 Timeline checks — {week_label}",
        "",
        f"<@{tl_user_id}> — your team timeline checks for the week",
    ]
    if len(done) == n:
        lines.append(f"Status: ✅ Complete ({n}/{n})")
    else:
        lines.append(f"Status: {len(done)}/{n} done")
        if done:
            lines.append(f"  Done: {', '.join(done)}")
        if pending:
            lines.append(f"  Pending: {', '.join(pending)}")
    if flag_count:
        lines.append(f"  Issues raised in thread ({flag_count}) — read replies below")
    lines += [
        "",
        f"Open <{LOOKER_DB_334_URL}|Looker DB 334> for each person below.",
        "Check clear start/finish, proper lunch breaks, no unexplained gaps.",
        "React ✅ on each person's thread message once checked.",
        "Reply in this thread if you spot an issue (name + what you saw).",
        "",
        "Cut-off: 11:00 — reward time runs after that.",
    ]
    return "\n".join(lines)


# ── Public API ──────────────────────────────────────────────────────────────


def post_friday_check_messages(
    reward_friday: date,
    channel: str | None = None,
) -> dict:
    """Post the three parent messages with per-person thread replies.

    If a state file already exists for this reward_friday, returns it without
    re-posting (idempotent for the same week).

    `channel` overrides #reward-time-questions-cs — pass the dry-run channel
    when testing.
    """
    existing = _load_state(reward_friday)
    if existing:
        logger.info(
            f"State already exists for {reward_friday}, skipping re-post."
        )
        return existing

    channel = channel or SLACK_CHANNEL_REWARD_QUESTIONS
    week_label = _week_label(reward_friday)
    state = {
        "reward_friday": reward_friday.isoformat(),
        "week_label": week_label,
        "channel": channel,
        "teams": {},
        "jo_dm_sent": False,
    }

    for tl_name, info in TEAMS.items():
        members = info["members"]
        initial_status = {m: {"ticked": False} for m in members}
        parent_text = _render_parent(
            week_label, tl_name, info["user_id"], members, initial_status,
        )
        parent_ts = _slack_post(channel, parent_text)

        thread = {}
        for member in members:
            reply_ts = _slack_post(channel, member, thread_ts=parent_ts)
            thread[member] = {
                "thread_ts": reply_ts,
                "ticked": False,
            }

        state["teams"][tl_name] = {
            "user_id": info["user_id"],
            "parent_ts": parent_ts,
            "members": thread,
        }
        logger.info(f"Posted {tl_name}'s parent + {len(members)} thread replies")

    _save_state(state)
    return state


def refresh_check_state(reward_friday: date) -> dict | None:
    """Re-read Slack to update tick + flag status for each team.

    One conversations.replies call per parent thread; reactions come back
    inline so we don't need the reactions:read scope.

    Returns the refreshed state, or None if no state file exists yet.
    """
    state = _load_state(reward_friday)
    if state is None:
        return None

    channel = state["channel"]
    for tl_name, team in state["teams"].items():
        parent_ts = team["parent_ts"]
        msgs = _fetch_thread(channel, parent_ts)
        msg_by_ts = {m["ts"]: m for m in msgs}

        # Tick status per member, from inline reactions on each thread reply.
        # Any user's ✅ counts (see comment in original implementation).
        for member, m in team["members"].items():
            msg = msg_by_ts.get(m["thread_ts"])
            if msg is None:
                m["ticked"] = False
                continue
            tickers = [
                r.get("users", [])
                for r in msg.get("reactions", [])
                if r["name"] == TICK_EMOJI
            ]
            m["ticked"] = any(tickers)

        # Flags: thread replies beyond the bot's known per-person ones.
        bot_known_ts = {m["thread_ts"] for m in team["members"].values()}
        bot_known_ts.add(parent_ts)
        extras = [m for m in msgs if m["ts"] not in bot_known_ts]
        team["flag_replies"] = [
            {"user": x.get("user"), "text": x.get("text", ""), "ts": x.get("ts")}
            for x in extras
        ]

    _save_state(state)
    return state


def all_teams_complete(state: dict) -> bool:
    """True when every member of every team has been ticked."""
    for team in state["teams"].values():
        for m in team["members"].values():
            if not m["ticked"]:
                return False
    return True


def team_progress(state: dict) -> dict[str, dict]:
    """Per-team summary for banners / DMs."""
    out = {}
    for tl_name, team in state["teams"].items():
        members = team["members"]
        n = len(members)
        done = sum(1 for m in members.values() if m["ticked"])
        pending = [name for name, m in members.items() if not m["ticked"]]
        flag_count = len(team.get("flag_replies", []))
        out[tl_name] = {
            "done": done,
            "total": n,
            "complete": done == n,
            "pending": pending,
            "flag_count": flag_count,
        }
    return out


def _dm_jo(text: str) -> None:
    """Best-effort DM to Jo. Logs and continues on failure."""
    try:
        r = requests.post(
            f"{SLACK_API}/chat.postMessage",
            headers={
                "Authorization": f"Bearer {get_slack_token()}",
                "Content-Type": "application/json",
            },
            json={"channel": JO_USER_ID, "text": text},
            timeout=10,
        )
        data = r.json()
        if not data.get("ok"):
            logger.warning(f"DM to Jo failed: {data.get('error')}")
    except Exception:
        logger.exception("DM to Jo blew up")


def _render_completion_dm(state: dict) -> str:
    progress = team_progress(state)
    lines = [
        f"✅ Timeline checks complete — {state['week_label']}",
        "All three TLs have signed off. Reward time is unblocked.",
    ]
    flagged = [f"{tl} ({p['flag_count']})" for tl, p in progress.items() if p["flag_count"]]
    if flagged:
        lines.append(
            f"Heads up — issues raised by {', '.join(flagged)}. "
            "Check the threads before sending the reward post."
        )
    return "\n".join(lines)


def render_and_update_parents(state: dict) -> None:
    """Edit each parent message with the current rendered status."""
    channel = state["channel"]
    week_label = state["week_label"]
    for tl_name, team in state["teams"].items():
        member_status = {
            name: {"ticked": m["ticked"]}
            for name, m in team["members"].items()
        }
        flag_count = len(team.get("flag_replies", []))
        text = _render_parent(
            week_label,
            tl_name,
            team["user_id"],
            list(team["members"].keys()),
            member_status,
            flag_count=flag_count,
        )
        _slack_update(channel, team["parent_ts"], text)


def run_friday_loop(channel: str | None = None, end_time: time | None = None) -> None:
    """Friday entry point. Posts the parents at start, then refreshes every
    minute, updating parent statuses and DMing Jo the moment all three teams
    hit complete. Exits at end_time (default 11:30 local).
    """
    end_time = end_time or FRIDAY_LOOP_END
    friday = get_check_friday()
    logger.info(f"Friday loop starting for reward week starting {friday}")

    try:
        state = post_friday_check_messages(friday, channel=channel)
    except Exception:
        logger.exception("Initial post failed; aborting loop.")
        _dm_jo("❌ Timeline checks failed to post this morning — check the logs.")
        return

    while datetime.now().time() < end_time:
        try:
            state = refresh_check_state(friday)
            if state is None:
                logger.warning(f"State disappeared for {friday}; exiting loop.")
                break
            render_and_update_parents(state)
            if all_teams_complete(state) and not state.get("jo_dm_sent"):
                _dm_jo(_render_completion_dm(state))
                state["jo_dm_sent"] = True
                _save_state(state)
                logger.info("All complete — Jo DM sent; continuing to watch for late replies")
        except Exception:
            logger.exception("Tick failed — continuing")
        _time.sleep(FRIDAY_LOOP_INTERVAL_SEC)

    logger.info(f"Friday loop ended at {datetime.now().time()}")


# ── CLI for manual testing ──────────────────────────────────────────────────


def _cli():
    import argparse

    p = argparse.ArgumentParser(description="Timeline check post + read")
    p.add_argument(
        "command",
        choices=[
            "post-dry-run", "post-live",
            "refresh", "progress",
            "run-friday", "run-friday-dry-run",
        ],
        help=(
            "post-dry-run/post-live: post the three parents once. "
            "refresh/progress: re-read Slack and print state. "
            "run-friday[-dry-run]: launchd entrypoint — post then watch until 11:30."
        ),
    )
    p.add_argument(
        "--friday",
        help="Reward Friday (YYYY-MM-DD). Defaults to the last reward Friday.",
    )
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    friday = (
        date.fromisoformat(args.friday) if args.friday else get_check_friday()
    )
    print(f"Reward Friday: {friday}  ({_week_label(friday)})")

    if args.command == "post-dry-run":
        state = post_friday_check_messages(friday, channel=SLACK_CHANNEL_DRY_RUN)
        print(f"Posted to #dry-run-testing-jo. Teams: {list(state['teams'])}")
    elif args.command == "post-live":
        state = post_friday_check_messages(friday)
        print(
            f"Posted to #reward-time-questions-cs. "
            f"Teams: {list(state['teams'])}"
        )
    elif args.command == "refresh":
        state = refresh_check_state(friday)
        if state is None:
            print("No state file. Run post-dry-run or post-live first.")
            return
        render_and_update_parents(state)
        print("Refreshed and updated parent messages.")
    elif args.command == "progress":
        state = _load_state(friday)
        if state is None:
            print("No state file.")
            return
        for tl_name, p in team_progress(state).items():
            tag = "✅" if p["complete"] else f"{p['done']}/{p['total']}"
            flag = f"  ({p['flag_count']} issue(s) in thread)" if p["flag_count"] else ""
            print(f"  {tl_name}: {tag}{flag}")
        print(f"All complete: {all_teams_complete(state)}")
    elif args.command == "run-friday":
        run_friday_loop()
    elif args.command == "run-friday-dry-run":
        run_friday_loop(channel=SLACK_CHANNEL_DRY_RUN)


if __name__ == "__main__":
    _cli()
