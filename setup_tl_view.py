#!/usr/bin/env python3
"""
Add the TL View tab (+ TL Calculator) to a generated CS Reward Time Tracker Google Sheet.

Usage:
    python3 setup_tl_view.py <spreadsheet_id>

Creates the TL View tab with formulas referencing the Data tab, adds conditional
formatting, dropdowns, and hides the Data tab. Also creates the TL Calculator tab.
"""
import sys
from pathlib import Path

CREDS_PATH = Path.home() / '.config/juno/claude-code/google-credentials.json'

# Agent layout matches fill_tracker.py
# Data tab rows: Core Phones start at row 4 (1-indexed), Wider Team at row 10
# (row 9 is the "WIDER TEAM" section header)
CORE_PHONES_DATA_ROWS = [4, 5, 6, 7, 8]      # Becky, Elida, Fionn, Jade, Kate
WIDER_TEAM_DATA_ROWS = list(range(10, 24))     # Charne..Tara (14 agents, rows 10-23, incl. Lucy, Harry, Roseanne)

# TL View rows
# Row 1: title, Row 2: week date, Row 3: headers (frozen)
# Row 4: "Core Phones" section header
# Rows 5-9: Core Phones agents
# Row 10: "Wider Team" section header
# Rows 11-24: Wider Team agents (14 agents incl. Lucy, Harry, Roseanne)
CORE_TL_ROWS = list(range(5, 10))     # 5..9
WIDER_TL_ROWS = list(range(11, 25))   # 11..24

# Day config: (role_col, baselineMet_col, stretchMet_col) in Data tab (0-indexed)
# Fri D-I (3-8), Mon J-O (9-14), Tue P-U (15-20), Wed V-AA (21-26), Thu AB-AG (27-32)
DAYS = [
    ('Fri', 3, 7, 8),    # role=D(3), baselineMet=H(7), stretchMet=I(8)
    ('Mon', 9, 13, 14),   # role=J(9), baselineMet=N(13), stretchMet=O(14)
    ('Tue', 15, 19, 20),  # role=P(15), baselineMet=T(19), stretchMet=U(20)
    ('Wed', 21, 25, 26),  # role=V(21), baselineMet=Z(25), stretchMet=AA(26)
    ('Thu', 27, 31, 32),  # role=AB(27), baselineMet=AF(31), stretchMet=AG(32)
]

SKIPS_DATA_COL = 39  # Column AN (0-indexed = 39) — weekly skips in Data tab

# TL Calculator team structure
TL_CALC_TEAMS = {
    'Courtney Elijah': [
        ('Fionn Burrows', 40), ('Kate O\'Neill', 40), ('Becky Smith', 40),
        ('Jade Regent', 40), ('Elida Gizli', 40), ('Harriet Clifton-Sprigg', 40),
        ('Harry McNicholas', 40),
    ],
    'Yasmin Aly': [
        ('Tara Dunkley', 22), ('Sophie Maloney', 30),
        ('Noemi Sip', 40), ('Lizzie Williamson', 30), ('Kirsty Rowley', 40),
        ('Roseanne Brooks-Brown', 40),
    ],
    'Jessica Jackson': [
        ('Cris Macagi', 40), ('Clare Brown', 32),
        ('Erika Frolova', 40), ('Lucy Riordan', 40), ('Maisha Begum', 40),
    ],
}


def col_letter(idx):
    """Convert 0-based column index to A1 notation letter(s)."""
    result = ''
    idx += 1
    while idx > 0:
        idx, rem = divmod(idx - 1, 26)
        result = chr(65 + rem) + result
    return result


def build_hit_formula(tl_row, data_row, role_col, bmet_col, smet_col):
    """Build the Hit column formula for a TL View cell.

    Data tab's BaselineMet/StretchMet columns return "Yes"/"No" strings
    (or blank for Off/Holiday/Sick/Training days).
    """
    rc = col_letter(role_col)
    bc = col_letter(bmet_col)
    sc = col_letter(smet_col)
    return (
        f'=IF(OR(Data!{rc}{data_row}="Holiday",Data!{rc}{data_row}="Sick",'
        f'Data!{rc}{data_row}="Off",Data!{rc}{data_row}="Training",'
        f'Data!{rc}{data_row}=""),"🌴 Off",'
        f'IF(Data!{bc}{data_row}="","⏳ Pending",'
        f'IF(Data!{sc}{data_row}="Yes","⭐ Stretch",'
        f'IF(Data!{bc}{data_row}="Yes","✅ Base","❌ Miss"))))'
    )


