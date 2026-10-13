"""Regression tests for null vs NaN sector fields in V1 parity audits."""
import unittest
import math
from audit_reaction_lab_v1_enrichment import (
    compare_value, compare_values, SECTOR_FIELDS, is_missing,
)


class SectorParityTests(unittest.TestCase):
    def test_null_vs_nan_is_not_applicable_only_for_missing_sector(self):
        for field in SECTOR_FIELDS:
            self.assertEqual(compare_value(field, None, float('nan'), 'SECTOR_UNAVAILABLE'),
                             'NOT_APPLICABLE')
            self.assertEqual(compare_value(field, float('nan'), None, 'SECTOR_UNAVAILABLE'),
                             'NOT_APPLICABLE')

    def test_null_vs_nan_in_ready_is_an_error(self):
        for field in SECTOR_FIELDS:
            self.assertEqual(compare_value(field, None, float('nan'), 'READY'), 'DIFF')

    def test_numeric_difference_is_not_suppressed(self):
        for field in SECTOR_FIELDS:
            self.assertEqual(compare_value(field, None, 1.25, 'SECTOR_UNAVAILABLE'), 'DIFF')
            self.assertEqual(compare_value(field, 1.25, float('nan'), 'SECTOR_UNAVAILABLE'), 'DIFF')
            self.assertEqual(compare_value(field, 1.25, 1.35, 'SECTOR_UNAVAILABLE'), 'DIFF')

    def test_core_missing_remains_a_failure(self):
        self.assertEqual(compare_value('market_breadth_4h', None, float('nan'),
                                       'SECTOR_UNAVAILABLE'), 'DIFF')
        self.assertEqual(compare_value('relative_volume_4h', None, 1.1,
                                       'SECTOR_UNAVAILABLE'), 'DIFF')

    def test_same_valid_sector_values_pass(self):
        for field in SECTOR_FIELDS:
            self.assertEqual(compare_value(field, 41.3793, 41.3793, 'READY'), 'PASS')

    def test_expected_60_event_accounting(self):
        # Emulate 60 full snapshots, 48 with sectors, 12 without sectors.
        fields = [item[0] for item in compare_values({}, {}, 'READY')]
        full = {name: 1.25 for name in fields}
        ref_empty = {**full, **{name: float('nan') for name in SECTOR_FIELDS}}
        stored_empty = {**full, **{name: None for name in SECTOR_FIELDS}}
        outcomes = []
        for _ in range(48):
            outcomes.extend(r[3] for r in compare_values(full, full, 'READY'))
        for _ in range(12):
            outcomes.extend(r[3] for r in compare_values(stored_empty, ref_empty,
                                                         'SECTOR_UNAVAILABLE'))
        self.assertEqual(outcomes.count('NOT_APPLICABLE'), 84)
        self.assertEqual(outcomes.count('DIFF'), 0)
        self.assertEqual(outcomes.count('PASS'), 1116)

    def test_two_missing_core_values_are_not_a_pass(self):
        self.assertEqual(compare_value('btc_return_pct_4h', None, None,
                                       'READY'), 'DIFF')

    def test_missing_detection_does_not_turn_zero_into_null(self):
        self.assertTrue(is_missing(float('nan')))
        self.assertTrue(is_missing(None))
        self.assertFalse(is_missing(0))
        self.assertFalse(is_missing(0.0))
        self.assertFalse(is_missing(-1.2))


if __name__ == '__main__':
    unittest.main()
