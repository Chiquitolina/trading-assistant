"""Static + dependency-light checks for full V2 Fast Research isolation.

Run from project root:
 python3 dashboard/scripts/test_candidate_v2_fast_isolation.py

The true V2 price-path/replay integration still requires a live V2 refresh.
"""
from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "dashboard" / "app.py"
STORE = ROOT / "dashboard" / "services" / "candidate_research_store.py"
app_text = APP.read_text(encoding="utf-8")
app_tree = ast.parse(app_text)
functions = {node.name: node for node in app_tree.body if isinstance(node, ast.FunctionDef)}

for name in (
    "_candidate_fast_current_version", "_candidate_fast_widget_key",
    "_candidate_fast_version_path", "_candidate_fast_strategy_label",
    "render_candidate_fast_explorer", "render_candidate_fast_no_trade_lab",
):
    assert name in functions, f"Missing {name}"

names = [
    "_candidate_fast_current_version", "_candidate_fast_widget_key",
    "_candidate_fast_version_path", "_candidate_fast_strategy_label",
]
function_nodes = [functions[name] for name in names]
namespace = {
    "st": SimpleNamespace(session_state={}),
    "Path": Path,
}
exec(compile(ast.Module(body=function_nodes, type_ignores=[]), str(APP), "exec"), namespace)

widget_key = namespace["_candidate_fast_widget_key"]
version_path = namespace["_candidate_fast_version_path"]
strategy_label = namespace["_candidate_fast_strategy_label"]
base = ROOT / "reports" / "research" / "candidate" / "no_trade_discovery_history.csv"
for candidate_version in ("v1", "v2"):
    namespace["st"].session_state["_candidate_fast_active_version"] = candidate_version
    if candidate_version == "v1":
        assert widget_key("candidate_fast_regime_auto_run") == "candidate_fast_regime_auto_run"
        assert version_path(base) == base
        assert strategy_label("Legacy V1 Base") == "Legacy V1 Base"
    else:
        assert widget_key("candidate_fast_regime_auto_run") == "v2_candidate_fast_regime_auto_run"
        assert version_path(base) == base.with_name("v2_no_trade_discovery_history.csv")
        assert strategy_label("Legacy V1 Base") == "Candidate V2 Base"

# Inspect actual Store PROFILE_FILES, including new V2 execution sources.
spec = importlib.util.spec_from_file_location("candidate_parity_store_test", STORE)
mod = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(mod)
profiles = mod.CandidateResearchStore.PROFILE_FILES
for profile in ("v1_raw", "v1_legacy_execution", "v1_legacy_matrix_execution", "v2", "v2_execution", "v2_matrix_execution"):
    assert profile in profiles, profile
assert profiles["v2_execution"] != profiles["v1_legacy_execution"]
assert profiles["v2_matrix_execution"] != profiles["v1_legacy_matrix_execution"]

# Manifest isolation even when Arrow/DuckDB unavailable: replace physical writer.
class FakeStore(mod.CandidateResearchStore):
    @property
    def available(self):
        return True
    @classmethod
    def _atomic_parquet(cls, frame, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"test-only")

with TemporaryDirectory() as tmp:
    store = FakeStore(tmp)
    v1 = store.write_bundle({"v1_raw": pd.DataFrame([{"candidate_v1_event_key": "v1"}])})
    v1_stamp = v1["profiles"]["v1_raw"]["built_at_epoch"]
    v2 = store.write_bundle({
        "v2": pd.DataFrame([{"candidate_v2_event_key": "v2"}]),
        "v2_execution": pd.DataFrame([{"candidate_v2_event_key": "v2"}]),
        "v2_matrix_execution": pd.DataFrame([{"candidate_v2_event_key": "v2"}]),
    })
    assert v2["profiles"]["v1_raw"]["built_at_epoch"] == v1_stamp
    assert v2["last_written_profiles"] == ["v2", "v2_execution", "v2_matrix_execution"]

