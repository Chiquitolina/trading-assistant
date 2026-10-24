#!/usr/bin/env python3
"""Apply ONLY three sidebar edits to dashboard/app.py. Backup + atomic replace.

Use --check before --apply; if a target marker is absent, nothing is written.
No runtime trading processes are touched. No imports from dashboard.
"""
from __future__ import annotations
import argparse
import hashlib
import os
from pathlib import Path
import tempfile

ROOT=Path(__file__).resolve().parent
APP=ROOT/'dashboard/app.py'
OPERATIONS=(
    ('    "compression_scanner": "🗜️ Compression Scanner",\n',
     '    "compression_scanner": "🗜️ Compression Scanner",\n    "horizontal_breakout_lab": "🧱 Horizontal Breakout Lab",\n'),
    ('        "compression_scanner",\n        "geometry_scanner",',
     '        "compression_scanner",\n        "horizontal_breakout_lab",\n        "geometry_scanner",'),
    ('if selected_section == "compression_scanner":\n',
     'if selected_section == "horizontal_breakout_lab":\n'
     '    # Offline, independent RESEARCH; no trading workers are changed.\n'
     '    from dashboard.views.horizontal_breakout_lab_view import render_horizontal_breakout_lab\n'
     '    render_horizontal_breakout_lab(BASE_DIR)\n\n\n'
     'if selected_section == "compression_scanner":\n'),
)


def change(source):
    if '"horizontal_breakout_lab": "🧱 Horizontal Breakout Lab"' in source:
        if ('if selected_section == "horizontal_breakout_lab":' not in source or
                '"horizontal_breakout_lab",\n        "geometry_scanner",' not in source):
            raise RuntimeError('Partial previous integration; stop and inspect app.py manually')
        return source,True
    for old,new in OPERATIONS:
        if source.count(old)!=1:
            raise RuntimeError(f'Expected exactly one target marker: {old!r}. No files changed.')
        source=source.replace(old,new,1)
    return source,False


def main():
    parser=argparse.ArgumentParser()
    flags=parser.add_mutually_exclusive_group(required=True)
    flags.add_argument('--check',action='store_true')
    flags.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    original=APP.read_text(encoding='utf-8')
    changed,already=change(original)
    print('app.py:',APP)
    print('Already integrated:',already)
    print('Original SHA256:',hashlib.sha256(original.encode()).hexdigest())
    if args.check or already:
        print('CHECK_OK. No file changes.')
        return
    backup=APP.with_name('app.py.hbl_backup')
    if backup.exists():
        raise RuntimeError(f'Backup already exists; refusing overwrite: {backup}')
    backup.write_text(original,encoding='utf-8')
    fd,name=tempfile.mkstemp(prefix='app_hbl_',suffix='.py',dir=APP.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:f.write(changed)
        compile(changed,str(APP),'exec')
        os.replace(name,APP)
    finally:
        if os.path.exists(name):os.unlink(name)
    print('APPLIED. Backup:',backup)

if __name__=='__main__':main()
