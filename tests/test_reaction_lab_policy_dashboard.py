"""Contract tests for read-only Policy Research viewer, no Streamlit needed."""
import csv
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from dashboard.services import reaction_lab_policy_dashboard as policy

S = "Runner Challenger 220"
OTHER = "Runner Challenger 220 · Extreme Strength Guard"
B = "BASELINE_ROOM"
ALT = "DIRECTIONAL_STRENGTH_VS_BTC_4H_PROXY"
A_KEY = "AAAUSDT|LONG|1799999940000"
B_KEY = "BBBUSDT|LONG|1799999940000"
TS = 1800000000001


def put_csv(p, rows):
    if rows:
        with p.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)


class PolicyDashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "runner_policy_test"
        self.root.mkdir()
        self.sel = Path(self.temp.name) / "runner_competition_selection"
        self.sel.mkdir()
        (self.sel / "selection_manifest.json").write_text('{"ok":true}')
        (self.sel / "candidate_factor_snapshots.csv").write_text("event_key,room_1h_pct\n")
        hashes = {"selection_manifest": policy.sha256(self.sel / "selection_manifest.json"),
                  "selection_snapshot":policy.sha256(self.sel / "candidate_factor_snapshots.csv")}
        m = {"engine":policy.ENGINE, "source_hashes":hashes,
             "source_digest_sha256":"TEST", "strategies":[S,OTHER], "policies":[B,ALT],
             "reference_parity":{x:{"verified_decisions":5,"verified_trades":2,
                                     "verified_equity_points":3,"reference_source_digest_sha256":"TEST"}
                                 for x in (S,OTHER)}}
        (self.root / "manifest.json").write_text(json.dumps(m))
        (self.root / "summary.json").write_text(json.dumps({"base_parity_verified":True,
                                "strategies":[S,OTHER], "policies":[B,ALT]}))
        put_csv(self.root / "strategy_policy_summary.csv", [
            {"strategy":s,"policy":p,"equity_final_realized_usdt":320 if p==B else 310,
             "return_realized_pct":60 if p==B else 55,"max_marked_drawdown_pct":-20,
             "profit_factor":1.4,"resolved_trades":3,"orders_placed":4}
            for s in (S,OTHER) for p in (B,ALT)
        ])
        put_csv(self.root / "policy_competitions.csv", [
            {"strategy":S, "policy":p, "ready_ms":TS, "eligible_event_keys":A_KEY+";"+B_KEY,
             "order_submitted_event_key":key,"eligible_count":2,
             "slot_was_available_under_policy":True}
            for p,key in ((B,A_KEY),(ALT,B_KEY))
        ])
        put_csv(self.root / "policy_divergences.csv", [
            {"strategy":S,"policy":ALT, "event_key":A_KEY,"ready_ms":TS,
             "baseline_decision":"ORDER_SUBMITTED", "policy_decision":"SKIPPED_SLOT_BUSY",
             "baseline_reason":"TP", "policy_reason":"slot_busy_through_1800000120000"},
            {"strategy":S,"policy":ALT, "event_key":B_KEY,"ready_ms":TS,
             "baseline_decision":"SKIPPED_SLOT_BUSY", "policy_decision":"ORDER_SUBMITTED",
             "baseline_reason":"slot_busy_through_1800000120000", "policy_reason":"SL"},
            {"strategy":S,"policy":ALT, "event_key":"CCCUSDT|LONG|1800000140000",
             "ready_ms":TS+180000,"baseline_decision":"SKIPPED_SLOT_BUSY",
             "policy_decision":"SKIPPED_SLOT_BUSY", "baseline_reason":"slot_busy_through_200",
             "policy_reason":"slot_busy_through_300"},
        ])
        put_csv(self.root / "policy_decisions.csv", [
            {"strategy":S,"policy":p,"ready_ms":TS,"event_key":key,"decision":"ORDER_SUBMITTED",
             "reason":"TP"} for p,key in ((B,A_KEY),(ALT,B_KEY))
        ])
        put_csv(self.root / "policy_trades.csv", [
            {"strategy":S,"policy":p,"event_key":key,"status":"TP","net_pct":1.9}
            for p,key in ((B,A_KEY),(ALT,B_KEY))
        ])
        # Make one 60-minute interval include a deep equity dip; it must survive downsampling
        points = [(TS,200.),(TS+60000,205.),(TS+120000,155.),(TS+180000,210.),(TS+240000,202.)]
        put_csv(self.root / "policy_equity.csv", [
            {"strategy":S,"policy":p,"timestamp_ms":ts,"wallet_usdt":x,
             "marked_equity_usdt":x} for p in (B,ALT) for ts,x in points
        ])

    def test_manifest_hashes_and_parity(self):
        m = policy.validate_policy_run(self.root,self.sel)
        self.assertEqual(m["engine"],policy.ENGINE)
        self.assertEqual(len(policy.find_policy_runs(self.sel)),1)

    def test_manifest_drift_fails_closed(self):
        (self.sel / "candidate_factor_snapshots.csv").write_text("changed")
        with self.assertRaisesRegex(ValueError,"no coinciden"):
            policy.validate_policy_run(self.root,self.sel)
        self.assertFalse(policy.find_policy_runs(self.sel))

    def test_missing_reference_parity_fails_closed(self):
        m = json.loads((self.root / "manifest.json").read_text())
        m["reference_parity"][S]["verified_trades"] = 0
        (self.root / "manifest.json").write_text(json.dumps(m))
        with self.assertRaisesRegex(ValueError, "paridad"):
            policy.validate_policy_run(self.root,self.sel)

    def test_comparisons_and_counts(self):
        m=policy.validate_policy_run(self.root,self.sel)
        summ, comps, diffs=policy.load_core(self.root,m)
        self.assertEqual(len(summ),4)
        p=policy.competition_comparison(comps,S,ALT)
        self.assertEqual(len(p),1)
        self.assertTrue(bool(p.iloc[0]["changed_pick"]))
        self.assertTrue(bool(p.iloc[0]["baseline_slot_free"]))
        after, before = policy.divergence_after(diffs,S,ALT,TS)
        self.assertFalse(before)
        self.assertEqual(len(after),3)
        self.assertEqual((after["change_type"]=="Decisión distinta").sum(),2)

    def test_prior_divergence_is_not_attributed_to_later_group(self):
        m=policy.validate_policy_run(self.root,self.sel)
        _,_,diffs=policy.load_core(self.root,m)
        after, prior = policy.divergence_after(diffs,S,ALT,TS+180000)
        self.assertTrue(prior)
        self.assertEqual(len(after),1)
        self.assertIn("reserva",after.iloc[0]["change_type"])

    def test_read_only_csv_filters_and_equity_extrema(self):
        observed = {f.name:policy.sha256(f) for f in self.root.iterdir() if f.is_file()}
        curves=policy.equity_curve(self.root,S,[B,ALT])
        self.assertEqual(set(curves["policy"]),{B,ALT})
        self.assertEqual(curves[curves["policy"]==B]["marked_equity_usdt"].min(),155.)
        self.assertEqual(curves[curves["policy"]==B]["marked_equity_usdt"].max(),210.)
        tr=policy.load_trade_detail(self.root,S,[B,ALT],[A_KEY,B_KEY])
        self.assertEqual(len(tr),2)
        de=policy.load_decisions_window(self.root,S,[B,ALT],TS,TS+3600000)
        self.assertEqual(len(de),2)
        self.assertEqual(observed,{f.name:policy.sha256(f) for f in self.root.iterdir() if f.is_file()})

    def test_duplicate_group_fails_closed(self):
        m=policy.validate_policy_run(self.root,self.sel)
        csv_file = self.root / "policy_competitions.csv"
        with csv_file.open(newline="", encoding="utf-8") as f:
            row = next(csv.DictReader(f))
        with csv_file.open("a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(row))
            writer.writerow(row)
        with self.assertRaisesRegex(ValueError, "duplicad"):
            policy.load_core(self.root,m)


if __name__ == "__main__":
    unittest.main()
