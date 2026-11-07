"""Local causal/recovery contract tests, no network, no source SQLite changes."""
import csv
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

import reaction_lab_failed_reclaim_audit_v1 as v1
from reaction_lab_runner_discovery_all_touches_v1 import recovery_index

ROOT=Path(__file__).resolve().parent
SAMPLE=ROOT/'reports/reaction_lab/failed_reclaim_audit_sample_v1/journal_metadata_sample.csv'
HISTORY=ROOT/'reports/reaction_lab/first_touch_historical_recovery_v11'


def make_bars(prices, *, entry=1_800_000_060_000):
    return [(entry+i*60000, c, max(o,c,hi), min(o,c,lo), c)
            for i,(o,hi,lo,c) in enumerate(prices)]


def fixed(price,n=260):
    return make_bars([(price,price,price,price)]*n)


def journal_row(side='LONG',level=100,close=99.8,ret=1_800_000_000_000,key='T'):
    meta={'swing_price':level,'retest_close':close,'retest_high':max(close,100),
          'retest_low':min(close,100),'touch_key':key,'signal':side,
          'status':'TOUCH_FAILED','known_timestamp':ret+60000}
    return v1.metadata_row(dict(touch_key=key,symbol='ABCUSDT',side=side,
        status='TOUCH_FAILED',retest_ts=ret,known_ts=ret+60000,
        first_seen_ms=ret+60000,observation_type='LIVE_OBSERVED',
        causal_json=json.dumps(meta)))


def event_row(close=99.8,side='LONG',ret=1_800_000_000_000,key='T'):
    return dict(touch_key=key,symbol='ABCUSDT',side=side,first_touch_status='TOUCH_FAILED',
        retest_ts=ret,date_utc=v1.date_utc(ret),path_source='FIRST_TOUCH_V2',
        path_quality='CONTIGUOUS_VALID',retest_close=close,path_count=260)


class DetectorTests(unittest.TestCase):
    def test_long_band_uses_close_not_high(self):
        b=fixed(99.7)
        b[0]=(b[0][0],99.7,100.5,99.5,99.7)
        b[1]=(b[1][0],99.7,99.92,99.7,99.92)
        self.assertEqual(v1.find_band_entry(b,100,'LONG',30),(2,'LONG'))

    def test_short_band_uses_close(self):
        b=fixed(100.2)
        b[0]=(b[0][0],100.2,100.5,99.5,100.2)
        b[1]=(b[1][0],100.2,100.2,100.07,100.07)
        self.assertEqual(v1.find_band_entry(b,100,'SHORT',30),(2,'SHORT'))

    def test_band_no_signal(self):
        self.assertIsNone(v1.find_band_entry(fixed(99.5),100,'LONG',30))

    def test_reclaim_requires_close_above_buffer(self):
        from reaction_lab_failed_entry_timing_v1 import find_entry
        b=fixed(99.8)
        b[0]=(b[0][0],99.8,100.5,99.7,100.02) # hit level but not +.05% close
        b[1]=(b[1][0],100.02,100.06,100.02,100.06)
        row={'side':'LONG','meta':{'swing_price':100}}
        self.assertEqual(find_entry('ORIGINAL_RECLAIM',b,row,30),(2,'LONG'))
        self.assertIsNone(find_entry('ORIGINAL_RECLAIM_2_CLOSES',b,row,2))
        b[2]=(b[2][0],100.06,100.1,100.06,100.09)
        self.assertEqual(find_entry('ORIGINAL_RECLAIM_2_CLOSES',b,row,30),(3,'LONG'))

    def test_future_bars_cannot_change_first_trigger(self):
        b=fixed(99.5)
        b[4]=(b[4][0],99.5,100.4,99.5,99.92)
        later=b[:]
        for i in range(7,40):
            later[i]=(later[i][0],99,101,95,99)
        self.assertEqual(v1.find_band_entry(b,100,'LONG',40),
                         v1.find_band_entry(later,100,'LONG',40))

    def test_all_modes_available(self):
        self.assertEqual(len(v1.MODES),9)
        self.assertIn('ORIGINAL_RECLAIM_2_CLOSES',v1.MODES)
        self.assertIn('INVERSE_RETEST_REJECTION',v1.MODES)


