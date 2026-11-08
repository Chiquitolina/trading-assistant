"""Read-only, verified V2.2-A episode inspector support; never generates events."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import pandas as pd

RUNS = Path('reports/horizontal_level_structural_behavior_v22/runs')
FIELDS_EP = {'episode_id','zone_id','first_test_closed_ts','last_test_closed_ts','state','side','resolved_at'}
FIELDS_TR = {'episode_id','zone_id','level_id','observed_closed_ts','transition_role','event_type','test_id'}


def digest(path: Path) -> str:
    h=hashlib.sha256()
    with path.open('rb') as fh:
        for block in iter(lambda: fh.read(1024*1024), b''):
            h.update(block)
    return h.hexdigest()


def verified_load(folder: Path, v1: Path, v2: Path, cutoff: int):
    """Reject stale, tampered and mismatched frozen runs, rather than silently mixing cuts."""
    folder,v1,v2=map(Path,(folder,v1,v2))
    m=json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
    if m.get('version')!='hll-structural-behavior-v2.2a-offline':
        raise ValueError('Versión de auditoría V2.2-A no compatible')
    if int(m['cutoff_ms'])!=int(cutoff):
        raise ValueError('El corte de la auditoría V2.2-A no coincide con el corte del gráfico')
    if Path(m['v1_dir']).resolve()!=v1.resolve() or Path(m['v2_dir']).resolve()!=v2.resolve():
        raise ValueError('Fuentes V1/V2 diferentes de las usadas por la auditoría')
    if digest(v1/'manifest.json')!=m['v1_manifest_sha256'] or digest(v2/'manifest.json')!=m['v2_manifest_sha256']:
        raise ValueError('Manifiestos V1/V2 diferentes de los auditados')
    for name in ('episodes.csv','episode_transitions.csv','behavior_summary.csv'):
        if digest(folder/name)!=m['files_sha256'][name]:
            raise ValueError('Hash inválido en '+name)
    ep=pd.read_csv(folder/'episodes.csv',low_memory=False)
    tr=pd.read_csv(folder/'episode_transitions.csv',low_memory=False)
    if not FIELDS_EP.issubset(ep.columns) or not FIELDS_TR.issubset(tr.columns):
        raise ValueError('Columnas incompletas de auditoría')
    if len(ep)!=int(m['episodes']) or len(tr)!=int(m['transitions']):
        raise ValueError('Cardinalidad diferente del manifiesto')
    if ep.episode_id.duplicated().any() or not set(tr.episode_id).issubset(set(ep.episode_id)):
        raise ValueError('Integridad de identificadores de episodios inválida')
    if (pd.to_numeric(ep.first_test_closed_ts)>cutoff).any() or (pd.to_numeric(tr.observed_closed_ts)>cutoff).any():
        raise ValueError('Evento posterior al corte')
    return m,ep,tr


def available_runs(root:Path, v1:Path, v2:Path, cutoff:int):
    """Small manifest scan without loading audit CSVs on each Streamlit rerun."""
    base=Path(root)/RUNS
    if not base.is_dir():return []
    matched=[]
    for p in sorted(base.iterdir(),reverse=True):
        if not p.is_dir() or not (p/'manifest.json').is_file():continue
        try:
            m=json.loads((p/'manifest.json').read_text(encoding='utf-8'))
            if (m.get('version')=='hll-structural-behavior-v2.2a-offline'
                and int(m['cutoff_ms'])==int(cutoff)
                and Path(m['v1_dir']).resolve()==Path(v1).resolve()
                and Path(m['v2_dir']).resolve()==Path(v2).resolve()):
                matched.append(p)
        except (OSError,ValueError,KeyError,TypeError,json.JSONDecodeError):continue
    return matched


def episode_window(row, cutoff:int, pre_hours:int=12,post_hours:int=12):
    """Window around the test with bounded historical follow-up; never beyond freeze."""
    hour=3_600_000
    first=int(row['first_test_closed_ts'])
    last=row['resolved_at']
    if pd.isna(last):last=row['last_test_closed_ts']
    anchor=max(first,int(last))
    return max(0,first-pre_hours*hour),min(int(cutoff),anchor+post_hours*hour)


def episode_marks(transitions:pd.DataFrame, episode_id:str, cutoff:int):
    data=transitions.loc[transitions.episode_id.astype(str).eq(str(episode_id))].copy()
    data=data.loc[pd.to_numeric(data.observed_closed_ts)<=int(cutoff)]
    return data.sort_values(['observed_closed_ts','level_id','event_type']).copy()
