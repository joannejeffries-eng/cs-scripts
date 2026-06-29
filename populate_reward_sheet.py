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
import refresh_daemon as rd
import reward_time as rt
from compat import get_slack_token

# ── Config ──────────────────────────────────────────────────────────────────

FOLDER_ID = "1APzc9fEFz86Hc4oqeUYTHDRh-9LtzC4s"
TEMPLATE_ID = "1BXYvxKcrG_jm3WaKBmqxYIb5p0SCWc6_AG4RvpTFEM0"
ROTA_SHEET_ID = "1CMSEZSb-4D4mO6iPb8tVSaAPsZT5KZst9VSXH4bpi0Y"
JO_USER_ID = "U07KFSSCUNT"

# Order Data + TL View rows so the 6 Core Phones land in the first 6
# Core Phones section rows. Wider Team follows alphabetically.
CORE_PHONES_ORDER = ["Harry", "Kate", "Becky", "Elida", "Fionn"]

LOG_DIR = Path.home() / ".juno/scheduled-tasks/reward-sheet"
LOG_FILE = LOG_DIR / "populate.log"

# Each tab → first data row (1-indexed). The template has 2-row headers on
# Data, 1-row everywhere else.
TAB_DATA_START_ROW = {
    "Moves & Splits": 2,
    "Working Hours": 2,
    "Targets": 2,
    "Reward Day Schedule": 2,
    # Data row 3 is a section header in Sam's template ("CORE PHONES"); the
    # TL View formulas reference Data!{col}4 onwards, so we start writing at
    # row 4 to keep the offsets aligned.
    "Data": 4,
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


def _get_sheet_id(sheets, spreadsheet_id: str, tab_name: str) -> int | None:
    ss = sheets.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    for s in ss["sheets"]:
        if s["properties"]["title"] == tab_name:
            return s["properties"]["sheetId"]
    return None


def clear_and_write(spreadsheet_id: str, tab_name: str, rows: list[list]) -> None:
    """Clear data rows on a tab (keep header) and write fresh rows.

    Clear range goes out to column BZ (78 cols) so Sam's reference-week
    values in the Data tab summary columns (AH-AY) get wiped. For the
    Data tab we ALSO clear from row 3 (one above the data start) to wipe
    any leftover values + the template's section-header backgrounds (pale
    green on rows 3 / "CORE PHONES" + row 9 / "WIDER TEAM") so freshly
    written rows don't inherit them.
    """
    sheets = _sheets()
    start_row = TAB_DATA_START_ROW.get(tab_name, 2)
    clear_start_row = 3 if tab_name == "Data" else start_row
    sheets.spreadsheets().values().clear(
        spreadsheetId=spreadsheet_id,
        range=f"'{tab_name}'!A{clear_start_row}:BZ",
    ).execute()

    if tab_name == "Data":
        # Reset cell formatting on Data rows 3-30 so the section-header
        # backgrounds carried over from Sam's template don't bleed into
        # our data rows. After clearing, re-apply the 0.0% number format
        # on the per-day archive cols (BB-BF) so raw decimals like 0.847
        # render as "84.7%".
        sheet_id = _get_sheet_id(sheets, spreadsheet_id, tab_name)
        if sheet_id is not None:
            sheets.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"requests": [
                    {"updateCells": {
                        "range": {"sheetId": sheet_id,
                                   "startRowIndex": 2, "endRowIndex": 30,
                                   "startColumnIndex": 0, "endColumnIndex": 78},
                        "fields": "userEnteredFormat",
                    }},
                    {"repeatCell": {
                        "range": {"sheetId": sheet_id,
                                   "startRowIndex": 2, "endRowIndex": 40,
                                   "startColumnIndex": 53, "endColumnIndex": 58},
                        "cell": {"userEnteredFormat": {
                            "numberFormat": {"type": "PERCENT", "pattern": "0%"},
                        }},
                        "fields": "userEnteredFormat.numberFormat",
                    }},
                ]},
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


