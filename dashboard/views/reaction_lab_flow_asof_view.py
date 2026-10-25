"""Historical 4h market-flow context for ALL first-touch statuses, READ ONLY.

An event can only receive a snapshot if it was OBSERVED before its first-touch
candle closed. Earlier events remain unavailable; never backfill from today.
"""
from __future__ import annotations
from pathlib import Path
from contextlib import closing
import sqlite3

REL='reports/reaction_lab/market_flow_history_4h_v1.sqlite3'
MINUTE=60_000
# Match the historical as-of availability and time-to-live used by V2.
MAX_AGE_MS=250*MINUTE
DISPLAY_FIELDS=('return_rank_4h_pct','volume_rank_4h_pct','relative_volume_4h',
                'return_4h_pct','flow_group','primary_sector','snapshot_close_ms','available_at_ms')


def get_market_flow_asof(project, touches):
    """{touch_key: snapshot} for any REACTION/FAILED/INDECISIVE.

    Uses `retest_ts+60s`, when its touch candle is first known; does not use
    current Redis market-flow. The archive's availability time is authoritative.
    Single indexed query per distinct symbol+timestamp in a page.
    """
    db=Path(project)/REL
    if not db.is_file() or not touches:return {}, 'NO_ARCHIVE'
    uri=db.resolve().as_uri()+'?mode=ro'
    result={}
    with closing(sqlite3.connect(uri,uri=True,timeout=5)) as con:
        con.execute('PRAGMA query_only=ON')
        sql='''SELECT return_rank_4h_pct,volume_rank_4h_pct,relative_volume_4h,
                   return_4h_pct,flow_group,primary_sector,snapshot_close_ms,available_at_ms
               FROM flow_snapshots
               WHERE symbol=? AND available_at_ms<=?
                 AND snapshot_close_ms<=?
                 AND snapshot_close_ms>=?
               ORDER BY snapshot_close_ms DESC,available_at_ms DESC LIMIT 1'''
        cache={}
        for item in touches:
            symbol=str(item['symbol']).strip().upper()
            if not symbol:continue
            when=int(item['retest_ts'])+MINUTE
            ck=(symbol,when)
            if ck not in cache:
                cache[ck]=con.execute(sql,(symbol,when,when,when-MAX_AGE_MS)).fetchone()
            r=cache[ck]
            if r is not None:
                result[item['touch_key']]=dict(zip(DISPLAY_FIELDS,r))
    return result,'OBSERVED_ARCHIVE'


def enrich_first_touch_page(project,df):
    """Add as-of market-flow to a pandas DataFrame; leave missing values null."""
    if df.empty:return df,'NO_ROWS',0
    asof,state=get_market_flow_asof(project,df[['touch_key','symbol','retest_ts']].to_dict('records'))
    out=df.copy()
    for field in DISPLAY_FIELDS:
        out[field]=out['touch_key'].map(lambda key:asof.get(key,{}).get(field))
    return out,state,len(asof)
