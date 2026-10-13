"""Runner Analyzer regression: correct control group, complete horizon, no lookahead."""
from __future__ import annotations
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import sys

import pandas as pd

from reaction_lab_runner_analyzer import (
    observed_horizon, load_eligible_outcomes, runner_analysis_frame,
    factor_buckets, factor_pair_matrix, factor_coverage, cohort_target_summary,
    spearman_feature_table, winner_control_medians, time_split_summary,
)
from reaction_lab_auto_paths import encode_path

T0=1_790_000_000_000


def path_for(side, horizon=60, up=0., low=0., gap=None):
    """Use prices around 100; move can be positive directional %."""
    out=[]
    for i in range(horizon):
        ts=T0+(i+1)*60000+(60000 if gap is not None and i>=gap else 0)
        hi=100*(1+up/100) if side=='LONG' else 100*(1+low/100)
        lo=100*(1-low/100) if side=='LONG' else 100*(1-up/100)
        out.append((ts,100.,max(100.,hi),min(100.,lo),100.))
    return out


class OutcomeTests(unittest.TestCase):
    def test_long_short_targets_strict_and_retest_excluded(self):
        for side in ('LONG','SHORT'):
            bars=path_for(side,60,up=2.2,low=1.2)
            scored=observed_horizon(bars,retest_timestamp=T0,retest_close=100,
                                    side=side,horizon=60)
            self.assertAlmostEqual(scored['run_horizon_pct'],2.2)
            self.assertAlmostEqual(scored['mae_horizon_pct'],1.2)
            self.assertTrue(scored['hit_2_0pct'])
            self.assertFalse(scored['hit_3_0pct'])
            self.assertEqual(scored['minutes_to_2_0pct'],1)
        exactly=observed_horizon(path_for('LONG',60,up=2.),
             retest_timestamp=T0,retest_close=100,side='LONG',horizon=60)
        self.assertFalse(exactly['hit_2_0pct'])  # strictly >, as runner archive

    def test_incomplete_horizon_never_becomes_loser(self):
        kw={'retest_timestamp':T0,'retest_close':100,'side':'LONG','horizon':60}
        self.assertIsNone(observed_horizon(path_for('LONG',59,up=10),**kw))
        self.assertIsNone(observed_horizon(path_for('LONG',60,up=10,gap=10),**kw))
        bad=path_for('LONG',60,up=10)
        bad[10]=(bad[10][0],100,99,101,100)
        self.assertIsNone(observed_horizon(bad,**kw))
        self.assertIsNone(observed_horizon([],**kw))
        self.assertIsNone(observed_horizon(path_for('LONG',60),**{**kw,'retest_close':None}))

    def test_exact_horizon_no_future_peek(self):
        bars=path_for('LONG',60,up=.1)
        bars+=path_for('LONG',61,up=20)[-1:]
        score=observed_horizon(bars,retest_timestamp=T0,retest_close=100,
                               side='LONG',horizon=60)
        self.assertFalse(score['hit_2_0pct'])
        self.assertAlmostEqual(score['run_horizon_pct'],.1)

    def test_database_only_base_and_complete_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            db=root/'reports/reaction_lab/auto_paths_v1.sqlite3'
            db.parent.mkdir(parents=True)
            with sqlite3.connect(db) as con:
                con.execute('CREATE TABLE events(event_key TEXT, path_blob BLOB, count INTEGER)')
                records=[('WIN|LONG|1',path_for('LONG',60,up=3)),
                         ('LOSE|SHORT|2',path_for('SHORT',60,up=.5)),
                         ('SHORT|LONG|3',path_for('LONG',59,up=8)),
                         ('GAP|LONG|4',path_for('LONG',60,up=11,gap=15)),
                         ('LEGACY|LONG|5',path_for('LONG',60,up=40))]
                con.executemany('INSERT INTO events VALUES (?,?,?)',
                       [(k,encode_path(path),len(path)) for k,path in records])
            all_reactions=pd.DataFrame([
                {'event_key':key,'retest_timestamp':T0,'retest_close':100.,
                 'signal':('SHORT' if 'LOSE' in key else 'LONG'),
                 'cohort':('Legacy' if 'LEGACY' in key else 'Continuous V1'),
                 'context_state':'READY','side_adjusted_strength_vs_btc_4h':1.}
                for key,_ in records
            ]+[{'event_key':'NO_PATH|LONG|6','retest_timestamp':T0,'retest_close':100.,
                'signal':'LONG','cohort':'Continuous V1'}])
            outcomes, q=load_eligible_outcomes(root,all_reactions,60)
            self.assertEqual(q['events'],5)
            self.assertEqual(q['eligible'],2)
            self.assertEqual(q['no_path'],1)
            self.assertEqual(q['short_path'],1)
            self.assertEqual(q['invalid_or_gapped'],1)
            eligible=runner_analysis_frame(all_reactions,outcomes)
            self.assertEqual(set(eligible.event_key),{'WIN|LONG|1','LOSE|SHORT|2'})
            self.assertEqual(int(eligible.hit_2_0pct.sum()),1)
            self.assertEqual(len(cohort_target_summary(eligible)),5)