def build_reward_formula(data_row, tl_row):
    """Build the weekly Reward column formula for a TL View cell.

    Reward is conditional on ALL of:
      - Hit baseline on every worked day        (Data!AJ = "Yes")
      - Skips within threshold                  (TL View!L <= 50 * worked_hours / 40)
      - Timeline Check passed by TL             (TL View!M does not contain "Fail")
      - Quality Check passed by TL              (TL View!N does not contain "Fail")

    Returns one of:
      "— Off"       — no days worked (all Off/Holiday/Sick/Training)
      "⏳ Pending"  — actuals still incoming OR TL hasn't filled Timeline/Quality yet
      "❌ None"    — failed at least one criterion (baseline, skips, timeline, quality)
      "✅ Base"    — passed everything at baseline level
      "⭐ Stretch"  — passed everything at stretch level (full-week stretch)

    Data tab columns used:
      AH = Days Worked      AJ = All Baselines Met?    AL = Full Week Stretch?
      AM = Hours Worked This Week (skip threshold pro-rata base)
      H/N/T/Z/AF = per-day Baseline Met?
    TL View columns used (same row as this Reward cell):
      L = Weekly Skips    (plain editable number — TL can override)
      N = Timeline Check  (Pass/Fail/N/A dropdown)
      O = Quality Check   (Pass/Fail/N/A dropdown)
    """
    r = data_row
    t = tl_row
    # days_assessed: count of baseline-met cells set to "Yes"/"No"
    days_assessed = (
        f'COUNTIF(Data!H{r},"Yes")+COUNTIF(Data!H{r},"No")'
        f'+COUNTIF(Data!N{r},"Yes")+COUNTIF(Data!N{r},"No")'
        f'+COUNTIF(Data!T{r},"Yes")+COUNTIF(Data!T{r},"No")'
        f'+COUNTIF(Data!Z{r},"Yes")+COUNTIF(Data!Z{r},"No")'
        f'+COUNTIF(Data!AF{r},"Yes")+COUNTIF(Data!AF{r},"No")'
    )
    # Skip threshold: 50 skips per 40 actually-worked hours, pro-rated.
    # Computed inline against TL View!L (which is plain/editable) so manual
    # overrides take effect immediately.
    skips_over = f'L{t}>ROUND(50*Data!AM{r}/40,0)'
    # Dropdown values on TL View!N/O are "✅ Pass" / "❌ Fail" / "⏸️ N/A".
    # Use ISNUMBER(SEARCH(...)) so a future tweak to the dropdown emoji/spacing
    # doesn't silently break the match.
    timeline_fail = f'ISNUMBER(SEARCH("Fail",N{t}))'
    quality_fail  = f'ISNUMBER(SEARCH("Fail",O{t}))'
    # Order matters:
    # 1. No days worked → Off
    # 2. Actuals still missing → Pending (regardless of TL fills)
    # 3. Definite fails (baseline missed, skips over, TL marked Fail) → None
    # 4. TL hasn't filled Timeline/Quality yet → Pending
    # 5. Otherwise pass → Stretch or Base
    return (
        f'=IF(Data!AH{r}=0,"— Off",'
        f'IF({days_assessed}<Data!AH{r},"⏳ Pending",'
        f'IF(OR(Data!AJ{r}="No",{skips_over},{timeline_fail},{quality_fail}),"❌ None",'
        f'IF(OR(N{t}="",O{t}=""),"⏳ Pending",'
        f'IF(Data!AL{r}="Yes","⭐ Stretch","✅ Base")))))'
    )


def build_archive_pct_formula(data_row):
    """Build the Archive % formula for a TL View cell.

    Shows the weekly email archive ratio: archived / (archived + escalated).
    Data tab columns: AZ = emails archived, BA = emails escalated.
    Displays "—" if no triage emails were processed that week.
    """
    r = data_row
    return (
        f'=IF(Data!AZ{r}+Data!BA{r}=0,"—",'
        f'TEXT(Data!AZ{r}/(Data!AZ{r}+Data!BA{r}),"0%"))'
    )


def get_sheets_service():
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    creds = Credentials.from_authorized_user_file(str(CREDS_PATH))
    return build('sheets', 'v4', credentials=creds)


