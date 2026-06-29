"""Unit tests for reward_time Daily Notes autofill name matching.

Run with:  python -m unittest test_reward_time
"""
import unittest
from datetime import date

import reward_time as rt


def _make_week(name, d, role='Triage only', shift_hours=8.0):
    """A one-person, one-working-day week_data fixture."""
    dr = rt.DayResult(role=role, is_working=True, shift_hours=shift_hours, metrics={})
    pw = rt.PersonWeek(name=name, days={d: dr})
    return {name: pw}, pw, dr


class ResolveWeekPerson(unittest.TestCase):
    def test_exact_match(self):
        week_data, pw, _ = _make_week('Kate', date(2026, 6, 24))
        self.assertEqual(rt._resolve_week_person(week_data, 'Kate'), ('Kate', pw))

    def test_case_insensitive_match(self):
        week_data, pw, _ = _make_week('Kate', date(2026, 6, 24))
        # Returns the canonical (properly-cased) key, not the raw input.
        self.assertEqual(rt._resolve_week_person(week_data, 'kate'), ('Kate', pw))

    def test_whitespace_tolerant_match(self):
        week_data, pw, _ = _make_week('Kate', date(2026, 6, 24))
        self.assertEqual(rt._resolve_week_person(week_data, '  KATE  '), ('Kate', pw))

    def test_no_match(self):
        week_data, _, _ = _make_week('Kate', date(2026, 6, 24))
        self.assertEqual(rt._resolve_week_person(week_data, 'Sophie'), ('Sophie', None))


class AutofillSplitsCaseInsensitive(unittest.TestCase):
    def test_lowercase_name_still_applies_reward_split(self):
        # This is the 2026-06-24 Kate regression: Daily Notes logged 'kate'
        # (lowercase) so the exact .get() missed and the split was dropped.
        d = date(2026, 6, 24)
        week_data, pw, dr = _make_week('Kate', d)
        notes_by_date = {d: [{'name': 'kate', 'time': '14:00-18:00',
                              'note': 'Reward time'}]}

        results = rt.autofill_reward_splits_from_notes(week_data, notes_by_date)

        applied = [r for r in results if r['status'] == 'applied']
        self.assertEqual(len(applied), 1)
        # Split actually landed on the day.
        self.assertTrue(dr.segments)
        self.assertTrue(any(s.role == 'Reward time (prev week)' for s in dr.segments))
        # Canonical display name is preserved in the result + override.
        self.assertEqual(applied[0]['name'], 'Kate')
        self.assertTrue(pw.overrides)

    def test_exact_name_still_applies_reward_split(self):
        d = date(2026, 6, 24)
        week_data, pw, dr = _make_week('Kate', d)
        notes_by_date = {d: [{'name': 'Kate', 'time': '14:00-18:00',
                              'note': 'Reward time'}]}

        results = rt.autofill_reward_splits_from_notes(week_data, notes_by_date)

        self.assertEqual([r['status'] for r in results], ['applied'])
        self.assertTrue(any(s.role == 'Reward time (prev week)' for s in dr.segments))


class AutofillHalfDayCaseInsensitive(unittest.TestCase):
    def test_lowercase_name_still_pro_rates_half_day(self):
        d = date(2026, 6, 24)
        week_data, pw, dr = _make_week('Sophie', d)
        notes_by_date = {d: [{'name': 'sophie', 'time': '09:00-13:00',
                              'note': 'AL half day'}]}

        results = rt.autofill_half_day_from_notes(week_data, notes_by_date)

        applied = [r for r in results if r['status'] == 'applied']
        self.assertEqual(len(applied), 1)
        # Working block snapped to the 4h Daily Notes range.
        self.assertAlmostEqual(dr.shift_hours, 4.0, places=1)
        self.assertEqual(applied[0]['name'], 'Sophie')


if __name__ == '__main__':
    unittest.main()
