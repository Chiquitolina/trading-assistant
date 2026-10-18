#!/usr/bin/env python3
"""Safe, read-only overview of Reaction Lab source/services/data paths.

Never includes .env, raw PM2 env, SSH keys, PM2 logs, CSV/SQLite payloads,
Redis values, or historical candles. A code-review ZIP, NOT a data backup.
"""
import argparse
import csv
import datetime as dt
import io
import json
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

EXCLUDED_DIRS = {'.git','.venv','venv','env','node_modules','__pycache__',
 '.pytest_cache','.mypy_cache','.ruff_cache','.tox','dist','build','snapshots',
 'reports','datasets','data','logs','log','.ssh','.aws','.config','backups',
 'backup','secrets','private','.certs','certs','tmp','temp','cache',
 'market_data_cache','historical_cache','downloads'}
SENSITIVE_NAMES = re.compile(r'(^\.env($|\.)|credential|password|passwd|secret|private[_-]?key|id_rsa|\.pem$|\.p12$|\.key$|\.crt$|keystore|wallet|token|authorization)', re.I)
SOURCE_EXT = {'.py','.md','.rst','.toml','.cfg','.ini','.yaml','.yml','.json','.sh','.js','.ts','.service','.sql'}
SPECIAL_FILENAMES = {'Dockerfile','Makefile','.gitignore','requirements.txt','constraints.txt'}
SENSITIVE_ASSIGN = re.compile(r'(?i)(?<![\w])(?:api[_-]?key|api[_-]?secret|password|passwd|secret[_-]?key|access[_-]?token|refresh[_-]?token|authorization|private[_-]?key)\s*(?:=|:)')
SENSITIVE_LITERALS = re.compile(r'AKIA[0-9A-Z]{16}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|(?i:bearer\s+[A-Za-z0-9_.-]{12,})|(?i:(?:https?|redis|postgresql|mysql)://)[^\s\'\"]+:[^\s\'\"@]+@')
SECRET_KEY = re.compile(r'(?i)(?:pass|token|secret|credential|authorization|private[_-]?key|access[_-]?key|api[_-]?key)')
SECRET_ARG = re.compile(r'(?i)^--?(?:api[_-]?key|secret|password|token|access[_-]?key|authorization)(?:=|$)')
REPORT_SUFFIXES={'.csv','.tsv','.json','.jsonl','.sqlite','.sqlite3','.db','.pkl','.parquet','.arrow','.feather','.npz','.npy','.gz','.zip','.txt','.md'}
DATA_ROOT_GLOBS=('runner_sep_*','runner_competition_*','runner_policy_*','runner_phase3*',
                 'replay_v2_*','runner_historical*','sweep-replay-*')
MAX_INV=150000


def safe_text(raw, label):
    text=raw.decode('utf-8', errors='replace')
    redactions=[]
    out=[]
    for index, line in enumerate(text.splitlines(keepends=True), 1):
        if SENSITIVE_ASSIGN.search(line) or SENSITIVE_LITERALS.search(line):
            redactions.append(index)
            out.append(f'# [REDACTED CREDENTIAL-LIKE LINE {index} IN {label}]\n')
        else:
            out.append(line)
    return ''.join(out).encode('utf-8'), redactions


def scrub_json(value):
    if isinstance(value, dict):
        return {k: ('[REDACTED]' if SECRET_KEY.search(str(k)) else scrub_json(v)) for k,v in value.items()}
    if isinstance(value, list): return [scrub_json(x) for x in value]
    if isinstance(value,str) and (SENSITIVE_LITERALS.search(value) or SENSITIVE_ASSIGN.search(value)):
        return '[REDACTED]'
    return value


def cmd_version(parts):
    try:
        r=subprocess.run(parts,capture_output=True,text=True,timeout=7,check=False)
        if r.returncode: return 'unavailable'
        line=(r.stdout or r.stderr).strip().splitlines()
        return line[0][:130] if line else 'unknown'
    except Exception: return 'unavailable'


def scrub_args(args):
    if isinstance(args, str): args=[args]
    result=[]; hide_next=False
    for arg in (args or []):
        s=str(arg)
        if hide_next:
            result.append('[REDACTED]'); hide_next=False; continue
        if SECRET_ARG.match(s):
            if '=' in s: result.append(s.split('=',1)[0]+'=[REDACTED]')
            else: result.append(s); hide_next=True
        elif SENSITIVE_LITERALS.search(s): result.append('[REDACTED]')
        else: result.append(s[:350])
    return result


