"""Offline tests for original pivot identity and strictly PRE-pivot historical memory."""
import sys
import tempfile
import unittest
import json
import sqlite3
import types
from pathlib import Path
from unittest.mock import patch

import pandas as pd

try:
    import reaction_structural_level_atlas
except ImportError:
    # The user VPS already has this existing module at the project root.
    local=Path('/mnt/data/horizontal_bulk_v1/src')
    if local.exists():sys.path.insert(0,str(local))

import audit_pivot_historical_memory_pre as m


def frame(touches=(35,50,92),peak=103,level_side='HIGH',pivot_index=190):
    rows=[]
    for k in range(210):
        price=100 if level_side=='HIGH' else 105
        o=price;c=price
        h=price+.3;l=price-.3
        if k in touches:
            if level_side=='HIGH':h=peak
            else:l=peak
        if k==pivot_index:
            if level_side=='HIGH':h=peak
            else:l=peak
        rows.append(dict(timestamp=k*m.STEP,open=o,high=h,low=l,close=c,volume=50))
    return pd.DataFrame(rows)


class PurePivotMemory(unittest.TestCase):
    def ev(self,pivot_index=190,side='SHORT'):
        return dict(event_key=f'XYZ|{side}|{(pivot_index+20)*m.STEP}',symbol='XYZ',side=side,
                    retest_ts=(pivot_index+20)*m.STEP,date_utc='2026-10-22',observation_type='LIVE_OBSERVED')

    def test_two_old_ceiling_visits_and_responses(self):
        bars=frame(pivot_index=190)
        group,r,why=m.qualified_histories(bars,{'pivot_ts':190*m.STEP,'pivot_price':103.,'side':'SHORT'})
        self.assertEqual(group,'PRIOR_RESISTANCE')
        self.assertEqual(r['prior_ceiling_visits'],3)
        self.assertEqual(r['prior_floor_visits'],0)
        self.assertTrue(all(int(v)<190*m.STEP for v in r['prior_responses_close_times'].split(';')))
        self.assertEqual(why,'EXPLICIT_CAUSAL_PRICE')

    def test_two_old_floor_visits(self):
        bars=frame(touches=(35,74),peak=102,level_side='LOW',pivot_index=190)
        # Historical lows near 102 and independent rallies back above the zone.
        grp,p,_=m.qualified_histories(bars,{'pivot_ts':190*m.STEP,'pivot_price':102.,'side':'LONG'})
        self.assertEqual(grp,'PRIOR_SUPPORT')
        self.assertEqual(p['prior_floor_visits'],2)

    def test_closes_after_pivot_are_never_historical_responses(self):
        b=frame(touches=(35,188),pivot_index=190)
        # At 188, 3 confirmation bars would close after the pivot begins.
        group,r,_=m.qualified_histories(b,{'pivot_ts':190*m.STEP,'pivot_price':103.,'side':'SHORT'})
        self.assertEqual(group,'NO_PRIOR_LEVEL')
        self.assertEqual(r['prior_ceiling_visits'],1)

    def test_gap_is_unknown_not_absence(self):
        b=frame()
        b=b[b.timestamp!=140*m.STEP].copy()
        group,_,reason=m.qualified_histories(b,{'pivot_ts':190*m.STEP,'pivot_price':103,'side':'SHORT'})
        self.assertEqual(group,'UNKNOWN_HISTORY')
        self.assertEqual(reason,'GAPPED_PRE_PIVOT_15M')

    def test_mismatched_explicit_pivot_price_fails(self):
        group,_,reason=m.qualified_histories(frame(),{'pivot_ts':190*m.STEP,'pivot_price':105,'side':'SHORT'})
        self.assertEqual(group,'UNKNOWN_PIVOT')
        self.assertEqual(reason,'PIVOT_PRICE_MISMATCH_WITH_15M')

    def test_insufficient_history_is_unknown(self):
        b=frame().iloc[120:].copy()
        group,_,_=m.qualified_histories(b,{'pivot_ts':190*m.STEP,'pivot_price':103,'side':'SHORT'})
        self.assertEqual(group,'UNKNOWN_HISTORY')

    def test_event_future_candle_does_not_affect_label(self):
        b=frame()
        a=self.ev()
        payload=json.dumps({'pivot_timestamp':190*m.STEP,'swing_price':103})
        first=m.classify_event(a,payload,b)
        b.loc[b.index>=191,'high']=100000
        second=m.classify_event(a,payload,b)
        self.assertEqual(first,second)
        self.assertEqual(first['memory_group_pre'],'PRIOR_RESISTANCE')

    def test_reject_nearest_pivot_guess(self):
        ev=self.ev()
        row=m.classify_event(ev,json.dumps({'swing_price':103}),frame())
        self.assertEqual(row['memory_group_pre'],'UNKNOWN_PIVOT')
        self.assertIn('MISSING_PIVOT_TIMESTAMP',row['reason'])

    def test_anchor_from_exact_pivot_to_retest_age(self):
        ev=self.ev()
        anchor,err=m.anchor_from_causal({'pivot_to_retest_min':20*15,'swing_price':103},ev)
        self.assertIsNone(err)
        self.assertEqual(anchor['pivot_ts'],190*m.STEP)

    def test_anchor_must_be_exact_15m_epoch(self):
        anchor,why=m.anchor_from_causal({'pivot_timestamp':190*m.STEP+60_000,'swing_price':103},self.ev())
        self.assertIsNone(anchor)
        self.assertEqual(why,'INVALID_PIVOT_TIMESTAMP')

    def test_prior_2_uses_time_span_not_simply_count(self):
        group,r,_=m.qualified_histories(frame(touches=(70,77),pivot_index=190),
                    {'pivot_ts':190*m.STEP,'pivot_price':103,'side':'SHORT'})
        self.assertEqual(group,'NO_PRIOR_LEVEL')
        self.assertEqual(r['prior_ceiling_visits'],2)

    def test_unknown_without_pivot_candle(self):
        b=frame()
        b=b[b.timestamp!=190*m.STEP].copy()
        group,_,reason=m.qualified_histories(b,{'pivot_ts':190*m.STEP,'pivot_price':103,'side':'SHORT'})
        self.assertEqual((group,reason),('UNKNOWN_PIVOT','PIVOT_BAR_NOT_ARCHIVED'))

    def test_respects_min_historical_confirmation_before_pivot(self):
        b=frame(touches=(35,190),pivot_index=190)
        grp,r,_=m.qualified_histories(b,{'pivot_ts':190*m.STEP,'pivot_price':103,'side':'SHORT'})
        self.assertEqual(grp,'NO_PRIOR_LEVEL')
        self.assertEqual(r['prior_ceiling_visits'],1)

    def test_mixed_floor_and_ceiling_prior_to_current_pivot(self):
        b=frame(touches=(35,),peak=103,level_side='HIGH',pivot_index=190)
        # Later price regime is higher, allowing prior floor at the same level.
        for k in range(99,185):
            b.loc[k,['open','high','low','close']]=[105,105.3,104.7,105]
        b.loc[135,'low']=103
        b.loc[190,['open','high','low','close']]=[100,103,99.7,100]
        grp,p,_=m.qualified_histories(b,{'pivot_ts':190*m.STEP,'pivot_price':103,'side':'SHORT'})
        self.assertEqual(grp,'PRIOR_MIXED_SR')
        self.assertEqual((p['prior_ceiling_visits'],p['prior_floor_visits']),(1,1))

    def test_symbol_resume_audit_and_pre_only_csv(self):
        ev=self.ev()
        df=pd.DataFrame([ev])
        provenance={'v1_events_sha':'synthetic_verified','v1_zones_sha':'synthetic_verified','cohort_events':6423}
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);database=root/'ctx.sqlite3'
            with sqlite3.connect(database) as con:
                con.execute('CREATE TABLE events (event_key TEXT, symbol TEXT, causal_json TEXT)')
                con.execute('INSERT INTO events VALUES (?,?,?)',(ev['event_key'],'XYZ',
                    json.dumps({'pivot_timestamp':190*m.STEP,'swing_price':103})))
            fake_v4=types.SimpleNamespace(_merge_tf=lambda ctx,hist,sym:
                                        (frame().set_index('timestamp'),pd.DataFrame(),'MOCK'))
            fake_source=lambda rt:(df,database,root/'missing.sqlite3',provenance)
            with patch.object(m,'source',side_effect=fake_source), patch.dict(sys.modules,{
                 'reaction_compression_breakout_retest_v4':fake_v4}):
                result=m.run(root,min_free_gb=0,max_events=0)
                self.assertTrue(result['success'])
                self.assertEqual(result['groups']['PRIOR_RESISTANCE'],1)
                result2=m.run(root,min_free_gb=0,max_events=0)
                self.assertEqual(result2['labels_sha'],result['labels_sha'])
                path=root/m.OUTPUT_ROOT/'all_live'/'pivot_memory_labels_pre.csv'
                labels=pd.read_csv(path)
                self.assertEqual(len(labels),1)
                self.assertNotIn('result_180m',labels.columns)
                self.assertEqual(m.audit(root)['groups']['PRIOR_RESISTANCE'],1)

    def test_end_to_end_no_outcome_columns(self):
        for name in m.COLS:
            self.assertNotIn('outcome',name)
            self.assertNotIn('tp_',name)
            self.assertNotIn('sl_',name)


if __name__=='__main__':unittest.main()