class FactorTests(unittest.TestCase):
    def setUp(self):
        self.df=pd.DataFrame({
            'event_key':[f'{i}|LONG|{i}' for i in range(80)],
            'cohort':['Continuous V1']*80,
            'known_ts':[T0 + i*86_400_000 for i in range(80)],
            'run_horizon_pct':[3. if i%2==0 else .5 for i in range(80)],
            'hit_2_0pct':[i%2==0 for i in range(80)],
            'side_adjusted_strength_vs_btc_4h':[2. if i%2==0 else -2. for i in range(80)],
            'market_breadth_4h':[60. if i%2==0 else 20. for i in range(80)],
            'Market alignment':['TAILWIND' if i%2==0 else 'HEADWIND' for i in range(80)],
            'signal':['LONG']*80,
        })

    def test_buckets_use_winners_and_control_not_archive(self):
        scores=factor_buckets(self.df,'Strength BTC 4h (direccional)',2.,min_n=10)
        self.assertEqual(len(scores),2)
        self.assertEqual(set(scores['hit_rate_pct'].tolist()),{0.,100.})
        self.assertEqual(scores['n'].sum(),80)
        self.assertAlmostEqual(float(scores.iloc[0]['lift']),2.)
        pair=factor_pair_matrix(self.df,'Strength BTC 4h (direccional)',
                                'Market Flow',2.,min_n=10)
        self.assertEqual(len(pair),2)
        self.assertEqual(int(pair.n.sum()),80)

    def test_unavailable_btc_corr_not_invented(self):
        coverage=factor_coverage(self.df)
        btc=coverage[coverage['Factor'].str.startswith('BTC Corr')]
        self.assertEqual(int(btc['Con dato'].sum()),0)
        self.assertTrue(factor_buckets(self.df,'BTC Corr 5m / 1h',2.,min_n=5).empty)

    def test_correlations_descriptive_and_temporal_split(self):
        corrs=spearman_feature_table(self.df,25)
        self.assertAlmostEqual(float(corrs.set_index('Factor').loc[
            'Strength BTC 4h (direccional)','Spearman con Run']),1.)
        medians=winner_control_medians(self.df,2.,10)
        self.assertEqual(int(medians.iloc[0]['Ganadores N']),40)
        periods=time_split_summary(self.df,2.,10)
        self.assertEqual(len(periods),2)
        self.assertEqual(sum(periods['Eventos']),80)

    def test_missing_factor_and_no_sample_do_not_infer(self):
        df=self.df.copy()
        df['side_adjusted_strength_vs_btc_4h']=None
        self.assertTrue(factor_buckets(df,'Strength BTC 4h (direccional)',2.,10).empty)
        self.assertTrue(time_split_summary(df.iloc[:3],2.,10).empty)
        with self.assertRaises(ValueError):factor_buckets(df,'fake field',2.,10)


if __name__=='__main__':
    unittest.main()
