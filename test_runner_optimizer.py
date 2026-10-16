"""Tests de regresión aislados: no usan las SQLite LIVE."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dashboard.services.reaction_lab_runner_optimizer import (  # noqa: E402
    BASELINE, SETTINGS, MINUTE, sample_configs, parameter_grid,
    simulate, evaluate, run_search,
)


T = 1_000_020_000_000


def fixture(*, ready=T+2*MINUTE+7_000, side="LONG", event="X"):
    rows = [(T+i*MINUTE, 100., 100.2, 99.8, 100.) for i in range(360)]
    return dict(key=event, side=side, symbol="X", retest_ts=T-MINUTE,
                ready_ms=ready, bars=rows, reference=100.,
                values=(2., 3., 70., 70.))


class TestRunnerOptimizer(unittest.TestCase):
    def test_grid_sampling_reproducible_and_frozen(self):
        grid = parameter_grid([
            {"Clave": k, "Optimizar": True, "Valores a probar": vals}
            for k, _, vals, _ in SETTINGS
        ])
        a, total = sample_configs(grid, budget=25)
        b, _ = sample_configs(grid, budget=25)
        self.assertEqual(a, b)
        self.assertGreater(total, 25)
        self.assertIn(BASELINE, a)

    def test_no_fill(self):
        e = fixture()
        self.assertEqual(simulate(e, BASELINE)["status"], "NO_FILL")

    def test_tp_after_fill(self):
        e = fixture()
        e["bars"][3] = (T+3*MINUTE, 99., 99.2, 97.8, 98.1)
        e["bars"][4] = (T+4*MINUTE, 98.1, 100.1, 98., 100.)
        x = simulate(e, BASELINE)
        self.assertEqual(x["status"], "TP")
        self.assertAlmostEqual(x["pnl"], 1.93)

    def test_short_tp_after_fill(self):
        e = fixture(side="SHORT")
        e["bars"][3] = (T+3*MINUTE, 101., 102.2, 100.5, 102.)
        e["bars"][4] = (T+4*MINUTE, 102., 102.2, 99.8, 100.)
        result = simulate(e, BASELINE)
        self.assertEqual(result["status"], "TP")
        self.assertAlmostEqual(result["pnl"], 1.93)

    def test_tp_fill_bar_ambiguous(self):
        e = fixture()
        e["bars"][3] = (T+3*MINUTE, 99., 101., 97.8, 98.5)
        self.assertEqual(simulate(e, BASELINE)["status"], "AMBIGUOUS_FILL_BAR")

    def test_sl_same_fill_bar(self):
        e = fixture()
        e["bars"][3] = (T+3*MINUTE, 99., 99.1, 96.5, 97.1)
        self.assertEqual(simulate(e, BASELINE)["status"], "SL")

    def test_both_same_following_bar_as_sl(self):
        e = fixture()
        e["bars"][3] = (T+3*MINUTE, 99., 99.2, 97.8, 98.1)
        e["bars"][4] = (T+4*MINUTE, 98.1, 101., 96., 98.)
        self.assertEqual(simulate(e, BASELINE)["status"], "AMBIGUOUS_BOTH_AS_SL")

    def test_not_count_touch_before_factors_ready(self):
        e = fixture(ready=T+5*MINUTE+7_000)
        e["bars"][3] = (T+3*MINUTE, 99., 99.1, 97.8, 98.)
        self.assertEqual(simulate(e, BASELINE)["status"], "NO_FILL")

    def test_market_orderable_at_candle_open(self):
        e = fixture()
        e["bars"][3] = (T+3*MINUTE, 97.5, 100., 97.4, 99.)
        self.assertEqual(simulate(e, BASELINE)["status"], "TP")

    def test_train_split_and_no_holdout_optimization(self):
        events = []
        for i in range(10):
            e = fixture(event=str(i))
            e["retest_ts"] += i*MINUTE
            events.append(e)
        grid = {k:(v,) for k,v in BASELINE.items()}
        outcome = run_search(events, grid, budget=50, min_fills=1)
        self.assertEqual(outcome["train_n"], 7)
        self.assertEqual(outcome["holdout_n"], 3)
        self.assertEqual(outcome["tested"], 1)
        self.assertEqual(evaluate(events, BASELINE)["sin_fill"], 10)


if __name__ == "__main__":
    unittest.main()
