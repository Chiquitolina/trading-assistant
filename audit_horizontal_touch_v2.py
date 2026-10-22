#!/usr/bin/env python3
"""Audit only, no writes: recompute verified KNOWN 1m contact for Atlas V2."""
import argparse
from pathlib import Path
from collections import Counter
from dashboard.views.reaction_structural_level_atlas_v2_view import load
from reaction_horizontal_level_research import load_retest_candles,build,GROUPS,A,B,C,UNKNOWN


def main():
    p=argparse.ArgumentParser(description='Audita intersección 1m real en Horizontal Level Research')
    p.add_argument('--project-root',default='.')
    p.add_argument('--tf',choices=('TODOS','15m','30m'),default='TODOS')
    p.add_argument('--symbols',default='COOKIEUSDT,SENTUSDT,GASUSDT,OPNUSDT')
    args=p.parse_args()
    root=Path(args.project_root).resolve()
    levels,events,summary=load(root)
    enriched=load_retest_candles(root,events)
    classified,_=build(enriched,levels,args.tf)
    print('===== HORIZONTAL CONTACT AUDIT (OHLC 1m INTERSECTION) =====')
    print('source events:',len(events),'source candidate levels:',len(levels))
    print('historical Atlas version:',summary.get('version'))
    print('verified model: horizontal_levels_verified_ohlc_contact_v2')
    for key in (A,B,C,UNKNOWN):
        print(GROUPS[key],':',int(classified.level_group.eq(key).sum()))
    print('old Atlas said touch, not verified:',int(classified.atlas_claimed_touch_not_verified.sum()))
    print('1m missing/invalid:',sum(not (row.known_high==row.known_high and row.known_low==row.known_low) for row in classified.itertuples(index=False)))
    wanted=[x.strip().upper() for x in args.symbols.split(',') if x.strip()]
    print('\n===== EXAMPLES =====')
    for name in wanted:
        rows=classified.loc[classified.symbol.eq(name)].sort_values('retest_ts')
        if rows.empty:
            print(name,'NOT IN COHORT');continue
        for item in rows.itertuples(index=False):
            print(f'{name:12s}',str(item.date_utc),str(item.side),GROUPS[item.level_group],
                  '1m low/high:',item.known_low,item.known_high,
                  'zone:',item.relevant_zone_low,item.relevant_zone_high,
                  'old flag:',item.atlas_claimed_touch,
                  'rejected old touch:',item.atlas_claimed_touch_not_verified)
    print('\nOffline read-only. No Atlas or First Touch files modified.')


if __name__=='__main__':main()
