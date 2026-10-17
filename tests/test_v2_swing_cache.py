"""Self-contained exact memo and strict comparison regression tests."""
import csv
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from replay_continuous_v2_optimized import _cache_detector_class, _v2_signature
from compare_replay_exact import compare
import replay_continuous_v1_historical as v1


class FakeDetector:
    actual_calls = 0
    def __init__(self, left_bars=3, right_bars=3, min_prominence_pct=0.0):
        self.left_bars=left_bars
        self.right_bars=right_bars
        self.min_prominence_pct=min_prominence_pct
    def detect_all(self, candles):
        FakeDetector.actual_calls += 1
        return [(x['timestamp'], x['high'], x['low']) for x in candles]


class CacheTests(unittest.TestCase):
    def setUp(self):
        FakeDetector.actual_calls=0
        self.stats={'calls':0,'hits':0,'bypassed':0,'compute_seconds':0.0}
        self.Det=_cache_detector_class(FakeDetector, self.stats)
    def test_identical_window_reused_without_changing_result(self):
        d=self.Det()
        bars=[{'timestamp':i*900000, 'high':float(i), 'low':float(i-1)} for i in range(400)]
        expected=d.detect_all(bars)
        for _ in range(14):
            actual=d.detect_all([dict(x) for x in bars])
            self.assertEqual(actual,expected)
        self.assertEqual(FakeDetector.actual_calls, 1)
        self.assertEqual(self.stats['hits'],14)
    def test_next_15m_close_invalidates(self):
        d=self.Det()
        candles=[{'timestamp':i*900000,'high':i+1,'low':i} for i in range(400)]
        d.detect_all(candles)
        d.detect_all(candles[1:]+[{'timestamp':400*900000,'high':123,'low':122}])
        self.assertEqual(FakeDetector.actual_calls,2)
    def test_parameters_invalidate(self):
        bars=[{'timestamp':1,'high':3,'low':1}]
        self.Det(left_bars=3).detect_all(bars)
        self.Det(left_bars=5).detect_all(bars)
        self.assertEqual(FakeDetector.actual_calls,2)
    def test_returned_list_does_not_mutate_cache(self):
        bars=[{'timestamp':1,'high':3,'low':1}]
        d=self.Det()
        a=d.detect_all(bars)
        a.clear()
        self.assertEqual(len(d.detect_all(bars)),1)
    def test_v2_progress_signature_differs_from_v1(self):
        args=dict(mode='research',start_ms=1,end_ms=2,step_minutes=1,
                  project=Path('/tmp/p'),journal=Path('/tmp/j'),cache_dir=Path('/tmp/c'),
                  symbols=('AAA',),fingerprint='abc')
        self.assertNotEqual(_v2_signature(**args),v1.progress_signature(**args))

class ParityGateTests(unittest.TestCase):
    def test_exact_events_and_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for dirname in ('a','b'):
                p=root/dirname;p.mkdir()
                (p/'summary.json').write_text(json.dumps(dict(mode='research',start_ms=1,end_ms=2,step_minutes=1,canonical_hash='hash',symbols_total=1,symbols_failed=0,data_gaps=0)))
                with (p/'replay_events.csv').open('w',newline='') as fh:
                    w=csv.DictWriter(fh,fieldnames=('event_key','symbol','side','retest_ts','known_ts','first_replay_observed_ms','observation_type','source_hash','causal_json'))
                    w.writeheader();w.writerow(dict(event_key='AAA|LONG|1',symbol='AAA',side='LONG',retest_ts='1',known_ts='60001',first_replay_observed_ms='60001',observation_type='HISTORICAL_REPLAY_UNVERIFIED',source_hash='hash',causal_json='{"room":1}'))
            self.assertTrue(compare(root/'a',root/'b')['strict_parity'])
            raw=(root/'b'/'replay_events.csv').read_text()
            (root/'b'/'replay_events.csv').write_text(raw.replace('60001','60002'))
            self.assertFalse(compare(root/'a',root/'b')['strict_parity'])

if __name__ == '__main__': unittest.main()
