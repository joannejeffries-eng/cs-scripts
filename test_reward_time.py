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


class WebchatTriageTargets(unittest.TestCase):
    def test_triage_webchat_plus_variant(self):
        self.assertEqual(rt._lookup_targets('Triage + Webchat'),
                         (100, 140, 'emails_archived'))

    def test_triage_webchat_and_variant(self):
        # Label variant ('and' rather than '+') still resolves.
        self.assertEqual(rt._lookup_targets('Triage and Webchat'),
                         (100, 140, 'emails_archived'))

    def test_plain_triage_unchanged(self):
        self.assertEqual(rt._lookup_targets('Triage only'),
                         (130, 180, 'emails_archived'))

    def test_is_webchat_triage(self):
        self.assertTrue(rt._is_webchat_triage('Triage + Webchat'))
        self.assertTrue(rt._is_webchat_triage('Triage and Webchat'))
        self.assertFalse(rt._is_webchat_triage('Triage only'))
        self.assertFalse(rt._is_webchat_triage('Triage and Video Calls'))
        # Webchat on the phones side is not a triage day.
        self.assertFalse(rt._is_webchat_triage('Inbound phones + Webchat'))


class NetSkips(unittest.TestCase):
    def test_untrained_id_skips_excluded(self):
        # Kirsty w/e 9 Jul: 37 ID-check + 34 other + 1 → 35 net.
        bd = {'id_check': 37, 'ics_valid': 0, 'other': 35}
        net, gross, excluded = rt._net_skips(bd, is_id_trained=False)
        self.assertEqual((net, gross), (35, 72))
        self.assertIn('37 ID-check (untrained)', excluded)

    def test_trained_id_skips_counted(self):
        bd = {'id_check': 37, 'ics_valid': 0, 'other': 35}
        net, gross, excluded = rt._net_skips(bd, is_id_trained=True)
        self.assertEqual((net, gross), (72, 72))
        self.assertEqual(excluded, [])

    def test_valid_ics_always_excluded(self):
        # Sophie w/e 9 Jul: 8 valid-ICS + 42 other, ID-trained → 42 net.
        bd = {'id_check': 0, 'ics_valid': 8, 'other': 42}
        net, gross, excluded = rt._net_skips(bd, is_id_trained=True)
        self.assertEqual((net, gross), (42, 50))
        self.assertIn('8 valid-ICS', excluded)

    def test_both_exclusions_untrained(self):
        bd = {'id_check': 5, 'ics_valid': 3, 'other': 10}
        net, gross, excluded = rt._net_skips(bd, is_id_trained=False)
        self.assertEqual((net, gross), (10, 18))
        self.assertEqual(len(excluded), 2)


if __name__ == '__main__':
    unittest.main()
