"""Offline synthetic tests for HLL mechanism overlay V1."""
from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

import hll_fingerprint_mechanism_join_audit_v1 as audit
import hll_similarity_mechanism_adapter_v1 as overlay
from test_hll_fingerprint_mechanism_join_audit_v1 import make_fixture, write_csv


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        make_fixture(self.root)
        self.audit_dir = self.root / audit.DEFAULT_OUTPUT
        self.summary = audit.run(self.root, self.audit_dir)
        self.assertTrue(self.summary["is_compatible"])
        self.output = self.root / overlay.DEFAULT_OUTPUT

    def test_not_only_episode_first_attempt_is_queryable(self):
        result = overlay.run(self.root, self.audit_dir, self.output)
        self.assertEqual(result["attempts"], 3)
        self.assertEqual(result["records"], 9)
        self.assertEqual(result["entries"], 2)
        self.assertEqual(result["scope"], "ALL_ATTEMPTS_BY_EXACT_ATTEMPT_ID")
        r = overlay.load_attempt(self.output / "candidate_mechanism_overlay.sqlite3", "a2")
        self.assertEqual(len(r), 3)
        self.assertTrue(all(v["attempt_id"] == "a2" for v in r))
        self.assertTrue(all(v["entry_status"] == "NOT_COVERED" for v in r))
        self.assertEqual({v["episode_id"] for v in r}, {"ep1"})
        # Exactly this attempt, never inherited from a1 in the same episode.
        self.assertTrue(all(v["entry_id"] == "" for v in r))
        self.assertEqual(r[0]["pre_closed_ts"], "300")

    def test_distinguishes_entry_routes_and_source_prices(self):
        overlay.run(self.root, self.audit_dir, self.output)
        r = {x["route"]: x for x in overlay.load_attempt(self.output / "candidate_mechanism_overlay.sqlite3", "a1")}
        self.assertEqual(set(r), set(overlay.ROUTES))
        self.assertEqual(r["FLIP_1M"]["entry_status"], "ENTRY_VERIFIED")
        self.assertEqual(r["FLIP_1M"]["confirmed_closed_ts"], "260")
        self.assertEqual(r["FLIP_1M"]["entry_open_execution_1m"], "100.5")
        self.assertEqual(r["FLIP_1M"]["signal_reference_timeframe"], "1m")
        self.assertEqual(r["V29_CONTINUATION_30M"]["signal_reference_timeframe"], "30m")
        self.assertEqual(r["V29_CONTINUATION_30M"]["mechanism_status"], "CONFIRMED")
        self.assertEqual(r["V29_RECLAIM_30M"]["entry_status"], "NO_SIGNAL")
        self.assertEqual(r["V29_RECLAIM_30M"]["entry_id"], "")
        self.assertTrue(all(x["post_is_retrospective"] == "true" for x in r.values()))
        self.assertEqual(overlay.load_attempt(self.output / "candidate_mechanism_overlay.sqlite3", "invalid"), [])

    def test_discrepancies_preserve_both_open_prices(self):
        path = self.root / audit.SOURCE_FILES["entry_events"]
        with path.open(encoding="utf-8", newline="") as f:
            events = list(csv.DictReader(f))
        events[0]["entry_price"] = "100.4"
        write_csv(path, events)
        path = self.root / audit.SOURCE_FILES["open_discrepancies"]
        path.write_text("entry_id,attempt_id,symbol,route,entry_ts,entry_utc,signal_reference_open_30m,execution_open_1m,difference_pct\n"
                        + f'{events[0]["entry_id"]},a1,BTCUSDT,{events[0]["route"]},350.0,,100.5,100.4,-0.0995024876\n', encoding="utf-8")
        manifest_path = self.root / audit.MANIFEST_FILES["entries"]
        manifest = json.loads(manifest_path.read_text())
        manifest["entry_open_discrepancies"] = 1
        manifest_path.write_text(json.dumps(manifest))
        audit.run(self.root, self.audit_dir)
        result = overlay.run(self.root, self.audit_dir, self.output)
        self.assertEqual(result["source_open_discrepancies"], 1)
        rec = {r["route"]: r for r in overlay.load_attempt(self.output / "candidate_mechanism_overlay.sqlite3", "a1")}
        r = rec[events[0]["route"]]
        self.assertEqual(r["entry_open_signal_reference"], "100.5")
        self.assertEqual(r["entry_open_execution_1m"], "100.4")
        self.assertEqual(r["open_source_discrepancy"], "true")

    def test_refuses_stale_audit_when_source_changed(self):
        source = self.root / audit.SOURCE_FILES["mechanisms"]
        with source.open("a", encoding="utf-8") as f:
            f.write("\n")
        with self.assertRaisesRegex(ValueError, "Fuente distinta"):
            overlay.run(self.root, self.audit_dir, self.output)
        self.assertFalse(self.output.exists())

    def test_refuses_failed_or_modified_audit(self):
        manifest_path = self.audit_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["status"] = "AUDIT_FAILED"
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "sin estado DONE"):
            overlay.run(self.root, self.audit_dir, self.output)
        self.assertFalse(self.output.exists())

    def test_only_outputs_new_reports_no_pre_or_source_modification(self):
        before = {k: audit.digest(self.root / p) for k, p in audit.SOURCE_FILES.items()}
        self.assertEqual(overlay.main(["--root", str(self.root), "--plan"]), 0)
        self.assertFalse(self.output.exists())
        overlay.run(self.root, self.audit_dir, self.output)
        after = {k: audit.digest(self.root / p) for k, p in audit.SOURCE_FILES.items()}
        self.assertEqual(before, after)
        self.assertTrue((self.output / "candidate_mechanism_overlay.csv").is_file())
        self.assertTrue((self.output / "candidate_mechanism_overlay.sqlite3").is_file())
        self.assertTrue((self.output / "manifest.json").is_file())


if __name__ == "__main__":
    unittest.main()
