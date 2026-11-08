"""Offline HLL hierarchy tests. Existing HLL v21 dependency is used on VPS.

The small fallback below is only for unit testing this package outside the VPS
when horizontal_breakout_lab.py was not part of the supplied source ZIP.
"""
from __future__ import annotations
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
import numpy as np
import pandas as pd

try:
    import horizontal_breakout_lab  # noqa: F401
except ModuleNotFoundError:
    fake=types.ModuleType('horizontal_breakout_lab')
    fake.TF=1_800_000
    fake.ROOT=Path('.')
    fake.Archive=object
    fake.aggregate_15m_to_30m=lambda f: f
    def normalize(b, tf):
        x=b.sort_values('timestamp').reset_index(drop=True).copy()
        return x
    fake.normalize=normalize
    sys.modules['horizontal_breakout_lab']=fake

from hll_hierarchical_levels_v1 import (TF, build, sha, score_asof, load_candles,
                                         source_metadata)
from dashboard.views.hll_hierarchical_levels_view import (chart_candles,visible_levels,
    make_chart,read_frozen)

START=1_790_000_000_000//TF*TF

def bars(n=620):
    i=np.arange(n)
    o=np.full(n,100.0)
    c=100+0.11*np.sin(i*.12)
    h=np.maximum(o,c)+.30
    l=np.minimum(o,c)-.30
    for j in (40,75,110,145,180):h[j]=102.0
    return pd.DataFrame(dict(timestamp=START+i*TF,open=o,high=h,low=l,close=c,
                             volume=np.full(n,100.),quoteVolume=np.full(n,10000.)))

