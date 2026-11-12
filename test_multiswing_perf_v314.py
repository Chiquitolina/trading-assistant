"""V3.1.4 read-only performance regression and causal equivalence.

Synthetic fixtures only. Never connects to LIVE, Redis, HLL or network.
Wall-time performance is *not* asserted (hardware dependent).
"""
from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

import numpy as np
import pandas as pd

from btc_multiswing_60d_explorer_v1 import TF_MINUTES, M, group_asof, window_evidence_stats
from btc_multiswing_structural_confluence_v2 import construct_regions
from dashboard.views.reaction_continuous_multiswing_view import (
    CONTEXT_DB, read_frozen_event, _cached_original_context,
)
from dashboard.views import reaction_multiswing_btc60d_view_v1 as view


def reference_group_asof(rows, *, collapse=False, multi_only=False):
    """Frozen pre-optimization V1 grouping, retained in tests for exact parity."""
    if rows.empty:
        return pd.DataFrame(columns=[*rows.columns, 'representations_asof', 'tf_set_asof',
                                     'first_available_asof', 'last_available_asof', 'plot_ts', 'group_key'])
    frame = rows.copy()
    known = frame['physical_identity_state'].eq('VERIFIED') & frame['physical_id'].ne('')
    frame['group_key'] = frame['pivot_id'].astype(str)
    frame.loc[known, 'group_key'] = frame.loc[known, 'physical_id'].astype(str)
    for _, g in frame.loc[known].groupby('group_key'):
        if (len(set(g['side'])) != 1 or g['physical_minute_ts'].nunique(dropna=True) != 1
                or g['price'].nunique() != 1):
            raise ValueError('PHYSICAL_GROUP_CONFLICT')
    info = {}
    for key, g in frame.groupby('group_key', sort=False):
        tfs = tuple(sorted(g.timeframe.unique(), key=lambda tf: TF_MINUTES[tf]))
        info[key] = (len(g), ','.join(tfs), int(g.available_ts.min()),
                     int(g.available_ts.max()))
    for i, col in enumerate(('representations_asof', 'tf_set_asof',
                              'first_available_asof', 'last_available_asof')):
        frame[col] = frame['group_key'].map(lambda k: info[k][i])
    frame['plot_ts'] = frame['pivot_ts'].astype('int64')
    frame.loc[known, 'plot_ts'] = frame.loc[known, 'physical_minute_ts'].astype('int64')
    if collapse:
        frame['_tf_rank'] = frame['timeframe'].map(TF_MINUTES)
        frame = (frame.sort_values(['group_key', '_tf_rank', 'available_ts'])
                 .drop_duplicates('group_key', keep='last')
                 .drop(columns='_tf_rank'))
    if multi_only:
        frame = frame[(frame['physical_identity_state'] == 'VERIFIED') &
                      (frame['representations_asof'] >= 2)]
    return frame.sort_values(['plot_ts', 'side', 'timeframe']).reset_index(drop=True)


def representative_catalog(n=2600, physical_groups=1750):
    rng = np.random.default_rng(314)
    physical = rng.integers(0, physical_groups, size=n)
    tf = np.array(list(TF_MINUTES))[rng.integers(0, len(TF_MINUTES), size=n)]
    base = 1790000000000
    f = pd.DataFrame({
        'pivot_id': ['p'+str(i) for i in range(n)],
        'physical_id': ['g'+str(g) for g in physical],
        'physical_identity_state': ['VERIFIED'] * n,
        'timeframe': tf,
        'side': np.where(physical % 2, 'HIGH', 'LOW'),
        'physical_minute_ts': base + physical * 600000,
        'pivot_ts': base + physical * 600000,
        'available_ts': base + physical * 600000 + 3600000,
        'price': 76000 + physical // 10,
    })
    # Ambiguous pivots must be preserved separately, even if near a known physical.
    tied = f.iloc[:12].copy()
    tied['pivot_id'] = ['t'+str(i) for i in range(len(tied))]
    tied['physical_identity_state'] = 'TIED_EXTREMA'
    tied['physical_id'] = ''
    tied['physical_minute_ts'] = np.nan
    return pd.concat([f, tied], ignore_index=True)


