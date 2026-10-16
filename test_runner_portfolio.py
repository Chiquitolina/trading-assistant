"""Offline deterministic tests, no Binance/Redis/SQLite inputs required."""
import json
import math
import sqlite3
import tempfile
import unittest
from pathlib import Path

from dashboard.services.reaction_lab_portfolio import (
    PROFILES, PortfolioSettings, simulate_strategy, run_comparison,
    source_digest, export_comparison, validate_profiles, MINUTE,
)
from dashboard.services.reaction_lab_runner_optimizer import BASELINE

T = 1_000_020_000_000


def record(key="X|LONG|1", ready=T + 2*MINUTE + 7000,
           base=T, room=3., mode="TP", side="LONG"):
    bars = [(base+i*MINUTE, 100., 100.15, 99.85, 100.) for i in range(360)]
    if mode in ("TP", "SL", "AMBIG"):
        if side == "LONG":
            bars[3] = (base+3*MINUTE, 99., 99.2, 97.8, 98.1)
            bars[4] = (base+4*MINUTE, 98.1, 100.3, 98., 100.) if mode == "TP" else (
                (base+4*MINUTE, 98.1, 98.2, 96., 97.) if mode == "SL" else
                (base+4*MINUTE, 98.1, 101., 96., 98.))
        else:
            bars[3] = (base+3*MINUTE, 101., 102.2, 100.5, 102.)
            bars[4] = (base+4*MINUTE, 102., 102.2, 99.8, 100.) if mode == "TP" else (
                (base+4*MINUTE, 103., 104., 101.8, 103.) if mode == "SL" else
                (base+4*MINUTE, 103., 104., 99.8, 102.))
    if mode == "AMBIG_FILL":
        bars[3] = (base+3*MINUTE, 99., 101., 97.8, 98.5)
    if mode == "LATE_TP":
        bars[10] = (base+10*MINUTE, 99., 99.2, 97.8, 98.1)
        bars[11] = (base+11*MINUTE, 98.1, 100.3, 98., 100.)
    return {"key": key, "symbol": "TEST", "side": side,
            "retest_ts": base-MINUTE, "ready_ms": ready,
            "values": (2., room, 70., 70.),
            "bars": tuple(bars), "reference": 100.,
            "context_status": "READY", "regime_status": "READY"}


