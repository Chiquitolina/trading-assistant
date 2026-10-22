"""Offline guards for exploratory reverse-engineering screen, without LIVE."""
from pathlib import Path
import sys
import unittest
import pandas as pd
import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parent))
from dashboard.services.reaction_reverse_engineering import (
    one_per_symbol_wave, chronological_split, mask_for,
    metrics, screen_rules, evaluate_rule_days,
)


def fixture(days=10, per_day=140):
    events=[]
    start=1_792_000_000_000
    for day in range(days):
        for j in range(per_day):
            # IDs independent enough for purging, two directions and market regimes
            k=j%70
            symbol=f"SYM{k:02d}USDT"
            x=(j % 10)/10 + 0.03*day
            win=(x>0.4 and (j%5 != 0)) or (j%11==0)
            events.append({'event_key':f'{day}:{j}', 'symbol':symbol,
                  'side':'LONG' if j%2 else 'SHORT',
                  'retest_ts':start+day*86400_000+j*600_000,
                  'date_utc':f'2026-10-{11+day:02d}',
                  'outcome':'TP_FIRST' if win else 'SL_FIRST',
                  'f':x, 'pre_realized_vol60_pct': 0.3+(j%13)/10})
    return pd.DataFrame(events)


class ReverseEngineeringTests(unittest.TestCase):
    def test_first_member_by_timestamp_not_outcome(self):
        a=fixture(days=4,per_day=3)
        a.loc[a.index[0],'symbol']='AAAUSDT'
        a.loc[a.index[1],'symbol']='AAAUSDT'
        a.loc[a.index[1],'retest_ts']=int(a.loc[0,'retest_ts'])+60_000
        a.loc[a.index[0],'outcome']='NO_HIT'
        selected,removed=one_per_symbol_wave(a,180)
        self.assertIn(a.loc[0,'event_key'],set(selected.event_key))
        self.assertNotIn(a.loc[1,'event_key'],set(selected.event_key))
        self.assertGreater(removed,0)

    def test_dates_never_shared(self):
        f=fixture()
        a,b,early,late=chronological_split(f)
        self.assertFalse(set(a.date_utc)&set(b.date_utc))
        self.assertEqual(len(early),6)
        self.assertEqual(len(late),4)

    def test_frozen_thresholds_ignore_changed_heldout_predictors(self):
        data=fixture()
        a,summary,train,held=screen_rules(data,['f'],min_train=45,purge_minutes=0,top=30)
        self.assertGreater(len(a),0)
        copy=data.copy()
        # Change holdout features strongly but retain early samples intact.
        copy.loc[copy.date_utc.isin(summary['held_days']),'f']=9999.0
        b,summary2,_,_=screen_rules(copy,['f'],min_train=45,purge_minutes=0,top=30)
        self.assertEqual([str(r) for r in a.rule], [str(r) for r in b.rule])
        self.assertEqual(summary['train_events'],summary2['train_events'])

    def test_missing_outcome_and_nan_never_form_tp(self):
        f=fixture(days=4,per_day=100)
        f.loc[f.index[:20],'outcome']='NO_HIT'
        f.loc[f.index[:30],'f']=np.nan
        selection=mask_for(f,(('f','>=',.4),))
        self.assertFalse(selection.iloc[:30].any())
        a,info,_,_=screen_rules(f,['f'],min_train=30,purge_minutes=0)
        self.assertEqual(info['train_events']+info['held_events'],len(f)-20)
        self.assertTrue(all(x==x for x in a.train_tp_rate))

    def test_proxy_resolved_only_is_explicit(self):
        x=pd.DataFrame({'outcome':['TP_FIRST','SL_FIRST','NO_HIT','AMBIGUOUS']})
        z=metrics(x,1,.5,.12)
        self.assertEqual(z['resolved'],2)
        self.assertEqual(z['no_hit'],1)
        self.assertAlmostEqual(z['resolved_net_proxy_pct'],.13)

    def test_all_same_symbol_no_independent_days(self):
        f=fixture(days=4,per_day=10)
        f['symbol']='ALLUSDT'
        selected,excluded=one_per_symbol_wave(f,180)
        self.assertGreater(excluded,0)
        self.assertEqual(len(f),len(selected)+excluded)


if __name__=='__main__':unittest.main()
