#!/usr/bin/env python3
"""Full TP 1..6 by SL 1..5 grid for causal FAILED Reversal/Retest V3.

Read-only input. Runs exactly 5 sequential stop configurations, using V3's
verified historical 1m reader, events, timing and ambiguous-fill bounds.
Previous V3 reports are NOT overwritten; derived reports live under
reports/reaction_lab/failed_reversal_retest_v3/matrix_tp1to6_sl1to5/.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil

import reaction_lab_failed_reclaim_audit_v1 as v1
import reaction_lab_failed_reversal_retest_v3 as v3

SCHEMA = 'FAILED_SWEEP_REVERSAL_TP_SL_MATRIX_V31'
TARGETS = tuple(range(1, 7))
STOPS = tuple(range(1, 6))
ROOT = v3.OUTPUT / 'matrix_tp1to6_sl1to5'


def matrix_dir(project):
    return Path(project).resolve() / ROOT


def stop_dir(project, sl):
    if sl not in STOPS:
        raise ValueError('SL outside frozen 1..5% matrix')
    return matrix_dir(project) / f'sl_{sl}pct'


def verify_sl_report(folder, sl):
    """Refuse stale or incomplete inputs rather than silently merge them."""
    manifest_path = folder / 'manifest.json'
    summary_path = folder / 'summary.csv'
    outcomes_path = folder / 'event_outcomes.csv'
    events_path = folder / 'events.csv'
    if not all(p.is_file() for p in (manifest_path, summary_path, outcomes_path, events_path)):
        raise ValueError(f'Missing output for SL {sl}: {folder}')
    data = json.loads(manifest_path.read_text(encoding='utf-8'))
    settings = data.get('settings', {})
    if (data.get('schema') != v3.SCHEMA or
        tuple(data.get('targets_pct', [])) != TARGETS or
        tuple(settings.get('depths_pct', [])) != v3.DEFAULT_DEPTHS or
        tuple(data.get('modes', [])) != v3.MODES or
        settings.get('stop_mode') != 'pct' or
        float(settings.get('sl_pct', -1)) != float(sl) or
        settings.get('hold_min') != 180 or settings.get('max_wait_min') != 60):
        raise ValueError(f'Incorrect manifest for SL {sl}: {folder}')
    with summary_path.open(newline='', encoding='utf-8') as f:
        summary = list(csv.DictReader(f))
    expected = {(mode, float(depth), float(tp))
                for mode in v3.MODES for depth in v3.DEFAULT_DEPTHS for tp in TARGETS}
    obtained = {(r['mode'], float(r['depth_pct']), float(r['target_pct']))
                for r in summary if r['group_type'] == 'ALL'}
    if expected != obtained or len([r for r in summary if r['group_type'] == 'ALL']) != len(expected):
        raise ValueError(f'Incomplete 84-cell mode/depth/TP grid for SL {sl}')
    return data, summary


def run(project, *, min_free_mb=8192, sample_metadata=None, recovery_root=None):
    project = Path(project).resolve()
    root = matrix_dir(project)
    cutoff_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    journal_snapshot = (v1.read_sample_journal(sample_metadata) if sample_metadata else
        v1.read_journal(project/'reports/reaction_lab/scanner_first_touches_v1.sqlite3'))
    frozen_keys = frozenset(journal_snapshot)
    eligible_keys = None
    cohort = None
    summaries = []
    sl_runs = []
    for i, sl in enumerate(STOPS, 1):
        free_mb = shutil.disk_usage(project).free / 1024**2
        if free_mb < min_free_mb:
            raise RuntimeError(f'Disk safety limit before SL -{sl}%: {free_mb:.0f} MB < {min_free_mb} MB')
        print('FAILED_V31_MATRIX_START ' + json.dumps({'index': i, 'total': len(STOPS),
              'sl_pct': sl, 'free_mb': round(free_mb)}), flush=True)
        result = v3.build(project, stop_mode='pct', sl_pct=sl,
                          targets=TARGETS, out=stop_dir(project, sl),
                          frozen_keys=frozen_keys, cutoff_ms=cutoff_ms,
                          eligible_keys=eligible_keys, sample_metadata=sample_metadata,
                          recovery_root=recovery_root)
        if eligible_keys is None:
            with (stop_dir(project, sl)/'events.csv').open(newline='', encoding='utf-8') as f:
                eligible_keys = frozenset(r['touch_key'] for r in csv.DictReader(f)
                     if r['eligible_common_180m'].strip().lower() in ('true','1'))
        manifest, summary = verify_sl_report(stop_dir(project, sl), sl)
        current = (manifest['cohort_total'], manifest['coverage_common'])
        if cohort is None:
            cohort = current
        elif current != cohort:
            raise RuntimeError(f'Inconsistent universes across stops: {cohort} vs {current}')
        summaries.extend(summary)
        sl_runs.append({'sl_pct':sl, 'cohort_total':current[0],
                        'coverage_common':current[1], 'out_dir':str(stop_dir(project, sl))})
        print('FAILED_V31_MATRIX_SL_DONE ' + json.dumps({'sl_pct':sl,
              'cohort_total':current[0], 'coverage_common':current[1],
              'all_group_rows':len([r for r in summary if r['group_type']=='ALL'])}), flush=True)
    expected_all = len(STOPS)*len(v3.DEFAULT_DEPTHS)*len(v3.MODES)*len(TARGETS)
    actual_all = sum(r['group_type'] == 'ALL' for r in summaries)
    if actual_all != expected_all:
        raise RuntimeError(f'Matrix incomplete: {actual_all}/{expected_all}')
    v1.write_csv_atomic(root/'summary.csv', summaries, v3.SUMMARY_FIELDS)
    metadata = {
      'schema':SCHEMA, 'created_utc':datetime.now(timezone.utc).isoformat(),
      'cohort_total':cohort[0], 'coverage_common':cohort[1],
      'source_snapshot_cutoff_ms':cutoff_ms, 'frozen_touch_keys':len(frozen_keys),
      'sample': bool(sample_metadata),
      'targets_pct':list(TARGETS), 'stops_pct':list(STOPS),
      'depths_pct':list(v3.DEFAULT_DEPTHS), 'modes':list(v3.MODES),
      'scenario_cells':expected_all, 'per_stop':sl_runs,
      'summary_csv':str(root/'summary.csv'),
      'notes':[
        'Every TP/SL cell evaluated from the original entry at identical causal timing.',
        'Only derived outputs created; V3 legacy per-stop results are not overwritten.',
        'Each SL has event-level worst/best intrabar uncertainty and time exits.',
        'Cohort identities and available 1m bars frozen at start of matrix run.',
        'No spread, funding, realistic slippage, portfolio overlap or OOS validation.',
      ],
    }
    v1.write_json_atomic(root/'manifest.json', metadata)
    print('FAILED_V31_MATRIX_FINISHED ' + json.dumps(metadata, ensure_ascii=False), flush=True)
    return metadata


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--project', default='.')
    p.add_argument('--min-free-mb', type=int, default=8192)
    p.add_argument('--sample-metadata', help='Tests only: frozen CSV with 106 FAILED metadata')
    p.add_argument('--recovery-root', help='Tests only: directory holding verified pilot batches')
    action = p.add_mutually_exclusive_group(required=True)
    action.add_argument('--plan', action='store_true')
    action.add_argument('--run', action='store_true')
    a = p.parse_args(argv)
    if a.min_free_mb < 0:
        p.error('--min-free-mb must be >= 0')
    if a.plan:
        root = Path(a.project).resolve()
        journal = (v1.read_sample_journal(a.sample_metadata) if a.sample_metadata else
                   v1.read_journal(root/'reports/reaction_lab/scanner_first_touches_v1.sqlite3'))
        print('FAILED_V31_MATRIX_PLAN ' + json.dumps({
            'schema':SCHEMA, 'journal_failed':len(journal),
            'targets_pct':list(TARGETS), 'stops_pct':list(STOPS),
            'depths_pct':list(v3.DEFAULT_DEPTHS), 'modes':list(v3.MODES),
            'scenario_cells':len(TARGETS)*len(STOPS)*len(v3.DEFAULT_DEPTHS)*len(v3.MODES),
            'out_dir':str(matrix_dir(root)), 'input_sources_read_only':True,
            'legacy_reports_unchanged':True, 'min_free_mb':a.min_free_mb,
            'sample':bool(a.sample_metadata),
        }, ensure_ascii=False))
        return 0
    run(a.project, min_free_mb=a.min_free_mb,
        sample_metadata=a.sample_metadata,recovery_root=a.recovery_root)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
