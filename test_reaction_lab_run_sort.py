"""Persisted post-REACTION run labels and descending-order regression tests."""
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from reaction_lab_unified_adapter import (
    observed_reaction_run, attach_observed_reaction_runs,
    sort_retests_by_run, inspector_rows,
)
from reaction_lab_auto_paths import encode_path

MIN = 60_000


def bars(base_ts, values):
    """values = (open, high, low, close) per closed minute."""
    return [(base_ts + (i+1)*MIN, *value) for i,value in enumerate(values)]


class RunMetricTests(unittest.TestCase):
    def test_long_matches_legacy_retest_close_baseline(self):
        data=bars(900_000,[(9,10.2,8.9,9.8),(9.8,10.8,9.5,10)])
        got=observed_reaction_run(data,retest_timestamp=900_000,
                                 retest_close=10,signal='LONG')
        self.assertAlmostEqual(got['reaction_mfe_pct'],8.0)
        self.assertEqual(got['reaction_observed_min'],2)
        # Using the next-1m OPEN would yield 20%, which is NOT Legacy run.
        self.assertNotAlmostEqual(got['reaction_mfe_pct'],20)

    def test_short_matches_favorable_low(self):
        data=bars(900_000,[(10.5,10.6,9.8,10.1),(10.1,10.2,9.1,9.4)])
        got=observed_reaction_run(data,retest_timestamp=900_000,
                                 retest_close=10,signal='SHORT')
        self.assertAlmostEqual(got['reaction_mfe_pct'],9.0)

    def test_never_include_retest_candle_or_missing_first_minute(self):
        data=bars(900_000,[(10,10.5,9.9,10)])
        self.assertIsNone(observed_reaction_run(data,retest_timestamp=840_000,
                                               retest_close=10,signal='LONG'))

    def test_stops_at_first_gap_even_when_later_bar_has_huge_high(self):
        data=[(960_000,10,10.2,9.8,10),(1_080_000,10,50,9,10)]
        got=observed_reaction_run(data,retest_timestamp=900_000,
                                 retest_close=10,signal='LONG')
        self.assertAlmostEqual(got['reaction_mfe_pct'],2)
        self.assertEqual(got['reaction_observed_min'],1)

    def test_no_path_is_not_zero(self):
        self.assertIsNone(observed_reaction_run([],retest_timestamp=900_000,
                                               retest_close=10,signal='LONG'))
        self.assertIsNone(observed_reaction_run(bars(900_000,[(10,11,9,10)]),
                                               retest_timestamp=900_000,
                                               retest_close=None,signal='LONG'))

    def test_true_zero_mfe_keeps_valid_zero(self):
        got=observed_reaction_run(bars(900_000,[(10,9.9,9,9.5)]),
                                 retest_timestamp=900_000,
                                 retest_close=10,signal='LONG')
        self.assertEqual(got['reaction_mfe_pct'],0)

    def test_cap_to_archived_360_bar_horizon(self):
        data=bars(900_000,[(10,10.2,9.9,10)]*360+[(10,100,9.9,10)])
        got=observed_reaction_run(data,retest_timestamp=900_000,
                                 retest_close=10,signal='LONG')
        self.assertAlmostEqual(got['reaction_mfe_pct'],2)
        self.assertEqual(got['reaction_observed_min'],360)

    def test_writes_no_db_and_sorts_descending_missing_last(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            db=root/'reports/reaction_lab/auto_paths_v1.sqlite3'
            db.parent.mkdir(parents=True)
            with sqlite3.connect(db) as con:
                con.execute('CREATE TABLE events (event_key TEXT PRIMARY KEY, count INTEGER, path_blob BLOB)')
                for key,data in (
                    ('ABC|LONG|900000',bars(900_000,[(10,10.5,9.5,10)])),
                    ('DEF|SHORT|900000',bars(900_000,[(10,10.2,9.1,10)])),
                ):
                    con.execute('INSERT INTO events VALUES (?,?,?)',(key,len(data),encode_path(data)))
            frame=pd.DataFrame([
                {'event_key':'ABC|LONG|900000','retest_timestamp':900000,'retest_close':10,'signal':'LONG','symbol':'ABC','swing_price':9},
                {'event_key':'GHI|LONG|920000','retest_timestamp':920000,'retest_close':10,'signal':'LONG','symbol':'GHI','swing_price':9},
                {'event_key':'DEF|SHORT|900000','retest_timestamp':900000,'retest_close':10,'signal':'SHORT','symbol':'DEF','swing_price':11},
            ])
            enriched=attach_observed_reaction_runs(root,frame)
            got=sort_retests_by_run(enriched)
            self.assertEqual(got['event_key'].tolist(),
                             ['DEF|SHORT|900000','ABC|LONG|900000','GHI|LONG|920000'])
            self.assertTrue(pd.isna(got.iloc[-1]['reaction_mfe_pct']))
            adapted=inspector_rows(got)
            self.assertAlmostEqual(adapted['reaction_mfe_pct'].iloc[0],9.0)
            with sqlite3.connect(f'file:{db}?mode=ro',uri=True) as con:
                self.assertEqual(con.execute('SELECT COUNT(*) FROM events').fetchone()[0],2)

    def test_view_plumbs_run_to_shared_legacy_inspector(self):
        code=(HERE/'reaction_lab_research_view.py').read_text()
        self.assertIn('sort_retests_by_run(attach_observed_reaction_runs(project, df))', code)
        self.assertIn('original=inspector_rows(df, tz=cohort_timezone)', code)
        self.assertIn('selected_event=shared_retest_inspector(', code)
        self.assertIn("'reaction_mfe_pct','reaction_observed_min'", code)


if __name__=='__main__':unittest.main()
