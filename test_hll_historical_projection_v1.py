import unittest
from hll_historical_projection_v1 import projection_segments

class HistoricalProjectionTests(unittest.TestCase):
    def test_continuous_then_extension(self):
        self.assertEqual(projection_segments(birth_ms=10,end_ms=50,first_ms=20,
            cutoff_ms=100,enabled=True), ((20,50),(50,100)))

    def test_outside_window_expired_before_start(self):
        self.assertEqual(projection_segments(birth_ms=10,end_ms=50,first_ms=75,
            cutoff_ms=100,enabled=True), (None,(75,100)))

    def test_original_only_when_disabled(self):
        self.assertEqual(projection_segments(birth_ms=10,end_ms=50,first_ms=20,
            cutoff_ms=100,enabled=False), ((20,50),None))

    def test_active_level_no_projection(self):
        self.assertEqual(projection_segments(birth_ms=10,end_ms=110,first_ms=20,
            cutoff_ms=100,enabled=True), ((20,100),None))

    def test_future_birth_not_displayed(self):
        self.assertEqual(projection_segments(birth_ms=120,end_ms=150,first_ms=20,
            cutoff_ms=100,enabled=True), (None,None))

    def test_zero_length_lifetime_can_be_history(self):
        self.assertEqual(projection_segments(birth_ms=30,end_ms=30,first_ms=20,
            cutoff_ms=100,enabled=True), (None,(30,100)))

    def test_no_render_after_cutoff(self):
        o,p=projection_segments(birth_ms=5,end_ms=75,first_ms=15,
            cutoff_ms=100,enabled=True)
        self.assertEqual(p[1],100)
        self.assertEqual(o[1],75)

    def test_invalid_end_before_birth(self):
        with self.assertRaises(ValueError):
            projection_segments(birth_ms=90,end_ms=50,first_ms=20,
                cutoff_ms=100,enabled=True)

    def test_invalid_chart_interval(self):
        with self.assertRaises(ValueError):
            projection_segments(birth_ms=10,end_ms=20,first_ms=100,
                cutoff_ms=100,enabled=True)

if __name__ == '__main__':
    unittest.main()
