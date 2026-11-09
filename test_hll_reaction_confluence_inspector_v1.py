import hashlib
import json
import tempfile
import unittest
from pathlib import Path
import pandas as pd
from hll_reaction_confluence_inspector_v1 import (
    load_candidates,event_context,focus_interval,interval_candles)

class ConfluenceInspectorV1(unittest.TestCase):
    def make(self,path):
        rows=[dict(touch_key='BTC-1',symbol='BTCUSDT',level_id='L1',
                   formation_closed_ts=60_000,zone_low=99.,zone_high=101.,
                   center=100.,gap_atr=.1,prior_tests=2,prior_defenses=1,
                   prior_losses=0,prior_returns=0,origin='LOW'),
              dict(touch_key='BTC-1',symbol='BTCUSDT',level_id='L2',
                   formation_closed_ts=90_000,zone_low=103.,zone_high=105.,
                   center=104.,gap_atr=.8,prior_tests=0,prior_defenses=0,
                   prior_losses=0,prior_returns=0,origin='HIGH')]
        f=path/'hll_candidates_near_1atr.csv'
        pd.DataFrame(rows).to_csv(f,index=False)
        h=hashlib.sha256(f.read_bytes()).hexdigest()
        (path/'audit.json').write_text(json.dumps(dict(
            version='universal-runner-hll-lifecycle-join-v1.0',
            no_post_outcomes_read=True,read_only_to_project=True,
            source_hashes={f.name:h})))
    def event(self):
        return pd.Series(dict(touch_key='BTC-1',known_ts=120_000,hll_near_05atr_count=1))
    def test_verified_candidates_and_pre(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);self.make(p)
            allrows=load_candidates(p)
            rows,near=event_context(self.event(),allrows)
            self.assertEqual(len(rows),2)
            self.assertEqual(near.level_id.tolist(),['L1'])
    def test_tamper_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);self.make(p)
            with (p/'hll_candidates_near_1atr.csv').open('a') as f:f.write('x\n')
            with self.assertRaises(ValueError):load_candidates(p)
    def test_future_level_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);self.make(p)
            rows=load_candidates(p)
            rows.loc[0,'formation_closed_ts']=120_000
            with self.assertRaises(ValueError):event_context(self.event(),rows)
    def test_missing_candidate_count_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);self.make(p)
            rows=load_candidates(p)
            event=self.event();event['hll_near_05atr_count']=2
            with self.assertRaises(ValueError):event_context(event,rows)
    def test_pre_hidden_vs_post_revealed(self):
        kw=dict(event_ms=10*3_600_000,pre_hours=2,post_hours=4,
                cutoff_ms=15*3_600_000,available_start=0,available_end=20*3_600_000)
        pre=focus_interval(reveal_post=False,**kw)
        post=focus_interval(reveal_post=True,**kw)
        self.assertEqual(pre,(8*3_600_000,10*3_600_000))
        self.assertEqual(post,(8*3_600_000,14*3_600_000))
        self.assertLess(pre[1],post[1])
    def test_future_cutoff_rejected(self):
        with self.assertRaises(ValueError):
            focus_interval(500_000,pre_hours=2,post_hours=2,
                reveal_post=False,cutoff_ms=400_000,available_start=0,
                available_end=600_000)
    def test_real_aggregation_and_no_future_partial_bar(self):
        bars=pd.DataFrame([dict(timestamp=i*60_000,open=100+i,high=102+i,
              low=99+i,close=101+i) for i in range(30)])
        out=interval_candles(bars,first=0,end=13*60_000,timeframe='5m',source_minute=1)
        self.assertEqual(len(out),2)
        self.assertEqual(out.timestamp.tolist(),[0,300_000])
        self.assertEqual(float(out.high.iloc[0]),106.)
        self.assertEqual(float(out.low.iloc[0]),99.)
        self.assertEqual(float(out.close.iloc[1]),110.)
    def test_reject_partial_and_too_many(self):
        bars=pd.DataFrame([dict(timestamp=i*60_000,open=1.,high=2.,low=.5,close=1.)
                           for i in range(30) if i!=6])
        out=interval_candles(bars,first=0,end=15*60_000,timeframe='5m',source_minute=1)
        self.assertEqual(out.timestamp.tolist(),[0,600_000])
        with self.assertRaises(ValueError):
            interval_candles(bars,first=0,end=30*60_000,timeframe='1m',
                             source_minute=1,max_bars=5)

if __name__=='__main__':unittest.main()
