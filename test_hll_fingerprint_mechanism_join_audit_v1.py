"""Synthetic offline regressions for the V1 PRE fingerprint / POST join audit."""
import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import hll_fingerprint_mechanism_join_audit_v1 as audit


def write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding='utf-8')


def make_fixture(root):
    # Three CROSS_UP attempts, only a1 and a3 are FIRSTs in their episodes.
    base = [
        dict(attempt_id='a1', symbol='BTCUSDT', level_id='l1', pre_closed_ts='100',
             breakout_closed_ts='200', episode_id='ep1', episode_first_attempt_id='a1', is_first_in_episode='True'),
        dict(attempt_id='a2', symbol='BTCUSDT', level_id='l1', pre_closed_ts='300',
             breakout_closed_ts='400', episode_id='ep1', episode_first_attempt_id='a1', is_first_in_episode='False'),
        dict(attempt_id='a3', symbol='ETHUSDT', level_id='l2', pre_closed_ts='500',
             breakout_closed_ts='600', episode_id='ep2', episode_first_attempt_id='a3', is_first_in_episode='True'),
    ]
    fps = [{k: b[k] for k in ('attempt_id', 'symbol', 'level_id', 'pre_closed_ts', 'episode_id')}
           | {'status': 'COMPLETE_PRE_96_288'} for b in (base[0], base[2])]
    mechanisms = [{k: b[k] for k in ('attempt_id','symbol','level_id','pre_closed_ts','breakout_closed_ts')}
                  | {'v26_status': 'FLIP_CONFIRMADO' if b['attempt_id']=='a1' else 'SIN_FLIP_EN_VENTANA',
                     'flip_confirmed_closed_ts': '260' if b['attempt_id']=='a1' else '',
                     'post_outcome': ''} for b in base]
    branches = []
    for b in base:
        for branch in audit.BRANCHES:
            branches.append(dict(attempt_id=b['attempt_id'], origin_symbol=b['symbol'], level_id=b['level_id'],
                                 branch=branch, status='CONFIRMED' if b['attempt_id']=='a1' else 'NO_EARLY_CONTINUATION',
                                 confirmed_ts='300' if b['attempt_id']=='a1' else '', signal_id='', next_open_ts='', next_open=''))
    coverage = []
    entries = []
    for route in audit.ENTRY_ROUTES:
        status = 'ENTRY_VERIFIED' if route != 'V29_RECLAIM_30M' else 'NO_SIGNAL'
        coverage.append({k: base[0][k] for k in ('attempt_id','symbol','level_id','breakout_closed_ts')} |
                        dict(route=route, status=status, entry_ts='350' if status=='ENTRY_VERIFIED' else '',
                             entry_price='100.5' if status=='ENTRY_VERIFIED' else '',
                             signal_ts='300' if status=='ENTRY_VERIFIED' else ''))
        if status == 'ENTRY_VERIFIED':
            entries.append({k: base[0][k] for k in ('attempt_id','symbol','level_id','pre_closed_ts','breakout_closed_ts')}
                           | dict(route=route, status=status, entry_ts='350', entry_price='100.5', signal_ts='300', entry_id=route))
    sources = {
        'episode_map': base, 'fingerprints': fps, 'pre_cohort': base,
        'mechanisms': mechanisms, 'continuation': branches,
        'entry_coverage': coverage, 'entry_events': entries,
    }
    for name, rows in sources.items():
        write_csv(root/audit.SOURCE_FILES[name], rows)
    write_json(root/audit.SOURCE_FILES['normalizers'], {'test': True})
    oa = root/audit.SOURCE_FILES['open_discrepancies']
    oa.parent.mkdir(parents=True, exist_ok=True)
    oa.write_text('entry_id,attempt_id,symbol,route,entry_ts,entry_utc,signal_reference_open_30m,execution_open_1m,difference_pct\n')
    sha = {k: audit.digest(root/p) for k, p in audit.SOURCE_FILES.items()}
    manifests = {
        'similarity': dict(version='similarity', episodes=2, index_cuts='EPISODE_FIRST_ONLY',
                           fingerprints_sha256=sha['fingerprints'],normalizers_sha256=sha['normalizers'],
                           source_map_sha256=sha['episode_map']),
        'episodes': dict(version='episode', attempts=3, symbols=2,
                         outputs_sha256={'attempt_episode_map.csv': sha['episode_map']},
                         source_sha256={'cohort': sha['pre_cohort']}),
        'pre': dict(version='pre', original_attempts=3, symbols=2),
        'entries': dict(version='entries', scope='FIRST_PER_LEVEL', entries=2, entry_open_discrepancies=0,
                        source=dict(n_attempts=3, n_symbols=2, pre_sha256=sha['pre_cohort'])),
        'continuation': dict(version='continuation', attempts=3, symbols=2, total_rows=6,
                             source_sha256=sha['pre_cohort']),
        'mechanisms': dict(version='mechanisms', attempts=3, symbols=2,source_prehash=sha['pre_cohort']),
    }
    for name, doc in manifests.items():
        write_json(root/audit.MANIFEST_FILES[name], doc)


class JoinAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        make_fixture(self.root)

    def test_one_to_many_routes_and_outside_coverage_are_distinct(self):
        joined, issues, summary, _, _ = audit.assess(self.root)
        self.assertEqual(issues, [])
        self.assertTrue(summary['is_compatible'])
        self.assertEqual(len(joined), 2)
        by_id = {r['attempt_id']: r for r in joined}
        self.assertEqual(by_id['a1']['verified_entry_count'], 2)
        self.assertEqual(by_id['a1']['entry_coverage_group'], 'WITH_VERIFIED_ENTRY')
        self.assertEqual(by_id['a3']['entry_coverage_group'], 'NOT_COVERED')
        self.assertEqual(by_id['a3']['continuation_status'], 'NO_EARLY_CONTINUATION')
        self.assertEqual(summary['index_entry_groups']['NOT_COVERED'], 1)
        self.assertEqual(summary['index_verified_routes_per_attempt']['2'], 1)

    def test_missing_entry_correspondence_is_detected(self):
        path = self.root/audit.SOURCE_FILES['entry_events']
        with path.open(encoding='utf-8',newline='') as f:
            rows = list(csv.DictReader(f))
        write_csv(path, rows[:1])
        joined, issues, summary, _, _ = audit.assess(self.root)
        types = {r['kind'] for r in issues}
        self.assertIn('VERIFIED_WITHOUT_ENTRY_EVENT', types)
        self.assertIn('ROW_COUNT_MISMATCH', types)
        self.assertFalse(summary['is_compatible'])

    def test_identity_mismatch_does_not_change_pre(self):
        path = self.root/audit.SOURCE_FILES['mechanisms']
        with path.open(encoding='utf-8',newline='') as f:
            rows = list(csv.DictReader(f))
        rows[0]['symbol']='BADUSDT'
        write_csv(path,rows)
        joined,issues,_,_,_ = audit.assess(self.root)
        self.assertTrue(any(x['kind']=='IDENTITY_MISMATCH' and x['attempt_id']=='a1' for x in issues))
        self.assertEqual({x['attempt_id']: x['symbol'] for x in joined}['a1'],'BTCUSDT')

    def test_source_sha_mismatch_is_reported(self):
        man_path = self.root/audit.MANIFEST_FILES['similarity']
        manifest=json.loads(man_path.read_text())
        manifest['fingerprints_sha256']='invalid'
        write_json(man_path,manifest)
        _,issues,_,_,_ = audit.assess(self.root)
        self.assertIn('HASH_OR_MISSING_CLAIM', {x['kind'] for x in issues})

    def test_reports_dont_modify_any_source_and_plan_does_not_write(self):
        before = {k: audit.digest(self.root/p) for k,p in audit.SOURCE_FILES.items()}
        rc = audit.main(['--plan','--root',str(self.root)])
        self.assertEqual(rc,0)
        out=self.root/'output'
        self.assertFalse(out.exists())
        result=audit.run(self.root,out)
        self.assertTrue(result['is_compatible'])
        self.assertTrue((out/'join_audit.csv').is_file())
        self.assertTrue((out/'coverage_summary.json').is_file())
        self.assertEqual(before, {k: audit.digest(self.root/p) for k,p in audit.SOURCE_FILES.items()})


    def test_reconciled_1m_open_differs_from_30m_reference_without_false_failure(self):
        # The existing V3.0.1 pipeline preserves 30m source OPEN in coverage
        # and writes reconciled executable 1m OPEN in entry_events.
        events_file = self.root/audit.SOURCE_FILES['entry_events']
        with events_file.open(encoding='utf-8', newline='') as f:
            events = list(csv.DictReader(f))
        events[0]['entry_price'] = '100.4'
        write_csv(events_file, events)
        audit_file = self.root/audit.SOURCE_FILES['open_discrepancies']
        audit_file.write_text(
            'entry_id,attempt_id,symbol,route,entry_ts,entry_utc,signal_reference_open_30m,execution_open_1m,difference_pct\n'
            + f"{events[0]['entry_id']},a1,BTCUSDT,{events[0]['route']},350,,100.5,100.4,-0.0995024876\n",
            encoding='utf-8')
        manifest_path = self.root/audit.MANIFEST_FILES['entries']
        manifest = json.loads(manifest_path.read_text())
        manifest['entry_open_discrepancies'] = 1
        write_json(manifest_path, manifest)
        _, issues, summary, _, source_differences = audit.assess(self.root)
        self.assertEqual(issues, [])
        self.assertTrue(summary['is_compatible'])
        self.assertEqual(summary['observed_open_source_differences'], 1)
        self.assertEqual(len(source_differences), 1)

    def test_only_first_episode_attempt_may_be_fingerprint(self):
        # a2's episode already represented by a1: audit must not allow it.
        file=self.root/audit.SOURCE_FILES['fingerprints']
        with file.open(encoding='utf-8',newline='') as f:
            rows=list(csv.DictReader(f))
        rows[0]['attempt_id']='a2'
        rows[0]['pre_closed_ts']='300'
        write_csv(file,rows)
        man_path=self.root/audit.MANIFEST_FILES['similarity']
        man=json.loads(man_path.read_text())
        man['fingerprints_sha256']=audit.digest(file)
        write_json(man_path,man)
        _,issues,_,_,_=audit.assess(self.root)
        self.assertIn('INDEX_NOT_EPISODE_FIRST',{x['kind'] for x in issues})

if __name__=='__main__':
    unittest.main()