def pm2_snapshot():
    try:
        r=subprocess.run(['pm2','jlist'],capture_output=True,text=True,timeout=15,check=False)
        if r.returncode: return {'status':'pm2_unavailable_or_nonzero'}
        processes=json.loads(r.stdout)
        result=[]
        for p in processes:
            env=p.get('pm2_env') or {}
            rec={'name':p.get('name'),'pm_id':p.get('pm_id'),
                 'status':env.get('status'),'pm_exec_path':env.get('pm_exec_path'),
                 'pm_cwd':env.get('pm_cwd'),'exec_interpreter':env.get('exec_interpreter'),
                 'exec_mode':env.get('exec_mode'),'args_sanitized':scrub_args(env.get('args')),
                 'restart_time':env.get('restart_time')}
            result.append(rec)
        return {'status':'ok','processes':sorted(result,key=lambda x:str(x['name']))}
    except Exception: return {'status':'pm2_unavailable_or_unparseable'}


def include_file(path):
    if SENSITIVE_NAMES.search(path.name): return False
    if path.name.startswith('.') and path.name != '.gitignore': return False
    return path.suffix.lower() in SOURCE_EXT or path.name in SPECIAL_FILENAMES or path.name.startswith(('requirements','constraints')) and path.suffix=='.txt'


def source_files(roots, max_bytes, max_files):
    seen=set(); paths=[]; skipped=[]
    for root in roots:
        if not root.is_dir(): continue
        for current,dirs,files in os.walk(root,followlinks=False):
            here=Path(current)
            dirs[:]=sorted(d for d in dirs if d.lower() not in EXCLUDED_DIRS and not (here/d).is_symlink())
            for name in sorted(files):
                path=here/name
                if not include_file(path) or path.is_symlink(): continue
                try: stat=path.stat()
                except OSError: continue
                if stat.st_size > max_bytes:
                    skipped.append({'file':str(path),'reason':'exceeds_source_size_limit'})
                    continue
                if str(path) in seen: continue
                paths.append(path);seen.add(str(path))
                if len(paths)>=max_files:
                    skipped.append({'reason':'max_source_files_reached'})
                    return paths,skipped
    return paths,skipped


def inventory(project,research_root):
    entries=[]; limits=[]; count=0
    roots=[project/'reports',project/'snapshots',project/'data',project/'datasets']
    roots += sorted(p for pat in DATA_ROOT_GLOBS for p in research_root.glob(pat) if p.is_dir())
    seen=set()
    for root in roots:
        if not root.is_dir() or str(root) in seen: continue
        seen.add(str(root))
        for current,dirs,files in os.walk(root,followlinks=False):
            here=Path(current)
            dirs[:]=[d for d in sorted(dirs) if d.lower() not in {'.git','.venv','venv','node_modules','__pycache__','.ssh','.aws'} and not (here/d).is_symlink()]
            for name in sorted(files):
                path=here/name
                if path.is_symlink() or SENSITIVE_NAMES.search(name) or name.startswith('.'): continue
                if path.suffix.lower() not in REPORT_SUFFIXES: continue
                try: st=path.stat()
                except OSError: continue
                entries.append((str(path),st.st_size,dt.datetime.fromtimestamp(st.st_mtime,dt.timezone.utc).isoformat()))
                count+=1
                if count>=MAX_INV:
                    limits.append('PERSISTENCE_INVENTORY_TRUNCATED_AT_150000')
                    return entries, limits, roots
    # root-level canonical monitors and configs - filenames only
    for path in sorted(project.iterdir()) if project.is_dir() else []:
        if path.is_symlink() or not path.is_file() or SENSITIVE_NAMES.search(path.name): continue
        if path.suffix.lower() not in REPORT_SUFFIXES: continue
        if not (path.name.startswith(('candidate_','reaction_','volume_','micro_')) or path.name in ('status.json','trades.csv')):continue
        try: st=path.stat()
        except OSError: continue
        entries.append((str(path),st.st_size,dt.datetime.fromtimestamp(st.st_mtime,dt.timezone.utc).isoformat()))
    return entries,limits,roots


def add_json(zf,name,data):
    zf.writestr(name,json.dumps(data,ensure_ascii=False,indent=2,sort_keys=True).encode('utf-8'))


