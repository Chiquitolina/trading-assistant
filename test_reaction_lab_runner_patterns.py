"""Regression tests for read-only, causal multidimensional discovery."""
import unittest
import numpy as np
import pandas as pd

from reaction_lab_runner_patterns import discover_runner_patterns, DEFAULT_PATTERN_FACTORS, match_rule


class PatternExplorerTests(unittest.TestCase):
    def make_frame(self, days=12, per_day=100, reverse_validation=False):
        rng=np.random.default_rng(907)
        rows=[]
        base=pd.Timestamp('2026-09-15T00:00:00Z')
        for day in range(days):
            for i in range(per_day):
                room=bool(rng.integers(2));strength=bool(rng.integers(2));flow=bool(rng.integers(2))
                positive=room and strength and flow
                if reverse_validation and day>=9:
                    positive=not positive
                # Honest noisy outcomes, reproducible across whole series.
                hit=bool(positive) if rng.random()>.10 else not positive
                rows.append({'event_key':f'SYM{i}|LONG|{day*per_day+i}',
                    'known_ts':int((base+pd.Timedelta(days=day,minutes=i)).timestamp()*1000),
                    'hit_2_0pct':hit,'run_horizon_pct':3.1 if hit else .7,
                    'nearest_opposing_room_pct':3.5 if room else .7,
                    'side_adjusted_strength_vs_btc_4h':1.1 if strength else -1.1,
                    'Market alignment':'TAILWIND' if flow else 'HEADWIND',
                    'btc_corr_5m_4h': .12 if i%3==0 else .8})
        return pd.DataFrame(rows)

    def test_temporal_three_factor_search_find_and_validate(self):
        frame=self.make_frame()
        up,down,info=discover_runner_patterns(frame, target=2., horizon=180,min_n=20,
            factors=['HTF Room mínimo','Strength BTC 4h (direccional)','Market Flow'],
            depth=3,top_each=60)
        self.assertEqual(info['mode'],'TEMPORAL')
        self.assertTrue(up['N factores'].eq(3).any())
        self.assertTrue(up['Validación'].eq('MISMO SIGNO').any())
        self.assertGreater(info['candidate_evaluations'],0)
        self.assertGreater(len(down),0)
        self.assertLess(info['train_events']+info['validation_events'],len(frame)) if info['embargo'] else None

    def test_holdout_not_used_for_discovery_selection(self):
        good=self.make_frame()
        flipped=self.make_frame(reverse_validation=True)
        factors=['HTF Room mínimo','Strength BTC 4h (direccional)','Market Flow']
        a,_,ma=discover_runner_patterns(good,factors=factors,min_n=20,top_each=40)
        b,_,mb=discover_runner_patterns(flipped,factors=factors,min_n=20,top_each=40)
        # The same discovery rankings even when future validation outcomes differ.
        self.assertEqual(a['Regla'].tolist(),b['Regla'].tolist())
        self.assertEqual(a['Score exploratorio'].tolist(),b['Score exploratorio'].tolist())
        self.assertNotEqual(a['Diferencia validación pp'].tolist(),b['Diferencia validación pp'].tolist())
        self.assertEqual(ma['mode'],mb['mode'])

    def test_missing_btc_never_counted_as_zero_or_in_control(self):
        frame=self.make_frame(days=3,per_day=80)
        frame.loc[frame.index[:160],'btc_corr_5m_4h']=np.nan
        up,down,info=discover_runner_patterns(frame,min_n=10,
            factors=['BTC Corr 5m / 4h'],depth=1)
        self.assertEqual(info['mode'],'EXPLORATORIO')
        for table in [up,down]:
            if len(table):
                self.assertTrue((table['N exploración']+table['N resto comparable']<=80).all())

    def test_no_outcome_or_noncausal_factor_is_rejected(self):
        f=self.make_frame(days=2,per_day=10)
        with self.assertRaises(ValueError):
            discover_runner_patterns(f,factors=['run_horizon_pct'])
        with self.assertRaises(ValueError):
            discover_runner_patterns(f,factors=['BTC context status'])
        with self.assertRaises(ValueError):
            discover_runner_patterns(f,factors=DEFAULT_PATTERN_FACTORS*3)
        with self.assertRaises(ValueError):
            discover_runner_patterns(f.assign(event_key='DUPLICATE'))
        f['hit_2_0pct']=f['hit_2_0pct'].astype(object)
        f.loc[1,'hit_2_0pct']=None
        with self.assertRaises(ValueError):
            discover_runner_patterns(f)

    def test_inspector_rule_reproduces_exact_bucket_intersection(self):
        f=self.make_frame(days=2,per_day=50)
        rule='HTF Room mínimo = [3.0, 5.0) ∧ Market Flow = TAILWIND'
        mask=match_rule(f,rule)
        expect=f['nearest_opposing_room_pct'].eq(3.5) & f['Market alignment'].eq('TAILWIND')
        self.assertEqual(mask.tolist(),expect.tolist())
        with self.assertRaises(ValueError):
            match_rule(f,'run_horizon_pct = [2.0, inf)')

    def test_all_losers_cannot_generate_fake_positive_lift(self):
        f=self.make_frame(days=2,per_day=120)
        f['hit_2_0pct']=False
        up,down,_=discover_runner_patterns(f,factors=['HTF Room mínimo','Market Flow'],min_n=10)
        self.assertTrue(up.empty)
        self.assertTrue(down.empty)


if __name__=='__main__':unittest.main()
