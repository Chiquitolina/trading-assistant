#!/usr/bin/env python3
"""Research Mirror: inventory / streaming tar.gz. Read-only source, SQLite online backup.

Designed for Python 3.10 on the VPS. Do not run as an automated live-service restart.
"""
import argparse
import fnmatch
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tarfile
import tempfile
from datetime import datetime, timezone

VERSION = 'research-mirror-audited-v1'
SQL_EXT = ('.sqlite3', '.db', '.sqlite')
DATA_EXT = ('.csv', '.tsv', '.json', '.jsonl', '.ndjson', '.pkl', '.pickle', '.gz', '.parquet', '.feather', '.sqlite3', '.db', '.sqlite', '.npz', '.npy', '.txt')
CODE_EXT = ('.py', '.md', '.txt', '.json', '.toml', '.yaml', '.yml', '.cjs', '.js', '.sh', '.ini', '.cfg', '.patch', '.sql', '.lock')
SKIP_DIRS = {'.git', '.venv', 'venv', '__pycache__', '.pytest_cache', 'node_modules', 'dist', 'build', '.mypy_cache', '.ruff_cache', 'logs', '.next', '.cache'}
SKIP_CODE_DATA_DIRS = {'reports', 'data', 'datasets', 'snapshots', 'compression_snapshots'}
SKIP_FILE_EXACT = {'.env', '.env.local', '.env.production', '.env.development', 'id_rsa', 'id_ed25519', 'known_hosts', 'authorized_keys', 'credentials.json', 'service-account.json', 'secrets.json', 'token.json', 'cookies.txt', 'session.json'}
SECRET_REGEX = re.compile(r'(?:^|[._-])(private.?key|secret|credential|api.?key|access.?token|refresh.?token|password)(?:[._-]|$)',re.I)
SKIP_SUFFIX = ('.pem', '.key', '.p12', '.pfx', '.kdbx', '.sqlite3-wal', '.sqlite3-shm', '.db-wal', '.db-shm')


def blocked(path):
    name = path.name.lower()
    return (name in SKIP_FILE_EXACT or name.startswith('.env.') or name.endswith(SKIP_SUFFIX)
            or SECRET_REGEX.search(name) is not None)


def walk_files(root, excluded_dirs=frozenset()):
    if not root.is_dir() or root.is_symlink():
        return
    for base, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and d not in excluded_dirs
                         and not (Path(base)/d).is_symlink())
        for name in sorted(files):
            f = Path(base) / name
            if f.is_file() and not f.is_symlink() and not blocked(f):
                yield f


def discover(project, research_root, include_code=False):
    entries = {}  # original absolute path -> category
    def add(p, category):
        if p.is_file() and not p.is_symlink() and not blocked(p):
            entries[str(p)] = category

    for folder in ('reports', 'data', 'datasets', 'snapshots', 'compression_snapshots'):
        for f in walk_files(project / folder):
            add(f, 'project-data')

    if project.is_dir():
        for f in sorted(project.iterdir()):
            if f.is_file() and (f.name.lower().endswith(DATA_EXT) or f.name in {'status.json'}):
                add(f, 'project-root-data')

    if research_root.is_dir():
        for folder in sorted(research_root.iterdir()):
            if folder.is_dir() and not folder.is_symlink() and (
                folder.name.startswith('runner_') or folder.name.startswith('replay_')
            ):
                for f in walk_files(folder):
                    add(f, 'historical-research')

    if include_code:
        for f in walk_files(project, excluded_dirs=SKIP_CODE_DATA_DIRS):
            if f.name.lower().endswith(CODE_EXT):
                add(f, 'source-code')

    return [(Path(k),v) for k,v in sorted(entries.items())]


def preflight(project, research_root, include_code):
    entries = discover(project, research_root, include_code)
    totals = {}
    changed = []
    sizes = 0
    for f,cat in entries:
        try:
            sz=f.stat().st_size
        except OSError:
            changed.append(str(f))
            continue
        sizes += sz
        d=totals.setdefault(cat,{'files':0,'bytes':0})
        d['files']+=1; d['bytes']+=sz
    expected = ['reports/reaction_lab/scanner_events_v1.sqlite3',
        'reports/reaction_lab/auto_paths_v1.sqlite3',
        'reports/reaction_lab/causal_context_v1.sqlite3',
        'reports/reaction_lab/runners_v1.sqlite3',
        'reports/reaction_lab/btc_context_v1.sqlite3',
        'reports/reaction_lab/deep_research_v1.sqlite3',
        'reports/reaction_lab/telemetry_v1.sqlite3',
        'reports/reaction_lab/telemetry_link_v1.sqlite3',
        'candidate_v2_monitor.csv', 'volume_exhaustion_events.csv']
    return dict(version=VERSION, project=str(project), research_root=str(research_root),
        include_code=include_code, file_count=len(entries), uncompressed_source_bytes=sizes,
        by_category=totals, missing_expected=[x for x in expected if not (project/x).is_file()],
        cannot_stat=changed,
        warnings=['Live exports are NOT an atomic multi-dataset snapshot.',
                  'Running 290-symbol replay may change files while exporting.',
                  'Code export may contain hard-coded credentials: keep archive PRIVATE and inspect before sharing.',
                  'Redis in-memory state, PM2 runtime, environment secrets and other projects are excluded.'])


