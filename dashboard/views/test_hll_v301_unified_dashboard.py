"""Regression: 290 inspector must preserve the proven 15-symbol UI components."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd

from dashboard.views.hll_v301_full_inspector import (
    visual_entries_for_episode,evaluate_local_25,_visible_summary_rows
)

TF=1_800_000
MIN=60_000

class UnifiedV301(unittest.TestCase):
    def setUp(self):
        self.t=1_800_000_000_000
        self.bars=pd.DataFrame([
            {'timestamp':self.t-TF,'open':.98,'high':1.02,'low':.97,'close':1.01},
            {'timestamp':self.t,'open':1.01,'high':1.04,'low':1.,'close':1.03},
            {'timestamp':self.t+TF,'open':1.03,'high':1.08,'low':1.02,'close':1.07}
        ])
        self.row=pd.Series({'symbol':'AAAUSDT','breakout_closed_ts':self.t,
                             'flip_confirmed_closed_ts':float('nan')})
    def test_no_signal_breakout_is_only_reference(self):
        with tempfile.TemporaryDirectory() as td:
            active,breakouts=visual_entries_for_episode(self.row,self.bars,[],Path(td))
        self.assertEqual(active,[])
        self.assertEqual(len(breakouts),1)
        self.assertEqual(breakouts[0]['key'],'BREAKOUT_30M')
    def test_confirmed_continuation_marks_actual_30m_open_and_guides(self):
        ev={'branch':'BREAKOUT_CONTINUATION','status':'CONFIRMED',
            'confirmed_ts':self.t+TF,'signal_open_ts':self.t,'signal_close':1.03,
            'next_open_ts':self.t+TF,'next_open':1.03}
        with tempfile.TemporaryDirectory() as td:
            active,breakouts=visual_entries_for_episode(self.row,self.bars,[ev],Path(td))
        self.assertEqual([a['key'] for a in active],['V29_CONTINUATION_30M'])
        self.assertEqual(active[0]['entry_ts'],self.t+TF)
        self.assertEqual(active[0]['price'],1.03)
        from dashboard.views.hll_v292_percent_guides import price_guides
        self.assertEqual(len(price_guides(active[0]['price'])),5)
        self.assertEqual(len({level for pct,tp,sl in price_guides(active[0]['price']) for level in (tp,sl)}),10)
    def test_mismatch_next_open_raises(self):
        ev={'branch':'RECLAIM_CONTINUATION','status':'CONFIRMED',
            'confirmed_ts':self.t+TF,'signal_open_ts':self.t,'signal_close':1.03,
            'next_open_ts':self.t+TF,'next_open':.89}
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError):visual_entries_for_episode(self.row,self.bars,[ev],Path(td))
    def test_25_matrix_with_1m_no_lookahead(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'AAAUSDT_1m.csv'
            data=[]
            for i in range(120):
                lo=0.97 if i==5 else 1.0
                hi=1.03 if i==3 else 1.0
                data.append({'timestamp':self.t+i*MIN,'open':1.,'high':hi,
                             'low':lo,'close':1.})
            pd.DataFrame(data).to_csv(p,index=False)
            x=evaluate_local_25(p,{'entry_ts':self.t,'price':1.},120,.1,0)
            self.assertEqual(len(x),25)
            q=x[(x.tp_pct==2)&(x.sl_pct==3)].iloc[0]
            self.assertEqual(q['result'],'TP_FIRST')
            self.assertAlmostEqual(q['net_pct'],1.9)
            self.assertEqual(q['first_hit_min'],3)
    def test_open_mismatch_blocks_matrix(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'candles.csv'
            pd.DataFrame([{'timestamp':self.t+i*MIN,'open':1,'high':1,'low':1,'close':1}
                for i in range(120)]).to_csv(p,index=False)
            with self.assertRaises(ValueError):
                evaluate_local_25(p,{'entry_ts':self.t,'price':1.4},120)
    def test_aggregated_only_filters_supported_dimensions(self):
        src=pd.DataFrame({'pre_window':[48,24],'pre_pattern':['ASCENDING_CLEAN']*2,
            'mechanism_pre_48':['A']*2})
        self.assertEqual(len(_visible_summary_rows(src,48,'ASCENDING_CLEAN','A',True)),1)
        with self.assertRaises(ValueError):_visible_summary_rows(src,24,'TODAS','A',True)

if __name__=='__main__':unittest.main()
