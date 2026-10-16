import csv
import json
import tempfile
import unittest
from pathlib import Path

from dashboard.services import reaction_lab_historical as h
from runner_historical_phase3 import execute, parser, parity_evidence

M=60_000
T0=1790000000000//M*M


def make_fixture(base:Path, n=2):
    events=[]; factors=[]
    for idx in range(n):
        ts=T0+idx*300*M
        key=f'AAA{idx}USDT|SHORT|{ts}'
        payload={'max_departure_pct':1.6,'retest_timestamp':ts}
        events.append({'event_key':key,'symbol':f'AAA{idx}USDT','side':'SHORT',
                       'retest_ts':ts,'known_ts':ts+M,
                       'first_replay_observed_ms':ts+15*M,
                       'observation_type':'HISTORICAL_REPLAY_UNVERIFIED',
                       'source_hash':'dummy_source','causal_json':json.dumps(payload)})
        factors.append({'event_key':key,'room_1h_pct':3.0,'volume_rank_pct_4h':99.,
                        'return_rank_pct_4h':90.,'room_available_ms':ts+15*M,
                        'ranks_available_ms':ts+15*M,'room_asof_ms':ts+M,
                        'ranks_asof_ms':ts+M,'room_method':'ASOF_CONFIRMED_SWINGS_1H',
                        'ranks_method':'ASOF_CLOSED_4H_CROSS_SECTION'})
        # Stable OHLC until ready, lower target touched only after fill.
        bars=[]
        for j in range(240):
            open_=100.0
            low=97.8 if j==20 else 99.8
            bars.append({'timestamp':ts+(j+1)*M,'open':open_,'high':100.2,
                         'low':low,'close':100.0,'volume':123.})
        cache=base/'cache';cache.mkdir(exist_ok=True)
        (cache/f'cache_{idx}.json').write_text(json.dumps({
            'symbol':f'AAA{idx}USDT','timeframe':'1m','origin':'binance_futures_native_klines',
            'start_ms':ts+M,'end_ms':ts+241*M,'candles':bars}))
    h.write_csv(base/'events.csv',events)
    h.write_csv(base/'factors.csv',factors)
    summary={'mode':'research','symbols_failed':0,'data_gaps':0,'symbols_total':n,
             'canonical_hash':'dummy_source','step_minutes':15,'journal_events':0,
             'metrics':{'FULL_NUMERIC_PARITY':False}}
    (base/'summary.json').write_text(json.dumps(summary))
    return events,factors


