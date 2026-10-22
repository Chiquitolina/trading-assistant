#!/usr/bin/env python3
"""Read-only report of causal PRE role vs REACTION direction from Atlas V2.

No OHLC KNOWN, First Touch, LIVE, market connectivity or CSV writes.
"""
import argparse
from collections import Counter
from pathlib import Path

from dashboard.views.reaction_structural_level_atlas_v2_view import load
from reaction_horizontal_level_research import (
    ROLE_VERSION, ROLE_LABELS, ROLE_REBOUND, ROLE_BREAK,
    ROLE_IN_ZONE, ROLE_NO_NEAR, ROLE_UNKNOWN, build_pre_roles,
)


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--project-root',default='.')
    p.add_argument('--tf',choices=('TODOS','15m','30m'),default='TODOS')
    p.add_argument('--symbols',default='SWARMUSDT,KGENUSDT,PAXGUSDT,SENTUSDT,GASUSDT')
    args=p.parse_args(argv)
    root=Path(args.project_root).resolve()
    levels,events,summary=load(root)
    roles=build_pre_roles(events,levels,args.tf)
    print('===== HORIZONTAL ROLE PRE AUDIT =====')
    print('version:',ROLE_VERSION)
    print('source version:',summary.get('version'))
    print('events:',len(events),'source zones:',len(levels),'TF:',args.tf)
    print('one role per event:',len(roles)==len(events) and not roles.event_key.duplicated().any())
    for role in (ROLE_REBOUND,ROLE_BREAK,ROLE_IN_ZONE,ROLE_NO_NEAR,ROLE_UNKNOWN):
        print(ROLE_LABELS[role]+':',int(roles.pre_role.eq(role).sum()))
    overview=roles.merge(events[['event_key','symbol','side','date_utc','retest_ts']],
                         on='event_key',how='left',validate='one_to_one')
    print('\n===== LONG / SHORT =====')
    for side,g in overview.groupby('side'):
        counts=Counter(g.pre_role)
        print(side,'|',len(g),'|',', '.join(f'{key}={counts.get(key,0)}'
              for key in (ROLE_REBOUND,ROLE_BREAK,ROLE_IN_ZONE,ROLE_NO_NEAR,ROLE_UNKNOWN)))
    print('\n===== INSPECTION EXAMPLES =====')
    for symbol in [x.strip().upper() for x in args.symbols.split(',') if x.strip()]:
        sample=overview.loc[overview.symbol.eq(symbol)].sort_values('retest_ts')
        if sample.empty:
            print(symbol,'not in audited Atlas V2 cohort')
            continue
        for e in sample.itertuples(index=False):
            print(symbol,e.date_utc,e.side,'ts=',e.retest_ts,
                  'role=',ROLE_LABELS[e.pre_role],
                  'tf=',e.role_timeframe,'zone=',e.role_zone_id,
                  'PRE=',e.role_pre_close,'distance_ATR=',e.role_distance_atr,
                  'position=',e.role_position)
    print('\nNO outcomes used. All source files read-only. '
          'To see TP/SL by role, open Horizontal Level Research and use '
          'the existing global First Touch controls.')


if __name__=='__main__':main()
