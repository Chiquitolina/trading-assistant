"""Causal and end-to-end tests for conservative Movement Episodes."""
import json
from pathlib import Path
import sqlite3
import struct
import tempfile
import unittest
import zlib

from reaction_lab_movement_episodes import (
    Policy, Reaction, advance_episode, build_episodes, group_reactions, ready_at,
)

MIN = 60000
BAR = struct.Struct("<qdddd")
BASE = 1_800_000_000_000  # minute-aligned epoch-ms
BASE -= BASE % MIN


def candles(retest, *, direction="flat", fall_at=None, peak_at=None):
    rows=[]
    for i in range(360):
        close=100.0
        if fall_at is not None and i >= fall_at:
            close=97.0
        if peak_at is not None and i >= peak_at:
            close=103.0
        lo=min(100,close)-.05
        hi=max(100,close)+.05
        rows.append((retest+(i+1)*MIN,100.,hi,lo,close))
    return rows


def reaction(symbol,side,retest,n,*,known=None,origin="LIVE_OBSERVED",bars=None,first_seen=None):
    key=f"{symbol}|{side}|{retest}"
    r=Reaction(key,symbol,side,retest,
               known if known is not None else retest+MIN,
               first_seen if first_seen is not None else retest+MIN,
               origin,100.,"COMPLETE" if bars else "MISSING",
               360 if bars else 0,retest+MIN,None,
               bars=bars,path_quality="COMPLETE_VALID" if bars else "NO_COMPLETE_PATH")
    r.ready_ms,r.ready_quality=ready_at(r.known_ts,r.first_seen_ms,r.observation_type,r.retest_ts)
    return r


class GroupingTests(unittest.TestCase):
    def test_same_symbol_side_merges_with_closed_continuity(self):
        first=reaction("AAA","LONG",BASE,0,bars=candles(BASE))
        second=reaction("AAA","LONG",BASE+15*MIN,1,bars=candles(BASE+15*MIN))
        events,episodes=group_reactions([first,second])
        self.assertEqual(len(episodes),1)
        self.assertEqual(episodes[0]["reactions"],2)
        self.assertEqual(events[1]["decision"],"JOIN_CAUSAL_CONTINUATION")

    def test_opposite_side_breaks(self):
        first=reaction("AAA","LONG",BASE,0,bars=candles(BASE))
        other=reaction("AAA","SHORT",BASE+15*MIN,1,bars=candles(BASE+15*MIN))
        events,episodes=group_reactions([first,other])
        self.assertEqual(len(episodes),2)
        self.assertEqual(events[1]["decision"],"OPPOSITE_REACTION")

    def test_other_symbol_is_independent(self):
        items=[reaction("AAA","LONG",BASE,0,bars=candles(BASE)),
               reaction("BBB","LONG",BASE+MIN,1,bars=candles(BASE+MIN))]
        _, episodes=group_reactions(items)
        self.assertEqual(len(episodes),2)

    def test_time_gap_splits_even_if_same_direction(self):
        items=[reaction("AAA","LONG",BASE,0,bars=candles(BASE)),
               reaction("AAA","LONG",BASE+100*MIN,1,bars=candles(BASE+100*MIN))]
        events, episodes=group_reactions(items)
        self.assertEqual(len(episodes),2)
        self.assertEqual(events[1]["decision"],"GAP_BETWEEN_REACTIONS")

    def test_no_future_wick_or_close_may_split_early_reaction(self):
        first=reaction("AAA","LONG",BASE,0,bars=candles(BASE,fall_at=24))
        near=reaction("AAA","LONG",BASE+10*MIN,1,bars=candles(BASE+10*MIN))
        after=reaction("AAA","LONG",BASE+31*MIN,2,bars=candles(BASE+31*MIN))
        events,episodes=group_reactions([first,near,after])
        self.assertEqual([x["position_in_episode"] for x in events],[1,2,1])
        self.assertEqual(events[2]["decision"],"ANCHOR_INVALIDATED")
        self.assertEqual(len(episodes),2)

    def test_giveback_detected_only_after_closed_bar(self):
        bars=candles(BASE,peak_at=9)
        # Manually inject a close retracement after positive peak.
        for i in range(20,len(bars)):
            ts=bars[i][0]
            bars[i]=(ts,100.,100.05,99.95,100.)
        first=reaction("AAA","LONG",BASE,0,bars=bars)
        before=reaction("AAA","LONG",BASE+15*MIN,1,bars=candles(BASE+15*MIN))
        after=reaction("AAA","LONG",BASE+30*MIN,2,bars=candles(BASE+30*MIN))
        events,episodes=group_reactions([first,before,after])
        self.assertEqual(len(episodes),2)
        self.assertEqual(events[2]["decision"],"PEAK_GIVEBACK")

    def test_incomplete_seed_is_not_assumed_continuation(self):
        a=reaction("AAA","LONG",BASE,0)
        b=reaction("AAA","LONG",BASE+15*MIN,1,bars=candles(BASE+15*MIN))
        events,episodes=group_reactions([a,b])
        self.assertEqual(len(episodes),2)
        self.assertEqual(events[1]["decision"],"NO_SEED_PATH")

    def test_late_observation_time_controls_grouping(self):
        a=reaction("AAA","LONG",BASE,0,bars=candles(BASE))
        b=reaction("AAA","LONG",BASE+20*MIN,1,
                   first_seen=BASE+120*MIN,bars=candles(BASE+20*MIN))
        events,episodes=group_reactions([a,b])
        self.assertEqual(len(episodes),2)
        self.assertEqual(events[1]["decision"],"GAP_BETWEEN_REACTIONS")

    def test_provenance_transition_splits(self):
        a=reaction("AAA","LONG",BASE,0,bars=candles(BASE),origin="RECOVERED_WINDOW")
        b=reaction("AAA","LONG",BASE+15*MIN,1,bars=candles(BASE+15*MIN))
        rows,eps=group_reactions([a,b])
        self.assertEqual(len(eps),2)
        self.assertEqual(rows[1]["decision"],"ORIGIN_TRANSITION")

    def test_historical_recovered_is_not_live(self):
        ready,quality=ready_at(BASE+MIN,BASE+100*MIN,"RECOVERED_WINDOW",BASE)
        self.assertEqual(ready,BASE+MIN)
        self.assertEqual(quality,"RECOVERED_ASOF_ONLY")

    def test_invalid_observation_time_keeps_singleton(self):
        a=reaction("AAA","LONG",BASE,0,bars=candles(BASE))
        b=reaction("AAA","LONG",BASE+15*MIN,1,bars=candles(BASE+15*MIN))
        b.first_seen_ms=None
        b.ready_ms,b.ready_quality=ready_at(b.known_ts,None,b.observation_type,b.retest_ts)
        events,episodes=group_reactions([a,b])
        self.assertEqual(len(episodes),2)
        self.assertEqual(events[1]["decision"],"UNKNOWN_FIRST_SEEN")

    def test_policy_validation(self):
        with self.assertRaises(ValueError):
            Policy(max_gap_min=400)


