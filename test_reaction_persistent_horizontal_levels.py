import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

import reaction_persistent_horizontal_levels as m


def synthetic(tf='15m', n=260, supports=(50,150), retreat=True):
    step=m.TF[tf]
    close=100 + 0.05*np.sin(np.arange(n)*0.6)
    hi=close+0.30;lo=close-0.30
    for idx in supports:
        if idx>=n: continue
        close[idx]=99.00
        lo[idx]=98.55
        hi[idx]=100.15
        # close enough next bars to support ATR-based excursion
        for q in range(idx+1,min(idx+5,n)):
            close[q]=100.5;hi[q]=101.1;lo[q]=100.1
    if retreat:
        close[-1]=98.9;hi[-1]=99.1;lo[-1]=98.6
    df=pd.DataFrame({'timestamp':np.arange(n,dtype=np.int64)*step,
         'open':close.copy(),'high':hi,'low':lo,'close':close,'volume':100.0})
    # Repair bars whose open and close approach pivots differently
    return df


def event(df,tf='15m',symbol='TESTUSDT',side='LONG'):
    # REACTION minute begins strictly after all historical bars have closed
    ts=int(df.timestamp.iloc[-1]+m.TF[tf])
    return dict(event_key=f'{symbol}|{side}|{ts}',symbol=symbol,side=side,
                date_utc='2026-10-22',retest_ts=ts,observation_type='LIVE_OBSERVED')


