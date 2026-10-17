"""Offline policy replay contract tests: no Binance, no network, no Streamlit."""
from __future__ import annotations

import csv
import math
import tempfile
import unittest
from pathlib import Path

from dashboard.services import reaction_lab_portfolio as frozen
from dashboard.services import reaction_lab_portfolio_policy_fork as fork
from dashboard.services.reaction_lab_policy_replay import (
    load_snapshots, policy_rank_key, run_policy_set, sha256,
)

MINUTE=60000
BASE_TS=1800000000000


def record(sym, room, return_4h, *, trend, side="LONG"):
    bars=[]
    for n in range(240):
        ts=BASE_TS+n*MINUTE
        if n==0:
            op=hi=lo=cl=100.0
        elif trend=="gain":
            op=100.0; hi=103.0; lo=99.9; cl=102.4
        else:
            op=100.0; hi=100.1; lo=97.0; cl=97.5
        bars.append((ts,op,hi,lo,cl))
    return {"key":f"{sym}|{side}|{BASE_TS-MINUTE}","symbol":sym,"side":side,
            "retest_ts":BASE_TS-MINUTE,"first_seen_ms":BASE_TS,
            "ready_ms":BASE_TS+1,"values":(2.0,room,80.,return_4h),
            "reference":100.0,"bars":tuple(bars),
            "context_status":"HISTORICAL_ASOF_DERIVED",
            "regime_status":"HISTORICAL_ASOF_DERIVED",
            "capture_method":"HISTORICAL_RECOMPUTED_ASOF_NOT_LIVE"}


def row(r, proxy, *, status="CLOSED_4H_RELATIVE_RETURN_PROXY_NOT_CANONICAL_STRENGTH"):
    return {"event_key":r["key"],"symbol":r["symbol"],"side":r["side"],
            "ready_ms":r["ready_ms"],"room_1h_pct":r["values"][1],
            "departure_pct":r["values"][0],"return_rank_pct_4h":r["values"][3],
            "volume_rank_pct_4h":r["values"][2],
            "directional_return_rank_4h":r["values"][3] if r["side"]=="LONG" else 100-r["values"][3],
            "room_asof_ms":BASE_TS-2*MINUTE,"room_available_ms":BASE_TS-MINUTE,
            "ranks_asof_ms":BASE_TS-MINUTE,"ranks_available_ms":BASE_TS-MINUTE,
            "symbol_return_closed_4h_pct":proxy,"btc_return_closed_4h_pct":0,
            "directional_strength_vs_btc_4h_proxy_pct":proxy if r["side"]=="LONG" else -proxy,
            "relative_volume_4h":2.0,"feature_snapshot_close_ms":BASE_TS-MINUTE,
            "strength_status":status}


