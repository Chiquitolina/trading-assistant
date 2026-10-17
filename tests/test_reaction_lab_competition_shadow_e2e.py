"""End-to-end create-only export using a tiny native-like local OHLC fixture."""
import csv
import json
import tempfile
import unittest
from pathlib import Path

from dashboard.services import reaction_lab_runner_optimizer as v2
from dashboard.services.reaction_lab_competition import export_readonly
from dashboard.services.reaction_lab_competition_shadow import export_shadow
from dashboard.services.reaction_lab_portfolio import source_digest
from dashboard.services import reaction_lab_historical as hist

STRATEGY = 'Runner Challenger 220'
PARAMS = {'departure':1.5,'room_1h':1,'volume_rank':40,'return_rank':40,
          'limit':0,'ttl':30,'tp':2,'sl':2}


def write_csv(path, rows):
    columns = list(rows[0])
    with path.open('w', encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(handle, columns)
        writer.writeheader();writer.writerows(rows)


class EndToEndTests(unittest.TestCase):
    def test_complete_export_and_provenance_hashes(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            run = base/'run';run.mkdir()
            cache=base/'cache';cache.mkdir()
            evpath=base/'events.csv'; fpath=base/'factors.csv'
            events=[]; factors=[]; records=[]
            for sym in ('AAAUSDT','BBBUSDT'):
                key=f'{sym}|LONG|0'
                causal={'max_departure_pct':2,'retest_timestamp':0}
                events.append({'event_key':key,'symbol':sym,'side':'LONG','retest_ts':0,
                               'known_ts':60000,'first_replay_observed_ms':60000,
                               'observation_type':'HISTORICAL_REPLAY_UNVERIFIED',
                               'source_hash':'fake_fixed_hash','causal_json':json.dumps(causal)})
                factors.append({'event_key':key,'room_1h_pct':9 if sym=='AAAUSDT' else 5,
                                'volume_rank_pct_4h':60,'return_rank_pct_4h':60,
                                'room_available_ms':60000,'ranks_available_ms':60000,
                                'room_asof_ms':60000,'ranks_asof_ms':60000,
                                'room_method':'ASOF_CONFIRMED_SWINGS_1H',
                                'ranks_method':'ASOF_CLOSED_4H_CROSS_SECTION'})
                candles=[]
                for i in range(1,241):
                    high=102.5 if i==4 else 100.4
                    candles.append({'timestamp':i*60000,'open':100.0,'high':high,
                                    'low':99.6,'close':100.0})
                (cache/f'{sym}.json').write_text(json.dumps({
                    'symbol':sym,'timeframe':'1m',
                    'origin':'binance_futures_native_klines','candles':candles}))
                records.append({'key':key,'symbol':sym,'side':'LONG','ready_ms':60001,
                                'retest_ts':0,'values':(2.0,9.0 if sym=='AAAUSDT' else 5.0,60.0,60.0),
                                'reference':100.0,
                                'bars':tuple((c['timestamp'],c['open'],c['high'],c['low'],c['close'])
                                             for c in candles)})
            write_csv(evpath,events);write_csv(fpath,factors)
            outcome=v2.simulate(records[0],PARAMS,costs=v2.DEFAULT_COSTS)
            self.assertEqual(outcome['status'],'TP')
            entry=outcome['fill_price']
            trades=[{'strategy':STRATEGY,'event_key':records[0]['key'],'status':'TP',
                     'fill_ts':outcome['fill_ts'],'exit_ts':outcome['exit_ts'],
                     'fill_price':entry,
                     'entry_liquidity':outcome['entry_liquidity'],
                     'exit_liquidity':outcome['exit_liquidity'],
                     'exit_price':entry*(1+outcome['gross_pct']/100),
                     'net_pct':outcome['pnl'],'gross_pct':outcome['gross_pct'],
                     'cost_pct':outcome['cost_pct']}]
            decisions=[{'strategy':STRATEGY,'event_key':records[0]['key'],
                        'symbol':records[0]['symbol'],'side':'LONG','ready_ms':60001,
                        'room_1h':9,'departure':2,'return_rank':60,'volume_rank':60,
                        'retest_ts':0,'decision':'ORDER_SUBMITTED','reason':'TP'},
                       {'strategy':STRATEGY,'event_key':records[1]['key'],
                        'symbol':records[1]['symbol'],'side':'LONG','ready_ms':60001,
                        'room_1h':5,'departure':2,'return_rank':60,'volume_rank':60,
                        'retest_ts':0,'decision':'SKIPPED_SLOT_BUSY',
                        'reason':f'slot_busy_through_{outcome["exit_ts"]+60000}'}]
            write_csv(run/'decisions.csv',decisions)
            write_csv(run/'trades.csv',trades)
            (run/'manifest.json').write_text(json.dumps({
                'source_records':len(records),'source_digest_sha256':source_digest(records),
                'cohort':'HISTORICAL_RESEARCH_UNVERIFIED_OR_PARTIAL',
                'profiles':{STRATEGY:PARAMS},'cost_assumptions':v2.DEFAULT_COSTS,
                'provenance':{'assumed_execution_delay_ms':0,'post_boundary_guard_ms':1}
            }))
            ledger=base/'ledger'
            export_readonly(run,ledger,events_path=evpath,factors_path=fpath)
            out=base/'shadow'
            result=export_shadow(run,ledger,evpath,fpath,cache,out)
            self.assertEqual(result['shadow_rows'],2)
            self.assertEqual(result['selected_orders_parity_verified'],1)
            self.assertTrue((out/'shadow_paths_1m.sqlite3').is_file())
            self.assertTrue((out/'competition_manifest.json').is_file())
            with self.assertRaises(FileExistsError):
                export_shadow(run,ledger,evpath,fpath,cache,out)
            with (out/'shadow_outcomes.csv').open() as f:
                rows=list(csv.DictReader(f))
            self.assertEqual(len(rows),2)
            self.assertEqual([x['shadow_status'] for x in rows],['TP','TP'])
            self.assertEqual(rows[1]['shadow_class'],'ELIGIBLE_BLOCKED_SLOT')

if __name__=='__main__':unittest.main()
