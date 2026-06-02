"""
populate_reward_sheet.py — fills the weekly Reward Time Tracker sheet.

Reads the current reward week's saved state (week_data + moves + daily notes)
and populates a sheet copied from the TEMPLATE in the CS Reward Time folder:

  - Moves & Splits     one row per (person, day, role segment)
  - Data               per-person per-day Role/Base/Stretch/Actual/Met
  - Working Hours      per-person daily hours
  - Targets            role → base/stretch
  - Reward Day Schedule  day/block → phones + non-phones (from REWARD_DAYS)

TL View, TL Calculator and Report Generator tabs are left alone — those are
manual/formula-driven surfaces Jo edits directly.

Idempotent: same reward Friday → updates the existing sheet, doesn't duplicate.

Usage:
    python3 populate_reward_sheet.py             # current reward Friday
    python3 populate_reward_sheet.py --friday 2026-05-22
    python3 populate_reward_sheet.py --dm        # DM Jo the link after writing
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

import requests
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

import generate_rota as gr
import reward_time as rt
from compat import get_slack_token

# ── Config ──────────────────────────────────────────────────────────────────

FOLDER_ID = "1APzc9fEFz86Hc4oqeUYTHDRh-9LtzC4s"
TEMPLATE_ID = "1BXYvxKcrG_jm3WaKBmqxYIb5p0SCWc6_AG4RvpTFEM0"
JO_USER_ID = "U07KFSSCUNT"

LOG_DIR = Path.home() / ".juno/scheduled-tasks/reward-sheet"
LOG_FILE = LOG_DIR / "populate.log"

# Each tab → first data row (1-indexed). The template has 2-row headers on
# Data, 1-row everywhere else.
TAB_DATA_START_ROW = {
    "Moves & Splits": 2,
    "Working Hours": 2,
    "Targets": 2,
    "Reward Day Schedule": 2,
    "Data": 3,
    "Split Breakdown": 4,  # rows 1-3 are title + day headers + sub-headers
}

logger = logging.getLogger(__name__)


# ── Google API helpers ──────────────────────────────────────────────────────


def _creds() -> Credentials:
    return Credentials.from_authorized_user_file(
        str(Path.home() / ".config/juno/claude-code/google-credentials.json")
    )


def _drive():
    return build("drive", "v3", credentials=_creds())


def _sheets():
    return build("sheets", "v4", credentials=_creds())


def find_or_copy_sheet(reward_friday: date) -> str:
    """Return spreadsheetId for the week's sheet (creating it if missing)."""
    drive = _drive()
    week_name = (
        f"CS Reward Time Tracker — wc {reward_friday.strftime('%Y-%m-%d')}"
    )
    safe = week_name.replace("'", "\\'")
    res = drive.files().list(
        q=f"'{FOLDER_ID}' in parents and name='{safe}' and trashed=false",
        fields="files(id, name)",
    ).execute()
    if res["files"]:
        sid = res["files"][0]["id"]
        logger.info(f"Found existing sheet for {reward_friday}: {sid}")
        return sid
    copy = drive.files().copy(
        fileId=TEMPLATE_ID,
        body={"name": week_name, "parents": [FOLDER_ID]},
    ).execute()
    logger.info(f"Copied template → {copy['id']} ({week_name})")
    return copy["id"]


def clear_and_write(spreadsheet_id: str, tab_name: str, rows: list[list]) -> None:
    """Clear data rows on a tab (keep header) and write fresh rows."""
    sheets = _sheets()
    start_row = TAB_DATA_START_ROW.get(tab_name, 2)
    sheets.spreadsheets().values().clear(
        spreadsheetId=spreadsheet_id,
        range=f"'{tab_name}'!A{start_row}:Z",
    ).execute()
    if not rows:
        return
    sheets.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"'{tab_name}'!A{start_row}",
        valueInputOption="USER_ENTERED",
        body={"values": rows},
    ).execute()


