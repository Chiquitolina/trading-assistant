"""Tests offline de adaptación del dataset al Multi-Swing Confluence vigente."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from reaction_multi_swing_quality_dataset_v1 import (
    make_feature_row, run_cohort, write_run, snapshot_for_event,
)
from dashboard.views.reaction_continuous_multiswing_view import (
    confirmed_points, confluence_matches,
)

M=60_000


def data_fixture():
    # 15m original 07:45; low físico único a 07:57, validado en 1m/5m.
    # Historial sintético continuo hasta la vela 09:00 (known=09:01).
    day=1_800_000_000_000 - 1_800_000_000_000 % (24*60*M)
    start=day + 7*60*M
    minute=[]
    pivot=start+57*M
    for i in range(121):
        ts=start+i*M
        distance=abs(i-57)
        low=100.0 if i==57 else 100.6+min(distance*.02, 2.0)
        minute.append(dict(timestamp=ts,open=104.0,high=105.0,low=low,close=104.0))
    def aggregate(scale):
        out=[]
        for offset in range(0,len(minute)//scale*scale,scale):
            group=minute[offset:offset+scale]
            out.append(dict(timestamp=group[0]['timestamp'],open=group[0]['open'],
                            high=max(z['high'] for z in group),
                            low=min(z['low'] for z in group),close=group[-1]['close']))
        return out
    retest=start+120*M
    event=dict(touch_key='touch-A',legacy_event_key=f'XUSDT|LONG|{retest}',
               symbol='XUSDT',side='LONG',status='REACTION',timeframe='15m',
               detector='3x3',pivot_ts=start+45*M,actionable_ts=start+90*M,
               retest_ts=retest,known_ts=retest+M,
               observation_type='LIVE_OBSERVED',
               causal_json=json.dumps({'swing_price':100.0}))
    snapshots={'1m':minute,'5m':aggregate(5),'15m':aggregate(15)}
    return event,snapshots


def setup_sqlite(root, *, include_missing=True):
    root=Path(root)
    a=root/'reports/reaction_lab/scanner_first_touches_v1.sqlite3'
    b=root/'reports/reaction_lab/causal_context_v1.sqlite3'
    a.parent.mkdir(parents=True,exist_ok=True)
    event,snaps=data_fixture()
    columns=('touch_key','legacy_event_key','symbol','side','status','timeframe','detector',
             'pivot_ts','actionable_ts','retest_ts','known_ts','observation_type','causal_json')
    with sqlite3.connect(a) as con:
        con.execute('CREATE TABLE first_touches (' + ','.join(c+' TEXT' if c in (
            'touch_key','legacy_event_key','symbol','side','status','timeframe','detector',
            'observation_type','causal_json') else c+' INTEGER' for c in columns) + ')')
        con.execute('INSERT INTO first_touches VALUES ('+','.join('?' for _ in columns)+')',
                    tuple(event[c] for c in columns))
        if include_missing:
            other=dict(event,touch_key='touch-B',status='TOUCH_INDECISIVE',
                       legacy_event_key='XUSDT|LONG|999999')
            con.execute('INSERT INTO first_touches VALUES ('+','.join('?' for _ in columns)+')',
                        tuple(other[c] for c in columns))
    with sqlite3.connect(b) as con:
        con.execute('CREATE TABLE contexts(event_key TEXT,known_ts INTEGER,status TEXT,candles_json TEXT)')
        con.execute('INSERT INTO contexts VALUES(?,?,?,?)',
                    (event['legacy_event_key'],event['known_ts'],'READY',json.dumps(snaps)))
    return event,snaps


class DatasetIntegration(unittest.TestCase):
    def test_multiswing_selector_matches_ui_and_resolves_one_minimum(self):
        event,snaps=data_fixture()
        result=make_feature_row(event,snaps)
        self.assertEqual(result['coincident_tf_count'],3)
        self.assertEqual(result['matched_timeframes'],'15m,1m,5m')
        self.assertEqual(result['physical_structure_type'],'SAME_PHYSICAL_EXTREME')
        self.assertEqual(result['unique_physical_extrema'],1)
        self.assertTrue(result['physical_identity_complete'])
        points={tf:confirmed_points(snaps[tf],tf,event['known_ts'],5)[1]
                for tf in ('1m','5m')}
        ui=confluence_matches(points,pivot_ts=event['pivot_ts'],pivot_price=100,
                              side='LOW',known_ts=event['known_ts'])
        self.assertEqual(len(ui)+1,result['coincident_tf_count'])

    def test_missing_context_is_not_no_confluence(self):
        with tempfile.TemporaryDirectory() as temp:
            setup_sqlite(temp)
            rows,summary=run_cohort(Path(temp))
            self.assertEqual(len(rows),2)
            by_key={r['touch_key']:r for r in rows}
            self.assertEqual(by_key['touch-A']['quality_state'],'FEATURES_OK')
            self.assertEqual(by_key['touch-B']['quality_state'],'NOT_COVERED')
            self.assertEqual(by_key['touch-B']['failure_reason'],'NO_FROZEN_CONTEXT')
            self.assertNotIn('coincident_tf_count',by_key['touch-B'])
            self.assertEqual(summary['event_statuses'],{'REACTION':1,'TOUCH_INDECISIVE':1})

    def test_altering_future_cannot_change_pre(self):
        event,snaps=data_fixture()
        first=make_feature_row(event,snaps)
        snaps['1m'].append(dict(timestamp=event['known_ts'],open=104,high=800,low=1,close=104))
        self.assertEqual(first,make_feature_row(event,snaps))

    def test_unsupported_detector_is_error_not_silent_rewrite(self):
        event,snaps=data_fixture()
        with self.assertRaisesRegex(ValueError,'15m/3x3'):
            make_feature_row(dict(event,detector='5x5'),snaps)

    def test_frozen_timestamp_mismatch_is_not_usable(self):
        with tempfile.TemporaryDirectory() as temp:
            event,_=setup_sqlite(temp,include_missing=False)
            with sqlite3.connect(Path(temp)/'reports/reaction_lab/causal_context_v1.sqlite3') as con:
                _,status=snapshot_for_event(con,event['legacy_event_key'],event['known_ts']+M)
            self.assertEqual(status,'FROZEN_KNOWN_TS_MISMATCH')

    def test_outputs_are_append_only_and_have_hash(self):
        with tempfile.TemporaryDirectory() as temp:
            setup_sqlite(temp,include_missing=False)
            rows,summary=run_cohort(Path(temp))
            out=Path(temp)/'outputs/test-001'
            result=write_run(rows,summary,out,code_root=Path(__file__).parent)
            self.assertTrue(result.is_file())
            meta=json.loads((out/'manifest.json').read_text())
            self.assertEqual(len(meta['output_sha256']),64)
            with self.assertRaises(FileExistsError):
                write_run(rows,summary,out,code_root=Path(__file__).parent)


if __name__ == '__main__':unittest.main()
