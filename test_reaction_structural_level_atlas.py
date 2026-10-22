import unittest
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent))
import numpy as np
import pandas as pd
import reaction_structural_level_atlas as m

M=60_000
S=15*M


def candles(n=110,step=S,base=100.):
    # high pivots at i=20,30,40 (confirmed i+3), then long accepted above 102.
    vals=[]
    for i in range(n):
        c=base + .5*np.sin(2*np.pi*i/10)
        if i>=76: c=103.0 + .05*np.sin(i)
        h=c+.4;l=c-.4
        if i in (20,30,40): h=102.0
        vals.append([i*step,c,h,l,c,1000.])
    return pd.DataFrame(vals,columns=['timestamp','open','high','low','close','volume'])


def e(t,side='LONG'):
    return dict(event_key=f'TEST|{side}|{t}',symbol='TESTUSDT',side=side,
                date_utc='2026-10-12',observation_type='LIVE_OBSERVED',retest_ts=t)

class StructuralAtlasTests(unittest.TestCase):
    def test_pivot_not_known_until_right_candles_close(self):
        d=candles(24)
        p=m.pivots_confirmed(d)
        self.assertTrue(any(x['pivot_i']==20 and x['confirm_i']==23 for x in p if x['kind']=='HIGH'))
        self.assertFalse(any(x['pivot_i']>=21 for x in p))
        d=candles(23)
        self.assertFalse(any(x['pivot_i']==20 for x in m.pivots_confirmed(d)))

    def test_zone_frozen_from_two_confirmed_pivots(self):
        d=candles()
        p=m.pivots_confirmed(d)
        zones=m.frozen_zones(d,p,'HIGH',S)
        self.assertTrue(zones)
        a=zones[0]
        self.assertLess(a['formation_closed_ts'],d.timestamp.iloc[-1]+S)
        self.assertEqual(a['pivot_count'],2)
        # changing bars after formation cannot rewrite frozen anchor
        altered=d.copy()
        altered.loc[altered.index>a['form_i'],'close']=200
        p2=m.pivots_confirmed(altered)
        # direct candidate stored already anchored, immutable
        self.assertAlmostEqual(a['center'],(102.+102.)/2)

    def test_no_retest_lookahead_pre_selection(self):
        d=candles(110)
        event=e(110*S)
        a=m.scan_event(event,'15m',d,'NATIVE_15M',102,[110*S,102,105,101,104])
        b=m.scan_event(event,'15m',d,'NATIVE_15M',102,[110*S,102,105,90,95])
        self.assertEqual(a['state_pre'],b['state_pre'])
        self.assertEqual(a['level_price'],b['level_price'])
        self.assertEqual(a['formation_closed_ts'],b['formation_closed_ts'])
        self.assertNotEqual(a['state_known'],b['state_known'])

    def test_future_bars_dont_change_event(self):
        d=candles(120)
        event=e(110*S)
        a=m.scan_event(event,'15m',d,'NATIVE_15M',None,None)
        changed=d.copy()
        changed.loc[changed.timestamp>=110*S,['open','high','low','close']]=500
        b=m.scan_event(event,'15m',changed,'NATIVE_15M',None,None)
        self.assertEqual(a['state_pre'],b['state_pre'])
        self.assertEqual(a['level_price'],b['level_price'])

    def test_unknown_coverage_not_no_level(self):
        d=candles(30)
        r=m.scan_event(e(30*S),'15m',d,'NATIVE_15M',None,None)
        self.assertEqual(r['coverage'],'INSUFFICIENT')
        self.assertEqual(r['state_pre'],'UNKNOWN_HISTORY')

    def test_gap_not_absence(self):
        d=candles(110).drop(index=70)
        r=m.scan_event(e(110*S),'15m',d,'NATIVE_15M',None,None)
        self.assertEqual(r['coverage'],'GAPPED')
        self.assertEqual(r['state_pre'],'UNKNOWN_HISTORY')

    def test_long_short_retest_wicks(self):
        z=dict(level_price=100,zone_low=99.8,zone_high=100.2)
        l=m.classify_known(z,'LONG',[0,105,110,100.1,103])
        s=m.classify_known(z,'SHORT',[0,95,99.9,90,98])
        self.assertTrue(l['retest_touch']);self.assertTrue(l['retest_hold'])
        self.assertTrue(s['retest_touch']);self.assertTrue(s['retest_hold'])

    def test_best_row_outcome_blind(self):
        t=110*S;event=e(t)
        rows=[m.scan_event(event,'15m',candles(110),'NATIVE_15M',None,None),
              m.scan_event(event,'30m',candles(30,2*S),'NATIVE_30M',None,None)]
        result=m.best_row(event,rows)
        self.assertEqual(result['event_key'],event['event_key'])
        self.assertEqual(result['coverage_all'],0)
        self.assertNotIn('outcome',result)

    def test_no_output_state_lookahead(self):
        # pre-snapshot never contains TP/SL, even if operator adds an outcome field
        x=e(110*S);x['outcome']='TP_FIRST'
        y=e(110*S);y['outcome']='SL_FIRST'
        a=m.scan_event(x,'15m',candles(110),'NATIVE_15M',None,None)
        b=m.scan_event(y,'15m',candles(110),'NATIVE_15M',None,None)
        self.assertEqual(a,b)

    def test_end_to_end_isolated_outputs_and_audit(self):
        import sqlite3
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);folder=root/m.lineage.ROOT_REL
            folder.mkdir(parents=True)
            db=folder/m.lineage.CTX_NAME
            con=sqlite3.connect(db)
            con.execute('CREATE TABLE candles(symbol TEXT,timeframe TEXT,open_ts INTEGER,open REAL,high REAL,low REAL,close REAL,volume REAL,quote_volume REAL)')
            con.execute('CREATE TABLE events(event_key TEXT,symbol TEXT,side TEXT,retest_ts INTEGER,observation_type TEXT)')
            retest=305*30*M
            con.execute('INSERT INTO events VALUES(?,?,?,?,?)',('XUSDT|LONG|'+str(retest),'XUSDT','LONG',retest,'LIVE_OBSERVED'))
            for tf,step,n in [('15m',15*M,630),('30m',30*M,305)]:
                d=candles(n,step)
                rows=[('XUSDT',tf,int(z.timestamp),float(z.open),float(z.high),float(z.low),float(z.close),1000.,100_000.) for z in d.itertuples(index=False)]
                con.executemany('INSERT INTO candles VALUES(?,?,?,?,?,?,?,?,?)',rows)
            con.execute('INSERT INTO candles VALUES(?,?,?,?,?,?,?,?,?)',('XUSDT','1m',retest-M,102.,103.,101.,102.,1000.,100_000.))
            con.execute('INSERT INTO candles VALUES(?,?,?,?,?,?,?,?,?)',('XUSDT','1m',retest,102.,103.,101.,102.,1000.,100_000.))
            con.commit();con.close()
            audit={'events':7631,'frozen_cohort_sha256':'synthetic'}
            before=m.lineage.hash_file(db)
            with patch.object(m.lineage,'integrity',return_value=audit):
                result=m.run(root,max_events=1,cohort='live-stratified')
                check=m.audit_output(root)
            self.assertEqual(result['events'],1)
            self.assertEqual(result['rows'],2)
            self.assertTrue(check['success'])
            self.assertEqual(m.lineage.hash_file(db),before)

if __name__=='__main__':unittest.main()
