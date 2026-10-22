"""Independent structural comparison, with frozen pre-event features only."""
import unittest
import pandas as pd
from dashboard.views.reaction_structure_independent_view import join_independent,stats_by_group
import reaction_compression_lineage as v3
import reaction_compression_v4_backfill as bf


class IndependentTests(unittest.TestCase):
    @staticmethod
    def data():
        o=pd.DataFrame([{'event_key':f'e{i}','symbol':'XUSDT','side':'LONG','date_utc':'2026-10-20',
            'outcome':x,'retest_ts':i*1000} for i,x in enumerate(['TP_FIRST','SL_FIRST','TP_FIRST','NO_HIT'],1)])
        c=pd.DataFrame([dict(event_key=f'e{i}',selection_state_pre='PRE_NEAR_WALL' if i<=2 else 'NO_DIRECTIONAL_BOX',
            selection_state_known='FIRST_RETEST_HELD' if i<=2 else 'NOT_EVALUABLE',
            selection_timeframe='15m' if i<=2 else None,selection_window=40 if i<=2 else None,
            has_qualified_any=1 if i<=2 else 0,coverage_all_5=1) for i in range(1,5)])
        l=pd.DataFrame([dict(event_key=f'e{i}',selection_state_pre=['ACCEPTED_DEFENDED','NO_LEVEL','RECLAIM_ACCEPTED','LOST_LEVEL_PRE'][i-1],
            selection_state_known='RETEST_HELD',selection_timeframe='30m',selection_pre_near_level=True,
            has_level=int(i!=2),coverage_all=1) for i in range(1,5)])
        return o,c,l

    def test_four_independent_groups_without_confluence_gate(self):
        o,c,l=self.data()
        d=join_independent(o,c,l).set_index('event_key')
        self.assertEqual(d.loc['e1','relacion_independiente'],'AMBAS')
        self.assertEqual(d.loc['e2','relacion_independiente'],'SOLO_RUPTURA_COMPRESION')
        self.assertEqual(d.loc['e3','relacion_independiente'],'SOLO_NIVEL_ACEPTADO')
        self.assertEqual(d.loc['e4','relacion_independiente'],'NINGUNA_DE_LAS_DOS')
        self.assertEqual(len(d),4)
        stats=stats_by_group(d.reset_index(),'relacion_independiente',1)
        self.assertEqual(int(stats.REACTIONs.sum()),4)

    def test_unknown_coverage_never_counted_as_none(self):
        o,c,l=self.data()
        c.loc[3,'coverage_all_5']=0
        d=join_independent(o,c,l).set_index('event_key')
        self.assertEqual(d.loc['e4','relacion_independiente'],'COBERTURA_PARCIAL')
        self.assertEqual(d.loc['e4','categoria_compresion'],'COBERTURA_INSUFICIENTE')

    def test_duplicate_event_fails_closed(self):
        o,c,l=self.data()
        with self.assertRaises(ValueError):join_independent(pd.concat([o,o.iloc[:1]]),c,l)

    def test_fixed_scales_and_hours(self):
        self.assertEqual(v3.WINDOWS['15m'],(20,30,40,60,80))
        self.assertEqual(v3.WINDOWS['30m'],(40,60,80))
        self.assertEqual(sum(map(len,v3.WINDOWS.values())),8)
        self.assertEqual([round(x*v3.TF['15m']/(60*v3.M),1) for x in v3.SCALE_GROUPS['INTERMEDIA_15M'][1]], [10,15,20])

    def test_backfill_margin_covers_oldest_snapshot_plus_longest_reference(self):
        from unittest.mock import patch
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            event={'event_key':'e1','symbol':'XUSDT','side':'LONG','date_utc':'2026-10-21',
                'retest_ts':1792608300000,'observation_type':'LIVE_OBSERVED'}
            with patch.object(bf.v3,'ro') as ro,patch.object(bf.v4,'select_events',return_value=[event]):
                ro.return_value.__enter__.return_value=None
                _,_,prior=bf.requests_for_selected(Path(d),max_events=1,search_hours=48)
            self.assertGreaterEqual(prior,180)
            self.assertGreaterEqual(prior,120+48+12)

if __name__=='__main__':unittest.main()
