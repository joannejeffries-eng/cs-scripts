#!/usr/bin/env python3
"""
Re-apply pro-rata + target overrides to an existing CS Reward Time Tracker.

Use after the rota's Working Hours / Daily Notes / Overrides tabs have been
updated and you need the existing tracker to reflect those changes WITHOUT
losing TL inputs (Timeline Check, Quality Check, TL Notes) on the Data tab.

Usage:
    python3 recompute_tracker.py <spreadsheet_id> <friday_yyyy_mm_dd>

What it does:
    1. Re-reads the rota (Working Hours, Overrides, Daily Notes) for the week
    2. Resets the Working Hours tab on the existing sheet to current rota values
    3. Re-applies pro-rata (reducing hours for absences in Daily Notes)
    4. Re-applies individual baseline/stretch overrides (formulas)
    5. Re-applies standing rule overrides (within-1-of-stretch, ID skips)
    6. Posts a correction summary report to stdout

It does NOT touch:
    - Actuals (Phones / Triage / ICS / Chasing counts) — those came from the DB
    - TL inputs in the TL View tab (Timeline Check, Quality Check, TL Notes)
    - Skips
    - The pre-existing role assignments on the Data tab

If you need to update actuals or roles, regenerate from scratch using
fill_tracker.py instead.
"""
import sys
from datetime import datetime, timedelta
from collections import defaultdict

import gspread
from google.oauth2.credentials import Credentials

import fill_tracker as ft


