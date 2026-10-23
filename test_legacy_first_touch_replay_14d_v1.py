import csv
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import pandas as pd

from legacy_first_touch_replay_14d_v1 import (
    ONE, bounds, normalized_symbols, make_paths, forward_cache, canonical_check,
    source_info, write_csv, read_csv, verify_symbol, report_all, replay_symbol,
    _get_entry_outcome, attach_fail_touch_metrics, VERSION, FILES,
    EVENT_COLUMNS,OPPORTUNITY_COLUMNS,ENTRY_COLUMNS,GEOMETRY_COLUMNS,
)
from pre_reaction_opportunity_lab import iso_ms, sha


class Tests(unittest.TestCase):
    def test_bounds_14d_outcomes_covered(self):
        a=iso_ms('2026-09-17T00:00:00Z');b=iso_ms('2026-10-01T00:00:00Z')
        lo,hi=bounds(a,b)
        self.assertEqual(lo,a-180*ONE)
        self.assertEqual(hi,b+1081*ONE)
        self.assertEqual((hi-lo)//ONE,14*1440+180+1081)

    def test_reject_time_bad(self):
        with self.assertRaises(ValueError):bounds(ONE,ONE)
        with self.assertRaises(ValueError):bounds(1,ONE)

    def test_sym_universe_disallows_silent_selection(self):
        with tempfile.TemporaryDirectory() as d:
            base=Path(d);f=base/'syms.txt';f.write_text('AAAUSDT\nBBBUSDT\n')
            class Args: symbols='';symbols_file=None
            (base/'reports/candidate_long_horizon/candle_cache/15m/AAAUSDT').mkdir(parents=True)
            with self.assertRaisesRegex(RuntimeError,'not 290'):
                normalized_symbols(base,Args)
            Args.symbols_file=Path('syms.txt')
            self.assertEqual(normalized_symbols(base,Args),['AAAUSDT','BBBUSDT'])
            Args.symbols='AAAUSDT,AAAUSDT'
            with self.assertRaisesRegex(ValueError,'Duplicate'):normalized_symbols(base,Args)

    def test_forward_cache_cannot_bridge_gap(self):
        one=pd.DataFrame({'timestamp':[0,ONE,3*ONE,4*ONE],
            'open':[1.]*4,'high':[2.]*4,'low':[.5]*4,'close':[1.]*4})
        f=forward_cache(one)
        self.assertEqual(list(f['segment_end']),[1,1,3,3])

    def test_canonical_touch_exact_and_mismatch(self):
        start=100*ONE
        one=pd.DataFrame({'timestamp':[start+i*ONE for i in range(4)],
            'open':[100.]*4,'high':[101.]*4,'low':[99.]*4,'close':[100.]*4,'volume':[1.]*4})
        op={'actionable_ts':start,'pivot_ts':0,'pivot_price':100.,'side':'LONG',
            'status':'TOUCH_FAILED','first_touch_ts':start+ONE}
        def correct(**_):return {'status':'TOUCH_FAILED','retest_timestamp':start+ONE}
        self.assertEqual(canonical_check(op,one,correct)[0],'EXACT_TOUCH')
        def wrong(**_):return {'status':'REACTION','retest_timestamp':start+ONE}
        self.assertEqual(canonical_check(op,one,wrong)[0],'STATUS_MISMATCH')
        def none(**_):return None
        self.assertEqual(canonical_check(op,one,none)[0],'MISSING_CANONICAL')
        self.assertEqual(canonical_check({**op,'status':'ARMED_NO_RETEST'},one,none)[0],'NO_TOUCH_BOTH')

    def test_legacy_failed_touch_geometry_and_grid(self):
        base=100*ONE
        one=pd.DataFrame({
            'timestamp':[base+i*ONE for i in range(370)],
            'open':[100.]*370,'high':[100.1]*370,'low':[99.9]*370,'close':[100.]*370})
        one.loc[0,['open','high','low','close']]=[100,100.2,97.0,97.6]
        one.loc[1,['open','high','low','close']]=[97.6,99.9,97.5,99.6]
        # remaining candles unchanged at 100 -> reclaim
        cache=forward_cache(one)
        row=dict(status='TOUCH_FAILED',signal='LONG',swing_price=100.,
            retest_timestamp=base,retest_open=100.,retest_high=100.2,
            retest_low=97.,retest_close=97.6)
        r=attach_fail_touch_metrics(row,cache)
        self.assertAlmostEqual(r['ftr_touch_penetration_pct'],3)
        self.assertEqual(r['ftr_reclaim_min'],2)
        entry=r['ftr_grid']['2.0|DEPTH_NEXT_OPEN']
        self.assertEqual(entry['signal_ts'],base)
        self.assertEqual(entry['entry_ts'],base+ONE)
        self.assertTrue(r['ftr_complete_180m'])
        self.assertEqual(len(r['ftr_grid']),15)
        self.assertIn(_get_entry_outcome(entry,2.0,1.0,180)[0],
            ('TP_FIRST','SL_FIRST','SL_AMBIGUOUS','NO_HIT','CENSORED'))

    def test_source_missing_does_not_invent_history(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t)
            s,df,files=source_info(root,Path('unused'),root/'isolate','AAAUSDT',0,10*ONE)
            self.assertEqual(s,'MISSING')
            self.assertIsNone(df)

    def test_output_verify_tamper_and_censoring(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);d=root/'result';s='AAAUSDT';folder=d/s;folder.mkdir(parents=True)
            app=root/'dashboard/app.py';app.parent.mkdir();app.write_text('app')
            scan=root/'reaction_lab_continuous_scanner.py';scan.write_text('scanner')
            a=0;b=100*ONE
            data={FILES[0]:[],FILES[1]:[],FILES[2]:[],FILES[3]:[]}
            cols=(EVENT_COLUMNS,OPPORTUNITY_COLUMNS,GEOMETRY_COLUMNS,ENTRY_COLUMNS)
            for name,col in zip(FILES,cols):write_csv(folder/name,data[name],col)
            m={'version':VERSION,'symbol':s,'data_start':'1970-01-01T00:00:00+00:00',
                'data_end_exclusive':'1970-01-01T01:40:00+00:00',
                'app_sha256':sha(app),'scanner_sha256':sha(scan),'source_files_sha256':{},
                'hashes':{name:sha(folder/name) for name in FILES},
                'first_touches':0,'failed_geometry':0,'entry_rows':0,'status_counts':{},'parity':{}}
            (folder/'manifest.json').write_text(json.dumps(m))
            self.assertEqual(verify_symbol(root,d,s,app,scan,a,b)['symbol'],s)
            (folder/'failed_entries.csv').write_text('tampered')
            with self.assertRaisesRegex(RuntimeError,'Corrupt output'):
                verify_symbol(root,d,s,app,scan,a,b)

    def test_full_symbol_replay_with_legacy_failed_and_entry_grid(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);symbol='AAAUSDT'
            first=0;last=1000*ONE
            start=180*ONE;end=279*ONE
            rows=pd.DataFrame({'timestamp':[first+i*ONE for i in range(1000)],
                'open':[100.]*1000,'high':[100.1]*1000,
                'low':[99.9]*1000,'close':[100.]*1000,'volume':[1.]*1000})
            rows.loc[190,['open','high','low','close']]=[100.,100.2,97.,97.6]
            source=root/'cache'/symbol/'1970-01.pkl.gz';source.parent.mkdir(parents=True)
            rows.to_pickle(source,compression='gzip')
            app=root/'dashboard/app.py';app.parent.mkdir(parents=True);app.write_text('mock app')
            scanner=root/'reaction_lab_continuous_scanner.py';scanner.write_text('mock scanner')
            op=dict(symbol=symbol,side='LONG',pivot_ts=60*ONE,
                actionable_ts=start,pivot_price=100.,
                status='TOUCH_FAILED',first_touch_ts=190*ONE,
                departure_ts=181*ONE,opportunity_key='AAAUSDT|3x3|LONG|pivot')
            legacy=dict(status='TOUCH_FAILED',signal='LONG',
                retest_timestamp=190*ONE,pivot_timestamp=60*ONE,
                swing_price=100.,departure_timestamp=181*ONE,
                departure_price=100.5,retest_open=100.,
                retest_high=100.2,retest_low=97.,retest_close=97.6)
            def fake_scan(*a,**k):return None
            from reaction_lab_continuous_scanner import compile_canonical_scanner
            with (mock.patch.dict(fake_scan.__globals__, {'_find_confirmed_swing_retest':lambda **kwargs:None}),
                  mock.patch('reaction_lab_continuous_scanner.compile_canonical_scanner',return_value=(fake_scan,'ast')),
                  mock.patch('legacy_first_touch_replay_14d_v1.find_variant_opportunities',return_value=[op]),
                  mock.patch('legacy_first_touch_replay_14d_v1.canonical_check',return_value=('EXACT_TOUCH',legacy))):
                report=replay_symbol(root,Path('cache'),root/'isolate',root/'out',symbol,start,end,0,1000*ONE,app,scanner)
            self.assertEqual(report['first_touches'],1)
            self.assertEqual(report['failed_geometry'],1)
            self.assertEqual(report['entry_rows'],15)
            valid=verify_symbol(root,root/'out',symbol,app,scanner,0,1000*ONE)
            self.assertEqual(valid['parity'],{'EXACT_TOUCH':1})
            entry=read_csv(root/'out'/symbol/'failed_entries.csv')
            self.assertEqual(sum(x['entry_ts']!='' for x in entry),15)

    def test_report_total_does_not_invent_winners(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t);out=p/'results';s='AAAUSDT';(out/s).mkdir(parents=True)
            (out/s/'manifest.json').write_text(json.dumps({'status_counts':{'TOUCH_FAILED':1},
                'parity':{'EXACT_TOUCH':1},'failed_geometry':1}))
            write_csv(out/s/'failed_entries.csv',[{'depth_pct':0.5,'mode':'DEPTH_NEXT_OPEN','side':'LONG','tp2_sl1_180_status':'CENSORED','barrier_first_hits_json':'','entry_failure_status':'CENSORED'}],
                ('depth_pct','mode','side','tp2_sl1_180_status','barrier_first_hits_json','entry_failure_status'))
            result=report_all(p,out,[s,'BBB'])
            self.assertEqual(result['symbols_complete'],1)
            self.assertEqual(result['tp2_sl1_180_depth0_5_next_open'],{'CENSORED':1})
            self.assertEqual(result['symbols_missing'],['BBB'])


if __name__=='__main__':unittest.main()
