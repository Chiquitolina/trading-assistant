import csv
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from reaction_lab_first_touch_paths_v2 import encode
from universal_runner_horizontal_tp_sl_matrix_v1 import (
    simulate_180, frozen_labels, run_matrix, HORIZON,
)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def fixture(temp):
    root = Path(temp)
    project = root / 'project'
    d = project / 'reports' / 'reaction_lab'
    d.mkdir(parents=True)
    run_dir = root / 'frozen'
    run_dir.mkdir()
    entry = 1_780_000_020_000
    entry -= entry % 60_000
    retest = entry - 60_000
    known = entry
    labels = [
        dict(touch_key='near', symbol='BTCUSDT', side='LONG', status='REACTION',
             observation_type='LIVE_OBSERVED', group_pre='PERSISTENT_LEVEL_NEAR_PRE',
             retest_ts=retest, known_ts=known),
        dict(touch_key='far', symbol='AAVEUSDT', side='SHORT', status='REACTION',
             observation_type='RECOVERED_WINDOW', group_pre='NO_PERSISTENT_LEVEL_NEAR_PRE',
             retest_ts=retest, known_ts=known),
    ]
    with (run_dir / 'event_labels.csv').open('w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(labels[0])); w.writeheader(); w.writerows(labels)
    (run_dir / 'candidate_levels.csv').write_text('touch_key\nnear\n', encoding='utf-8')
    (run_dir / 'audit.json').write_text(json.dumps({
        'no_outcomes_used_for_labels': True, 'source_mode': 'OFFLINE_FROZEN',
        'sha256': {x: digest(run_dir / x) for x in ('event_labels.csv', 'candidate_levels.csv')},
    }), encoding='utf-8')
    db = sqlite3.connect(d / 'first_touch_paths_v2.sqlite3')
    db.execute('''CREATE TABLE paths(
        touch_key TEXT PRIMARY KEY, symbol TEXT, side TEXT, status TEXT,
        retest_ts INTEGER, entry_ts INTEGER, first_seen_ms INTEGER,
        state TEXT, count INTEGER, path_blob BLOB)''')
    candles = []
    for i in range(180):
        hi, lo = (102.0, 99.9) if i == 1 else (100.3, 99.8)
        candles.append((entry + i*60_000, 100., hi, lo, 100.))
    db.execute('INSERT INTO paths VALUES (?,?,?,?,?,?,?,?,?,?)',
               ('near', 'BTCUSDT', 'LONG', 'REACTION', retest, entry, known,
                'COMPLETE', len(candles), encode(candles)))
    db.commit(); db.close()
    return project, run_dir, labels, candles


class MatrixTest(unittest.TestCase):
    def test_full_grid_and_missing_denominator(self):
        with tempfile.TemporaryDirectory() as temp:
            project, run_dir, labels, candles = fixture(temp)
            out = run_matrix(project, run_dir, no_recovery=True)
            self.assertEqual(out['events_labeled'], 2)
            self.assertEqual(out['events_full_180m_near_far'], 1)
            self.assertEqual(out['outcome_rows'], 50)
            self.assertEqual(out['settings']['tp_pct'], [1, 2, 3, 4, 5])
            self.assertEqual(out['settings']['sl_pct'], [1, 2, 3, 4, 5])
            with (Path(out['output_dir']) / 'matrix_by_status.csv').open() as f:
                rows = list(csv.DictReader(f))
            near = next(r for r in rows if r['status']=='REACTION' and
                        r['group_pre']=='PERSISTENT_LEVEL_NEAR_PRE' and
                        r['perspective']=='ORIGINAL_OPEN' and r['tp_pct']=='1' and r['sl_pct']=='1')
            self.assertEqual(near['N_labeled'], '1')
            self.assertEqual(near['N_evaluable'], '1')
            self.assertEqual(near['N_tp_first'], '1')
            far = next(r for r in rows if r['status']=='REACTION' and
                       r['group_pre']=='NO_PERSISTENT_LEVEL_NEAR_PRE' and
                       r['perspective']=='ORIGINAL_OPEN' and r['tp_pct']=='1' and r['sl_pct']=='1')
            self.assertEqual(far['N_labeled'], '1')
            self.assertEqual(far['N_evaluable'], '0')
            self.assertEqual(far['tp_first_pct'], '')

    def test_precomputed_parity_matches(self):
        with tempfile.TemporaryDirectory() as temp:
            project, run_dir, _, _ = fixture(temp)
            db_path = project/'reports/reaction_lab/first_touch_paths_v2.sqlite3'
            with sqlite3.connect(db_path) as db:
                db.execute('CREATE TABLE outcomes(touch_key TEXT, horizon INTEGER, perspective TEXT, tp REAL, sl REAL, outcome TEXT)')
                db.execute('INSERT INTO outcomes VALUES (?,?,?,?,?,?)', ('near', 180, 'ORIGINAL_OPEN', 1.0, 1.0, 'TP_FIRST'))
                db.commit()
            result = run_matrix(project, run_dir, no_recovery=True)
            self.assertEqual(result['existing_tp1_sl1_180_parity_compared'], 1)

    def test_precomputed_parity_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            project, run_dir, _, _ = fixture(temp)
            db_path = project/'reports/reaction_lab/first_touch_paths_v2.sqlite3'
            with sqlite3.connect(db_path) as db:
                db.execute('CREATE TABLE outcomes(touch_key TEXT, horizon INTEGER, perspective TEXT, tp REAL, sl REAL, outcome TEXT)')
                db.execute('INSERT INTO outcomes VALUES (?,?,?,?,?,?)', ('near', 180, 'ORIGINAL_OPEN', 1.0, 1.0, 'SL_FIRST'))
                db.commit()
            with self.assertRaisesRegex(ValueError, 'parity mismatch'):
                run_matrix(project, run_dir, no_recovery=True)

    def test_outcomes_must_have_180_bars(self):
        bars = [(1_780_000_020_000 + 60_000*i, 100., 100., 100., 100.) for i in range(179)]
        with self.assertRaisesRegex(ValueError, '180 bars'):
            simulate_180(bars, 'LONG', 1, 1)

    def test_same_candle_ambiguous_is_stop_conservative(self):
        bars = [(1_780_000_020_000 + 60_000*i, 100., 101.2 if i==0 else 100.,
                 98.8 if i==0 else 100., 100.) for i in range(HORIZON)]
        outcome, minute, gross = simulate_180(bars, 'LONG', 1, 1)
        self.assertEqual((outcome, minute, gross), ('SL_AMBIGUOUS', 1, -1.))

    def test_frozen_label_integrity(self):
        with tempfile.TemporaryDirectory() as temp:
            _, run_dir, _, _ = fixture(temp)
            (run_dir/'event_labels.csv').write_text('tampered\n')
            with self.assertRaisesRegex(ValueError, 'SHA mismatch'):
                frozen_labels(run_dir)

    def test_recovered_window_not_counted_as_live(self):
        with tempfile.TemporaryDirectory() as temp:
            project, run_dir, labels, _ = fixture(temp)
            out = run_matrix(project, run_dir, no_recovery=True)
            with (Path(out['output_dir'])/'event_coverage.csv').open() as f:
                rows = list(csv.DictReader(f))
            by_key = {r['touch_key']:r for r in rows}
            self.assertEqual(by_key['far']['entry_observable_at_next_open'], 'NO')
            self.assertEqual(by_key['near']['entry_observable_at_next_open'], 'YES_PROXY')

    def test_noncausal_known_ts_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            _, run_dir, _, _ = fixture(temp)
            src = run_dir/'event_labels.csv'
            lines = src.read_text().splitlines()
            # Adjust known_ts and recompute SHA to isolate causal check.
            with src.open() as f:
                rows = list(csv.DictReader(f))
            rows[0]['known_ts'] = str(int(rows[0]['known_ts'])+60_000)
            with src.open('w',newline='') as f:
                w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
            audit_file=run_dir/'audit.json'
            audit=json.loads(audit_file.read_text())
            audit['sha256']['event_labels.csv']=digest(src)
            audit_file.write_text(json.dumps(audit))
            with self.assertRaisesRegex(ValueError, 'Noncausal'):
                frozen_labels(run_dir)


if __name__ == '__main__':
    unittest.main()
