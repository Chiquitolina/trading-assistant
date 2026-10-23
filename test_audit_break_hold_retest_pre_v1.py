import hashlib
import json
import tempfile
import types
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
import pandas as pd
import audit_break_hold_retest_pre_v1 as m

S=m.TF_MS['15m']

def bars(side='LONG',retest=True):
    # First 4 old-side closes, then break, hold, optional retest, then hold.
    seq=([97]*4 + [103]*10) if side=='LONG' else ([103]*4+[97]*10)
    data=[]
    for i,c in enumerate(seq):
        lo=c-.3;hi=c+.3
        if retest and i==8:
            lo=min(lo,100);hi=max(hi,100)
        data.append(dict(timestamp=i*S,open=c,high=hi,low=lo,close=c,volume=10))
    return pd.DataFrame(data)

def zone(side='LONG',tf='15m'):
    return dict(event_key='ID',zone_id='Z',timeframe=tf,origin_kind='HIGH' if side=='LONG' else 'LOW',
        zone_low=99,zone_high=101,formation_closed_ts=S,confirmed_responses=2,
        independent_visits=2,response_timestamps=f'{2*S};{3*S}',
        age_bars=80,distance_pre_atr=.1)

def event(side='LONG',t=15*S):
    return dict(event_key='ID',symbol='XUSDT',side=side,retest_ts=t,date_utc='2026-10-22',
        observation_type='LIVE_OBSERVED',group_pre=m.v2.NEAR)

class PureLifecycle(unittest.TestCase):
    def test_bullish_full_break_hold_retest(self):
        x=m.path_for_zone(bars(),zone(),15*S,'LONG')
        self.assertEqual(x['state'],'BULLISH_BREAK_HOLD_RETEST_PRE')
        self.assertEqual(x['first_break_closed_ts'],5*S)
        self.assertEqual(x['break_confirm_closed_ts'],6*S)
        self.assertEqual(x['hold_confirm_closed_ts'],8*S)
        self.assertEqual(x['pre_retest_closed_ts'],9*S)
        self.assertEqual(x['retest_hold_confirm_closed_ts'],11*S)
    def test_bearish_full_break_hold_retest(self):
        self.assertEqual(m.path_for_zone(bars('SHORT'),zone('SHORT'),15*S,'SHORT')['state'],
                         'BEARISH_BREAK_HOLD_RETEST_PRE')
    def test_break_hold_without_prior_retest_kept_separate(self):
        self.assertEqual(m.path_for_zone(bars(retest=False),zone(),15*S,'LONG')['state'],
                         'BULLISH_BREAK_HOLD_PRE')
    def test_unconfirmed_retest_at_reaction_boundary_is_not_counted(self):
        # Only the retest candle closes exactly at event timestamp, too late to prove it.
        x=m.path_for_zone(bars(),zone(),9*S,'LONG')
        self.assertEqual(x['state'],'BULLISH_BREAK_HOLD_PRE')
        self.assertEqual(x['pre_retest_closed_ts'],'')
    def test_future_bars_cannot_change_pre_label(self):
        b=bars();x=m.path_for_zone(b,zone(),15*S,'LONG')
        extra=pd.DataFrame([dict(timestamp=16*S,open=98,high=105,low=97,close=97,volume=1)])
        y=m.path_for_zone(pd.concat([b,extra],ignore_index=True),zone(),15*S,'LONG')
        self.assertEqual(x,y)
    def test_break_after_unconfirmed_level_does_not_qualify(self):
        z=zone();z['response_timestamps']=f'{5*S};{6*S}'
        self.assertNotIn('BREAK_HOLD_RETEST',m.path_for_zone(bars(),z,15*S,'LONG')['state'])
    def test_level_formed_after_break_does_not_qualify(self):
        z=zone();z['formation_closed_ts']=7*S
        self.assertNotIn('BREAK_HOLD_RETEST',m.path_for_zone(bars(),z,15*S,'LONG')['state'])
    def test_reversed_after_confirmed_flip_is_no_longer_hold(self):
        b=bars()
        for i in (12,13):b.loc[i,['open','high','low','close']]=[97,97.3,96.7,97]
        self.assertEqual(m.path_for_zone(b,zone(),15*S,'LONG')['state'],'BREAK_INVALIDATED_PRE')
    def test_gap_is_unknown(self):
        b=bars().drop(index=7)
        self.assertEqual(m.path_for_zone(b,zone(),15*S,'LONG')['state'],'UNKNOWN_PATH')
    def test_bad_ohlc_unknown(self):
        b=bars();b.loc[3,'high']=b.loc[3,'low']-.2
        self.assertEqual(m.path_for_zone(b,zone(),15*S,'LONG')['state'],'UNKNOWN_PATH')
    def test_close_at_event_does_not_confirm_break(self):
        self.assertNotIn('BREAK_HOLD',m.path_for_zone(bars(),zone(),6*S,'LONG')['state'])
    def test_choose_nearest_opposite_kind_before_path(self):
        a=zone();b=dict(a,zone_id='B',distance_pre_atr=.05,origin_kind='LOW')
        c=dict(a,zone_id='C',distance_pre_atr=.4,origin_kind='HIGH')
        self.assertEqual(m.choose_zone(event(),[a,b,c])['zone_id'],'Z')
    def test_group_unknown_does_not_call_path(self):
        e=event();e['group_pre']=m.v2.UNKNOWN
        self.assertEqual(m.classify(e,[],{})['bhr_group_pre'],'UNKNOWN_HISTORY')
    def test_pre_labels_do_not_read_any_outcome(self):
        e=event();e['result_180m']='SL_FIRST'
        one=m.classify(e,[zone()],{'15m':bars()})
        e['result_180m']='TP_FIRST'
        two=m.classify(e,[zone()],{'15m':bars()})
        self.assertEqual(one,two)
        self.assertFalse(any('result' in k for k in one))
    def test_unsupported_tf_unknown(self):
        z=zone();z['timeframe']='1h'
        self.assertEqual(m.path_for_zone(bars(),z,15*S,'LONG')['state'],'UNKNOWN_PATH')
    def test_responses_before_break_strict(self):
        z=zone();z['response_timestamps']=f'{2*S};{5*S}'
        self.assertNotIn('BREAK_HOLD_RETEST',m.path_for_zone(bars(),z,15*S,'LONG')['state'])

