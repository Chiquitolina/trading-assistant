#!/usr/bin/env python3
"""Multi-Swing Structural Behavior V1.2.2: immutable cross-band clock index fix.

Rebuild ONLY the descriptive clock-dependency index of V1.2.1 from the
immutable physical transitions of V1.2. Pandas groupby(dropna=False) is
required: direction is legitimately NA for ACCEPT_INSIDE_3C events.

No reconstruction of swings, OHLC, geometric stages, PRE, event labels or
POST. No Streamlit, LIVE imports, network or trading. This index must NEVER
be used as a causal PRE feature (it describes co-occurring events across
bands known only at their confirmation timestamps).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from pathlib import Path

import pandas as pd

VERSION = 'btc-multiswing-structural-behavior-clock-dependency-v1.2.2'
BASE = Path('reports/reaction_lab/btc_multiswing_60d_v1')
DEFAULT_V12 = BASE / 'btc_sep09_nov08_structural_behavior_15m_1h_v12'
DEFAULT_V121 = BASE / 'btc_sep09_nov08_structural_behavior_pre_safety_v121'
DEFAULT_OUTPUT = BASE / 'btc_sep09_nov08_structural_behavior_clock_dependency_v122'
V12_VERSION = 'btc-multiswing-structural-behavior-15m-1h-4h-v1.2'
V121_VERSION = 'btc-multiswing-structural-behavior-pre-safety-v1.2.1'
GROUP_COLS = ['confirmation_ts', 'source_tf', 'event_type', 'direction']
INDEX_COLS = GROUP_COLS + [
    'physical_transition_id', 'region_id', 'concurrent_bands', 'audit_role'
]
ROLE = 'RETROSPECTIVE_CLOCK_DEPENDENCY_NOT_PRE_FEATURE'


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def validated_manifest(folder: Path, version: str, required: tuple[str, ...]) -> dict:
    manifest_path = folder / 'manifest.json'
    m = json.loads(manifest_path.read_text(encoding='utf-8'))
    if (m.get('version') != version or m.get('immutable_run') is not True
            or m.get('no_live_imports_or_network') is not True):
        raise ValueError('V122_PARENT_MANIFEST_NOT_IMMUTABLE_OR_WRONG_VERSION: ' + str(folder))
    hashes = m.get('files_sha256')
    if not isinstance(hashes, dict):
        raise ValueError('V122_PARENT_HASHES_MISSING')
    for name in required:
        if name not in hashes or sha256(folder / name) != hashes[name]:
            raise ValueError('V122_PARENT_HASH_MISMATCH: ' + name)
    return m


def load_parents(parent_v12: Path, parent_v121: Path):
    m12 = validated_manifest(parent_v12, V12_VERSION, ('physical_transitions_v12.csv',))
    m121 = validated_manifest(parent_v121, V121_VERSION,
        ('clock_dependency_v121.csv', 'transition_pre_safe_v121.csv',
         'visit_pre_stage_v121.csv', 'geometric_stage_history_v121.csv',
         'pre_safety_summary_v121.json'))
    if m121.get('parent_v12_manifest_sha256') != sha256(parent_v12 / 'manifest.json'):
        raise ValueError('V122_PARENTS_NOT_LINKED_BY_MANIFEST_HASH')
    for name in ('start', 'end', 'source_sha256', 'catalog_sha256'):
        if m12.get('source_provenance', {}).get(name) != m121.get('source_provenance', {}).get(name):
            raise ValueError('V122_PARENT_SOURCE_PROVENANCE_MISMATCH: ' + name)
    physical = pd.read_csv(parent_v12 / 'physical_transitions_v12.csv', low_memory=False)
    old = pd.read_csv(parent_v121 / 'clock_dependency_v121.csv', low_memory=False)
    return physical, old, m12, m121


def rebuild_clock_dependency(physical: pd.DataFrame, old: pd.DataFrame):
    """Return full index, delta omitted by V1.2.1, and audit counts.

    Treat null direction as its own, explicitly non-directional group. Never
    impute BUY/SELL, alter original event identifiers, or merge physical IDs.
    """
    required = set(GROUP_COLS + ['physical_transition_id', 'region_id'])
    if not required.issubset(physical) or not set(INDEX_COLS).issubset(old):
        raise ValueError('V122_REQUIRED_COLUMNS_MISSING')
    if physical.physical_transition_id.isna().any() or physical.physical_transition_id.duplicated().any():
        raise ValueError('V122_PHYSICAL_IDS_NOT_UNIQUE')
    if old.physical_transition_id.isna().any() or old.physical_transition_id.duplicated().any():
        raise ValueError('V122_OLD_INDEX_IDS_NOT_UNIQUE')
    if physical[['confirmation_ts', 'source_tf', 'event_type', 'region_id']].isna().any().any():
        raise ValueError('V122_REQUIRED_GROUP_DATA_MISSING')
    if (physical['confirmation_ts'].lt(0)).any():
        raise ValueError('V122_INVALID_CONFIRMATION_TIMESTAMP')
    updated = physical[GROUP_COLS + ['physical_transition_id', 'region_id']].copy()
    # V1.2.1 omitted null-direction groups at this exact operation. 
    updated['concurrent_bands'] = updated.groupby(
        GROUP_COLS, sort=True, dropna=False)['region_id'].transform('nunique')
    updated = updated.loc[updated.concurrent_bands.gt(1)].copy()
    updated['concurrent_bands'] = updated['concurrent_bands'].astype('int64')
    updated['audit_role'] = ROLE
    updated = updated[INDEX_COLS].reset_index(drop=True)

    if not set(old.physical_transition_id).issubset(set(updated.physical_transition_id)):
        raise ValueError('V122_OLD_INDEX_NOT_SUBSET_OF_REBUILT')
    # Existing V1.2.1 rows must be exactly unchanged, not merely matching IDs.
    common = (updated.set_index('physical_transition_id')
              .loc[old.physical_transition_id]
              .reset_index().set_index('physical_transition_id'))
    prior = old.set_index('physical_transition_id')
    for name in INDEX_COLS:
        if name == 'physical_transition_id':
            continue
        if not common[name].equals(prior[name]):
            raise ValueError('V122_OLD_ROW_DRIFT: ' + name)
    delta = updated.loc[~updated.physical_transition_id.isin(old.physical_transition_id)].copy()
    if delta.empty:
        raise ValueError('V122_NO_OMITTED_EVENTS_FOUND_RECHECK_PARENT')
    if not delta.direction.isna().all():
        raise ValueError('V122_UNEXPLAINED_NON_NULL_DIRECTION_OMISSION')

    # Count groups consistently, *including* NA direction.
    groups_all = updated.groupby(GROUP_COLS, dropna=False).ngroups
    groups_old = old.groupby(GROUP_COLS, dropna=False).ngroups
    groups_delta = delta.groupby(GROUP_COLS, dropna=False).ngroups
    counts = {
        'physical_events_total': len(physical),
        'old_dependency_rows': len(old),
        'new_dependency_rows': len(updated),
        'recovered_null_direction_rows': len(delta),
        'old_dependency_groups': groups_old,
        'new_dependency_groups': groups_all,
        'recovered_null_direction_groups': groups_delta,
        'max_concurrent_bands': int(updated.concurrent_bands.max()),
        'recovered_event_types': {str(k): int(v) for k,v in delta.event_type.value_counts().items()},
        'parent_row_data_changed': 0,
        'event_labels_recalculated': False,
        'pre_features_recalculated': False,
        'not_a_trade_signal': True,
        'warning': ('A same-clock collision is a retrospective dependency, NOT an independent '
                    'observation, PRE feature, validated band state or trading signal.')
    }
    return updated, delta, counts


def write_immutable(output: Path, full: pd.DataFrame, delta: pd.DataFrame, counts: dict,
                    *, v12: Path, v121: Path, m12: dict, m121: dict) -> None:
    if output.exists():
        raise FileExistsError('V122_IMMUTABLE_OUTPUT_ALREADY_EXISTS: ' + str(output))
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.ms_v122_', dir=output.parent) as temp:
        t = Path(temp)
        full.to_csv(t / 'clock_dependency_v122.csv', index=False)
        delta.to_csv(t / 'clock_dependency_recovered_v122.csv', index=False)
        (t / 'dependency_summary_v122.json').write_text(
            json.dumps({'version': VERSION, **counts}, indent=2, ensure_ascii=False, sort_keys=True)+'\n',
            encoding='utf-8')
        names = ('clock_dependency_v122.csv', 'clock_dependency_recovered_v122.csv',
                 'dependency_summary_v122.json')
        manifest = {
            'version': VERSION, 'immutable_run': True, 'read_only': True,
            'no_live_imports_or_network': True,
            'parent_v12': str(v12), 'parent_v121': str(v121),
            'parent_v12_manifest_sha256': sha256(v12 / 'manifest.json'),
            'parent_v121_manifest_sha256': sha256(v121 / 'manifest.json'),
            'source_provenance': m12['source_provenance'],
            'audit_scope': 'ONLY_CLOCK_DEPENDENCY_NA_DIRECTION_FIX',
            'pre_tables_reused_unchanged_from_parent_v121': True,
            'physical_events_reused_unchanged_from_parent_v12': True,
            'files_sha256': {name: sha256(t / name) for name in names},
        }
        (t / 'manifest.json').write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True)+'\n', encoding='utf-8')
        t.rename(output)  # atomic within the same filesystem


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--plan', action='store_true')
    mode.add_argument('--run', action='store_true')
    parser.add_argument('--project-root', type=Path, default=Path('.'))
    parser.add_argument('--parent-v12', type=Path, default=DEFAULT_V12)
    parser.add_argument('--parent-v121', type=Path, default=DEFAULT_V121)
    parser.add_argument('--output-dir', type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    project = args.project_root.resolve()
    def abs_path(p): return p if p.is_absolute() else project / p
    v12, v121, output = (abs_path(p).resolve() for p in
                        (args.parent_v12, args.parent_v121, args.output_dir))
    # An explicit output override must never write inside immutable parents.
    if any(output == parent or parent in output.parents for parent in (v12, v121)):
        raise ValueError('V122_OUTPUT_MUST_BE_OUTSIDE_IMMUTABLE_PARENTS')
    print('MS_BEHAVIOR_V122_PLAN', json.dumps({
        'mode': 'RUN' if args.run else 'PLAN', 'version': VERSION,
        'parent_v12': str(v12), 'parent_v121': str(v121), 'output_dir': str(output),
        'read_only_parents': True, 'changes_PRE_or_events': False, 'no_live': True,
    }, sort_keys=True), flush=True)
    if args.plan:
        return 0
    if output.exists():
        raise FileExistsError('V122_IMMUTABLE_OUTPUT_ALREADY_EXISTS: ' + str(output))
    physical, old, m12, m121 = load_parents(v12, v121)
    full, delta, counts = rebuild_clock_dependency(physical, old)
    write_immutable(output, full, delta, counts, v12=v12, v121=v121, m12=m12, m121=m121)
    print('MS_BEHAVIOR_V122_FINISHED', json.dumps({'counts': counts, 'output': str(output)},
                                                 sort_keys=True), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