class CausalLevels(unittest.TestCase):
    def test_gap_coverage_fails_closed(self):
        df=synthetic().drop(index=100).reset_index(drop=True)
        e=event(df)
        _,cov=m.candidate_zones(e,'15m',df)
        self.assertEqual(cov,'GAPPED')

    def test_after_reaction_bar_is_ignored(self):
        df=synthetic()
        e=event(df)
        a,c=m.candidate_zones(e,'15m',df)
        extra=df.iloc[-1:].copy()
        extra['timestamp']=e['retest_ts'];extra[['open','high','low','close']]=[95,115,92,99]
        b,d=m.candidate_zones(e,'15m',pd.concat([df,extra],ignore_index=True))
        self.assertEqual(c,'COMPLETE')
        self.assertEqual(d,'COMPLETE')
        self.assertEqual(a,b)

    def test_two_previous_independent_responses_required(self):
        df=synthetic()
        e=event(df)
        rows,_=m.candidate_zones(e,'15m',df)
        self.assertTrue(all(x['confirmed_responses']>=2 for x in rows))
        self.assertTrue(all(x['formation_closed_ts']<e['retest_ts'] for x in rows))
        self.assertTrue(all(x['age_bars']>=20 for x in rows))
        self.assertTrue(any(x['origin_kind']=='LOW' for x in rows),rows)

    def test_second_pivot_without_completed_response_fails(self):
        # Second pivot is too close to REACTION to measure a confirmed move
        df=synthetic(supports=(50,257),retreat=False)
        e=event(df)
        rows,_=m.candidate_zones(e,'15m',df)
        self.assertFalse(any(x['origin_kind']=='LOW' and x['zone_low']<99 for x in rows))

    def test_two_quick_visits_do_not_count_as_persistent(self):
        df=synthetic(supports=(50,58),retreat=False)
        e=event(df)
        levels,_=m.candidate_zones(e,'15m',df)
        self.assertFalse(any(z['origin_kind']=='LOW' and z['zone_low']<99 for z in levels))

    def test_missing_history_unknown_not_absence(self):
        df=synthetic(n=85)
        e=event(df)
        z,c=m.candidate_zones(e,'15m',df)
        self.assertEqual(c,'INSUFFICIENT')
        label=m.classify(e,{'15m':[],'30m':[]},{'15m':c,'30m':'COMPLETE'})
        self.assertEqual(label['group_pre'],m.GROUP_UNKNOWN)
        self.assertEqual(label['near_pre'],'')

    def test_orientation_does_not_use_reaction_result(self):
        e={'event_key':'id','symbol':'SYM','side':'LONG','date_utc':'2026-10-22',
           'retest_ts':12,'observation_type':'LIVE_OBSERVED','tp':'SL_FIRST'}
        zone=dict(zone_id='q',timeframe='15m',origin_kind='LOW',zone_low=1.,zone_high=2.,
                  age_bars=100,distance_pre_atr=0.1,independent_visits=2,confirmed_responses=2,
                  level_status_pre='SUPPORT_BELOW',relation_pre='ABOVE')
        a=m.classify(e,{'15m':[zone],'30m':[]},{'15m':'COMPLETE','30m':'COMPLETE'})
        e['tp']='TP_FIRST'
        b=m.classify(e,{'15m':[zone],'30m':[]},{'15m':'COMPLETE','30m':'COMPLETE'})
        self.assertEqual(a,b)
        self.assertEqual(a['group_pre'],m.GROUP_NEAR)

    def test_role_near_and_far(self):
        e={'event_key':'id','symbol':'SYM','side':'SHORT','date_utc':'2026-10-22',
           'retest_ts':12,'observation_type':'LIVE_OBSERVED'}
        z={'zone_id':'z','timeframe':'30m','origin_kind':'HIGH','zone_low':1.,'zone_high':2.,
           'age_bars':85,'distance_pre_atr':.7,'independent_visits':2,'confirmed_responses':2,
           'level_status_pre':'RESISTANCE_ABOVE'}
        result=m.classify(e,{'15m':[],'30m':[z]},{'15m':'COMPLETE','30m':'COMPLETE'})
        self.assertEqual(result['group_pre'],m.GROUP_FAR)
        self.assertEqual(result['chosen_zone_id'],'')

    def test_equal_reaction_boundary_is_not_a_historical_response(self):
        df=synthetic()
        e=event(df)
        # Construct 2 old historical pivots but make the second response
        # appear ONLY at the exact REACTION open, which audit rejects.
        step=m.TF['15m']
        closed,_=m.coverage_slice(df,'15m',e['retest_ts'])
        pivots=[dict(kind='LOW',pivot_i=i,confirm_i=i+3,price=99.,
                     confirm_ts=int(closed.timestamp.iloc[i+3])+step,
                     pivot_ts=int(closed.timestamp.iloc[i])) for i in (12,45)]
        zone=dict(origin='LOW',low=98.5,high=99.5,center=99.,
                  formation_closed_ts=int(closed.timestamp.iloc[48])+step)
        def proof(_frame,p,_next,z,atr):
            return {'response_atr':1., 'response_closed_ts':(
                e['retest_ts'] if p['pivot_i']==45 else e['retest_ts']-step)}
        with mock.patch.object(m.atlas_v1,'pivots_confirmed',return_value=pivots), \
             mock.patch.object(m.atlas_v1,'frozen_zones',return_value=[zone]), \
             mock.patch.object(m.atlas_v1,'true_range_at',return_value=1.), \
             mock.patch.object(m,'response_from_pivot',side_effect=proof):
            levels,cov=m.candidate_zones(e,'15m',df)
        self.assertEqual(cov,'COMPLETE')
        self.assertFalse(levels)

    def test_invalid_completed_shard_is_repaired_without_full_rerun(self):
        import sqlite3,csv,json
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            out=root/'reports/reaction_lab/reaction_persistent_horizontal_v1/runs/test'
            df=synthetic(n=260)
            e=event(df)
            setup=([e],{'source_sha256':'toy','version':m.VERSION},out)
            def fake_merge(ctx,hist,symbol):
                return df.set_index('timestamp'),synthetic('30m',180).set_index('timestamp'),'NATIVE_30M'
            def fake_ro(path):return sqlite3.connect(':memory:')
            with mock.patch.object(m,'_setup',return_value=setup), \
                 mock.patch.object(m.v4,'_merge_tf',side_effect=fake_merge) as merge, \
                 mock.patch.object(m.atlas_v1.lineage,'ro',side_effect=fake_ro):
                first=m.run(root,'all-live',0,min_free_gb=0)
                self.assertTrue(first['success'])
                a,b,meta=m._symbol_paths(out,e['symbol'])
                with b.open(newline='') as f: records=list(csv.DictReader(f))
                self.assertTrue(records)
                # Imitate a past shard with valid checksums but a response at
                # the REACTION open -- only this symbol should be recomputed.
                proof_times=records[0]['response_timestamps'].split(';')
                self.assertGreaterEqual(len(proof_times),2)
                proof_times[-1]=str(e['retest_ts'])
                records[0]['response_timestamps']=';'.join(proof_times)
                with b.open('w',newline='') as f:
                    writer=csv.DictWriter(f,fieldnames=m.ZONE_COLS)
                    writer.writeheader();writer.writerows(records)
                state=json.loads(meta.read_text())
                state['levels_sha256']=m.digest(b)
                meta.write_text(json.dumps(state))
                self.assertIn('at/after REACTION', m._bad_shard_reason(b,[e['event_key']]))
                fixed=m.run(root,'all-live',0,min_free_gb=0)
                self.assertTrue(fixed['success'])
                self.assertEqual(merge.call_count,2)
                self.assertIsNone(m._bad_shard_reason(b,[e['event_key']]))
                again=m.run(root,'all-live',0,min_free_gb=0)
                self.assertTrue(again['success'])
                self.assertEqual(merge.call_count,2)

    def test_resume_audit_without_outcomes(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            out=root/'reports/reaction_lab/reaction_persistent_horizontal_v1/runs/test'
            df=synthetic(n=260)
            e=event(df)
            setup=( [e],{'source_sha256':'toy','version':m.VERSION},out)
            def fake_merge(ctx,hist,symbol):
                return df.set_index('timestamp'),synthetic('30m',180).set_index('timestamp'),'NATIVE_30M'
            def fake_ro(path):return sqlite3.connect(':memory:')
            with mock.patch.object(m,'_setup',return_value=setup), \
                 mock.patch.object(m.v4,'_merge_tf',side_effect=fake_merge) as merge, \
                 mock.patch.object(m.atlas_v1.lineage,'ro',side_effect=fake_ro):
                a=m.run(root,'live-stratified',1,min_free_gb=0)
                self.assertTrue(a['success'])
                b=m.run(root,'live-stratified',1,min_free_gb=0)
                self.assertTrue(b['success'])
                self.assertEqual(merge.call_count,1)
                self.assertEqual(b['events'],1)

if __name__=='__main__':unittest.main()