# ── Row builders ────────────────────────────────────────────────────────────


def _yes_no(flag: bool, target: int) -> str:
    if not target:
        return ""
    return "Yes" if flag else "No"


# Map raw week_data absence labels to the canonical strings the TL View
# formula recognises as "🌴 Off". Anything not in this map passes through.
_ABSENCE_NORMALISATION = {
    "Annual leave":      "Annual leave",
    "Holiday":           "Annual leave",
    "AL":                "Annual leave",
    "Sick":              "Unplanned absence",
    "Sickness":          "Unplanned absence",
    "Illness":           "Unplanned absence",
    "Unplanned absence": "Unplanned absence",
    "Off":               "Off",
    "NWD":               "Off",
    "Non working day":   "Off",
    "Training":          "Training",
}


def _normalise_absence_label(role: str) -> str:
    """Pass-through unless `role` is a known absence label; then return the
    canonical version the TL View formula can interpret as Off."""
    if not role:
        return ""
    return _ABSENCE_NORMALISATION.get(role.strip(), role)


def build_moves_and_splits_rows(reward_friday: date) -> list[list]:
    """One row per (person, day, role segment)."""
    week_data = rt.load_week(reward_friday)
    if not week_data:
        return []
    rows = []
    dates = rt.get_weekday_dates(reward_friday)  # [Fri, Mon, Tue, Wed, Thu]
    for d in dates:
        for name in sorted(week_data):
            pw = week_data[name]
            dr = pw.days.get(d)
            if not dr or not dr.is_working:
                continue
            day_label = d.strftime("%a %d %b")
            if dr.segments:
                for s in dr.segments:
                    rows.append([
                        day_label, name, s.role, round(s.minutes / 60, 2),
                        s.actual, s.target_base, s.target_stretch,
                        _yes_no(s.met_base, s.target_base),
                        _yes_no(s.met_stretch, s.target_stretch),
                    ])
            else:
                rows.append([
                    day_label, name, dr.role, round(dr.shift_hours, 2),
                    dr.actual, dr.target_base, dr.target_stretch,
                    _yes_no(dr.met_base, dr.target_base),
                    _yes_no(dr.met_stretch, dr.target_stretch),
                ])
    return rows


def build_working_hours_rows(reward_friday: date) -> list[list]:
    """Per-person daily hours + schedule + manager."""
    rows = []
    for name in sorted(gr.DEFAULT_HOURS):
        per_day = gr.DEFAULT_HOURS[name]
        days = [per_day.get(i, 0) for i in range(5)]  # Mon..Fri
        total = sum(days)
        sw = gr.DEFAULT_SHIFTS.get(name)
        schedule = (
            f"{sw[0]}-{sw[1]}"
            if sw and isinstance(sw[0], (int, float))
            else ""
        )
        manager = ""
        for tl, members in rt.TL_TEAMS.items():
            if name in members:
                manager = tl
                break
        rows.append([name] + days + [total, schedule, manager])
    return rows


def build_targets_rows() -> list[list]:
    """Role → throughput base/stretch (full-time). Pulled from rt.ROLE_TARGETS,
    which maps the rota role string to (base, stretch, metric_name).
    """
    rows = []
    for role, (base, stretch, _metric) in rt.ROLE_TARGETS.items():
        if base == 0 and stretch == 0:
            continue  # split-role-only roles (training, appointment, reward time)
        rows.append([role, base, stretch])
    return rows


