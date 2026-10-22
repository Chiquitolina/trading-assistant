#!/usr/bin/env python3
"""Read-only CLI verification of PRE direction alignment (never TP/SL)."""
from __future__ import annotations
import argparse
from collections import Counter
from pathlib import Path
from reaction_horizontal_transition_alignment import VERSION, add_transition_alignment
from reaction_horizontal_level_research import build_pre_roles
from reaction_horizontal_level_lifecycle import read_checked_cache


def audit(root: Path, tf: str = 'TODOS', symbols=()):
    from dashboard.views.reaction_structural_level_atlas_v2_view import load
    levels,events,_ = load(root)
    roles = build_pre_roles(events,levels,tf)
    validated=read_checked_cache(root,events,roles,tf)
    annotated=add_transition_alignment(validated)
    counts=Counter(annotated.transition_alignment)
    print('HORIZONTAL_TRANSITION_ALIGNMENT_AUDIT',
          {'version':VERSION,'tf_filter':tf,'events':len(annotated),
           'states':dict(counts),'transition_eligible':counts.get('ALINEADA_CON_TRANSICION_PRE',0)+
           counts.get('CONTRARIA_A_TRANSICION_PRE',0),'outcomes_used':False})
    if symbols:
        print('\n=== EJEMPLOS PRE (SIN OUTCOMES) ===')
        selection=annotated[annotated.symbol.astype(str).str.upper().isin(set(symbols))]
        fields=['event_key','symbol','side','timeframe','zone_id','lifecycle_state',
                'initial_side','current_confirmed_side','transition_alignment',
                'transition_alignment_reason']
        print(selection[fields].to_string(index=False) if len(selection) else 'SIN EJEMPLOS')
    return annotated


def main():
    parser=argparse.ArgumentParser(description='Auditoría PRE lectura solamente')
    parser.add_argument('--project-root',default='.')
    parser.add_argument('--tf',choices=['TODOS','15m','30m'],default='TODOS')
    parser.add_argument('--symbols',default='XLMUSDT,ZEREBROUSDT')
    args=parser.parse_args()
    audit(Path(args.project_root).resolve(),args.tf,
          [s.strip().upper() for s in args.symbols.split(',') if s.strip()])
if __name__=='__main__':main()
