"""V2.9.3 plotting guides: visual levels only, no claims of order execution."""
import math
import unittest
import pandas as pd
import plotly.graph_objects as go

from dashboard.views.hll_v292_percent_guides import (
    available_reference_entries, price_guides, overlay_visual_levels,
)

TF=1_800_000
MIN=60_000


def fixtures():
    bars=pd.DataFrame([dict(timestamp=t*TF,open=100+t,close=101+t,high=102+t,low=99+t)
                      for t in range(20,40)])
    row=pd.Series(dict(breakout_closed_ts=24*TF,flip_confirmed_closed_ts=26*TF))
    survival=pd.DataFrame([dict(horizon_min=120,path_state='COMPLETE_1M',
                                entry_ts=26*TF+MIN,entry_open=110)])
    return row,bars,survival


class PercentLevelsTests(unittest.TestCase):
    def test_long_symmetric_ladder(self):
        ladder=price_guides(100,5)
        self.assertEqual([x[0] for x in ladder],[1,2,3,4,5])
        self.assertEqual([round(x[1],8) for x in ladder],[101,102,103,104,105])
        self.assertEqual([round(x[2],8) for x in ladder],[99,98,97,96,95])

    def test_invalid_levels_prices_do_not_draw(self):
        for p in (-3,0,float('nan'),float('inf')):
            with self.assertRaises(ValueError):price_guides(p,5)
        for n in (0,6,True,2.5):
            with self.assertRaises(ValueError):price_guides(100,n)

    def test_breakout_next_open_not_signal_close(self):
        row,bars,survival=fixtures()
        refs=available_reference_entries(row,bars,survival)
        self.assertEqual(refs['BREAKOUT_30M']['price'],124)
        self.assertEqual(refs['BREAKOUT_30M']['entry_ts'],24*TF)
        self.assertEqual(refs['FLIP_1M']['price'],110)
        self.assertEqual(refs['FLIP_1M']['entry_ts'],26*TF+MIN)

    def test_censored_path_never_uses_fake_flip_price(self):
        row,bars,survival=fixtures()
        survival.loc[0,'path_state']='GAP_1M'
        survival.loc[0,'entry_open']=float('nan')
        refs=available_reference_entries(row,bars,survival)
        self.assertNotIn('FLIP_1M',refs)
        self.assertIn('BREAKOUT_30M',refs)

    def test_missing_next_open_never_uses_close(self):
        row,bars,survival=fixtures()
        bars=bars[bars.timestamp.ne(24*TF)]
        refs=available_reference_entries(row,bars,None)
        self.assertNotIn('BREAKOUT_30M',refs)

    def test_flip_known_time_guard(self):
        row,bars,survival=fixtures()
        survival.loc[0,'entry_ts']=27*TF
        with self.assertRaises(ValueError):available_reference_entries(row,bars,survival)

    def test_next_open_for_v29_continuation(self):
        row,bars,survival=fixtures()
        events=[dict(status='CONFIRMED',branch='BREAKOUT_CONTINUATION',confirmed_ts=28*TF)]
        refs=available_reference_entries(row,bars,survival,events)
        self.assertEqual(refs['V29_CONTINUATION_30M']['price'],128)
        self.assertEqual(refs['V29_CONTINUATION_30M']['entry_ts'],28*TF)

    def test_plot_adds_1_reference_and_10_levels(self):
        fig=overlay_visual_levels(go.Figure(),dict(price=100),max_pct=5)
        self.assertEqual(len(fig.layout.shapes),11)
        self.assertEqual(len(fig.layout.annotations),11)
        guide_y=sorted(round(s.y0,6) for s in fig.layout.shapes)
        self.assertEqual(guide_y,[95,96,97,98,99,100,101,102,103,104,105])
        self.assertTrue(all(s.type=='line' for s in fig.layout.shapes))

    def test_options_and_plot_post_only_in_view(self):
        import inspect
        from dashboard.views.hll_v282_pre_followup_view import render_hll_v282_pre_followup
        source=inspect.getsource(render_hll_v282_pre_followup)
        self.assertIn('if show_post and not blind:',source)
        self.assertIn('hll293_show_percent_levels',source)
        self.assertIn('overlay_visual_levels(',source)
        self.assertNotIn('st.session_state[\'hll293_tp_filled\']',source)


if __name__=='__main__': unittest.main()
