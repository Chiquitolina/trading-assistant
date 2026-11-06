#!/usr/bin/env python3
"""Regresiones del auditor de cobertura First Touch V2; sin Redis ni red."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import sqlite3
import struct
import sys
import tempfile
import unittest
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parent))
import reaction_lab_first_touch_recovery_audit_v1 as m

BASE = 1_780_000_000_000 // m.MINUTE * m.MINUTE


def candles(entry, n, price=100.0):
    return [(entry + i*m.MINUTE, price, price+1, price-1, price+0.5) for i in range(n)]


def blob(rows):
    return zlib.compress(b''.join(m.BAR.pack(*row) for row in rows)) if rows else None


def touch(state='STALE_BEFORE_COLLECTOR', count=0, price=100.0):
    entry = BASE + m.MINUTE
    return dict(touch_key='hash1', symbol='BTCUSDT', side='LONG', status='REACTION',
                retest_ts=BASE, entry_ts=entry, state=state, count=count,
                path_blob=blob(candles(entry, count, price=price)))


def auto(count, price=100.0, entry=None):
    entry = BASE+m.MINUTE if entry is None else entry
    return dict(event_key=f'BTCUSDT|LONG|{BASE}', symbol='BTCUSDT', side='LONG',
                retest_ts=BASE, entry_ts=entry, state='COMPLETE' if count==360 else 'ACTIVE',
                count=count, path_blob=blob(candles(entry, count, price)),
                continuous_scan=True)


class AuditPureTests(unittest.TestCase):
    def test_no_auto_match_returns_missing(self):
        r = m.audit_event(touch(), [], BASE + 500*m.MINUTE)
        self.assertEqual(r['audit_state'], 'NO_AUTO_MATCH')
        self.assertEqual(r['missing_bars_to_360'], 360)
        self.assertEqual(r['full_180m'], 0)

    def test_full_auto_path_from_zero(self):
        r = m.audit_event(touch(), [auto(360)], BASE + 500*m.MINUTE)
        self.assertEqual(r['audit_state'], 'AUTO_FULL_360')
        self.assertEqual(r['added_from_auto'], 360)
        self.assertEqual(r['source_parity_bars'], 0)
        self.assertIn('sin OHLC', r['note'])
        self.assertEqual(r['full_360m'], 1)

    def test_prefix_overlap_parity_then_auto_extension(self):
        r = m.audit_event(touch(count=50), [auto(200)], BASE+500*m.MINUTE)
        self.assertEqual(r['audit_state'], 'AUTO_EXTENDS_PREFIX')
        self.assertEqual(r['source_parity_bars'], 50)
        self.assertEqual(r['added_from_auto'], 150)
        self.assertEqual(r['full_180m'], 1)
        self.assertEqual(r['first_missing_ts'], BASE+201*m.MINUTE)

    def test_ohlc_conflict_is_excluded_and_prefix_kept(self):
        r = m.audit_event(touch(count=10), [auto(360, price=101.0)], BASE+500*m.MINUTE)
        self.assertEqual(r['audit_state'], 'AUTO_OHLC_CONFLICT')
        self.assertEqual(r['prefix_available'], 10)
        self.assertEqual(r['added_from_auto'], 0)
        self.assertEqual(m.windows_from_events([r], BASE+500*m.MINUTE), [])

    def test_bad_auto_contiguity_or_length_rejected(self):
        bad = auto(15)
        rows = candles(BASE + m.MINUTE, 15)
        rows[6] = (rows[6][0] + m.MINUTE,) + rows[6][1:]
        bad['path_blob'] = blob(rows)
        r = m.audit_event(touch(), [bad], BASE+500*m.MINUTE)
        self.assertEqual(r['audit_state'], 'AUTO_PATH_CORRUPT')

    def test_ambiguous_auto_does_not_choose(self):
        a = auto(360)
        b = dict(a, event_key='other')
        r = m.audit_event(touch(), [a, b], BASE+500*m.MINUTE)
        self.assertEqual(r['audit_state'], 'AMBIGUOUS_AUTO_IDENTITY')
        self.assertEqual(r['prefix_available'], 0)

    def test_entry_mismatch_rejected(self):
        bad = auto(360, entry=BASE+2*m.MINUTE)
        r = m.audit_event(touch(), [bad], BASE+500*m.MINUTE)
        self.assertEqual(r['audit_state'], 'AUTO_ENTRY_MISMATCH')

    def test_merge_intervals_and_page_estimate(self):
        merged = m.merge_intervals([
            ('BTCUSDT', BASE+100*m.MINUTE, BASE+300*m.MINUTE),
            ('BTCUSDT', BASE+200*m.MINUTE, BASE+1800*m.MINUTE),
            ('ETHUSDT', BASE+100*m.MINUTE, BASE+200*m.MINUTE)
        ])
        self.assertEqual(len(merged), 2)
        self.assertEqual([i for i in merged if i[0]=='BTCUSDT'][0][2], BASE+1800*m.MINUTE)

    def test_recent_event_not_mature_excluded_from_windows(self):
        r = m.audit_event(touch(), [], BASE+120*m.MINUTE)
        self.assertEqual(r['ready_360m_asof'], 0)
        self.assertEqual(m.windows_from_events([r], BASE+120*m.MINUTE), [])

    def test_stale_first_touch_state_is_not_changed(self):
        r = m.audit_event(touch(), [auto(360)], BASE+500*m.MINUTE)
        self.assertEqual(r['ft_state'], 'STALE_BEFORE_COLLECTOR')


class IntegrationAuditTests(unittest.TestCase):
    def test_read_only_uses_real_schema_and_writes_only_reports(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            dbdir = root/'reports'/'reaction_lab'
            dbdir.mkdir(parents=True)
            ft = dbdir/'first_touch_paths_v2.sqlite3'
            ap = dbdir/'auto_paths_v1.sqlite3'
            with sqlite3.connect(ft) as db:
                db.execute('''CREATE TABLE paths (touch_key TEXT PRIMARY KEY, symbol TEXT,
                  side TEXT,status TEXT,retest_ts INTEGER,entry_ts INTEGER,state TEXT,
                  count INTEGER,path_blob BLOB)''')
                t=touch(count=15)
                db.execute('INSERT INTO paths VALUES (?,?,?,?,?,?,?,?,?)',
                           tuple(t[k] for k in ('touch_key','symbol','side','status',
                              'retest_ts','entry_ts','state','count','path_blob')))
                db.commit()
            with sqlite3.connect(ap) as db:
                db.executescript('''CREATE TABLE events (event_key TEXT PRIMARY KEY,
                  symbol TEXT,side TEXT,retest_ts INTEGER,entry_ts INTEGER,
                  state TEXT,count INTEGER,path_blob BLOB);
                  CREATE TABLE event_sources (event_key TEXT,source TEXT);''')
                a=auto(360)
                db.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?,?)',
                           tuple(a[k] for k in ('event_key','symbol','side','retest_ts',
                               'entry_ts','state','count','path_blob')))
                db.execute('INSERT INTO event_sources VALUES (?,?)', (a['event_key'],'continuous_scan'))
                db.commit()
            before=[hashlib.sha256(p.read_bytes()).hexdigest() for p in (ft,ap)]
            report, events, windows = m.run_audit(root, BASE+500*m.MINUTE)
            self.assertEqual(report['auto_fully_recovers_360'],1)
            self.assertEqual(report['matched_continuous_scan_memberships'],1)
            self.assertEqual(events[0]['source_parity_bars'],15)
            self.assertEqual(windows,[])
            self.assertEqual([hashlib.sha256(p.read_bytes()).hexdigest() for p in (ft,ap)],before)
            output = root/'new_reports'
            m.write_reports(output, report, events, windows)
            self.assertTrue((output/'summary.json').exists())
            self.assertEqual(len(json.loads((output/'summary.json').read_text())['by_date']),1)
            with self.assertRaises(FileExistsError):
                m.write_reports(output, report, events, windows)

    def test_no_auto_db_still_audits(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            dbdir=root/'reports'/'reaction_lab'
            dbdir.mkdir(parents=True)
            with sqlite3.connect(dbdir/'first_touch_paths_v2.sqlite3') as db:
                db.execute('''CREATE TABLE paths(touch_key TEXT PRIMARY KEY,symbol TEXT,
                  side TEXT,status TEXT,retest_ts INTEGER,entry_ts INTEGER,state TEXT,
                  count INTEGER,path_blob BLOB)''')
                t=touch()
                db.execute('INSERT INTO paths VALUES (?,?,?,?,?,?,?,?,?)',
                           tuple(t[k] for k in ('touch_key','symbol','side','status',
                               'retest_ts','entry_ts','state','count','path_blob')))
            report,rows,windows = m.run_audit(root, BASE+500*m.MINUTE)
            self.assertFalse(report['source_db_exists']['auto_paths_v1'])
            self.assertEqual(report['target_events'],1)
            self.assertEqual(len(windows),1)
            self.assertEqual(windows[0]['minutes'],360)


if __name__=='__main__':
    unittest.main(verbosity=2)