def read_individual_overrides() -> list[dict]:
    """Read the rota's Overrides tab → list of {agent, role, baseline, stretch}.

    Mirrors fill_tracker.read_overrides() but only pulls the individual
    target overrides section (we don't need the standing rules or pro-rata
    keywords here — those are applied by reward_time.py before save).
    """
    try:
        gc = _gspread()
        ss = gc.open_by_key(ROTA_SHEET_ID)
        ws = ss.worksheet("Overrides")
    except Exception:
        logger.exception("Could not read Overrides tab; continuing with no overrides")
        return []
    out = []
    section = None
    for row in ws.get_all_values():
        if not row or all(c.strip() == "" for c in row):
            continue
        first = row[0].strip()
        if first == "INDIVIDUAL TARGET OVERRIDES":
            section = "header"
            continue
        if first.startswith("STANDING RULES"):
            break
        if section == "header" and first == "Name":
            section = "data"
            continue
        if section != "data":
            continue
        name = row[0].strip()
        role = row[1].strip() if len(row) > 1 else ""
        baseline_s = row[2].strip() if len(row) > 2 else ""
        stretch_s = row[3].strip() if len(row) > 3 else ""
        active = (row[5].strip().upper() if len(row) > 5 else "Y")
        if not name or active != "Y":
            continue
        try:
            baseline = int(baseline_s) if baseline_s else None
            stretch = int(stretch_s) if stretch_s else None
        except ValueError:
            logger.warning(f"Bad override values for {name}: base={baseline_s!r} stretch={stretch_s!r}")
            continue
        out.append({"agent": name, "role": role,
                     "baseline": baseline, "stretch": stretch})
    return out


def _gspread():
    """Lazy gspread client (avoids loading streamlit imports at top of file)."""
    from rota_app import _cached_gspread
    return _cached_gspread()


def _apply_override(name: str, role: str, shift_hours: float,
                     target_base: int, target_stretch: int,
                     overrides: list[dict]) -> tuple[int, int]:
    """Apply individual target override if (name, role) matches one in the
    Overrides tab. Override values are full-time numbers; we pro-rate them
    against `shift_hours / 8` so a part-timer's override scales with their
    contracted hours that day.
    """
    for ov in overrides:
        if ov["agent"] != name:
            continue
        ov_role = ov["role"]
        if not (role == ov_role or role.startswith(ov_role)):
            continue
        ratio = (shift_hours / 8.0) if shift_hours else 1.0
        if ov["baseline"] is not None:
            target_base = round(ov["baseline"] * ratio)
        if ov["stretch"] is not None:
            target_stretch = round(ov["stretch"] * ratio)
        return target_base, target_stretch
    return target_base, target_stretch


# Loaded once per populator run from read_individual_overrides().
_OVERRIDES: list[dict] = []


def _effective_targets_and_met(dr_or_seg, name: str) -> tuple[int, int, bool, bool]:
    """For a DayResult OR RoleSegment, return:
        (effective_base, effective_stretch, met_base, met_stretch)

    - Individual target override applied (pro-rated by shift hours) from
      the rota's Overrides tab — e.g. Kate's Phones target is 61/70 vs
      the team default 72/82.
    - Met flags use rounded archive % for triage roles so '85% archive'
      doesn't sit next to 'Base Met? = No' for a 0.847-raw day.
    """
    import math as _math
    role = (dr_or_seg.role or "")
    base = dr_or_seg.target_base
    stretch = dr_or_seg.target_stretch
    # Shift hours: DayResult has .shift_hours; RoleSegment has .minutes.
    if hasattr(dr_or_seg, "minutes") and not hasattr(dr_or_seg, "shift_hours"):
        shift_h = dr_or_seg.minutes / 60.0
    elif hasattr(dr_or_seg, "shift_hours"):
        shift_h = dr_or_seg.shift_hours
    else:
        shift_h = 8.0

    base, stretch = _apply_override(name, role, shift_h, base, stretch, _OVERRIDES)

    is_triage = "triage" in role.lower()
    base_hit = bool(base) and dr_or_seg.actual >= base
    stretch_hit = bool(stretch) and dr_or_seg.actual >= stretch
    if is_triage and dr_or_seg.archive_ratio:
        rounded_pct = _math.floor(dr_or_seg.archive_ratio * 100 + 0.5)
        base_hit = base_hit and rounded_pct >= 85
        stretch_hit = stretch_hit and rounded_pct >= 87
    return int(base or 0), int(stretch or 0), bool(base_hit), bool(stretch_hit)


