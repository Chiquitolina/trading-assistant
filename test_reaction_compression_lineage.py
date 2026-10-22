"""Isolated reproducibility + chronology/coverage tests; no Binance, no LIVE."""
import csv
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

import reaction_compression_lineage as lab

S=lab.TF['15m']


def df15(n=240):
    return pd.DataFrame([{'timestamp':i*S,'open':100,'high':101,'low':99,
                          'close':100,'volume':10.0,'quoteVolume':1000.0} for i in range(n)])


class LineageTests(unittest.TestCase):
    def test_plan_reports_actual_required_hours(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / lab.ROOT_REL
            folder.mkdir(parents=True)
            con = sqlite3.connect(folder / lab.CTX_NAME)
            con.execute("CREATE TABLE candles (timeframe TEXT)")
            con.commit()
            con.close()
            with patch.object(lab, 'integrity', return_value={
                'events': 7631, 'frozen_cohort_sha256': 'abc123'
            }):
                plan = lab.plan(root)
            actual = {
                tf: [item['required_hours'] for item in values]
                for tf, values in plan['windows'].items()
            }
            self.assertEqual(actual, {
                '15m': [15.0, 22.5, 30.0, 45.0, 60.0],
                '30m': [60.0, 90.0, 120.0],
            })

    def test_30m_from_closed_contiguous_15m_only(self):
        df=df15(8)
        r=lab.derive_30m(df)
        self.assertEqual(list(r.timestamp),[0,2*S,4*S,6*S])
        bad=df[df.timestamp!=S]
        self.assertNotIn(0,list(lab.derive_30m(bad).timestamp))

    def test_closed_snapshot_and_strict_missing_history(self):
        d=df15().set_index('timestamp')
        self.assertIsNone(lab.validate_slice(d,110*S,60,lab.TF['30m']))
        good=lab.validate_slice(d,110*S,60,S)
        self.assertEqual(len(good),60)
        self.assertEqual(int(good.timestamp.iloc[-1]),110*S)
        withgap=d.drop(index=100*S)
        self.assertIsNone(lab.validate_slice(withgap,110*S,60,S))

    def test_calls_existing_scanner_and_filters_with_nonoverlapping_base(self):
        import sys
        import types
        mod=types.ModuleType('dashboard.services.compression_market_scanner_service')
        calls=[]
        def scan(histories,**kw):
            calls.append(('scan',kw))
            self.assertEqual(list(histories),['XUSDT'])
            return pd.DataFrame([{'high':101.0,'low':99.0,'end_ts':90*S+S-1,
                'start_ts':61*S,'readiness_score':71.0}]),{'scanned':1}
        def filt(raw,**kw):
            calls.append(('filter',kw));return raw
        mod.scan_compression_market=scan
        mod.filter_compression_market=filt
        with patch.dict(sys.modules,{'dashboard.services.compression_market_scanner_service':mod}):
            out=lab.scan_snapshot(df15().iloc[10:100].copy(),'XUSDT','15m',30,S)
        self.assertEqual(out['readiness_score'],71.0)
        self.assertEqual(calls[0][1]['lookback'],30)
        self.assertEqual(calls[0][1]['base_lookback'],60)
        self.assertEqual(calls[0][1]['timeframe_ms'],S)
        self.assertFalse(calls[1][1]['only_near_wall'])

    def test_first_breakout_must_come_after_box_and_before_retest(self):
        d=df15(105)
        d.loc[d.timestamp==94*S,'close']=103
        d.loc[d.timestamp==94*S,'high']=103
        d.loc[d.timestamp==95*S,'close']=104
        d.loc[d.timestamp==95*S,'high']=104
        r=100*S
        e={'symbol':'TESTUSDT','side':'LONG','retest_ts':r}
        box={'high':101,'low':99,'end_ts':(92*S)+S-1,
             'start_ts':(50*S),'readiness_score':85}
        observed=lab.classify_box(e,box,d.set_index('timestamp'),100.9,
                                  (r,101,101.2,100.9,101),S)
        self.assertEqual(observed['lineage_state'],'BROKEN_RETEST_KNOWN')
        self.assertEqual(observed['breakout_ts'],94*S)
        self.assertLess(observed['breakout_ts'],r)
        modified=d.copy()
        modified.loc[modified.timestamp>=r,'close']=9999
        modified.loc[modified.timestamp>=r,'high']=10000
        still=lab.classify_box(e,box,modified.set_index('timestamp'),100.9,
                               (r,101,101.2,100.9,101),S)
        self.assertEqual(still['breakout_ts'],94*S)
        self.assertEqual(still['lineage_state'],observed['lineage_state'])

    def test_first_opposite_break_blocks_directional_match(self):
        d=df15(105)
        d.loc[d.timestamp==93*S,['close','low']]=[97,97]
        d.loc[d.timestamp==94*S,['close','high']]=[103,103]
        e={'symbol':'TESTUSDT','side':'LONG','retest_ts':100*S}
        b={'high':101,'low':99,'end_ts':92*S+S-1}
        z=lab.classify_box(e,b,d.set_index('timestamp'),100,
                           (100*S,100,101,100.9,100),S)
        self.assertEqual(z['lineage_state'],'OPPOSITE_FIRST')

    def test_search_grid_never_ends_at_or_after_retest(self):
        r=200*S+5*lab.M
        times=lab.candidate_snapshots(r,S,8)
        self.assertTrue(times)
        self.assertTrue(all(t+S-1<r for t in times))
        self.assertEqual(len(times),len(set(times)))

    def test_no_coverage_is_not_no_compression(self):
        e={'event_key':'X|LONG|900','symbol':'X','side':'LONG','retest_ts':900,
           'date_utc':'2026-10-21','observation_type':'LIVE_OBSERVED'}
        row=lab._full_row(e,'30m',80,0,0,None,'AGGREGATED_15M_PROXY','INSUFFICIENT_HISTORY')
        self.assertEqual(row['lineage_state'],'INSUFFICIENT_HISTORY')
        self.assertNotEqual(row['lineage_state'],'NO_QUALIFIED_BOX')

    def test_end_to_end_pilot_writes_isolated_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);folder=root/lab.ROOT_REL;folder.mkdir(parents=True)
            path=folder/lab.CTX_NAME
            con=sqlite3.connect(path)
            con.execute('CREATE TABLE candles(symbol TEXT,timeframe TEXT,open_ts INTEGER,open REAL,high REAL,low REAL,close REAL,volume REAL,quote_volume REAL)')
            con.execute('CREATE TABLE events(event_key TEXT,symbol TEXT,side TEXT,retest_ts INTEGER,observation_type TEXT)')
            r=200*S
            con.execute('INSERT INTO events VALUES (?,?,?,?,?)',(f'XUSDT|LONG|{r}','XUSDT','LONG',r,'LIVE_OBSERVED'))
            for bar in df15(201).itertuples(index=False):
                con.execute('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?)',('XUSDT','15m',bar.timestamp,bar.open,bar.high,bar.low,bar.close,bar.volume,bar.quoteVolume))
            con.execute('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?)',('XUSDT','1m',r-lab.M,100,100,100,100,0,0))
            con.execute('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?)',('XUSDT','1m',r,100,101.2,100.9,101,0,0))
            con.commit();con.close()
            fakeaudit={'events':7631,'frozen_cohort_sha256':'abc123'}
            def mockscan(cut,symbol,tf,n,tfms):
                if n==20:return {'high':101,'low':99,'start_ts':int(cut.timestamp.iloc[-n]),
                      'end_ts':int(cut.timestamp.iloc[-1])+tfms-1,'readiness_score':80,
                      'range_ratio':.6,'atr_ratio':.8,'volume_ratio':.8,
                      'compression_height_pct':1.8,'touches_high':3,'touches_low':3,
                      'compression_shape':'horizontal_range','micro_range_ratio':.7}
                return None
            with patch.object(lab,'integrity',return_value=fakeaudit),patch.object(lab,'scan_snapshot',side_effect=mockscan):
                out=lab.build(root,max_events=1,search_hours=8,progress=0)
                self.assertEqual(out['rows'],8)
                aud=lab.audit_output(root)
                self.assertTrue(aud['success'],aud)
            with (folder/lab.OUT_NAME/'lineage_structures.csv').open() as file:
                rows=list(csv.DictReader(file))
            self.assertEqual({(x['timeframe'],x['window']) for x in rows},
                             {('15m','20'),('15m','30'),('15m','40'),('15m','60'),('15m','80'),('30m','40'),('30m','60'),('30m','80')})
            self.assertFalse(out['complete_universe'])