class PortfolioPolicyReplayTests(unittest.TestCase):
    def setUp(self):
        self.a=record("AAAUSDT",5.0,70.0,trend="loss")
        self.b=record("BBBUSDT",2.0,90.0,trend="gain")
        self.records=[self.a,self.b]
        self.rows=[row(self.a,0.4),row(self.b,2.5)]
        self.snaps={r["event_key"]:r for r in self.rows}

    def test_unmodified_fork_is_exactly_original(self):
        params=frozen.PROFILES["Runner Challenger 220"]
        orig=frozen.simulate_strategy(self.records,"Runner Challenger 220",params)
        copy=fork.simulate_strategy(self.records,"Runner Challenger 220",params)
        self.assertEqual(orig,copy)
        self.assertEqual(orig["trades"][0]["status"],"SL")
        self.assertEqual(len(orig["trades"]),1)

    def test_only_tie_order_changed_and_full_slot_resimulated(self):
        params=frozen.PROFILES["Runner Challenger 220"]
        selector, groups=policy_rank_key(self.records,self.snaps,params,False,"DIRECTIONAL_STRENGTH_VS_BTC_4H_PROXY")
        self.assertEqual(groups,1)
        alt=fork.simulate_strategy(self.records,"Runner Challenger 220",params,selection_sort_key=selector)
        self.assertEqual(alt["trades"][0]["symbol"],"BBBUSDT")
        self.assertEqual(alt["trades"][0]["status"],"TP")
        self.assertEqual(alt["summary"]["orders_placed"],1)
        original=frozen.simulate_strategy(self.records,"Runner Challenger 220",params)
        self.assertGreater(alt["summary"]["equity_final_realized_usdt"],original["summary"]["equity_final_realized_usdt"])
        self.assertEqual([d["decision"] for d in original["decisions"]].count("SKIPPED_SLOT_BUSY"),1)
        self.assertEqual([d["decision"] for d in alt["decisions"]].count("SKIPPED_SLOT_BUSY"),1)

    def test_room_first_never_uses_strength_when_room_unequal(self):
        selector,_=policy_rank_key(self.records,self.snaps,frozen.PROFILES["Runner Challenger 220"],False,"ROOM_THEN_STRENGTH")
        alt=fork.simulate_strategy(self.records,"Runner Challenger 220",frozen.PROFILES["Runner Challenger 220"],selection_sort_key=selector)
        orig=frozen.simulate_strategy(self.records,"Runner Challenger 220",frozen.PROFILES["Runner Challenger 220"])
        self.assertEqual(alt["summary"],orig["summary"])

    def test_missing_strength_rejected_not_imputed(self):
        self.snaps[self.b["key"]]["strength_status"]="MISSING_CLOSED_4H_SYMBOL_OR_BTC"
        with self.assertRaisesRegex(ValueError,"Unavailable closed-4h strength"):
            policy_rank_key(self.records,self.snaps,frozen.PROFILES["Runner Challenger 220"],False,"STRENGTH_THEN_ROOM")

    def test_guard_veto_not_in_eligible_rank(self):
        self.a["side"]="SHORT"; self.b["side"]="SHORT"
        self.a["key"]=f"AAAUSDT|SHORT|{BASE_TS-MINUTE}"
        self.b["key"]=f"BBBUSDT|SHORT|{BASE_TS-MINUTE}"
        self.a["values"]=(2.,5.,99.,97.)  # vetoes A by 95/90
        self.b["values"]=(2.,2.,80.,80.)
        self.snaps={r["key"]:row(r, 1.) for r in self.records}
        selector, groups=policy_rank_key(self.records,self.snaps,frozen.PROFILES["Runner Challenger 220"],True,"BASELINE_ROOM")
        self.assertEqual(groups,0)
        alt=fork.simulate_strategy(self.records,frozen.EXPERIMENTAL_NAME,
                                   frozen.PROFILES["Runner Challenger 220"],
                                   experimental_guard=True,selection_sort_key=selector)
        self.assertIn(frozen.EXPERIMENTAL_REJECT_DECISION,[x["decision"] for x in alt["decisions"]])
        self.assertEqual(alt["summary"]["orders_placed"],1)

    def test_saved_baseline_reference_parity(self):
        with tempfile.TemporaryDirectory() as temp:
            out=Path(temp)/"frozen"
            comp=frozen.run_comparison(self.records,include_extreme_strength_guard=True)
            frozen.export_comparison(comp,self.records,out,
                 cohort="HISTORICAL_RESEARCH_UNVERIFIED_OR_PARTIAL")
            results, verified, divergences=run_policy_set(self.records,self.snaps,
                 reference_dir=out, strategies=["Runner Challenger 220",frozen.EXPERIMENTAL_NAME],
                 policies=["BASELINE_ROOM","DIRECTIONAL_STRENGTH_VS_BTC_4H_PROXY"])
            self.assertEqual(verified["Runner Challenger 220"]["verified_trades"],1)
            self.assertTrue(divergences)
            self.assertEqual(results[("Runner Challenger 220","BASELINE_ROOM")]["summary"],
                             comp["Runner Challenger 220"]["summary"])

    def test_reference_equity_tampering_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            out=Path(temp)/"frozen"
            comp=frozen.run_comparison(self.records,include_extreme_strength_guard=True)
            frozen.export_comparison(comp,self.records,out,
                 cohort="HISTORICAL_RESEARCH_UNVERIFIED_OR_PARTIAL")
            path=out/"equity.csv"
            with path.open(newline="") as f:
                rd=csv.DictReader(f)
                header=rd.fieldnames
                rows=list(rd)
            row=next(r for r in rows if r["strategy"]=="Runner Challenger 220")
            row["marked_equity_usdt"]=str(float(row["marked_equity_usdt"])+10)
            with path.open("w",newline="") as f:
                w=csv.DictWriter(f,fieldnames=header);w.writeheader();w.writerows(rows)
            with self.assertRaisesRegex(ValueError,"Baseline equity curve mismatch"):
                run_policy_set(self.records,self.snaps,reference_dir=out,
                      strategies=["Runner Challenger 220"], policies=["BASELINE_ROOM"])

    def test_snapshot_future_and_wrong_factors_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/"snap.csv"
            with p.open("w",newline="") as f:
                w=csv.DictWriter(f,fieldnames=list(self.rows[0]));w.writeheader();w.writerows(self.rows)
            self.assertEqual(len(load_snapshots(p,self.records)),2)
            data=[dict(r) for r in self.rows]
            data[0]["room_available_ms"]=str(BASE_TS+MINUTE)
            with p.open("w",newline="") as f:
                w=csv.DictWriter(f,fieldnames=list(data[0]));w.writeheader();w.writerows(data)
            with self.assertRaisesRegex(ValueError,"Future/missing"):
                load_snapshots(p,self.records)

if __name__=="__main__":
    unittest.main()
