import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
import sys

import pandas as pd

sys.path.insert(0,str(Path(__file__).resolve().parent))
import pre_reaction_opportunity_lab as lab

ONE=lab.ONE; FIFTEEN=lab.FIFTEEN


def mkbars(n=361,action=FIFTEEN,base=100.):
    return [dict(timestamp=action+i*ONE,open=base,high=base+0.01,
                 low=base-0.01,close=base,volume=10.) for i in range(n)]


def pivot(side='HIGH',price=100.,confirmed=0):
    return SimpleNamespace(side=side,price=price,
                           pivot_timestamp=confirmed-3*FIFTEEN,
                           confirmed_timestamp=confirmed)


class PureTests(unittest.TestCase):
    def test_no_departure_is_separate(self):
        r=lab.scan_opportunity(mkbars(),pivot())
        self.assertEqual(r['status'],'NO_DEPARTURE')
        self.assertIsNone(r['departure_known_ts'])

    def test_short_reaction_after_departure(self):
        bars=mkbars()
        bars[1].update(low=99.7,high=100.01,close=99.75)
        bars[2].update(open=99.8,low=99.8,high=100.02,close=99.83)
        r=lab.scan_opportunity(bars,pivot())
        self.assertEqual(r['status'],'REACTION')
        self.assertEqual(r['departure_known_ts'],bars[1]['timestamp']+ONE)
        self.assertEqual(r['first_touch_ts'],bars[2]['timestamp'])

    def test_long_reaction_mirror(self):
        bars=mkbars()
        bars[1].update(high=100.3,close=100.3)
        bars[2].update(high=100.2,low=99.99,close=100.19)
        r=lab.scan_opportunity(bars,pivot('LOW'))
        self.assertEqual(r['status'],'REACTION')

    def test_failed_first_touch_not_skipped(self):
        bars=mkbars()
        bars[1].update(low=99.7,close=99.7)
        bars[2].update(high=100.3,low=99.7,close=100.21)
        bars[3].update(high=100.,low=99.8,close=99.8)
        r=lab.scan_opportunity(bars,pivot())
        self.assertEqual(r['status'],'TOUCH_FAILED')
        self.assertEqual(r['first_touch_ts'],bars[2]['timestamp'])

    def test_ignores_same_candle_touch(self):
        bars=mkbars()
        bars[1].update(low=99.6,high=100.1,close=99.6)
        for bar in bars[2:]:bar.update(high=99.8,low=99.6,close=99.7)
        r=lab.scan_opportunity(bars,pivot())
        self.assertIsNone(r['first_touch_ts'])
        self.assertEqual(r['status'],'ARMED_NO_RETEST')

    def test_gap_never_looks_past_gap(self):
        bars=mkbars()
        bars[1].update(low=99.6,close=99.6)
        del bars[2]
        bars[2].update(low=99.8,high=100.03,close=99.8)
        r=lab.scan_opportunity(bars,pivot())
        self.assertEqual(r['status'],'UNKNOWN_GAP')
        self.assertIsNone(r['first_touch_ts'])

    def test_incomplete_horizon_is_unknown(self):
        bars=mkbars(6)
        bars[1].update(low=99.6,close=99.6)
        for bar in bars[2:]:bar.update(high=99.8,low=99.6,close=99.7)
        r=lab.scan_opportunity(bars,pivot())
        self.assertEqual(r['status'],'UNKNOWN_GAP')

    def test_15m_no_gap_aggregation(self):
        d=pd.DataFrame(mkbars(45,action=0))
        self.assertEqual(len(lab.contiguous_15m_from_1m(d)),3)
        d=d[d.timestamp!=7*ONE]
        self.assertEqual(len(lab.contiguous_15m_from_1m(d)),2)

    def test_real_3x3_swing_is_actionable_only_after_confirm_candle(self):
        bars=[]
        for i in range(12):
            high=101.0 if i==4 else 100.2
            bars.append(dict(timestamp=i*FIFTEEN,open=100.,high=high,
                             low=99.9,close=100.,volume=100.))
        points=lab.find_swings(pd.DataFrame(bars))
        highs=[p for p in points if p.side=='HIGH']
        self.assertEqual(len(highs),1)
        self.assertEqual(highs[0].pivot_timestamp,4*FIFTEEN)
        self.assertEqual(highs[0].confirmed_timestamp,7*FIFTEEN)
        self.assertEqual(highs[0].confirmed_timestamp+FIFTEEN,8*FIFTEEN)

    def test_no_future_changes_departure_PRE(self):
        x=mkbars()
        x[1].update(low=99.6,close=99.6)
        for b in x[2:]:b.update(high=99.7,low=99.6,close=99.7)
        a=lab.scan_opportunity(x,pivot())
        future=mkbars(20,action=x[-1]['timestamp']+ONE)
        for b in future:b.update(high=110.,close=102.)
        z=lab.scan_opportunity(x+future,pivot())
        for k in ('status','departure_ts','departure_known_ts','departure_price',
                  'departure_close','relative_volume_30m','first_touch_ts'):
            self.assertEqual(a[k],z[k],k)

    def test_month_crossing(self):
        a=lab.iso_ms('2026-09-30T23:00:00Z')
        b=lab.iso_ms('2026-10-01T23:00:00Z')
        self.assertEqual(list(lab.month_list(a,b)),['2026-09','2026-10'])

    def test_source_plan_and_pilot_local_read_only(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);cache=lab.DEFAULT_CACHE
            p=root/cache/'HANAUSDT'/'2026-10.pkl.gz';p.parent.mkdir(parents=True)
            start=lab.iso_ms('2026-10-12T12:00:00Z')
            end=start+3*60*ONE
            begin=start-3*60*ONE
            d=pd.DataFrame(mkbars(9*60,action=begin))
            d.to_pickle(p,compression='gzip')
            inv=lab.source_inventory(root,cache,['HANAUSDT'],start,end)
            self.assertTrue(inv['symbols'][0]['available_months'])
            out=root/'my_output'
            manifest=lab.write_pilot(root,cache,['HANAUSDT'],start,end,out)
            self.assertEqual(manifest['version'],lab.VERSION)
            lab.audit_pilot(out)
            with self.assertRaises(RuntimeError):lab.write_pilot(root,cache,['HANAUSDT'],start,end,out)
            self.assertEqual(d.shape[0],len(pd.read_pickle(p,compression='gzip')))


if __name__=='__main__':unittest.main()
