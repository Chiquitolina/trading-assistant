import csv
import io
import json
import contextlib
import math
from pathlib import Path
import sqlite3
import struct
import tempfile
from types import SimpleNamespace
from contextlib import closing
import unittest
import zlib

import runner_precursor_pilot as p


class PilotTests(unittest.TestCase):
    def test_decode_strict_full_and_gaps(self):
        r=1_790_000_000_000//p.MIN*p.MIN
        rows=[(r+p.MIN+i*p.MIN,100.,101.,99.,100.) for i in range(360)]
        blob=zlib.compress(b''.join(p.BAR.pack(*x) for x in rows))
        self.assertEqual(len(p.decode(blob,r+p.MIN,r,360)),360)
        self.assertIsNone(p.decode(blob,r+p.MIN*2,r,360))
        rows[15]=(rows[15][0]+p.MIN,*rows[15][1:])
        bad=zlib.compress(b''.join(p.BAR.pack(*x) for x in rows))
        self.assertIsNone(p.decode(bad,r+p.MIN,r,360))

    def test_outcomes_long_short(self):
        base=100
        rows=[(i*p.MIN,base,base,base,base) for i in range(360)]
        rows[19]=(rows[19][0],100,104,100,104)
        _base,mfe,mae,hits=p.outcomes(rows,'LONG')
        self.assertAlmostEqual(mfe,4)
        self.assertEqual(hits[3],20)
        self.assertEqual(hits[5],None)
        self.assertLess(p.outcomes(rows,'SHORT')[1],1)

    def test_windows_both_anchors_and_audit_only_future(self):
        retest=1_790_000_000_000//p.MIN*p.MIN
        s={'event_key':'ABCUSDT|LONG|x','symbol':'ABCUSDT','side':'LONG',
           'retest_ts':retest,'known_ts':retest+p.MIN}
        rs=p.windows([s],120,96,15)
        one=rs[('ABCUSDT','1m')]
        # Pre retest covers 120 earlier minutes, while post audit covers 15 later.
        self.assertTrue(any(a<=retest-120*p.MIN<=b for a,b in one))
        self.assertTrue(any(a<=retest+15*p.MIN<=b for a,b in one))
        self.assertIn(('BTCUSDT','15m'),rs)

    def test_match_not_same_wave_and_symbol(self):
        base=1_790_000_000_000//p.MIN*p.MIN
        cs=[]
        for i in range(12):
            c={'event_key':str(i),'symbol':'S'+str(i//2),
               'side':'LONG','retest_ts':base+i*p.MIN,
               'known_ts':base+(i+1)*p.MIN,
               'wave_minute':(base+(i+1)*p.MIN)//p.MIN,
               'episode_id':'EP'+str(i//2), 'class_label':'RUNNER_3' if i%2==0 else 'CONTROL_LT3',
               'observation_type':'LIVE_OBSERVED'}
            cs.append(c)
        selected=p.select_sample(cs,2,1)
        self.assertEqual(len(selected),4)
        self.assertEqual(len({x['episode_id'] for x in selected}),4)

    def test_episode_members_only_seed(self):
        with tempfile.TemporaryDirectory() as td:
            file=Path(td)/'members.csv'
            file.write_text('event_key,episode_id,position_in_episode\nA,E1,1\nB,E1,2\n')
            ep=p.load_episode_csv(file)
            self.assertEqual(ep['A'],('E1',1))
            self.assertEqual(ep['B'],('E1',2))

    def test_api_payload_validated_activity(self):
        ts=1_790_000_000_000//p.MIN*p.MIN
        data=[[ts,'100','102','99','101','35',ts+p.MIN-1,'3500',22,'14','1400','0']]
        rows=p.parse_klines(json.dumps(data).encode(),'TESTUSDT','1m',ts,ts)
        self.assertEqual(rows[0],(ts,100,102,99,101,35,3500,22,14,1400))
        data[0][9]='36'
        with self.assertRaises(ValueError):p.parse_klines(json.dumps(data).encode(),'TESTUSDT','1m',ts,ts)
        data[0][9]='14';data[0][6]=ts+p.MIN+1
        with self.assertRaises(ValueError):p.parse_klines(json.dumps(data).encode(),'TESTUSDT','1m',ts,ts)

    def test_missing_ranges_and_chunks(self):
        self.assertEqual(list(p.missing(0,4,[0,2,4],1)),[(1,1),(3,3)])
        self.assertEqual(list(p.chunks(0,2500,1,1000)),[(0,999),(1000,1999),(2000,2500)])

    def test_save_resume_conflict(self):
        with tempfile.TemporaryDirectory() as td:
            with closing(p.connect(Path(td)/'db.sqlite')) as con:
                row=(0,100.,102.,99.,101.,35.,3500.,22,14.,1400.)
                new,bad=p.ingest(con,'XUSDT','1m',0,0,[row],'abc')
                self.assertEqual((new,bad),(1,0))
                new,bad=p.ingest(con,'XUSDT','1m',0,0,[row],'abc')
                self.assertEqual((new,bad),(0,0))
                changed=list(row);changed[4]=100.8
                new,bad=p.ingest(con,'XUSDT','1m',0,0,[changed],'def')
                self.assertEqual((new,bad),(0,1))
                self.assertEqual(con.execute('SELECT COUNT(*) FROM conflicts').fetchone()[0],1)

    def test_metrics_causal_direction_volume_taker(self):
        rows=[]
        for i in range(120):
            close=100+i*.1
            rows.append((i*p.MIN,close,close+0.1,close-0.1,close,10,1000,4,5,500))
        f=p.segment_features(rows,'LONG','a')
        self.assertGreater(f['a_ret_30_pct'],0)
        self.assertAlmostEqual(f['a_taker_share_15'],.5)
        self.assertEqual(f['a_trades_15'],60)
        self.assertLess(p.segment_features(rows,'SHORT','b')['b_ret_30_pct'],0)

    def test_aggregation_against_1m(self):
        with tempfile.TemporaryDirectory() as td:
            with closing(p.connect(Path(td)/'db.sqlite')) as con:
                ts=1_790_000_000_000//(15*p.MIN)*(15*p.MIN)
                for i in range(15):
                    p.ingest(con,'XUSDT','1m',ts+i*p.MIN,ts+i*p.MIN,
                       [(ts+i*p.MIN,100,100,100,100,10,1000,3,5,500)],'abc')
                p.ingest(con,'XUSDT','15m',ts,ts,[(ts,100,100,100,100,150,15000,45,75,7500)],'q')
                matched,issues=p.validate_aggregation(con,[{'symbol':'XUSDT'}],120,96)
                self.assertEqual((matched,issues),(1,[]))

    def _make_provider_anomaly(self,con,symbol,ts,proof):
        # 15 synthetic 1m bars with exact observed aggregate totals. SQLite
        # uses the same schema/aggregation path as the real pilot database.
        got=proof['one_min_aggregate'];expected=proof['binance_15m']
        for i in range(15):
            row=(ts+i*p.MIN,got[0],got[1],got[2],
                 got[3] if i==14 else got[0],
                 got[4]/15,got[5]/15,0,got[7]/15,got[8]/15)
            # Preserve exact trade total as integers.
            row=list(row)
            row[7]=got[6]//15+(1 if i<got[6]%15 else 0)
            p.ingest(con,symbol,'1m',ts+i*p.MIN,ts+i*p.MIN,[tuple(row)],'one_min')
        p.ingest(con,symbol,'15m',ts,ts,[(ts,*expected)],'fifteen_min')

    def test_provider_anomalies_only_16_exact_cases(self):
        with tempfile.TemporaryDirectory() as td:
            with closing(p.connect(Path(td)/'db.sqlite')) as con:
                for (sym,ts), proof in p.BINANCE_VERIFIED_15M_ANOMALIES.items():
                    self._make_provider_anomaly(con,sym,ts,proof)
                samples=[{'symbol':sym} for sym,ts in p.BINANCE_VERIFIED_15M_ANOMALIES]
                matched,issues,verified=p.validate_aggregation(
                    con,samples,120,96,include_verified=True)
                self.assertEqual(matched,0)
                self.assertEqual(issues,[])
                self.assertEqual(len(verified),16)
                self.assertEqual({(x['symbol'],x['open_ts']) for x in verified},
                                 set(p.BINANCE_VERIFIED_15M_ANOMALIES))
                self.assertTrue(all(x['verification_method']==p.BINANCE_ANOMALY_CHECK_METHOD
                                    for x in verified))

    def test_provider_exception_rejects_new_or_changed_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            with closing(p.connect(Path(td)/'db.sqlite')) as con:
                (sym,ts),proof=next(iter(p.BINANCE_VERIFIED_15M_ANOMALIES.items()))
                self._make_provider_anomaly(con,sym,ts,proof)
                _,issues,verified=p.validate_aggregation(con,[{'symbol':sym}],120,96,True)
                self.assertEqual((len(issues),len(verified)),(0,1))
                # Existing case becomes INVALID if the recorded 15m changes.
                con.execute('UPDATE candles SET high=high+0.0002 WHERE symbol=? AND timeframe=? AND open_ts=?',
                            (sym,'15m',ts))
                con.commit()
                _,issues,verified=p.validate_aggregation(con,[{'symbol':sym}],120,96,True)
                self.assertEqual((len(issues),len(verified)),(1,0))
                # Even if the price profile matches exactly, another date is NOT whitelisted.
                self.assertIsNone(p.exact_verified_provider_anomaly(
                    sym,ts+15*p.MIN,proof['one_min_aggregate'],proof['binance_15m']))
                self.assertIsNone(p.exact_verified_provider_anomaly(
                    'NOT'+sym,ts,proof['one_min_aggregate'],proof['binance_15m']))

    def test_provider_exception_does_not_allow_volume_or_trade_changes(self):
        (sym,ts),proof=next(iter(p.BINANCE_VERIFIED_15M_ANOMALIES.items()))
        agg=list(proof['one_min_aggregate']);expected=list(proof['binance_15m'])
        agg[5]+=2.0
        self.assertIsNone(p.exact_verified_provider_anomaly(sym,ts,agg,expected))
        agg=list(proof['one_min_aggregate']);agg[6]+=1
        self.assertIsNone(p.exact_verified_provider_anomaly(sym,ts,agg,expected))
        agg=list(proof['one_min_aggregate']);expected[0]+=0.0001
        self.assertIsNone(p.exact_verified_provider_anomaly(sym,ts,agg,expected))

    def test_full_offline_run_audit_export(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);base=root/'reports'/'reaction_lab';base.mkdir(parents=True)
            entry=1_790_000_000_000//(15*p.MIN)*(15*p.MIN)
            selected=[]
            with closing(sqlite3.connect(base/'auto_paths_v1.sqlite3')) as ap:
                ap.execute('CREATE TABLE events(event_key TEXT PRIMARY KEY,entry_ts INTEGER, retest_ts INTEGER,state TEXT,count INTEGER,path_blob BLOB)')
                for sym,kind in (('AAAUSDT','RUNNER_3'),('BBBUSDT','CONTROL_LT3')):
                    retest=entry+8*p.MIN;known=retest+p.MIN
                    key=f'{sym}|LONG|{retest}'
                    rows=[(known+i*p.MIN,100.,104. if kind=='RUNNER_3' and i==20 else 100.,100.,100.)
                          for i in range(360)]
                    blob=zlib.compress(b''.join(p.BAR.pack(*r) for r in rows))
                    ap.execute('INSERT INTO events VALUES (?,?,?,?,?,?)',(key,known,retest,'COMPLETE',360,blob))
                    selected.append({'event_key':key,'symbol':sym,'side':'LONG',
                         'retest_ts':retest,'known_ts':known,'first_seen_ms':known,
                         'observation_type':'LIVE_OBSERVED','episode_id':None,
                         'wave_minute':known//p.MIN,'entry_open':100.,'class_label':kind,
                         'mfe_180_pct':4.0 if kind=='RUNNER_3' else 0.0,'mae_180_pct':0.0,
                         'first_2m':21 if kind=='RUNNER_3' else None,
                         'first_3m':21 if kind=='RUNNER_3' else None,'first_5m':None})
                ap.commit()
            with closing(sqlite3.connect(base/'scanner_events_v1.sqlite3')) as sc:
                sc.execute('CREATE TABLE events(event_key TEXT PRIMARY KEY, symbol TEXT, side TEXT, retest_ts INTEGER, known_ts INTEGER, first_seen_ms INTEGER, observation_type TEXT, causal_json TEXT)')
                for event in selected:
                    sc.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?,?)',
                      (event['event_key'],event['symbol'],event['side'],event['retest_ts'],
                       event['known_ts'],event['first_seen_ms'],event['observation_type'],
                       json.dumps({'pivot_to_retest_min':75,'max_departure_pct':1.2})))
                sc.commit()
            candidates, excluded=p.source_candidates(root)
            self.assertEqual(len(candidates),2)
            self.assertEqual(len(excluded),0)
            self.assertEqual({x['class_label'] for x in candidates},{'RUNNER_3','CONTROL_LT3'})
            self.assertAlmostEqual(p.load_causal_swing_features(root, {selected[0]['event_key']})
                                   [selected[0]['event_key']]['known_swing_max_departure_pct'],1.2)
            # The plan must be read-only and must not create the pilot database.
            stdout=io.StringIO()
            with contextlib.redirect_stdout(stdout):
                p.main(['--plan','--project',str(root),'--cases','1','--controls','1'])
            self.assertIn('NO_FILES_OR_NETWORK_MODIFIED',stdout.getvalue())
            self.assertFalse((base/p.DB_NAME).exists())
            def fake_fetch(sym,tf,start,end):
                step=p.TFS[tf];out=[]
                for ts in range(start,end+1,step):
                    m=1 if tf=='1m' else 15
                    out.append((ts,100.,100.,100.,100.,10*m,1000*m,2*m,5*m,500*m))
                return out,'fake_sha'
            args=SimpleNamespace(pre1=120,pre15=96,post_audit=15,seed=7,
                     cases=1,controls=1,episodes_csv=None,min_free_gb=0,
                     max_requests=1000,max_failures=1,pause=0)
            db=base/p.DB_NAME
            r=p.run(root,db,args,selected,fake_fetch)
            self.assertGreater(r['new_bars'],100)
            summary=p.audit(root,db)
            self.assertTrue(summary['success'],str(summary))
            self.assertEqual(summary['auto_paths_parity']['compared'],30)
            self.assertGreater(summary['aggregate_match'],0)
            out=p.export(root,db,base/'runner_precursor_pilot')
            self.assertEqual(out['exported'],2)
            with (base/'runner_precursor_pilot'/'features_before_retest.csv').open() as fh:
                header=next(csv.reader(fh))
            self.assertNotIn('class_label',header)
            self.assertIn('asset_prer_1m_quote_usdt_15',header)
            self.assertIn('asset_prer_15m_move_24h_pct',header)
            self.assertNotIn('known_swing_max_departure_pct',header)
            with (base/'runner_precursor_pilot'/'features_at_known.csv').open() as fh:
                known_header=next(csv.reader(fh))
            self.assertIn('known_swing_max_departure_pct',known_header)
            self.assertEqual(p.run(root,db,args,selected,fake_fetch)['requests'],0)

    def test_create_files_limited_to_isolated_dir(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            with closing(p.connect(root/'pilot.sqlite')) as con:
                self.assertEqual(con.execute('SELECT COUNT(*) FROM samples').fetchone()[0],0)
            self.assertFalse((root/'reports'/'reaction_lab'/'scanner_events_v1.sqlite3').exists())


if __name__=='__main__':unittest.main()
