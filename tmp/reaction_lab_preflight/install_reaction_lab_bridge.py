#!/usr/bin/env python3
"""Idempotent guarded app.py patch; --check = dry run, --apply = backup+apply."""
import argparse
import ast
import os
import difflib
from pathlib import Path
from datetime import datetime,timezone

OLD_SCAN='''    return scan_confirmed_swing_retests_all_symbols(
        symbols=tuple(str(symbol) for symbol in symbols),
        swing_timeframes=(str(swing_timeframe),),
        swing_detector_items=((str(swing_timeframe), str(swing_detector)),),
        min_swing_prominence_pct=float(min_swing_prominence_pct),
        retest_tolerance_pct=float(retest_tolerance_pct),
        min_departure_pct=float(min_departure_pct),
        max_age_minutes=int(max_age_minutes),
        max_retest_age_minutes=int(max_retest_age_minutes),
    )'''
NEW_SCAN='''    from reaction_lab_scan_bridge import merge_scanner_reactions
    current = scan_confirmed_swing_retests_all_symbols(
        symbols=tuple(str(symbol) for symbol in symbols),
        swing_timeframes=(str(swing_timeframe),),
        swing_detector_items=((str(swing_timeframe), str(swing_detector)),),
        min_swing_prominence_pct=float(min_swing_prominence_pct),
        retest_tolerance_pct=float(retest_tolerance_pct),
        min_departure_pct=float(min_departure_pct),
        max_age_minutes=int(max_age_minutes),
        max_retest_age_minutes=int(max_retest_age_minutes),
    )
    return merge_scanner_reactions(
        current, BASE_DIR, symbols=symbols,
        timeframe=swing_timeframe, detector=swing_detector,
        min_prominence=min_swing_prominence_pct,
        tolerance=retest_tolerance_pct, departure=min_departure_pct,
        max_age=max_age_minutes,
    )'''


def replace_function_return(source,name):
    tree=ast.parse(source)
    func=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name==name)
    text=source.splitlines(keepends=True)
    segment=''.join(text[func.lineno-1:func.end_lineno])
    # match ONLY within the intended function
    marker='    return candidate_v1_fast_refresh_path_store('
    if segment.count(marker)!=1:
        raise RuntimeError(f'Expected one path-store return in {name}')
    segment=segment.replace(marker, '    from reaction_lab_scan_bridge import merge_auto_paths\n    legacy = candidate_v1_fast_refresh_path_store(',1)
    if not segment.rstrip().endswith('    )'):
        raise RuntimeError(f'Unexpected end of {name}; cannot patch safely')
    segment=segment.rstrip()+ '\n    return merge_auto_paths(legacy, BASE_DIR, candidate_rows)\n'
    # Preserve newline formatting based on original splitlines
    return ''.join(text[:func.lineno-1])+segment+''.join(text[func.end_lineno:])


FAST_ANCHOR = """            if fast_snapshot_ready:
                render_candidate_fast_explorer("""
FAST_INSERT = """            if fast_snapshot_ready:
                # Fast view also shows the persistent feed even if the heavy
                # Candidate materialization has not been refreshed.
                from reaction_lab_scan_bridge import scan_rows
                latest_scanner_events = scan_rows(
                    BASE_DIR / 'reports/reaction_lab/scanner_events_v1.sqlite3',
                    symbols=structural_symbols, limit=100,
                )
                if not latest_scanner_events.empty:
                    with st.expander('📡 Continuous REACTION scanner · last 100 persisted', expanded=False):
                        st.caption(
                            'Live-feed journal only. Fast Candidate matrix is a separate snapshot; '
                            'new events are not automatically scored/executed until materialized.'
                        )
                        scan_fields = [x for x in (
                            'symbol','signal','retest_timestamp','status',
                            'reaction_scanner_first_seen_ms',
                            'reaction_scanner_observation_type'
                        ) if x in latest_scanner_events.columns]
                        st.dataframe(latest_scanner_events[scan_fields], use_container_width=True)
                render_candidate_fast_explorer("""


def modify(content):
    if 'merge_scanner_reactions(' in content and 'merge_auto_paths(' in content:
        raise RuntimeError('Bridge appears already installed; refusing second patch')
    if content.count(OLD_SCAN)!=1:
        raise RuntimeError('Expected original shared scan function exactly once')
    content=content.replace(OLD_SCAN,NEW_SCAN,1)
    if content.count(FAST_ANCHOR)!=1:
        raise RuntimeError('Fast Candidate panel signature not found')
    content=content.replace(FAST_ANCHOR, FAST_INSERT,1)
    for name in ('_candidate_v1_fast_path_store', '_candidate_v1_matched_control_path_store',
                 '_candidate_v2_path_store'):
        content=replace_function_return(content,name)
    ast.parse(content)
    return content


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--project',type=Path,default=Path.cwd())
    g=p.add_mutually_exclusive_group(required=True)
    g.add_argument('--check',action='store_true')
    g.add_argument('--apply',action='store_true')
    args=p.parse_args()
    file=args.project.resolve()/'dashboard/app.py'
    data=file.read_bytes(); newline='\r\n' if b'\r\n' in data else '\n'
    old=data.decode('utf-8-sig').replace('\r\n','\n')
    new=modify(old)
    print('PATCH TARGET:',file)
    print('SIZE:',len(data),'bytes; CRLF:',newline=='\r\n')
    diff=list(difflib.unified_diff(old.splitlines(),new.splitlines(),fromfile='existing/app.py',tofile='patched/app.py'))
    print('PATCH PREVIEW (first 110 lines):')
    print('\n'.join(diff[:110]))
    if args.check:
        print('CHECK PASS: no files modified')
        return
    timestamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup_dir=Path('/root') if Path('/root').is_dir() and os.access('/root',os.W_OK) else file.parent
    backup=backup_dir/('app.py.before_continuous_reactions_'+timestamp)
    backup.write_bytes(data)
    tmp=file.with_name('app.py.reaction_bridge.tmp')
    tmp.write_bytes(new.replace('\n',newline).encode('utf-8'))
    tmp.replace(file)
    print('APPLIED; BACKUP:',backup)
if __name__=='__main__':main()
