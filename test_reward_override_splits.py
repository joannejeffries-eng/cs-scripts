"""Regression test: individual target overrides on split-role days.

A person with an Overrides-tab target (e.g. Kate's Inbound phones 61/70 vs the
team default 72/82) used to lose that override on a *split* day. The override is
resolved at display time keyed on the role string: a segment carries its own
role ("Inbound phones + Webchat" → matches "Inbound phones"), but the combined
day label ("Phones / Reward") matches nothing, so the Data tab (and the
formula-driven TL View that reads it) fell back to the un-overridden default
while Moves & Splits / Split Breakdown showed the correct overridden number.

This locks in that all three tabs agree on a split day.

Runs standalone (`python3 test_reward_override_splits.py`) or under pytest.
"""
import populate_reward_sheet as p
import reward_time as rt


def _kate_wednesday_split():
    """Kate's 2026-06-24: Inbound phones + Webchat 4h / Reward time 4h, phones
    actual 33. Override = 61/70; default phones = 72/82. Pro-rated to 4h the
    override base is 30 (33 ≥ 30 → base HIT) but the default base is 36 (MISS)."""
    dr = rt.DayResult(role="Phones / Reward", shift_hours=8.0, is_working=True)
    seg_phones = rt.RoleSegment(role="Inbound phones + Webchat", minutes=240,
                                actual=33)
    seg_reward = rt.RoleSegment(role="Reward time (prev week)", minutes=240)
    rt._recalc_segment_targets(seg_phones)   # default 72/82 pro-rated → 36/41
    rt._recalc_segment_targets(seg_reward)   # no target
    dr.segments = [seg_phones, seg_reward]
    dr.target_base = sum(s.target_base for s in dr.segments)
    dr.target_stretch = sum(s.target_stretch for s in dr.segments)
    dr.actual = 33
    return dr, seg_phones


def test_split_day_override_consistent_across_tabs():
    overrides = [{"agent": "Kate", "role": "Inbound phones",
                  "baseline": 61, "stretch": 70}]
    saved = p._OVERRIDES
    p._OVERRIDES = overrides
    try:
        dr, seg_phones = _kate_wednesday_split()

        # Moves & Splits + Split Breakdown both resolve per segment.
        moves_and_splits = p._effective_targets_and_met(seg_phones, "Kate")
        # Data tab now aggregates per segment via the day-level helper.
        data_tab = p._day_effective_targets_and_met(dr, "Kate")

        # Override (not the default) is applied: base 30, stretch 35.
        assert moves_and_splits == (30, 35, True, False), moves_and_splits
        # All tabs agree on the same numbers and the same base HIT.
        assert data_tab == moves_and_splits, (data_tab, moves_and_splits)

        # The Data-tab "Base Met?" string the TL View formula reads is "Yes".
        eb, es, mb, ms = data_tab
        assert p._yes_no(mb, eb) == "Yes"
        assert p._yes_no(ms, es) == "No"
    finally:
        p._OVERRIDES = saved


def test_split_day_without_override_uses_default():
    """Sanity: with no override, the split day still reports the default 36/41."""
    saved = p._OVERRIDES
    p._OVERRIDES = []
    try:
        dr, _ = _kate_wednesday_split()
        assert p._day_effective_targets_and_met(dr, "Kate") == (36, 41, False, False)
    finally:
        p._OVERRIDES = saved


if __name__ == "__main__":
    test_split_day_override_consistent_across_tabs()
    test_split_day_without_override_uses_default()
    print("OK — split-day override tests passed")
