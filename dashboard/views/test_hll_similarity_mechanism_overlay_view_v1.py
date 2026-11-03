"""Offline regression tests for HLL historical overlays inside Structural Discovery PRE."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import plotly.graph_objects as go

import hll_fingerprint_mechanism_join_audit_v1 as audit
import hll_similarity_mechanism_adapter_v1 as adapter
from dashboard.views import hll_similarity_mechanism_overlay_view as ui


PRE = 1_800_000_000_000
AID = 'exact-attempt-5'


def fixture(root: Path, *, outside_coverage=False):
    index = root/'reports/horizontal_level_structural_similarity_v1/first_attempts_290_v1'
    episodes = root/'reports/horizontal_level_structural_overlap_audit_v1/full290_v1'
    index.mkdir(parents=True, exist_ok=True)
    episodes.mkdir(parents=True, exist_ok=True)
    (index/'manifest.json').write_text(json.dumps({'fingerprints_sha256':'fingerprint-sha'}))
    (episodes/'manifest.json').write_text(json.dumps({'outputs_sha256':{
        'attempt_episode_map.csv':'episode-map-sha'}}))
    db, man = ui.overlay_paths(root)
    db.parent.mkdir(parents=True, exist_ok=True)
    records=[]
    for route in adapter.ROUTES:
        r = {k:'' for k in adapter.COLUMNS}
        r.update(attempt_id=AID,symbol='WAXPUSDT',level_id='lvl5',episode_id='episode-A',
            pre_closed_ts=str(PRE)+'.0', breakout_closed_ts=str(PRE+ui.CANDLE_MS),
            route=route, entry_cohort='FIRST_PER_LEVEL',audit_match='MATCHED',
            post_is_retrospective='true', entry_status='NOT_COVERED' if outside_coverage else 'NO_SIGNAL',
            signal_reference_timeframe='1m' if route=='FLIP_1M' else '30m',
            open_source_discrepancy='false')
        if not outside_coverage and route != 'V29_RECLAIM_30M':
            confirm = PRE + (2 if route=='FLIP_1M' else 3)*ui.CANDLE_MS
            entry = confirm + (60_000 if route == 'FLIP_1M' else 0)
            r.update(mechanism_status='FLIP_CONFIRMED_LATER_LOST' if route=='FLIP_1M' else 'CONFIRMED',
                confirmed_closed_ts=str(confirm), entry_status='ENTRY_VERIFIED',
                entry_id='entry-'+route, signal_ts=str(confirm), entry_ts=str(entry),
                entry_open_signal_reference='0.0195',entry_open_execution_1m='0.0195001')
        elif route=='V29_RECLAIM_30M':
            r['mechanism_status']='NO_ORIGINAL_FAILURE'
        records.append(r)
    con=sqlite3.connect(db)
    try:
        con.execute('CREATE TABLE overlay (' + ', '.join('"'+c+'" TEXT NOT NULL' for c in adapter.COLUMNS)+')')
        con.executemany('INSERT INTO overlay VALUES ('+','.join('?' for _ in adapter.COLUMNS)+')',
                        [[r[k] for k in adapter.COLUMNS] for r in records])
        con.commit()
    finally:
        con.close()
    manifest=dict(version=adapter.VERSION,status='DONE',scope='ALL_ATTEMPTS_BY_EXACT_ATTEMPT_ID',
        entry_scope='FIRST_PER_LEVEL_ONLY',ranker_changed=False,pre_features_changed=False,
        routes_per_attempt=3,source_fingerprints_sha256='fingerprint-sha',
        source_episode_map_sha256='episode-map-sha',output_db_sha256=audit.digest(db))
    man.write_text(json.dumps(manifest))
    return db,man


class OverlayViewTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.db,self.man=fixture(self.root)

    def read(self, **kwargs):
        x=dict(root=self.root,attempt_id=AID,symbol='WAXPUSDT',level_id='lvl5',
               pre_closed_ts=str(PRE),episode_id='episode-A')
        x.update(kwargs)
        return ui.read_attempt(**x)

    def test_exact_selected_attempt_N_and_three_independent_routes(self):
        rows=self.read()
        self.assertEqual(len(rows),3)
        self.assertEqual({r['route'] for r in rows}, set(adapter.ROUTES))
        table=ui.mechanism_table(rows)
        self.assertEqual(table['Cobertura FIRST_PER_LEVEL'].tolist().count('ENTRY_VERIFIED'),2)
        self.assertIn('OPEN ejecutable 1m',table.columns)

    def test_post_markers_are_hidden_until_explicit_reveal(self):
        rows=self.read()
        base=go.Figure()
        ui.add_post_markers(base,rows,PRE,96,reveal=False)
        self.assertEqual(len(base.data),0)
        self.assertEqual(len(base.layout.shapes),0)
        ui.add_post_markers(base,rows,PRE,96,reveal=True)
        self.assertEqual(len(base.data),2)
        self.assertEqual(len(base.layout.shapes),2)
        self.assertEqual(len({t.name for t in base.data}),2)

    def test_visible_post_window_and_missing_open_never_imputed(self):
        rows=self.read()
        base=go.Figure()
        ui.add_post_markers(base,rows,PRE,2,reveal=True)
        self.assertEqual(len(base.data),0)
        self.assertEqual(len(base.layout.shapes),0)
        import pandas as pd
        self.assertTrue(pd.isna(ui.mechanism_table(rows).loc[2,'OPEN ejecutable 1m']))

    def test_noncovered_attempt_has_raw_mechanisms_but_no_entry_marker(self):
        self.db.unlink();self.man.unlink()
        fixture(self.root,outside_coverage=True)
        rows=self.read()
        self.assertTrue(all(r['entry_status']=='NOT_COVERED' for r in rows))
        self.assertTrue(all(r['entry_open_execution_1m']=='' for r in rows))
        base=go.Figure()
        ui.add_post_markers(base,rows,PRE,96,reveal=True)
        self.assertEqual(len(base.data),0)

    def test_wrong_identity_fails_closed(self):
        for part in (dict(attempt_id='episode-first-instead-of-five'),
                     dict(symbol='BSVUSDT'), dict(level_id='different'),
                     dict(episode_id='different'),dict(pre_closed_ts=str(PRE+1))):
            with self.subTest(part=part),self.assertRaises(ValueError):
                self.read(**part)

    def test_modified_sqlite_or_stale_source_hash_rejected(self):
        with sqlite3.connect(self.db) as c:
            c.execute("UPDATE overlay SET entry_open_execution_1m = '9' WHERE route = 'FLIP_1M'")
        with self.assertRaisesRegex(ValueError,'SQLite'):
            self.read()
        manifest=json.loads(self.man.read_text())
        manifest['output_db_sha256']=audit.digest(self.db)
        manifest['source_fingerprints_sha256']='wrong-pre-index'
        self.man.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError,'huella PRE'):
            self.read()

    def test_real_time_mismatch_rejected_without_timestamp_rounding(self):
        with sqlite3.connect(self.db) as c:
            c.execute("UPDATE overlay SET entry_ts = ? WHERE route = 'FLIP_1M'",(str(PRE+2*ui.CANDLE_MS+60_001),))
        m=json.loads(self.man.read_text());m['output_db_sha256']=audit.digest(self.db)
        self.man.write_text(json.dumps(m))
        with self.assertRaisesRegex(ValueError,'flip no corresponde'):
            self.read()

    def test_missing_overlay_is_optional_not_fake_no_signal(self):
        self.db.unlink();self.man.unlink()
        self.assertIsNone(self.read())


if __name__=='__main__':
    unittest.main()
