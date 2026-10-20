"""Isolated deterministic tests for Runner Discovery V2: no PM2/REST/live I/O."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from reaction_lab_auto_paths import encode_path
from reaction_lab_runner_discovery_v2 import (
    build_dataset, first_touch, validate_path, first_bar_observable,
    outcome_at_horizon, generate_rows, first_favorable_hits,
)

M = 60_000
BASE = 1_800_000_000_000


def bars(*, high_tp=None, low_sl=None, first_price=100, count=360):
    rows=[]
    for i in range(count):
        high=first_price+0.2
        low=first_price-0.2
        if i==high_tp:high=103.1
        if i==low_sl:low=98.9
        rows.append((BASE+(i+1)*M,first_price,high,low,first_price))
    return rows


class EngineTests(unittest.TestCase):
    def test_first_touch_order_and_ambiguity(self):
        self.assertEqual(first_touch(bars(high_tp=4,low_sl=8),side="LONG",price=100,target_pct=2),("TP_FIRST",5))
        self.assertEqual(first_touch(bars(high_tp=8,low_sl=4),side="LONG",price=100,target_pct=2),("SL_FIRST",5))
        self.assertEqual(first_touch(bars(high_tp=4,low_sl=4),side="LONG",price=100,target_pct=2),("AMBIGUOUS",5))
        self.assertEqual(first_touch(bars(),side="LONG",price=100,target_pct=2),("NO_HIT",None))
        sh=bars();sh[0]=(sh[0][0],100,101.3,98.0,100)
        self.assertEqual(first_touch(sh,side="SHORT",price=100,target_pct=2),("AMBIGUOUS",1))

    def test_quality_and_horizon(self):
        data=bars()
        self.assertEqual(validate_path(data,retest_ts=BASE,entry_ts=BASE+M,count=360),"COMPLETE_VALID")
        broken=data.copy();broken[40]=(broken[40][0]+M,*broken[40][1:])
        self.assertEqual(validate_path(broken,retest_ts=BASE,entry_ts=BASE+M,count=360),"GAPPED_OR_SHIFTED")
        self.assertEqual(outcome_at_horizon("TP_FIRST",60,30,360),"NO_HIT")
        self.assertEqual(outcome_at_horizon("SL_FIRST",21,30,360),"SL_FIRST")
        self.assertEqual(outcome_at_horizon("NO_HIT",None,180,120),"INSUFFICIENT")

    def test_first_observed_open_vs_hindsight(self):
        data=bars()
        self.assertEqual(first_bar_observable(data,known_ts=BASE+M,first_seen_ms=BASE+M+1000,
             observation_type="LIVE_OBSERVED"),(1,"OBSERVED_PROXY"))
        self.assertEqual(first_bar_observable(data,known_ts=BASE+M,first_seen_ms=BASE+M,
             observation_type="RECOVERED_WINDOW"),(None,"RECOVERED_NOT_LIVE"))

    def test_sqlite_full_build_includes_controls_invalid_and_recovered(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp)
            target=root/"reports/reaction_lab"
            target.mkdir(parents=True)
            with sqlite3.connect(target/"scanner_events_v1.sqlite3") as c:
                c.execute("""CREATE TABLE events(event_key TEXT PRIMARY KEY,symbol TEXT,side TEXT,
                   retest_ts INTEGER,known_ts INTEGER,first_seen_ms INTEGER,observation_type TEXT,causal_json TEXT)""")
                for symbol, origin in (("A","LIVE_OBSERVED"),("B","RECOVERED_WINDOW"),("C","LIVE_OBSERVED"),("D","LIVE_OBSERVED")):
                    c.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?)",(
                       f"{symbol}|LONG|{BASE}",symbol,"LONG",BASE,BASE+M,
                       BASE+M+1000,origin,json.dumps({"retest_close":100,"max_departure_pct":1.4})))
                c.commit()
            with sqlite3.connect(target/"auto_paths_v1.sqlite3") as c:
                c.execute("""CREATE TABLE events(event_key TEXT PRIMARY KEY,entry_ts INTEGER,
                   state TEXT,count INTEGER,path_blob BLOB)""")
                c.execute("INSERT INTO events VALUES (?,?,?,?,?)",(f"A|LONG|{BASE}", BASE+M,"COMPLETE",360,encode_path(bars(high_tp=9))))
                c.execute("INSERT INTO events VALUES (?,?,?,?,?)",(f"B|LONG|{BASE}", BASE+M,"COMPLETE",360,encode_path(bars(low_sl=4))))
                c.execute("INSERT INTO events VALUES (?,?,?,?,?)",(f"C|LONG|{BASE}", BASE+M,"ACTIVE",30,encode_path(bars(count=30))))
                c.execute("INSERT INTO events VALUES (?,?,?,?,?)",(f"D|LONG|{BASE}", BASE+M,"COMPLETE",360,b"corrupt-zlib"))
                c.commit()
            rows=list(generate_rows(root))
            self.assertEqual(len(rows),4)
            a=next(r for r in rows if r["symbol"]=="A")
            b=next(r for r in rows if r["symbol"]=="B")
            c=next(r for r in rows if r["symbol"]=="C")
            self.assertEqual(a["quality"],"COMPLETE_VALID")
            self.assertEqual(a["retest_close"],100.0)
            self.assertEqual(a["struct_tp2_result"],"TP_FIRST")
            self.assertEqual(a["struct_tp2_min"],10)
            self.assertEqual(a["obs_tp2_min"],9)  # observation AFTER first bar
            self.assertEqual(a["obs_time_to_run_2pct"],9)
            self.assertIsNone(b["obs_time_to_run_2pct"])
            self.assertIsNone(c["obs_time_to_run_2pct"])
            self.assertEqual(b["observed_quality"],"RECOVERED_NOT_LIVE")
            self.assertIsNone(b["obs_tp2_result"])
            self.assertEqual(c["quality"],"NO_COMPLETE_PATH")
            d=next(r for r in rows if r["symbol"]=="D")
            self.assertEqual(d["quality"],"DECODE_OR_DATA_ERROR")
            manifest=build_dataset(root)
            self.assertEqual(manifest["counts"]["scanner_events"],4)
            self.assertEqual(manifest["counts"]["path_COMPLETE_VALID"],2)
            self.assertTrue((target/"runner_discovery_v2/runner_discovery_v2_events.csv").exists())


if __name__ == "__main__":unittest.main()