class HistoricalTest(unittest.TestCase):
    def test_replay_validation_and_factor_join(self):
        with tempfile.TemporaryDirectory() as tmp:
            b=Path(tmp);make_fixture(b)
            events, summary=h.load_replay(b/'events.csv',b/'summary.json')
            factors=h.load_factors(b/'factors.csv')
            cand,err=h.prepare_candidates(events,factors)
            self.assertEqual(len(cand),2)
            self.assertEqual(err,[])
            self.assertEqual(cand[0]['ready_ms'],T0+15*M)

    def test_missing_factors_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            b=Path(tmp);make_fixture(b)
            events,_=h.load_replay(b/'events.csv')
            cand,problems=h.prepare_candidates(events,{})
            self.assertEqual(len(cand),0)
            self.assertEqual(len(problems),2)

    def test_bad_temporal_availability_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            b=Path(tmp);make_fixture(b,1)
            events,_=h.load_replay(b/'events.csv')
            factors=h.load_factors(b/'factors.csv')
            key=next(iter(factors))
            factors[key]['ranks_asof_ms']=T0+999*M
            self.assertIn('INVALID_FACTOR_ASOF',h.prepare_candidates(events,factors)[1][0]['reason'])

    def test_preexisting_closed_4h_snapshot_is_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            b=Path(tmp);make_fixture(b,1)
            events,_=h.load_replay(b/'events.csv')
            factors=h.load_factors(b/'factors.csv')
            key=next(iter(factors))
            # Last closed 4h context is often available BEFORE a REACTION.
            factors[key]['ranks_available_ms']=T0-30*M
            factors[key]['ranks_asof_ms']=T0-60*M
            factors[key]['room_available_ms']=T0-10*M
            factors[key]['room_asof_ms']=T0-11*M
            candidates, problems=h.prepare_candidates(events,factors)
            self.assertEqual(problems,[])
            self.assertEqual(candidates[0]['ready_ms'],T0+15*M)

    def test_noncausal_method_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            b=Path(tmp);make_fixture(b,1)
            events,_=h.load_replay(b/'events.csv')
            factors=h.load_factors(b/'factors.csv')
            key=next(iter(factors))
            factors[key]['room_method']='LATEST_REDIS'
            self.assertEqual(h.prepare_candidates(events,factors)[1][0]['reason'],
                             'NONCAUSAL_OR_UNKNOWN_FACTOR_METHOD')

    def test_path_contiguity_and_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            b=Path(tmp);make_fixture(b,1)
            events,_=h.load_replay(b/'events.csv')
            cand,_=h.prepare_candidates(events,h.load_factors(b/'factors.csv'))
            cache,_=h.load_candles_from_cache(b/'cache',{cand[0]['symbol']})
            records,errors=h.attach_paths(cand,cache)
            self.assertEqual(len(records),1)
            self.assertFalse(errors)
            del cache[cand[0]['symbol']][T0+120*M]
            self.assertEqual(h.attach_paths(cand,cache)[1][0]['reason'],
                             'MISSING_CONTIGUOUS_1M_PATH')

    def test_replay_does_not_claim_prospective(self):
        with tempfile.TemporaryDirectory() as tmp:
            b=Path(tmp);make_fixture(b,1)
            _,s=h.load_replay(b/'events.csv',b/'summary.json')
            self.assertFalse(parity_evidence(s))

    def test_template_has_no_fabricated_factors(self):
        with tempfile.TemporaryDirectory() as tmp:
            b=Path(tmp);make_fixture(b,1)
            events,_=h.load_replay(b/'events.csv')
            template=h.factor_template(events)
            self.assertEqual(template[0]['room_1h_pct'],'')
            self.assertEqual(template[0]['ranks_method'],'')

    def test_backtest_end_to_end_dry_run_and_new_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            b=Path(tmp);make_fixture(b)
            options=['--project',str(b),'--events',str(b/'events.csv'),
                     '--summary',str(b/'summary.json'),'--factors',str(b/'factors.csv'),
                     '--cache-dir',str(b/'cache'),'--output-root',str(b/'results'),
                     '--mode','run','--research-unverified']
            args=parser().parse_args(options+['--dry-run'])
            dry=execute(args)
            self.assertEqual(dry['complete_1m_paths'],2)
            self.assertIn('Runner Challenger 220',dry['strategies'])
            self.assertEqual(list((b/'results').glob('*')),[])
            saved=execute(parser().parse_args(options))
            folders=list((b/'results').glob('HIST_*'))
            self.assertEqual(len(folders),1)
            run=folders[0]
            self.assertTrue((run/'portfolio_ledger_v1.sqlite3').is_file())
            self.assertTrue((run/'historical_excursions.csv').is_file())
            manifest=json.loads((run/'manifest.json').read_text())
            self.assertEqual(manifest['cohort'],'HISTORICAL_RESEARCH_UNVERIFIED_OR_PARTIAL')
            self.assertEqual(saved['complete_1m_paths'],2)

    def test_unverified_source_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            b=Path(tmp);make_fixture(b)
            args=parser().parse_args(['--project',str(b),'--events',str(b/'events.csv'),
                       '--summary',str(b/'summary.json'),'--factors',str(b/'factors.csv'),
                       '--cache-dir',str(b/'cache'),'--mode','run'])
            with self.assertRaisesRegex(ValueError,'parity'):
                execute(args)


if __name__=='__main__':unittest.main()
