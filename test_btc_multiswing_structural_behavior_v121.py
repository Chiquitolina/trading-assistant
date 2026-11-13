#!/usr/bin/env python3
"""V1.2.1 tests: strict PRE, stage-asof, birth identity, immutable output."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

import btc_multiswing_structural_behavior_v121 as v
from btc_multiswing_structural_confluence_v2 import _region_id


def sample_frames():
    st = pd.DataFrame([
        {'region_id':'b','known_from':20*v.M,'known_until':80*v.M,
         'band_low':99.,'band_high':101.,'independent_extrema':2,'side':'HIGH'},
        {'region_id':'b','known_from':80*v.M,'known_until':300*v.M,
         'band_low':98.,'band_high':102.,'independent_extrema':3,'side':'HIGH'},
        {'region_id':'b','known_from':150*v.M,'known_until':350*v.M,
         'band_low':97.,'band_high':103.,'independent_extrema':4,'side':'HIGH'},
    ])
    def p(eid,minute,ref_known):
        return {'transition_id':eid,'episode_id':'ep','region_id':'b',
            'pre_cutoff_ts':minute*v.M,'pre_reference_stage_known':ref_known,
            'pre_episode_touch_occurred':minute>=60,
            'pre_stage_reference_low':98.,'pre_stage_reference_high':102.,
            'pre_1m_close':100.0,'pre_15m_close':100.0,'pre_1h_close':100.0,'pre_4h_close':100.0,
            'pre_1m_position':'INSIDE','pre_15m_position':'INSIDE',
            'pre_1h_position':'INSIDE','pre_4h_position':'INSIDE',
            'pre_1h_ret_4h_pct':1.0, 'pre_4h_ret_12h_pct':1.0}
    pre=pd.DataFrame([p('before_birth',10,False),p('at_touch_open',60,False),
                      p('after_touch_close',61,False),p('after_ref_stage',120,True),
                      p('after_future_stage',160,True)])
    visits=pd.DataFrame([{'event_id':'v1','episode_id':'ep','region_id':'b',
                         'pre_cutoff_ts':60*v.M,'stage_low':99.,'stage_high':101.},
                         {'event_id':'v2','episode_id':'ep','region_id':'b',
                         'pre_cutoff_ts':120*v.M,'stage_low':98.,'stage_high':102.}])
    observations=pd.DataFrame({'transition_id':pre.transition_id})
    physical=pd.DataFrame([{'physical_transition_id':'x','confirmation_ts':100*v.M,
          'source_tf':'M15_CLOSED','event_type':'BREAK_ABOVE_2C','direction':'ABOVE','region_id':'b'},
         {'physical_transition_id':'y','confirmation_ts':100*v.M,
          'source_tf':'M15_CLOSED','event_type':'BREAK_ABOVE_2C','direction':'ABOVE','region_id':'c'}])
    return st,pre,visits,observations,physical


class StagePolicyV121(unittest.TestCase):
    def test_asof_uses_old_stage_not_future_or_final_stage(self):
        st,pre,*_=sample_frames()
        out=v.lookup_asof(st,pre,key_col='transition_id',time_col='pre_cutoff_ts',lookback_days=7)
        d=out.set_index('transition_id')
        self.assertFalse(bool(d.loc['before_birth','asof_stage_known']))
        self.assertTrue(pd.isna(d.loc['before_birth','asof_stage_low']))
        self.assertEqual(float(d.loc['at_touch_open','asof_stage_low']),99.)
        self.assertEqual(float(d.loc['after_ref_stage','asof_stage_low']),98.)
        self.assertEqual(float(d.loc['after_future_stage','asof_stage_low']),97.)
        self.assertTrue((d.loc[d.asof_stage_known,'asof_stage_known_from']<=
                         pre.set_index('transition_id').loc[d[d.asof_stage_known].index,'pre_cutoff_ts']).all())

    def test_reference_mask_is_total_but_closed_independent_prices_remain(self):
        st,pre,visits,observations,physical=sample_frames()
        frames,counts=v.build_safe_tables(pre,visits,observations,physical,st,lookback_days=7)
        d=frames['transition_pre_safe_v121.csv'].set_index('transition_id')
        for key in ('before_birth','at_touch_open','after_touch_close'):
            for col in ('pre_stage_reference_low','pre_stage_reference_high',
                        'pre_1m_position','pre_15m_position','pre_1h_position','pre_4h_position'):
                self.assertTrue(pd.isna(d.loc[key,col]),(key,col))
            self.assertEqual(float(d.loc[key,'pre_1h_close']),100.)
            self.assertEqual(float(d.loc[key,'pre_1h_ret_4h_pct']),1.)
        self.assertFalse(d.loc['at_touch_open','pre_episode_touch_known'])
        self.assertTrue(d.loc['after_touch_close','pre_episode_touch_known'])
        self.assertEqual(int(counts['fixed_reference_masked_rows']),3)
        self.assertEqual(int(counts['events_cross_band_clock_collisions']),2)
        self.assertEqual(int(counts['pre_without_closed_first_touch']),2)
        self.assertEqual(d.loc['after_touch_close','pre_1m_position_asof'],'INSIDE')
        self.assertTrue(d.loc['after_future_stage','asof_stage_known_from']<=160*v.M)
        self.assertNotIn('event_type',d.columns)
        self.assertNotIn('confirmation_close',d.columns)
        self.assertTrue((d.pre_model_eligible_structure == (d.asof_stage_known &
                            d.pre_episode_touch_known)).all())

    def test_future_stage_cannot_replace_old_pre_on_append(self):
        st,pre,*_=sample_frames()
        one=v.lookup_asof(st.iloc[:2],pre,key_col='transition_id',
                          time_col='pre_cutoff_ts',lookback_days=7)
        two=v.lookup_asof(st,pre,key_col='transition_id',
                          time_col='pre_cutoff_ts',lookback_days=7)
        self.assertEqual(one.iloc[3].asof_stage_low,two.iloc[3].asof_stage_low)
        self.assertEqual(one.iloc[1].asof_stage_low,two.iloc[1].asof_stage_low)

    def test_visit_stage_audit_and_touch_not_known_yet(self):
        st,pre,visits,observations,physical=sample_frames()
        frames,counts=v.build_safe_tables(pre,visits,observations,physical,st,lookback_days=7)
        visits_out=frames['visit_pre_stage_v121.csv']
        self.assertTrue(visits_out.asof_stage_matches_visit_reference.all())
        self.assertFalse(visits_out.visit_touch_known_at_pre.any())
        self.assertEqual(counts['visit_stage_reference_drift'],0)

    def test_stage_expiry_is_visual_not_active_or_unconfirmed(self):
        st,pre,*_=sample_frames()
        p=pd.DataFrame([{'region_id':'b','transition_id':'late','pre_cutoff_ts':150*v.M+8*1440*v.M}])
        d=v.lookup_asof(st,p,key_col='transition_id',time_col='pre_cutoff_ts',lookback_days=7)
        self.assertTrue(d.loc[0,'asof_stage_known'])
        self.assertFalse(d.loc[0,'asof_visual_projection_within_lookback'])

    def test_parent_or_stage_id_mismatch_fails_closed(self):
        st,pre,visits,observations,physical=sample_frames()
        with self.assertRaisesRegex(ValueError,'V121_PRE_OBSERVATION_MISMATCH'):
            v.build_safe_tables(pre,visits,observations.iloc[:1],physical,st,lookback_days=7)
        with self.assertRaisesRegex(ValueError,'V121_NONUNIQUE_PRE_ID'):
            v.lookup_asof(st,pd.concat([pre,pre.iloc[:1]]),key_col='transition_id',
                          time_col='pre_cutoff_ts',lookback_days=7)

    def test_birth_reconstructs_with_same_identity_and_no_future_anchor(self):
        pivots=[]
        for n,(price,minute,available) in enumerate([(100.,10,20),(100.08,80,100),(100.13,150,160)]):
            pivots.append({'pivot_id':'p'+str(n),'physical_id':'f'+str(n),
                'physical_minute_ts':minute*v.M,'pivot_ts':minute*v.M,
                'available_ts':available*v.M,'price':price,'side':'HIGH',
                'timeframe':'15m','physical_identity_state':'VERIFIED'})
        catalog=pd.DataFrame(pivots)
        rid=_region_id('HIGH','f0','f1')
        births=pd.DataFrame([{'region_id':rid,'birth_known_ts':100*v.M,
             'band_low_birth':100.,'band_high_birth':100.08,'side':'HIGH'}])
        config={'timeframes':['15m'],'side':'BOTH','geometry':{'tolerance_pct':.2,
               'lookback_days':7,'separation_minutes':60}}
        stage=v.reconstruct_stage_history(catalog,source_end=180*v.M,
                        config=config,original_births=births)
        before=v.lookup_asof(stage,pd.DataFrame([{'region_id':rid,
           'transition_id':'before','pre_cutoff_ts':99*v.M}]),key_col='transition_id',
           time_col='pre_cutoff_ts',lookback_days=7)
        self.assertFalse(before.loc[0,'asof_stage_known'])
        later=v.lookup_asof(stage,pd.DataFrame([{'region_id':rid,
           'transition_id':'later','pre_cutoff_ts':165*v.M}]),key_col='transition_id',
           time_col='pre_cutoff_ts',lookback_days=7)
        self.assertEqual(later.loc[0,'asof_stage_extrema'],3)
        with self.assertRaisesRegex(ValueError,'V121_BIRTH_GEOMETRY_RECONSTRUCTION_DRIFT'):
            wrong=births.copy();wrong.loc[0,'band_high_birth']=101.
            v.reconstruct_stage_history(catalog,source_end=180*v.M,
                config=config,original_births=wrong)

    def test_immutable_does_not_replace_parent_outputs(self):
        st,pre,visits,observations,physical=sample_frames()
        frames,counts=v.build_safe_tables(pre,visits,observations,physical,st,lookback_days=7)
        frames['geometric_stage_history_v121.csv']=st
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);p12=root/'v12';p24=root/'v24';out=root/'v121'
            p12.mkdir();p24.mkdir()
            (p12/'manifest.json').write_text('parent unchanged')
            (p24/'manifest.json').write_text('v24 unchanged')
            source={'start':1,'end':2,'source_sha256':'source','catalog_sha256':'catalog'}
            v.write_immutable(out,frames,counts,root=root,v12=p12,v24a=p24,
                m12={},m24={},source=source)
            self.assertTrue((out/'manifest.json').exists())
            m=json.loads((out/'manifest.json').read_text())
            self.assertEqual(m['version'],v.VERSION)
            for name,hash_ in m['files_sha256'].items():
                self.assertEqual(v.digest(out/name),hash_)
            with self.assertRaisesRegex(FileExistsError,'V121_IMMUTABLE'):
                v.write_immutable(out,frames,counts,root=root,v12=p12,v24a=p24,
                    m12={},m24={},source=source)
            self.assertEqual((p12/'manifest.json').read_text(),'parent unchanged')

    def test_source_never_imports_dashboard_live_or_edits_parent(self):
        text=Path(v.__file__).read_text()
        for banned in ('engine.live','import streamlit','pm2 restart all'):
            self.assertNotIn(banned,text)
        self.assertNotIn('to_csv(v12',text)

    def test_plan_is_no_write(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(v.main(['--plan','--project-root',d]),0)
            self.assertEqual(list(Path(d).iterdir()),[])

    def test_real_v12_export_if_available(self):
        base=Path('/mnt/data/msv12audit')
        if not base.is_dir():
            self.skipTest('archived V1.2 results not mounted; synthetic tests sufficient')
        rows={name:pd.read_csv(base/name,low_memory=False) for name in
           ('transition_pre_v12.csv','transition_observations_v12.csv',
            'visit_pre_v12.csv','physical_transitions_v12.csv')}
        self.assertEqual(len(rows['transition_pre_v12.csv']),33821)
        self.assertEqual(int((~rows['transition_pre_v12.csv'].pre_reference_stage_known).sum()),48)
        # This test uses ONLY birth stages because full pivot catalog is hosted
        # on VPS; assert masking, not accurate as-of stage geometry here.
        b=pd.DataFrame({'region_id':rows['visit_pre_v12.csv'].region_id.unique()})
        # Fixture has no full stage evidence; all stage fields must stay null.
        empty=pd.DataFrame(columns=['region_id','known_from','band_low',
                                    'band_high','independent_extrema'])
        frames,counts=v.build_safe_tables(rows['transition_pre_v12.csv'],
                    rows['visit_pre_v12.csv'],rows['transition_observations_v12.csv'],
                    rows['physical_transitions_v12.csv'],empty,lookback_days=7)
        self.assertEqual(counts['fixed_reference_masked_rows'],48)
        self.assertEqual(counts['pre_without_closed_first_touch'],1117)
        self.assertEqual(counts['v12_touch_flag_corrected_rows'],64)
        safe=frames['transition_pre_safe_v121.csv']
        self.assertTrue((safe.pre_episode_touch_occurred==safe.pre_episode_touch_known).all())
        self.assertTrue(frames['transition_pre_safe_v121.csv'].loc[
            lambda x:x.reference_sanitized,'pre_1m_position'].isna().all())
        self.assertTrue(frames['transition_pre_safe_v121.csv'].asof_stage_low.isna().all())


if __name__=='__main__':
    unittest.main()
