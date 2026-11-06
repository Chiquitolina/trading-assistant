#!/usr/bin/env python3
"""Regression tests for offline PRE->POST join V1.2; no external data needed."""
import csv
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import zlib

from reaction_multi_swing_outcome_join_v12 import (
    BAR, MINUTE, sha256_file, load_features, open_ro, decode_path,
    defense_30m, opposite_break_60m, new_outcome, join_rows, save_results,
)


def candles(n=60, close=100.0, start=1_800_000_000_000):
    return [(start + i * MINUTE, close, close, close, close) for i in range(n)]


def encoded(bars):
    return zlib.compress(b''.join(BAR.pack(*b) for b in bars)) if bars else None


def feat(**changes):
    f = dict(touch_key='X|LONG|1', symbol='BTCUSDT', side='LONG', original_status='REACTION',
             swing_price='100', retest_ts='1799999940000', known_ts='1800000000000',
             v11_state='V11_OK', w240_near_zone_independent_extrema='2')
    f.update(changes)
    return f


def path(**changes):
    bars = candles()
    p = dict(symbol='BTCUSDT', side='LONG', status='REACTION',
             retest_ts=1_799_999_940_000, entry_ts=1_800_000_000_000,
             state='COMPLETE', count=len(bars), path_blob=encoded(bars))
    p.update(changes)
    return tuple(p[k] for k in ('symbol','side','status','retest_ts','entry_ts','state','count','path_blob'))


class DefenseTests(unittest.TestCase):
    def test_favorable_long(self):
        b = candles(); b[2] = (b[2][0],100,100.6,100,100.4)
        self.assertEqual(defense_30m(b,'LONG',100),('FAVORABLE_FIRST',3))

    def test_favorable_short(self):
        b = candles(close=100); b[1] = (b[1][0],100,100,99.3,99.7)
        self.assertEqual(defense_30m(b,'SHORT',100),('FAVORABLE_FIRST',2))

    def test_wick_invalid_before_target(self):
        b = candles(); b[0] = (b[0][0],100,100.1,99.99,100.02)
        b[1] = (b[1][0],100,100.6,99.99,100.5)
        self.assertEqual(defense_30m(b,'LONG',100),('INVALIDATION_FIRST',1))

    def test_same_bar_ambiguous_not_win(self):
        b = candles(); b[0] = (b[0][0],100,100.7,99.90,100.1)
        self.assertEqual(defense_30m(b,'LONG',100),('AMBIGUOUS_SAME_BAR',1))

    def test_favorable_short_same_bar(self):
        b = candles(); b[0] = (b[0][0],100,100.04,99.4,99.9)
        self.assertEqual(defense_30m(b,'SHORT',100),('AMBIGUOUS_SAME_BAR',1))

    def test_bad_entry_is_distinct(self):
        b = candles(close=99)
        self.assertEqual(defense_30m(b,'LONG',100),('INVALIDATION_FIRST',1))

    def test_neither_full_and_censored_partial(self):
        self.assertEqual(defense_30m(candles(),'LONG',100),('NEITHER',None))
        self.assertEqual(defense_30m(candles(29),'LONG',100),('CENSORED',None))

    def test_future_path_mutation_no_effect_after_30(self):
        a = candles(); b = list(a)
        b[45] = (b[45][0],100,101.2,97.0,99)
        self.assertEqual(defense_30m(a,'LONG',100),defense_30m(b,'LONG',100))


class BreakTests(unittest.TestCase):
    def test_long_opposite_two_closes_and_confirmation_is_second_close(self):
        b = candles();
        b[3]=(b[3][0],100,100,99.82,99.88)
        b[4]=(b[4][0],99.88,99.88,99.7,99.75)
        self.assertEqual(opposite_break_60m(b,'LONG',100),('OPPOSITE_CONFIRMED',b[4][0]+MINUTE))

    def test_short_opposite_symmetric(self):
        b = candles();
        b[0]=(b[0][0],100,100.25,99.99,100.12)
        b[1]=(b[1][0],100.12,100.3,100.05,100.16)
        self.assertEqual(opposite_break_60m(b,'SHORT',100),('OPPOSITE_CONFIRMED',b[1][0]+MINUTE))

    def test_single_close_does_not_confirm(self):
        b = candles(); b[0]=(b[0][0],100,100,99.8,99.8)
        self.assertEqual(opposite_break_60m(b,'LONG',100),('NOT_CONFIRMED',None))

    def test_partial_censored_and_future_60_not_read(self):
        a = candles(59)
        self.assertEqual(opposite_break_60m(a,'LONG',100),('CENSORED',None))
        b = candles(62)
        b[60]=(b[60][0],100,100,99.7,99.7)
        b[61]=(b[61][0],99.7,99.7,99.6,99.6)
        self.assertEqual(opposite_break_60m(b,'LONG',100),('NOT_CONFIRMED',None))

    def test_strict_band_equality_not_confirmed(self):
        b = candles();
        # equality is outside? No: rule requires strict close below 99.9
        b[1]=(b[1][0],100,100,99.9,99.9)
        b[2]=(b[2][0],99.9,99.9,99.9,99.9)
        self.assertEqual(opposite_break_60m(b,'LONG',100),('NOT_CONFIRMED',None))