class HashReader:
    def __init__(self, fd, limit):
        self.fd=fd
        self.remaining=limit
        self.digest=hashlib.sha256()
        self.read_bytes=0
    def read(self, size=-1):
        if self.remaining<=0:
            return b''
        cap = min(self.remaining, 1024*1024 if size is None or size<0 else size)
        b=self.fd.read(cap)
        self.remaining-=len(b)
        self.read_bytes+=len(b)
        self.digest.update(b)
        return b


def write_tar(tar, src, cat):
    before=src.stat()
    snap_tmp=None
    staged=src
    is_sql=src.name.lower().endswith(SQL_EXT)
    if is_sql:
        fd,name=tempfile.mkstemp(prefix='reaction_sqlite_backup_',suffix='.sqlite3')
        os.close(fd)
        snap_tmp=Path(name)
        try:
            conn=sqlite3.connect(src.resolve().as_uri()+'?mode=ro',uri=True,timeout=30)
            try:
                conn.execute('PRAGMA busy_timeout=30000')
                dest=sqlite3.connect(str(snap_tmp),timeout=30)
                try: conn.backup(dest,pages=256,sleep=0.05)
                finally: dest.close()
            finally: conn.close()
            staged=snap_tmp
        except Exception:
            snap_tmp.unlink(missing_ok=True)
            raise
    try:
        sz=staged.stat().st_size
        arcname='mirror/'+str(src).lstrip('/')
        ti=tarfile.TarInfo(arcname)
        ti.size=sz
        ti.mode=0o644
        ti.mtime=int(before.st_mtime)
        with staged.open('rb') as fd:
            reader=HashReader(fd,sz)
            tar.addfile(ti,reader)
            if reader.read_bytes!=sz:
                raise IOError(f'File shortened during backup: {src}')
            digest=reader.digest.hexdigest()
        after=src.stat()
        return {'source':str(src),'archive_path':arcname,'category':cat,
                'bytes':sz,'sha256':digest,'sqlite_consistent_backup':is_sql,
                'source_changed_during_export':(before.st_size != after.st_size or before.st_mtime_ns!=after.st_mtime_ns)}
    finally:
        if snap_tmp: snap_tmp.unlink(missing_ok=True)


def stream(project, research_root, include_code):
    entries=discover(project,research_root,include_code)
    pre=preflight(project,research_root,include_code)
    manifest={'format_version':VERSION,'created_utc':datetime.now(timezone.utc).isoformat(),
        'preflight':pre,'files':[],'errors':[],
        'integrity_scope':'byte_exact_per_archived_file; not globally atomic across services',
        'status':'INCOMPLETE_UNTIL_VERIFIED'}
    try:
        with tarfile.open(fileobj=sys.stdout.buffer,mode='w|gz',compresslevel=1) as tar:
            for n,(file,cat) in enumerate(entries,1):
                try:
                    manifest['files'].append(write_tar(tar,file,cat))
                except Exception as e:
                    manifest['errors'].append({'source':str(file),'error_type':type(e).__name__,'detail':str(e)[:250]})
                    print(f'ERROR copying {file}: {type(e).__name__}: {e}',file=sys.stderr,flush=True)
                if n%1000==0:
                    print(f'{n}/{len(entries)} files streamed',file=sys.stderr,flush=True)
            changed=sum(bool(f['source_changed_during_export']) for f in manifest['files'])
            manifest['status']='COMPLETE_WITH_LIVE_CHANGES' if changed and not manifest['errors'] else ('INCOMPLETE_ERRORS' if manifest['errors'] else 'COMPLETE_PER_FILE')
            encoded=json.dumps(manifest,ensure_ascii=False,indent=2).encode('utf-8')
            info=tarfile.TarInfo('backup_manifest.json')
            info.size=len(encoded);info.mode=0o644
            tar.addfile(info,io.BytesIO(encoded))
        sys.stdout.buffer.flush()
    except BrokenPipeError:
        return 1
    return 1 if manifest['errors'] else 0


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=('list','stream'))
    p.add_argument('--project',type=Path,default=Path('/home/multiservices-1m'))
    p.add_argument('--research-root',type=Path,default=Path('/root'))
    p.add_argument('--include-code',action='store_true',help='Includes project source (may contain hard-coded secrets).')
    args=p.parse_args(argv)
    if args.command=='list':
        print(json.dumps(preflight(args.project,args.research_root,args.include_code),indent=2,ensure_ascii=False))
        return 0
    return stream(args.project,args.research_root,args.include_code)

if __name__=='__main__':
    sys.exit(main())