class Integration(unittest.TestCase):
    def test_run_resume_audit_and_report(self):
        class Conn:
            def close(self):pass
            def __enter__(self):return self
            def __exit__(self,*args):self.close()
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);rf=root/'input';rf.mkdir()
            ev=pd.DataFrame([event()]);z=pd.DataFrame([zone()]);aud=dict(outputs_sha256={'event_labels.csv':'stubE','candidate_levels.csv':'stubZ'})
            def fake_prepare(folder,max_events=0):return ev,z,aud
            lineage=types.SimpleNamespace(ROOT_REL=Path('src'),CTX_NAME='ctx',HISTORY_NAME='h',ro=lambda path:Conn())
            fake_atlas=types.ModuleType('reaction_structural_level_atlas');fake_atlas.lineage=lineage
            fake_v4=types.ModuleType('reaction_compression_breakout_retest_v4')
            fake_v4._merge_tf=lambda ctx,hist,sym:(bars().set_index('timestamp'),bars().set_index('timestamp'),'SYNTH')
            with patch.object(m,'prepare',fake_prepare),patch.dict(sys.modules,{
                'reaction_structural_level_atlas':fake_atlas,'reaction_compression_breakout_retest_v4':fake_v4}):
                a=m.run(root,rf)
                self.assertEqual(a['groups']['BULLISH_BREAK_HOLD_RETEST_PRE'],1)
                b=m.run(root,rf)
                self.assertEqual(a['labels_sha'],b['labels_sha'])
                self.assertTrue(m.audit_run(rf)['success'])
                label=rf/'break_hold_retest_pre_v1/all_live/bhr_labels_pre.csv'
                self.assertNotIn('result_180m',pd.read_csv(label).columns)
                ft=root/'ft.csv'
                pd.DataFrame([dict(event_key='ID',symbol='XUSDT',side='LONG',observation_type='LIVE_OBSERVED',
                    entry_mode='OBSERVED_OPEN',tp_pct=1,sl_pct=1,
                    result_180m='TP_FIRST',result_360m='TP_FIRST')]).to_csv(ft,index=False)
                rep=m.report(rf,0,ft)
                self.assertEqual(rep['by_side']['180']['LONG']['BULLISH_BREAK_HOLD_RETEST_PRE']['TP'],1)
    def test_tamper_shard_fails_audit(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);rf=root/'input';rf.mkdir()
            ev=pd.DataFrame([event()]);zones=pd.DataFrame([zone()]);aud={'outputs_sha256':{'event_labels.csv':'X','candidate_levels.csv':'Y'}}
            def fake_prepare(*args):return ev,zones,aud
            with patch.object(m,'prepare',fake_prepare):
                out=m.outdir(rf,0);atom=m.atom_csv
                m.atom_json(out/'config.json',m.config(aud,0))
                x=m.classify(event(),[zone()],{'15m':bars()})
                atom(out/'shards/XUSDT.csv',pd.DataFrame([x],columns=m.FIELDS))
                m.atom_json(out/'shards/XUSDT.json',dict(keys=['ID'],sha256=m.sha(out/'shards/XUSDT.csv')))
                (out/'shards/XUSDT.csv').write_text('garbled')
                with self.assertRaisesRegex(ValueError,'Invalid BHR shard'):
                    m.audit_run(rf)
if __name__=='__main__':unittest.main()
