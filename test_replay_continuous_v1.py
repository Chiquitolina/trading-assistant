import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('replay_continuous_v1', ROOT/'replay_continuous_v1.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

class ReplayTests(unittest.TestCase):
    def test_timezone_required(self):
        with self.assertRaises(ValueError):
            mod.iso_to_ms('2026-10-14T12:40:00')
        self.assertEqual(mod.iso_to_ms('2026-10-14T12:40:00-03:00'),
                         mod.iso_to_ms('2026-10-14T15:40:00Z'))

    def test_checkpoints_inclusive_close(self):
        start, end = 12*mod.ONE_MIN+1000, 40*mod.ONE_MIN
        self.assertEqual(list(mod.checkpoints(start,end,15)),
                         [15*mod.ONE_MIN,30*mod.ONE_MIN,45*mod.ONE_MIN])

    def test_contiguity_detects_gap(self):
        data=[{'timestamp':0},{'timestamp':mod.ONE_MIN},{'timestamp':3*mod.ONE_MIN}]
        self.assertEqual(mod.contiguous_diagnostics(data,mod.ONE_MIN,0,4*mod.ONE_MIN),
                         [(mod.ONE_MIN,3*mod.ONE_MIN,1)])

    def test_comparison_numeric_and_identity(self):
        a={'A|LONG|10':{'event_key':'A|LONG|10','symbol':'A','side':'LONG','retest_ts':10,
           'payload':{'pivot_timestamp':1,'retest_close':100,'retest_timestamp':10}}}
        b={'A|LONG|10':{'event_key':'A|LONG|10','symbol':'A','side':'LONG','retest_ts':10,
           'source_hash':'hash','causal_json':json.dumps(a['A|LONG|10']['payload'])},
           'B|SHORT|20':{'symbol':'B','side':'SHORT','retest_ts':20,'source_hash':'hash',
           'causal_json':'{}'}}
        rows, metrics=mod.compare_events(a,b,'hash')
        self.assertEqual(metrics['MATCHED'],1)
        self.assertEqual(metrics['JOURNAL_ONLY'],1)
        self.assertEqual(metrics['NUMERIC_DIFFERENCES'],0)
        self.assertFalse(metrics['FULL_ID_PARITY'])
        self.assertEqual(rows[0]['source_hash_equal'],True)

    def test_numeric_comparison_handles_missing_optional_field(self):
        replayed={'A|LONG|10':{'event_key':'A|LONG|10','symbol':'A','side':'LONG',
                  'retest_ts':10,'payload':{'pivot_timestamp':None}}}
        existing={'A|LONG|10':{'event_key':'A|LONG|10','symbol':'A','side':'LONG',
                  'retest_ts':10,'source_hash':'hash',
                  'causal_json':json.dumps({'pivot_timestamp':1})}}
        rows,counts=mod.compare_events(replayed, existing, 'hash')
        self.assertEqual(counts['NUMERIC_DIFFERENCES'],1)
        self.assertEqual(rows[0]['numeric_differences'],'pivot_timestamp')

    def test_no_future_candles_in_virtual_loader(self):
        # Fake scanner checks two bar sets; closing-time guard must reject
        # 1m t+60 and 15m t+900 while virtual now is t.
        from pandas import DataFrame
        ticks=[180*mod.ONE_MIN,181*mod.ONE_MIN]
        bars1=[dict(timestamp=t,open=1.,high=1.,low=1.,close=1.,volume=2.)
               for t in range(0,190*mod.ONE_MIN,mod.ONE_MIN)]
        bars15=[dict(timestamp=t,open=1.,high=1.,low=1.,close=1.,volume=2.)
                for t in range(0,200*mod.ONE_MIN,15*mod.ONE_MIN)]
        class Scan:
            __globals__={}
            def __call__(self,**kwargs):
                loader=self.__globals__['load_volume_exhaustion_research_candles']
                a=loader('TEST','1m',5000)
                b=loader('TEST','15m',400)
                self_seen.append((ticks[len(self_seen)],int(a.timestamp.max()),int(b.timestamp.max())))
                return DataFrame()
        self_seen=[]
        events=mod.run_symbol('TEST',Scan(),lambda df,t:{},ticks,bars1,bars15,0,200*mod.ONE_MIN,15)
        self.assertEqual(events,{})
        self.assertEqual(self_seen,[(180*mod.ONE_MIN,179*mod.ONE_MIN,165*mod.ONE_MIN),
                                    (181*mod.ONE_MIN,180*mod.ONE_MIN,165*mod.ONE_MIN)])

    def test_latest_pivot_resolves_same_event_key_without_changing_event_id(self):
        from pandas import DataFrame
        ticks = [180 * mod.ONE_MIN]
        bars1 = [dict(timestamp=t, open=1., high=1., low=1., close=1., volume=2.)
                 for t in range(0, 190 * mod.ONE_MIN, mod.ONE_MIN)]
        bars15 = [dict(timestamp=t, open=1., high=1., low=1., close=1., volume=2.)
                  for t in range(0, 200 * mod.ONE_MIN, 15 * mod.ONE_MIN)]
        retest = 178 * mod.ONE_MIN
        class Scanner:
            __globals__ = {}
            def __call__(self, **kwargs):
                return DataFrame([
                    dict(symbol='TEST', signal='LONG', status='REACTION',
                         retest_timestamp=retest, pivot_timestamp=10 * mod.ONE_MIN,
                         swing_price=10., max_departure_pct=3.),
                    dict(symbol='TEST', signal='LONG', status='REACTION',
                         retest_timestamp=retest, pivot_timestamp=30 * mod.ONE_MIN,
                         swing_price=20., max_departure_pct=2.),
                ])
        def normalize(df, now):
            result = {}
            for rec in df.to_dict('records'):
                key = f"{rec['symbol']}|{rec['signal']}|{rec['retest_timestamp']}"
                result.setdefault(key, rec)
            return result
        def run(policy):
            return mod.run_symbol('TEST', Scanner(), normalize, ticks, bars1, bars15,
                                  0, 181 * mod.ONE_MIN, 1, policy)
        key = f'TEST|LONG|{retest}'
        self.assertEqual(run('native')[key]['payload']['swing_price'], 10.)
        self.assertEqual(run('latest-pivot')[key]['payload']['swing_price'], 20.)
        self.assertEqual(set(run('native')), set(run('latest-pivot')))

    def test_collision_policy_separates_resume_signatures(self):
        kw = dict(mode='parity', start_ms=1, end_ms=2, step_minutes=15,
                  project=Path('/tmp/p'), journal=Path('/tmp/j'),
                  cache_dir=Path('/tmp/c'), symbols=['TEST'], fingerprint='hash')
        self.assertNotEqual(mod.progress_signature(**kw, collision_policy='native'),
                            mod.progress_signature(**kw, collision_policy='latest-pivot'))

    def test_actual_ast_fingerprint_and_retest(self):
        import sys
        # Source package from the provided audit ZIP, not the user's live VPS.
        import os
        src=Path(os.environ.get('CONTINUOUS_V1_PROJECT','/home/multiservices-1m'))
        if not src.joinpath('reaction_lab_continuous_scanner.py').exists():
            self.skipTest('source not included')
        sys.path.insert(0,str(src))
        from reaction_lab_continuous_scanner import compile_canonical_scanner, normalize_reactions
        scan, h=compile_canonical_scanner(src/'dashboard/app.py', lambda **kw: None)
        self.assertEqual(len(h),20)
        base=5*60*mod.ONE_MIN  # 5-hour offset in ms
        bars15=[]
        for i in range(50):
            low=90.0 if i==10 else 95.0
            bars15.append(dict(timestamp=base+i*mod.FIFTEEN_MIN,
                               open=98.,high=100.,low=low,close=99.,volume=100.))
        # 1m history continuous covering confirmation + retest
        bars1=[]
        confirmation=base+14*mod.FIFTEEN_MIN
        for ts in range(confirmation-370*mod.ONE_MIN,confirmation+20*mod.ONE_MIN,mod.ONE_MIN):
            bar=dict(timestamp=ts,open=95.,high=95.5,low=94.5,close=95.,volume=100.)
            if ts==confirmation+5*mod.ONE_MIN:
                bar.update(open=90.4,high=90.8,low=90.03,close=90.7)
            bars1.append(bar)
        result=mod.run_symbol('TEST',scan,normalize_reactions,
                              [confirmation+6*mod.ONE_MIN],bars1,bars15,
                              confirmation,confirmation+12*mod.ONE_MIN,1)
        self.assertTrue(any(v['side']=='LONG' and v['retest_ts']==confirmation+5*mod.ONE_MIN
                            for v in result.values()),str(result))

    def test_replay_main_and_resume_without_repeating_symbols(self):
        import tempfile
        import types
        import sys
        from unittest.mock import patch
        import csv
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            project = root/'project'
            project.mkdir()
            output = root/'output'
            cache = root/'cache'
            journal = root/'journal.sqlite3'
            journal.touch()
            symbol = 'MOCKUSDT'
            ts = mod.iso_to_ms('2026-10-13T04:00:00Z')
            key = f'{symbol}|LONG|{ts}'
            payload = {'symbol':symbol,'signal':'LONG','status':'REACTION',
                       'retest_timestamp':ts,'pivot_timestamp':ts - 20 * 60_000,
                       'entry_price':100.0,'retest_close':100.0}
            event = {'event_key':key,'symbol':symbol,'side':'LONG','retest_ts':ts,
                     'known_ts':ts+mod.ONE_MIN, 'first_replay_observed_ms':ts+mod.ONE_MIN,
                     'payload':payload}
            journal_rows = {key:{'event_key':key,'symbol':symbol,'side':'LONG',
                                 'retest_ts':ts,'source_hash':'fakefingerprint',
                                 'causal_json':json.dumps(payload)}}
            fake_live = types.ModuleType('reaction_lab_continuous_scanner')
            fake_live.get_symbols = lambda project: (symbol,)
            fake_live.compile_canonical_scanner = lambda app,loader: (None,'fakefingerprint')
            fake_live.normalize_reactions = lambda frame,tick: {}
            argv = ['--project',str(project),'--mode','parity',
                    '--start','2026-10-13T00:00:00Z',
                    '--end','2026-10-14T00:00:00Z',
                    '--step-minutes','15','--output',str(output),
                    '--cache-dir',str(cache),'--journal',str(journal)]
            single_bar = [{'timestamp':ts,'open':100.,'high':100.,'low':100.,'close':100.,'volume':1.}]
            with patch.dict(sys.modules, {'reaction_lab_continuous_scanner':fake_live}), \
                 patch.object(mod,'load_journal',return_value=journal_rows), \
                 patch.object(mod,'fetch_klines',return_value=(single_bar,False)) as fetch, \
                 patch.object(mod,'run_symbol',return_value={key:event}) as run:
                self.assertEqual(mod.main(argv),0)
                self.assertEqual(run.call_count,1)
                self.assertEqual(fetch.call_count,2)
                self.assertTrue((output/'progress.json').is_file())
                self.assertEqual(mod.main(argv+['--resume']),0)
                self.assertEqual(run.call_count,1)
                self.assertEqual(fetch.call_count,2)
                with (output/'replay_events.csv').open() as f:
                    self.assertEqual(len(list(csv.DictReader(f))),1)
                with self.assertRaisesRegex(RuntimeError,'already contains'):
                    mod.main(argv)

    def test_resume_rejects_mismatched_signature(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            output=Path(d)
            mod.csv_write(output/'replay_events.csv',('event_key',),[])
            mod.csv_write(output/'history_gaps.csv',('symbol',),[])
            mod.write_progress(output/'progress.json','old',set())
            with self.assertRaisesRegex(RuntimeError,'changed'):
                mod.resume_or_initialize(output,resume=True,signature='new',symbols=['A'],start_ms=1,end_ms=2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
