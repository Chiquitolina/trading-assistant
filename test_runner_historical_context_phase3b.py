"""Offline tests: no Binance or production-data writes."""
import csv
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import runner_historical_context_phase3b as h


def candle(ts, price=10., *, quote=1000., high=None, low=None):
    return {'timestamp': int(ts), 'open': float(price), 'high': float(high if high is not None else price*1.005),
            'low': float(low if low is not None else price*.995), 'close': float(price),
            'volume': float(quote/price), 'quoteVolume': float(quote)}


def markets(boundary, symbols=('AAAUSDT', 'BBBUSDT', 'CCCUSDT')):
    # 43 consecutive 4h candles. Current returns sorted and relative vols sorted.
    out = {}
    for i, sym in enumerate(symbols):
        bars = [candle(boundary-(42-j)*h.FOUR_H, price=10, quote=1000) for j in range(43)]
        bars[-1] = candle(boundary, price=10+(.1*i), quote=1000*(1+i))
        out[sym] = bars
    return out


class TestCausalContext(unittest.TestCase):
    def test_boundary_uses_only_closed_bars(self):
        self.assertEqual(h.closed_boundary(5*h.FOUR_H+1, h.FOUR_H), 4*h.FOUR_H)
        self.assertEqual(h.closed_boundary(5*h.FOUR_H, h.FOUR_H), 4*h.FOUR_H)
        self.assertEqual(h.closed_boundary(5*h.FOUR_H-1, h.FOUR_H), 3*h.FOUR_H)

    def test_last_candle_checks(self):
        e={'known_ts':22*h.HOUR+60_000, 'side':'SHORT', 'payload':{'retest_close':10.}}
        bars = [candle(i*h.HOUR) for i in range(21)]
        r, err, _ = h.build_room(e,bars, {'SwingDetector': mock.Mock(), 'room':mock.Mock()})
        self.assertEqual(err,'MISSING_LAST_CLOSED_1H')

    def test_room_uses_168_bar_window_and_prevents_future(self):
        calls=[]
        class Detector:
            def __init__(self,**kwargs):
                self.kwargs=kwargs
            def detect_all(self, bars):
                calls.append(bars)
                return ['swing']
        def calc(swings, known, tf, price, side):
            self.assertEqual(known,200*h.HOUR+60_000)
            self.assertEqual(tf,h.HOUR)
            self.assertEqual(price,10.)
            self.assertEqual(side,'LONG')
            return {'opposing_room_pct':3.0,'opposing_swing_price':10.3,
                    'opposing_swing_confirmed_timestamp':190*h.HOUR,
                    'opposing_swing_actionable_timestamp':191*h.HOUR}
        e={'known_ts':200*h.HOUR+60_000,'side':'LONG','payload':{'retest_close':10.}}
        bars=[candle(i*h.HOUR) for i in range(201)] + [candle(202*h.HOUR,price=1e6)]
        v,err,info=h.build_room(e,bars,{'SwingDetector':Detector,'room':calc})
        self.assertEqual(err,'')
        self.assertEqual(v,3.0)
        self.assertEqual(len(calls[0]),168)
        self.assertEqual(calls[0][-1]['timestamp'],199*h.HOUR)
        self.assertEqual(info['asof_ms'],200*h.HOUR)
        self.assertEqual(info['available_ms'],200*h.HOUR+60_000)

    def test_room_cannot_use_not_yet_actionable_swing(self):
        class Detector:
            def __init__(self,**kwargs):pass
            def detect_all(self,bars):return ['x']
        def calc(*args):
            return {'opposing_room_pct':2.,'opposing_swing_confirmed_timestamp':300*h.HOUR,
                    'opposing_swing_actionable_timestamp':301*h.HOUR,'opposing_swing_price':10.2}
        e={'known_ts':200*h.HOUR+60_000,'side':'LONG','payload':{'retest_close':10.}}
        bars=[candle(i*h.HOUR) for i in range(200)]
        with self.assertRaisesRegex(RuntimeError,'future confirmed swing'):
            h.build_room(e,bars,{'SwingDetector':Detector,'room':calc})

    def test_room_no_opposing_is_missing_not_zero(self):
        class Detector:
            def __init__(self,**kwargs):pass
            def detect_all(self,bars):return []
        e={'known_ts':200*h.HOUR+60_000,'side':'LONG','payload':{'retest_close':10.}}
        bars=[candle(i*h.HOUR) for i in range(200)]
        r,issue,_ = h.build_room(e,bars,{'SwingDetector':Detector,'room':lambda *x:{'opposing_room_pct':math.nan}})
        self.assertIsNone(r)
        self.assertEqual(issue,'NO_FAVORABLE_CONFIRMED_OPPOSING_SWING_1H')

    def test_ranks_are_whole_universe(self):
        boundary=100*h.FOUR_H
        known=boundary+h.FOUR_H+60_000
        event={'event_key':'AAAUSDT|LONG|x','known_ts':known}
        def snapshot(boundary, histories, universe, sectors, baseline, min_sector, sig):
            import pandas as pd
            self.assertEqual(set(histories),set(universe))
            self.assertEqual(baseline,42)
            return pd.DataFrame([{'symbol':s,'return_rank_pct_4h':50.+i*25,
                                  'volume_rank_pct_4h':50.+i*25} for i,s in enumerate(universe)])
        symbols=('AAAUSDT','BBBUSDT','CCCUSDT')
        result,diag=h.rank_snapshots({'a':event},symbols,markets(boundary,symbols),
                                     {'build_snapshot':snapshot,'universe_signature':lambda x:'x'},.95)
        self.assertEqual(diag[0]['coverage_pct'],100)
        self.assertEqual(len(result[boundary][0]),3)
        self.assertEqual(result[boundary][0]['CCCUSDT']['volume_rank_pct_4h'],100)

    def test_missing_market_does_not_fake_percentile(self):
        boundary=100*h.FOUR_H
        e={'known_ts':boundary+h.FOUR_H+60_000}
        symbols=('AAAUSDT','BBBUSDT','CCCUSDT')
        m=markets(boundary,symbols)
        m['CCCUSDT']=[]
        import pandas as pd
        result,diag=h.rank_snapshots({'x':e},symbols,m,
                 {'build_snapshot':lambda *args:pd.DataFrame([{'symbol':'AAAUSDT'},{'symbol':'BBBUSDT'}]),
                  'universe_signature':lambda x:'x'},.95)
        self.assertEqual(diag[0]['issue'],'LOW_4H_UNIVERSE_COVERAGE')
        self.assertFalse(result[boundary][0])

    def test_gapped_historical_universe_is_rejected(self):
        boundary=100*h.FOUR_H
        e={'known_ts':boundary+h.FOUR_H+60_000}
        symbols=('AAAUSDT','BBBUSDT','CCCUSDT')
        m=markets(boundary,symbols)
        m['CCCUSDT'].pop(-8)
        m['CCCUSDT'].insert(0,candle(boundary-43*h.FOUR_H))
        result,diag=h.rank_snapshots({'x':e},symbols,m,
                 {'build_snapshot':lambda *args:None, 'universe_signature':lambda x:'x'},.95)
        self.assertEqual(diag[0]['issue'],'NONCONTIGUOUS_CANONICAL_4H_HISTORY')

    def test_missing_room_factor_fails_closed(self):
        known=200*h.HOUR+60_000
        key=f'AAAUSDT|LONG|{known-60_000}'
        events={key:{'event_key':key,'symbol':'AAAUSDT','side':'LONG','retest_ts':known-60_000,'known_ts':known}}
        b=h.closed_boundary(known,h.FOUR_H)
        rows=h.factors_for_events(events,{key:(None,'NO_ROOM',{})},
            {b:({'AAAUSDT':{'volume_rank_pct_4h':90.,'return_rank_pct_4h':80.}},'')},
            [{'boundary_open_ms':b,'valid_symbols':3,'universe_total':3,'coverage_pct':100}])
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['room_1h_pct'],'')
        self.assertEqual(rows[0]['room_method'],'')
        self.assertEqual(rows[0]['factor_issue'],'NO_ROOM')

    def test_factor_availability_can_feed_phase3a_bridge(self):
        from dashboard.services.reaction_lab_historical import prepare_candidates
        known=200*h.HOUR+60_000
        retest=known-60_000
        key=f'AAAUSDT|LONG|{retest}'
        events={key:{'event_key':key,'symbol':'AAAUSDT','side':'LONG','retest_ts':retest,
                     'known_ts':known,'first_replay_observed_ms':known+14*60_000,
                     'causal_json':json.dumps({'max_departure_pct':2.,'retest_timestamp':retest})}}
        b=h.closed_boundary(known,h.FOUR_H)
        rooms={key:(3.,'',{'available_ms':known,'asof_ms':h.closed_boundary(known,h.HOUR)+h.HOUR,
                           'reference_price':10.,'opposing_swing_price':10.3,
                           'opposing_swing_confirmed_ts':known-2*h.HOUR,
                           'opposing_swing_actionable_ts':known-h.HOUR})}
        rankings={b:({'AAAUSDT':{'volume_rank_pct_4h':90.,'return_rank_pct_4h':80.}},'')}
        diag=[{'boundary_open_ms':b,'valid_symbols':100,'universe_total':100,'coverage_pct':100}]
        row=h.factors_for_events(events,rooms,rankings,diag)[0]
        candidates,problems=prepare_candidates(events,{key:row})
        self.assertEqual(problems,[])
        self.assertEqual(len(candidates),1)
        self.assertEqual(candidates[0]['values'],(2.,3.,90.,80.))
        self.assertEqual(candidates[0]['first_seen_ms'],known+14*60_000)

    def test_live_canonical_source_compiles_when_present(self):
        project=Path(__file__).resolve().parent
        if not (project/'dashboard/app.py').exists():
            self.skipTest('Canonical project source not in test directory')
        result=h.load_canonical_source(project)
        self.assertTrue(result['math_ast_sha256'])
        self.assertTrue(result['room_ast_sha256'])
        self.assertTrue(callable(result['build_snapshot']))
        self.assertTrue(callable(result['room']))

    def test_canonical_numeric_fixture_if_project_available(self):
        project=Path(__file__).resolve().parent
        if not (project/'dashboard/app.py').exists():
            self.skipTest('Real dashboard source not included in standalone tests directory')
        cs=h.load_canonical_source(project)
        boundary=100*h.FOUR_H
        symbols=('AAAUSDT','BBBUSDT','CCCUSDT')
        events={'x':{'known_ts':boundary+h.FOUR_H+60_000}}
        output,diag=h.rank_snapshots(events,symbols,markets(boundary,symbols),cs,.95)
        self.assertEqual(diag[0]['coverage_pct'],100.0)
        self.assertEqual([output[boundary][0][s]['return_rank_pct_4h'] for s in symbols],[0.,50.,100.])
        self.assertEqual([output[boundary][0][s]['volume_rank_pct_4h'] for s in symbols],[0.,50.,100.])

    def test_replay_summary_cannot_be_partial(self):
        with tempfile.TemporaryDirectory() as d:
            base=Path(d)
            csvfile=base/'replay_events.csv'
            csvfile.write_text('x')
            (base/'summary.json').write_text(json.dumps({
                'mode':'research','symbols_failed':1,'symbols_ok':4,
                'symbols_total':5,'data_gaps':0,'replay_events':1,
                'canonical_hash':'fingerprint'}))
            with self.assertRaisesRegex(ValueError,'incomplete'):
                h.verify_replay_summary(csvfile,{'x':{'source_hash':'fingerprint'}})

    def test_offline_cache_cannot_silently_fetch(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(h,'_api_klines') as api:
                with self.assertRaises(FileNotFoundError):
                    h.download_cached('AAAUSDT','4h',0,1000,d,offline=True)
                api.assert_not_called()

    def test_cache_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            rows=[candle(0)]
            with mock.patch.object(h,'_api_klines',return_value=rows) as api:
                a,cached,path=h.download_cached('AAAUSDT','4h',0,h.FOUR_H,d)
                self.assertFalse(cached)
                b,cached2,path2=h.download_cached('AAAUSDT','4h',0,h.FOUR_H,d,offline=True)
                self.assertTrue(cached2)
                self.assertEqual(a,b)
                self.assertEqual(path,path2)
                self.assertEqual(api.call_count,1)


if __name__=='__main__': unittest.main()
