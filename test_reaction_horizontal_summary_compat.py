"""Regression: Rich Card should work with the older, already-imported logic module."""
import unittest
import pandas as pd
from dashboard.views import reaction_horizontal_level_rich_card_view as view


class HorizontalSummaryCompatibilityTests(unittest.TestCase):
    def test_structure_counts_and_missing_outcome_do_not_become_losses(self):
        events=pd.DataFrame([
            {'structure_relation_pre':'PRESION_CONTRA_REACTION','outcome':'TP_FIRST'},
            {'structure_relation_pre':'PRESION_CONTRA_REACTION','outcome':'SL_FIRST'},
            {'structure_relation_pre':'PRESION_CONTRA_REACTION','outcome':float('nan')},
            {'structure_relation_pre':'PRESION_CONTRA_REACTION','outcome':'NO_HIT'},
            {'structure_relation_pre':'SIN_PRESION_CLARA','outcome':'AMBIGUOUS'},
            {'structure_relation_pre':None,'outcome':'None'},
        ])
        # This deliberately never uses view.logic.summary_by.
        result=view._summary_pre_structure(events)
        self.assertEqual(int(result['REACTIONs'].sum()),6)
        self.assertEqual(int(result['TP'].sum()),1)
        self.assertEqual(int(result['SL'].sum()),1)
        self.assertEqual(int(result['NO_HIT'].sum()),1)
        self.assertEqual(int(result['AMBIGUOUS'].sum()),1)
        self.assertEqual(int(result['SIN_RESULTADO'].sum()),2)
        first=result[result['Lectura PRE'].eq('Presión contraria a la REACTION')].iloc[0]
        self.assertEqual(first['TP resueltos (%)'],50.0)
        self.assertEqual(first['N resueltos'],2)

    def test_works_when_loaded_logic_does_not_expose_summary_by(self):
        module=view.logic
        had_function=hasattr(module,'summary_by')
        original=getattr(module,'summary_by',None)
        try:
            if had_function:
                delattr(module,'summary_by')
            events=pd.DataFrame([{'structure_relation_pre':'PRESION_A_FAVOR_REACTION','outcome':'TP_FIRST'}])
            result=view._summary_pre_structure(events)
            self.assertEqual(len(result),1)
            self.assertEqual(result['TP'].iloc[0],1)
        finally:
            if had_function:
                setattr(module,'summary_by',original)

    def test_missing_column_and_empty_input_are_safe(self):
        self.assertTrue(view._summary_pre_structure(pd.DataFrame()).empty)
        self.assertTrue(view._summary_pre_structure(pd.DataFrame(columns=['structure_relation_pre','outcome'])).empty)


if __name__=='__main__':
    unittest.main()