def _met_with_rounded_archive(dr_or_seg, name: str = "") -> tuple[bool, bool]:
    """Backwards-compat wrapper — drops the targets, returns only met flags."""
    _b, _s, mb, ms = _effective_targets_and_met(dr_or_seg, name)
    return mb, ms


def _day_effective_targets_and_met(dr, name: str) -> tuple[int, int, bool, bool]:
    """Day-level (effective_base, effective_stretch, met_base, met_stretch).

    For split-role days the override is resolved per *segment*, then summed —
    the rota Overrides tab keys on a segment's own role (e.g. "Inbound phones",
    which matches "Inbound phones + Webchat") whereas the combined day label
    ("Phones / Reward") matches nothing, so resolving at day level silently
    drops the override. This keeps the Data tab — and the formula-driven TL
    View that reads it — in step with the Moves & Splits and Split Breakdown
    tabs, which already resolve per segment.
    """
    if not dr.segments:
        return _effective_targets_and_met(dr, name)
    eff_base = eff_stretch = 0
    base_ok = stretch_ok = True
    any_target = False
    for s in dr.segments:
        if not s.target_base and not s.target_stretch:
            continue  # no-target role (Reward time / Training / Appointment)
        any_target = True
        eb, es, mb, ms = _effective_targets_and_met(s, name)
        eff_base += eb
        eff_stretch += es
        if not mb:
            base_ok = False
        if not ms:
            stretch_ok = False
    if not any_target:
        return 0, 0, False, False
    return eff_base, eff_stretch, bool(base_ok), bool(stretch_ok)


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
    name_order = _ordered_names(week_data)
    for d in dates:
        for name in name_order:
            pw = week_data[name]
            dr = pw.days.get(d)
            if not dr or not dr.is_working:
                continue
            day_label = d.strftime("%a %d %b")
            if dr.segments:
                for s in dr.segments:
                    eb, es, mb, ms = _effective_targets_and_met(s, name)
                    rows.append([
                        day_label, name, s.role, round(s.minutes / 60, 2),
                        s.actual, eb, es,
                        _yes_no(mb, eb),
                        _yes_no(ms, es),
                    ])
            else:
                eb, es, mb, ms = _effective_targets_and_met(dr, name)
                rows.append([
                    day_label, name, dr.role, round(dr.shift_hours, 2),
                    dr.actual, eb, es,
                    _yes_no(mb, eb),
                    _yes_no(ms, es),
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


def _day_outcome(dr, name: str = "") -> str:
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
        mb, ms = _met_with_rounded_archive(s, name)
        if not mb:
            base_ok = False
        if not ms:
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
    for name in _ordered_names(week_data):
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
                eb, es, mb, ms = _effective_targets_and_met(s, name)
                target_str = f"{eb}/{es}"
                base = _yes_no(mb, eb)
                stretch = _yes_no(ms, es)
                return [s.role, target_str, base, stretch]
            quad1 = cell_quad(segs[0])
            quad2 = cell_quad(segs[1]) if len(segs) > 1 else ["", "", "", ""]
            row += quad1 + quad2 + [_day_outcome(dr, name)]
        rows.append(row)
    return rows


def _ordered_names(week_data: dict) -> list:
    """Core Phones first (in CORE_PHONES_ORDER), then everyone else
    alphabetically. Skips names missing from this week's data."""
    core = [n for n in CORE_PHONES_ORDER if n in week_data]
    others = sorted(n for n in week_data if n not in CORE_PHONES_ORDER)
    return core + others


def build_data_rows(reward_friday: date) -> list[list]:
    """Per-person row matching Sam's Data tab shape.

    Columns (matching the template):
      A  Week starting
      B  Name
      C  Primary role
      D-I  Friday    (Role / Base / Stretch / Actual / BaseMet / StretchMet)
      J-O  Monday
      P-U  Tuesday
      V-AA Wednesday
      AB-AG Thursday
      AH  Days Worked
      AI  Baselines Hit
      AJ  All Baselines Met?
      AK  Stretch Days Hit
      AL  Full Week Stretch?
      AM  Hours Worked This Week  ← key for TLs
      AN  Weekly Skips
      AO  Skips OK?
      AP  Quality Check     (manual)
      AQ  Team Queues OK?   (manual)
      AR  Reward Eligible?  (manual)
      AS  Stretch Bonus?
      AT-AV  Reward Hours (leave blank — driven by TL Calculator)
      AW  Reward Block
    """
    week_data = rt.load_week(reward_friday)
    if not week_data:
        return []
    dates = rt.get_weekday_dates(reward_friday)
    # UK slash format matches Sam's template + avoids inconsistent
    # auto-parsing where Sheets reads some cells as dates and others as
    # raw strings (which was making Fionn's row look different).
    week_str = reward_friday.strftime("%d/%m/%Y")
    rows = []
    for name in _ordered_names(week_data):
        pw = week_data[name]
        row = [week_str, name, ""]  # A, B, C
        # Per-day blocks D-AG
        base_hits = stretch_hits = 0
        days_worked = 0
        for d in dates:
            dr = pw.days.get(d)
            if dr is None:
                row += ["", "", "", "", "", ""]
                continue
            if not dr.is_working:
                role = _normalise_absence_label(dr.role or "Off")
                row += [role, "", "", "", "", ""]
                continue
            days_worked += 1
            if dr.segments:
                role = " / ".join(s.role for s in dr.segments)
            else:
                role = dr.role
            eff_base, eff_stretch, met_base_eff, met_stretch_eff = (
                _day_effective_targets_and_met(dr, name)
            )
            if met_base_eff and eff_base:
                base_hits += 1
            if met_stretch_eff and eff_stretch:
                stretch_hits += 1
            row += [
                role, eff_base, eff_stretch, dr.actual,
                _yes_no(met_base_eff, eff_base),
                _yes_no(met_stretch_eff, eff_stretch),
            ]
        # Summary AH..AW
        hours_worked = sum(
            dr.shift_hours for dr in pw.days.values() if dr.is_working
        )
        reward_block = rt.REWARD_DAYS.get(name)
        _DAY_FULL = {"Mon": "Monday", "Tue": "Tuesday", "Wed": "Wednesday",
                     "Thu": "Thursday", "Fri": "Friday"}
        reward_block_str = (
            f"{_DAY_FULL.get(reward_block[0], reward_block[0])} {reward_block[1]}"
            if reward_block else ""
        )
        skips_ok = "Yes" if pw.skips <= 4 else "No"  # rough threshold; TL can override
        row += [
            days_worked,                                       # AH
            base_hits,                                         # AI
            "Yes" if days_worked and base_hits == days_worked else "No",   # AJ
            stretch_hits,                                      # AK
            "Yes" if days_worked and stretch_hits == days_worked else "No",  # AL
            round(hours_worked, 2),                            # AM
            pw.skips,                                          # AN
            skips_ok,                                          # AO
            "",                                                # AP Quality (manual)
            "",                                                # AQ Team Queues (manual)
            "",                                                # AR Reward Eligible (manual)
            "",                                                # AS Stretch Bonus
            "",                                                # AT Base Reward Hours
            "",                                                # AU Stretch Hours
            "",                                                # AV Total Reward Hours
            reward_block_str,                                  # AW
        ]
        # Pad past template's TL Notes / ChatGPT / Archive count/total cells.
        row += ["", "", "", ""]                                # AX, AY, AZ, BA
        # Per-day archive % so a triage miss caused by archive ratio (e.g.
        # Tara Mon 01 Jun: hit target with 155, but archive ratio 84.7% < 85%
        # threshold) is visible without spelunking the raw data.
        for d in dates:                                        # BB..BF
            dr = pw.days.get(d)
            if dr and dr.is_working and dr.archive_ratio:
                # Raw decimal — paired with a 0% column format so the
                # display rounds to whole percentages. Note: 0.847
                # renders as 85% (standard rounding) but the threshold
                # check uses the raw decimal, so it still misses the 85%
                # base threshold. The Base Met? cell on the same row is
                # the source of truth for pass/fail.
                row.append(dr.archive_ratio)
            else:
                row.append("")
        rows.append(row)
    return rows


# Full-name → first-name mapping for TL Calculator updates (Sam's tab uses
# full names whereas week_data keys are first names). Mirrors rt.DB_NAMES.
def _full_name_to_first(name_full: str) -> str | None:
    """Reverse lookup full name → first name used in week_data."""
    for first, full in rt.DB_NAMES.items():
        if full == name_full:
            return first
    # Already a first name (e.g. 'Harry')
    if name_full in rt.ALL_AGENTS:
        return name_full
    return None


def update_tl_calculator_weekly_hours(spreadsheet_id: str, reward_friday: date) -> None:
    """Walk the TL Calculator's per-person rows and refresh column C
    (Weekly Hours) so the Base/Stretch reward formulas pro-rate against
    this week's actual hours worked.
    """
    week_data = rt.load_week(reward_friday)
    if not week_data:
        return
    sheets = _sheets()
    res = sheets.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range="'TL Calculator'!A1:C60",
    ).execute()
    rows = res.get("values", [])
    updates = []
    for r_idx, row in enumerate(rows):
        if len(row) < 2 or not row[0] or not row[1]:
            continue
        # Data rows have a Manager in col B (col A is the agent's full name).
        # Section header rows have only col A populated.
        name_full = row[0].strip()
        first = _full_name_to_first(name_full)
        if first is None or first not in week_data:
            continue
        pw = week_data[first]
        hours_worked = sum(
            dr.shift_hours for dr in pw.days.values() if dr.is_working
        )
        cell = f"'TL Calculator'!C{r_idx + 1}"
        updates.append({"range": cell, "values": [[round(hours_worked, 2)]]})
    if updates:
        sheets.spreadsheets().values().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"valueInputOption": "USER_ENTERED", "data": updates},
        ).execute()


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