def create_tl_view(service, spreadsheet_id):
    """Create the TL View tab with all formulas, formatting, and dropdowns.

    Layout (17 columns):
      A: Name
      B-C, D-E, F-G, H-I, J-K: (Role, Hit) per day Fri..Thu
      L: Weekly Skips
      M: Archive % (auto — weekly email archive ratio)
      N: Timeline Check (weekly)
      O: Quality Check (weekly)
      P: Reward (auto — earned at base / stretch / none)
      Q: TL Notes
    """

    NUM_COLS = 17
    # Column index references:
    HIT_COL_INDICES = [2, 4, 6, 8, 10]           # C, E, G, I, K
    ROLE_COL_INDICES = [1, 3, 5, 7, 9]           # B, D, F, H, J
    ARCHIVE_PCT_COL = 12                          # M
    TIMELINE_COL = 13                             # N
    QUALITY_COL = 14                              # O
    REWARD_COL = 15                               # P
    NOTES_COL = 16                                # Q
    SKIPS_COL = 11                                # L

    # First, check if TL View already exists and delete it
    meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    for sheet in meta['sheets']:
        if sheet['properties']['title'] == 'TL View':
            service.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={'requests': [{'deleteSheet': {'sheetId': sheet['properties']['sheetId']}}]}
            ).execute()
            break

    # Add the TL View sheet
    resp = service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={'requests': [{
            'addSheet': {
                'properties': {
                    'title': 'TL View',
                    'index': 0,
                    'gridProperties': {'frozenRowCount': 3, 'rowCount': 30, 'columnCount': NUM_COLS},
                }
            }
        }]}
    ).execute()
    tl_sheet_id = resp['replies'][0]['addSheet']['properties']['sheetId']

    # --- Read current skip values from Data tab so we can embed them as
    # plain numbers (not formulas) in TL View!L. This lets TLs manually
    # correct a skip count without breaking a formula reference.
    sc_letter = col_letter(SKIPS_DATA_COL)
    all_data_rows = CORE_PHONES_DATA_ROWS + WIDER_TEAM_DATA_ROWS
    skip_ranges = [f'Data!{sc_letter}{r}' for r in all_data_rows]
    skip_resp = service.spreadsheets().values().batchGet(
        spreadsheetId=spreadsheet_id,
        ranges=skip_ranges,
        valueRenderOption='UNFORMATTED_VALUE',
    ).execute()
    skip_values = {}
    for r, vr in zip(all_data_rows, skip_resp['valueRanges']):
        v = vr.get('values', [[0]])[0][0]
        skip_values[r] = v if isinstance(v, (int, float)) else 0

    # --- Populate cell values ---
    rows = []

    # Row 1: Title
    rows.append(['CS Reward Time Tracker — TL View'])

    # Row 2: Week date
    rows.append(['Week ending:', '=Data!A4'])

    # Row 3: Headers
    headers = ['Name']
    for day_name, _, _, _ in DAYS:
        headers.extend([f'{day_name} Role', f'{day_name} Hit'])
    headers.extend(['Weekly Skips', 'Archive %', 'Timeline Check', 'Quality Check', 'Reward', 'TL Notes'])
    rows.append(headers)

    # Row 4: Core Phones section header
    rows.append(['Core Phones'])

    # Rows 5-9: Core Phones agents
    for tl_row, data_row in zip(CORE_TL_ROWS, CORE_PHONES_DATA_ROWS):
        row = [f'=Data!B{data_row}']
        for day_name, role_col, bmet_col, smet_col in DAYS:
            rc = col_letter(role_col)
            row.append(f'=Data!{rc}{data_row}')  # Role
            row.append(build_hit_formula(tl_row, data_row, role_col, bmet_col, smet_col))  # Hit
        row.append(skip_values.get(data_row, 0))  # Weekly Skips (plain — TL can edit)
        row.append(build_archive_pct_formula(data_row))  # Archive % (auto)
        row.append('')  # Timeline Check (TL fills)
        row.append('')  # Quality Check (TL fills)
        row.append(build_reward_formula(data_row, tl_row))  # Reward (auto)
        row.append('')  # TL Notes
        rows.append(row)

    # Row 10: Wider Team section header
    rows.append(['Wider Team'])

    # Rows 11-24: Wider Team agents
    for tl_row, data_row in zip(WIDER_TL_ROWS, WIDER_TEAM_DATA_ROWS):
        row = [f'=Data!B{data_row}']
        for day_name, role_col, bmet_col, smet_col in DAYS:
            rc = col_letter(role_col)
            row.append(f'=Data!{rc}{data_row}')
            row.append(build_hit_formula(tl_row, data_row, role_col, bmet_col, smet_col))
        row.append(skip_values.get(data_row, 0))  # Weekly Skips (plain — TL can edit)
        row.append(build_archive_pct_formula(data_row))  # Archive % (auto)
        row.append('')  # Timeline
        row.append('')  # Quality
        row.append(build_reward_formula(data_row, tl_row))  # Reward (auto)
        row.append('')  # Notes
        rows.append(row)

    # Write all values
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range='TL View!A1',
        valueInputOption='USER_ENTERED',
        body={'values': rows}
    ).execute()

    # --- Formatting requests ---
    requests = []

    # Column widths — tuned to fit on one screen (~1200px usable)
    col_widths = {
        0: 110,              # A: Name
        SKIPS_COL: 50,       # L: Skips
        ARCHIVE_PCT_COL: 62, # M: Archive %
        TIMELINE_COL: 75,    # N: Timeline Check
        QUALITY_COL: 75,     # O: Quality Check
        REWARD_COL: 95,      # P: Reward (auto)
        NOTES_COL: 140,      # Q: Notes
    }
    for i in ROLE_COL_INDICES:
        col_widths[i] = 56   # Role columns
    for i in HIT_COL_INDICES:
        col_widths[i] = 72   # Hit columns

    for col_idx, width in col_widths.items():
        requests.append({
            'updateDimensionProperties': {
                'range': {'sheetId': tl_sheet_id, 'dimension': 'COLUMNS',
                          'startIndex': col_idx, 'endIndex': col_idx + 1},
                'properties': {'pixelSize': width},
                'fields': 'pixelSize'
            }
        })

    # Juno brand colours
    # - Blue: #0F5CB8 (links/headers)
    # - Green: #218B21 (success)
    # - Grey: #808080 (subtitles)
    # Softer tints are used for fills to feel less intimidating

    # Default: Overpass 10pt across the whole sheet
    requests.append({
        'repeatCell': {
            'range': {'sheetId': tl_sheet_id, 'startRowIndex': 0, 'endRowIndex': 30,
                      'startColumnIndex': 0, 'endColumnIndex': NUM_COLS},
            'cell': {'userEnteredFormat': {
                'textFormat': {'fontFamily': 'Overpass', 'fontSize': 10}
            }},
            'fields': 'userEnteredFormat.textFormat.fontFamily,userEnteredFormat.textFormat.fontSize'
        }
    })

    # Row 1: Title — bold, 14pt Overpass, soft Juno blue bg with white text
    requests.append({
        'repeatCell': {
            'range': {'sheetId': tl_sheet_id, 'startRowIndex': 0, 'endRowIndex': 1,
                      'startColumnIndex': 0, 'endColumnIndex': NUM_COLS},
            'cell': {'userEnteredFormat': {
                'textFormat': {
                    'bold': True, 'fontSize': 14, 'fontFamily': 'Overpass',
                    'foregroundColorStyle': {'rgbColor': {'red': 1, 'green': 1, 'blue': 1}}
                },
                'backgroundColor': {'red': 0.059, 'green': 0.361, 'blue': 0.722},  # #0F5CB8 Juno blue
                'horizontalAlignment': 'CENTER',
                'verticalAlignment': 'MIDDLE',
            }},
            'fields': 'userEnteredFormat'
        }
    })
    requests.append({
        'mergeCells': {
            'range': {'sheetId': tl_sheet_id, 'startRowIndex': 0, 'endRowIndex': 1,
                      'startColumnIndex': 0, 'endColumnIndex': NUM_COLS},
            'mergeType': 'MERGE_ALL'
        }
    })
    # Title row taller
    requests.append({
        'updateDimensionProperties': {
            'range': {'sheetId': tl_sheet_id, 'dimension': 'ROWS',
                      'startIndex': 0, 'endIndex': 1},
            'properties': {'pixelSize': 42},
            'fields': 'pixelSize'
        }
    })

    # Row 2: Week ending — italic grey
    requests.append({
        'repeatCell': {
            'range': {'sheetId': tl_sheet_id, 'startRowIndex': 1, 'endRowIndex': 2,
                      'startColumnIndex': 0, 'endColumnIndex': NUM_COLS},
            'cell': {'userEnteredFormat': {
                'textFormat': {
                    'italic': True, 'fontFamily': 'Overpass', 'fontSize': 10,
                    'foregroundColorStyle': {'rgbColor': {'red': 0.502, 'green': 0.502, 'blue': 0.502}}  # #808080
                },
            }},
            'fields': 'userEnteredFormat.textFormat'
        }
    })

    # Row 3: Headers — bold Overpass, white on Juno blue, wrapped so full titles show
    requests.append({
        'repeatCell': {
            'range': {'sheetId': tl_sheet_id, 'startRowIndex': 2, 'endRowIndex': 3,
                      'startColumnIndex': 0, 'endColumnIndex': NUM_COLS},
            'cell': {'userEnteredFormat': {
                'textFormat': {
                    'bold': True, 'fontSize': 10, 'fontFamily': 'Overpass',
                    'foregroundColorStyle': {'rgbColor': {'red': 1, 'green': 1, 'blue': 1}}
                },
                'backgroundColor': {'red': 0.059, 'green': 0.361, 'blue': 0.722},  # #0F5CB8
                'horizontalAlignment': 'CENTER',
                'verticalAlignment': 'MIDDLE',
                'wrapStrategy': 'WRAP',
            }},
            'fields': 'userEnteredFormat'
        }
    })
    # Header row taller so wrapped titles (Weekly Skips, Timeline Check, etc.) are fully visible
    requests.append({
        'updateDimensionProperties': {
            'range': {'sheetId': tl_sheet_id, 'dimension': 'ROWS',
                      'startIndex': 2, 'endIndex': 3},
            'properties': {'pixelSize': 44},
            'fields': 'pixelSize'
        }
    })

    # Section header rows (4 and 10) — bold, soft cream bg
    for row_idx in [3, 9]:
        requests.append({
            'repeatCell': {
                'range': {'sheetId': tl_sheet_id, 'startRowIndex': row_idx, 'endRowIndex': row_idx + 1,
                          'startColumnIndex': 0, 'endColumnIndex': NUM_COLS},
                'cell': {'userEnteredFormat': {
                    'textFormat': {
                        'bold': True, 'fontSize': 10, 'fontFamily': 'Overpass',
                        'foregroundColorStyle': {'rgbColor': {'red': 0.251, 'green': 0.251, 'blue': 0.251}}  # dark grey
                    },
                    'backgroundColor': {'red': 0.996, 'green': 0.953, 'blue': 0.780},  # soft cream
                }},
                'fields': 'userEnteredFormat'
            }
        })

    # Center-align columns B-N (indices 1 to NOTES_COL-1=13) for data rows
    for start_row in [4, 10]:
        end_row = start_row + (5 if start_row == 4 else 14)
        requests.append({
            'repeatCell': {
                'range': {'sheetId': tl_sheet_id, 'startRowIndex': start_row, 'endRowIndex': end_row,
                          'startColumnIndex': 1, 'endColumnIndex': NOTES_COL},
                'cell': {'userEnteredFormat': {
                    'horizontalAlignment': 'CENTER',
                    'verticalAlignment': 'MIDDLE',
                }},
                'fields': 'userEnteredFormat.horizontalAlignment,userEnteredFormat.verticalAlignment'
            }
        })

    # Data rows: ensure Overpass everywhere, taller rows for readability
    for start_row in [4, 10]:
        end_row = start_row + (5 if start_row == 4 else 14)
        requests.append({
            'updateDimensionProperties': {
                'range': {'sheetId': tl_sheet_id, 'dimension': 'ROWS',
                          'startIndex': start_row, 'endIndex': end_row},
                'properties': {'pixelSize': 32},
                'fields': 'pixelSize'
            }
        })

    # Alternating row stripes on col A only (subtle readability) — very light blue-grey
    for row_idx in [4, 6, 8, 10, 12, 14, 16, 18, 20, 22]:
        requests.append({
            'repeatCell': {
                'range': {'sheetId': tl_sheet_id, 'startRowIndex': row_idx, 'endRowIndex': row_idx + 1,
                          'startColumnIndex': 0, 'endColumnIndex': 1},
                'cell': {'userEnteredFormat': {
                    'backgroundColor': {'red': 0.961, 'green': 0.969, 'blue': 0.980}
                }},
                'fields': 'userEnteredFormat.backgroundColor'
            }
        })

    # Left-align Name column + bold
    for start_row in [4, 10]:
        end_row = start_row + (5 if start_row == 4 else 14)
        requests.append({
            'repeatCell': {
                'range': {'sheetId': tl_sheet_id, 'startRowIndex': start_row, 'endRowIndex': end_row,
                          'startColumnIndex': 0, 'endColumnIndex': 1},
                'cell': {'userEnteredFormat': {
                    'horizontalAlignment': 'LEFT',
                    'textFormat': {'bold': True, 'fontFamily': 'Overpass', 'fontSize': 10},
                    'padding': {'left': 8},
                }},
                'fields': 'userEnteredFormat.horizontalAlignment,userEnteredFormat.textFormat,userEnteredFormat.padding'
            }
        })

    # --- Conditional formatting ---
    # Hit columns + Reward column — they share the same Stretch/Base/Off palette
    hit_ranges = [
        {'sheetId': tl_sheet_id, 'startRowIndex': 4, 'endRowIndex': 25,
         'startColumnIndex': c, 'endColumnIndex': c + 1}
        for c in HIT_COL_INDICES + [REWARD_COL]
    ]
    reward_only_ranges = [
        {'sheetId': tl_sheet_id, 'startRowIndex': 4, 'endRowIndex': 25,
         'startColumnIndex': REWARD_COL, 'endColumnIndex': REWARD_COL + 1}
    ]

    # Softer Juno-style colours:
    # - Stretch ⭐ → soft green bg #D4EFDF, dark Juno green text #196F3D (bold)
    # - Base ✅   → very light green bg #EAFAF1, Juno green text #218B21
    # - Miss ❌   → soft red bg #FADBD8, muted red text #922B21
    # - Off 🌴    → light warm grey bg #F2F3F4, grey text #808080

    # Stretch ⭐ → soft green, dark green text, bold
    requests.append({
        'addConditionalFormatRule': {
            'rule': {
                'ranges': hit_ranges,
                'booleanRule': {
                    'condition': {'type': 'TEXT_CONTAINS', 'values': [{'userEnteredValue': 'Stretch'}]},
                    'format': {
                        'backgroundColor': {'red': 0.831, 'green': 0.937, 'blue': 0.875},  # #D4EFDF
                        'textFormat': {
                            'bold': True,
                            'foregroundColor': {'red': 0.098, 'green': 0.435, 'blue': 0.239}  # #196F3D
                        }
                    }
                }
            },
            'index': 0
        }
    })
    # Base ✅ → very light green, Juno green text
    requests.append({
        'addConditionalFormatRule': {
            'rule': {
                'ranges': hit_ranges,
                'booleanRule': {
                    'condition': {'type': 'TEXT_CONTAINS', 'values': [{'userEnteredValue': 'Base'}]},
                    'format': {
                        'backgroundColor': {'red': 0.918, 'green': 0.980, 'blue': 0.945},  # #EAFAF1
                        'textFormat': {
                            'foregroundColor': {'red': 0.129, 'green': 0.545, 'blue': 0.129}  # #218B21 Juno green
                        }
                    }
                }
            },
            'index': 1
        }
    })
    # Miss ❌ → soft red bg, muted red text (no harsh white-on-red)
    requests.append({
        'addConditionalFormatRule': {
            'rule': {
                'ranges': hit_ranges,
                'booleanRule': {
                    'condition': {'type': 'TEXT_CONTAINS', 'values': [{'userEnteredValue': 'Miss'}]},
                    'format': {
                        'backgroundColor': {'red': 0.980, 'green': 0.859, 'blue': 0.847},  # #FADBD8
                        'textFormat': {
                            'foregroundColor': {'red': 0.573, 'green': 0.169, 'blue': 0.129}  # #922B21
                        }
                    }
                }
            },
            'index': 2
        }
    })
    # Off 🌴 → light warm grey, grey text
    requests.append({
        'addConditionalFormatRule': {
            'rule': {
                'ranges': hit_ranges,
                'booleanRule': {
                    'condition': {'type': 'TEXT_CONTAINS', 'values': [{'userEnteredValue': 'Off'}]},
                    'format': {
                        'backgroundColor': {'red': 0.949, 'green': 0.953, 'blue': 0.957},  # #F2F3F4
                        'textFormat': {
                            'foregroundColor': {'red': 0.502, 'green': 0.502, 'blue': 0.502}  # #808080
                        }
                    }
                }
            },
            'index': 3
        }
    })
    # Reward-only rules: "None" (earned nothing) + "Pending" (quality not filled / actuals missing)
    # None ❌ → same soft red as Miss
    requests.append({
        'addConditionalFormatRule': {
            'rule': {
                'ranges': reward_only_ranges,
                'booleanRule': {
                    'condition': {'type': 'TEXT_CONTAINS', 'values': [{'userEnteredValue': 'None'}]},
                    'format': {
                        'backgroundColor': {'red': 0.980, 'green': 0.859, 'blue': 0.847},  # #FADBD8
                        'textFormat': {
                            'foregroundColor': {'red': 0.573, 'green': 0.169, 'blue': 0.129}  # #922B21
                        }
                    }
                }
            },
            'index': 4
        }
    })
    # Pending ⏳ → pale amber, muted amber text (applies to Hit cols + Reward col)
    requests.append({
        'addConditionalFormatRule': {
            'rule': {
                'ranges': hit_ranges,
                'booleanRule': {
                    'condition': {'type': 'TEXT_CONTAINS', 'values': [{'userEnteredValue': 'Pending'}]},
                    'format': {
                        'backgroundColor': {'red': 0.996, 'green': 0.953, 'blue': 0.839},  # #FEF3D6
                        'textFormat': {
                            'foregroundColor': {'red': 0.549, 'green': 0.361, 'blue': 0.0}   # #8C5C00
                        }
                    }
                }
            },
            'index': 5
        }
    })

    # Timeline/Quality columns: M (12), N (13)
    tl_ranges = [
        {'sheetId': tl_sheet_id, 'startRowIndex': 4, 'endRowIndex': 25,
         'startColumnIndex': c, 'endColumnIndex': c + 1}
        for c in [TIMELINE_COL, QUALITY_COL]
    ]
    # Pass → soft green with Juno green text (uses TEXT_CONTAINS to match "✅ Pass")
    requests.append({
        'addConditionalFormatRule': {
            'rule': {
                'ranges': tl_ranges,
                'booleanRule': {
                    'condition': {'type': 'TEXT_CONTAINS', 'values': [{'userEnteredValue': 'Pass'}]},
                    'format': {
                        'backgroundColor': {'red': 0.918, 'green': 0.980, 'blue': 0.945},  # #EAFAF1
                        'textFormat': {
                            'foregroundColor': {'red': 0.129, 'green': 0.545, 'blue': 0.129}  # #218B21
                        }
                    }
                }
            },
            'index': 4
        }
    })
    # Fail → soft red with muted red text
    requests.append({
        'addConditionalFormatRule': {
            'rule': {
                'ranges': tl_ranges,
                'booleanRule': {
                    'condition': {'type': 'TEXT_CONTAINS', 'values': [{'userEnteredValue': 'Fail'}]},
                    'format': {
                        'backgroundColor': {'red': 0.980, 'green': 0.859, 'blue': 0.847},  # #FADBD8
                        'textFormat': {
                            'foregroundColor': {'red': 0.573, 'green': 0.169, 'blue': 0.129}  # #922B21
                        }
                    }
                }
            },
            'index': 5
        }
    })
    # N/A → light warm grey
    requests.append({
        'addConditionalFormatRule': {
            'rule': {
                'ranges': tl_ranges,
                'booleanRule': {
                    'condition': {'type': 'TEXT_CONTAINS', 'values': [{'userEnteredValue': 'N/A'}]},
                    'format': {
                        'backgroundColor': {'red': 0.949, 'green': 0.953, 'blue': 0.957},  # #F2F3F4
                        'textFormat': {
                            'foregroundColor': {'red': 0.502, 'green': 0.502, 'blue': 0.502}
                        }
                    }
                }
            },
            'index': 6
        }
    })

    # Archive % column (M): colour-coded by archive ratio threshold
    archive_ranges = [
        {'sheetId': tl_sheet_id, 'startRowIndex': 4, 'endRowIndex': 25,
         'startColumnIndex': ARCHIVE_PCT_COL, 'endColumnIndex': ARCHIVE_PCT_COL + 1}
    ]
    # "—" (no triage days) → light grey
    requests.append({
        'addConditionalFormatRule': {
            'rule': {
                'ranges': archive_ranges,
                'booleanRule': {
                    'condition': {'type': 'TEXT_EQ', 'values': [{'userEnteredValue': '—'}]},
                    'format': {
                        'backgroundColor': {'red': 0.949, 'green': 0.953, 'blue': 0.957},
                        'textFormat': {'foregroundColor': {'red': 0.502, 'green': 0.502, 'blue': 0.502}}
                    }
                }
            },
            'index': 7
        }
    })
    # ≥85% → soft green (baseline target met)
    requests.append({
        'addConditionalFormatRule': {
            'rule': {
                'ranges': archive_ranges,
                'booleanRule': {
                    'condition': {'type': 'CUSTOM_FORMULA',
                                  'values': [{'userEnteredValue': f'=AND(M5<>"—",VALUE(SUBSTITUTE(M5,"%",""))/100>=0.85)'}]},
                    'format': {
                        'backgroundColor': {'red': 0.918, 'green': 0.980, 'blue': 0.945},
                        'textFormat': {'foregroundColor': {'red': 0.129, 'green': 0.545, 'blue': 0.129}}
                    }
                }
            },
            'index': 8
        }
    })
    # 75-84% → pale amber (close but below target)
    requests.append({
        'addConditionalFormatRule': {
            'rule': {
                'ranges': archive_ranges,
                'booleanRule': {
                    'condition': {'type': 'CUSTOM_FORMULA',
                                  'values': [{'userEnteredValue': f'=AND(M5<>"—",VALUE(SUBSTITUTE(M5,"%",""))/100>=0.75,VALUE(SUBSTITUTE(M5,"%",""))/100<0.85)'}]},
                    'format': {
                        'backgroundColor': {'red': 0.996, 'green': 0.953, 'blue': 0.839},
                        'textFormat': {'foregroundColor': {'red': 0.549, 'green': 0.361, 'blue': 0.0}}
                    }
                }
            },
            'index': 9
        }
    })
    # <75% → soft red (well below target)
    requests.append({
        'addConditionalFormatRule': {
            'rule': {
                'ranges': archive_ranges,
                'booleanRule': {
                    'condition': {'type': 'CUSTOM_FORMULA',
                                  'values': [{'userEnteredValue': f'=AND(M5<>"—",VALUE(SUBSTITUTE(M5,"%",""))/100<0.75)'}]},
                    'format': {
                        'backgroundColor': {'red': 0.980, 'green': 0.859, 'blue': 0.847},
                        'textFormat': {'foregroundColor': {'red': 0.573, 'green': 0.169, 'blue': 0.129}}
                    }
                }
            },
            'index': 10
        }
    })

    # --- Data validation (dropdowns) ---
    # Timeline Check (N=13) + Quality Check (O=14) — single weekly value per person
    for col_idx in [TIMELINE_COL, QUALITY_COL]:
        # Apply to data rows 5-9 (indices 4-8) and 11-24 (indices 10-23)
        for start_idx, end_idx in [(4, 9), (10, 24)]:
            requests.append({
                'setDataValidation': {
                    'range': {'sheetId': tl_sheet_id, 'startRowIndex': start_idx, 'endRowIndex': end_idx,
                              'startColumnIndex': col_idx, 'endColumnIndex': col_idx + 1},
                    'rule': {
                        'condition': {
                            'type': 'ONE_OF_LIST',
                            'values': [
                                {'userEnteredValue': '✅ Pass'},
                                {'userEnteredValue': '❌ Fail'},
                                {'userEnteredValue': '⏸️ N/A'},
                            ]
                        },
                        'showCustomUi': True,
                        'strict': True,
                    }
                }
            })

    # Execute all formatting requests
    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={'requests': requests}
    ).execute()

    return tl_sheet_id