class DataTests(unittest.TestCase):
    def test_contiguous_1m_validation(self):
        b=candles(2); b[1]=(b[1][0]+MINUTE,*b[1][1:])
        with self.assertRaisesRegex(ValueError,'discontinuo'):
            decode_path(encoded(b),2,b[0][0])

    def test_bad_ohlc_validation(self):
        b=candles(1); b[0]=(b[0][0],100,99,98,100)
        with self.assertRaisesRegex(ValueError,'OHLC'):
            decode_path(encoded(b),1,b[0][0])

    def test_missing_path_is_not_no_break(self):
        a=new_outcome(feat(),None)
        self.assertEqual(a['v12_quality_state'],'NOT_COVERED')
        self.assertEqual(a['v12_opposite_break_60m'],'NOT_EVALUABLE')

    def test_identity_mismatch_rejected(self):
        a=new_outcome(feat(),path(status='TOUCH_FAILED'))
        self.assertEqual(a['v12_quality_state'],'IDENTITY_MISMATCH')

    def test_status_immutable_and_pre_fields_immutable(self):
        f=feat(); snapshot=f.copy()
        with tempfile.TemporaryDirectory() as d:
            db=Path(d)/'paths.sqlite3'; con=sqlite3.connect(db)
            con.execute('CREATE TABLE paths(touch_key TEXT PRIMARY KEY, symbol, side, status, retest_ts,entry_ts,state,count,path_blob)')
            con.execute('INSERT INTO paths VALUES (?,?,?,?,?,?,?,?,?)',('X|LONG|1',*path()))
            con.commit();con.close()
            with open_ro(db) as ro:
                rows,_=join_rows([f],ro)
            self.assertEqual(f,snapshot)
            for k,v in f.items(): self.assertEqual(rows[0][k],v)
            self.assertEqual(rows[0]['v12_quality_state'],'JOINED')
            self.assertEqual(rows[0]['v12_path_covered_60m'],1)

    def test_partial_path_early_win_censored_conservatively(self):
        b=candles(6); b[2]=(b[2][0],100,100.8,100,100.5)
        a=new_outcome(feat(),path(count=6,path_blob=encoded(b),state='ACTIVE'))
        self.assertEqual(a['v12_defense_30m'],'CENSORED')
        self.assertEqual(a['v12_path_covered_30m'],0)
        self.assertEqual(a['v12_opposite_break_60m'],'CENSORED')

    def test_censored_not_neither_with_short_path(self):
        b=candles(3)
        a=new_outcome(feat(),path(count=3,path_blob=encoded(b),state='ACTIVE'))
        self.assertEqual(a['v12_defense_30m'],'CENSORED')
        self.assertEqual(a['v12_opposite_break_60m'],'CENSORED')

    def test_missing_frozen_pre_kept_separate(self):
        a=new_outcome(feat(v11_state='V11_PARTIAL'),path())
        self.assertEqual(a['v12_quality_state'],'PRE_NOT_COVERED')

    def test_manifest_sha_rejects_file_alteration(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); csvfile=root/'features.csv'; man=root/'manifest.json'
            f=feat()
            with csvfile.open('w',newline='') as out:
                w=csv.DictWriter(out,fieldnames=list(f));w.writeheader();w.writerow(f)
            man.write_text(json.dumps(dict(dataset_version='REACTION_MULTI_SWING_QUALITY_DATASET_V11',output_sha256=sha256_file(csvfile),structural_cutoff='retest_ts (PRE_TOUCH)')))
            self.assertEqual(len(load_features(csvfile)[0]),1)
            csvfile.write_text(csvfile.read_text()+'X')
            with self.assertRaisesRegex(ValueError,'SHA256'):
                load_features(csvfile)

    def test_new_output_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/'result'
            save_results(root,[dict(touch_key='X')],{})
            with self.assertRaises(FileExistsError):
                save_results(root,[dict(touch_key='X')],{})


if __name__=='__main__':
    unittest.main()
