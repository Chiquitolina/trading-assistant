"""Deterministic shadow tests; no network, exchange, or external data needed."""
import unittest
from dashboard.services import reaction_lab_runner_optimizer as v2
from dashboard.services.reaction_lab_competition_shadow import (
    isolated_rows, verify_selected, excursion,
)

S = "Runner Challenger 220"
G = "Runner Challenger 220 · Extreme Strength Guard"
PARAMS = {"departure": 1.5, "room_1h": 1.0, "volume_rank": 40.0,
          "return_rank": 40.0, "limit": 0.0, "ttl": 30, "tp": 2.0, "sl": 2.0}


def rec(key, *, dep=2.0, room=5, ret=60, vol=60, ready=60001):
    # Minute open at 60000 cannot be used; first full bar starts at 120000.
    bars = [(i * 60000, 100.0, 100.4, 99.6, 100.0) for i in range(1, 241)]
    # First post-ready bar fill at 100, second bar TP at 102
    bars[1] = (120000, 100.0, 100.4, 99.6, 100.0)
    bars[2] = (180000, 100.0, 102.5, 99.8, 101.0)
    return {"key": key, "symbol": key.split("|")[0], "side": "LONG",
            "ready_ms": ready, "retest_ts": 0,
            "values": (dep, room, vol, ret), "reference": 100.0, "bars": tuple(bars)}


def d(strategy, key, decision, reason):
    return {"strategy": strategy, "event_key": key, "decision": decision,
            "reason": reason, "side": "LONG", "ready_ms": 60001}


class ShadowTests(unittest.TestCase):
    def setUp(self):
        self.costs = v2._validated_costs()
        self.a = rec("A|LONG|0")
        self.b = rec("B|LONG|0")
        self.c = rec("C|LONG|0", dep=0.2)
        self.o = v2.simulate(self.a, PARAMS, costs=self.costs)
        self.assertEqual(self.o["status"], "TP")
        direction = 1
        entry = self.o["fill_price"]
        exit_ = entry * (1 + direction * self.o["gross_pct"] / 100)
        self.trade = {
            "strategy": S, "event_key": self.a["key"], "status": "TP",
            "fill_ts": self.o["fill_ts"], "exit_ts": self.o["exit_ts"],
            "entry_liquidity": self.o["entry_liquidity"],
            "exit_liquidity": self.o["exit_liquidity"],
            "fill_price": self.o["fill_price"], "exit_price": exit_,
            "net_pct": self.o["pnl"], "gross_pct": self.o["gross_pct"],
            "cost_pct": self.o["cost_pct"],
        }

    def test_selected_parity(self):
        verify_selected(self.o, d(S, self.a["key"], "ORDER_SUBMITTED", "TP"), self.trade)

    def test_selected_mutation_fails(self):
        tampered = dict(self.trade, net_pct=999)
        with self.assertRaisesRegex(ValueError, "Selected-order parity failure"):
            verify_selected(self.o, d(S, self.a["key"], "ORDER_SUBMITTED", "TP"), tampered)

    def test_track_selected_busy_and_ineligible(self):
        decisions = [d(S, self.a["key"], "ORDER_SUBMITTED", "TP"),
                     d(S, self.b["key"], "SKIPPED_SLOT_BUSY", "slot_busy_through_240000"),
                     d(S, self.c["key"], "FILTER_REJECTED", "thresholds")]
        result, stats = isolated_rows(decisions=decisions,
                   trade_index={(S, self.a["key"]): self.trade},
                   records=[self.a, self.b, self.c], profiles={S: PARAMS}, costs=self.costs)
        self.assertEqual(len(result), 3)
        self.assertEqual(stats["selected_parity_verified"], 1)
        self.assertEqual([r["shadow_class"] for r in result],
            ["SELECTED_PARITY_CHECKED", "ELIGIBLE_BLOCKED_SLOT", "OUTSIDE_FROZEN_FILTERS"])
        self.assertEqual(result[1]["shadow_status"], "TP")
        self.assertEqual(result[2]["eligible_under_frozen_rules"], 0)

    def test_shadow_guard_veto_not_new_portfolio(self):
        results, stats = isolated_rows(
            decisions=[d(G, self.a["key"], "FILTER_REJECTED_EXTREME_STRENGTH", "guard")],
            trade_index={}, records=[self.a], profiles={G: PARAMS}, costs=self.costs)
        self.assertEqual(results[0]["shadow_class"], "GUARD_VETO_HYPOTHETICAL")
        self.assertEqual(results[0]["shadow_status"], "TP")
        self.assertEqual(stats.get("selected_parity_verified", 0), 0)

    def test_inconsistent_eligibility_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "Frozen eligibility inconsistency"):
            isolated_rows(decisions=[d(S, self.c["key"], "SKIPPED_SLOT_BUSY", "busy")],
                trade_index={}, records=[self.c], profiles={S: PARAMS}, costs=self.costs)

    def test_excursion_raw_envelope(self):
        ext = excursion(self.a, self.o)
        self.assertGreaterEqual(ext["mfe"], 2.0)
        self.assertGreaterEqual(ext["mae"], 0)
        self.assertGreaterEqual(ext["bars"], 1)


if __name__ == "__main__":
    unittest.main()
