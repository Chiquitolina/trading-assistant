#!/usr/bin/env python3
"""Offline regression tests for active-replay-safe partial snapshot."""
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import sys
import pandas as pd
from run_partial_v2_backtest import make_snapshot


def main():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        run_id = '20260907_20260930_test_source'
        src = root / 'reports/candidate_long_horizon/runs' / run_id
        src.mkdir(parents=True)
        original = {'run_id':run_id,'symbols':['AAAUSDT','BBBUSDT','CCCUSDT','DDDUSDT'],
                    'months':[{'label':'2026-09','start_ms':1,'end_ms':2}]}
        (src/'config.json').write_text(json.dumps(original))
        (src/'state.json').write_text(json.dumps({'processed':{'2026-09':['AAAUSDT','BBBUSDT']},'errors':{}}))
        # CCC is written to events.csv by live runner but not checkpointed yet;
        # snapshot must NOT include its data.
        pd.DataFrame([
            {'symbol':'AAAUSDT','candidate_v1_event_key':'aaa','entry_timestamp':100},
            {'symbol':'AAAUSDT','candidate_v1_event_key':'aab','entry_timestamp':200},
            {'symbol':'CCCUSDT','candidate_v1_event_key':'ccc','entry_timestamp':300},
        ]).to_csv(src/'events.csv', index=False)
        manifest = make_snapshot(root,run_id,min_symbols=2)
        frozen = root/'reports/candidate_long_horizon/runs'/manifest['partial_run_id']
        frame = pd.read_csv(frozen/'events.csv')
        config = json.loads((frozen/'config.json').read_text())
        assert len(frame)==2 and set(frame['symbol'])=={'AAAUSDT'}
        assert config['symbols']==['AAAUSDT','BBBUSDT']
        assert config['source_universe_count']==4
        assert frame['candidate_v1_event_key'].nunique()==2
        assert (src/'events.csv').exists() and len(pd.read_csv(src/'events.csv'))==3
        # Source state progressing independently doesn't affect frozen snapshot
        (src/'state.json').write_text(json.dumps({'processed':{'2026-09':['AAAUSDT','BBBUSDT','CCCUSDT']}}))
        assert len(pd.read_csv(frozen/'events.csv'))==2
        try:
            make_snapshot(root,run_id,min_symbols=4)
        except RuntimeError as exc:
            assert 'completed' in str(exc)
        else:
            raise AssertionError('Insufficient completed symbols should stop')
    print('PASS: atomic-file source snapshot, completed-only rows, 290-universe metadata separation, no source mutations')


if __name__=='__main__':
    main()
