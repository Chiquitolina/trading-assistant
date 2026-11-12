#!/usr/bin/env python3
"""Causal Arrival × REACTION V1 unit tests; no LIVE or real account access."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from btc_multiswing_arrival_reaction_join_v1 import (
    JOIN_PRE_COLUMNS, POST_SUMMARY_COLUMNS, JOIN_POST_COLUMNS, M, _check_reactions,
    _parse_price, join_arrivals_to_reactions, read_reaction_journal,
    _write_immutable, load_arrival_run, main, _sha,
)


def sample_pre():
    return pd.DataFrame([
        dict(event_id='A|V1|6000000',region_id='A',visit_number=1,side='LOW',
             pre_cutoff_ts=100*M,stage_present_at_pre=True,
             current_stage_low=99.,current_stage_high=101.,
             reference_mismatch_possible=False,
             execution_eligibility='STRUCTURAL_STAGE_PRESENT_NOT_TRADE_SIGNAL'),
        dict(event_id='B|V1|9000000',region_id='B',visit_number=1,side='HIGH',
             pre_cutoff_ts=150*M,stage_present_at_pre=False,
             current_stage_low=None,current_stage_high=None,
             reference_mismatch_possible=True,
             execution_eligibility='RETROSPECTIVE_BIRTH_BAND_NO_STAGE_AT_PRE'),
    ])


def sample_reactions():
    return pd.DataFrame([
        dict(touch_key='known_aligned',symbol='BTCUSDT',side='LONG',
             status='REACTION',retest_ts=95*M,known_ts=96*M,swing_price=100.),
        dict(touch_key='known_opposite',symbol='BTCUSDT',side='SHORT',
             status='TOUCH_FAILED',retest_ts=98*M,known_ts=99*M,swing_price=100.2),
        dict(touch_key='on_arrival_open',symbol='BTCUSDT',side='LONG',
             status='REACTION',retest_ts=100*M,known_ts=101*M,swing_price=100.1),
        dict(touch_key='far_price',symbol='BTCUSDT',side='LONG',
             status='REACTION',retest_ts=101*M,known_ts=102*M,swing_price=150.),
        dict(touch_key='missing_price',symbol='BTCUSDT',side='LONG',
             status='TOUCH_FAILED',retest_ts=102*M,known_ts=103*M,swing_price=None),
        dict(touch_key='late',symbol='BTCUSDT',side='SHORT',
             status='TOUCH_INDECISIVE',retest_ts=104*M,known_ts=105*M,swing_price=100.2),
    ])


class JoinCausality(unittest.TestCase):
    def build(self,p=None,r=None,end=400*M):
        return join_arrivals_to_reactions(p if p is not None else sample_pre(),
                                         r if r is not None else sample_reactions(),
                                         freeze_end=end)

    def test_pre_known_counts_and_post_split(self):
        pre,post_summary,post,counts=self.build()
        a=pre.iloc[0]
        self.assertEqual(a['pre_near_known_240m'],2)
        self.assertEqual(a['pre_near_known_60m'],2)
        self.assertEqual(a['pre_aligned_reaction_240m'],1)
        self.assertEqual(a['pre_opposite_known_240m'],1)
        self.assertEqual(a['pre_last_near_touch_key'],'known_opposite')
        self.assertEqual(post_summary.iloc[0]['post_near_5m'],2)
        self.assertEqual(post_summary.iloc[0]['post_first_aligned_reaction_known_ts'],101*M)
        self.assertEqual(set(post['reaction_touch_key']),{'on_arrival_open','late'})
        self.assertEqual(counts['post_status_counts']['TOUCH_INDECISIVE'],1)
        self.assertEqual(list(pre.columns),JOIN_PRE_COLUMNS)
        self.assertFalse(any(c.startswith('post_') for c in pre.columns))
        self.assertEqual(list(post_summary.columns),POST_SUMMARY_COLUMNS)
        self.assertEqual(list(post.columns),JOIN_POST_COLUMNS)

    def test_post_is_not_pre_when_retest_is_arrival_open(self):
        p,post_summary,post,_=self.build()
        self.assertEqual(p.iloc[0]['pre_aligned_reaction_240m'],1)
        r=post.loc[post.reaction_touch_key.eq('on_arrival_open')].iloc[0]
        self.assertEqual(r.minutes_after_arrival_open,1)
        self.assertGreater(r.reaction_known_ts,r.pre_cutoff_ts)

    def test_stage_unavailable_is_unknown_not_negative(self):
        p,post_summary,post,_=self.build()
        b=p.iloc[1]
        b_post=post_summary.iloc[1]
        self.assertEqual(b['matching_status'],'UNKNOWN_NO_PRE_STAGE')
        self.assertTrue(pd.isna(b['pre_near_known_240m']))
        self.assertTrue(pd.isna(b_post['post_near_180m']))
        self.assertFalse(post.event_id.eq(b.event_id).any())

    def test_changed_future_events_cannot_change_pre(self):
        a,_,_,_=self.build()
        x=sample_reactions().copy()
        x.loc[x.touch_key=='late','swing_price']=300
        x.loc[x.touch_key=='on_arrival_open','status']='TOUCH_FAILED'
        b,_,_,_=self.build(r=x)
        for col in [c for c in JOIN_PRE_COLUMNS if c.startswith('pre_')]:
            self.assertEqual(str(a.iloc[0][col]),str(b.iloc[0][col]),col)

    def test_spatial_nonidentity_and_side_not_silently_filtered(self):
        _,_,post,_=self.build()
        other=post.loc[post.reaction_touch_key.eq('late')].iloc[0]
        self.assertEqual(other.direction_relation,'OPPOSITE')
        self.assertIn('NO_SHARED_TOUCH',other.matched_by)
        self.assertEqual(other.price_relation,'INSIDE')

    def test_bad_causal_timeline_rejected(self):
        r=sample_reactions().copy()
        r.loc[0,'known_ts']=r.loc[0,'retest_ts']
        with self.assertRaisesRegex(ValueError,'KNOWN_TS'):
            self.build(r=r)

    def test_duplicate_keys_rejected(self):
        r=pd.concat([sample_reactions(),sample_reactions().iloc[[0]]])
        with self.assertRaisesRegex(ValueError,'DUPLICATED'):
            self.build(r=r)

    def test_invalid_reaction_state_rejected(self):
        r=sample_reactions().copy()
        r.loc[0,'status']='BROKEN'
        with self.assertRaisesRegex(ValueError,'STATUS'):
            self.build(r=r)

    def test_freeze_censored_is_null_not_zero(self):
        pre=sample_pre().iloc[[0]].copy()
        pre.loc[pre.index[0],'pre_cutoff_ts']=100*M
        p,post_summary,post,counts=self.build(p=pre,r=sample_reactions().loc[
            sample_reactions().known_ts<=104*M].copy(),end=104*M)
        self.assertEqual(post_summary.iloc[0]['post_180m_coverage'],'CUTOFF_CENSORED')
        self.assertEqual(post_summary.iloc[0]['post_available_minutes'],4)
        self.assertTrue(pd.isna(post_summary.iloc[0]['post_near_180m']))
        self.assertEqual(counts['arrival_post_180m_censored'],1)
        self.assertLessEqual(post.reaction_known_ts.max(),104*M)

    def test_unpriced_count_not_silent_no_match(self):
        pre,post_summary,_,_=self.build()
        self.assertEqual(post_summary.iloc[0]['post_near_unpriced_events_180m'],1)

    def test_parse_level_from_causal_json_and_unknown(self):
        self.assertEqual(_parse_price('{"pivot_price": 101.2}'),101.2)
        self.assertEqual(_parse_price('{"swing_price": "100.1"}'),100.1)
        self.assertIsNone(_parse_price('{"irrelevant":1}'))
        with self.assertRaisesRegex(ValueError,'INVALID_CAUSAL_JSON'):
            _parse_price('{bad')

    def test_sqlite_readonly_and_causal_close(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'reaction.sqlite3'
            db=sqlite3.connect(p)
            try:
                db.execute('CREATE TABLE first_touches (touch_key TEXT, symbol TEXT, side TEXT, status TEXT, retest_ts INTEGER, known_ts INTEGER, causal_json TEXT)')
                db.execute('INSERT INTO first_touches VALUES (?,?,?,?,?,?,?)',
                           ('x','BTCUSDT','LONG','REACTION',95*M,96*M,json.dumps({'swing_price':100.})))
                db.execute('INSERT INTO first_touches VALUES (?,?,?,?,?,?,?)',
                           ('other','ETHUSDT','LONG','REACTION',95*M,96*M,json.dumps({'swing_price':100.})))
                db.commit()
            finally:
                db.close()
            x,meta=read_reaction_journal(p,freeze_end=200*M)
            self.assertEqual(x.touch_key.tolist(),['x'])
            self.assertEqual(x.swing_price.iloc[0],100.)
            self.assertEqual(meta['source'],'CANONICAL_SCANNER_FIRST_TOUCHES_SQLITE_READONLY')
            self.assertEqual(len(meta['snapshot_sha256']),64)

    def test_sqlite_unknown_journal_schema_fails_closed(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'bad.sqlite3'
            sqlite3.connect(p).close()
            with self.assertRaisesRegex(ValueError,'SCHEMA_MISSING'):
                read_reaction_journal(p,freeze_end=200*M)

    def test_immutable_output_has_hashes(self):
        p,ps,o,counts=self.build()
        with tempfile.TemporaryDirectory() as t:
            root=Path(t)
            arrival=root/'arrival';arrival.mkdir()
            (arrival/'manifest.json').write_text('{}')
            target=root/'joined'
            _write_immutable(target,p,ps,o,counts,arrival,{},
                   {'snapshot_sha256':'a'*64,'source':'TEST'},
                   {'tolerance_pct':0.10})
            self.assertTrue((target/'manifest.json').is_file())
            self.assertEqual(len(pd.read_csv(target/'arrival_reaction_pre_v1.csv')),2)
            self.assertEqual(len(pd.read_csv(target/'arrival_reaction_post_matches_v1.csv')),2)
            self.assertEqual(len(pd.read_csv(target/'arrival_reaction_post_summary_v1.csv')),2)
            with self.assertRaisesRegex(FileExistsError,'IMMUTABLE'):
                _write_immutable(target,p,ps,o,counts,arrival,{},
                    {'snapshot_sha256':'a'*64},{})

    def test_end_to_end_join_cli_and_parent_validation(self):
        from btc_multiswing_arrival_lab_v1 import VERSION as ARRIVAL_VERSION
        with tempfile.TemporaryDirectory() as t:
            root=Path(t)
            arrival=root/'arrival';arrival.mkdir()
            pre=sample_pre()
            pre.to_csv(arrival/'arrival_pre_v1.csv',index=False)
            pd.DataFrame({'event_id':['dummy'],'horizon_minutes':[5]}).to_csv(
                arrival/'arrival_post_outcomes_v1.csv',index=False)
            provenance={'start':30*M,'end':400*M,'source_sha256':'a'*64}
            summary={'version':ARRIVAL_VERSION,'freeze_end_exclusive':400*M,
                     'source_provenance':provenance}
            (arrival/'summary_v1.json').write_text(json.dumps(summary))
            manifest={'version':ARRIVAL_VERSION,'immutable_run':True,
                      'research_only':True,'no_live':True,'source_provenance':provenance,
                      'counts':{'arrivals':2},
                      'sha256':{name:_sha(arrival/name) for name in
                       ('arrival_pre_v1.csv','arrival_post_outcomes_v1.csv','summary_v1.json')}}
            (arrival/'manifest.json').write_text(json.dumps(manifest))
            db=root/'reaction.sqlite3'
            con=sqlite3.connect(db)
            try:
                con.execute('CREATE TABLE first_touches (touch_key TEXT, symbol TEXT, side TEXT, status TEXT, retest_ts INTEGER, known_ts INTEGER, causal_json TEXT)')
                for r in sample_reactions().itertuples(index=False):
                    payload=json.dumps({'swing_price':r.swing_price}) if pd.notna(r.swing_price) else '{}'
                    con.execute('INSERT INTO first_touches VALUES (?,?,?,?,?,?,?)',
                                (r.touch_key,r.symbol,r.side,r.status,int(r.retest_ts),int(r.known_ts),payload))
                con.commit()
            finally:
                con.close()
            result=main(['--run','--project-root',str(root),'--arrival-run','arrival',
                         '--reaction-db','reaction.sqlite3','--output-dir','results'])
            self.assertEqual(result,0)
            output=root/'results'
            self.assertEqual(len(pd.read_csv(output/'arrival_reaction_pre_v1.csv')),2)
            self.assertEqual(len(pd.read_csv(output/'arrival_reaction_post_summary_v1.csv')),2)
            self.assertEqual(len(pd.read_csv(output/'arrival_reaction_post_matches_v1.csv')),2)
            self.assertTrue((output/'manifest.json').exists())
            with self.assertRaisesRegex(FileExistsError,'IMMUTABLE'):
                main(['--run','--project-root',str(root),'--arrival-run','arrival',
                      '--reaction-db','reaction.sqlite3','--output-dir','results'])
            (arrival/'arrival_pre_v1.csv').write_text('modified')
            with self.assertRaisesRegex(ValueError,'HASH_CHANGED'):
                load_arrival_run(arrival)

    def test_empty_reaction_universe_is_not_a_positive_signal(self):
        empty=sample_reactions().iloc[0:0]
        a,ps,b,c=self.build(r=empty)
        self.assertEqual(len(a),2)
        self.assertEqual(len(ps),2)
        self.assertEqual(len(b),0)
        self.assertEqual(c['reaction_event_rows_in_scope'],0)

    def test_snapshot_will_not_mutate_live_imports(self):
        from pathlib import Path
        src=Path(__file__).with_name('btc_multiswing_arrival_reaction_join_v1.py').read_text()
        for dangerous in ('import redis','requests.get','pm2 restart','engine.live','sqlite3.connect(db_path)'):
            self.assertNotIn(dangerous,src)
        self.assertIn('mode=ro',src)

if __name__=='__main__':
    unittest.main()
