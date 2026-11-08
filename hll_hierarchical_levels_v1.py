#!/usr/bin/env python3
"""HLL Hierarchical Levels V1 — frozen, read-only 30m structural hierarchy.

Reuses *unchanged* HLL Lifecycle V2.1 formation/event semantics. No outcome,
no trading, no Redis. All importance scores are descriptive, as-of snapshots,
not a prediction or a validated ranking. The frozen file is immutable by tag.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from horizontal_level_lifecycle_v21 import (Config, scan_symbol, FORMATION_COLS, EVENT_COLS, END_COLS)

VERSION = 'hll-hierarchical-levels-v1.0-exploratory'
TF = 1_800_000
ROOT = Path('/home/multiservices-1m')
DEFAULT_SOURCE = Path('reports/horizontal_level_290_history_v298/candles_30m_aug20_oct04_v298/BTCUSDT_30m.csv')
RUNS = Path('reports/horizontal_level_hierarchy_v1/runs')
FIELDS = ('timestamp','open','high','low','close','volume')
OUT_FIELDS = ('level_id','symbol','origin','formation_closed_ts','center','zone_low','zone_high',
              'tier','importance_score','score_formula','state_at_cutoff','source_end_state',
              'last_observed_closed_ts','tests_pre','defenses_pre','losses_pre',
              'reconfirmations_pre','cross_up_pre','cross_down_pre','returns_pre',
              'age_days','distance_atr','quality_flag')


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for part in iter(lambda: f.read(1024*1024), b''):
            h.update(part)
    return h.hexdigest()


def utc(ms: int) -> str:
    return datetime.fromtimestamp(int(ms)/1000,timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def parse_utc(value: str) -> int:
    if not value.endswith('Z'):
        raise ValueError('Fecha UTC requerida con Z')
    clock = int(datetime.fromisoformat(value.replace('Z','+00:00')).timestamp()*1000)
    if clock % TF:
        raise ValueError('Corte debe estar alineado con cierre 30m')
    return clock


def source_metadata(path: Path, symbol: str, *, allow_unverified=False) -> dict:
    """Support frozen V298 30m and the verified Universal Runner offline cache."""
    path = path.resolve()
    candidates = []
    # Universal source: .../symbols/SYMBOL/30m.csv with sibling manifest.
    if path.name == '30m.csv':
        candidates.append(path.parent/'manifest.json')
    # Original HLL V298 source: .../SYMBOL_30m.csv with provenance/SYMBOL.json.
    candidates.append(path.parent/'provenance'/f'{symbol}.json')
    actual = sha(path)
    for p in candidates:
        if not p.is_file():
            continue
        m = json.loads(p.read_text(encoding='utf-8'))
        if m.get('symbol') != symbol:
            raise ValueError('Manifiesto de otro símbolo: ' + str(p))
        if 'timeframes' in m:
            info = m['timeframes'].get('30m') or {}
            expected = info.get('sha256')
            coverage = info.get('validation',{}).get('status')
        else:
            expected = m.get('sha256')
            coverage = m.get('status')
        if expected != actual:
            raise ValueError('SHA de fuente 30m no coincide con manifiesto: ' + str(p))
        if coverage != 'FULL':
            raise ValueError('Fuente 30m sin cobertura FULL: ' + str(coverage))
        return {'provenance':'VERIFIED','manifest':str(p),'sha256':actual,'source_coverage':coverage}
    if not allow_unverified:
        raise ValueError('Falta manifiesto verificado para el CSV: '+str(path))
    return {'provenance':'UNVERIFIED_TEST_ONLY','manifest':None,'sha256':actual,'source_coverage':'UNKNOWN'}


def load_candles(path: Path, symbol: str, *, allow_unverified=False) -> tuple[pd.DataFrame,dict]:
    if not re.fullmatch(r'[\w\u4e00-\u9fff]{2,50}',symbol):
        raise ValueError('Símbolo no válido')
    if not path.is_file():
        raise FileNotFoundError(path)
    meta = source_metadata(path,symbol,allow_unverified=allow_unverified)
    b = pd.read_csv(path,encoding='utf-8-sig',usecols=lambda c:c in FIELDS,
                    float_precision='round_trip',low_memory=False)
    if not set(FIELDS).issubset(b.columns) or len(b)<260:
        raise ValueError('CSV sin campos/longitud necesarios para HLL')
    for col in FIELDS:
        b[col] = pd.to_numeric(b[col],errors='raise')
    t=b.timestamp.to_numpy(dtype=np.int64)
    if (t % TF != 0).any() or (np.diff(t)!=TF).any():
        raise ValueError('Huecos, duplicados o desorden en 30m')
    o=b.open.to_numpy(dtype=float);h=b.high.to_numpy(dtype=float)
    l=b.low.to_numpy(dtype=float);c=b.close.to_numpy(dtype=float)
    v=b.volume.to_numpy(dtype=float)
    if (not all(np.isfinite(x).all() for x in (o,h,l,c,v)) or
        (np.minimum.reduce([o,h,l,c])<=0).any() or (v<0).any() or
        (h < np.maximum.reduce([o,l,c])).any() or
        (l > np.minimum.reduce([o,h,c])).any()):
        raise ValueError('OHLCV inválido')
    return b.sort_values('timestamp').reset_index(drop=True), meta


def _counts(events:pd.DataFrame, cutoff:int) -> dict:
    if events.empty:
        return {}
    e=events.loc[pd.to_numeric(events.observed_closed_ts)>=0]
    e=e.loc[e.observed_closed_ts.astype('int64')<=cutoff]
    return {str(k):Counter(v.event_type.tolist()) for k,v in e.groupby('level_id',sort=False)}


def score_asof(formations:pd.DataFrame, events:pd.DataFrame, ends:pd.DataFrame,
               bars:pd.DataFrame, cutoff:int) -> pd.DataFrame:
    """No facts later than *cutoff* contribute to tier, state, or proximity.

    Age/expiry are kept apart from importance. Scores/grade are provisional,
    explicitly fixed *before* any runner-outcome joining.
    """
    if cutoff % TF:
        raise ValueError('Corte no alineado 30m')
    available = bars.loc[bars.timestamp.astype('int64')+TF<=cutoff]
    if available.empty or cutoff>int(bars.timestamp.iloc[-1])+TF:
        raise ValueError('Corte fuera de OHLC cerrado disponible')
    candle=available.iloc[-1]
    if int(candle.timestamp)+TF!=cutoff:
        raise ValueError('Falta vela de cierre al corte')
    forms=formations.loc[formations.formation_closed_ts.astype('int64')<=cutoff]
    if forms.empty:
        return pd.DataFrame(columns=OUT_FIELDS)
    stats=_counts(events,cutoff)
    endings={str(r.level_id):r for r in ends.itertuples(index=False)} if not ends.empty else {}
    last_price=float(candle.close)
    result=[]
    for form in forms.itertuples(index=False):
        lv=str(form.level_id)
        counter=stats.get(lv,Counter())
        tests=counter['TEST_FROM_ABOVE']+counter['TEST_FROM_BELOW']
        defenses=counter['DEFENSE_CONFIRMED']
        losses=counter['LOSS_CONFIRMED']
        reconf=counter['LEVEL_RECONFIRMED']
        timeout=counter['TEST_TIMEOUT']
        finish=endings.get(lv)
        end_ts=(int(finish.end_closed_ts)
                if finish is not None and pd.notna(finish.end_closed_ts) else None)
        end_state=str(finish.end_state) if finish is not None else 'UNKNOWN_END'
        active=(end_ts is None or end_ts>=cutoff or end_state=='ACTIVE_AT_END')
        state=('ACTIVE' if active else 'GAP_CENSORED' if end_state=='GAP_CENSORED'
               else 'EXPIRED')
        last=min(cutoff,end_ts) if end_ts is not None and not active else cutoff
        age=max(0,(last-int(form.formation_closed_ts))/86_400_000)
        # Provisional transparent fixed weights, not fitted to runner outcomes.
        raw=(20 + min(8,tests)*4 + min(5,defenses)*7 + min(4,reconf)*6 +
             min(10,age*.5) - min(5,losses)*6 - min(4,timeout)*2 -
             (8 if str(form.quality_flag)=='WIDE_ZONE_REVIEW' else 0))
        score=int(max(0,min(100,round(raw))))
        tier='A' if score>=75 else 'B' if score>=55 else 'C' if score>=35 else 'D'
        atr=float(form.atr_at_formation)
        result.append(dict(level_id=lv,symbol=str(form.symbol),origin=str(form.origin),
            formation_closed_ts=int(form.formation_closed_ts),center=float(form.center),
            zone_low=float(form.zone_low),zone_high=float(form.zone_high),
            tier=tier,importance_score=score,score_formula='FIXED_EXPLORATORY_V1',
            state_at_cutoff=state,source_end_state=end_state,
            last_observed_closed_ts=int(last),tests_pre=tests,defenses_pre=defenses,
            losses_pre=losses,reconfirmations_pre=reconf,
            cross_up_pre=counter['CROSS_UP'],cross_down_pre=counter['CROSS_DOWN'],
            returns_pre=counter['RETURN_INSIDE'],age_days=round(age,2),
            distance_atr=round(abs(float(form.center)-last_price)/atr,3) if atr>0 else None,
            quality_flag=str(form.quality_flag)))
    return pd.DataFrame(result,columns=OUT_FIELDS).sort_values(
        ['importance_score','distance_atr','formation_closed_ts'],
        ascending=[False,True,False],kind='stable').reset_index(drop=True)


def collect_snapshot(frames,cutoff):
    forms,events,ends,bars=frames
    return score_asof(forms,events,ends,bars,cutoff)


def output_root(root:Path,tag:str) -> Path:
    if not re.fullmatch('[A-Za-z0-9_.-]{2,80}',tag):
        raise ValueError('Etiqueta de corrida inválida')
    return root/RUNS/tag


def build(root:Path,source:Path,symbol:str,tag:str,*,asof:int|None=None,
          min_free_gb:float=10,allow_unverified=False,plot_days:int=30,run=False) -> dict:
    source = source if source.is_absolute() else root/source
    bars,source_meta=load_candles(source,symbol,allow_unverified=allow_unverified)
    cutoff=int(bars.timestamp.iloc[-1])+TF if asof is None else int(asof)
    if cutoff>int(bars.timestamp.iloc[-1])+TF or cutoff<int(bars.timestamp.iloc[259])+TF:
        raise ValueError('Corte fuera de datos o con menos de 260 velas')
    selected=bars.loc[bars.timestamp+TF<=cutoff].copy()
    cfg=Config(max_age_bars=2880,max_active_per_origin=35)
    result=dict(version=VERSION,symbol=symbol,source=str(source.resolve()),
                source_sha256=source_meta['sha256'],source_provenance=source_meta['provenance'],
                source_manifest=source_meta['manifest'],source_first_utc=utc(int(selected.timestamp.iloc[0])),
                source_last_closed_utc=utc(cutoff),bars_30m=len(selected),
                chart_30m_days_available=round(len(selected)/48,2),
                requested_chart_days=plot_days,source_covers_requested_days=len(selected)>=plot_days*48,
                warmup_days_before_view=round(max(0,len(selected)/48-plot_days),2),
                method='HLL_V21_2x2_CAUSAL_TTL_EXTENDED_2880_30M',
                scores='EXPLORATORY_FIXED_NOT_VALIDATED',mode='PLAN_READ_ONLY' if not run else 'RUN_OFFLINE',
                output_dir=str(output_root(root,tag)))
    if not run:
        return result
    if shutil.disk_usage(root).free/(1024**3)<min_free_gb:
        raise RuntimeError('DISK_GUARD por debajo de mínimo establecido')
    target=output_root(root,tag)
    if target.exists():
        raise FileExistsError('La corrida ya existe; no sobreescribir: '+str(target))
    forms,events,ends,coverage=scan_symbol(symbol,selected,cfg,source=source_meta['sha256'])
    if coverage['gaps30'] or coverage['status']!='COMPLETE':
        raise ValueError('HLL con huecos o cobertura incompleta')
    f=pd.DataFrame(forms,columns=FORMATION_COLS)
    e=pd.DataFrame(events,columns=EVENT_COLS)
    x=pd.DataFrame(ends,columns=END_COLS)
    scores=score_asof(f,e,x,selected,cutoff)
    result.update(levels_detected=len(f),events_detected=len(e),
                  tier_counts={str(k):int(v) for k,v in scores.tier.value_counts().items()},
                  active_levels=int(scores.state_at_cutoff.eq('ACTIVE').sum()),
                  hll_coverage=coverage,cutoff_ms=cutoff)
    target.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent,prefix='.hll-hierarchy-v1-') as tmp:
        temp=Path(tmp)
        for name,df in [('candles_30m.csv',selected),('formations.csv',f),
                        ('events.csv',e),('ends.csv',x),('snapshot_asof.csv',scores)]:
            df.to_csv(temp/name,index=False)
        result['files_sha256']={n:sha(temp/n) for n in
             ('candles_30m.csv','formations.csv','events.csv','ends.csv','snapshot_asof.csv')}
        (temp/'manifest.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        temp.rename(target)
    return result


def parser(argv=None):
    p=argparse.ArgumentParser(description='HLL hierarchy; frozen 30m, read-only toward existing HLL/LIVE')
    g=p.add_mutually_exclusive_group(required=True)
    g.add_argument('--plan',action='store_true');g.add_argument('--run',action='store_true')
    p.add_argument('--root',type=Path,default=ROOT)
    p.add_argument('--source',type=Path,default=DEFAULT_SOURCE)
    p.add_argument('--symbol',default='BTCUSDT')
    p.add_argument('--run-tag',default='btc_hierarchy_pilot_v1')
    p.add_argument('--asof',help='Corte 30m cerrado, YYYY-MM-DDTHH:MM:SSZ')
    p.add_argument('--chart-days',type=int,choices=[3,7,14,21,30,60],default=30)
    p.add_argument('--min-free-gb',type=float,default=10)
    return p.parse_args(argv)


def main(argv=None):
    a=parser(argv)
    if a.min_free_gb<0:
        raise ValueError('Mínimo libre inválido')
    res=build(a.root,a.source,a.symbol,a.run_tag,asof=parse_utc(a.asof) if a.asof else None,
              min_free_gb=a.min_free_gb,plot_days=a.chart_days,run=a.run)
    print('HLL_HIERARCHY_'+('RESULT' if a.run else 'PLAN'),
          json.dumps(res,ensure_ascii=False,default=str),flush=True)
    return 0

if __name__=='__main__':
    raise SystemExit(main())
