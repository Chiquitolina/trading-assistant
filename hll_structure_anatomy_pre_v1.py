#!/usr/bin/env python3
"""HLL Anatomy PRE V1 - descriptive OFFLINE review of frozen reference attempts.

Reuses the existing HLL close-only, causally-confirmed reversal pivots. A pivot is
KNOWN only on its confirmation candle; its extreme timestamp is NOT a signal time.
Features see only candles with open_ts < PRE and close_ts <= PRE. Prior labels are
never accessed by feature calculation. No LIVE, POST, entries or quality score.

Run:
 python3 hll_structure_anatomy_pre_v1.py --plan --source-zip HLL_FUENTES_ANATOMIA_PRE_V2.zip
 python3 hll_structure_anatomy_pre_v1.py --run --source-zip HLL_FUENTES_ANATOMIA_PRE_V2.zip
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import io
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from zipfile import ZipFile

import hll_phase_reconstruction_pre as phase
import hll_structural_approach_ohlc_pre as ohlc

BAR_MS = 1_800_000
WINDOWS = (96, 288)
SCALES = (0.5, 1.0, 1.5)
DEFAULT_SOURCE = Path('HLL_FUENTES_ANATOMIA_PRE_V2.zip')
DEFAULT_OUT = Path('reports/horizontal_level_anatomy_pre_v1/reference_pilot_v1')

# Frozen reference-case coordinates. Labels and preferences live ONLY in the
# report function, not the detector or the metrics pipeline.
REFERENCES = {
    'ARCUSDT': ('7ed2cc0a24c0fc4267755bc4', '328221f526f41b4b36800608'),
    'BANDUSDT': ('5d99fc897c9f467821b436e5', '78bf48c6bab133aa1a6540f0'),
    'BICOUSDT': ('3aaaece417d98ecb3b7c2641', 'ee928730c5fe592c35974f85'),
    'AXLUSDT': ('fdc2bd7f86bea76d5bd3ab26', '7a3ea803cbdc471e7f54305b'),
    'WAXPUSDT': ('b81f52b9c63e1278ffa45160', 'f62d5e98bdc5d3034c0a3218'),
    'TREEUSDT': ('9cf4c99a82614cf3b5d955ab', 'd34d2484806a91f180a18cbf'),
    'PYTHUSDT': ('abfd300f237261a0374a11b4', 'd7c882e20340e45339fbc40a'),
    'ANKRUSDT': ('964bdaa745a87b2c49367c50', 'aac8e34a0d31b3ef424e77b8'),
    'BANUSDT': ('d5d2de8e059f94d354d903a4', '6290cc975c67ef2cebc183c8'),
    'AWEUSDT': ('a5896024a78792bff925d703', '04925d468168e9709dceaf0e'),
    'EVAAUSDT': ('4f76ee895c6882a08fd6cd75', '3f99421bd903a9afbdcb252c'),
    'BSVUSDT': ('083f070fbbc34c0a780470a8', '6eba98bdbd55eb5e81c6ca8a'),
    'FLOCKUSDT': ('a2e061e67b8af26734587456', '1ee404e8e1317327b18ebb02'),
    'TACUSDT': ('9a9930a90cd7cbc94c24122d', '71eca3c50cc83fe8ba6c3bdd'),
    'AUSDT': ('200630f6fffda143ce9fb960', '64a9fbc8ee4a804105f63c68'),
}
REVIEW = { # report-only subjective historical impressions, not used in feature code
 'ARCUSDT':('A','Reconstrucción favorable'),
 'BANDUSDT':('A','Compresión organizada'),
 'BICOUSDT':('A','Compresión / posible flip (no verificado)'),
 'AXLUSDT':('A−','Organización y conservación'),
 'WAXPUSDT':('A','Proximidad sostenida, mejor maduración al final'),
 'TREEUSDT':('A−/A','Segunda subida / reconstrucción'),
 'PYTHUSDT':('B+','Avance estructurado, menos comprimido'),
 'ANKRUSDT':('C','Lateral, poco atractivo'),
 'BANUSDT':('C','Mid-range, sin construcción convincente'),
 'AWEUSDT':('C','Poca claridad estructural'),
 'EVAAUSDT':('C+/B−','Posible reacción transitoria, no verificada'),
 'BSVUSDT':('B+/B','Recuperación que termina más lateral'),
 'FLOCKUSDT':('B/B+','Más favorable pero irregular'),
 'TACUSDT':('B−/C+','Madurez limitada'),
 'AUSDT':('B','Interesante pero poco definido'),
}

REQUIRED_MAP = ('attempt_id','symbol','level_id','pre_closed_ts','episode_id',
                'episode_first_attempt_id','episode_ordinal','zone_low','zone_high','atr_pre','status')


def sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_zip_csv(z:ZipFile, path:str):
    with z.open(path) as raw:
        with io.TextIOWrapper(raw, encoding='utf-8-sig', newline='') as stream:
            reader=csv.DictReader(stream)
            if not reader.fieldnames:
                raise ValueError('CSV sin cabecera: '+path)
            return list(reader)


def load_source(path:Path):
    if not path.is_file(): raise FileNotFoundError(path)
    with ZipFile(path) as z:
        all_names=set(z.namelist())
        inv=json.loads(z.read('INVENTARIO_RECOLECCION.json'))
        if inv.get('version')!='HLL_ANATOMIA_PRE_V2':
            raise ValueError('Paquete recolector incompatible')
        reference=inv.get('reference_cases') or {}
        for sym,(aid,eid) in REFERENCES.items():
            if reference.get(sym)!=[aid,eid]:
                raise ValueError('Identificador de referencia discrepante '+sym)
        mp='muestras_no_canonicas/attempt_episode_map_REFERENCIAS.csv'
        rows=read_zip_csv(z,mp)
        if any(not set(REQUIRED_MAP).issubset(r) for r in rows):
            raise ValueError('Columnas obligatorias ausentes en mapa de episodios')
        ids=set(); by_ep=defaultdict(list); selected=[]
        for r in rows:
            aid=r['attempt_id'];sym=r['symbol'];eid=r['episode_id']
            if aid in ids:raise ValueError('Attempt ID duplicado')
            ids.add(aid)
            if sym not in REFERENCES or eid != REFERENCES[sym][1]:
                raise ValueError('Intento fuera del conjunto de referencia')
            if sym in ('WAXPUSDT','BSVUSDT') and r['episode_first_attempt_id'] != REFERENCES[sym][0]:
                raise ValueError('Primera referencia incompatible '+sym)
            pre=int(r['pre_closed_ts']);ord_=int(r['episode_ordinal']);atr=float(r['atr_pre'])
            zl=float(r['zone_low']);zh=float(r['zone_high'])
            if (not 0 < ord_ or pre%BAR_MS or not all(math.isfinite(x) and x>0 for x in (atr,zl,zh))
                 or zl>zh):
                raise ValueError('Datos PRE/ATR/zona inválidos '+sym)
            rr={**r,'pre_closed_ts':pre,'episode_ordinal':ord_,'atr_pre':atr,
                'zone_low':zl,'zone_high':zh}
            by_ep[eid].append(rr);selected.append(rr)
        if len(by_ep)!=len(REFERENCES) or len(selected)!=36:
            raise ValueError(f'Esperados 15 episodios y 36 cortes, encontrados {len(by_ep)} y {len(selected)}')
        for sym,(aid,eid) in REFERENCES.items():
            seq=sorted(by_ep[eid],key=lambda r:r['episode_ordinal'])
            if sym in ('WAXPUSDT','BSVUSDT'):
                if seq[0]['attempt_id']!=aid or [r['episode_ordinal'] for r in seq]!=list(range(1,len(seq)+1)):
                    raise ValueError('Orden de cortes irregular '+sym)
            elif len(seq)!=1 or seq[0]['attempt_id']!=aid:
                raise ValueError('Corte de referencia no coincide '+sym)
            n=9 if sym=='WAXPUSDT' else 14 if sym=='BSVUSDT' else 1
            if len(seq)!=n:raise ValueError('Cantidad cortes inesperada '+sym)
            if any(b['pre_closed_ts']<=a['pre_closed_ts'] for a,b in zip(seq,seq[1:])):
                raise ValueError('Fechas no crecientes '+sym)
        reviewed=read_zip_csv(z,'muestras_no_canonicas/attempts_pre_REFERENCIAS.csv')
        if len(reviewed)!=15 or {r['attempt_id'] for r in reviewed}!={x[0] for x in REFERENCES.values()}:
            raise ValueError('Cortes revisados no coinciden con Discovery')
        for r in reviewed:
            ref=next(x for x in selected if x['attempt_id']==r['attempt_id'])
            if any(str(ref[k])!=r[k] for k in ('symbol','level_id','pre_closed_ts')):
                raise ValueError('Discovery/mapa no coincide para '+r['symbol'])
        candles={}
        for sym in REFERENCES:
            filename=f'velas/{sym}_30m.csv'
            if filename not in all_names:raise FileNotFoundError('Velas faltantes '+filename)
            with z.open(filename) as raw, io.TextIOWrapper(raw,encoding='utf-8-sig',newline='') as stream:
                reader=csv.DictReader(stream)
                # CSV `timestamp` is the start of the 30m bar; no guessing.
                schema=ohlc.schema_for(reader.fieldnames,timestamp_kind='open')
                if schema['time_kind']!='open':raise ValueError('Contrato de vela no open')
                data={}
                for r in reader:
                    bar=ohlc.decode_candle(r,schema)
                    if bar['open_ts'] in data:raise ValueError('Vela duplicada '+sym)
                    data[bar['open_ts']]=bar
                candles[sym]=data
    selected.sort(key=lambda r:(r['symbol'],r['pre_closed_ts'],r['attempt_id']))
    return selected,candles,dict(by_ep),sha_bytes(path.read_bytes())


def take_pre(candles,pre,n):
    b=[candles.get(pre-i*BAR_MS) for i in range(n,0,-1)]
    if any(x is None for x in b):return None
    if any(x['open_ts']!=pre-(n-i)*BAR_MS or x['close_ts']>pre for i,x in enumerate(b)):
        raise ValueError('Ventana PRE contiene vela futura o desalineada')
    return b


def quantile(v,q):
    if not v:return None
    s=sorted(v);i=(len(s)-1)*q;a=int(i)
    return s[a]*(1-(i-a))+s[min(a+1,len(s)-1)]*(i-a)


def ratio(a,b):return a/b if b and abs(b)>1e-12 else None


def residual_oscillation(bars,atr):
    """P90-P10 of detrended close residuals. Removes endpoint linear drift.
    This is a DESCRIPTOR, not a traded or causal online signal.
    """
    if len(bars)<2:return None
    first=bars[0]['close'];last=bars[-1]['close'];n=len(bars)-1
    resid=[b['close']-(first+(last-first)*i/n) for i,b in enumerate(bars)]
    return (quantile(resid,.9)-quantile(resid,.1))/atr


def path_efficiency(bars):
    if len(bars)<2:return None
    closes=[b['close'] for b in bars]
    return ratio(closes[-1]-closes[0],sum(abs(b-a) for a,b in zip(closes,closes[1:])))


def signed_pivot_stats(pivots,kind,atr):
    xs=[p for p in pivots if p['type']==kind]
    diffs=[(b['price']-a['price'])/atr for a,b in zip(xs[-3:],xs[-2:])]
    if len(xs)<3:return None,None,None
    return sum(v>0 for v in diffs),(xs[-1]['price']-xs[-3]['price'])/atr,\
        (xs[-1]['open_ts']-xs[-3]['open_ts'])/BAR_MS


def audit_snapshot(bars,meta,scale,window):
    """No labels, POST or other attempts are supplied to this function."""
    pre=int(meta['pre_closed_ts']);atr=float(meta['atr_pre'])
    if len(bars)!=window or bars[-1]['close_ts']!=pre:
        raise ValueError('Ventana PRE mal formada')
    pivots,terminal_direction,terminal_extreme=phase.close_reversal_pivots(bars,scale*atr)
    if any(p['known_closed_ts']>pre or p['open_ts']>=pre or p['open_ts']>p['known_closed_ts'] for p in pivots):
        raise ValueError('Lookahead en pivotes')
    lows=[p for p in pivots if p['type']=='LOW'];highs=[p for p in pivots if p['type']=='HIGH']
    low_up,low_net,low_span=signed_pivot_stats(pivots,'LOW',atr)
    high_up,high_net,high_span=signed_pivot_stats(pivots,'HIGH',atr)
    base={k:meta[k] for k in ('attempt_id','symbol','episode_id','level_id','pre_closed_ts','episode_ordinal')}
    base.update(window_bars=window,scale_atr=scale)
    out_piv=[];out_legs=[];out_chains=[]
    for i,p in enumerate(pivots):
        out_piv.append({**base,'pivot_ordinal':i+1,'pivot_type':p['type'],
            'pivot_open_ts':p['open_ts'],'pivot_price_close':p['price'],
            'confirmed_known_closed_ts':p['known_closed_ts'],
            'confirmation_delay_bars':p['confirmation_delay_bars'],
            'is_window_boundary':int(p['index']==0),'status':'CONFIRMED_PRE'})
    for i,(a,b) in enumerate(zip(pivots,pivots[1:]),1):
        if a['type']==b['type'] or a['index']>=b['index']:
            raise ValueError('Pivotes no alternantes / mal ordenados')
        seg=bars[a['index']:b['index']+1]
        delta=b['price']-a['price']
        out_legs.append({**base,'leg_ordinal':i,'direction':'UP' if delta>0 else 'DOWN',
            'start_pivot_ts':a['open_ts'],'start_confirmed_known_ts':a['known_closed_ts'],
            'end_pivot_ts':b['open_ts'],'end_confirmed_known_ts':b['known_closed_ts'],
            'amplitude_atr':abs(delta)/atr,'net_change_atr':delta/atr,
            'duration_bars':b['index']-a['index'],'signed_path_efficiency':path_efficiency(seg),
            'status':'ENDPOINT_PIVOT_CONFIRMED_PRE'})
    recovery=[];retracement=[]
    for i in range(len(pivots)-2):
        a,b,c=pivots[i:i+3]
        if [a['type'],b['type'],c['type']]!=['LOW','HIGH','LOW']:continue
        rise=b['price']-a['price']
        if rise>1e-12:
            recovery.append((c['price']-a['price'])/rise)
            retracement.append((b['price']-c['price'])/rise)
    for i in range(len(pivots)-3):
        a,b,c,d=pivots[i:i+4]
        if [a['type'],b['type'],c['type'],d['type']]!=['LOW','HIGH','LOW','HIGH']:continue
        rise1=b['price']-a['price'];rise2=d['price']-c['price']
        if rise1<=0 or rise2<=0:continue
        out_chains.append({**base,'chain_ordinal':len(out_chains)+1,
            'l1_ts':a['open_ts'],'h1_ts':b['open_ts'],'l2_ts':c['open_ts'],'h2_ts':d['open_ts'],
            'fully_known_by_ts':max(p['known_closed_ts'] for p in (a,b,c,d)),
            'first_rise_atr':rise1/atr,'second_rise_atr':rise2/atr,
            'correction_ratio':(b['price']-c['price'])/rise1,
            'low2_minus_low1_atr':(c['price']-a['price'])/atr,
            'high2_minus_high1_atr':(d['price']-b['price'])/atr,
            'high2_distance_to_zone_low_atr':(meta['zone_low']-d['price'])/atr,
            'age_since_h2_confirmed_bars':(pre-d['known_closed_ts'])/BAR_MS,
            'last_high_is_terminal':int(d is pivots[-1]),
            'status':'ALL_FOUR_PIVOTS_CONFIRMED_PRE'})
    last_low=lows[-1] if lows else None
    last_close=bars[-1]['close'];zl=meta['zone_low'];zh=meta['zone_high']
    recent24=bars[-24:];prev24=bars[-48:-24]
    osc_recent=residual_oscillation(recent24,atr)
    osc_prev=residual_oscillation(prev24,atr)
    near=sum(zl-.5*atr<=b['close']<=zh for b in bars[-48:])/48
    trend48=path_efficiency(bars[-48:]);trend24=path_efficiency(recent24)
    highs_=[b['high'] for b in bars];lows_=[b['low'] for b in bars]
    price_span=max(highs_)-min(lows_)
    summ={**base, 'status':'COMPLETE_PRE',
        'pivots_confirmed':len(pivots),'lows_confirmed':len(lows),'highs_confirmed':len(highs),
        'low_up_steps_last3':low_up,'low_net_progress_last3_atr':low_net,
        'low_span_last3_bars':low_span,
        'high_up_steps_last3':high_up,'high_net_progress_last3_atr':high_net,
        'high_span_last3_bars':high_span,
        'confirmed_lhl_count':len(recovery),'confirmed_lhlh_count':len(out_chains),
        'median_lhl_retention_fraction':median(recovery) if recovery else None,
        'latest_lhl_retention_fraction':recovery[-1] if recovery else None,
        'median_lhl_retracement_fraction':median(retracement) if retracement else None,
        'latest_lhl_retracement_fraction':retracement[-1] if retracement else None,
        'latest_lhlh_low_step_atr':out_chains[-1]['low2_minus_low1_atr'] if out_chains else None,
        'latest_lhlh_high_step_atr':out_chains[-1]['high2_minus_high1_atr'] if out_chains else None,
        'last_low_known_age_bars':(pre-last_low['known_closed_ts'])/BAR_MS if last_low else None,
        'last_low_to_close_lift_atr':(last_close-last_low['price'])/atr if last_low else None,
        'signed_efficiency_24':trend24,'signed_efficiency_48':trend48,
        'residual_osc_24_recent_atr':osc_recent,
        'residual_osc_24_previous_atr':osc_prev,
        'residual_osc_ratio_recent_vs_prev24':ratio(osc_recent,osc_prev),
        'near_resistance_share_48':near,
        'zone_position_in_window_range':ratio((zl+zh)/2-min(lows_),price_span),
        'close_distance_to_zone_low_atr':(zl-last_close)/atr,
        'max_low_below_zone_48_atr':max(0,(zl-min(b['low'] for b in bars[-48:]))/atr),
        'terminal_leg_direction':('UP' if terminal_direction>0 else 'DOWN' if terminal_direction<0 else 'UNRESOLVED'),
        'terminal_leg_is_provisional':bool(pivots),
        'last_pivot_known_before_pre':bool(pivots and pivots[-1]['known_closed_ts']<=pre),
        'latest_chain_fully_confirmed':bool(out_chains),
    }
    return summ,out_piv,out_legs,out_chains


def collect(rows,candles):
    summaries=[];pivots=[];legs=[];chains=[];missing=[];cache={}
    for r in rows:
        sym=r['symbol'];pre=r['pre_closed_ts']
        for window in WINDOWS:
            bars=take_pre(candles[sym],pre,window)
            if bars is None:
                missing.append({'attempt_id':r['attempt_id'],'symbol':sym,
                    'window_bars':window,'status':'INCOMPLETE_PRE'})
                continue
            cache[(r['attempt_id'],window)]=bars
            for scale in SCALES:
                summ,ps,ls,cs=audit_snapshot(bars,r,scale,window)
                summaries.append(summ);pivots.extend(ps);legs.extend(ls);chains.extend(cs)
    return summaries,pivots,legs,chains,missing,cache


def write_csv(path,rows):
    if not rows:return
    cols=list(rows[0])
    with path.open('x',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=cols,extrasaction='raise');w.writeheader();w.writerows(rows)


def fmt(value,dec=2):
    if value is None or value=='':return '—'
    if isinstance(value,str):return html.escape(value)
    return f'{value:.{dec}f}' if isinstance(value,(int,float)) and not isinstance(value,bool) else str(value)


def tsutc(t):return datetime.fromtimestamp(int(t)/1000,timezone.utc).strftime('%Y-%m-%d %H:%M UTC')


def svg_graph(bars,meta,pivots,window):
    """Read-only inline SVG close path, zone and PRE-confirmed pivots, no JS."""
    w,h=940,240;pad=35
    values=[b['close'] for b in bars]+[meta['zone_low'],meta['zone_high']]
    mn=min(values);mx=max(values);rng=mx-mn or mx*.01
    mn-=rng*.04;mx+=rng*.04;rng=mx-mn
    xpos=lambda i: pad+i*(w-2*pad)/(len(bars)-1)
    ypos=lambda v: pad+(mx-v)*(h-2*pad)/rng
    points=' '.join(f'{xpos(i):.1f},{ypos(b["close"]):.1f}' for i,b in enumerate(bars))
    zonelo=ypos(meta['zone_low']);zonehi=ypos(meta['zone_high'])
    ids={b['open_ts']:i for i,b in enumerate(bars)}
    circles=[]
    for p in pivots:
        if p['pivot_open_ts'] not in ids:continue
        i=ids[p['pivot_open_ts']];v=p['pivot_price_close'];low=p['pivot_type']=='LOW'
        col='#16a394' if low else '#de934e'
        circles.append(f'<circle cx="{xpos(i):.1f}" cy="{ypos(v):.1f}" r="3.0" fill="{col}"><title>{p["pivot_type"]} pivot {tsutc(p["pivot_open_ts"])} known {tsutc(p["confirmed_known_closed_ts"])}</title></circle>')
    return (f'<svg viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="PRE close chart with confirmed pivots">'
      f'<rect width="{w}" height="{h}" fill="#101720"/>'
      f'<rect x="{pad}" y="{zonehi:.1f}" width="{w-2*pad}" height="{max(1,zonelo-zonehi):.1f}" fill="#d7a64c" opacity=".17"/>'
      f'<line x1="{pad}" x2="{w-pad}" y1="{zonelo:.1f}" y2="{zonelo:.1f}" stroke="#d7a64c" stroke-dasharray="6 4"/>'
      f'<line x1="{pad}" x2="{w-pad}" y1="{zonehi:.1f}" y2="{zonehi:.1f}" stroke="#d7a64c" stroke-dasharray="6 4"/>'
      f'<polyline fill="none" stroke="#6fc0e8" stroke-width="1.5" points="{points}"/>'
      +''.join(circles)
      +f'<text x="{pad}" y="20" fill="#b9cbd6" font-size="12">{window} × 30m PRE · Cierre · Verde LOW / Naranja HIGH · resistencia sombreada</text>'
      +f'<text x="{w-pad}" y="{h-9}" fill="#8395a2" font-size="11" text-anchor="end">Corte {tsutc(meta["pre_closed_ts"])}</text></svg>')


def report_html(rows,summaries,pivots,cache):
    """Labels introduced here ONLY, after metrics have been calculated."""
    byid={r['attempt_id']:r for r in rows}
    ss={(r['attempt_id'],r['window_bars'],r['scale_atr']):r for r in summaries}
    pp=defaultdict(list)
    for p in pivots:
        if p['window_bars']==288 and p['scale_atr']==1.0:pp[p['attempt_id']].append(p)
    style='''<style>body{background:#0b1118;color:#e7ebee;font:15px system-ui;margin:30px auto;max-width:1160px;line-height:1.48}h1,h2{color:#eaf5ff}h2{margin-top:38px}article{border:1px solid #34404c;border-radius:10px;margin:20px 0;padding:17px;background:#131e27}table{border-collapse:collapse;width:100%;margin:15px 0;font-size:13px}td,th{border-bottom:1px solid #34404c;padding:9px;text-align:left}th{color:#aed4e2}code{color:#d2c590}small{color:#b1bfca}.sub{color:#a4bac8}svg{max-width:100%}.warn{border-left:4px solid #cf9b4e;padding:12px;background:#1b2227}</style>'''
    doc=['<!doctype html><html lang="es"><meta charset="utf-8"><title>HLL - Auditoría anatomía PRE V1</title>',style,'<body><h1>HLL — Anatomía estructural PRE · piloto 15 episodios</h1>','<p>36 cortes exportados, 15 símbolos. En 13 episodios hay un único corte revisado que no necesariamente es el primero; WAXP (9) y BSV (14) tienen seguimiento completo. 96/288 velas; sensibilidad 0.5, 1.0 y 1.5 ATR. Este informe ilustra <strong>descriptores</strong>, no un score ni señales de entrada.</p>',
      '<p class="warn">Los pivotes son giros de <strong>cierre</strong> conocidos tras una reversión en ATR. La ubicación de su vela extrema no es su fecha de confirmación. Una estructura en 288 velas puede diferir de la de 96 por inicialización. Las notas A/B/C son visuales, no etiquetas usadas por el algoritmo. Las velas son históricas, pero cada gráfico está cortado estrictamente en PRE, sin POST.</p>']
    doc.append('<h2>Referencias en su corte revisado · 96 PRE / 1 ATR</h2><table><thead><tr><th>Símbolo</th><th>Valoración previa</th><th>LOW ↑ últimos 3</th><th>Retención L-H-L última</th><th>Eficiencia 48</th><th>Oscilación residual 24</th><th>Ubicación zona 288</th></tr></thead><tbody>')
    for sym in REFERENCES:
        a,e=REFERENCES[sym];r=ss[(a,96,1.0)];q=ss[(a,288,1.0)];label=REVIEW[sym][0]
        doc.append('<tr>'+''.join(f'<td>{fmt(x)}</td>' for x in (sym,label,r['low_up_steps_last3'],r['latest_lhl_retention_fraction'],r['signed_efficiency_48'],r['residual_osc_24_recent_atr'],q['zone_position_in_window_range']))+'</tr>')
    doc.append('</tbody></table><small>Retención L-H-L = (mínimo posterior − mínimo anterior) / subida previa. Puede ser negativa o &gt;1. Eficiencia 48 = avance neto / recorrido absoluto. Ubicación 288 = rango normalizado, no fuerza validada de resistencia.</small>')
    for sym in REFERENCES:
        aid,eid=REFERENCES[sym];meta=byid[aid];s96=ss[(aid,96,1.0)];s288=ss[(aid,288,1.0)]
        doc.append('<article>')
        doc.append(f'<h2>{html.escape(sym)} · {html.escape(REVIEW[sym][0])} · {html.escape(REVIEW[sym][1])}</h2>')
        doc.append(f'<p><small>Attempt <code>{aid}</code> · Episodio <code>{eid}</code> · Corte {tsutc(meta["pre_closed_ts"])} · Solo PRE</small></p>')
        doc.append(svg_graph(cache[(aid,288)],meta,pp[aid],288))
        doc.append('<table><thead><tr><th>Parámetro</th><th>96 PRE</th><th>288 PRE</th></tr></thead><tbody>')
        for k,label in [('pivots_confirmed','Pivotes confirmados'),('lows_confirmed','Mínimos confirmados'),('low_up_steps_last3','LOW crecientes (2 últimos pasos)'),('low_net_progress_last3_atr','Progreso últimos 3 LOW (ATR)'),('latest_lhl_retention_fraction','Conservación último L-H-L'),('signed_efficiency_48','Eficiencia 48'),('residual_osc_24_recent_atr','Oscilación residual 24 (ATR)'),('residual_osc_ratio_recent_vs_prev24','Ratio oscilación residual 24'),('near_resistance_share_48','Persistencia cerca de zona (48)'),('zone_position_in_window_range','Ubicación resistencia en rango')]:
            doc.append(f'<tr><td>{label}</td><td>{fmt(s96[k])}</td><td>{fmt(s288[k])}</td></tr>')
        doc.append('</tbody></table></article>')
    for sym in ('WAXPUSDT','BSVUSDT'):
        arr=sorted((r for r in rows if r['symbol']==sym),key=lambda r:r['episode_ordinal'])
        doc.append(f'<h2>Evolución histórica · {sym} · {len(arr)} cortes</h2>')
        doc.append('<p>La fila de cada corte utiliza <em>solo las velas anteriores a ese corte</em>. Comparar las filas es retrospectivo; el corte #1 no conoce futuros intentos.</p>')
        doc.append('<table><thead><tr><th>Corte</th><th>PRE (UTC)</th><th>LOW ↑</th><th>LOW neto ATR</th><th>Retención LHL</th><th>Eficiencia48</th><th>Oscilación residual24</th><th>Cerca zona48</th></tr></thead><tbody>')
        for r in arr:
            q=ss[(r['attempt_id'],96,1.0)]
            values=(r['episode_ordinal'],tsutc(r['pre_closed_ts']),q['low_up_steps_last3'],q['low_net_progress_last3_atr'],q['latest_lhl_retention_fraction'],q['signed_efficiency_48'],q['residual_osc_24_recent_atr'],q['near_resistance_share_48'])
            doc.append('<tr>'+''.join(f'<td>{fmt(v)}</td>' for v in values)+'</tr>')
        doc.append('</tbody></table>')
    doc.append('<h2>Qué NO concluye este piloto</h2><p>Los favoritos no prueban capacidad predictiva; las escalas ATR están congeladas al corte PRE y las fechas de pivote reflejan el conocimiento del extremo con ese parámetro de corte. No hay resultado POST, validación fuera de muestra, ni fórmula de clasificación por calidad. Este informe debe usarse para inspeccionar si la medición describe bien las organizaciones antes de escalar al universo de 290.</p></body></html>')
    return ''.join(doc)


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    g=p.add_mutually_exclusive_group(required=True);g.add_argument('--plan',action='store_true');g.add_argument('--run',action='store_true')
    p.add_argument('--source-zip',type=Path,default=DEFAULT_SOURCE)
    p.add_argument('--output-dir',type=Path,default=DEFAULT_OUT)
    args=p.parse_args(argv)
    rows,candles,episodes,sourcehash=load_source(args.source_zip)
    missing=[]
    for r in rows:
        for window in WINDOWS:
            if take_pre(candles[r['symbol']],r['pre_closed_ts'],window) is None:
                missing.append((r['attempt_id'],window))
    planned={'version':'HLL_ANATOMY_PRE_V1','references':len(REFERENCES),
      'attempts':len(rows),'episodes':len(episodes),'scales':SCALES,'windows':WINDOWS,
      'missing_pre_windows':missing,'reads_post':False,'modifies_live':False,
      'uses_visual_labels_for_features':False,'source_zip_sha256':sourcehash,
      'output':str(args.output_dir)}
    print('HLL_ANATOMY_PLAN',json.dumps(planned,ensure_ascii=False))
    if args.plan:return 0
    if args.output_dir.exists():raise FileExistsError(f'Salida ya existe, no sobrescribimos: {args.output_dir}')
    summaries,pivots,legs,chains,missing,cache=collect(rows,candles)
    if missing:raise ValueError(f'Faltan ventanas PRE en piloto: {missing[:5]}')
    # Compute in memory first. Write only new reports, never source or LIVE.
    args.output_dir.mkdir(parents=True,exist_ok=False)
    files={'summary_pre.csv':summaries,'pivots_pre.csv':pivots,
           'legs_pre.csv':legs,'chains_pre.csv':chains}
    for filename,data in files.items():write_csv(args.output_dir/filename,data)
    htmlfile=args.output_dir/'structure_audit.html'
    htmlfile.write_text(report_html(rows,summaries,pivots,cache),encoding='utf-8')
    manifest={**planned,'status':'DONE',
       'summary_rows':len(summaries),'pivot_rows':len(pivots),
       'leg_rows':len(legs),'chain_rows':len(chains),
       'lineage':'Reused HLL close_reversal_pivots; frozen reference episode map',
       'window_contract':'Only closed 30m bars strictly before each PRE cut',
       'labels':'Report-only, never passed to audit_snapshot/collect',
       'pivots_are':'close reversal with explicit confirmation time (not online event at extreme)',
       'output_hashes':{name:sha_bytes((args.output_dir/name).read_bytes()) for name in (*files,'structure_audit.html')}}
    (args.output_dir/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    print('HLL_ANATOMY_DONE',json.dumps({k:manifest[k] for k in ('status','attempts','summary_rows','pivot_rows','leg_rows','chain_rows','output')},ensure_ascii=False))
    return 0

if __name__=='__main__':raise SystemExit(main())
