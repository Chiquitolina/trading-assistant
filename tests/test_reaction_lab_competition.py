"""Phase-1 unit tests: no exchange/network/dependency or main-dashboard import."""
import csv
import json
import tempfile
import unittest
from pathlib import Path
from dashboard.services.reaction_lab_competition import construct, export_readonly

S = "Runner Challenger 220"
G = "Runner Challenger 220 · Extreme Strength Guard"


def _csv(path, rows):
    columns = sorted({k for row in rows for k in row})
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def _decision(strategy, key, ready, room, decision, reason):
    return {"strategy": strategy, "event_key": key, "symbol": key.split("|")[0],
            "side": "SHORT", "ready_ms": ready, "room_1h": room,
            "departure": 2, "return_rank": 96, "volume_rank": 95,
            "retest_ts": ready-60000, "decision": decision, "reason": reason}


class CompetitionLabTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.run = Path(self.temp.name) / "run"
        self.run.mkdir()
        self.out = Path(self.temp.name) / "derived"
        # 120000: A wins with higher Room, B skipped; later C skipped
        # A exit bar begins 180000, slot freed at 240000.
        # 240000: D submits NO_FILL and E is blocked during its pending TTL.
        decisions = [
            _decision(S, "A|SHORT|60000", 120000, 9, "ORDER_SUBMITTED", "SL"),
            _decision(S, "B|SHORT|60000", 120000, 5, "SKIPPED_SLOT_BUSY", "slot_busy_through_240000"),
            _decision(S, "C|SHORT|150000", 180000, 3, "SKIPPED_SLOT_BUSY", "slot_busy_through_240000"),
            _decision(S, "D|SHORT|180000", 240000, 3, "ORDER_SUBMITTED", "NO_FILL"),
            _decision(S, "E|SHORT|180000", 240000, 2, "SKIPPED_SLOT_BUSY", "slot_busy_through_2040000"),
            _decision(G, "Z|SHORT|60000", 120000, 10, "FILTER_REJECTED_EXTREME_STRENGTH", "guard"),
            _decision(G, "Y|SHORT|60000", 120000, 8, "ORDER_SUBMITTED", "NO_FILL"),
        ]
        trades = [{"strategy": S, "event_key": "A|SHORT|60000",
                   "status": "SL", "fill_ts": "120000", "exit_ts": "180000"}]
        _csv(self.run / "decisions.csv", decisions)
        _csv(self.run / "trades.csv", trades)

    def test_exact_slot_owners_and_busy_until(self):
        result = construct(self.run)
        self.assertEqual(result["strategies"][S]["slot_busy_owner_verified"], 3)
        self.assertEqual(result["strategies"][G]["guard_veto"], 1)
        d = {(row["strategy"], row["event_key"]):row for row in result["decisions"]}
        self.assertEqual(d[(S, "B|SHORT|60000")]["owner_at_decision"], "A|SHORT|60000")
        self.assertEqual(d[(S, "E|SHORT|180000")]["owner_stage"], "PENDING_ORDER")
        self.assertEqual(d[(S, "D|SHORT|180000")]["owner_at_decision"], "")
        self.assertEqual(d[(G, "Z|SHORT|60000")]["admitted_after_filters"], 0)
        self.assertEqual(d[(G, "Y|SHORT|60000")]["simultaneous_eligible"], 1)
        self.assertEqual(result["strategies"][S]["simultaneous_free_slot_groups"], 2)

    def test_busy_reason_tampered_rejected(self):
        path = self.run / "decisions.csv"
        with path.open(newline="") as f:
            rows=list(csv.DictReader(f))
        for row in rows:
            if row["event_key"].startswith("B|"):
                row["reason"] = "slot_busy_through_999999"
        _csv(path, rows)
        with self.assertRaisesRegex(ValueError, "Busy-through mismatch"):
            construct(self.run)

    def test_duplicate_source_decision_rejected(self):
        path = self.run / "decisions.csv"
        with path.open(newline="") as f:
            rows=list(csv.DictReader(f))
        rows.append(dict(rows[0]))
        _csv(path, rows)
        with self.assertRaisesRegex(ValueError, "Duplicate decision keys"):
            construct(self.run)

    def test_create_only_export(self):
        report=export_readonly(self.run, self.out)
        self.assertFalse(report["shadow_outcomes_computed"])
        for filename in ("competition_decisions.csv", "slot_episodes.csv", "ready_groups.csv",
                         "competition_manifest.json"):
            self.assertTrue((self.out / filename).exists())
        with self.assertRaises(FileExistsError):
            export_readonly(self.run, self.out)

    def test_manifest_ttl_conflict_rejected(self):
        (self.run / "manifest.json").write_text(json.dumps({"profiles": {
            S:{"ttl": 10},G:{"ttl":30}}}))
        with self.assertRaisesRegex(ValueError, "Busy-through mismatch"):
            construct(self.run)


if __name__ == "__main__":
    unittest.main()
