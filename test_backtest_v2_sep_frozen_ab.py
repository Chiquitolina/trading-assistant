#!/usr/bin/env python3
"""Offline smoke test: genuine dashboard simulation functions, deterministic gates."""
from pathlib import Path
import numpy as np
import pandas as pd
from backtest_v2_sep_frozen_ab import audit_events,load_canonical_portfolio,SETTINGS,run_backtest,REL_DATA,RUN

# Input helper itself does not depend on Streamlit. The candidate's HTF room
# is deliberately unverified so the selector tests its strength fallback.
def synthetic():
    rows=[]
    for i in range(8):
        entry=1_800_000_000_000+i*8*60_000
        rows.append(dict(candidate_v1_event_key=f'XYZUSDT|LONG|{entry-60000}',symbol=f'S{i}USDT',side='LONG',
                         entry_timestamp=entry,exit_timestamp=entry+4*60_000,Outcome='TP' if i%3 else 'SL',
                         net_pnl_pct=.4 if i%3 else -3.1,execution_cost_pct=.10,
                         side_adjusted_strength_vs_btc_4h=.2+i/10,btc_return_pct_4h=.1234,
                         btc_return_pct_4h_rebuilt=.1234,btc_return_delta_4h=-.5 if i%4==0 else 0,
                         breadth_delta_4h=50 if i%4==1 else 0,ab_context_available=True,
                         nearest_opposing_room_pct=np.nan,retest_timestamp=entry-60000))
    return pd.DataFrame(rows)


def test():
    selected,blocked,parity,counts=audit_events(synthetic())
    assert counts['strength_positive']==8
    assert counts['blocked_total']==4
    assert counts['A_only']==2 and counts['B_only']==2 and counts['both']==0
    assert parity['max_abs_diff_pp']==0
    from pathlib import Path
    candidates=[Path.cwd() / 'dashboard/app.py',
                Path(__file__).resolve().parent.parent / 'candidate_candle_cache_fix/dashboard/app.py',
                Path(__file__).resolve().parent.parent / 'candidate_restore_replay_work/dashboard/app.py']
    app=next((p for p in candidates if p.exists()),None)
    if app is None:
        print('SKIP: dashboard source not present; gate checks passed')
        return
    runner=load_canonical_portfolio(app)
    x=runner(selected,**SETTINGS)
    y=runner(selected.loc[~blocked],**SETTINGS)
    assert x['summary']['Eligible trades']==8
    assert y['summary']['Eligible trades']==4
    assert x['summary']['Accepted trades']==8
    assert y['summary']['Accepted trades']==4
    import tempfile
    import shutil
    import json
    with tempfile.TemporaryDirectory() as tmp:
        project=Path(tmp)
        (project/'dashboard').mkdir()
        shutil.copyfile(app, project/'dashboard/app.py')
        events=project/REL_DATA
        events.parent.mkdir(parents=True)
        synthetic().to_csv(events,index=False)
        config=project/'reports/candidate_long_horizon/runs'/RUN/'config.json'
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({'tp_pct': .5, 'sl_pct': 3.0, 'horizon_min':180, 'symbols':['S0USDT','S1USDT']}))
        run_backtest(project,parity_only=True)
        run_backtest(project,parity_only=False)
        out=project/'reports/candidate_long_horizon/diagnostics/v2_sep_2026/portfolio_frozen_ab'
        assert (out/'portfolio_comparison.json').exists()
        assert (out/'baseline_portfolio_ledger.csv').exists()
        result=json.loads((out/'portfolio_comparison.json').read_text())
        assert result['candidate_counts']['blocked_total']==4
        assert result['baseline']['Accepted trades']==8
        assert result['with_frozen_gate']['Accepted trades']==4
    print('PASS: canonical engine, script CLI prep, reporting, parity checks, A/B masks')

if __name__=='__main__': test()