class PersistenceTests(unittest.TestCase):
    def test_readonly_source_and_csv_output_and_stable_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            base=root/"reports"/"reaction_lab"
            base.mkdir(parents=True)
            scan=base/"scanner_events_v1.sqlite3"
            paths=base/"auto_paths_v1.sqlite3"
            with sqlite3.connect(scan) as conn:
                conn.execute("""CREATE TABLE events(event_key TEXT PRIMARY KEY, symbol TEXT,
                  side TEXT,retest_ts INTEGER,known_ts INTEGER,first_seen_ms INTEGER,
                  observation_type TEXT,causal_json TEXT)""")
                for i in (0,15):
                    stamp=BASE+i*MIN
                    conn.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?)",(
                        f"AAA|LONG|{stamp}","AAA","LONG",stamp,stamp+MIN,
                        stamp+MIN,"LIVE_OBSERVED",json.dumps({"retest_close":100.0})))
            conn.close()
            with sqlite3.connect(paths) as conn:
                conn.execute("""CREATE TABLE events(event_key TEXT PRIMARY KEY,entry_ts INTEGER,
                  state TEXT,count INTEGER,path_blob BLOB)""")
                for i in (0,15):
                    stamp=BASE+i*MIN
                    raw=b"".join(BAR.pack(*row) for row in candles(stamp))
                    conn.execute("INSERT INTO events VALUES(?,?,?,?,?)",(
                        f"AAA|LONG|{stamp}",stamp+MIN,"COMPLETE",360,zlib.compress(raw)))
            conn.close()
            first=build_episodes(root)
            self.assertEqual(first["counts"]["reactions"],2)
            self.assertEqual(first["counts"]["episodes"],1)
            self.assertEqual(first["counts"]["multi_reaction_episodes"],1)
            out=base/"movement_episodes_v1"/"events.csv"
            self.assertTrue(out.exists())
            original=out.read_bytes()
            build_episodes(root)
            self.assertEqual(original,out.read_bytes())
            with sqlite3.connect(scan) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],2)
            conn.close()


if __name__=="__main__":
    unittest.main()