class CausalityAndCohortTests(unittest.TestCase):
    def base(self, n=260):
        bars=fixed(99.8,n)
        j=journal_row()
        b=v1.event_base(event_row(),bars,j,60,180)
        return b,bars,j

    def test_failed_status_close_depth_is_valid(self):
        b,bars,j=self.base()
        self.assertEqual(b['metadata_state'],'VALID_FAILED')
        self.assertAlmostEqual(b['fail_depth_close_pct'],.2)
        self.assertEqual(b['known_ts'],j['known_ts'])

    def test_invalid_identity_rejected(self):
        _,_,j=self.base()
        wrong=event_row(close=99.6)
        self.assertEqual(v1.checked_metadata(wrong,j)[0],'RETEST_CLOSE_CONFLICT')

    def test_future_label_not_rewritten(self):
        _,_,j=self.base()
        wrong=event_row()
        j['meta']['status']='REACTION'
        self.assertEqual(v1.checked_metadata(wrong,j)[0],'IDENTITY_MISMATCH')

    def test_recovery_known_close_is_next_open(self):
        base,bars,j=self.base()
        bars[0]=(bars[0][0],99.8,100,99.8,99.92)
        result=v1.calculate_mode(base,bars,j['meta'],'BAND_REENTRY',180,60,2,.2)
        self.assertEqual(result['entry_min_after_fail'],1)
        self.assertEqual(result['signal_close_ts'],bars[1][0])
        self.assertEqual(result['entry_ts'],bars[1][0])
        self.assertEqual(result['signal_state'],'SIGNAL')

    def test_no_signal_kept_in_denominator(self):
        base,bars,j=self.base()
        o=v1.calculate_mode(base,bars,j['meta'],'ORIGINAL_RECLAIM',180,60,2,.2)
        self.assertEqual((o['cohort_eligible'],o['signal_state'],o['outcome']),
                         (True,'NO_SIGNAL','NO_TRADE'))

    def test_censored_kept_out_of_denominator(self):
        base,bars,j=self.base(n=239)
        self.assertFalse(base['eligible_common_180m'])
        o=v1.calculate_mode(base,bars,j['meta'],'IMMEDIATE_ORIGINAL',180,60,2,.2)
        self.assertEqual(o['signal_state'],'CENSORED_COMMON_HORIZON')

    def test_exit_gap_timestamp_is_open_not_close(self):
        base,bars,j=self.base()
        bars[1]=(bars[1][0],98.0,98.0,97.9,97.95)
        o=v1.calculate_mode(base,bars,j['meta'],'IMMEDIATE_ORIGINAL',180,60,2,.2)
        self.assertEqual(o['outcome'],'SL_GAP')
        self.assertEqual(o['exit_ts'],bars[1][0])

    def test_same_bar_ambiguous_not_tp(self):
        base,bars,j=self.base()
        bars[0]=(bars[0][0],99.8,102.1,98.0,99.8)
        o=v1.calculate_mode(base,bars,j['meta'],'IMMEDIATE_ORIGINAL',180,60,2,.2)
        self.assertEqual(o['outcome'],'SL_AMBIGUOUS')
        self.assertEqual(o['gross_pct'],-1.0)
        self.assertEqual(o['net_pct'],-1.2)

    def test_signal_next_open_not_trigger_close(self):
        base,bars,j=self.base()
        bars[0]=(bars[0][0],99.8,100.10,99.7,100.10)
        bars[1]=(bars[1][0],100.15,100.2,99.9,100.1)
        o=v1.calculate_mode(base,bars,j['meta'],'ORIGINAL_RECLAIM',180,60,2,.2)
        self.assertEqual(o['entry_open'],100.15)
        self.assertEqual(o['entry_ts'],bars[1][0])


@unittest.skipUnless(SAMPLE.is_file() and HISTORY.is_dir(),'Full pilot fixture not present')
class RealPilotTests(unittest.TestCase):
    def test_real_sample_validated_sha_and_full_360(self):
        index,batches=recovery_index(HISTORY)
        self.assertGreaterEqual(batches,1)
        sample=v1.read_sample_journal(SAMPLE)
        self.assertEqual(len(sample),106)
        x=list(v1.sample_paths(sample,HISTORY))
        self.assertEqual(len(x),106)
        self.assertTrue(all(len(b)==360 and r['path_quality']=='CONTIGUOUS_VALID' for r,b in x))
        self.assertEqual({v1.checked_metadata(r,sample[r['touch_key']])[0] for r,b in x},
                         {'VALID_FAILED'})

    def test_sample_derived_write_does_not_change_sources(self):
        db=HISTORY/'batch_05779c29866d'/'recovered_paths.sqlite3'
        before=hashlib.sha256(db.read_bytes()).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            x=v1.build(ROOT,sample_metadata=SAMPLE,recovery_root=HISTORY,
                       max_wait=60,hold=180,out=tmp)
            self.assertEqual((x['cohort_total'],x['coverage_common']), (106,106))
            with open(Path(tmp)/'event_outcomes.csv') as f:
                rows=list(csv.DictReader(f))
            self.assertEqual(len(rows),106*len(v1.MODES)*len(v1.TARGETS))
            with open(Path(tmp)/'summary.csv') as f:
                summary=list(csv.DictReader(f))
            r=next(s for s in summary if s['group_type']=='ALL' and
                   s['mode']=='ORIGINAL_RECLAIM_2_CLOSES' and s['target_pct']=='2')
            self.assertEqual(int(r['signal_n'])+int(r['no_signal_n'])+int(r['invalid_metadata_n']),106)
            self.assertEqual(int(r['censored_n']),0)
        self.assertEqual(hashlib.sha256(db.read_bytes()).hexdigest(),before)


if __name__=='__main__':unittest.main()

