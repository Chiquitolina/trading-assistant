import unittest
from pathlib import Path
import pandas as pd
from reaction_horizontal_transition_alignment import (
    ALIGN, OPPOSE, INDET, NO_CHANGE, NO_LEVEL, VERSION,
    classify_transition, add_transition_alignment,
)

BASE = dict(
    event_key='XLMUSDT|SHORT|1791836940000',
    side='SHORT', lifecycle_state='NIVEL_RECUPERADO_PRE',
    initial_side='BELOW',current_confirmed_side='BELOW',
    history_quality='COMPLETE_POST_FORMATION',
    lifecycle_reason='CLASSIFIED_FROM_CLOSED_PRE_HTF_ONLY',
    last_break_close_ts=1000,last_reclaim_close_ts=1200,
    last_failed_retest_close_ts=None,last_pre_close_ts=1300,retest_ts=1350,
    outcome_norm='TP_FIRST'
)

class TransitionDirectionTests(unittest.TestCase):
    def row(self, **changes):
        return {**BASE,**changes}

    def test_xlm_short_below_restored_resistance_aligned(self):
        self.assertEqual(classify_transition(self.row())[0],ALIGN)

    def test_same_restored_resistance_long_opposes(self):
        self.assertEqual(classify_transition(self.row(side='LONG'))[0],OPPOSE)

    def test_zerebro_long_above_recovered_support_aligned(self):
        r=self.row(event_key='ZEREBRO',side='LONG',initial_side='ABOVE',current_confirmed_side='ABOVE')
        self.assertEqual(classify_transition(r)[0],ALIGN)
        self.assertEqual(classify_transition({**r,'side':'SHORT'})[0],OPPOSE)

    def test_bearish_break_with_failed_retest_short_aligned(self):
        r=self.row(lifecycle_state='RETEST_FALLIDO_PRE', initial_side='ABOVE',
                   current_confirmed_side='BELOW',last_failed_retest_close_ts=1100,
                   last_reclaim_close_ts=None)
        self.assertEqual(classify_transition(r)[0],ALIGN)
        self.assertEqual(classify_transition({**r,'side':'LONG'})[0],OPPOSE)

    def test_bullish_break_with_failed_retest_long_aligned(self):
        r=self.row(lifecycle_state='RETEST_FALLIDO_PRE', initial_side='BELOW',
                   current_confirmed_side='ABOVE',last_failed_retest_close_ts=1100,
                   last_reclaim_close_ts=None,side='LONG')
        self.assertEqual(classify_transition(r)[0],ALIGN)
        self.assertEqual(classify_transition({**r,'side':'SHORT'})[0],OPPOSE)

    def test_break_both_directions(self):
        for initial,current,good in [('ABOVE','BELOW','SHORT'),('BELOW','ABOVE','LONG')]:
            r=self.row(lifecycle_state='NIVEL_PERDIDO_PRE',initial_side=initial,
                       current_confirmed_side=current,side=good,
                       last_reclaim_close_ts=None)
            self.assertEqual(classify_transition(r)[0],ALIGN)

    def test_no_transition_and_no_level_not_negative_controls(self):
        self.assertEqual(classify_transition(self.row(lifecycle_state='NIVEL_VIGENTE_PRE'))[0],NO_CHANGE)
        self.assertEqual(classify_transition(self.row(lifecycle_state='SIN_NIVEL_IMPORTANTE_CERCANO_PRE'))[0],NO_LEVEL)
        self.assertEqual(classify_transition(self.row(lifecycle_state='INDETERMINADO_PRE'))[0],INDET)

    def test_consistency_invalid_and_timestamp_guards(self):
        base=self.row()
        variants=[
            dict(current_confirmed_side='ABOVE'),
            dict(last_reclaim_close_ts=999),
            dict(last_break_close_ts=None),
            dict(last_pre_close_ts=1400),
            dict(history_quality='PARTIAL'),
            dict(lifecycle_reason='GAPPED_HTF_HISTORY'),
            dict(side='UNKNOWN'),
            dict(lifecycle_state='RETEST_FALLIDO_PRE',initial_side='ABOVE',
                 current_confirmed_side='BELOW',last_failed_retest_close_ts=None),
        ]
        for variant in variants:
            with self.subTest(variant=variant):
                self.assertEqual(classify_transition({**base,**variant})[0],INDET)

    def test_outcome_and_known_reaction_invariance(self):
        for outcome in ['TP_FIRST','SL_FIRST','NO_HIT','SIN_RESULTADO',None]:
            row=self.row(outcome_norm=outcome,known_high=999,known_low=0.00001)
            self.assertEqual(classify_transition(row)[0],ALIGN)

    def test_frame_preserves_rows_and_outcomes(self):
        df=pd.DataFrame([self.row(event_key='xlm'),
                         self.row(event_key='zerebro',side='LONG',initial_side='ABOVE',
                                  current_confirmed_side='ABOVE',outcome_norm='SL_FIRST')])
        result=add_transition_alignment(df)
        self.assertEqual(result.outcome_norm.tolist(),['TP_FIRST','SL_FIRST'])
        self.assertEqual(result.event_key.tolist(),['xlm','zerebro'])
        self.assertEqual(result.transition_alignment.tolist(),[ALIGN,ALIGN])
        self.assertEqual(len(result),len(df))
        with self.assertRaises(ValueError):
            add_transition_alignment(pd.concat([df,df.iloc[[0]]]))

    def test_version(self):
        self.assertTrue(VERSION.endswith('pre-v1.0'))

class IntegrationTests(unittest.TestCase):
    def test_view_preserves_lifecycle_and_shows_new_comparison(self):
        source=(Path(__file__).parent/'dashboard/views/reaction_horizontal_level_rich_card_view.py').read_text()
        self.assertIn('df=add_transition_alignment(merged)',source)
        self.assertIn('Experimento PRE · ¿REACTION alineada con la última transición confirmada?',source)
        self.assertIn('Alineación con transición PRE (solo inspector)',source)
        self.assertIn("'transition_alignment','transition_confirmed_side'",source)
        self.assertIn('read_checked_cache',source)
        self.assertIn('meaning_of_level_pre',source)
        self.assertIn('get_post_bars',source)
        self.assertIn('overlay_future',source)
        self.assertIn('group_summary(',source)

    def test_audit_is_read_only(self):
        source=(Path(__file__).parent/'audit_horizontal_transition_alignment.py').read_text()
        self.assertIn('read_checked_cache(',source)
        self.assertNotIn('to_csv(',source)
        self.assertNotIn('os.replace(',source)

if __name__=='__main__':unittest.main()
