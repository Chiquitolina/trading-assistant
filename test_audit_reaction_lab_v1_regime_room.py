"""Focused validation of read-only parity-audit logic, no Redis connection."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
import pandas as pd
from audit_reaction_lab_v1_regime_room import value_equal, read_only, extract_dashboard_function


class ParityAuditTests(unittest.TestCase):
    def test_v1_rounding_and_missing_values(self):
        self.assertTrue(value_equal(1.8986955989,1.8987,0.000055))
        self.assertTrue(value_equal(-2.4614662,-2.4615,0.000055))
        self.assertFalse(value_equal(1.3,1.2,0.000055))
        self.assertFalse(value_equal(None,1.0))
        self.assertTrue(value_equal(None,None))

    def test_source_sqlite_is_read_only(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'db.sqlite3'
            with sqlite3.connect(path) as conn:
                conn.execute('CREATE TABLE t(x INTEGER)')
                conn.execute('INSERT INTO t VALUES (5)')
            with read_only(path) as conn:
                self.assertEqual(conn.execute('SELECT x FROM t').fetchone(),(5,))
                with self.assertRaises(sqlite3.OperationalError):
                    conn.execute('INSERT INTO t VALUES (6)')

    def test_original_room_causality_validator_pass_and_fail(self):
        app=Path(__file__).parent/'dashboard/app.py'
        if not app.is_file():
            self.skipTest('Para probar la función original, ejecutar en la raíz del proyecto')
        f=extract_dashboard_function(app,'_candidate_v2_room_causality_components')
        known=36_000_000
        confirmed=known-7_200_000
        tf=3_600_000
        row={
            'side':'LONG', 'candidate_v1_reaction_known_ts':known,
            'retest_close':100.,'nearest_opposing_room_pct':2.0,
            'nearest_opposing_swing_tf':'1h',
            'nearest_opposing_swing_price':102.,
            'nearest_opposing_swing_pivot_timestamp':confirmed-5*tf,
            'nearest_opposing_swing_confirmed_timestamp':confirmed,
            'nearest_opposing_swing_actionable_timestamp':confirmed+tf,
        }
        result=f(pd.DataFrame([row]))
        self.assertEqual(result.iloc[0]['room_causal_status'],'PASS')
        row['nearest_opposing_swing_actionable_timestamp']=known+60_000
        result=f(pd.DataFrame([row]))
        self.assertEqual(result.iloc[0]['room_causal_status'],'FAIL')

if __name__=='__main__':unittest.main()