def build_reward_day_rows() -> list[list]:
    """Day/Block → Phones list, Non-Phones list. From REWARD_DAYS."""
    phones_team = set(rt.TL_TEAMS.get("Courtney", []))
    by_slot: dict = {}
    for name, (day, block) in rt.REWARD_DAYS.items():
        by_slot.setdefault((day, block), {"phones": [], "non_phones": []})
        bucket = "phones" if name in phones_team else "non_phones"
        by_slot[(day, block)][bucket].append(name)
    rows = []
    rows.append(["Monday", "ALL DAY", "NONE", "NONE", "No reward time"])
    for day in ["Tue", "Wed", "Thu", "Fri"]:
        for block in ["AM", "PM"]:
            entry = by_slot.get((day, block), {"phones": [], "non_phones": []})
            rows.append([
                {"Tue": "Tuesday", "Wed": "Wednesday",
                 "Thu": "Thursday", "Fri": "Friday"}[day],
                block,
                ", ".join(sorted(entry["phones"])) or "(None)",
                ", ".join(sorted(entry["non_phones"])) or "(None)",
                "",
            ])
    return rows


def _day_outcome(dr) -> str:
    """Day-level outcome label matching the TL View vocabulary."""
    if dr is None:
        return ""
    if not dr.is_working:
        role = _normalise_absence_label(dr.role or "Off")
        if role in ("Annual leave", "Unplanned absence", "Off", "Training", ""):
            return "🌴 Off"
        return "🌴 Off"
    # Walk segments (or treat the whole day as one segment) and ask:
    # did every targeted segment hit base / stretch?
    segs = dr.segments if dr.segments else [dr]
    base_ok = True
    stretch_ok = True
    any_target = False
    for s in segs:
        if not s.target_base and not s.target_stretch:
            continue  # split-role-only roles like Reward time / Training: no target
        any_target = True
        if not s.met_base:
            base_ok = False
        if not s.met_stretch:
            stretch_ok = False
    if not any_target:
        return "🌴 Off"
    if stretch_ok:
        return "⭐ Stretch"
    if base_ok:
        return "✅ Base"
    return "❌ Miss"


def build_split_breakdown_rows(reward_friday: date) -> list[list]:
    """Per-person row with per-day breakdown:
       Role 1 / Target / Base? / Stretch? / Role 2 / Target / Base? / Stretch? / Day outcome.

    For split days, role segments are sorted by hours descending so the
    primary role appears first. Rare 3+ segment days are truncated to 2.
    """
    week_data = rt.load_week(reward_friday)
    if not week_data:
        return []
    dates = rt.get_weekday_dates(reward_friday)
    rows = []
    for name in sorted(week_data):
        pw = week_data[name]
        weekly_hours = sum(
            dr.shift_hours for dr in pw.days.values() if dr.is_working
        )
        row = [name, round(weekly_hours, 2)]
        for d in dates:
            dr = pw.days.get(d)
            if dr is None:
                row += ["", "", "", "", "", "", "", "", ""]
                continue
            if not dr.is_working:
                label = _normalise_absence_label(dr.role or "Off")
                row += [label, "", "", "", "", "", "", "", "🌴 Off"]
                continue
            # Pick segments, sorted by hours descending
            segs = (sorted(dr.segments, key=lambda s: -s.minutes)
                    if dr.segments else [dr])
            # Build per-segment cell tuples
            def cell_quad(s) -> list:
                if not s.target_base and not s.target_stretch:
                    return [s.role, "—", "—", "—"]  # no-target role (Reward time etc.)
                target_str = f"{s.target_base}/{s.target_stretch}"
                base = _yes_no(s.met_base, s.target_base)
                stretch = _yes_no(s.met_stretch, s.target_stretch)
                return [s.role, target_str, base, stretch]
            quad1 = cell_quad(segs[0])
            quad2 = cell_quad(segs[1]) if len(segs) > 1 else ["", "", "", ""]
            row += quad1 + quad2 + [_day_outcome(dr)]
        rows.append(row)
    return rows


