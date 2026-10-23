import copy
import json
from pathlib import Path
import tempfile
import unittest

import failed_touch_causal_validation_50_v1 as m
import failed_touch_causal_2pct_v1 as core


def snap(key,side='LONG',check=0,wick=0.6,ret=0.3,struct='RECOVERY_FIRST',trade='TP_FIRST',gross=2.):
    return dict(event_key=key,symbol='AUSDT',side=side,first_touch_ts='1000',
                cluster_key='AUSDT|'+side+'|1000',checkpoint_min=str(check),
                checkpoint_status='ACTIVE',favorable_wick_share=str(wick),
                favorable_return_5m_pct=str(ret),
                structural_outcome=struct,trade_outcome=trade,trade_gross_pct=str(gross))

class Tests(unittest.TestCase):
    def test_rules_are_frozen_and_use_only_features(self):
        row=snap('x')
        self.assertTrue(m.eligible(row,m.RULES['A_WICK50_T0']))
        bad=dict(row,favorable_wick_share='0.49',structural_outcome='RECOVERY_FIRST')
        self.assertFalse(m.eligible(bad,m.RULES['A_WICK50_T0']))
        good=dict(row,structural_outcome='CONTINUATION_FIRST',trade_outcome='SL_FIRST')
        self.assertTrue(m.eligible(good,m.RULES['A_WICK50_T0']))
        self.assertFalse(m.eligible(row,m.RULES['B_RETURN5_POS_T15']))
        self.assertTrue(m.eligible(snap('q',check=15),m.RULES['B_RETURN5_POS_T15']))
        self.assertFalse(m.eligible(snap('q',check=15,ret=0),m.RULES['B_RETURN5_POS_T15']))

    def test_reject_resolved_and_missing_signal(self):
        row=snap('x');row['checkpoint_status']='ALREADY_EXTENDED_3'
        self.assertFalse(m.eligible(row,m.RULES['A_WICK50_T0']))
        row['checkpoint_status']='ACTIVE';row['favorable_wick_share']=''
        self.assertFalse(m.eligible(row,m.RULES['A_WICK50_T0']))

    def test_dedup_before_selection_not_cherrypicked(self):
        rows=[snap('A',wick=0.3),snap('B',wick=0.9)]
        pool=m.deduplicate(rows)
        self.assertEqual(len(pool),1)
        self.assertEqual(pool[0]['event_key'],'A')
        self.assertEqual(sum(m.eligible(x,m.RULES['A_WICK50_T0']) for x in pool),0)

    def test_summary_accounting_timeout_and_net(self):
        rows=[snap('a',wick=.6,struct='RECOVERY_FIRST',trade='TP_FIRST',gross=2.),
              snap('b',wick=.2,struct='CONTINUATION_FIRST',trade='SL_FIRST',gross=-1.5),
              snap('c',wick=.6,struct='NO_DECISION',trade='TIMEOUT',gross=.4)]
        rows[1]['cluster_key']='b';rows[2]['cluster_key']='c'
        out=m.compute_summary(rows)
        selected=next(x for x in out if x['rule']=='A_WICK50_T0' and x['dedup']=='ALL_LEVELS'
                      and x['direction']=='ALL' and x['group']=='SELECTED')
        baseline=next(x for x in out if x['rule']=='A_WICK50_T0' and x['dedup']=='ALL_LEVELS'
                      and x['direction']=='ALL' and x['group']=='BASELINE')
        self.assertEqual(selected['n'],2)
        self.assertEqual(selected['no_decision'],1)
        self.assertEqual(selected['recovery_decided_pct'],100.)
        self.assertEqual(selected['net_mean_cost_0_10_pct'],1.1)
        self.assertEqual(baseline['n'],3)

    def test_unicode_safe_and_path_injection_rejected(self):
        self.assertTrue(m.symbol_is_safe('币安人生USDT'))
        self.assertFalse(m.symbol_is_safe('../BADUSDT'))
        self.assertFalse(m.symbol_is_safe('a-USDT'))

    def test_freeze_random_deterministic_50_of_257_and_refuse_retroactive_change(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            _,_,oldout,dev,study,src=m.get_paths(root)
            src.mkdir(parents=True)
            syms=sorted(set(['ADAUSDT','1000PEPEUSDT','1000BONKUSDT']+
                [f'Q{i:03}USDT' for i in range(286)]+['币安人生USDT']))
            self.assertEqual(len(syms),290)
            for s in syms:(src/s).mkdir()
            dev.mkdir(parents=True)
            devsyms=syms[:30]+sorted(core.PILOT)
            devsyms=sorted(set(devsyms))
            if len(devsyms)<33:
                devsyms=sorted(set(devsyms+syms[30:30+(33-len(devsyms))]))
            self.assertEqual(len(devsyms),33)
            core.atomic_json(dev/'frozen_development_universe.json',dict(version=core.VERSION,symbols=devsyms))
            frozen,new=m.freeze(root)
            self.assertTrue(new)
            self.assertEqual(len(frozen['validation_symbols']),50)
            self.assertEqual(len(frozen['reserved_unselected_symbols']),207)
            self.assertTrue((study/'holdout50_symbols.txt').is_file())
            self.assertEqual((study/'holdout50_symbols.txt').read_text(encoding='utf-8').splitlines(),frozen['validation_symbols'])
            self.assertFalse(set(frozen['validation_symbols']) & set(frozen['reserved_unselected_symbols']))
            self.assertEqual(len(set(frozen['validation_symbols']+frozen['reserved_unselected_symbols'])),257)
            self.assertFalse(set(devsyms)&set(frozen['validation_symbols']))
            self.assertEqual(m.locked(root),frozen)
            new2,created=m.freeze(root)
            self.assertFalse(created)
            (study/'holdout50_symbols.txt').write_text('FRAUDUSDT\n',encoding='utf-8')
            with self.assertRaises(RuntimeError):m.locked(root)
            (study/'holdout50_symbols.txt').write_text('\n'.join(frozen['validation_symbols'])+'\n',encoding='utf-8')
            src.joinpath('NEWUSDT').mkdir()
            with self.assertRaises(RuntimeError):m.locked(root)

if __name__=='__main__':unittest.main()

# Additional test can be discovered with unittest loading even though this
# test class appears after the module's optional __main__ guard.
class NoPeekingTests(unittest.TestCase):
    def test_partial_holdout_report_never_exposes_labels(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            _,_,_,_,study,_=m.get_paths(root)
            with patch.object(m,'verified_completion',return_value=({'validation_symbols':['AAA']},['AAA'],[])):
                progress=m.report(root)
            self.assertEqual(progress['is_final'],False)
            self.assertTrue(progress['holdout_outcomes_sealed'])
            self.assertFalse((study/'holdout_frozen_rules_summary.csv').exists())
            self.assertTrue((study/'holdout_progress.json').exists())