def update_tl_view_week_label(spreadsheet_id: str, reward_friday: date) -> None:
    """Overwrite the TL View header so it always reads
    'Week beginning: DD/MM/YYYY' for the populated reward Friday."""
    sheets = _sheets()
    sheets.spreadsheets().values().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={
            "valueInputOption": "USER_ENTERED",
            "data": [
                {"range": "'TL View'!A2", "values": [["Week beginning:"]]},
                {"range": "'TL View'!B2",
                 "values": [[reward_friday.strftime("%d/%m/%Y")]]},
            ],
        },
    ).execute()


def prefill_quality_timeline_suggestions(spreadsheet_id: str,
                                         reward_friday: date) -> int:
    """Pre-fill the TL View Timeline (N) + Quality (O) cells with the
    auto-computed suggestions from saved week state, WITHOUT clobbering
    anything a TL has already entered.

    Suggestions come from `reward_time.run_quality_timeline_checks` (the
    Thursday cron), stored on each PersonWeek as `timeline_suggested` /
    `quality_suggested` (True / False / None). We only write into a cell that
    is currently BLANK — once a TL (or this pre-fill) has put a value there,
    it's left alone, so a TL's decision is never overwritten. Returns the
    number of cells written.
    """
    week_data = rt.load_week(reward_friday)
    if not week_data:
        return 0
    sheets = _sheets()
    # Col A resolves =Data!B{r} to the actual names; read alongside the
    # current Timeline/Quality cells so we can align by row and skip non-blank.
    res = sheets.spreadsheets().values().batchGet(
        spreadsheetId=spreadsheet_id,
        ranges=["'TL View'!A4:A30", "'TL View'!N4:O30"],
    ).execute()
    names_block = res["valueRanges"][0].get("values", [])
    no_block = res["valueRanges"][1].get("values", [])
    SUG = {True: "✅ Pass", False: "❌ Fail"}
    updates = []
    for i, name_row in enumerate(names_block):
        name = name_row[0].strip() if name_row else ""
        if not name or name not in week_data:
            continue  # section headers / strangers
        pw = week_data[name]
        if pw.days_worked == 0:
            continue
        no = no_block[i] if i < len(no_block) else []
        n_cur = no[0].strip() if len(no) > 0 and no[0] else ""
        o_cur = no[1].strip() if len(no) > 1 and no[1] else ""
        row_num = 4 + i  # A4 is row 4
        if not n_cur and pw.timeline_suggested in (True, False):
            updates.append({"range": f"'TL View'!N{row_num}",
                            "values": [[SUG[pw.timeline_suggested]]]})
        if not o_cur and pw.quality_suggested in (True, False):
            updates.append({"range": f"'TL View'!O{row_num}",
                            "values": [[SUG[pw.quality_suggested]]]})
    if updates:
        sheets.spreadsheets().values().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"valueInputOption": "USER_ENTERED", "data": updates},
        ).execute()
    logger.info(f"Pre-filled {len(updates)} quality/timeline suggestion cell(s).")
    return len(updates)


