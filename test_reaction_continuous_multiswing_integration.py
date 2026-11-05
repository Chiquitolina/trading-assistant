"""End-to-end offline Streamlit mock: original persisted REACTION → frozen chart."""
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from dashboard.views.reaction_continuous_multiswing_view import render_continuous_multiswing_view


class MultiSwingRenderTests(unittest.TestCase):
    def test_render_frozen_event_without_network_or_mutation(self):
        import pandas as pd
        import plotly.graph_objects as go
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = root / 'reports/reaction_lab/scanner_first_touches_v1.sqlite3'
            db.parent.mkdir(parents=True)
            known = 180 * 60_000
            retest = known - 60_000
            pivot = 30 * 60_000
            key = f'XUSDT|SHORT|{retest}'
            payload = dict(swing_price=100., retest_price=100.05,
                           pivot_timestamp=pivot)
            with sqlite3.connect(db) as con:
                con.execute('CREATE TABLE first_touches ('
                            'touch_key TEXT, legacy_event_key TEXT, symbol TEXT, side TEXT, '
                            'status TEXT, timeframe TEXT, detector TEXT, pivot_ts INTEGER, '
                            'actionable_ts INTEGER, retest_ts INTEGER, known_ts INTEGER, '
                            'observation_type TEXT, first_seen_ms INTEGER, causal_json TEXT)')
                con.execute('INSERT INTO first_touches VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                            ('touch-A',key,'XUSDT','SHORT','REACTION','15m','3x3',
                             pivot,60*60_000,retest,known,'LIVE_OBSERVED',known,
                             json.dumps(payload)))
            context = root / 'reports/reaction_lab/causal_context_v1.sqlite3'
            snaps = {}
            for tf, span in [('1m',1),('5m',5),('15m',15)]:
                snaps[tf] = [dict(timestamp=i*span*60_000,open=100.,high=101.,
                                  low=99.,close=100.) for i in range(known//(span*60_000))]
            with sqlite3.connect(context) as con:
                con.execute('CREATE TABLE contexts(event_key TEXT,known_ts INTEGER,'
                            'status TEXT,candles_json TEXT)')
                con.execute('INSERT INTO contexts VALUES(?,?,?,?)',
                            (key,known,'READY',json.dumps(snaps)))

            class FakeUI(types.ModuleType):
                def __init__(self):
                    super().__init__('streamlit')
                    self.figures=[]; self.metrics=[]; self.warnings=[]
                def __enter__(self): return self
                def __exit__(self,*args): return False
                def columns(self,n): return [self]* (len(n) if isinstance(n,(list,tuple)) else n)
                def markdown(self,*args,**kw): pass
                def caption(self,*args,**kw): pass
                def warning(self,msg): self.warnings.append(msg)
                def error(self,msg): self.warnings.append(msg)
                def info(self,*args,**kw): pass
                def selectbox(self,label,options,**kw):
                    if label == 'Primer retest persistido': return 'touch-A'
                    if label == 'Símbolo REACTION': return 'XUSDT'
                    if label == 'Coincidencia temporal': return '15m'
                    return options[0]
                def number_input(self,*args,**kw): return kw['value']
                def slider(self,*args,**kw): return kw['value']
                def multiselect(self,*args,**kw): return kw['default']
                def metric(self,*args,**kw): self.metrics.append(args)
                def dataframe(self,*args,**kw): pass
                def expander(self,*args,**kw): return self
                def json(self,*args,**kw): pass
                def plotly_chart(self,fig,**kw): self.figures.append(fig)

            ui = FakeUI()
            def canonical_stub(**kwargs):
                frame=kwargs['candles']
                return go.Figure(data=[go.Candlestick(
                    x=pd.to_datetime(frame.timestamp,unit='ms',utc=True),
                    open=frame.open,high=frame.high,low=frame.low,close=frame.close)])
            with patch.dict(sys.modules,{'streamlit':ui}):
                render_continuous_multiswing_view(root,chart_builder=canonical_stub)
            self.assertEqual(len(ui.figures),1, ui.warnings)
            self.assertFalse(ui.warnings)
            self.assertTrue(any(row[0]=='Doble o más' for row in ui.metrics))
            with sqlite3.connect(db) as con:
                self.assertEqual(con.execute('SELECT COUNT(*) FROM first_touches').fetchone()[0],1)


if __name__ == '__main__': unittest.main()
