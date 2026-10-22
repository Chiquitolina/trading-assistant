import unittest
from unittest.mock import patch
from pathlib import Path
from datetime import timedelta
import sqlite3
import sys
import tempfile
import types

import pandas as pd

from reaction_horizontal_pre_structure import (
    MINUTE, TF_STEPS, classify_structure_pre, enrich_pre_structure,
)
from dashboard.views.reaction_horizontal_level_rich_card_view import normalized_outcome, outcome_label
from reaction_horizontal_level_card import summary_by, groups_summary


def make_bars(tf='15m', kind='HIGH', value1=110, value2=108, approach='BAJISTA'):
    step=TF_STEPS[tf]
    n=96 if tf=='15m' else 48
    time=[i*step for i in range(n)]
    highs=[105.0]*n
    lows=[97.0]*n
    for i,v in ((n//3,value1),(n-18,value2)):
        if kind=='HIGH':highs[i]=v
        else:lows[i]=v
    o=[100.0]*n
    c=[101.0]*n
    # A short approach is intentionally independent of older structural pivots.
    # 120m HTF closes form a contiguous local upward/downward trend.
    k=120//(step//MINUTE)
    if approach=='ALCISTA':
        for j in range(k+1): c[n-k-1+j]=99.0+0.35*j
    elif approach=='BAJISTA':
        for j in range(k+1): c[n-k-1+j]=101.0-0.30*j
    return pd.DataFrame({'timestamp':time,'open':o,'high':highs,'low':lows,'close':c}).set_index('timestamp')


def ev(tf='15m',role='ABOVE',side='LONG'):
    return dict(retest_ts=24*60*MINUTE,all_covered=1,
                primary_timeframe=tf,primary_distance_atr=.1,
                arrival_origin_pre=role,side=side)


class StructuralPressureTests(unittest.TestCase):
    def test_eva_long_lower_highs_against_support(self):
        bars=make_bars(kind='HIGH',value1=111,value2=109)
        s=classify_structure_pre(ev(),bars)
        self.assertEqual(s['structure_pre'],'LH_HACIA_SOPORTE')
        self.assertEqual(s['structure_relation_pre'],'PRESION_CONTRA_REACTION')
        self.assertEqual((s['structure_pivot_first'],s['structure_pivot_second']),(111,109))
    def test_bull_short_higher_lows_into_resistance(self):
        bars=make_bars(kind='LOW',value1=94,value2=95,approach='ALCISTA')
        s=classify_structure_pre(ev(role='BELOW',side='SHORT'),bars)
        self.assertEqual(s['structure_pre'],'HL_HACIA_RESISTENCIA')
        self.assertEqual(s['structure_relation_pre'],'PRESION_CONTRA_REACTION')
    def test_pressure_with_trade_direction_does_not_become_confirmed_breakout(self):
        s=classify_structure_pre(ev(role='BELOW',side='LONG'),
                                 make_bars(kind='LOW',value1=94,value2=95,approach='ALCISTA'))
        self.assertEqual(s['structure_relation_pre'],'PRESION_A_FAVOR_REACTION')
    def test_short_support_falling_highs_is_pressure_with_short(self):
        s=classify_structure_pre(ev(side='SHORT'),
                                 make_bars(kind='HIGH',value1=111,value2=109))
        self.assertEqual(s['structure_relation_pre'],'PRESION_A_FAVOR_REACTION')
    def test_enso_short_lower_highs_but_rebound_is_mixed_and_recent_against_short(self):
        s=classify_structure_pre(ev(side='SHORT'),
             make_bars(kind='HIGH',value1=111,value2=109,approach='ALCISTA'))
        self.assertEqual(s['structure_pre'],'LH_HACIA_SOPORTE')
        self.assertEqual(s['structure_recent_direction_pre'],'ALCISTA')
        self.assertEqual(s['structure_recent_vs_reaction_pre'],'CONTRA_REACTION')
        self.assertEqual(s['structure_relation_pre'],'PRESION_MIXTA')
    def test_rebound_long_into_support_after_lh_is_mixed_and_recent_with_long(self):
        s=classify_structure_pre(ev(side='LONG'),
             make_bars(kind='HIGH',value1=111,value2=109,approach='ALCISTA'))
        self.assertEqual(s['structure_recent_vs_reaction_pre'],'A_FAVOR_REACTION')
        self.assertEqual(s['structure_relation_pre'],'PRESION_MIXTA')
    def test_side_by_side_same_pivots_change_relation_without_POST(self):
        e=ev(role='ABOVE',side='SHORT')
        down=classify_structure_pre(e,make_bars(kind='HIGH',value1=111,value2=109,approach='BAJISTA'))
        up=classify_structure_pre(e,make_bars(kind='HIGH',value1=111,value2=109,approach='ALCISTA'))
        self.assertEqual(down['structure_relation_pre'],'PRESION_A_FAVOR_REACTION')
        self.assertEqual(up['structure_relation_pre'],'PRESION_MIXTA')
    def test_current_incomplete_HTF_is_excluded(self):
        e=ev(role='BELOW',side='SHORT')
        pre=make_bars(kind='LOW',value1=94,value2=95,approach='ALCISTA')
        s1=classify_structure_pre(e,pre)
        late=pre.copy()
        # Candle *opening* at retest time is not closed and must not influence PRE
        late.loc[e['retest_ts']]=[100,150,1,101]
        s2=classify_structure_pre(e,late)
        self.assertEqual(s1,s2)
    def test_missing_candle_fails_closed(self):
        bars=make_bars()
        bars=bars.drop(index=bars.index[10])
        s=classify_structure_pre(ev(),bars)
        self.assertEqual(s['structure_relation_pre'],'NO_EVALUABLE')
    def test_distant_level_fails_closed(self):
        e=ev();e['primary_distance_atr']=2.0
        s=classify_structure_pre(e,make_bars())
        self.assertEqual(s['structure_reason'],'NIVEL_DISTANTE')
    def test_arrival_inside_zone_fails_closed(self):
        s=classify_structure_pre(ev(role='IN_ZONE'),make_bars())
        self.assertEqual(s['structure_relation_pre'],'NO_EVALUABLE')
    def test_30m_context_supported(self):
        s=classify_structure_pre(ev(tf='30m',role='BELOW',side='SHORT'),
                                 make_bars('30m','LOW',94,95,approach='ALCISTA'))
        self.assertEqual(s['structure_pre'],'HL_HACIA_RESISTENCIA')
    def test_missing_outcome_is_not_NO_HIT(self):
        for x in (None,float('nan'),pd.NA,'nan','None','null','','NaT'):
            self.assertIsNone(normalized_outcome(x))
            self.assertEqual(outcome_label(x),'SIN_RESULTADO')
        self.assertEqual(outcome_label('no_hit'),'NO_HIT')
        df=pd.DataFrame([{'setup_pre':'X','structure_relation_pre':'PRESION_CONTRA_REACTION','outcome':v}
                         for v in [None,'TP_FIRST','SL_FIRST','NO_HIT','AMBIGUOUS']])
        a=groups_summary(df).iloc[0]
        b=summary_by(df,'structure_relation_pre').iloc[0]
        self.assertEqual(a['Sin resultado'],1)
        self.assertEqual(a['AMBIGUOUS'],1)
        self.assertEqual(b['SIN_RESULTADO'],1)
        self.assertEqual(b['NO_HIT'],1)
        self.assertEqual(b['N resueltos'],2)


class ReadOnlyBatchTests(unittest.TestCase):
    def test_enrichment_real_read_only_connection_and_duplicate_symbols(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)
            root=p/'reports/reaction_lab'
            root.mkdir(parents=True)
            db=root/'context.sqlite3'
            sqlite3.connect(db).close()
            prior=db.stat().st_size
            fake=types.ModuleType('reaction_structural_level_atlas_v2')
            lineage=types.SimpleNamespace(ROOT_REL=Path('reports/reaction_lab'),
                                          CTX_NAME='context.sqlite3',HISTORY_NAME='missing.sqlite3')
            n_calls=[]
            def merge(con,hist,sym):
                self.assertIsNone(hist)
                n_calls.append(sym)
                return make_bars(kind='HIGH',value1=111,value2=109),make_bars('30m'), 'MIXED'
            fake.v1=types.SimpleNamespace(lineage=lineage,v4=types.SimpleNamespace(_merge_tf=merge))
            events=pd.DataFrame([
                dict(event_key='e1',symbol='EVAUSDT',**ev()),
                dict(event_key='e2',symbol='EVAUSDT',**ev()),
            ])
            with patch.dict(sys.modules,{'reaction_structural_level_atlas_v2':fake}):
                out=enrich_pre_structure(p,events)
            self.assertEqual(n_calls,['EVAUSDT'])
            self.assertEqual(out.structure_relation_pre.tolist(),['PRESION_CONTRA_REACTION']*2)
            self.assertEqual(db.stat().st_size,prior)

if __name__ == "__main__":
    unittest.main()
