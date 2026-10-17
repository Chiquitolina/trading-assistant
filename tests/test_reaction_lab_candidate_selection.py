"""Tests for Research-only Candidate Factor Snapshot + Selector, no network/Streamlit."""
import copy
import unittest
from dashboard.services.reaction_lab_candidate_selection import (
    build_factor_snapshots, build_selection, POLICIES,
)


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.t = 1800000000000
        self.closed = self.t - 1000
        self.ready = self.t + 1
        self.factors = []
        self.decisions = []
        self.shadows = []
        for sym, room, ret, vol, status, pnl in (
            ("AAAUSDT", 4.0, 80.0, 72.0, "SL", -2.0),
            ("BBBUSDT", 2.0, 45.0, 93.0, "TP", 1.9),
        ):
            key=f"{sym}|LONG|{self.t-60000}"
            self.factors.append({
                "event_key": key, "symbol": sym, "side": "LONG",
                "room_1h_pct": str(room), "return_rank_pct_4h": str(ret),
                "volume_rank_pct_4h": str(vol), "room_asof_ms": str(self.t - 3000),
                "room_available_ms": str(self.t - 1000),
                "ranks_asof_ms": str(self.closed), "ranks_available_ms": str(self.closed),
            })
            d={"strategy":"STRAT", "event_key":key, "symbol":sym, "side":"LONG",
                "ready_ms":str(self.ready), "room_1h":str(room), "departure":"1.6",
                "return_rank":str(ret), "volume_rank":str(vol),
                "group_id":f"STRAT|{self.ready}",
                "decision":"ORDER_SUBMITTED" if sym=="AAAUSDT" else "SKIPPED_SLOT_BUSY"}
            self.decisions.append(d)
            self.shadows.append({"strategy":"STRAT", "event_key":key,
                                 "actual_decision":d["decision"],
                                 "shadow_class":"SELECTED_PARITY_CHECKED" if sym=="AAAUSDT" else "ELIGIBLE_BLOCKED_SLOT",
                                 "shadow_status":status,"shadow_net_pct":str(pnl),
                                 "shadow_fill_ts":str(self.ready+60000)})
        self.groups=[{"strategy":"STRAT","group_id":f"STRAT|{self.ready}",
                      "ready_ms":str(self.ready), "eligible_count":"2",
                      "competition_type":"SIMULTANEOUS_FREE_SLOT",
                      "selected_event_key":self.decisions[0]["event_key"]}]
        self.market={
            (self.closed,"AAAUSDT"):{"return_pct_4h":"1.2","relative_volume_4h":"1.3","market_breadth_4h":"54"},
            (self.closed,"BBBUSDT"):{"return_pct_4h":"3.5","relative_volume_4h":"2.1","market_breadth_4h":"54"},
            (self.closed,"BTCUSDT"):{"return_pct_4h":"1.0","relative_volume_4h":"1","market_breadth_4h":"54"},
        }

    def run_selection(self):
        return build_selection(self.factors,self.decisions,self.groups,self.shadows,self.market)

    def test_timestamp_aware_strength_and_outcome_independent_choice(self):
        features,_,groups,choices,parity=self.run_selection()
        self.assertEqual(parity,1)
        self.assertEqual(len(features),2)
        self.assertEqual(float(features[0]["directional_strength_vs_btc_4h_proxy_pct"]),0.2)
        self.assertEqual(float(features[1]["directional_strength_vs_btc_4h_proxy_pct"]),2.5)
        b={r["policy"]:r for r in choices}
        self.assertEqual(b["BASELINE_ROOM"]["choice_event_key"],self.decisions[0]["event_key"])
        self.assertEqual(b["DIRECTIONAL_STRENGTH_VS_BTC_4H_PROXY"]["choice_event_key"],self.decisions[1]["event_key"])
        self.assertEqual(b["STRENGTH_THEN_ROOM"]["choice_event_key"],self.decisions[1]["event_key"])
        self.assertEqual(b["ROOM_THEN_STRENGTH"]["choice_event_key"],self.decisions[0]["event_key"])
        self.assertEqual(b["DIRECTIONAL_STRENGTH_VS_BTC_4H_PROXY"]["isolated_delta_net_pct_both_filled"],3.9)
        self.shadows[0]["shadow_net_pct"]="5000"  # fake future manipulation
        choices2=self.run_selection()[3]
        self.assertEqual([r["choice_event_key"] for r in choices],
                         [r["choice_event_key"] for r in choices2])

    def test_missing_btc_skips_policies_not_impute(self):
        self.market.pop((self.closed,"BTCUSDT"))
        snap,_,_,choices,_=self.run_selection()
        self.assertEqual({r["strength_status"] for r in snap},{"MISSING_CLOSED_4H_SYMBOL_OR_BTC"})
        ch={r["policy"]:r for r in choices}
        self.assertEqual(ch["DIRECTIONAL_STRENGTH_VS_BTC_4H_PROXY"]["decision_status"],
                         "SKIPPED_MISSING_CAUSAL_FEATURE")
        self.assertEqual(ch["BASELINE_ROOM"]["changed_from_baseline"],0)

    def test_future_factor_rejected(self):
        self.factors[0]["ranks_available_ms"]=str(self.ready+60_000)
        with self.assertRaisesRegex(ValueError,"Lookahead"):
            self.run_selection()

    def test_mismatch_factor_vs_decision_rejected(self):
        self.factors[0]["room_1h_pct"]="9"
        with self.assertRaisesRegex(ValueError,"Room mismatch"):
            self.run_selection()

    def test_wrong_baseline_room_rejected(self):
        self.groups[0]["selected_event_key"]=self.decisions[1]["event_key"]
        self.decisions[0]["decision"]="SKIPPED_SLOT_BUSY"
        self.decisions[1]["decision"]="ORDER_SUBMITTED"
        self.shadows[0]["actual_decision"]="SKIPPED_SLOT_BUSY"
        self.shadows[1]["actual_decision"]="ORDER_SUBMITTED"
        with self.assertRaisesRegex(ValueError,"BASELINE_ROOM parity"):
            self.run_selection()

    def test_busy_group_never_ranked_as_free(self):
        self.groups[0]["competition_type"]="SIMULTANEOUS_PREOCCUPIED"
        self.groups[0]["selected_event_key"]=""
        self.decisions[0]["decision"]="SKIPPED_SLOT_BUSY"
        self.shadows[0]["actual_decision"]="SKIPPED_SLOT_BUSY"
        _,_,groups,choices,parity=self.run_selection()
        self.assertEqual(len(groups),1)
        self.assertEqual(len(choices),0)
        self.assertEqual(parity,0)

    def test_short_strength_is_direction_adjusted(self):
        for d in self.decisions:d["side"]="SHORT"
        for f in self.factors:f["side"]="SHORT"
        features=build_factor_snapshots(self.factors,self.decisions,self.market)
        self.assertEqual(float(features[0]["directional_strength_vs_btc_4h_proxy_pct"]),-0.2)
        self.assertEqual(float(features[1]["directional_strength_vs_btc_4h_proxy_pct"]),-2.5)
        self.assertEqual(float(features[1]["directional_return_rank_4h"]),55.0)

    def test_shadow_outcome_mismatch_rejected(self):
        self.shadows[1]["actual_decision"]="ORDER_SUBMITTED"
        with self.assertRaisesRegex(ValueError,"decision mismatch"):
            self.run_selection()


if __name__=="__main__":
    unittest.main()
