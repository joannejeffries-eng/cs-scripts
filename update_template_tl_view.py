#!/usr/bin/env python3
"""
update_template_tl_view.py — bring the Reward Time Tracker template's TL View tab
up to the spreadsheet-only design.

What it does (idempotent — safe to re-run):
  1. Adds five per-day OVERRIDE columns R-V (Fri..Thu): a dropdown of
        ⭐ Force Stretch / ✅ Force Base / ❌ Force Miss   (blank = no override)
     so a TL/Jo can flip a single day's outcome.
  2. Rewrites the per-day Hit formulas (C/E/G/I/K) so an override WINS over the
     computed Base/Stretch/Miss — except an Off day, which always stays Off.
  3. Rewrites the weekly Reward formula (P) so all-base / all-stretch / pending
     are derived from the five Hit cells (which now honour overrides), still
     gated on the TL Timeline (N) + Quality (O) cells + the skip threshold.

The TL View is built from the Data tab the populator writes (rows 4+ contiguous).
This script discovers each agent row from column A (=Data!B{n}) so it adapts to
the roster automatically.

By default it targets the shared TEMPLATE so every FUTURE week-sheet inherits the
change. Pass --sheet <id> to run it against a scratch copy for verification.

Usage:
    python3 update_template_tl_view.py                 # the template
    python3 update_template_tl_view.py --sheet <ID>    # a scratch copy
    python3 update_template_tl_view.py --dry-run       # print, don't write
"""
from __future__ import annotations

import argparse
import re
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

TEMPLATE_ID = "1BXYvxKcrG_jm3WaKBmqxYIb5p0SCWc6_AG4RvpTFEM0"
CREDS_PATH = Path.home() / ".config/juno/claude-code/google-credentials.json"

# Per day: (tl_hit_col, override_col, data_role_col, data_basemet_col, data_stretchmet_col)
# TL View hit cells live at C/E/G/I/K; overrides go to the new R/S/T/U/V.
DAYS = [
    ("C", "R", "D", "H", "I"),     # Fri
    ("E", "S", "J", "N", "O"),     # Mon
    ("G", "T", "P", "T", "U"),     # Tue
    ("I", "U", "V", "Z", "AA"),    # Wed
    ("K", "V", "AB", "AF", "AG"),  # Thu
]
HIT_COLS = [d[0] for d in DAYS]        # C E G I K
OVERRIDE_COLS = [d[1] for d in DAYS]   # R S T U V
OVERRIDE_HEADERS = ["Fri ovr", "Mon ovr", "Tue ovr", "Wed ovr", "Thu ovr"]
OVERRIDE_VALUES = ["⭐ Force Stretch", "✅ Force Base", "❌ Force Miss"]

# Absence labels the Hit formula treats as "🌴 Off" (must match the populator's
# _ABSENCE_NORMALISATION output + the existing template formula).
OFF_LABELS = ["Holiday", "Annual leave", "Sick", "Unplanned absence",
              "Off", "Training", ""]


def _svc():
    creds = Credentials.from_authorized_user_file(str(CREDS_PATH))
    return build("sheets", "v4", credentials=creds)


def build_hit_formula(role_col, bmet_col, smet_col, ovr_cell, r):
    """Per-day Hit formula. Priority: Off > override > computed Base/Stretch/Miss.

    role_col/bmet_col/smet_col are Data-tab column letters; r is the Data row.
    ovr_cell is the same-sheet override cell, e.g. 'R5'.
    """
    role = f"Data!{role_col}{r}"
    bmet = f"Data!{bmet_col}{r}"
    smet = f"Data!{smet_col}{r}"
    off_test = ",".join(f'{role}="{lbl}"' for lbl in OFF_LABELS)
    return (
        f'=IF(OR({off_test}),"🌴 Off",'
        f'IF(ISNUMBER(SEARCH("Stretch",{ovr_cell})),"⭐ Stretch",'
        f'IF(ISNUMBER(SEARCH("Base",{ovr_cell})),"✅ Base",'
        f'IF(ISNUMBER(SEARCH("Miss",{ovr_cell})),"❌ Miss",'
        f'IF({bmet}="","⏳ Pending",'
        f'IF({smet}="Yes","⭐ Stretch",'
        f'IF({bmet}="Yes","✅ Base","❌ Miss")))))))'
    )


def build_reward_formula(t, r):
    """Weekly Reward formula, derived from the five Hit cells on row t.

    Hit cells (C/E/G/I/K) already reflect per-day overrides, so all-base /
    all-stretch / pending flow through automatically. Still gated on the TL
    Timeline (N) + Quality (O) cells and the pro-rated skip threshold.
    """
    def cnt(substr):
        return "+".join(f'COUNTIF({c}{t},"*{substr}*")' for c in HIT_COLS)

    n_off = cnt("Off")
    n_pend = cnt("Pending")
    n_miss = cnt("Miss")
    n_str = cnt("Stretch")
    worked = f"(5-({n_off}))"
    skips_over = f"L{t}>ROUND(50*Data!AM{r}/40,0)"
    timeline_fail = f'ISNUMBER(SEARCH("Fail",N{t}))'
    quality_fail = f'ISNUMBER(SEARCH("Fail",O{t}))'
    return (
        f'=IF({worked}=0,"— Off",'
        f'IF(({n_pend})>0,"⏳ Pending",'
        f'IF(OR(({n_miss})>0,{skips_over},{timeline_fail},{quality_fail}),"❌ None",'
        f'IF(OR(N{t}="",O{t}=""),"⏳ Pending",'
        f'IF(({n_str})={worked},"⭐ Stretch","✅ Base")))))'
    )