# V2 uses the same simulator and NO-TRADE engine, with different persisted execution.
build_source = ast.get_source_segment(app_text, functions["_candidate_fast_build_snapshot"])
assert '"v2_execution": v2_fixed' in build_source
assert '"v2_matrix_execution": v2_matrix' in build_source
assert "_candidate_fast_discovery_history_capture(\n                v2_fixed" in build_source
explorer_source = ast.get_source_segment(app_text, functions["render_candidate_fast_explorer"])
for expected in ('render_candidate_fast_legacy_v1_equity(candidate_version="v2")',
                 'render_candidate_fast_legacy_v1_matrix(candidate_version="v2")',
                 'render_candidate_fast_no_trade_lab(candidate_version="v2")'):
    assert expected in explorer_source, expected

lab_source = ast.get_source_segment(app_text, functions["render_candidate_fast_no_trade_lab"])
assert '"v2_execution"' in lab_source
assert "_candidate_fast_render_auto_regime_scanner" in lab_source
assert "_candidate_fast_render_discovery_snapshot_history" in lab_source
assert "_candidate_fast_render_no_trade_validation" in lab_source

for file_name in ("no_trade_discovery_history.csv", "no_trade_discovery_forward.csv", "no_trade_validation.json"):
    path = ROOT / "reports" / "research" / "candidate" / file_name
    namespace["st"].session_state["_candidate_fast_active_version"] = "v2"
    assert version_path(path) != path

print("PASS: V2 execution/matrix, shared analytics, isolated keys, manifest, history, forward and validation")
print("INTEGRATION PENDING: refresh V2 and inspect live execution/matrix/replay outcomes")

# Execute the real V2 builder branch with dependency-light fakes. This checks
# that the V2 path actually writes *only* V2 files, and captures its own history.
build_fn = functions["_candidate_fast_build_snapshot"]
reactions = pd.DataFrame([{
    "candidate_v1_event_key": "X|LONG|123", "candidate_v2_event_key": "X|LONG|123",
    "symbol": "X", "side": "LONG", "entry_timestamp": 123,
}])
write_calls = []
grid_calls = []
history_calls = []

class MockResearchStore:
    available = True
    def write_bundle(self, frame_map, *, source_meta=None):
        write_calls.append((frame_map, source_meta))
        return {"profiles": list(frame_map), "source": source_meta}

def fake_grid(context, *, tp_values, sl_values, **kwargs):
    grid_calls.append((tuple(tp_values), tuple(sl_values)))
    return context.assign(
        **{"TP %": tp_values[0], "SL %": sl_values[0],
           "Outcome": "TP", "net_pnl_pct": 0.5, "exit_timestamp": 124}
    )

def fake_history(execution, **kwargs):
    history_calls.append((namespace["st"].session_state.get("_candidate_fast_active_version"), len(execution)))
    return {"status": "recorded", "rows_added": 10}

namespace.update({
    "candidate_research_store": MockResearchStore(),
    "pd": pd,
    "time": __import__("time"),
    "_candidate_v2_load_or_freeze_universe": lambda frame: (frame.copy(), {}, False),
    "_candidate_v2_build_market_context": lambda frame, force=False: (frame.copy(), None),
    "_candidate_v2_execution_grid": fake_grid,
    "_candidate_v2_merge_execution_context": lambda grid, context: grid.copy(),
    "_candidate_analysis_retests_signature": lambda frame: (len(frame), "abc123", 1000),
    "_candidate_fast_discovery_history_capture": fake_history,
})
exec(compile(ast.Module(body=[build_fn], type_ignores=[]), str(APP), "exec"), namespace)
namespace["st"].session_state["_candidate_fast_active_version"] = "v1"
result = namespace["_candidate_fast_build_snapshot"](
    reactions, {}, {}, candidate_version="v2"
)
assert len(grid_calls) == 2 and len(write_calls) == 1
assert set(write_calls[0][0]) == {"v2", "v2_execution", "v2_matrix_execution"}
assert history_calls == [("v2", 1)]
assert namespace["st"].session_state["_candidate_fast_active_version"] == "v1"
assert result["discovery_history"]["status"] == "recorded"
print("PASS: V2 BUILD branch writes only own Parquet profiles and captures V2 history")
