"""Isolated smoke test of Candidate snapshot writes (no live data needed)."""
from __future__ import annotations

import importlib.util
import tempfile
from pathlib import Path
import pandas as pd

SERVICE = Path(__file__).resolve().parents[1] / "services" / "candidate_research_store.py"
spec = importlib.util.spec_from_file_location("candidate_research_store_under_test", SERVICE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

class FakeStore(module.CandidateResearchStore):
    @property
    def available(self):
        return True

    @classmethod
    def _atomic_parquet(cls, frame, path):
        # Writes a harmless fixture in a temporary directory. Test only the
        # profile routing/metadata, not the pyarrow serialization layer.
        path.parent.mkdir(parents=True, exist_ok=True)
        frame.to_csv(path, index=False)

with tempfile.TemporaryDirectory() as directory:
    store = FakeStore(Path(directory))
    old = pd.DataFrame({"candidate_v1_event_key": ["V1-A", "V1-B"]})
    v2 = pd.DataFrame({"candidate_v2_event_key": ["V2-A"]})
    v1_profiles = {
        "v1_raw": old,
        "v1_legacy": old,
        "v1_legacy_execution": old,
        "v1_legacy_matrix_execution": old,
    }
    manifest1 = store.write_bundle(v1_profiles, source_meta={"refreshed_candidate": "v1"})
    v1_time = manifest1["profiles"]["v1_raw"]["built_at_epoch"]
    v1_bytes = store.profile_path("v1_raw").read_bytes()
    manifest2 = store.write_bundle({"v2": v2}, source_meta={"refreshed_candidate": "v2"})
    assert store.profile_path("v1_raw").read_bytes() == v1_bytes
    assert manifest2["profiles"]["v1_raw"]["built_at_epoch"] == v1_time
    assert manifest2["profiles"]["v2"]["rows"] == 1
    assert manifest2["source"]["refreshed_candidate"] == "v2"
    assert manifest2["last_written_profiles"] == ["v2"]
    old_v2_bytes = store.profile_path("v2").read_bytes()
    v2_time = manifest2["profiles"]["v2"]["built_at_epoch"]
    manifest3 = store.write_bundle(v1_profiles, source_meta={"refreshed_candidate": "v1"})
    assert store.profile_path("v2").read_bytes() == old_v2_bytes
    assert manifest3["profiles"]["v2"]["built_at_epoch"] == v2_time
    assert manifest3["last_written_profiles"] == list(v1_profiles)
    for profile in store.PROFILE_FILES:
        assert store.snapshot_exists(profile), profile

print("PASS: V1/V2 Parquet profiles and timestamps are independent")

# Verify the BUILD router itself never touches the other Candidate version.
import ast
import time
app_text = (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
app_module = ast.parse(app_text)
build_node = next(
    n for n in app_module.body
    if isinstance(n, ast.FunctionDef) and n.name == "_candidate_fast_build_snapshot"
)
function_module = ast.Module(body=[build_node], type_ignores=[])
call_log = []

class FakeResearchStore:
    available = True
    def write_bundle(self, frames, *, source_meta=None):
        call_log.append(("write", tuple(frames)))
        return {"profiles": {name: {"rows": len(df)} for name, df in frames.items()}}

def data(label):
    return pd.DataFrame({"candidate_v1_event_key": [label], "candidate_v2_event_key": [label]})

def mark(name, response):
    def f(*args, **kwargs):
        call_log.append((name, None))
        return response
    return f

ns = {
    "candidate_research_store": FakeResearchStore(),
    "time": time,
    "pd": pd,
    "_candidate_v2_load_or_freeze_universe": mark("v2_universe", (data("V2"), {}, False)),
    "_candidate_v2_build_market_context": mark("context", (data("CTX"), None)),
    "_candidate_analysis_retests_signature": mark("signature", (3, "digest", 123)),
    "_candidate_v1_persist_legacy_prefilter_ledger": mark("v1_ledger", {}),
    "_candidate_fast_build_v1_raw_universe": mark("v1_raw", data("RAW")),
    "_candidate_v1_unified_research_history": mark("v1_legacy", data("LEGACY")),
    "_candidate_v2_execution_grid": mark("v1_exec", data("EXEC")),
    "_candidate_v2_merge_execution_context": mark("merge", data("MERGE")),
    "_candidate_fast_discovery_history_capture": mark("capture", {"status": "recorded"}),
}
exec(compile(function_module, "app.py", "exec"), ns)
run = ns["_candidate_fast_build_snapshot"]

call_log.clear()
result = run(data("SCAN"), {}, {}, candidate_version="v2")
assert result["discovery_history"]["status"] == "not_applicable_to_v2"
assert ("write", ("v2",)) in call_log, call_log
assert not any(name.startswith("v1_") or name == "capture" for name, _ in call_log), call_log

call_log.clear()
result = run(data("SCAN"), {}, {}, candidate_version="v1")
assert ("write", (
    "v1_raw", "v1_legacy", "v1_legacy_execution", "v1_legacy_matrix_execution"
)) in call_log, call_log
assert not any(name == "v2_universe" for name, _ in call_log), call_log
assert ("capture", None) in call_log
print("PASS: BUILD routing writes only selected Candidate; V1 history is isolated")