def create_tl_calculator(service, spreadsheet_id):
    """Create the TL Calculator tab."""

    # Check if it already exists and delete
    meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    for sheet in meta['sheets']:
        if sheet['properties']['title'] == 'TL Calculator':
            service.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={'requests': [{'deleteSheet': {'sheetId': sheet['properties']['sheetId']}}]}
            ).execute()
            break

    # Add the tab
    resp = service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={'requests': [{
            'addSheet': {
                'properties': {
                    'title': 'TL Calculator',
                    'index': 1,
                    'gridProperties': {'frozenRowCount': 5, 'rowCount': 30, 'columnCount': 8},
                }
            }
        }]}
    ).execute()
    calc_sheet_id = resp['replies'][0]['addSheet']['properties']['sheetId']

    # Build rows
    rows = [
        ['CLIENT SUPPORT - WEEKLY REWARD TIME CALCULATOR'],
        [],
        ['Tick the checkbox if someone hit their Base or Stretch target. Reward hours are pro-rated from 40hrs.'],
        [],
        ['Name', 'Manager', 'Weekly Hours', 'Base Reward', 'Stretch Reward', 'Hit Base?', 'Hit Stretch?', 'Reward Hours Due'],
    ]

    current_row = 6  # 1-indexed
    team_lead_rows = []
    data_rows = []

    for manager, members in TL_CALC_TEAMS.items():
        team_lead_rows.append(current_row - 1)  # 0-indexed for formatting
        rows.append([manager])
        current_row += 1

        for name, hours in members:
            r = current_row
            rows.append([
                name, manager, hours,
                f'=3*(C{r}/40)',
                f'=4*(C{r}/40)',
                False, False,
                f'=IF(IF(G{r}=TRUE, E{r}, IF(F{r}=TRUE, D{r}, 0))=0, "—", '
                f'IF(CEILING(MOD(IF(G{r}=TRUE, E{r}, IF(F{r}=TRUE, D{r}, 0)),1)*60, 15)=60, '
                f'INT(IF(G{r}=TRUE, E{r}, IF(F{r}=TRUE, D{r}, 0)))+1 & "h 0m", '
                f'INT(IF(G{r}=TRUE, E{r}, IF(F{r}=TRUE, D{r}, 0))) & "h " & '
                f'CEILING(MOD(IF(G{r}=TRUE, E{r}, IF(F{r}=TRUE, D{r}, 0)),1)*60, 15) & "m"))'
            ])
            data_rows.append(current_row - 1)  # 0-indexed
            current_row += 1

    # Empty row + TOTALS
    rows.append([])
    current_row += 1
    totals_row = current_row - 1  # 0-indexed

    # Data rows range for totals (first data row to last)
    first_dr = data_rows[0] + 1   # 1-indexed
    last_dr = data_rows[-1] + 1
    rows.append([
        'TOTALS', '', '', '', '', '', '',
        f'=IF(SUMPRODUCT((G{first_dr}:G{last_dr}=TRUE)*E{first_dr}:E{last_dr})'
        f'+SUMPRODUCT((F{first_dr}:F{last_dr}=TRUE)*D{first_dr}:D{last_dr})=0, "—", '
        f'IF(CEILING(MOD(SUMPRODUCT((G{first_dr}:G{last_dr}=TRUE)*E{first_dr}:E{last_dr})'
        f'+SUMPRODUCT((F{first_dr}:F{last_dr}=TRUE)*D{first_dr}:D{last_dr}),1)*60, 15)=60, '
        f'INT(SUMPRODUCT((G{first_dr}:G{last_dr}=TRUE)*E{first_dr}:E{last_dr})'
        f'+SUMPRODUCT((F{first_dr}:F{last_dr}=TRUE)*D{first_dr}:D{last_dr}))+1 & "h 0m", '
        f'INT(SUMPRODUCT((G{first_dr}:G{last_dr}=TRUE)*E{first_dr}:E{last_dr})'
        f'+SUMPRODUCT((F{first_dr}:F{last_dr}=TRUE)*D{first_dr}:D{last_dr})) & "h " & '
        f'CEILING(MOD(SUMPRODUCT((G{first_dr}:G{last_dr}=TRUE)*E{first_dr}:E{last_dr})'
        f'+SUMPRODUCT((F{first_dr}:F{last_dr}=TRUE)*D{first_dr}:D{last_dr}),1)*60, 15) & "m"))'
    ])

    # Write values
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range='TL Calculator!A1',
        valueInputOption='USER_ENTERED',
        body={'values': rows}
    ).execute()

    # Formatting
    requests = []

    # Column widths
    for col_idx, width in {0: 143, 1: 119, 2: 110, 3: 110, 4: 120, 5: 93, 6: 93, 7: 140}.items():
        requests.append({
            'updateDimensionProperties': {
                'range': {'sheetId': calc_sheet_id, 'dimension': 'COLUMNS',
                          'startIndex': col_idx, 'endIndex': col_idx + 1},
                'properties': {'pixelSize': width},
                'fields': 'pixelSize'
            }
        })

    # Row 1: Title
    requests.append({
        'repeatCell': {
            'range': {'sheetId': calc_sheet_id, 'startRowIndex': 0, 'endRowIndex': 1,
                      'startColumnIndex': 0, 'endColumnIndex': 8},
            'cell': {'userEnteredFormat': {
                'textFormat': {'bold': True, 'fontSize': 14, 'fontFamily': 'Arial'}
            }},
            'fields': 'userEnteredFormat.textFormat'
        }
    })

    # Row 3: Instructions — italic
    requests.append({
        'repeatCell': {
            'range': {'sheetId': calc_sheet_id, 'startRowIndex': 2, 'endRowIndex': 3,
                      'startColumnIndex': 0, 'endColumnIndex': 8},
            'cell': {'userEnteredFormat': {
                'textFormat': {'italic': True, 'fontSize': 10, 'fontFamily': 'Arial'}
            }},
            'fields': 'userEnteredFormat.textFormat'
        }
    })

    # Row 5: Headers — bold+italic, white on dark blue
    requests.append({
        'repeatCell': {
            'range': {'sheetId': calc_sheet_id, 'startRowIndex': 4, 'endRowIndex': 5,
                      'startColumnIndex': 0, 'endColumnIndex': 8},
            'cell': {'userEnteredFormat': {
                'textFormat': {'bold': True, 'italic': True, 'fontSize': 11, 'fontFamily': 'Arial',
                               'foregroundColorStyle': {'rgbColor': {'red': 1, 'green': 1, 'blue': 1}}},
                'backgroundColor': {'red': 0.20, 'green': 0.40, 'blue': 0.65},
            }},
            'fields': 'userEnteredFormat'
        }
    })

    # Team lead header rows — bold, yellow bg
    for row_idx in team_lead_rows:
        requests.append({
            'repeatCell': {
                'range': {'sheetId': calc_sheet_id, 'startRowIndex': row_idx, 'endRowIndex': row_idx + 1,
                          'startColumnIndex': 0, 'endColumnIndex': 8},
                'cell': {'userEnteredFormat': {
                    'textFormat': {'bold': True, 'fontSize': 10, 'fontFamily': 'Arial'},
                    'backgroundColor': {'red': 0.98, 'green': 0.93, 'blue': 0.60},
                }},
                'fields': 'userEnteredFormat'
            }
        })

    # Totals row — bold, grey bg
    requests.append({
        'repeatCell': {
            'range': {'sheetId': calc_sheet_id, 'startRowIndex': totals_row, 'endRowIndex': totals_row + 1,
                      'startColumnIndex': 0, 'endColumnIndex': 8},
            'cell': {'userEnteredFormat': {
                'textFormat': {'bold': True, 'fontSize': 11, 'fontFamily': 'Arial'},
                'backgroundColor': {'red': 0.90, 'green': 0.90, 'blue': 0.90},
            }},
            'fields': 'userEnteredFormat'
        }
    })

    # Zebra stripes on odd data rows
    for i, row_idx in enumerate(data_rows):
        if i % 2 == 0:  # odd visual rows
            requests.append({
                'repeatCell': {
                    'range': {'sheetId': calc_sheet_id, 'startRowIndex': row_idx, 'endRowIndex': row_idx + 1,
                              'startColumnIndex': 0, 'endColumnIndex': 5},
                    'cell': {'userEnteredFormat': {
                        'backgroundColor': {'red': 0.95, 'green': 0.95, 'blue': 0.97}
                    }},
                    'fields': 'userEnteredFormat.backgroundColor'
                }
            })

    # Center-align F and G columns, bold G and H
    for row_idx in data_rows:
        requests.append({
            'repeatCell': {
                'range': {'sheetId': calc_sheet_id, 'startRowIndex': row_idx, 'endRowIndex': row_idx + 1,
                          'startColumnIndex': 5, 'endColumnIndex': 7},
                'cell': {'userEnteredFormat': {
                    'horizontalAlignment': 'CENTER',
                    'verticalAlignment': 'MIDDLE',
                }},
                'fields': 'userEnteredFormat.horizontalAlignment,userEnteredFormat.verticalAlignment'
            }
        })

    # Checkbox data validation on F and G columns
    for col_idx in [5, 6]:
        for row_idx in data_rows:
            requests.append({
                'setDataValidation': {
                    'range': {'sheetId': calc_sheet_id, 'startRowIndex': row_idx, 'endRowIndex': row_idx + 1,
                              'startColumnIndex': col_idx, 'endColumnIndex': col_idx + 1},
                    'rule': {'condition': {'type': 'BOOLEAN'}, 'showCustomUi': True}
                }
            })

    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={'requests': requests}
    ).execute()

    return calc_sheet_id


