"""V3 continuity offline tests. No Binance/PM2/market data/network required."""
import json
import tempfile
import unittest
from pathlib import Path

from research.micro_flow_v3.continuous_recorder import RotatingTapeWriter
from research.micro_flow_v3.continuous_replay import replay_continuous, ReplayError
from research.micro_flow_v3.replay import replay_tape
from research.micro_flow_v3.tape import TapeStore, replay_digest

BASE_MS=1792500000000


def add(w,idx,offset_ms,*,gen=1,price='2500',qty='1',maker=False):
    now=BASE_MS+offset_ms
    payload=json.dumps({'e':'aggTrade','s':'ETHUSDT','a':idx,'T':now,'E':now,
                        'p':price,'q':qty,'m':maker})
    return w.ingest(payload,connection=gen,wall_ns=now*1_000_000,
                    mono_ns=(10_000+offset_ms)*1_000_000)


class ContinuousV3Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)

    def files(self):
        return sorted(self.root.glob('aggtrades_raw_*.sqlite3'))

    def replay(self,files,label='test',maxsilence=15):
        out=self.root/f'{label}.jsonl'
        summary=self.root/f'{label}.json'
        cov=self.root/f'{label}.coverage.jsonl'
        stats=replay_continuous(files,out,summary_path=summary,coverage_path=cov,
                                max_silence_seconds=maxsilence)
        return stats,[json.loads(x) for x in out.read_text().splitlines()],\
            [json.loads(x) for x in cov.read_text().splitlines()]

    def test_rotation_same_ws_ids_preserve_continuity_no_trades(self):
        w=RotatingTapeWriter(self.root,'ETHUSDT',rotate_seconds=3600)
        for i,offset in [(1,100),(2,1100),(3,2100)]:
            add(w,i,offset)
        w._file_open_monotonic -= 3601
        add(w,4,7100)
        add(w,5,8100)
        add(w,6,9100)
        w.marker('PROCESS_STOP')
        w.close()
        files=self.files()
        self.assertEqual(len(files),2)
        before=[replay_digest(p) for p in files]
        stats,rows,gaps=self.replay(files)
        self.assertEqual(stats['boundary_join_count'],1)
        self.assertEqual(stats['boundary_unverified_count'],0)
        self.assertEqual(stats['trades_seen'],6)
        self.assertEqual(stats['buckets_emitted'],4)
        self.assertEqual([r['first_agg_id'] for r in rows],[2,3,4,5])
        self.assertEqual(stats['inferred_no_trade_seconds'],4)
        self.assertEqual(len(gaps),1)
        self.assertEqual(gaps[0]['seconds'],4)
        self.assertEqual(gaps[0]['verified_only_after_wall_ns'],(BASE_MS+7100)*1_000_000)
        self.assertEqual(before,[replay_digest(p) for p in files])
        self.assertEqual(len({r['segment_id'] for r in rows}),1)
        self.assertTrue(all(r['trades']==1 for r in rows))

    def test_corrupt_metadata_prevents_bridge_without_mutating(self):
        w=RotatingTapeWriter(self.root,'ETHUSDT',rotate_seconds=3600)
        for i in range(1,4):add(w,i,(i-1)*1000+100)
        w._file_open_monotonic-=3601
        for i in range(4,7):add(w,i,(i-1)*1000+100)
        w.marker('PROCESS_STOP');w.close()
        files=self.files()
        import sqlite3
        with sqlite3.connect(files[1]) as db:
            db.execute("UPDATE meta SET value='tampered' WHERE key='continuous_capture_id'")
        stats,rows,_=self.replay(files)
        self.assertEqual(stats['boundary_join_count'],0)
        self.assertEqual(len(stats['segments']),2)
        self.assertNotEqual(rows[0]['segment_id'],rows[-1]['segment_id'])

    def test_id_jump_at_rotation_breaks_segment_and_has_no_fake_fill(self):
        w=RotatingTapeWriter(self.root,'ETHUSDT',rotate_seconds=3600)
        for i in range(1,4):add(w,i,(i-1)*1000+100)
        w._file_open_monotonic-=3601
        for i in range(8,11):add(w,i,(i-3)*1000+100)
        w.marker('PROCESS_STOP');w.close()
        stats,rows,gaps=self.replay(self.files())
        self.assertEqual(stats['boundary_join_count'],0)
        self.assertEqual(gaps,[])
        self.assertEqual(len({r['segment_id'] for r in rows}),2)

    def test_long_silence_does_not_count_as_no_trade_interval(self):
        w=RotatingTapeWriter(self.root,'ETHUSDT')
        for i,offset in [(1,100),(2,1100),(3,2100),(4,33100),(5,34100),(6,35100)]:
            add(w,i,offset)
        w.marker('PROCESS_STOP');w.close()
        stats,rows,gaps=self.replay(self.files())
        self.assertEqual(stats['long_silence_boundaries'],1)
        self.assertEqual(stats['inferred_no_trade_seconds'],0)
        self.assertEqual(gaps,[])
        self.assertEqual(len({r['segment_id'] for r in rows}),2)

    def test_legacy_tapes_with_restarts_are_not_stitched(self):
        for n in range(2):
            db=self.root/f'legacy_{n}.sqlite3'
            s=TapeStore(db)
            s.marker('PROCESS_START_UNVERIFIED_PRIOR_HISTORY')
            for i in range(4):add(s,n*4+i+1,n*5000+i*1000+100,gen=1)
            s.marker('PROCESS_STOP');s.close()
        stats,rows,_=self.replay([self.root/'legacy_0.sqlite3',self.root/'legacy_1.sqlite3'])
        self.assertEqual(stats['boundary_join_count'],0)
        self.assertEqual(len({r['segment_id'] for r in rows}),2)

    def test_single_tape_bucket_parity_with_v1_when_no_long_silence(self):
        tape=self.root/'single.sqlite3';w=TapeStore(tape)
        for i in range(1,9):add(w,i,100+(i-1)*1000,maker=i%2==0)
        w.marker('PROCESS_STOP');w.close()
        original=self.root/'old.jsonl';old_summary=replay_tape(tape,original)
        new_summary,_,_=self.replay([tape])
        self.assertEqual(new_summary['replay_jsonl_sha256'],old_summary['replay_jsonl_sha256'])

    def test_reject_active_source_and_no_overwrite(self):
        tape=self.root/'unfinished.sqlite3';w=TapeStore(tape)
        add(w,1,100);w.close()
        with self.assertRaisesRegex(ReplayError,'not closed'):
            self.replay([tape])
        w2=TapeStore(self.root/'closed.sqlite3')
        for i in range(4):add(w2,i+1,i*1000+100)
        w2.marker('PROCESS_STOP');w2.close()
        self.replay([w2.path],label='done')
        with self.assertRaisesRegex(ReplayError,'exists'):
            self.replay([w2.path],label='done')