class FullSourceIntegrationTests(unittest.TestCase):
    def test_live_sqlite_reader_read_only_and_full_dataset(self):
        from reaction_lab_runner_discovery_all_touches_v1 import BAR
        import zlib
        with tempfile.TemporaryDirectory() as tmp:
            project=Path(tmp)
            root=project/'reports/reaction_lab'
            root.mkdir(parents=True)
            ft=root/'first_touch_paths_v2.sqlite3'
            journal=root/'scanner_first_touches_v1.sqlite3'
            ts=60000;key='TEST|LONG|15m|3x3|60000|60000|60000'
            bars=[(ts+60000+i*60000,99.8,99.95,99.65,99.8) for i in range(360)]
            bb=zlib.compress(b''.join(BAR.pack(*b) for b in bars))
            with sqlite3.connect(ft) as db:
                db.execute('''CREATE TABLE paths(touch_key TEXT PRIMARY KEY, symbol TEXT,
                    side TEXT, status TEXT, retest_ts INTEGER,entry_ts INTEGER,first_seen_ms INTEGER,
                    state TEXT,count INTEGER,path_blob BLOB,retest_close REAL)''')
                db.execute('INSERT INTO paths VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                    (key,'TEST','LONG','TOUCH_FAILED',ts,ts+60000,ts+60000,'COMPLETE',360,bb,99.8))
            j=journal_row(ret=ts,key=key)
            j['symbol']='TEST'
            meta=j['meta'];meta['symbol']='TEST'
            with sqlite3.connect(journal) as db:
                db.execute('''CREATE TABLE first_touches(touch_key TEXT PRIMARY KEY, symbol TEXT,
                    side TEXT,status TEXT,retest_ts INTEGER,known_ts INTEGER,first_seen_ms INTEGER,
                    observation_type TEXT,causal_json TEXT)''')
                db.execute('INSERT INTO first_touches VALUES (?,?,?,?,?,?,?,?,?)',
                    (key,'TEST','LONG','TOUCH_FAILED',ts,ts+60000,ts+60000,
                     'LIVE_OBSERVED',json.dumps(meta)))
            hashes=[hashlib.sha256(x.read_bytes()).hexdigest() for x in (ft,journal)]
            manifest=v1.build(project,out=project/'derived')
            self.assertEqual(manifest['scope'],'FULL_VPS_VERIFIED_ONLINE_AND_HISTORY')
            self.assertEqual(manifest['cohort_total'],1)
            self.assertEqual(manifest['coverage_common'],1)
            self.assertEqual(manifest['metadata_states'],{'VALID_FAILED':1})
            self.assertEqual(manifest['sources'],{'FIRST_TOUCH_V2':1})
            self.assertEqual([hashlib.sha256(x.read_bytes()).hexdigest() for x in (ft,journal)],hashes)

@unittest.skipUnless(SAMPLE.is_file() and HISTORY.is_dir(),'Real pilot fixture not present')
class RunnerParityTests(unittest.TestCase):
    def test_immediate_tp2_matches_historical_runner_barriers(self):
        path=HISTORY/'batch_05779c29866d'/'events_outcomes.csv'
        with path.open(newline='',encoding='utf-8') as file:
            historical={r['touch_key']:r for r in csv.DictReader(file)
                        if r['first_touch_status']=='TOUCH_FAILED'}
        journal=v1.read_sample_journal(SAMPLE)
        n=0
        for row,bars in v1.sample_paths(journal,HISTORY):
            base=v1.event_base(row,bars,journal[row['touch_key']],60,180)
            found=v1.calculate_mode(base,bars,journal[row['touch_key']]['meta'],
                                    'IMMEDIATE_ORIGINAL',180,60,2,.2)
            old=historical[row['touch_key']]
            label=old['open_tp2_result']
            minute=old['open_tp2_minute']
            if not minute or int(minute)>180:
                self.assertEqual(found['outcome'],'TIME_EXIT')
            else:
                expected='SL_AMBIGUOUS' if label=='AMBIGUOUS' else label
                self.assertEqual((found['outcome'],found['exit_min_after_entry']),
                                 (expected,int(minute)))
            n+=1
        self.assertEqual(n,106)

class LiveCollectorRaceTests(unittest.TestCase):
    def test_journal_record_not_yet_in_paths_is_censored_not_dropped(self):
        from unittest.mock import patch
        record=journal_row()
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(v1,'read_sample_journal',return_value={'T':record}), \
                 patch.object(v1,'sample_paths',return_value=iter(())):
                manifest=v1.build(tmp,sample_metadata='ignored.csv',out=Path(tmp)/'derived')
            self.assertEqual(manifest['cohort_total'],1)
            self.assertEqual(manifest['journal_without_path'],1)
            self.assertEqual(manifest['coverage_common'],0)
            with (Path(tmp)/'derived'/'event_outcomes.csv').open() as f:
                rows=list(csv.DictReader(f))
            self.assertEqual(len(rows),27)
            self.assertTrue(all(r['outcome']=='CENSORED' for r in rows))
