"""Read-only bridge: standalone scanner journal / auto-path SQLite -> Reaction Lab.

Never writes to legacy monitors, snapshots or path pickles.
"""
from pathlib import Path
import json
import sqlite3
import time


def scan_rows(db_path: Path, symbols=None, limit=None):
    import pandas as pd
    columns = ['symbol','signal','status','retest_timestamp','candidate_v1_event_key']
    if not Path(db_path).exists():
        return pd.DataFrame(columns=columns)
    con=sqlite3.connect(f'file:{Path(db_path)}?mode=ro',uri=True,timeout=10)
    try:
        query='SELECT causal_json, first_seen_ms, observation_type FROM events ORDER BY retest_ts DESC'
        # retain ALL once-observed events, even if no longer in Redis; UI can track provenance.
        if limit is not None:
            query+=' LIMIT '+str(max(1,int(limit)))
        rows=[]
        allowed=set(str(s).upper() for s in symbols) if symbols is not None else None
        for payload, seen, origin in con.execute(query):
            row=json.loads(payload)
            if allowed is not None and str(row.get('symbol')).upper() not in allowed:
                continue
            row['reaction_scanner_first_seen_ms']=int(seen)
            row['reaction_scanner_observation_type']=origin
            row['reaction_scanner_persisted']=True
            rows.append(row)
        return pd.DataFrame(rows) if rows else pd.DataFrame(columns=columns)
    finally:con.close()


def merge_scanner_reactions(current, project, *, symbols, timeframe, detector,
                            min_prominence, tolerance, departure, max_age,
                            now_ms=None):
    """Use persisted scanner rows ONLY for precisely matching 15m-3x3 contract."""
    import pandas as pd
    if not (str(timeframe)=='15m' and str(detector)=='3x3'
        and abs(float(min_prominence))<1e-12
        and abs(float(tolerance)-0.10)<1e-10
        and abs(float(departure)-0.20)<1e-10
        and int(max_age)==360):
        return current
    persisted=scan_rows(Path(project)/'reports/reaction_lab/scanner_events_v1.sqlite3', symbols=symbols)
    if persisted.empty:return current
    # Ingest only reactions whose causal 1m context is plausibly still in Redis.
    # Older events remain durably visible in the SQLite journal / Fast feed;
    # recomputing RSI/Room on today's rolling buffer would invent historical context.
    now=int(now_ms if now_ms is not None else time.time()*1000)
    retest=pd.to_numeric(persisted['retest_timestamp'],errors='coerce')
    persisted=persisted.loc[retest.between(now-300*60_000,now-60_000)].copy()
    if persisted.empty:return current
    if current is None or current.empty:return persisted
    # Prefer the in-memory scanner result for same Event ID; legacy fields take precedence.
    combined=pd.concat([persisted,current],ignore_index=True,sort=False)
    return combined.drop_duplicates(subset=['symbol','signal','retest_timestamp'],keep='last').reset_index(drop=True)


def merge_auto_paths(legacy, project, candidate_rows=None):
    """Add SQLite-captured bars in MEMORY only. Never alter source pickle.

    Existing legacy bars win whenever both stores contain the same minute.
    """
    import pandas as pd
    db=Path(project)/'reports/reaction_lab/auto_paths_v1.sqlite3'
    if not db.is_file():return legacy
    keys=None
    if candidate_rows is not None:
        if candidate_rows.empty:return legacy
        col=('candidate_v1_event_key' if 'candidate_v1_event_key' in candidate_rows
             else 'candidate_v2_event_key')
        keys=set(candidate_rows[col].dropna().astype(str))
    from reaction_lab_auto_paths import read_auto_paths_dataframe
    automatic=read_auto_paths_dataframe(db,event_keys=keys)
    if automatic.empty:return legacy
    if legacy is None or legacy.empty:return automatic
    merged=pd.concat([legacy,automatic],ignore_index=True,sort=False)
    return (merged.drop_duplicates(subset=['candidate_v1_event_key','timestamp'],keep='first')
            .sort_values(['candidate_v1_event_key','timestamp'],kind='stable')
            .reset_index(drop=True))
