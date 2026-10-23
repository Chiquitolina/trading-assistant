import unittest
from audit_pre_reaction_pilot_parity_v1 import compare_native_15m, reaction_journal_comparison, price_close

class ParityTests(unittest.TestCase):
    def test_price_float_roundoff(self):
        self.assertTrue(price_close(0.012345678,0.01234568,'open'))
    def test_price_big_mismatch(self):
        self.assertFalse(price_close(0.0123,0.0133,'open'))
    def test_volume_roundoff(self):
        self.assertTrue(price_close(100.0001,100.0,'volume'))
    def test_native_missing_not_mismatch(self):
        agg=[dict(timestamp=900000,open=1.,high=2.,low=.5,close=1.5,volume=7.)]
        self.assertEqual(compare_native_15m(agg,[])[0]['status'],'MISSING_NATIVE_15M')
    def test_native_match(self):
        agg=[dict(timestamp=900000,open=1.,high=2.,low=.5,close=1.5,volume=7.)]
        native=[dict(open_ts=900000,open=1.,high=2.,low=.5,close=1.5,volume=7.)]
        self.assertEqual(compare_native_15m(agg,native)[0]['status'],'MATCH')
    def test_native_mismatch_fields(self):
        agg=[dict(timestamp=900000,open=1.,high=2.,low=.5,close=1.5,volume=7.)]
        native=[dict(open_ts=900000,open=1.,high=3.,low=.5,close=1.5,volume=7.)]
        r=compare_native_15m(agg,native)[0]
        self.assertEqual((r['status'],r['bad_fields']),('MISMATCH','high'))
    def test_scanner_exact(self):
        opp=[dict(status='REACTION',first_touch_ts=12,symbol='AAA',side='LONG',opportunity_key='a')]
        seen=[dict(symbol='AAA',side='LONG',retest_ts=12)]
        self.assertEqual(reaction_journal_comparison(opp,seen)[0]['scanner_status'],'EXACT_SCANNER_MATCH')
    def test_scanner_missing_not_failure(self):
        opp=[dict(status='REACTION',first_touch_ts=12,symbol='AAA',side='LONG',opportunity_key='a')]
        self.assertEqual(reaction_journal_comparison(opp,[])[0]['scanner_status'],'NOT_IN_OBSERVED_SCANNER_JOURNAL')
    def test_non_reaction_excluded(self):
        opp=[dict(status='TOUCH_FAILED',first_touch_ts=12,symbol='AAA',side='LONG',opportunity_key='a')]
        self.assertEqual(reaction_journal_comparison(opp,[]),[])
    def test_other_symbol_never_matched(self):
        opp=[dict(status='REACTION',first_touch_ts=12,symbol='AAA',side='LONG',opportunity_key='a')]
        seen=[dict(symbol='BBB',side='LONG',retest_ts=12)]
        self.assertNotEqual(reaction_journal_comparison(opp,seen)[0]['scanner_status'],'EXACT_SCANNER_MATCH')

if __name__ == '__main__':unittest.main()

class EndToEndTest(unittest.TestCase):
    def test_local_report_and_audit(self):
        import json, tempfile
        from pathlib import Path
        import pandas as pd
        import sqlite3
        from audit_pre_reaction_pilot_parity_v1 import run,audit
        from pre_reaction_opportunity_lab import sha
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            pilot=root/'pilot';pilot.mkdir()
            cache=Path('cache');(root/cache/'AAA').mkdir(parents=True)
            start=1791763200000  # arbitrary UTC-aligned timestamp
            bars=[dict(timestamp=start+i*60000,open=1.,high=1.1,low=.9,close=1.,volume=5.) for i in range(15)]
            p=root/cache/'AAA'/'2026-10.pkl.gz'
            pd.DataFrame(bars).to_pickle(p,compression='gzip')
            frame=pd.DataFrame([dict(opportunity_key='AAA|SHORT|1|2',symbol='AAA',side='SHORT',status='REACTION',first_touch_ts=start)])
            f=pilot/'opportunities_PRE.csv';frame.to_csv(f,index=False)
            (pilot/'manifest.json').write_text(json.dumps({'start_utc':'2026-10-12T00:00:00+00:00',
              'end_exclusive_utc':'2026-10-13T00:00:00+00:00','symbols':['AAA'],
              'opportunities_sha256':sha(f),'source_hashes':{str(p):sha(p)}}))
            db_dir=root/'reports/reaction_lab';db_dir.mkdir(parents=True)
            with sqlite3.connect(db_dir/'reaction_full_context_v1.sqlite3') as con:
                con.execute('CREATE TABLE candles(symbol TEXT,timeframe TEXT,open_ts INTEGER,open REAL,high REAL,low REAL,close REAL,volume REAL)')
                con.execute("INSERT INTO candles VALUES ('AAA','15m',?,?,?,?,?,?)",(start,1.,1.1,.9,1.,75.))
            with sqlite3.connect(db_dir/'scanner_events_v1.sqlite3') as con:
                con.execute('CREATE TABLE events(symbol TEXT,side TEXT,retest_ts INTEGER,observation_type TEXT)')
                con.execute("INSERT INTO events VALUES ('AAA','SHORT',?,'LIVE_OBSERVED')",(start,))
            out=root/'out'
            run(root,pilot,out,cache)
            audit(out)
            m=json.loads((out/'parity_report.json').read_text())
            self.assertEqual(m['15m_by_symbol'][0]['match'],1)
            self.assertEqual(m['exact_matches'],1)