class TestPortfolio(unittest.TestCase):
    def test_profiles_locked(self):
        validate_profiles(PROFILES)
        wrong = {**PROFILES, "Runner V1 Frozen": {**BASELINE, "tp": 3.0}}
        with self.assertRaisesRegex(ValueError, "baseline modified"):
            validate_profiles(wrong)

    def test_one_trade_compounds_exactly(self):
        out = simulate_strategy([record()], "Frozen", BASELINE)
        self.assertEqual(out["summary"]["resolved_trades"], 1)
        self.assertEqual(out["trades"][0]["status"], "TP")
        self.assertAlmostEqual(out["trades"][0]["net_pct"], 1.96)
        self.assertAlmostEqual(out["trades"][0]["notional_usdt"], 480.)
        self.assertAlmostEqual(out["summary"]["equity_final_realized_usdt"], 209.408)
        self.assertEqual(out["summary"]["audit_status"], "COMPLETE_1M_ESTIMATED")

    def test_one_pending_order_blocks_next_signal(self):
        waiting = record("A", mode="NONE")
        next_signal = record("B", base=T+5*MINUTE, ready=T+6*MINUTE+7000)
        out = simulate_strategy([waiting, next_signal], "Frozen", BASELINE)
        self.assertEqual(out["summary"]["skipped_slot_busy"], 1)
        self.assertEqual(out["summary"]["orders_placed"], 1)
        self.assertEqual(out["summary"]["equity_final_realized_usdt"], 200.)

    def test_open_trade_blocks_overlapping_signal(self):
        e1 = record("A", mode="LATE_TP")
        e2 = record("B", base=T+4*MINUTE, ready=T+5*MINUTE+7000)
        out = simulate_strategy([e1, e2], "Frozen", BASELINE)
        self.assertEqual(out["summary"]["skipped_slot_busy"], 1)
        self.assertEqual(out["summary"]["resolved_trades"], 1)

    def test_nonoverlapping_trades_reinvest_realized_equity(self):
        first = record("A", base=T)
        second = record("B", base=T+30*MINUTE, ready=T+32*MINUTE+7000)
        out = simulate_strategy([first, second], "Frozen", BASELINE)
        self.assertEqual(out["summary"]["resolved_trades"], 2)
        self.assertAlmostEqual(out["summary"]["equity_final_realized_usdt"],
                               200.*(1+1.96/100*.8*3)**2, places=6)
        self.assertAlmostEqual(out["trades"][1]["wallet_before_usdt"], 209.408)

    def test_room_priority_tie_uses_only_known_context(self):
        a = record("A", room=2.1)
        b = record("B", room=4.2)
        out = simulate_strategy([a,b], "Frozen", BASELINE)
        placed = [e for e in out["events"] if e["kind"] == "ORDER_PLACED"]
        self.assertEqual(placed[0]["event_key"], "B")
        self.assertEqual(out["summary"]["skipped_slot_busy"], 1)

    def test_stop_loss_and_drawdown(self):
        out = simulate_strategy([record(mode="SL")], "Frozen", BASELINE)
        self.assertEqual(out["trades"][0]["status"], "SL")
        self.assertLess(out["summary"]["equity_final_realized_usdt"], 200.)
        self.assertLess(out["summary"]["max_marked_drawdown_pct"], 0.)
        self.assertGreater(len(out["equity"]), 1)

    def test_ambiguity_not_silently_booked_as_zero_pnl(self):
        out = simulate_strategy([record(mode="AMBIG_FILL")], "Frozen", BASELINE)
        self.assertIsNone(out["trades"][0]["net_pnl_usdt"])
        self.assertTrue(out["summary"]["audit_status"].startswith("INCOMPLETE"))
        self.assertEqual(out["summary"]["resolved_trades"], 0)

    def test_uncertain_trade_freezes_later_portfolio_not_fake_growth(self):
        first = record("A", mode="AMBIG_FILL")
        later = record("B", base=T+400*MINUTE, ready=T+402*MINUTE+7000)
        out = simulate_strategy([first,later], "Frozen", BASELINE)
        self.assertIsNone(out["summary"]["equity_final_realized_usdt"])
        self.assertEqual(out["summary"]["resolved_trades"], 0)
        self.assertEqual(out["summary"]["outcome_counts"]["BLOCKED_UNRESOLVED_STATE"], 1)

    def test_no_buy_on_signal_before_factors_ready(self):
        e = record(ready=T+5*MINUTE+7000)
        out = simulate_strategy([e], "Frozen", BASELINE)
        self.assertEqual(out["summary"]["resolved_trades"], 0)
        self.assertEqual(out["summary"]["outcome_counts"].get("NO_FILL"), 1)

    def test_source_fingerprint_changes_with_price(self):
        a = record()
        b = record()
        bars = list(b["bars"])
        bars[5] = (bars[5][0], bars[5][1], bars[5][2]+.01, bars[5][3], bars[5][4])
        b["bars"] = bars
        self.assertNotEqual(source_digest([a]), source_digest([b]))

    def test_export_isolated_immutable_folder_and_sqlite(self):
        records = [record()]
        comparison = run_comparison(records)
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)/"v1"
            export_comparison(comparison, records, folder)
            for fname in ("manifest.json", "summary.json", "decisions.csv",
                          "events.csv", "trades.csv", "equity.csv", "path_traces.csv",
                          "portfolio_ledger_v1.sqlite3"):
                self.assertTrue((folder/fname).exists(), fname)
            with sqlite3.connect(folder/"portfolio_ledger_v1.sqlite3") as con:
                self.assertGreater(con.execute("SELECT count(*) FROM events").fetchone()[0], 0)
                self.assertGreater(con.execute("SELECT count(*) FROM path_traces").fetchone()[0], 0)
            with self.assertRaises(FileExistsError):
                export_comparison(comparison, records, folder)
            manifest = json.loads((folder/"manifest.json").read_text())
            self.assertEqual(manifest["source_digest_sha256"], source_digest(records))

    def test_trade_trace_reconstructs_ohlc_with_levels(self):
        e = record()
        out = simulate_strategy([e], "Frozen", BASELINE)
        trace = [b for b in out["path_traces"] if b["stage"] in ("IN_POSITION", "EXIT_BAR")]
        self.assertEqual(trace[0]["timestamp_ms"], T+3*MINUTE)
        self.assertGreater(trace[-1]["running_mfe_pct"], 2.0)
        self.assertAlmostEqual(trace[0]["limit_price"], 98.0)
        self.assertAlmostEqual(trace[0]["tp_price"], 99.96)
        self.assertAlmostEqual(trace[0]["sl_price"], 97.02)

    def test_exhausted_pending_order_paths_included(self):
        out = simulate_strategy([record(mode="NONE")], "Frozen", BASELINE)
        self.assertTrue(out["path_traces"])
        self.assertTrue(all(r["stage"] == "ORDER_WAIT" for r in out["path_traces"]))
        self.assertEqual(out["summary"]["resolved_trades"], 0)

    def test_challenger_parameters_do_not_mutate_baseline(self):
        self.assertEqual(PROFILES["Runner Challenger 220"]["limit"], 0.0)
        self.assertEqual(PROFILES["Runner Challenger 220"]["sl"], 2.0)
        self.assertEqual(PROFILES["Runner V1 Frozen"]["limit"], 2.0)
        self.assertEqual(PROFILES["Runner V1 Frozen"]["sl"], 1.0)

    def test_censored_trade_has_no_fake_pnl(self):
        e = record()
        bars = list(e["bars"][:7])
        bars[4] = (T+4*MINUTE, 98.1, 98.5, 98., 98.2)  # no TP yet
        bars[5] = (T+5*MINUTE, 98.2, 98.8, 97.5, 98.)
        bars[6] = (T+6*MINUTE, 98., 98.6, 97.4, 98.)
        e["bars"] = bars
        out = simulate_strategy([e], "Frozen", BASELINE)
        self.assertIsNone(out["trades"][0]["net_pnl_usdt"])
        self.assertNotEqual(out["summary"]["audit_status"], "COMPLETE_1M_ESTIMATED")


if __name__ == "__main__":
    unittest.main()
