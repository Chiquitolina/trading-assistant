#!/usr/bin/env python3
"""Download complete research mirror to Windows using SSH streaming; verify all archived SHA-256."""
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile

REMOTE_SCRIPT='/home/multiservices-1m/scripts/research_mirror_vps.py'

def verified_manifest(archive):
    with tarfile.open(archive,'r:gz') as tar:
        m=tar.extractfile('backup_manifest.json')
        if m is None:raise ValueError('No backup_manifest.json found')
        manifest=json.loads(m.read().decode('utf-8'))
        expected={i['archive_path']:i for i in manifest['files']}
        seen=set()
        for member in tar:
            if not member.isfile() or member.name=='backup_manifest.json':continue
            if member.name not in expected:raise ValueError(f'Unexpected archive member {member.name}')
            if member.name in seen:raise ValueError(f'Duplicate archive member {member.name}')
            seen.add(member.name)
            sha=hashlib.sha256();count=0
            fp=tar.extractfile(member)
            if fp is None:raise ValueError(f'Unreadable entry {member.name}')
            while True:
                chunk=fp.read(1024*1024)
                if not chunk:break
                sha.update(chunk);count+=len(chunk)
            e=expected[member.name]
            if count!=e['bytes'] or sha.hexdigest()!=e['sha256']:
                raise ValueError(f'CHECKSUM MISMATCH {member.name}')
        if seen!=set(expected):raise ValueError(f'Missing archived entries: {len(set(expected)-seen)}')
        if manifest['errors']:raise ValueError(f"VPS reported {len(manifest['errors'])} copying errors")
    return manifest


def cmd_ssh(host,args):
    if not re.fullmatch(r'[a-zA-Z0-9._@:-]+',host):
        raise ValueError('Invalid SSH host format')
    return ['ssh','-o','StrictHostKeyChecking=ask',host,'python3',REMOTE_SCRIPT,*args]


def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--host',help='e.g. root@VPS_IP')
    ap.add_argument('--dest',type=Path,help='Local destination folder')
    ap.add_argument('--include-code',action='store_true')
    ap.add_argument('--verify-only',type=Path,help='Verify an existing tar.gz without SSH')
    ap.add_argument('--assume-yes',action='store_true')
    a=ap.parse_args(argv)
    if a.verify_only:
        m=verified_manifest(a.verify_only)
        print(json.dumps({'status':'VERIFIED','files':len(m['files']), 'manifest_status':m['status'],'live_files_changed':sum(x['source_changed_during_export'] for x in m['files'])},indent=2))
        return 0
    if not a.host or not a.dest:ap.error('--host and --dest are required to download')
    opts=['--include-code'] if a.include_code else []
    r=subprocess.run(cmd_ssh(a.host,['list',*opts]),capture_output=True,text=True)
    if r.returncode:raise RuntimeError(f'VPS preflight failed: {r.stderr.strip()[:1500]}')
    estimate=json.loads(r.stdout)
    print('VPS files:',estimate['file_count'])
    print('Uncompressed GB:',round(estimate['uncompressed_source_bytes']/1e9,2))
    print('Expected sources missing:',estimate['missing_expected'])
    print('Code included:',a.include_code)
    a.dest.mkdir(parents=True,exist_ok=True)
    free=shutil.disk_usage(a.dest).free
    print('PC free GB:',round(free/1e9,2))
    if free < estimate['uncompressed_source_bytes']*1.20:
        print('WARNING: Free disk space is low relative to source bytes; compression may or may not suffice.')
        if free < estimate['uncompressed_source_bytes']*0.25:
            raise RuntimeError('Too little free space for a safe transfer')
    if not a.assume_yes:
        answer=input('Download from VPS to this folder? Type YES: ').strip()
        if answer!='YES':print('Canceled');return 2
    now=datetime.now().strftime('%Y%m%d_%H%M%S')
    dest=a.dest/f'reaction_mirror_{now}.tar.gz'
    part=Path(str(dest)+'.part')
    cmd=cmd_ssh(a.host,['stream',*opts])
    p=subprocess.Popen(cmd,stdout=subprocess.PIPE)
    size=0
    with part.open('wb') as out:
        while True:
            buf=p.stdout.read(1024*1024)
            if not buf:break
            out.write(buf);size+=len(buf)
            if size and size%(256*1024*1024)<len(buf):
                print(f'Downloaded {size/1e9:.2f} GB',flush=True)
    ret=p.wait()
    if ret:
        print('Transfer failed; preserved partial file:',part)
        raise RuntimeError(f'SSH export failed (exit {ret})')
    print('Verifying all archived SHA-256 hashes; this can take several minutes...')
    try:m=verified_manifest(part)
    except Exception:
        print('Archive NOT VERIFIED. Retained as:',part)
        raise
    part.replace(dest)
    (a.dest/f'{dest.name}.manifest.json').write_text(json.dumps(m,indent=2,ensure_ascii=False),encoding='utf-8')
    report={'archive':str(dest),'verified':True,'files':len(m['files']),
            'copied_bytes':sum(x['bytes'] for x in m['files']),
            'manifest_status':m['status'],
            'source_changed_during_export':sum(bool(x['source_changed_during_export']) for x in m['files']),
            'notes':'Original research may have been active; not globally atomic.'}
    (a.dest/f'{dest.name}.verification.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report,indent=2))
    print('KEEP PRIVATE: source code and trading research can contain sensitive information.')
    return 0

if __name__=='__main__':
    try:sys.exit(main())
    except (OSError,ValueError,RuntimeError,tarfile.TarError) as e:
        print(f'ERROR: {e}',file=sys.stderr);sys.exit(1)
