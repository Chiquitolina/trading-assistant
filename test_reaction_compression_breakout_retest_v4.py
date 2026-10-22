"""Synthetic chronology, hold, and no-lookahead checks; no Binance or LIVE."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd
import numpy as np

import reaction_compression_breakout_retest_v4 as v4

S=v4.v3.TF['15m'];M=v4.M


def events():
    return [('XUSDT|LONG|1','XUSDT','LONG',300*S,'LIVE_OBSERVED'),
            ('YUSDT|SHORT|2','YUSDT','SHORT',301*S,'LIVE_OBSERVED'),
            ('ZUSDT|LONG|3','ZUSDT','LONG',302*S,'RECOVERED_WINDOW')]

class V4Tests(unittest.TestCase):
    def test_stratified_selection_is_only_live_and_never_reads_outcomes(self):
        with sqlite3.connect(':memory:') as con:
            con.execute('CREATE TABLE events(event_key TEXT,symbol TEXT,side TEXT,retest_ts INTEGER,observation_type TEXT)')
            con.executemany('INSERT INTO events VALUES(?,?,?,?,?)',events())
            a=v4.select_events(con,'live-stratified',0)
            self.assertEqual(len(a),2)
            self.assertEqual({x['side'] for x in a},{'LONG','SHORT'})
            self.assertFalse(any(x['observation_type']=='RECOVERED_WINDOW' for x in a))
            self.assertEqual(a,v4.select_events(con,'live-stratified',0))

    def test_all_closed_snapshot_search_not_sparse(self):
        s=v4.full_causal_snapshots(200*S,S,12,1)
        self.assertGreater(len(s),15)
        self.assertTrue(all((x+S)<200*S for x in s))
        self.assertTrue(all(a-b==S for a,b in zip(s,s[1:])))
        sparse=v4.full_causal_snapshots(200*S,S,12,4)
        self.assertLess(len(sparse),len(s))

    def test_age_buckets_edges(self):
        self.assertEqual(v4.age_bucket(180),'FRESH_0_3H')
        self.assertEqual(v4.age_bucket(181),'FRESH_3_6H')
        self.assertEqual(v4.age_bucket(361),'INTERMEDIATE_6_12H')
        self.assertEqual(v4.age_bucket(1441),'STALE_OVER_24H')

    def test_first_retest_requires_unbroken_one_minute_path(self):
        breakout=100*M;r=110*M
        idx=np.arange(breakout,r,M,dtype='int64')
        d=pd.DataFrame({'timestamp':idx,'high':[102.]*len(idx),'low':[101.8]*len(idx)}).set_index('timestamp')
        z=v4.prior_minute_touches(d,breakout,r,101,'LONG')
        self.assertEqual(z[0],'VERIFIED_FIRST')
        wrong=d.copy();wrong.loc[104*M,'low']=101.1
        self.assertEqual(v4.prior_minute_touches(wrong,breakout,r,101,'LONG')[0],'PREVIOUS_TOUCH')
        self.assertEqual(v4.prior_minute_touches(d.drop(index=107*M),breakout,r,101,'LONG')[0],'UNKNOWN_GAP')
        short=d.copy();short['low']=98;short['high']=98.2
        self.assertEqual(v4.prior_minute_touches(short,breakout,r,99,'SHORT')[0],'VERIFIED_FIRST')
        short.loc[105*M,'high']=98.85
        self.assertEqual(v4.prior_minute_touches(short,breakout,r,99,'SHORT')[0],'PREVIOUS_TOUCH')

    def test_retest_candle_changes_known_but_never_pre_box(self):
        e=dict(symbol='XUSDT',side='LONG',retest_ts=220*S)
        idx=np.arange((220*S)-17*M,220*S,M,dtype='int64')
        m1=pd.DataFrame({'timestamp':idx,'open':102.,'high':102.2,'low':101.8,'close':102.}).set_index('timestamp')
        df=pd.DataFrame({'timestamp':[i*S for i in range(225)],'open':100.,'high':101.,'low':99.,'close':100.,'quoteVolume':1000.}).set_index('timestamp')
        selected=dict(wall=101,age_break_min=16,breakout_ts=219*S-0*S,
            breakout_closed_ts=219*S,box_last_close_ts=216*S+S-1,
            pre_wall_distance_pct=.2,readiness_score=85)
        prechosen=v4.choose_prior_box([selected])
        good=v4.annotate(e,selected,(e['retest_ts'],101.2,102,100.9,101.4),m1,df)
        bad=v4.annotate(e,selected,(e['retest_ts'],101.2,102,100.9,100.4),m1,df)
        self.assertEqual(good['state_pre'],bad['state_pre'])
        self.assertEqual(good['state_known'],'FIRST_RETEST_HELD')
        self.assertEqual(bad['state_known'],'RETEST_FAILED_HOLD')
        self.assertIs(prechosen,selected)

    def test_pre_breakout_never_uses_opposite_first(self):
        tf=S;df=pd.DataFrame([{'timestamp':i*tf,'open':100.,'high':101.,'low':99.,'close':100.,'quoteVolume':1000.} for i in range(250)]).set_index('timestamp')
        df.loc[222*tf,['low','close']]=[97,97]
        df.loc[223*tf,['high','close']]=[103,103]
        stage,bt,bc=v4.first_break(df,220*tf,tf,230*tf,101,99,'LONG')
        self.assertEqual(stage,'OPPOSITE_FIRST')
        self.assertEqual(bt,222*tf)

    def test_end_to_end_output_audits_and_does_not_touch_sources(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);folder=root/v4.v3.ROOT_REL;folder.mkdir(parents=True)
            path=folder/v4.v3.CTX_NAME
            con=sqlite3.connect(path)
            con.execute('CREATE TABLE candles(symbol TEXT,timeframe TEXT,open_ts INTEGER,open REAL,high REAL,low REAL,close REAL,volume REAL,quote_volume REAL)')
            con.execute('CREATE TABLE events(event_key TEXT,symbol TEXT,side TEXT,retest_ts INTEGER,observation_type TEXT)')
            e=('XUSDT|LONG|'+str(300*S),'XUSDT','LONG',300*S,'LIVE_OBSERVED')
            con.execute('INSERT INTO events VALUES(?,?,?,?,?)',e)
            for i in range(305):
                price=103. if i==298 else 100.
                con.execute('INSERT INTO candles VALUES(?,?,?,?,?,?,?,?,?)',('XUSDT','15m',i*S,100.,max(101.,price),99.,price,10.,1000.))
            for t in range(298*S+S,300*S,M):
                con.execute('INSERT INTO candles VALUES(?,?,?,?,?,?,?,?,?)',('XUSDT','1m',t,102.,102.5,101.8,102.,10.,1000.))
            con.execute('INSERT INTO candles VALUES(?,?,?,?,?,?,?,?,?)',('XUSDT','1m',300*S-M,102.,102.5,101.8,102.,10.,1000.))
            con.execute('INSERT INTO candles VALUES(?,?,?,?,?,?,?,?,?)',('XUSDT','1m',300*S,101.2,101.5,100.9,101.3,10.,1000.))
            con.commit();con.close()
            fakeaudit={'events':7631,'frozen_cohort_sha256':'abc'}
            def fake_scan(cut,symbol,tf,n,ms):
                end=int(cut.timestamp.iloc[-1])
                if tf=='15m' and n==20 and end==296*S:
                    return dict(start_ts=(end-19*S),end_ts=end+S-1,high=101.,low=99.,readiness_score=80.,
                        range_ratio=.5,atr_ratio=.7,volume_ratio=.7,compression_height_pct=2.,compression_shape='horizontal_range')
                return None
            fakeout=pd.DataFrame([dict(event_key=e[0],result_180m='TP_FIRST',entry_mode='OBSERVED_OPEN',entry_delay_min=1)]).set_index('event_key')
            before=path.stat().st_size
            with patch.object(v4.v3,'integrity',return_value=fakeaudit),patch.object(v4.v3,'scan_snapshot',side_effect=fake_scan),patch.object(v4,'_load_outcomes',return_value=(fakeout,{'frozen_cohort_sha256':'abc'})):
                out=v4.run(root,1,'all-live',6,1,0)
                self.assertEqual(out['events'],1)
                self.assertEqual(out['rows'],5)
                self.assertTrue(v4.audit_output(root)['success'])
            self.assertEqual(path.stat().st_size,before)
            import csv
            with (folder/v4.OUTPUT/'event_best_v4.csv').open() as f:rows=list(csv.DictReader(f))
            self.assertEqual(len(rows),1)
            self.assertEqual(rows[0]['outcome'],'TP_FIRST')
            with (folder/v4.OUTPUT/'structures_v4.csv').open() as f:s=list(csv.DictReader(f))
            self.assertEqual(len(s),5)
            self.assertEqual(next(x for x in s if x['timeframe']=='15m' and x['window']=='20')['breakout_ts'],str(298*S))

if __name__=='__main__':unittest.main()