def discover_agent_rows(svc, sheet_id):
    """Return [(tl_row, data_row)] for every agent row (col A = '=Data!B{n}')."""
    res = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id, range="'TL View'!A1:A40",
        valueRenderOption="FORMULA",
    ).execute()
    out = []
    for i, row in enumerate(res.get("values", []), start=1):
        if not row:
            continue
        m = re.match(r"^=Data!B(\d+)$", str(row[0]).strip())
        if m:
            out.append((i, int(m.group(1))))
    return out


def tl_view_sheet_id(svc, sheet_id):
    meta = svc.spreadsheets().get(spreadsheetId=sheet_id).execute()
    for s in meta["sheets"]:
        if s["properties"]["title"] == "TL View":
            return s["properties"]["sheetId"], s["properties"]["gridProperties"]
    raise SystemExit("No 'TL View' tab found")


def run(sheet_id, dry_run=False):
    svc = _svc()
    tl_sid, grid = tl_view_sheet_id(svc, sheet_id)
    agent_rows = discover_agent_rows(svc, sheet_id)
    print(f"TL View sheetId={tl_sid}, {len(agent_rows)} agent rows: "
          f"{[t for t, _ in agent_rows]}")

    # --- 0. Ensure the grid has the override columns BEFORE writing to them ---
    if not dry_run and grid.get("columnCount", 0) < 22:
        svc.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [{"updateSheetProperties": {
                "properties": {"sheetId": tl_sid,
                               "gridProperties": {"columnCount": 22}},
                "fields": "gridProperties.columnCount",
            }}]},
        ).execute()
        print("Expanded TL View to 22 columns.")

    # --- 1. Formula value updates (header + per-row Hit + Reward) ---
    data = [{"range": "'TL View'!R3:V3", "values": [OVERRIDE_HEADERS]}]
    for t, r in agent_rows:
        for (hit_col, ovr_col, role_col, bmet_col, smet_col) in DAYS:
            data.append({
                "range": f"'TL View'!{hit_col}{t}",
                "values": [[build_hit_formula(role_col, bmet_col, smet_col,
                                               f"{ovr_col}{t}", r)]],
            })
        data.append({"range": f"'TL View'!P{t}",
                     "values": [[build_reward_formula(t, r)]]})

    if dry_run:
        sample_t, sample_r = agent_rows[0]
        print("\n--- sample Fri Hit (row", sample_t, ") ---")
        print(build_hit_formula("D", "H", "I", f"R{sample_t}", sample_r))
        print("\n--- sample Reward (row", sample_t, ") ---")
        print(build_reward_formula(sample_t, sample_r))
        print(f"\n[dry-run] would write {len(data)} value ranges + grid/validation/width requests")
        return

    svc.spreadsheets().values().batchUpdate(
        spreadsheetId=sheet_id,
        body={"valueInputOption": "USER_ENTERED", "data": data},
    ).execute()
    print(f"Wrote {len(data)} value ranges (headers + Hit + Reward).")

    # --- 2. Validation + width requests ---
    requests = []
    # Override dropdowns on R..V (cols 17-21) for each agent row, in contiguous blocks
    blocks = []
    rows_sorted = sorted(t for t, _ in agent_rows)
    start = prev = rows_sorted[0]
    for t in rows_sorted[1:]:
        if t == prev + 1:
            prev = t
        else:
            blocks.append((start, prev))
            start = prev = t
    blocks.append((start, prev))
    for (r0, r1) in blocks:
        requests.append({"setDataValidation": {
            "range": {"sheetId": tl_sid,
                      "startRowIndex": r0 - 1, "endRowIndex": r1,
                      "startColumnIndex": 17, "endColumnIndex": 22},
            "rule": {
                "condition": {"type": "ONE_OF_LIST",
                              "values": [{"userEnteredValue": v} for v in OVERRIDE_VALUES]},
                "showCustomUi": True, "strict": False,
            },
        }})
    # Column widths for R..V
    requests.append({"updateDimensionProperties": {
        "range": {"sheetId": tl_sid, "dimension": "COLUMNS",
                  "startIndex": 17, "endIndex": 22},
        "properties": {"pixelSize": 95}, "fields": "pixelSize",
    }})
    svc.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id, body={"requests": requests}).execute()
    print(f"Applied {len(requests)} grid/validation/width requests "
          f"({len(blocks)} validation block(s)).")
    print("Done.")


def _cli():
    p = argparse.ArgumentParser()
    p.add_argument("--sheet", default=TEMPLATE_ID,
                   help="Spreadsheet ID (default: the shared template).")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    run(args.sheet, dry_run=args.dry_run)


if __name__ == "__main__":
    _cli()
