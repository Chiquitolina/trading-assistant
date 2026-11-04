"""Contractos de causalidad del overlay Reaction en PRE Discovery."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from dashboard.views import hll_reaction_pre_overlay as overlay

BASE=1_788_048_000_000  # alineado 15m en UTC
BASE=BASE-(BASE % overlay.TF15)


def minute_fixture(nbars=72):
    """15m completos; swing LOW seguido por alejamiento y retest REACTION."""
    rows=[]
    for i in range(nbars):
        block=BASE+i*overlay.TF15
        base=101.0
        low=100.6
        high=101.4
        close=101.0
        # Pivot: swing LOW isolated 15m; confirmado tres velas después.
        if i==40: low=100.0; high=101.2;close=100.8
        # Prohibir retest inmediato tras la confirmación.
        if i in (44,45,46):low=100.4;high=101.4;close=101.0
        if i==47:low=100.03;high=100.9;close=100.8
        # Evitar el mínimo del pivot durante el resto del test.
        for m in range(15):
            ts=block+m*overlay.TF1
            if m==0:
                o=close; h=high; l=low; c=close
            else:
                o=close; h=close; l=close; c=close
            rows.append(dict(timestamp=ts,open=o,high=h,low=l,close=c,volume=100.))
    return pd.DataFrame(rows)


class ReactionPreOverlayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root=Path(__file__).resolve().parents[2]
        cls.env,cls.swing,cls.hash=overlay.canonical_functions(
            str(cls.root/'dashboard/app.py'),(cls.root/'dashboard/app.py').stat().st_mtime_ns)

    def test_original_functions_only(self):
        self.assertTrue(all(name in self.env for name in overlay.ORIGINAL_FUNCTIONS))
        self.assertFalse('scan_confirmed_swing_retests_all_symbols' in self.env)

    def test_reaction_cutoff_does_not_see_future(self):
        tape=minute_fixture()
        visible=BASE+36*overlay.TF15
        end=BASE+60*overlay.TF15
        a=overlay.replay_canonical_pre(tape,end,visible,env=self.env,
              swing_detector_class=self.swing,symbol='TESTUSDT')
        self.assertTrue(any(e['side']=='LONG' and e['status']=='REACTION' for e in a),a)
        for e in a:
            self.assertLessEqual(e['known_ts'],end)
            self.assertLessEqual(e['actionable_ts'],e['retest_ts'])
        b=overlay.replay_canonical_pre(tape,end,visible,env=self.env,
              swing_detector_class=self.swing,symbol='TESTUSDT')
        self.assertEqual(a,b)
        # Cambiar una vela después de PRE no puede afectar señales PRE.
        extra=tape.iloc[-1:].copy()
        extra['timestamp']=end+overlay.TF1
        extra['high']=999.0
        newer=pd.concat([tape,extra],ignore_index=True)
        self.assertEqual(a,overlay.replay_canonical_pre(newer,end,visible,
                          env=self.env,swing_detector_class=self.swing,symbol='TESTUSDT'))

    def test_cannot_see_reaction_before_minute_close(self):
        tape=minute_fixture()
        visible=BASE+36*overlay.TF15
        # retest at 47*15 min, cutoff one minute BEFORE its close.
        cutoff=BASE+46*overlay.TF15
        prior=overlay.replay_canonical_pre(tape,cutoff,visible,
                  env=self.env,swing_detector_class=self.swing,symbol='TESTUSDT')
        self.assertFalse(any(e['known_ts']>cutoff for e in prior))
        after=overlay.replay_canonical_pre(tape,BASE+48*overlay.TF15,visible,
               env=self.env,swing_detector_class=self.swing,symbol='TESTUSDT')
        self.assertGreater(len(after),len(prior))

    def test_gap_refuses_replay(self):
        tape=minute_fixture()
        missing=BASE+50*overlay.TF15+overlay.TF1
        tape=tape.loc[tape.timestamp!=missing]
        with self.assertRaisesRegex(ValueError,'INCOMPLETE_1M_PRE'):
            overlay.replay_canonical_pre(tape,BASE+60*overlay.TF15,
                BASE+36*overlay.TF15,env=self.env,swing_detector_class=self.swing,
                symbol='TESTUSDT')

    def test_15m_aggregation(self):
        tape=minute_fixture()
        subset=overlay.closed_prefix(tape,BASE,BASE+30*overlay.TF15)
        frame=overlay.fifteen_from_one_minute(subset)
        self.assertEqual(len(frame),30)
        self.assertEqual(float(frame.iloc[0].low),100.6)

    def test_marker_does_not_modify_base_candle(self):
        import plotly.graph_objects as go
        f=go.Figure(go.Candlestick(x=[pd.Timestamp(BASE,unit='ms',tz='UTC')],
                                   open=[1],high=[2],low=[.5],close=[1.5]))
        events=[{'side':'LONG','known_ts':BASE+overlay.TF1,'retest_close':1.25,
                 'event_key':'TESTUSDT|LONG|'+str(BASE),'pivot_ts':BASE,
                 'swing_price':1.0}]
        overlay.add_markers(f,events,('LONG',))
        self.assertEqual(len(f.data),2)
        self.assertEqual(f.data[0].type,'candlestick')
        self.assertEqual(f.data[1].name,'REACTION LONG · 15m')

    def test_manifest_hash_guard(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'TEST_1m.csv';man=Path(temp)/'TEST.json'
            f=minute_fixture(4)
            f.to_csv(path,index=False)
            import json
            man.write_text(json.dumps({'bars':len(f),'sha256':overlay.file_sha256(path)}))
            self.assertEqual(len(overlay.load_verified_minute(path,man)),len(f))
            path.write_text(path.read_text()+'\n')
            with self.assertRaisesRegex(ValueError,'Hash'):
                overlay.load_verified_minute(path,man)

if __name__=='__main__': unittest.main()