def build_data_rows(reward_friday: date) -> list[list]:
    """Per-person row matching Sam's Data tab shape (minimum viable).

    Cols: Week, Name, [for each day: Role, Base, Stretch, Actual, BaseMet, StretchMet], Skips
    """
    week_data = rt.load_week(reward_friday)
    if not week_data:
        return []
    dates = rt.get_weekday_dates(reward_friday)
    week_str = reward_friday.strftime("%Y-%m-%d")
    rows = []
    for name in sorted(week_data):
        pw = week_data[name]
        row = [week_str, name, ""]  # Primary Role left blank — split-aware
        for d in dates:
            dr = pw.days.get(d)
            if dr is None:
                row += ["", "", "", "", "", ""]
                continue
            if not dr.is_working:
                role = _normalise_absence_label(dr.role or "Off")
                row += [role, "", "", "", "", ""]
                continue
            if dr.segments:
                role = " / ".join(s.role for s in dr.segments)
            else:
                role = dr.role
            row += [
                role, dr.target_base, dr.target_stretch, dr.actual,
                _yes_no(dr.met_base, dr.target_base),
                _yes_no(dr.met_stretch, dr.target_stretch),
            ]
        row.append(pw.skips)
        rows.append(row)
    return rows


# ── Slack ───────────────────────────────────────────────────────────────────


def _slack_dm_jo(text: str) -> None:
    try:
        r = requests.post(
            "https://slack.com/api/chat.postMessage",
            headers={
                "Authorization": f"Bearer {get_slack_token()}",
                "Content-Type": "application/json",
            },
            json={"channel": JO_USER_ID, "text": text},
            timeout=10,
        )
        if not r.json().get("ok"):
            logger.warning(f"DM to Jo failed: {r.json().get('error')}")
    except Exception:
        logger.exception("DM to Jo blew up")


# ── Main ────────────────────────────────────────────────────────────────────


def autoresize_all_columns(spreadsheet_id: str) -> None:
    """Auto-resize every column on every tab so wide content is readable."""
    sheets = _sheets()
    ss = sheets.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    requests = []
    for s in ss["sheets"]:
        sid = s["properties"]["sheetId"]
        col_count = s["properties"]["gridProperties"].get("columnCount", 26)
        requests.append({
            "autoResizeDimensions": {
                "dimensions": {
                    "sheetId": sid, "dimension": "COLUMNS",
                    "startIndex": 0, "endIndex": col_count,
                }
            }
        })
    if requests:
        sheets.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id, body={"requests": requests}
        ).execute()


def run(reward_friday: date, dm: bool = False) -> str:
    sheet_id = find_or_copy_sheet(reward_friday)

    clear_and_write(sheet_id, "Moves & Splits",
                     build_moves_and_splits_rows(reward_friday))
    clear_and_write(sheet_id, "Working Hours",
                     build_working_hours_rows(reward_friday))
    clear_and_write(sheet_id, "Targets", build_targets_rows())
    clear_and_write(sheet_id, "Reward Day Schedule", build_reward_day_rows())
    clear_and_write(sheet_id, "Data", build_data_rows(reward_friday))
    clear_and_write(sheet_id, "Split Breakdown",
                     build_split_breakdown_rows(reward_friday))

    autoresize_all_columns(sheet_id)

    link = f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"
    logger.info(f"Sheet populated: {link}")

    if dm:
        week_label = reward_friday.strftime("%a %d %b")
        _slack_dm_jo(
            f"📊 Reward Time tracker populated for the week starting "
            f"*{week_label}* — {link}"
        )
    return link


def _cli():
    p = argparse.ArgumentParser(description="Populate the weekly Reward Time tracker.")
    p.add_argument(
        "--friday",
        help="Reward Friday (YYYY-MM-DD). Defaults to the current reward Friday.",
    )
    p.add_argument(
        "--dm", action="store_true",
        help="DM Jo the sheet link after writing.",
    )
    args = p.parse_args()

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s populate-reward-sheet %(levelname)s %(message)s",
        handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stdout)],
    )

    friday = (
        date.fromisoformat(args.friday)
        if args.friday
        else rt.get_reward_friday()
    )
    link = run(friday, dm=args.dm)
    print(f"Sheet: {link}")


if __name__ == "__main__":
    _cli()