class MultiSwingPerformanceV314(unittest.TestCase):
    def test_vectorized_physical_groups_exactly_match_legacy(self):
        rows=representative_catalog()
        for cutoff in (1790000000000+100*600000, int(rows.available_ts.max())):
            known = rows.loc[rows.available_ts <= cutoff].copy()
            for collapsed in (False, True):
                for multi in (False, True):
                    with self.subTest(cutoff=cutoff, collapsed=collapsed, multi=multi):
                        before = reference_group_asof(known, collapse=collapsed,
                                                      multi_only=multi)
                        after = group_asof(known, collapse=collapsed, multi_only=multi)
                        pd.testing.assert_frame_equal(before, after)

    def test_conflicting_identity_still_fails_closed(self):
        rows=representative_catalog(100, 30)
        pair=rows.loc[rows.physical_id.eq('g1') & rows.physical_identity_state.eq('VERIFIED')]
        if len(pair)<2:
            rows=pd.concat([rows,rows.loc[rows.physical_id.eq('g1')].iloc[:1].assign(
                pivot_id='extra',price=100.0)],ignore_index=True)
        else:
            rows.loc[pair.index[0], 'price'] *= 1.2
        with self.assertRaisesRegex(ValueError, 'PHYSICAL_GROUP_CONFLICT'):
            group_asof(rows,collapse=True)

    def test_region_state_identical_when_future_pivot_is_unavailable(self):
        rows=representative_catalog(450,300)
        cut=int(rows.available_ts.max())-60*M
        early=rows.loc[rows.available_ts <= cut].copy()
        before=construct_regions(early,cut)
        # Mutating a future row must not rewrite the causal PRE catalog.
        later=rows.copy()
        future=later.available_ts > cut
        later.loc[future,'price'] += 25000
        after=construct_regions(later.loc[later.available_ts <= cut].copy(),cut)
        for old,new in zip(before[:2],after[:2]):
            pd.testing.assert_frame_equal(old,new)
        self.assertEqual(before[2],after[2])

    def test_sqlite_snapshot_cached_and_invalidates_after_update(self):
        _cached_original_context.cache_clear()
        with tempfile.TemporaryDirectory() as tmp:
            db=Path(tmp)/CONTEXT_DB
            db.parent.mkdir(parents=True)
            with sqlite3.connect(db) as con:
                con.execute('CREATE TABLE contexts (event_key TEXT PRIMARY KEY, known_ts INTEGER, '
                            'status TEXT, candles_json TEXT)')
                con.execute('INSERT INTO contexts VALUES (?,?,?,?)',
                            ('event-1',60000,'ORIGINAL','{"1m": []}'))
            x=read_frozen_event(tmp,'event-1',60000)
            self.assertEqual(x[1],'ORIGINAL')
            y=read_frozen_event(tmp,'event-1',60000)
            self.assertIs(x[0],y[0])
            self.assertGreaterEqual(_cached_original_context.cache_info().hits,1)
            with sqlite3.connect(db) as con:
                con.execute('UPDATE contexts SET status=?, candles_json=? WHERE event_key=?',
                            ('UPDATED','{"1m": [1]}','event-1'))
            # Altering the timestamp unambiguously models a new journal revision.
            stat=db.stat()
            os.utime(db, ns=(stat.st_atime_ns,stat.st_mtime_ns+1_000_000))
            z=read_frozen_event(tmp,'event-1',60000)
            self.assertEqual(z[1],'UPDATED')
            self.assertEqual(z[0]['1m'],[1])
            self.assertEqual(read_frozen_event(tmp,'event-1',0)[1],
                             'KNOWN_TS_MISMATCH')

    def test_view_never_prepares_hidden_pivot_markers(self):
        import inspect
        src=inspect.getsource(view.render_btc_multiswing_60d_view)
        self.assertIn('if show_all_pivots:',src)
        self.assertIn("markers=pd.DataFrame(columns=['timeframe','side','plot_ts'])",src)
        self.assertIn('if mode == \'INSPECTOR\':\n        display=',src)


if __name__ == '__main__':
    unittest.main()
