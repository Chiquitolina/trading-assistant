"""Pruebas regresión visual V1.1.1: misma matriz sin ocultar scale_agreement."""
import unittest
from dashboard.views.hll_unified_lifecycle_family_view_v11 import (
    matrix_rows_for_attempt, episode_timeline,
)


def selected(**kwargs):
    return {'attempt_id':'a','symbol':'WAXPUSDT','level_id':'L1','episode_id':'E1',
            'pre_closed_ts':'1787292000000','episode_ordinal':'1','split':'HOLDOUT_SYMBOL',
            'pre_pattern_96':'MULTI_CROSS_RANGE','family_1p0':'REBOTE_TRAS_CORRECCION',
            'scale_agreement':'IGUAL_3_ATR',**kwargs}


def matrix(scenario,agree,split='HOLDOUT_SYMBOL',route='FLIP_1M',geometry='MULTI_CROSS_RANGE',family='REBOTE_TRAS_CORRECCION'):
    return {'split':split,'route':route,'scenario':scenario,'scale':'1p0','family':family,
            'pre_pattern_96':geometry,'scale_agreement':agree,'entries':'3', 'episodes':'3','symbols':'2',
            'tp_first':'2','sl_first':'1','timeout':'0','ambiguous':'0','censored':'0','tp_pct_resolved':'66.7'}


class FamilyPanelV111Tests(unittest.TestCase):
    def test_default_keeps_only_selected_agreement_even_same_scenario(self):
        d={'by_geometry_family_route_outcomes_v11.csv':[
            matrix('TP3_SL1P5_48H','IGUAL_3_ATR'),
            matrix('TP3_SL1P5_48H','DOS_COINCIDEN'),
        ]}
        x=matrix_rows_for_attempt(d,selected(),'FLIP_1M')
        self.assertEqual(len(x),1)
        self.assertEqual(x[0]['scale_agreement'],'IGUAL_3_ATR')
        self.assertTrue(x[0]['same_agreement_as_selected'])

    def test_compare_other_agreements_are_exposed_not_silently_merged(self):
        d={'by_geometry_family_route_outcomes_v11.csv':[
            matrix('TP3_SL1P5_48H','DOS_COINCIDEN'),
            matrix('TP3_SL1P5_48H','IGUAL_3_ATR'),
        ]}
        x=matrix_rows_for_attempt(d,selected(),'FLIP_1M',include_other_agreements=True)
        self.assertEqual(len(x),2)
        self.assertEqual([v['same_agreement_as_selected'] for v in x],[True,False])
        self.assertNotEqual(x[0]['scale_agreement'],x[1]['scale_agreement'])

    def test_matrix_never_mixes_holdout_and_discovery_or_route_or_geometry(self):
        d={'by_geometry_family_route_outcomes_v11.csv':[
            matrix('TP3_SL1P5_48H','IGUAL_3_ATR'),
            matrix('TP3_SL1P5_48H','IGUAL_3_ATR',split='DISCOVERY_SYMBOL'),
            matrix('TP3_SL1P5_48H','IGUAL_3_ATR',route='V29_RECLAIM_30M'),
            matrix('TP3_SL1P5_48H','IGUAL_3_ATR',geometry='ASCENDING_CLEAN'),
            matrix('TP3_SL1P5_48H','IGUAL_3_ATR',family='ASCENSO_ESCALONADO'),
        ]}
        self.assertEqual(len(matrix_rows_for_attempt(d,selected(),'FLIP_1M',include_other_agreements=True)),1)

    def test_timeline_per_attempt_keeps_own_pre_and_ordinal(self):
        a=selected()
        b={**a,'attempt_id':'b','episode_ordinal':'2','pre_closed_ts':'1787293800000',
           'family_1p0':'ASCENSO_ESCALONADO','pre_pattern_96':'ASCENDING_CLEAN'}
        d={'unified_attempts_family_v11.csv':[b,a]}
        timeline=episode_timeline(d,a)
        self.assertEqual(len(timeline),2)
        self.assertEqual([x['intento'] for x in timeline],[1,2])
        self.assertEqual([x['familia_1_ATR'] for x in timeline],['Rebote tras corrección','Ascenso escalonado'])
        self.assertEqual([x['cambio_vs_anterior'] for x in timeline],['—','Sí'])
        self.assertEqual([x['seleccionado'] for x in timeline],['◀',''])

    def test_wrong_symbol_and_nonconsecutive_ordinals_rejected(self):
        a=selected(); b={**a,'attempt_id':'b','episode_ordinal':'2','symbol':'OTHERUSDT'}
        with self.assertRaisesRegex(ValueError,'cruza símbolo'):
            episode_timeline({'unified_attempts_family_v11.csv':[a,b]},a)
        b['symbol']='WAXPUSDT';b['episode_ordinal']='3'
        with self.assertRaisesRegex(ValueError,'Orden de intentos'):
            episode_timeline({'unified_attempts_family_v11.csv':[a,b]},a)

    def test_dashboard_source_preserves_post_gate_and_original_shape(self):
        from pathlib import Path
        s=Path(__file__).with_name('hll_unified_lifecycle_family_view_v11.py').read_text()
        self.assertIn('if not reveal_post:',s)
        self.assertIn('entry_outcomes_family_v11.csv',s)
        self.assertIn("if row['split']=='HOLDOUT_SYMBOL':",s)
        self.assertIn("st.checkbox('Comparar también otros acuerdos",s)

if __name__=='__main__':unittest.main()
