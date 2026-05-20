#!/usr/bin/env python3
"""
Pull daily actuals from Looker/Postgres into the reward-time week JSON.

Usage:
    # Pull yesterday's actuals (the typical daily-cron use)
    python3 pull_reward_actuals.py

    # Pull a specific date
    python3 pull_reward_actuals.py 2026-05-14

    # Pull every elapsed working day in a reward week (catch-up)
    python3 pull_reward_actuals.py --week 2026-05-15 --through-today
"""
import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import reward_time as rt
import generate_rota as gr


def _is_weekday(d):
    return d.weekday() < 5


def _yesterday_workday():
    today = date.today()
    d = today - timedelta(days=1)
    while not _is_weekday(d):
        d -= timedelta(days=1)
    return d


def _load_or_build_week(friday):
    """Return week_data for `friday`, building from rota if the JSON doesn't exist."""
    week = rt.load_week(friday)
    if week:
        return week

    # No file yet — build from rota. Fri lives in the rota week starting friday-4d (Monday).
    fri_monday = friday - timedelta(days=friday.weekday())  # Monday of Friday's rota week
    mon_thu_monday = fri_monday + timedelta(days=7)        # Mon-Thu live in the next rota week

    gc = gr.get_gspread()
    fri_assignments, _ = gr.read_original_rota(gc, fri_monday)
    mon_thu_assignments, _ = gr.read_original_rota(gc, mon_thu_monday)

    if not any(any(days.values()) for days in fri_assignments.values()):
        fri_assignments = mon_thu_assignments
    if not any(any(days.values()) for days in mon_thu_assignments.values()):
        mon_thu_assignments = fri_assignments

    return rt.build_week(
        friday,
        assignments_fri=fri_assignments,
        assignments_mon_thu=mon_thu_assignments,
    )


def pull_for_date(target):
    """Pull actuals for one date into its reward-week file. Returns the friday."""
    friday = rt.get_reward_friday(target)
    week = _load_or_build_week(friday)
    actuals = rt.pull_day_data(target)
    rt.update_day_actuals(week, target, actuals)
    rt.save_week(friday, week)
    print(f"  {target} ({target.strftime('%a')}) → week_{friday.isoformat()}.json "
          f"({len(actuals)} people, friday={friday})")
    return friday


def main():
    p = argparse.ArgumentParser()
    p.add_argument('date', nargs='?', help='YYYY-MM-DD (default: most recent weekday before today)')
    p.add_argument('--week', help='Reward Friday (YYYY-MM-DD); used with --through-today')
    p.add_argument('--through-today', action='store_true',
                   help='With --week, pull every elapsed working day in that reward week')
    args = p.parse_args()

    if args.week and args.through_today:
        friday = datetime.strptime(args.week, '%Y-%m-%d').date()
        if friday.weekday() != 4:
            friday = rt.get_reward_friday(friday)
        today = date.today()
        targets = [d for d in rt.get_weekday_dates(friday) if d <= today]
        if not targets:
            print(f"No elapsed working days in reward week starting {friday}.")
            return
        print(f"Pulling {len(targets)} day(s) into week_{friday.isoformat()}.json:")
        for t in targets:
            pull_for_date(t)
        return

    if args.date:
        target = datetime.strptime(args.date, '%Y-%m-%d').date()
    else:
        target = _yesterday_workday()
        print(f"Auto-picked yesterday workday: {target}")

    pull_for_date(target)


if __name__ == '__main__':
    main()
