"""Offline tests for causal structural subzones V2. Run from project root."""
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
    import horizontal_breakout_lab
except ModuleNotFoundError:
    fake=types.ModuleType('horizontal_breakout_lab')
    fake.TF=1_800_000;fake.ROOT=Path('.');fake.Archive=object
    fake.aggregate_15m_to_30m=lambda x:x;fake.normalize=lambda b,tf:b
    sys.modules['horizontal_breakout_lab']=fake

from hll_hierarchical_levels_v1 import TF, sha
from hll_structural_zones_v2 import (
    pair_links, snapshot_zones, reconstruct_clusters,compact_episodes, zone_id,
    band_pair,build,load_v2,verified_source,independent_times,zone_score_history)
from dashboard.views.hll_hierarchical_levels_view import chart_candles,make_zone_chart

START=1_790_000_000_000//TF*TF

def form(i,price,origin='HIGH',born=START+60*TF):
    return dict(level_id=i,symbol='BTCUSDT',origin=origin,
      formation_closed_ts=born,center=price,zone_low=price-.2,
      zone_high=price+.2,atr_at_formation=1.0,quality_flag='STANDARD_WIDTH')

def test_events(name,times,side='ABOVE',typ=None):
    return [dict(level_id=name,event_type=typ or 'TEST_FROM_'+side,
       observed_closed_ts=t,close=100.) for t in times]

def bars(n=3200):
    i=np.arange(n)
    return pd.DataFrame(dict(timestamp=START+i*TF,open=100.,high=100.4,
         low=99.6,close=100.,volume=10.))