def main():
    pa=argparse.ArgumentParser(description=__doc__)
    pa.add_argument('--project',default='/home/multiservices-1m')
    pa.add_argument('--research-root',default='/root')
    pa.add_argument('--out',default='/root/reaction_project_audit_safe.zip')
    pa.add_argument('--extra-root',action='append',default=[],help='Optional additional code project directory')
    pa.add_argument('--max-source-mib',type=int,default=8)
    pa.add_argument('--max-source-files',type=int,default=20000)
    args=pa.parse_args()
    project=Path(args.project).resolve();research=Path(args.research_root).resolve()
    out=Path(args.out).resolve()
    roots=[project]+[Path(x).resolve() for x in args.extra_root]
    for r in roots:
        if not r.is_dir() or r==Path('/') or r==Path('/root') or r==Path('/home'):
            pa.error('Each source root must be a specific existing project directory')
    sources,skipped=source_files(roots,args.max_source_mib*1024**2,args.max_source_files)
    files,limits,scan_roots=inventory(project,research)
    versions={name:cmd_version(argv) for name,argv in {
        'python3':['python3','--version'],'node':['node','--version'],
        'npm':['npm','--version'],'pm2':['pm2','--version'],
        'redis-cli':['redis-cli','--version'],'git':['git','--version'],
        'streamlit':['streamlit','--version']}.items()}
    manifest={'generated_utc':dt.datetime.now(dt.timezone.utc).isoformat(),
              'purpose':'Source and process audit only, no historical data payloads',
              'project_roots':[str(p) for p in roots],
              'source_files_included':len(sources),'data_file_metadata_count':len(files),
              'redacted_lines':[],'source_files_skipped':skipped,
              'inventory_limits':limits,
              'warnings':['Read archive before uploading: automated secret scrub cannot guarantee no credentials in arbitrary source code.',
              'PM2 online means process online, not verified data freshness.',
              'Not a restoration-ready backup. Use research_backup_vps.py for data payloads.']}
    with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as zf:
        add_json(zf,'SYSTEM/pm2_services_sanitized.json',pm2_snapshot())
        add_json(zf,'SYSTEM/tool_versions.json',versions)
        buffer=io.StringIO();writer=csv.writer(buffer);writer.writerow(['vps_path','bytes','mtime_utc'])
        for row in files:writer.writerow(row)
        zf.writestr('DATA/persistence_inventory.csv',buffer.getvalue())
        add_json(zf,'DATA/inventory_summary.json',{'roots_scanned':[str(x) for x in scan_roots],
            'files':len(files),'total_bytes':sum(row[1] for row in files),'warnings':limits})
        for root_index,root in enumerate(roots):
            for path in (p for p in sources if p==root or root in p.parents):
                name=f'SOURCE/project_{root_index}/'+path.relative_to(root).as_posix()
                try:
                    raw=path.read_bytes()
                    if path.suffix.lower()=='.json':
                        try: raw=json.dumps(scrub_json(json.loads(raw)),ensure_ascii=False,indent=2).encode('utf-8')
                        except Exception: raw,red=safe_text(raw,name)
                        else: red=[]
                    else: raw,red=safe_text(raw,name)
                    if red: manifest['redacted_lines'].append({'file':name,'line_numbers':red})
                    zf.writestr(name,raw)
                except Exception as exc:
                    skipped.append({'file':str(path),'reason':type(exc).__name__})
        add_json(zf,'SYSTEM/audit_manifest.json',manifest)
        zf.writestr('LEEME_ANTES_DE_COMPARTIR.txt',
            'Este paquete NO contiene históricos CSV/SQLite, ni PM2 env, ni logs.\n'
            'ANTES DE COMPARTIR: revisar SOURCE/ por secretos embebidos que los filtros no detecten.\n'
            'Adjuntar el ZIP en ChatGPT para construir el runbook exacto por servicio y el plan de espejo de Windows.\n')
    print(json.dumps({'output':str(out),'size_mib':round(out.stat().st_size/1024**2,2),
        'source_files':len(sources),'data_paths_inventoried':len(files),
        'source_lines_redacted':sum(len(x['line_numbers']) for x in manifest['redacted_lines']),
        'pm2_snapshot':'included_sanitized','data_payloads_included':False},indent=2))

if __name__=='__main__': main()