if __name__=='__main__':unittest.main()

# Regression: the Binance 1m SQL tuple is (timestamp,open,HIGH,LOW,close).
# LONG retest must use LOW at index 3; SHORT retest must use HIGH at index 2.
class RetestWickIndexRegression(unittest.TestCase):
    def test_long_retest_uses_low_not_high(self):
        event={'symbol':'X','side':'LONG','retest_ts':100*S}
        bar={'high':101,'low':99,'end_ts':92*S+S-1}
        candles=df15(105).set_index('timestamp')
        candles.loc[94*S,'close']=103.;candles.loc[94*S,'high']=103.
        got=lab.classify_box(event,bar,candles,101.2,(100*S,101.1,103.,100.95,101.5),S)
        self.assertEqual(got['lineage_state'],'BROKEN_RETEST_KNOWN')
        self.assertAlmostEqual(got['known_wall_distance_pct'],100*(100.95/101-1))
    def test_short_retest_uses_high_not_open(self):
        event={'symbol':'X','side':'SHORT','retest_ts':100*S}
        bar={'high':101,'low':99,'end_ts':92*S+S-1}
        candles=df15(105).set_index('timestamp')
        candles.loc[94*S,'close']=97.;candles.loc[94*S,'low']=97.
        got=lab.classify_box(event,bar,candles,98.8,(100*S,97.,99.05,97.,98.5),S)
        self.assertEqual(got['lineage_state'],'BROKEN_RETEST_KNOWN')
        self.assertAlmostEqual(got['known_wall_distance_pct'],100*(99.05/99-1))