def run(reward_friday: date, dm: bool = False) -> str:
    # Pull fresh actuals for the current reward week before reading saved
    # state — keeps ad-hoc / mid-week runs in sync with whatever happened
    # since the last 06/11/13:30/15:30 daily-actuals cron. refresh_now()
    # only refreshes the live reward week; calls for historical weeks are
    # no-ops on data freshness, so guard.
    if reward_friday == rt.get_reward_friday():
        try:
            rd.refresh_now()
        except Exception:
            logger.exception("refresh_now failed; continuing with saved state")

    # Load individual target overrides from the rota's Overrides tab.
    # Cached for this run; row builders read via the _OVERRIDES module var.
    global _OVERRIDES
    _OVERRIDES = read_individual_overrides()
    if _OVERRIDES:
        label = ", ".join(f"{o['agent']}/{o['role']}" for o in _OVERRIDES)
        logger.info(
            f"Loaded {len(_OVERRIDES)} individual target override(s): {label}"
        )

    sheet_id = find_or_copy_sheet(reward_friday)

    update_tl_view_week_label(sheet_id, reward_friday)
    update_tl_calculator_weekly_hours(sheet_id, reward_friday)

    clear_and_write(sheet_id, "Moves & Splits",
                     build_moves_and_splits_rows(reward_friday))
    clear_and_write(sheet_id, "Working Hours",
                     build_working_hours_rows(reward_friday))
    clear_and_write(sheet_id, "Targets", build_targets_rows())
    clear_and_write(sheet_id, "Reward Day Schedule", build_reward_day_rows())
    clear_and_write(sheet_id, "Data", build_data_rows(reward_friday))
    clear_and_write(sheet_id, "Split Breakdown",
                     build_split_breakdown_rows(reward_friday))

    # Pre-fill the TL Timeline/Quality suggestion cells from saved state
    # (set by the Thursday checks). No-clobber: only blank cells are filled,
    # so a TL's own entry is never overwritten. Safe mid-week — suggestions
    # are None until the Thursday checks run, so this is a no-op before then.
    try:
        prefill_quality_timeline_suggestions(sheet_id, reward_friday)
    except Exception:
        logger.exception("prefill_quality_timeline_suggestions failed; continuing")

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
