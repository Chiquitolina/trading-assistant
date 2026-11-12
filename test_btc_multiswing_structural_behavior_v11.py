#!/usr/bin/env python3
"""Causality, symmetry, episode identity, parent provenance for Behavior V1.1."""
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

import btc_multiswing_structural_behavior_v11 as v


def synthetic_bars(tfm, closes, initial_open=100.0):
    step=tfm*v.M
    result=[]
    last=initial_open
    for i,close in enumerate(closes):
        hi=max(last,close)+.1;lo=min(last,close)-.1
        result.append({'timestamp':i*step,'open':last,'high':hi,'low':lo,'close':close})
        last=close
    return v._closed_series(pd.DataFrame(result),step)


def fake_eligible(timestamps, *, rid='msA', stage_low=99.5,stage_high=100.5):
    rows=[]
    for j,t in enumerate(timestamps,1):
        rows.append({'event_id':f'{rid}|V{j}|{t}','region_id':rid,
           'visit_number':j,'side':'HIGH','pre_cutoff_ts':t,
           'birth_touch_open_ts':t,'stage_at_pre':True,'stage_known_from':0,
           'stage_low':stage_low,'stage_high':stage_high,'stage_extrema':3,
           'pre_close_1m':99.,'approach_position':'BELOW',
           'pre_last_15m_close_ts':(t//v.FIFTEEN)*v.FIFTEEN,
           'pre_last_15m_close':99.,'prior_completed_birth_visits_asof':j-1,
           'eligibility':'ELIGIBLE_CURRENT_STAGE_1M_TOUCH',
           'touch_1m_confirmed_ts':t+v.M})
    return pd.DataFrame(rows)


def fake_visits(timestamps,rid='msA',end_gap=30):
    return pd.DataFrame([{'region_id':rid,'visit_number':i,
        'first_touch_ts':t,'end_known_ts':t+end_gap*v.M}
        for i,t in enumerate(timestamps,1)])


class BehaviorV11(unittest.TestCase):
    def test_break_above_then_failed_return_below_both_closes(self):
        b=synthetic_bars(15,[101,101.5,101.5,99,98,98])
        rows=v.classify_transitions(b,episode_id='ep',region_id='rid',
              origin_side='LOW',approach='BELOW',band_low=99.5,
              band_high=100.5,touch_open=0,source_end=90*v.M,
              tf_minutes=15,horizon_minutes=90)
        events={r['event_type']:r for r in rows}
        self.assertIn('BREAK_ABOVE_2C',events)
        self.assertIn('FAILED_BREAK_RETURN_ORIGIN_2C',events)
        self.assertEqual(events['BREAK_ABOVE_2C']['confirmation_ts'],30*v.M)
        self.assertEqual(events['FAILED_BREAK_RETURN_ORIGIN_2C']['confirmation_ts'],75*v.M)
        self.assertEqual(events['FAILED_BREAK_RETURN_ORIGIN_2C']['parent_transition_id'],
                         events['BREAK_ABOVE_2C']['transition_id'])
        self.assertTrue(all(r['confirmation_ts']>0 for r in rows))

    def test_reverse_side_from_above_mirror(self):
        b=synthetic_bars(15,[98,98,98,101,102,102])
        rows=v.classify_transitions(b,episode_id='ep',region_id='rid',
              origin_side='HIGH',approach='ABOVE',band_low=99.5,
              band_high=100.5,touch_open=0,source_end=90*v.M,
              tf_minutes=15,horizon_minutes=90)
        names=[r['event_type'] for r in rows]
        self.assertIn('BREAK_BELOW_2C',names)
        self.assertIn('FAILED_BREAK_RETURN_ORIGIN_2C',names)
        self.assertEqual(next(r for r in rows if r['event_type']=='BREAK_BELOW_2C')['direction'],'BELOW')

    def test_inside_is_neither_support_nor_resistance_by_default(self):
        b=synthetic_bars(15,[101,101,99.9,100.,100.1,100.])
        rows=v.classify_transitions(b,episode_id='ep',region_id='rid',
              origin_side='LOW',approach='INSIDE',band_low=99.5,
              band_high=100.5,touch_open=0,source_end=90*v.M,
              tf_minutes=15,horizon_minutes=90)
        names=[r['event_type'] for r in rows]
        self.assertIn('EXIT_ABOVE_2C',names)
        self.assertIn('FAILED_EXIT_RETURN_INSIDE_2C',names)
        self.assertNotIn('BREAK_ABOVE_2C',names)

    def test_1h_breakout_confirmed_only_at_second_hour_close(self):
        b=synthetic_bars(60,[101,101,98,98])
        rows=v.classify_transitions(b,episode_id='ep',region_id='rid',
              origin_side='HIGH',approach='BELOW',band_low=99.5,
              band_high=100.5,touch_open=0,source_end=240*v.M,
              tf_minutes=60,horizon_minutes=240)
        br=[r for r in rows if r['event_type']=='BREAK_ABOVE_2C']
        self.assertEqual([x['confirmation_ts'] for x in br],[120*v.M])
        self.assertEqual(br[0]['source_tf'],'H1_CLOSED')

    def test_higher_tf_context_discards_partial_4h(self):
        b=synthetic_bars(240,[100,102,104,106,108,110,112,114])
        at=v.context_asof(b,5*60*v.M,99,101,period_minutes=240)
        self.assertEqual(at['last_close_ts'],240*v.M)
        self.assertEqual(at['last_close'],100)
        self.assertIsNone(at['returns'][12])
        at2=v.context_asof(b,29*60*v.M,99,101,period_minutes=240)
        self.assertEqual(at2['last_close_ts'],28*60*v.M)
        self.assertAlmostEqual(at2['returns'][12],(112/106-1)*100)
        self.assertAlmostEqual(at2['returns'][24],(112/100-1)*100)

    def test_1h_context_has_closed_returns_and_counts(self):
        b=synthetic_bars(60,[99,99,101,101,101,102,103,104,105,106,107,108,109])
        c=v.context_asof(b,9*60*v.M,99.5,100.5,period_minutes=60)
        self.assertEqual(c['last_close_ts'],9*60*v.M)
        self.assertEqual(c['last3_counts']['ABOVE'],3)
        self.assertAlmostEqual(c['returns'][4],(105/101-1)*100)

    def test_future_changes_do_not_modify_pre_context(self):
        b=synthetic_bars(60,[100+i for i in range(30)])
        c0=v.context_asof(b,14*60*v.M,105,106,period_minutes=60)
        b2={k:x.copy() for k,x in b.items()}
        b2['close'][15:]=800
        c1=v.context_asof(b2,14*60*v.M,105,106,period_minutes=60)
        self.assertEqual(c0,c1)

    def test_episode_id_stable_with_later_visits_and_stage_change(self):
        t1=1000*v.M; t2=1120*v.M; t3=1500*v.M
        all_rows=fake_eligible([t1,t2,t3]); visits=fake_visits([t1,t2,t3])
        heads,m=v.make_episodes(all_rows,visits)
        self.assertEqual(len(heads),2)
        self.assertEqual(m.iloc[0].episode_id,m.iloc[1].episode_id)
        self.assertNotEqual(m.iloc[1].episode_id,m.iloc[2].episode_id)
        heads2,_=v.make_episodes(all_rows.iloc[:1],visits.iloc[:1])
        self.assertEqual(heads[0][0],heads2[0][0])
        changed=all_rows.copy();changed.at[1,'stage_low']=98
        heads3,_=v.make_episodes(changed,visits)
        self.assertNotEqual(heads3[0][0],heads3[1][0])

    def test_post_outcomes_only_after_confirmation_and_censor_null(self):
        b=synthetic_bars(1,[100,101,102,103,104,105])
        tr={'transition_id':'x','episode_id':'ep','source_tf':'M15_CLOSED',
            'event_type':'BREAK_ABOVE_2C','confirmation_ts':3*v.M,
            'confirmation_close':103}
        out=v.evaluate_transition(b,tr,end=6*v.M,horizons=(2,5))
        self.assertEqual(out[0]['observed_minutes'],2)
        self.assertEqual(out[0]['status'],'FULL_HORIZON')
        self.assertEqual(out[1]['status'],'RIGHT_CENSORED')
        self.assertIsNone(out[1]['up_0p5_touched'])
        self.assertIsNotNone(out[1]['max_up_pct'])

    def test_link_never_uses_15m_after_1h(self):
        rows=[]
        for name,tf,direction,t,kind in [('mfirst','M15_CLOSED','ABOVE',60,'BREAK_ABOVE_2C'),
               ('hlate','H1_CLOSED','ABOVE',120,'BREAK_ABOVE_2C'),
               ('mfuture','M15_CLOSED','ABOVE',180,'BREAK_ABOVE_2C')]:
            rows.append({'transition_id':name,'episode_id':'ep','source_tf':tf,
                          'direction':direction,'event_type':kind,'confirmation_ts':t*v.M,
                          'linked_15m_transition_id':None,'linked_lag_minutes':None})
        linked=v.link_h1_to_m15(pd.DataFrame(rows))
        self.assertEqual(linked.iloc[1]['linked_15m_transition_id'],'mfirst')
        self.assertEqual(linked.iloc[1]['linked_lag_minutes'],60)
        # A 15m breakout must not be linked to a 1h retest (different family).
        mutated=pd.DataFrame(rows)
        mutated.at[1,'event_type']='RETEST_TOUCH'
        self.assertTrue(pd.isna(v.link_h1_to_m15(mutated).iloc[1]['linked_15m_transition_id']))

    def test_build_complete_synthetic_pipeline_and_pre_isolation(self):
        n=2*24*60
        t=24*60+5
        price=np.full(n,99.0)
        price[t:t+30]=101.2
        price[t+30:t+180]=98.7
        full=pd.DataFrame({'timestamp':np.arange(n)*v.M,
            'open':np.r_[price[0],price[:-1]],'close':price})
        full['high']=full[['open','close']].max(axis=1)+.6
        full['low']=full[['open','close']].min(axis=1)-.6
        eligible=fake_eligible([t*v.M]);visits=fake_visits([t*v.M])
        # PRE values in the historical parent MUST agree with the frozen bars.
        eligible.loc[0,'pre_last_15m_close']=float(full.loc[full.timestamp==(t//15*15-1)*v.M,'close'].iloc[0])
        source={'start':0,'end':n*v.M}
        pre,members,trans,out,summary=v.build_v11(full,eligible,visits,source)
        self.assertEqual(len(pre),1)
        self.assertEqual(len(members),1)
        self.assertGreater(len(trans),0)
        self.assertEqual(len(out),len(trans)*len(v.OUTCOME_HORIZONS))
        self.assertGreater(summary['1h_events'],0)
        self.assertEqual(pre.loc[0,'pre_last_4h_close_ts'],24*60*v.M)
        later=full.copy()
        later.loc[later.timestamp>t*v.M,'close']=later.loc[later.timestamp>t*v.M,'close']+11
        later['open']=later['close'].shift(1).fillna(later['close'].iloc[0])
        later['high']=later[['open','close']].max(axis=1)+.6
        later['low']=later[['open','close']].min(axis=1)-.6
        # A source mutation in POST does not change any PRE features.
        p2,_,_,_,_=v.build_v11(later,eligible,visits,source)
        pd.testing.assert_frame_equal(pre,p2)
        corrupted=full.copy()
        corrupted.loc[corrupted.timestamp==(t-1)*v.M,'close']+=.2
        corrupted['high']=corrupted[['open','close']].max(axis=1)+.6
        corrupted['low']=corrupted[['open','close']].min(axis=1)-.6
        with self.assertRaisesRegex(ValueError,'V11_V1_PRE_PRICE_OR_PHYSICAL_CONTACT_MISMATCH'):
            v.build_v11(corrupted,eligible,visits,source)

    def test_actual_v1_parent_hashes_are_valid_if_supplied(self):
        # When running in the extracted package, real audit files are included
        # ONLY as optional local fixtures. VPS does not need these fixture copies.
        here=Path(__file__).parent
        parent=here/'results'
        older=here/'v24a'
        if not (parent/'manifest.json').exists() or not (older/'manifest.json').exists():
            self.skipTest('optional local read-only parent fixtures not attached')
        eligible,m=v.verify_parent_v1(parent)
        visits=v.verify_parent_v24a(older,m)
        self.assertEqual(len(eligible),2587)
        self.assertEqual(len(visits),4565)
        heads,members=v.make_episodes(eligible,visits)
        self.assertEqual(len(members),2587)
        self.assertLess(len(heads),2587)

    def test_immutable_write_creates_manifest_and_rejects_repeat(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);p1=root/'v1';p24=root/'v24a'
            p1.mkdir();p24.mkdir()
            (p1/'manifest.json').write_text('v1')
            (p24/'manifest.json').write_text('v24')
            dest=root/'result'
            v.write_immutable(dest,{'episode_pre_v11.csv':pd.DataFrame({'x':[1]})},
                {'version':v.VERSION}, {'start':0,'end':0},p1,p24)
            self.assertEqual(json.loads((dest/'manifest.json').read_text())['version'],v.VERSION)
            with self.assertRaises(FileExistsError):
                v.write_immutable(dest,{}, {},{},p1,p24)


if __name__=='__main__':
    unittest.main()
