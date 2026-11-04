#!/usr/bin/env python3
"""READ-ONLY consolidation of independently audited multitimeframe symbol outputs.

Never promotes exploratory 30m/1h comparisons to model selection or LIVE.
Requires each input's companion manifest and matching file hashes/paths.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path

import pandas as pd

import audit_reaction_multitf_v1 as m


def pool_paths(paths):
    seen=set();events=[];contexts=[];records=[]
    for p in paths:
        path=Path(p).resolve()
        if not path.is_file():raise FileNotFoundError(path)
        symbol=path.name.split('_multitf_events.csv')[0]
        if not path.name.endswith('_multitf_events.csv') or not symbol:
            raise ValueError('Expected *_multitf_events.csv path')
        if symbol in seen:raise ValueError('Duplicate symbol in pooled cohorts: '+symbol)
        seen.add(symbol)
        manifest_path=path.parent/(symbol+'_multitf_manifest.json')
        meta=json.loads(manifest_path.read_text(encoding='utf8'))
        if meta.get('version')!=m.VERSION or meta.get('symbol')!=symbol:
            raise ValueError('Wrong manifest version/symbol for '+symbol)
        if meta.get('15m_overlay_exact_parity') is None:
            raise ValueError('Missing mandatory original 15m parity')
        for key,expected in [('events',path),
                             ('context',path.parent/(symbol+'_htf_context_15m_events.csv'))]:
            if Path(meta['outputs'][key]).resolve()!=expected:
                raise ValueError('Manifest output path differs from supplied '+key+' path')
        for key in ('events','summary','context','context_summary'):
            expected_path=Path(meta['outputs'][key]).resolve()
            if m.file_sha256(expected_path)!=meta['output_sha256'][key]:
                raise ValueError('CSV checksum mismatch: '+str(expected_path))
        e=pd.read_csv(path)
        c=pd.read_csv(path.parent/(symbol+'_htf_context_15m_events.csv'))
        if len(e)!=sum(sum(int(n) for n in counts.values()) for counts in meta['first_touches_by_timeframe_status'].values()):
            raise ValueError('Direct-event count does not match manifest: '+symbol)
        if len(c)!=int(meta['15m_overlay_exact_parity']['first_touches']):
            raise ValueError('15m context count does not match manifest: '+symbol)
        if len(e) and (e['symbol'].astype(str)!=symbol).any():
            raise ValueError('Mixed symbols in direct events')
        if len(c) and (c['symbol'].astype(str)!=symbol).any():
            raise ValueError('Mixed symbols in HTF context')
        if len(e):events.append(e)
        if len(c):contexts.append(c)
        records.append({'symbol':symbol,'pre_cutoff_utc':meta['pre_cutoff_utc'],
                        'observation_hours':meta['observation_hours'],
                        'first_touches_15m':meta['15m_overlay_exact_parity']['first_touches'],
                        'reactions_15m':meta['15m_overlay_exact_parity']['reactions'],
                        'direct_events_all_timeframes':len(e)})
    if len({r['observation_hours'] for r in records})>1:
        raise ValueError('Cannot pool unequal observation periods')
    direct=pd.concat(events,ignore_index=True) if events else pd.DataFrame()
    context=pd.concat(contexts,ignore_index=True) if contexts else pd.DataFrame()
    if len(direct) and direct[['symbol','pivot_touch_id']].duplicated().any():
        raise ValueError('Repeated unique pivot first retests')
    direct_summary=m.summarize_direct(direct) if len(direct) else pd.DataFrame()
    htf_summary=m.summarize_htf(context) if len(context) else pd.DataFrame()
    # Multi-symbol rates are descriptive only; row-level events may share paths.
    return {'manifest':pd.DataFrame(records),'events':direct,'summary':direct_summary,
            'context':context,'context_summary':htf_summary}


def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--inputs',required=True,nargs='+')
    ap.add_argument('--out',required=True)
    a=ap.parse_args(argv)
    out=Path(a.out).resolve()
    if not out.name.endswith('.csv'):
        raise ValueError('--out must end with .csv')
    names={'events':out,'summary':out.with_name(out.stem+'_summary.csv'),
           'context':out.with_name(out.stem+'_context.csv'),
           'context_summary':out.with_name(out.stem+'_context_summary.csv'),
           'manifest':out.with_name(out.stem+'_symbols.csv')}
    if any(f.exists() for f in names.values()):
        raise FileExistsError('Refusing to overwrite pooled research artifacts')
    data=pool_paths(a.inputs)
    out.parent.mkdir(parents=True,exist_ok=True)
    for key,path in names.items():data[key].to_csv(path,index=False)
    print('REACTION_MULTITF_POOL',len(data['manifest']),'symbols;',len(data['events']),'direct first touches;',len(data['context']),'15m contexts')
    if len(data['summary']):
        print(data['summary'][['timeframe','side','status','first_touches_n','30m_complete_n','30m_wick_wins_n','opposite_breaks_n']].to_string(index=False))
    print('OUTPUT',*[str(x) for x in names.values()])

if __name__=='__main__':main()