class ZonesV2(unittest.TestCase):
    def setUp(self):
        self.f=pd.DataFrame([form('a',100),form('b',100.06,'LOW'),form('c',100.12)])
        self.times=[START+i*TF for i in (100,110,120,140,160)]
        self.e=pd.DataFrame(test_events('a',self.times)+test_events('b',self.times)+
                 test_events('c',[self.times[0],self.times[1]]))

    def test_independent_contacts_deduplicated(self):
        self.assertEqual(independent_times([10*TF,10*TF,11*TF,14*TF],4*TF),[10*TF,14*TF])
        c=compact_episodes(self.e,{'a','b'},self.times[-1])
        self.assertEqual(c['TEST_FROM_ABOVE'],5)

    def test_pair_requires_three_independent_same_direction(self):
        l=pair_links(self.f,self.e)
        self.assertEqual(len(l),1)
        self.assertEqual(set(l.iloc[0][['level_a','level_b']]),{'a','b'})
        self.assertEqual(int(l.linked_at.iloc[0]),self.times[2])
        wrong=pd.DataFrame(test_events('a',self.times)+test_events('b',self.times,side='BELOW'))
        self.assertTrue(pair_links(self.f,wrong).empty)

    def test_prefix_invariance_when_future_appended(self):
        e0=self.e.loc[self.e.observed_closed_ts<=self.times[2]].copy()
        links0=pair_links(self.f,e0)
        links1=pair_links(self.f,self.e)
        self.assertEqual(int(links0.linked_at.iloc[0]),int(links1.linked_at.iloc[0]))
        g0,_=reconstruct_clusters(self.f,links0,self.times[2])
        g1,_=reconstruct_clusters(self.f,links1,self.times[2])
        self.assertEqual(sorted(g0),sorted(g1))

    def test_no_future_links_or_formation(self):
        links=pair_links(self.f,self.e)
        groups,_=reconstruct_clusters(self.f,links,self.times[2]-TF)
        self.assertEqual(len(groups),3)
        groups,_=reconstruct_clusters(self.f,links,self.times[2])
        self.assertIn(('a','b'),groups)
        future_form=pd.DataFrame([form('f',101,born=self.times[-1]+TF)])
        g,_=reconstruct_clusters(future_form,links,self.times[0])
        self.assertEqual(g,[])

    def test_complete_link_prevents_bridge(self):
        # a-b and b-c can link, but a-c lacks common tests => no chain merge.
        times1=self.times[:3]
        times2=[self.times[2],self.times[3],self.times[4]]
        e=pd.DataFrame(test_events('a',times1)+test_events('b',times1+times2)+test_events('c',times2))
        links=pair_links(self.f,e)
        groups,_=reconstruct_clusters(self.f,links,self.times[-1])
        self.assertEqual(len(groups),2)
        self.assertNotIn(('a','b','c'),groups)

    def test_no_join_solely_on_price_and_band(self):
        self.assertTrue(band_pair(self.f.iloc[0],self.f.iloc[1]))
        self.assertTrue(pair_links(self.f,pd.DataFrame(columns=self.e.columns)).empty)

    def test_one_line_can_be_high_importance(self):
        f=pd.DataFrame([form('solo',100)])
        e=pd.DataFrame(test_events('solo',self.times)+test_events('solo',self.times,
                     typ='DEFENSE_CONFIRMED'))
        s,_=snapshot_zones(f,e,pd.DataFrame(),bars(321),pair_links(f,e),START+321*TF)
        self.assertEqual(len(s),1)
        self.assertEqual(int(s.members.iloc[0]),1)
        self.assertEqual(s.score_method.iloc[0],'MAX_MEMBER_ASOF_NOT_ZONE_CALIBRATED')

    def test_structural_plot_starts_at_join_not_leader_formation(self):
        b=bars(321)
        t=START+321*TF
        leader=type('Leader',(),dict(level_id='a',formation_closed_ts=START+60*TF,
                last_observed_closed_ts=t,zone_low=99.8,zone_high=100.2,
                center=100,origin='HIGH',tier='A',importance_score=80))()
        joined=START+270*TF
        z=dict(id='z',leader=leader,anchor=100.,kind='HIGH',tier='A',score=80,
              count=2,min_center=99.9,max_center=100.1,
              low=99.7,high=100.3,created_at=joined)
        fig,tf=make_zone_chart(b,[z],t,3,'30m',selected_zone_id='z')
        line=next(trace for trace in fig.data if trace.type=='scatter' and trace.mode=='lines')
        self.assertEqual(str(line.x[0]),str(pd.Timestamp(joined,unit='ms',tz='UTC')))

    def test_sixty_day_full_30m_uses_real_candles(self):
        b=bars(3600)
        t=int(b.timestamp.iloc[-1])+TF
        sub,tf=chart_candles(b,t,60,'30m')
        self.assertEqual(tf,'30m')
        self.assertEqual(len(sub),2880)
        c,tf=chart_candles(b,t,60,'4h')
        self.assertEqual(tf,'4h')
        self.assertEqual(len(c),359)  # A partial UTC 4h boundary is correctly omitted.

    def test_score_history_never_backpaints_before_zone_birth(self):
        b=bars(321)
        join=self.times[2]
        out=zone_score_history(self.f,self.e,pd.DataFrame(),b,{'a','b'},join,START+321*TF)
        self.assertTrue((out.cutoff_ms>=join).all())
        self.assertEqual(int(out.cutoff_ms.iloc[0]),join)
        self.assertEqual(int(out.cutoff_ms.iloc[-1]),START+321*TF)

    def test_zone_lineage_preserves_past_ids(self):
        # First a,b join. Later c gets a,b tests and completes all crosspairs.
        d=self.e.copy()
        tail=[START+i*TF for i in (180,190,200)]
        d=pd.concat([d,pd.DataFrame(test_events('a',tail)+test_events('b',tail)+
                            test_events('c',tail))],ignore_index=True)
        links=pair_links(self.f,d)
        early,birth0=reconstruct_clusters(self.f,links,self.times[2])
        mature,birth1=reconstruct_clusters(self.f,links,tail[-1])
        self.assertIn(('a','b'),early)
        self.assertIn(('a','b','c'),mature)
        ab=zone_id(['a','b'],'BTCUSDT')
        abc=zone_id(['a','b','c'],'BTCUSDT')
        self.assertEqual(birth0[0]['zone_id'],ab)
        self.assertEqual(birth1[-1]['zone_id'],abc)
        self.assertIn(ab,birth1[-1]['parent_zone_ids'])

    def test_frozen_read_verifies_hash_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            folder=root/'v1';folder.mkdir()
            b=bars(321)
            f=self.f
            e=self.e
            # Valid score inputs are written with correct hashes.
            end=pd.DataFrame([dict(level_id=k,end_state='ACTIVE_AT_END',
                      end_closed_ts=START+321*TF) for k in f.level_id])
            import hll_hierarchical_levels_v1 as v1
            snap=v1.score_asof(f,e,end,b,START+321*TF)
            maps=dict(formations=f,events=e,ends=end,candles_30m=b,snapshot_asof=snap)
            dig={}
            for key,df in maps.items():
                df.to_csv(folder/(key+'.csv'),index=False)
                dig[key+'.csv']=sha(folder/(key+'.csv'))
            m=dict(version='hll-hierarchical-levels-v1.0-exploratory',
               levels_detected=len(f),events_detected=len(e),bars_30m=len(b),
               source_sha256='test',symbol='BTCUSDT',cutoff_ms=START+321*TF,
               files_sha256=dig)
            (folder/'manifest.json').write_text(json.dumps(m))
            out=build(root,folder,'pilot',run=True,min_free_gb=0)
            self.assertEqual(out['links_confirmed'],1)
            v2=root/'reports/horizontal_level_structural_zones_v2/runs/pilot'
            man,links=load_v2(v2,folder/'manifest.json')
            self.assertEqual(len(links),1)
            with self.assertRaises(FileExistsError):build(root,folder,'pilot',run=True,min_free_gb=0)
            with (v2/'pair_links.csv').open('a') as fh:fh.write('tampered')
            with self.assertRaisesRegex(ValueError,'alterado'):load_v2(v2,folder/'manifest.json')

if __name__=='__main__':unittest.main()