def main():
    if len(sys.argv) != 3:
        print("Usage: python3 recompute_tracker.py <spreadsheet_id> <friday_yyyy_mm_dd>")
        sys.exit(1)

    spreadsheet_id = sys.argv[1]
    friday = datetime.strptime(sys.argv[2], '%Y-%m-%d').date()
    if friday.weekday() != 4:
        print(f"WARNING: {sys.argv[2]} is a {friday.strftime('%A')}, not a Friday")

    dates = ft.get_week_dates(friday)
    print(f"Recomputing tracker for w/c {friday}")

    # 1. Read rota inputs
    working_hours = ft.read_working_hours()
    overrides = ft.read_overrides()
    daily_notes = ft.read_daily_notes(friday, dates)
    roles = ft.read_rota(friday)

    if not working_hours:
        print("ERROR: Could not read Working Hours from rota — aborting.")
        sys.exit(2)

    # 2. Connect to the existing sheet
    creds = Credentials.from_authorized_user_file(str(ft.CREDS_PATH))
    gc = gspread.authorize(creds)
    ss = gc.open_by_key(spreadsheet_id)

    # 3. Reset the Working Hours tab and apply pro-rata
    print("Resetting Working Hours tab...")
    try:
        ws_wh = ss.worksheet('Working Hours')
    except gspread.WorksheetNotFound:
        print("ERROR: Working Hours tab not found on the target sheet.")
        sys.exit(3)

    # Read current agent->row mapping from the live sheet
    wh_rows_data = ws_wh.get_all_values()
    wh_agent_rows = {}
    for i, row in enumerate(wh_rows_data, start=1):
        if not row or not row[0].strip():
            continue
        name = row[0].strip()
        if name in ft.ALL_AGENTS:
            wh_agent_rows[name] = i

    # Build batch update: write each agent's per-day hours back to defaults from rota
    DAY_TO_COL_LETTER = {1: 'B', 2: 'C', 3: 'D', 4: 'E', 0: 'F'}  # day_idx -> col letter
    cells_to_update = []
    for agent, row in wh_agent_rows.items():
        per_day = working_hours.get(agent, {})
        for day_idx, col_letter in DAY_TO_COL_LETTER.items():
            cells_to_update.append({
                'range': f"{col_letter}{row}",
                'values': [[per_day.get(day_idx, 0)]],
            })
    if cells_to_update:
        ws_wh.batch_update(cells_to_update, value_input_option='USER_ENTERED')
    print(f"  Reset {len(cells_to_update)} cells in Working Hours")

    # 4. Compute pro-rata adjustments (in Python; we then write reduced hours to live sheet)
    keywords = overrides.get('keywords', ft.DEFAULT_PRO_RATA_KEYWORDS)

    daily_totals = defaultdict(float)
    daily_reasons = defaultdict(list)
    skipped = []
    for e in daily_notes:
        if not ft.is_pro_rata_entry(e['note'], e['role_str'], keywords):
            continue
        if ft.is_split_role_entry(e['note'], e['role_str']):
            skipped.append({**e, 'skip_reason': 'split-role'})
            continue
        if e['hours_lost'] is None:
            skipped.append({**e, 'skip_reason': 'unparseable_time'})
            continue
        if e['role_str'].lower() in ('annual leave', 'unplanned absence'):
            continue
        daily_totals[(e['agent'], e['day_idx'])] += e['hours_lost']
        daily_reasons[(e['agent'], e['day_idx'])].append(e)

    applied_pr = []
    pr_cells = []
    for (agent, day_idx), hours_lost in daily_totals.items():
        if agent not in wh_agent_rows:
            skipped.append({'agent': agent, 'day_idx': day_idx, 'skip_reason': 'agent_not_in_template'})
            continue
        row = wh_agent_rows[agent]
        col_letter = DAY_TO_COL_LETTER[day_idx]
        before = working_hours.get(agent, {}).get(day_idx, 0.0)
        after = max(0.0, round(before - hours_lost, 2))
        pr_cells.append({'range': f"{col_letter}{row}", 'values': [[after]]})
        factor = round(after / before, 2) if before > 0 else 0
        applied_pr.append({
            'agent': agent, 'day_idx': day_idx,
            'date': daily_reasons[(agent, day_idx)][0]['date'],
            'hours_before': before, 'hours_after': after, 'hours_lost': hours_lost,
            'factor': factor,
            'reasons': [e['note'] for e in daily_reasons[(agent, day_idx)]],
        })
    if pr_cells:
        ws_wh.batch_update(pr_cells, value_input_option='USER_ENTERED')
        # Apply yellow fill to those cells
        sheet_id = ws_wh._properties['sheetId']
        # Use Google Sheets API for formatting (gspread doesn't directly format)
        from googleapiclient.discovery import build
        sheets_service = build('sheets', 'v4', credentials=creds)
        requests = []
        for cell in pr_cells:
            rng = cell['range']  # e.g. 'B5'
            col_letter = ''.join(c for c in rng if c.isalpha())
            row_num = int(''.join(c for c in rng if c.isdigit()))
            col_idx = ord(col_letter.upper()) - ord('A')
            requests.append({
                'repeatCell': {
                    'range': {
                        'sheetId': sheet_id,
                        'startRowIndex': row_num - 1, 'endRowIndex': row_num,
                        'startColumnIndex': col_idx, 'endColumnIndex': col_idx + 1,
                    },
                    'cell': {'userEnteredFormat': {'backgroundColor': {
                        'red': 1.0, 'green': 0.949, 'blue': 0.8}}},
                    'fields': 'userEnteredFormat.backgroundColor',
                }
            })
        if requests:
            sheets_service.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id, body={'requests': requests}).execute()
    print(f"  Applied pro-rata to {len(applied_pr)} person-days")

    # 5. Re-apply individual overrides + standing overrides via Sheets API
    # Read Data tab to find agent rows
    ws_data = ss.worksheet('Data')
    data_rows = ws_data.get_all_values()
    data_agent_rows = {}
    for i, row in enumerate(data_rows, start=1):
        if len(row) > 1:
            name = row[1].strip()
            if name in ft.ALL_AGENTS:
                data_agent_rows[name] = i

    # Build wh_after for standing-rule comparisons
    wh_after = {a: dict(d) for a, d in working_hours.items()}
    for e in applied_pr:
        wh_after.setdefault(e['agent'], {})[e['day_idx']] = e['hours_after']

    # Individual overrides: write override formulas
    applied_ind = []
    formula_updates = []
    override_map = {(o['agent'], o['role']): o for o in overrides.get('individual', [])}
    for agent, row in data_agent_rows.items():
        for day_idx in range(5):
            role = roles.get(agent, {}).get(day_idx, '')
            if not role or role in ('Off', 'Holiday', 'Sick', 'Training'):
                continue
            ov = override_map.get((agent, role))
            if not ov:
                continue
            base_col = ft.INFO_COLS + 1 + day_idx * ft.CPD
            wh_lookup = (f"VLOOKUP($B{row},'Working Hours'!$A:$G,"
                         f"{ft.TEMPLATE_WH_DAY_COL[day_idx]},FALSE)")
            if ov['baseline'] is not None:
                cl = _col_letter(base_col + 1)
                formula = (f"=IFERROR(IF({wh_lookup}=0,\"\","
                           f"ROUND({ov['baseline']}*{wh_lookup}/8,0)),\"\")")
                formula_updates.append({'range': f"{cl}{row}", 'values': [[formula]]})
            if ov['stretch'] is not None:
                cl = _col_letter(base_col + 2)
                formula = (f"=IFERROR(IF({wh_lookup}=0,\"\","
                           f"ROUND({ov['stretch']}*{wh_lookup}/8,0)),\"\")")
                formula_updates.append({'range': f"{cl}{row}", 'values': [[formula]]})
            applied_ind.append({
                'agent': agent, 'day_idx': day_idx, 'role': role,
                'baseline': ov['baseline'], 'stretch': ov['stretch'], 'reason': ov['reason'],
            })
    if formula_updates:
        ws_data.batch_update(formula_updates, value_input_option='USER_ENTERED')
    print(f"  Applied {len(applied_ind)} individual override cell(s)")

    # Standing overrides: compute and write 'Yes' literals where applicable
    applied_std = []
    yes_updates = []
    id_skip_set = set()
    if overrides.get('standing', {}).get('id_skips_below_baseline'):
        for e in daily_notes:
            if ft.is_id_skip_entry(e['note']):
                id_skip_set.add((e['agent'], e['day_idx']))

    for agent, row in data_agent_rows.items():
        for day_idx in range(5):
            role = roles.get(agent, {}).get(day_idx, '')
            if not role or role in ('Off', 'Holiday', 'Sick', 'Training'):
                continue
            day_date = dates[day_idx]
            # Re-query actual from the sheet (don't re-pull DB)
            actual_col = ft.INFO_COLS + 1 + day_idx * ft.CPD + 3  # actual col
            try:
                actual = ws_data.cell(row, actual_col).value
                actual = int(actual) if actual not in (None, '') else None
            except (ValueError, TypeError):
                actual = None
            if actual is None:
                continue
            hrs = wh_after.get(agent, {}).get(day_idx, 0.0)
            if hrs <= 0:
                continue
            std = ft.STANDARD_TARGETS.get(role)
            if not std:
                continue
            ov = override_map.get((agent, role))
            base_full = ov['baseline'] if ov and ov.get('baseline') is not None else std['baseline']
            stretch_full = ov['stretch'] if ov and ov.get('stretch') is not None else std['stretch']
            baseline = round(base_full * hrs / 8)
            stretch = round(stretch_full * hrs / 8)

            base_col = ft.INFO_COLS + 1 + day_idx * ft.CPD
            bmet_col = base_col + 4
            smet_col = base_col + 5

            if overrides['standing'].get('within_1_of_stretch') and actual == stretch - 1:
                yes_updates.append({'range': f"{_col_letter(smet_col)}{row}", 'values': [['Yes']]})
                applied_std.append({'agent': agent, 'day_idx': day_idx,
                                    'rule': 'within_1_of_stretch',
                                    'actual': actual, 'stretch': stretch})

            if (overrides['standing'].get('id_skips_below_baseline')
                    and (agent, day_idx) in id_skip_set
                    and actual < baseline):
                yes_updates.append({'range': f"{_col_letter(bmet_col)}{row}", 'values': [['Yes']]})
                applied_std.append({'agent': agent, 'day_idx': day_idx,
                                    'rule': 'id_skips_below_baseline',
                                    'actual': actual, 'baseline': baseline})
    if yes_updates:
        ws_data.batch_update(yes_updates, value_input_option='USER_ENTERED')
    print(f"  Applied {len(applied_std)} standing override(s)")

    # 6. Validation + report
    warnings = ft.validate(applied_pr, skipped, applied_ind, applied_std,
                            daily_notes, keywords)
    ft.print_report(friday, applied_pr, skipped, applied_ind, applied_std, warnings)

    url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}"
    print(f"\nDone! Recomputed: {url}")


def _col_letter(idx_1based):
    """Convert 1-based column index to A1 letter(s)."""
    result = ''
    n = idx_1based
    while n > 0:
        n, rem = divmod(n - 1, 26)
        result = chr(65 + rem) + result
    return result


if __name__ == '__main__':
    main()
