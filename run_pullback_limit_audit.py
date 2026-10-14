#!/usr/bin/env python3
"""Research-only limit-entry pullback simulation for frozen Continuous V1 cohort.

Read-only SQLite, frozen 1,311 COMPLETE/360m paths. Does not modify production.
Do NOT use future MAE as an entry decision: place predeclared pending limit levels.
Conservative assumptions: limit filled at its limit (not improved gap/open), same-minute
TP after limit fill is not credited (OHLC order unknown); SL touch is counted.
Each filled trade receives 180 full minutes from its fill, within validated 360m data.
No portfolio slot, funding, spread, slippage, exchange order rejection, or latency.
"""
from __future__ import annotations
import argparse, csv, math, sqlite3, sys
from pathlib import Path
from collections import Counter, defaultdict

MINUTE=60000
DEPTHS=(0.5,1.0,1.5,2.0,2.5,3.0)
TTLS=(5,15,30,60)
SCENARIOS=((2.,0.5),(2.,1.),(2.,1.5),(3.,1.),(3.,1.5),(3.,2.))
FIELDS_DETAIL=['event_key','symbol','side','group','classification','depth_pct','ttl_min','scenario','entry_open','limit_price','filled','fill_minute','fill_timestamp','result','exit_minute_from_fill','exit_timestamp','gross_pct','net_pct','fill_bar_tp_possible_before_fill','original_result','original_net_pct']

def number(s):
    try:
        n=float(s)
        return n if math.isfinite(n) else None
    except (ValueError,TypeError,OverflowError):return None

def selected(row):
    return all(number(row.get(k)) is not None and number(row[k])>=x for k,x in
        [('max_departure_pct',1.5),('opposing_room_1h_pct',2.),('volume_rank_pct_4h',60.),('return_rank_pct_4h',60.)])

def group(row):
    return '4_FACTORS' if selected(row) else ('REST_ROOM_AVAILABLE' if number(row.get('opposing_room_1h_pct')) is not None else 'ROOM_MISSING')

def label(tp,sl):return f'TP{tp:g}_SL{sl:g}'

def limit_fill(path,side,depth_pct,ttl):
    """A resting buy below (or sell above) original next-open; touch -> assumed fill.
       Fill at limit even if market gapped past it (conservative price)."""
    assert side in ('LONG','SHORT')
    open0=float(path[0][1])
    level=open0*(1-depth_pct/100.) if side=='LONG' else open0*(1+depth_pct/100.)
    for i,(_,op,hi,lo,close) in enumerate(path[:ttl]):
        if (side=='LONG' and float(lo)<=level) or (side=='SHORT' and float(hi)>=level):
            return i,level
    return None,level

def simulate_at_fill(path,side,fill_idx,entry,tp,sl,fee=.10,horizon=180):
    assert fill_idx is not None and fill_idx>=0
    assert fill_idx+horizon<=len(path)
    sign=1 if side=='LONG' else -1
    uncertainty=False
    result='NO_HIT';end_idx=fill_idx+horizon-1
    for idx in range(fill_idx,fill_idx+horizon):
        ts,op,hi,lo,close=path[idx]
        gain=100.*(float(hi)/entry-1.) if sign==1 else 100.*(1.-float(lo)/entry)
        adverse=100.*(1.-float(lo)/entry) if sign==1 else 100.*(float(hi)/entry-1.)
        # In the fill minute, extrema sequence is unknowable; a TP could be BEFORE entry.
        # Never credit that TP. Do count SL as the low/high beyond the entry threshold
        # necessarily involves passage through the resting limit (assuming a fill).
        if idx==fill_idx and gain>tp:uncertainty=True
        if adverse>=sl:
            result=('AMBIGUOUS_SL_FIRST' if gain>tp and idx!=fill_idx else 'SL_FIRST')
            end_idx=idx;break
        if idx==fill_idx:continue
        if gain>tp:
            result='TP_FIRST';end_idx=idx;break
    if result=='TP_FIRST':gross=tp
    elif result in ('SL_FIRST','AMBIGUOUS_SL_FIRST'):gross=-sl
    else:gross=100.*sign*(float(path[end_idx][4])/entry-1.)
    return {'result':result,'exit_minute_from_fill':end_idx-fill_idx+1,
            'exit_timestamp':int(path[end_idx][0]),'gross_pct':gross,'net_pct':gross-fee,
            'fill_bar_tp_possible_before_fill':uncertainty}

def original_sim(path,side,tp,sl,fee=.10,horizon=180):
    """Legacy original next OPEN baseline: credited TP within initial candle."""
    entry=float(path[0][1]);sign=1 if side=='LONG' else -1
    for idx,(_,o,h,l,c) in enumerate(path[:horizon]):
        gain=100.*(float(h)/entry-1.) if sign==1 else 100.*(1.-float(l)/entry)
        adverse=100.*(1.-float(l)/entry) if sign==1 else 100.*(float(h)/entry-1.)
        if gain>tp and adverse>=sl:return ('AMBIGUOUS_SL_FIRST',-sl-fee)
        if adverse>=sl:return ('SL_FIRST',-sl-fee)
        if gain>tp:return ('TP_FIRST',tp-fee)
    return ('NO_HIT',100.*sign*(float(path[horizon-1][4])/entry-1.)-fee)

