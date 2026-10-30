"""Causal tests for the initial rising line -> new HL sequence."""
import json
import tempfile
import unittest
from pathlib import Path
import numpy as np
import pandas as pd

try:
    from dashboard.views.hll_relaxed_hl_sequence_audit import (
        TF, parse_pivots, initial_line_and_break, sequence_at_pre,
        evaluate, load_bars, main)
except ModuleNotFoundError:
    from hll_relaxed_hl_sequence_audit import (
        TF, parse_pivots, initial_line_and_break, sequence_at_pre,
        evaluate, load_bars, main)


def pv(idx,low,known):
    return dict(open_ts=idx*TF,low=float(low),known_closed_ts=known*TF)


def bars(size=60):
    closes=np.full(size,95.,dtype=float)
    # earliest rising line anchored around 80-82; break at opens 18,19.
    closes[18]=82.9;closes[19]=83.0
    return pd.DataFrame(dict(timestamp=np.arange(size,dtype='int64')*TF,
       open=closes,high=closes+5,low=closes-.1,close=closes))


def original():
    return [pv(3,80,6),pv(7,81,10),pv(12,82,15)]


class SequenceResearch(unittest.TestCase):
    def test_first_line_break_clock_is_bar_close(self):
        c=bars(45);p=original()
        a=initial_line_and_break(c,p,1.,45*TF)
        self.assertEqual(a['line_state'],'INICIAL_ROTA')
        self.assertEqual(a['initial_break_closed_ts'],20*TF)
        self.assertEqual(a['anchor_third_ts'],12*TF)

    def test_first_line_is_selected_not_latest(self):
        c=bars(45)
        a=initial_line_and_break(c, original()+[pv(25,83,28)],1.,45*TF)
        self.assertEqual(a['anchor_first_ts'],3*TF)
        self.assertEqual(a['anchor_third_ts'],12*TF)

    def test_new_hl_strict_after_break(self):
        c=bars(45);p=original()+[pv(23,83,26)]
        line=initial_line_and_break(c,p,1.,45*TF)
        s=sequence_at_pre(c,p,line,1.,45*TF,95)
        self.assertEqual(s['sequence'],'ROTA_NUEVO_HL_ESTRICTO')
        self.assertEqual(s['new_hl_known_closed_ts'],26*TF)
        self.assertTrue(s['slope_reduced'])
        self.assertFalse(s['anchor_low_was_undercut'])

    def test_broad_hl_not_labeled_strict(self):
        c=bars(45);p=original()+[pv(23,81.5,26)]
        line=initial_line_and_break(c,p,1.,45*TF)
        s=sequence_at_pre(c,p,line,1.,45*TF,95)
        self.assertEqual(s['sequence'],'ROTA_CORRECCION_SOBRE_HL_ANTERIOR_REVISAR')
        self.assertFalse(s['slope_reduced'])

    def test_new_lower_pivot_is_not_higher_low(self):
        c=bars(45);p=original()+[pv(23,80.5,26)]
        line=initial_line_and_break(c,p,1.,45*TF)
        s=sequence_at_pre(c,p,line,1.,45*TF,95)
        self.assertEqual(s['sequence'],'ROTA_NUEVO_MINIMO_NO_HL')

    def test_undercut_is_not_mislabeled_as_clean_hl(self):
        c=bars(45);c.loc[21,'low']=80.9
        p=original()+[pv(23,83,26)]
        l=initial_line_and_break(c,p,1.,45*TF)
        x=sequence_at_pre(c,p,l,1.,45*TF,95)
        self.assertEqual(x['sequence'],'ROTA_NUEVO_HL_CON_BARRIDA_PREVIA_REVISAR')
        self.assertTrue(x['anchor_low_was_undercut'])

    def test_future_unconfirmed_pivot_never_counts(self):
        c=bars(45);p=original()+[pv(23,83,46)]
        raw=json.dumps(p)
        known=parse_pivots(raw,45*TF,0)
        self.assertEqual(len(known),3)
        a=initial_line_and_break(c,known,1.,45*TF)
        s=sequence_at_pre(c,known,a,1.,45*TF,95)
        self.assertEqual(s['sequence'],'ROTA_SIN_NUEVO_PIVOTE_CONFIRMADO')

    def test_pre_only_ignores_future_price(self):
        x=bars(60);p=original()+[pv(23,83,26)]
        row=dict(attempt_id='ID',symbol='SYM',level_id='L',
            pre_closed_ts=50*TF,breakout_closed_ts=51*TF,atr_pre=1.,zone_low=95.,
            pre_pattern_48='ASCENDING_CLEAN',pre48_pivot_lows_json=json.dumps(p))
        a=evaluate(row,x,48)
        x.loc[x.timestamp>=50*TF,'close']=100_000
        b=evaluate(row,x,48)
        self.assertEqual(a,b)

    def test_reject_missing_candle(self):
        c=bars(60).drop(index=17)
        row=dict(attempt_id='ID',symbol='SYM',level_id='L',
            pre_closed_ts=50*TF,breakout_closed_ts=51*TF,atr_pre=1.,zone_low=95.,
            pre_pattern_48='ASCENDING_CLEAN',pre48_pivot_lows_json=json.dumps(original()))
        x=evaluate(row,c,48)
        self.assertEqual(x['sequence'],'INDETERMINADO_GAP_30M')

    def test_no_line_cannot_be_called_clean_line(self):
        x=initial_line_and_break(bars(45),original()[:2],1.,45*TF)
        self.assertEqual(x['line_state'],'SIN_DIRECTRIZ_INICIAL')

    def test_first_new_pivot_not_cherrypicked(self):
        c=bars(45);p=original()+[pv(23,79,26),pv(30,84,33)]
        l=initial_line_and_break(c,p,1.,45*TF)
        x=sequence_at_pre(c,p,l,1.,45*TF,95)
        self.assertEqual(x['sequence'],'ROTA_NUEVO_MINIMO_NO_HL')
        self.assertEqual(x['new_hl_open_ts'],23*TF)

    def test_missing_source_plan_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            rc=main(['--plan','--cohort',str(Path(d)/'missing.csv')])
            self.assertEqual(rc,2)


if __name__=='__main__': unittest.main()
