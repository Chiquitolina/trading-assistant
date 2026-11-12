#!/usr/bin/env python3
"""Arrival Lab V1: causality, census, source hash and separation tests."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from btc_multiswing_60d_explorer_v1 import M
from btc_multiswing_arrival_lab_v1 import (
    HORIZONS, PRE_COLUMNS, POST_COLUMNS,
    _last_confirmed_stage, _parent_run,
    _post_horizon, _write_immutable, build_arrival_dataset, main,
)


def sample():
    n=300
    stamps=np.arange(n,dtype=np.int64)*M
    x=110+np.sin(np.arange(n)/15)*2
    frame=pd.DataFrame({'timestamp':stamps,'open':x,'high':x+.5,
                        'low':x-.5,'close':x+.2,'volume':100+np.arange(n)*.01})
    # The selected bars really touch the immutable birth band [100,101].
    for i in (100,155):
        frame.loc[i,'open']=100.5
        frame.loc[i,'low']=99.8
        frame.loc[i,'high']=110.5
        frame.loc[i,'close']=110.1
    regions=pd.DataFrame([{'region_id':'A','side':'LOW',
        'birth_known_ts':40*M,'band_low_birth':100.,'band_high_birth':101.,
        'extrema_at_birth':2},
        {'region_id':'B','side':'HIGH','birth_known_ts':40*M,
         'band_low_birth':140.,'band_high_birth':141.,'extrema_at_birth':2}])
    visits=pd.DataFrame([
        {'region_id':'A','visit_number':1,'first_touch_ts':100*M,
         'first_known_ts':101*M,'end_known_ts':135*M,'touch_minutes':4},
        {'region_id':'A','visit_number':2,'first_touch_ts':155*M,
         'first_known_ts':156*M,'end_known_ts':200*M,'touch_minutes':8},
    ])
    stages=pd.DataFrame([
       {'region_id':'A','side':'LOW','known_from':40*M,'known_until':140*M,
        'band_low':100.,'band_high':101.,'independent_extrema':2},
       {'region_id':'A','side':'LOW','known_from':140*M,'known_until':280*M,
        'band_low':99.,'band_high':101.,'independent_extrema':3},
       {'region_id':'B','side':'HIGH','known_from':40*M,'known_until':280*M,
        'band_low':140.,'band_high':141.,'independent_extrema':2},
    ])
    return frame, regions, visits, stages, 30*M, 300*M


class ArrivalLabV1(unittest.TestCase):
    def build(self, data=None, **kw):
        return build_arrival_dataset(*(data or sample()),**kw)

    def test_run_writes_immutable_artifacts_under_target_root(self):
        frame, regions, visits, stages, start, end=sample()
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            parent=root/'parent'; parent.mkdir()
            (parent/'manifest.json').write_text('{}',encoding='utf-8')
            source={'start':start,'end':end,'warmup_start':0,
                    'source_sha256':'a'*64,'catalog_sha256':'b'*64,
                    'source_hll_parity':'PARITY_OK'}
            manifest={'version':'test_parent','config':{'geometry':{'lookback_days':7}}}
            with (patch('btc_multiswing_arrival_lab_v1.load_snapshot',return_value=(frame,pd.DataFrame(),source)),
                  patch('btc_multiswing_arrival_lab_v1._parent_run',return_value=(regions,visits,manifest,{})),
                  patch('btc_multiswing_arrival_lab_v1._validate_and_reconstruct',return_value=stages)):
                code=main(['--run','--project-root',str(root),
                           '--parent','parent','--output-dir','generated'])
                self.assertEqual(code,0)
                self.assertEqual(len(pd.read_csv(root/'generated'/'arrival_pre_v1.csv')),2)
                self.assertEqual(len(pd.read_csv(root/'generated'/'arrival_post_outcomes_v1.csv')),10)
                with self.assertRaises(FileExistsError):
                    main(['--run','--project-root',str(root),
                          '--parent','parent','--output-dir','generated'])

    def test_all_visits_and_horizons_retained(self):
        p,o,s=self.build()
        self.assertEqual((len(p),len(o)),(2,2*len(HORIZONS)))
        self.assertEqual(set(p.columns),set(PRE_COLUMNS))
        self.assertEqual(set(o.columns),set(POST_COLUMNS))
        self.assertEqual(len(set(p.event_id)),2)
        self.assertEqual(s['arrival_pre_rows'],2)

    def test_causal_stage_evolves_without_retroactive_birth_band_change(self):
        p,_,_=self.build()
        by=p.set_index('visit_number')
        self.assertEqual(by.loc[1,'current_stage_extrema'],2)
        self.assertEqual(by.loc[2,'current_stage_extrema'],3)
        self.assertEqual(by.loc[1,'birth_band_low'],100.)
        self.assertEqual(by.loc[2,'birth_band_low'],100.)
        self.assertEqual(by.loc[2,'current_stage_low'],99.)
        self.assertFalse(bool(by.loc[1,'reference_mismatch_possible']))
        self.assertTrue(bool(by.loc[2,'reference_mismatch_possible']))

    def test_prior_visit_known_only_after_end(self):
        p,_,_=self.build()
        self.assertEqual(p.prior_visits_closed_asof.tolist(),[0,1])
        self.assertEqual(p.prior_touch_minutes_known_asof.tolist(),[0,4])

    def test_future_ohlc_mutation_does_not_change_pre_features(self):
        d=sample()
        p1,o1,_=self.build(d)
        d2=list(copy.deepcopy(d))
        frame=d2[0]
        frame.loc[110,'high']=frame.loc[110,'high']+200
        frame.loc[110,'close']=frame.loc[110,'close']+10
        frame.loc[110,'low']=min(frame.loc[110,'low'],frame.loc[110,'open']-3)
        d2[0]=frame
        p2,o2,_=self.build(tuple(d2))
        cols=[c for c in p1.columns if c not in ('pre_cutoff_utc',)]
        pd.testing.assert_series_equal(p1.loc[0,cols],p2.loc[0,cols])
        a=o1[(o1.visit_number==1)&(o1.horizon_minutes==15)].iloc[0]
        b=o2[(o2.visit_number==1)&(o2.horizon_minutes==15)].iloc[0]
        self.assertNotEqual(a.max_up_from_preclose_pct,b.max_up_from_preclose_pct)

    def test_future_stage_ignored_at_old_pre(self):
        d=list(sample())
        p1,_,_=self.build(tuple(d))
        d[3]=d[3].copy()
        d[3].loc[d[3].region_id=='A','known_until']=[140*M,280*M]
        d[3]=pd.concat([d[3],pd.DataFrame([{
          'region_id':'A','side':'LOW','known_from':240*M,
          'known_until':280*M,'band_low':97.,'band_high':104.,
          'independent_extrema':8}])],ignore_index=True)
        p2,_,_=self.build(tuple(d))
        self.assertEqual(p1.current_stage_low.tolist(),p2.current_stage_low.tolist())
        self.assertEqual(p1.current_stage_extrema.tolist(),p2.current_stage_extrema.tolist())

    def test_expired_stage_is_not_an_executable_current_stage(self):
        p,_,s=self.build(max_stage_age_minutes=10)
        self.assertFalse(bool(p.stage_present_at_pre.any()))
        self.assertEqual(s['retrospective_no_current_stage_pre'],2)
        self.assertTrue(p.reference_mismatch_possible.all())
        self.assertTrue(p.current_stage_low.isna().all())

    def test_censored_post_is_null_not_zero(self):
        p,o,_=self.build()
        cens=o[(o.visit_number==2)&(o.horizon_minutes==180)].iloc[0]
        self.assertEqual(cens.status,'CUTOFF_CENSORED')
        self.assertTrue(pd.isna(cens.side_proxy_favorable_excursion_pct))
        self.assertTrue(pd.isna(cens.post_close_return_pct))
        self.assertLess(cens.observed_minutes,180)

    def test_touching_bar_cannot_be_replaced_by_non_touch_and_pass(self):
        d=list(sample())
        d[0].loc[100,'low']=105.
        with self.assertRaisesRegex(ValueError,'ARRIVAL_INVALID_FROZEN_OHLC'):
            self.build(tuple(d))
        d[0].loc[100,['open','low','close']]=[105.,104.,105.]
        with self.assertRaisesRegex(ValueError,'ARRIVAL_PARENT_FIRST_TOUCH_NOT_IN_BIRTH_BAND'):
            self.build(tuple(d))

    def test_invalid_gap_or_missing_ohlc_fails_closed(self):
        d=list(sample())
        d[0]=d[0].drop(index=5)
        with self.assertRaisesRegex(ValueError,'ARRIVAL_INCOMPLETE_FROZEN_1M'):
            self.build(tuple(d))

    def test_unsupported_horizon_fails_closed(self):
        with self.assertRaisesRegex(ValueError,'ARRIVAL_UNSUPPORTED_HORIZON'):
            self.build(horizons=(5,999))

    def test_arrival_event_id_is_not_a_signal_or_order(self):
        p,o,s=self.build()
        self.assertEqual(set(p.event_source),{'RETROSPECTIVE_V24A_BIRTH_FIXED_VISIT'})
        self.assertTrue(p.causal_note.str.contains('retrospectively').all())
        self.assertTrue(s['research_only'])
        self.assertTrue(s['no_live'])
        self.assertNotIn('tp', [c.lower() for c in p.columns])
        self.assertNotIn('pnl', [c.lower() for c in o.columns])

    def test_parent_immutable_rejects_existing_output(self):
        p,o,s=self.build()
        with tempfile.TemporaryDirectory() as root:
            folder=Path(root)
            parent=folder/'parent';parent.mkdir()
            (parent/'manifest.json').write_text('{}',encoding='utf-8')
            dest=folder/'arrivals'
            _write_immutable(dest,p,o,s,{'version':'parent'},parent,{'source_sha256':'test'})
            self.assertTrue((dest/'arrival_pre_v1.csv').is_file())
            self.assertTrue((dest/'arrival_post_outcomes_v1.csv').is_file())
            self.assertTrue((dest/'manifest.json').is_file())
            with self.assertRaises(FileExistsError):
                _write_immutable(dest,p,o,s,{'version':'parent'},parent,{})

    def test_plan_is_read_only_even_if_files_not_present(self):
        with tempfile.TemporaryDirectory() as root:
            rc=main(['--plan','--project-root',root])
            self.assertEqual(rc,0)
            self.assertEqual(list(Path(root).iterdir()),[])

    def test_current_bar_not_used_by_PRE_return_calculation(self):
        d=sample()
        p,_,_=self.build(d)
        i=100
        preclose=d[0].loc[i-1,'close']
        self.assertAlmostEqual(p.loc[0,'pre_close'],preclose)
        self.assertNotEqual(p.loc[0,'pre_close'],d[0].loc[i,'close'])

    def test_high_side_direction_proxy_is_short_not_long(self):
        highs=np.array([110.,111.,112.,109.,107.,108.])
        lows=np.array([108.,106.,105.,102.,100.,101.])
        closes=np.array([109.,108.,106.,104.,101.,105.])
        x=_post_horizon(closes,highs,lows,0,5,6,109.,'HIGH')
        _,state,_,_,up,down,fav,adv=x
        self.assertEqual(state,'FULL_HORIZON')
        self.assertAlmostEqual(fav,down)
        self.assertAlmostEqual(adv,up)

    def test_current_visit_future_closure_never_in_pre(self):
        d=list(sample())
        p1,_,_=self.build(tuple(d))
        d[2]=d[2].copy()
        d[2].loc[d[2].visit_number.eq(2),'end_known_ts']=250*M
        p2,_,_=self.build(tuple(d))
        pd.testing.assert_frame_equal(p1,p2)

    def test_first_touch_candle_ohcl_never_enters_pre_factors(self):
        d=list(sample())
        p1,o1,_=self.build(tuple(d))
        d[0]=d[0].copy()
        d[0].loc[100,'high']=160.
        p2,o2,_=self.build(tuple(d))
        pd.testing.assert_frame_equal(p1.loc[[0]],p2.loc[[0]])
        a=o1[(o1.visit_number==1)&(o1.horizon_minutes==5)].iloc[0]
        b=o2[(o2.visit_number==1)&(o2.horizon_minutes==5)].iloc[0]
        self.assertNotEqual(a.side_proxy_favorable_excursion_pct,
                            b.side_proxy_favorable_excursion_pct)

    def test_post_proxy_direction_and_reference(self):
        _,o,_=self.build()
        t=o[(o.visit_number==1)&(o.horizon_minutes==5)].iloc[0]
        self.assertAlmostEqual(t.side_proxy_favorable_excursion_pct,
                               t.max_up_from_preclose_pct)
        self.assertAlmostEqual(t.side_proxy_adverse_excursion_pct,
                               t.max_down_from_preclose_pct)


if __name__=='__main__':
    unittest.main()