def hide_data_and_reorder(service, spreadsheet_id, tl_view_id, tl_calc_id):
    """Hide the Data, Working Hours, and Targets tabs (TL-internal references)."""
    meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()

    tabs_to_hide = {'Data', 'Working Hours', 'Targets'}
    requests = []
    for sheet in meta['sheets']:
        title = sheet['properties']['title']
        sid = sheet['properties']['sheetId']
        if title in tabs_to_hide:
            requests.append({
                'updateSheetProperties': {
                    'properties': {'sheetId': sid, 'hidden': True},
                    'fields': 'hidden'
                }
            })

    if requests:
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={'requests': requests}
        ).execute()


def make_tl_copy(service, source_spreadsheet_id, name_prefix='TL View - '):
    """Create a TL-facing copy of the full tracker with Data/Working Hours/Targets hidden.

    Call this AFTER any pro-rata adjustments have been applied to the full version,
    so the TL copy reflects the same pro-rated targets.

    Returns the URL of the new copy.
    """
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build as _build
    creds = Credentials.from_authorized_user_file(str(CREDS_PATH))
    drive = _build('drive', 'v3', credentials=creds)

    # Look up source name so the copy gets a sensible title
    src_meta = drive.files().get(fileId=source_spreadsheet_id, fields='name').execute()
    new_name = name_prefix + src_meta['name']

    # Copy the file in Drive
    copy = drive.files().copy(
        fileId=source_spreadsheet_id,
        body={'name': new_name},
    ).execute()
    new_id = copy['id']

    # Hide TL-internal tabs on the copy
    meta = service.spreadsheets().get(spreadsheetId=new_id).execute()
    tabs_to_hide = {'Data', 'Working Hours', 'Targets'}
    requests = []
    for sheet in meta['sheets']:
        title = sheet['properties']['title']
        sid = sheet['properties']['sheetId']
        if title in tabs_to_hide:
            requests.append({
                'updateSheetProperties': {
                    'properties': {'sheetId': sid, 'hidden': True},
                    'fields': 'hidden'
                }
            })
    if requests:
        service.spreadsheets().batchUpdate(
            spreadsheetId=new_id, body={'requests': requests}
        ).execute()

    return f'https://docs.google.com/spreadsheets/d/{new_id}'


def main():
    if len(sys.argv) != 2:
        print("Usage: python3 setup_tl_view.py <spreadsheet_id>")
        sys.exit(1)

    spreadsheet_id = sys.argv[1]
    print(f"Setting up TL View for spreadsheet: {spreadsheet_id}")

    service = get_sheets_service()

    print("Creating TL View tab...")
    tl_view_id = create_tl_view(service, spreadsheet_id)
    print(f"  TL View created (sheetId={tl_view_id})")

    print("Creating TL Calculator tab...")
    tl_calc_id = create_tl_calculator(service, spreadsheet_id)
    print(f"  TL Calculator created (sheetId={tl_calc_id})")

    print("Hiding Data tab and reordering...")
    hide_data_and_reorder(service, spreadsheet_id, tl_view_id, tl_calc_id)

    print("Done!")


if __name__ == '__main__':
    main()
