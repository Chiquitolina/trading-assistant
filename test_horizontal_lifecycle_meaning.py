import unittest
import pandas as pd
from reaction_horizontal_lifecycle_meaning import meaning_label,origin_consistency

class SideAwareMeaning(unittest.TestCase):
    def test_reclaimed_below_is_not_called_support_reclaimed(self):
        text=meaning_label('NIVEL_RECUPERADO_PRE','BELOW')
        self.assertIn('INFERIOR',text)
        self.assertNotIn('soporte',text.lower())
    def test_reclaimed_above(self):
        self.assertIn('SUPERIOR',meaning_label('NIVEL_RECUPERADO_PRE','ABOVE'))
    def test_failed_is_directional(self):
        self.assertIn('BAJA',meaning_label('RETEST_FALLIDO_PRE','ABOVE'))
        self.assertIn('ALZA',meaning_label('RETEST_FALLIDO_PRE','BELOW'))
    def test_source_conflict_is_diagnostic_only(self):
        self.assertEqual(origin_consistency('BELOW','LOW').split(':')[0],'DIFIERE')
        self.assertEqual(origin_consistency('ABOVE','LOW'),'COINCIDE')
    def test_outcome_invariance(self):
        for out in ('TP_FIRST','SL_FIRST','NO_HIT',None):
            self.assertEqual(meaning_label('NIVEL_RECUPERADO_PRE','BELOW'),
                             'Regresó al lado INFERIOR inicialmente aceptado')
    def test_view_does_not_override_raw_class_or_tp(self):
        from pathlib import Path
        source=(Path(__file__).parent/'dashboard/views/reaction_horizontal_level_rich_card_view.py').read_text()
        self.assertIn('meaning_of_level_pre',source)
        self.assertIn('df[\'lifecycle_detail_label\']',source)
        self.assertNotIn('outcome_norm==',source)

if __name__=='__main__':unittest.main()