class Hierarchy(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.f=self.root/'src'/'BTCUSDT_30m.csv'
        self.f.parent.mkdir(parents=True)
        bars().to_csv(self.f,index=False)
        (self.f.parent/'provenance').mkdir()
        (self.f.parent/'provenance'/'BTCUSDT.json').write_text(json.dumps({
            'symbol':'BTCUSDT','sha256':sha(self.f),'status':'FULL'}))
    def tearDown(self):self.tmp.cleanup()

    def test_plan_read_only(self):
        x=build(self.root,self.f,'BTCUSDT','pilot',run=False)
        self.assertEqual(x['bars_30m'],620)
        self.assertFalse((self.root/'reports').exists())

    def test_unverified_source_rejected(self):
        (self.f.parent/'provenance'/'BTCUSDT.json').unlink()
        with self.assertRaises(ValueError):load_candles(self.f,'BTCUSDT')

    def test_tampered_source_rejected(self):
        with self.f.open('a') as p:p.write('\n')
        with self.assertRaisesRegex(ValueError,'SHA'):load_candles(self.f,'BTCUSDT')

    def test_gap_rejected_even_with_matching_sha(self):
        x=bars().drop(index=[200])
        x.to_csv(self.f,index=False)
        p=self.f.parent/'provenance'/'BTCUSDT.json'
        m=json.loads(p.read_text());m['sha256']=sha(self.f);p.write_text(json.dumps(m))
        with self.assertRaisesRegex(ValueError,'Huecos'):load_candles(self.f,'BTCUSDT')

    def test_score_strict_asof_and_late_defense(self):
        b=bars()
        t=int(b.timestamp.iloc[400])+TF
        forms=pd.DataFrame([dict(level_id='l1',symbol='BTCUSDT',origin='HIGH',
          formation_closed_ts=int(b.timestamp.iloc[200])+TF,center=100.,
          zone_low=99.7,zone_high=100.3,atr_at_formation=1.,quality_flag='STANDARD_WIDTH')])
        events=pd.DataFrame([dict(level_id='l1',event_type=x,observed_closed_ts=tm) for x,tm in [
          ('TEST_FROM_BELOW',t-TF),('DEFENSE_CONFIRMED',t+TF),
          ('LOSS_CONFIRMED',t+3*TF)]])
        ends=pd.DataFrame([dict(level_id='l1',end_state='ACTIVE_AT_END',end_closed_ts=int(b.timestamp.iloc[-1])+TF)])
        before=score_asof(forms,events,ends,b,t).iloc[0]
        self.assertEqual((before.tests_pre,before.defenses_pre,before.losses_pre),(1,0,0))
        after=score_asof(forms,events,ends,b,t+4*TF).iloc[0]
        self.assertEqual((after.tests_pre,after.defenses_pre,after.losses_pre),(1,1,1))

    def test_formation_known_only_after_confirmed(self):
        b=bars();t=int(b.timestamp.iloc[400])+TF
        forms=pd.DataFrame([dict(level_id='late',symbol='BTCUSDT',origin='HIGH',
          formation_closed_ts=t+TF,center=100,zone_low=99,zone_high=101,
          atr_at_formation=1.,quality_flag='STANDARD_WIDTH')])
        out=score_asof(forms,pd.DataFrame(),pd.DataFrame(),b,t)
        self.assertTrue(out.empty)

    def test_invalid_cutoff_fails(self):
        b=bars()
        with self.assertRaises(ValueError):
            score_asof(pd.DataFrame(),pd.DataFrame(),pd.DataFrame(),b,START+620*TF+TF)

    def test_full_build_frozen_sha_and_no_overwrite(self):
        x=build(self.root,self.f,'BTCUSDT','pilot',run=True,min_free_gb=0)
        folder=Path(x['output_dir'])
        self.assertTrue(folder.is_dir())
        manifest,f,e,ends,b=read_frozen(folder)
        self.assertEqual(len(b),620)
        self.assertEqual(x['source_provenance'],'VERIFIED')
        with self.assertRaises(FileExistsError):
            build(self.root,self.f,'BTCUSDT','pilot',run=True,min_free_gb=0)
        with (folder/'snapshot_asof.csv').open('a') as out:out.write('changed')
        with self.assertRaisesRegex(ValueError,'alterado'):
            read_frozen(folder)

    def test_aggregated_1h_4h_preserves_ohlc(self):
        b=bars()
        end=int(b.timestamp.iloc[-1])+TF
        o,tf=chart_candles(b,end,7,'4h')
        self.assertEqual(tf,'4h')
        self.assertTrue((o.high >= o[['open','close','low']].max(axis=1)).all())
        self.assertTrue((o.low <= o[['open','close','high']].min(axis=1)).all())
        self.assertEqual(len(o),42)

    def test_visible_overlap_hides_duplicate_but_keeps_ids(self):
        scores=pd.DataFrame([
         dict(level_id='a',tier='A',importance_score=88,distance_atr=.1,state_at_cutoff='ACTIVE',
              zone_low=99,zone_high=101),
         dict(level_id='b',tier='B',importance_score=60,distance_atr=.2,state_at_cutoff='ACTIVE',
              zone_low=99.2,zone_high=100.8),
         dict(level_id='c',tier='C',importance_score=40,distance_atr=1.,state_at_cutoff='ACTIVE',
              zone_low=90,zone_high=91)])
        v=visible_levels(scores,'ABC',True,True,20)
        self.assertEqual(v.level_id.tolist(),['a','c'])
        self.assertEqual(len(visible_levels(scores,'ALL',True,False,20)),3)

    def test_plot_line_begins_at_formation_and_ends_at_cutoff(self):
        b=bars()
        end=int(b.timestamp.iloc[-1])+TF
        f=pd.DataFrame([dict(level_id='a',tier='A',importance_score=85,origin='HIGH',
             center=100,zone_low=99.8,zone_high=100.2,
             formation_closed_ts=end-3*86_400_000,last_observed_closed_ts=end,
             tests_pre=2,defenses_pre=1,losses_pre=0)])
        fig,tf=make_chart(b,f,end,7,'30m',selected_id='a')
        traces=[trace for trace in fig.data if trace.type=='scatter']
        self.assertEqual(len(traces),1)
        self.assertEqual(str(traces[0].x[0]),str(pd.Timestamp(end-3*86_400_000,unit='ms',tz='UTC')))

if __name__=='__main__':unittest.main()
