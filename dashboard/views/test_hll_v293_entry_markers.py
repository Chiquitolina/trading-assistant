"""Entry markers: no retroactive confirmation, all branches, no invented OPENs."""
import tempfile
import unittest
from pathlib import Path
import pandas as pd
import plotly.graph_objects as go

from dashboard.views.hll_v293_entry_markers import (
    get_entries, overlay_entry_markers, _read_observed_1m_open,
)

TF = 1_800_000
MIN = 60_000


def fixture():
    bars = pd.DataFrame([
        dict(timestamp=t*TF, open=100+t, high=102+t, low=99+t, close=101+t)
        for t in range(20, 50)
    ])
    row = pd.Series(dict(attempt_id='A1', symbol='TESTUSDT',
                         breakout_closed_ts=24*TF, flip_confirmed_closed_ts=26*TF))
    return row,bars


class EntryMarkersTests(unittest.TestCase):
    def test_all_three_confirmed_paths_keep_separate_entry(self):
        row,bars=fixture()
        survival=pd.DataFrame([dict(horizon_min=120,path_state='COMPLETE_1M',
                                    entry_ts=26*TF+MIN,entry_open=112)])
        events=[
            dict(branch='BREAKOUT_CONTINUATION',status='CONFIRMED',confirmed_ts=28*TF,signal_open_ts=27*TF,signal_close=128),
            dict(branch='RECLAIM_CONTINUATION',status='CONFIRMED',confirmed_ts=31*TF,signal_open_ts=30*TF,signal_close=131),
        ]
        entries=get_entries(row,bars,survival,events)
        self.assertEqual([e['key'] for e in entries],
                         ['FLIP_1M','V29_CONTINUATION_30M','V29_RECLAIM_30M'])
        self.assertEqual([e['entry_ts'] for e in entries],
                         [26*TF+MIN,28*TF,31*TF])
        self.assertEqual([e['price'] for e in entries],[112,128,131])
        fig=overlay_entry_markers(go.Figure(),entries,selected_reference='FLIP_1M')
        self.assertEqual(len(fig.data),3)
        self.assertEqual(fig.data[0].x[0],pd.Timestamp(26*TF+MIN,unit='ms',tz='UTC'))
        self.assertEqual(fig.data[0].y[0],112)
        self.assertEqual(fig.data[0].text[0],'ENTRADA FLIP')
        self.assertEqual(fig.data[1].text[0],'ENTRADA CONT.')
        self.assertEqual(fig.data[2].text[0],'ENTRADA RECLAIM')

    def test_only_breakout_is_not_confirmed_entry(self):
        row,bars=fixture()
        row['flip_confirmed_closed_ts']=float('nan')
        self.assertEqual(get_entries(row,bars,None,[]),[])
        entries=get_entries(row,bars,None,[],include_breakout_reference=True)
        self.assertEqual([e['key'] for e in entries],['BREAKOUT_30M'])
        self.assertIn('REF.',entries[0]['short'])

    def test_next_open_missing_never_uses_signal_close(self):
        row,bars=fixture()
        bars=bars[bars.timestamp.ne(28*TF)]
        events=[dict(branch='BREAKOUT_CONTINUATION',status='CONFIRMED',confirmed_ts=28*TF,
                     signal_open_ts=27*TF,signal_close=128)]
        self.assertEqual(get_entries(row,bars,None,events),[])

    def test_flip_can_mark_observed_open_despite_later_gap(self):
        row,bars=fixture()
        survival=pd.DataFrame([dict(horizon_min=120,path_state='GAP_1M',
                                    entry_ts=26*TF+MIN,entry_open=float('nan'))])
        with tempfile.TemporaryDirectory() as d:
            pd.DataFrame([dict(timestamp=26*TF+MIN,open=105.5,high=106,
                               low=105,close=105.7),
                          dict(timestamp=26*TF+2*MIN,open=105.7,high=106.1,
                               low=105.6,close=105.8),
                          dict(timestamp=26*TF+4*MIN,open=105.8,high=106.2,
                               low=105.7,close=105.9)]).to_csv(Path(d)/'TESTUSDT_1m.csv',index=False)
            entries=get_entries(row,bars,survival,[],minute_dir=Path(d))
        self.assertEqual(len(entries),1)
        self.assertEqual(entries[0]['price'],105.5)
        self.assertEqual(entries[0]['entry_ts'],26*TF+MIN)
        self.assertEqual(survival.iloc[0].path_state,'GAP_1M')

    def test_missing_minute_does_not_invent_flip(self):
        row,bars=fixture()
        with tempfile.TemporaryDirectory() as d:
            pd.DataFrame([dict(timestamp=26*TF+2*MIN,open=105.5,high=106,
                               low=105,close=105.7)]).to_csv(Path(d)/'TESTUSDT_1m.csv',index=False)
            entries=get_entries(row,bars,None,[],minute_dir=Path(d))
        self.assertEqual(entries,[])

    def test_invalid_minute_wick_not_used(self):
        row,bars=fixture()
        with tempfile.TemporaryDirectory() as d:
            pd.DataFrame([dict(timestamp=26*TF+MIN,open=105.5,high=104,
                               low=105,close=105.7)]).to_csv(Path(d)/'TESTUSDT_1m.csv',index=False)
            with self.assertRaises(ValueError):get_entries(row,bars,None,[],minute_dir=Path(d))

    def test_wrong_flip_clock_rejected(self):
        row,bars=fixture()
        survival=pd.DataFrame([dict(horizon_min=120,path_state='COMPLETE_1M',
                                    entry_ts=26*TF,entry_open=105)])
        with self.assertRaises(ValueError):get_entries(row,bars,survival,[])

    def test_view_shows_entries_independently_of_percent_guide_or_signal_layer(self):
        import inspect
        from dashboard.views.hll_v282_pre_followup_view import render_hll_v282_pre_followup
        source=inspect.getsource(render_hll_v282_pre_followup)
        self.assertIn('continuation_all=',source)
        self.assertIn('get_entries(',source)
        self.assertIn('overlay_entry_markers(',source)
        self.assertIn('if show_entries:',source)
        self.assertIn("if show_v29:",source)


if __name__ == '__main__':
    unittest.main()