class Aggregate:
    def __init__(self):
        self.n=0;self.c=Counter();self.net=0.;self.pos=0.;self.neg=0.
        self.orig_net=0.;self.uncertain=0;self.fillmins=[]
    def add(self,d,orig_net):
        self.n+=1;self.c[d['result']]+=1;v=d['net_pct'];self.net+=v
        self.pos+=max(v,0.);self.neg+=max(-v,0.);self.orig_net+=orig_net
        self.uncertain+=bool(d['fill_bar_tp_possible_before_fill'])
        self.fillmins.append(d['fill_minute'])
    def summarize(self,possible):
        ordered=sorted(self.fillmins)
        return {'possible_signals':possible,'filled':self.n,'unfilled':possible-self.n,
                'fill_pct':round(100*self.n/possible,3) if possible else '',
                'tp':self.c['TP_FIRST'],'sl':self.c['SL_FIRST'],'amb':self.c['AMBIGUOUS_SL_FIRST'],
                'no_hit':self.c['NO_HIT'],'fill_bar_tp_uncertain':self.uncertain,
                'tp_pct_per_fill':round(100*self.c['TP_FIRST']/self.n,3) if self.n else '',
                'net_avg_per_fill_pct':round(self.net/self.n,6) if self.n else '',
                'net_avg_per_all_signals_pct':round(self.net/possible,6) if possible else '',
                'matched_original_avg_net_pct':round(self.orig_net/self.n,6) if self.n else '',
                'paired_delta_avg_pct':round((self.net-self.orig_net)/self.n,6) if self.n else '',
                'profit_factor':round(self.pos/self.neg,5) if self.neg else ('INF' if self.pos else ''),
                'median_fill_min':ordered[len(ordered)//2] if ordered else ''}

def load_paths(args,records):
    db=Path(args.project)/'reports/reaction_lab/auto_paths_v1.sqlite3'
    if not db.is_file():raise RuntimeError(f'Path DB not found: {db}')
    sys.path.insert(0,str(Path(args.project).resolve()))
    from reaction_lab_auto_paths import decode_path
    from reaction_lab_deep_metrics import forward_metrics
    rows={}
    with sqlite3.connect('file:'+db.resolve().as_posix()+'?mode=ro',uri=True) as con:
        for i in range(0,len(records),250):
            keys=[r['event_key'] for r in records[i:i+250]]
            q=','.join('?' for _ in keys)
            for z in con.execute('SELECT event_key,symbol,side,retest_ts,entry_ts,state,count,path_blob FROM events WHERE event_key IN ('+q+')',keys):
                rows[z[0]]=z
    paths={};issues=[]
    for r in records:
        k=r['event_key'];rec=rows.get(k)
        if not rec:issues.append((k,'MISSING'));continue
        key,sym,side,retest,entry,state,n,blob=rec
        if (sym!=r['symbol'] or side!=r['signal'] or int(retest)!=int(r['retest_timestamp']) or
            int(entry)!=int(r['known_ts']) or state!='COMPLETE' or int(n)!=360):
            issues.append((k,'BAD_METADATA'));continue
        try:
            p=decode_path(blob)
            if len(p)!=360:raise ValueError('wrong path len')
            fm=forward_metrics(p,side,float(r['retest_close']),int(retest))
            if fm.get('valid_path_minutes')!=360:raise ValueError('not continuous path')
            if not math.isclose(float(p[0][1]),float(r['entry_price']),rel_tol=1e-9,abs_tol=1e-12):
                raise ValueError('entry open mismatch')
            paths[k]=p
        except Exception as e:issues.append((k,str(e)))
    if issues:raise RuntimeError(f'{len(issues)} invalid paths; first {issues[:2]}')
    return paths

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--project',default='/home/multiservices-1m')
    ap.add_argument('--csv',default=str(Path(__file__).with_name('runner_matrix_cohort_2026_10_14.csv')))
    ap.add_argument('--diagnostics',default=str(Path(__file__).with_name('selected_72_diagnostics.csv')))
    ap.add_argument('--out',default='/root/pullback_entry_results_20261014')
    ap.add_argument('--fee',type=float,default=.10,help='Total round trip commission in %% notional')
    args=ap.parse_args()
    if not (0<=args.fee<=5):ap.error('fee must be 0-5%')
    with open(args.csv,newline='',encoding='utf-8-sig') as f:records=list(csv.DictReader(f))
    if len(records)!=1311 or sum(selected(r) for r in records)!=72:
        ap.error('Frozen cohort must contain exactly 1311 COMPLETE and 72 4-factor events')
    if len({r['event_key'] for r in records})!=len(records):ap.error('duplicate event_key')
    for r in records:
        if r['path_state']!='COMPLETE' or r['event_key']!=f"{r['symbol']}|{r['signal']}|{int(r['retest_timestamp'])}":
            ap.error('cohort mismatch '+r['event_key'])
        if int(r['known_ts'])!=int(r['retest_timestamp'])+MINUTE:ap.error('known_ts mismatch')
    with open(args.diagnostics,newline='',encoding='utf-8-sig') as f:
        classes={r['event_key']:r['classification'] for r in csv.DictReader(f)}
    if len(classes)!=72:ap.error('diagnostic classifications must cover 72 distinct events')
    paths=load_paths(args,records)
    baseline_counter=Counter(original_sim(paths[r['event_key']],r['signal'],2.,1.,args.fee)[0]
                             for r in records if selected(r))
    if (baseline_counter['TP_FIRST'],baseline_counter['SL_FIRST'],baseline_counter['NO_HIT'])!=(18,49,5):
        ap.error('Baseline original 72 TP/SL parity mismatch: '+repr(baseline_counter))
    counts=Counter(group(r) for r in records)
    counts['ALL_COMPLETE']=len(records)
    for c in ('RUNNER_TP_FIRST','RUNNER_SL_FIRST','SELECTED_NO_RUNNER'):
        counts['4F_'+c]=sum(v==c for v in classes.values())
    aggs=defaultdict(Aggregate)
    out=Path(args.out).resolve();out.mkdir(parents=True,exist_ok=True)
    details_path=out/'selected_72_limit_entries.csv'
    with details_path.open('w',newline='',encoding='utf-8') as dst:
        writer=csv.DictWriter(dst,fieldnames=FIELDS_DETAIL)
        writer.writeheader()
        for r in records:
            key=r['event_key'];p=paths[key];g=group(r);side=r['signal']
            classification=classes.get(key,'')
            orig={label(tp,sl):original_sim(p,side,tp,sl,args.fee) for tp,sl in SCENARIOS}
            for ttl in TTLS:
                for depth in DEPTHS:
                    fill_idx,level=limit_fill(p,side,depth,ttl)
                    if fill_idx is None:continue
                    for tp,sl in SCENARIOS:
                        sc=label(tp,sl)
                        v=simulate_at_fill(p,side,fill_idx,level,tp,sl,args.fee)
                        d={'event_key':key,'symbol':r['symbol'],'side':side,'group':g,
                           'classification':classification,'depth_pct':depth,'ttl_min':ttl,'scenario':sc,
                           'entry_open':float(p[0][1]),'limit_price':level,'filled':True,
                           'fill_minute':fill_idx+1,'fill_timestamp':int(p[fill_idx][0]),
                           'original_result':orig[sc][0],'original_net_pct':orig[sc][1],**v}
                        group_names=[g,'ALL_COMPLETE']
                        if g=='4_FACTORS':group_names += ['4F_'+classification]
                        for gg in group_names:aggs[(gg,ttl,depth,sc)].add(d,orig[sc][1])
                        if g=='4_FACTORS':writer.writerow(d)
    summary=[]
    for gg in ['4_FACTORS','4F_RUNNER_TP_FIRST','4F_RUNNER_SL_FIRST','4F_SELECTED_NO_RUNNER','REST_ROOM_AVAILABLE','ALL_COMPLETE']:
        for ttl in TTLS:
            for depth in DEPTHS:
                for tp,sl in SCENARIOS:
                    sc=label(tp,sl);stat=aggs[(gg,ttl,depth,sc)].summarize(counts[gg])
                    summary.append({'group':gg,'ttl_min':ttl,'depth_pct':depth,'scenario':sc,**stat})
    fields=['group','ttl_min','depth_pct','scenario','possible_signals','filled','unfilled','fill_pct',
            'tp','sl','amb','no_hit','fill_bar_tp_uncertain','tp_pct_per_fill','net_avg_per_fill_pct',
            'net_avg_per_all_signals_pct','matched_original_avg_net_pct','paired_delta_avg_pct',
            'profit_factor','median_fill_min']
    with (out/'limit_matrix_summary.csv').open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(summary)
    with (out/'README.txt').open('w',encoding='utf-8') as f:
        f.write(__doc__+'\nImportant: all rules are in-sample research, not prospective validated signals.\n')
    print('PATHS',len(paths),'| 4F',counts['4_FACTORS'],'| BASELINE',dict(baseline_counter),'| ISSUES 0')
    for depth in DEPTHS:
        for ttl in (15,30):
            for sc in ('TP2_SL1','TP3_SL1'):
                s=next(x for x in summary if x['group']=='4_FACTORS' and x['ttl_min']==ttl
                       and x['depth_pct']==depth and x['scenario']==sc)
                print(f"4F DIP{depth:g}% TTL{ttl} {sc}: filled {s['filled']}/72 TP {s['tp']} SL {s['sl']} NO_HIT {s['no_hit']} uncertain {s['fill_bar_tp_uncertain']} netavg/fill {s['net_avg_per_fill_pct']} PF {s['profit_factor']}")
    print('OUTPUT:',out)

if __name__=='__main__':main()