if __name__=='__main__':unittest.main()

# Fully offline transport integration (one fake WS, multiple SQLite files).
class ContinuousTransportTests(unittest.TestCase):
    def test_single_socket_not_reconnected_during_internal_rotation(self):
        import asyncio
        import time
        from research.micro_flow_v3.continuous_recorder import run_continuous
        from research.micro_flow_v3.tape import replay_digest

        class Socket:
            def __init__(self):self.i=0
            async def recv(self):
                await asyncio.sleep(.015)
                self.i+=1
                now=int(time.time()*1000)
                return json.dumps({'e':'aggTrade','s':'ETHUSDT','a':self.i,
                                   'T':now,'E':now,'p':'2500','q':'1','m':False})
        class Connection:
            async def __aenter__(self):return Socket()
            async def __aexit__(self,*args):return False
        class Factory:
            calls=0
            def __call__(self,*args,**kwargs):
                self.calls+=1
                return Connection()
        factory=Factory()
        with tempfile.TemporaryDirectory() as td:
            # Rotation every 80ms, capture 320ms.
            result=run_continuous('ETHUSDT',td,rotate_hours=0.080/3600,
                                  duration_hours=0.32/3600,min_free_gb=0,
                                  first_trade_timeout=.2,connect_factory=factory)
            tapes=sorted(Path(td).glob('aggtrades_raw_*.sqlite3'))
            self.assertEqual(factory.calls,1)
            self.assertGreaterEqual(len(tapes),2)
            self.assertEqual(result['tapes'],len(tapes))
            self.assertEqual(result['trades'],sum(1 for p in tapes
                             for r in __import__('sqlite3').connect(p).execute(
                                 "SELECT seq FROM tape WHERE kind='TRADE'")))
            self.assertTrue(all(replay_digest(p)[0]>1 for p in tapes))
