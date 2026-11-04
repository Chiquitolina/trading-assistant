#!/usr/bin/env python3
"""Causal regression tests for HLL Force Breakout Discovery V1."""
import csv
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime, timezone

import hll_force_breakout_discovery_v1 as m

T0=int(datetime(2026,9,7,tzinfo=timezone.utc).timestamp()*1000)


def write_csv(path,records):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('w',newline='',encoding='utf8') as f:
        w=csv.DictWriter(f,fieldnames=list(records[0]));w.writeheader();w.writerows(records)


def write_json(path,obj):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(obj),encoding='utf8')


def tape(n=650,*, start=T0,boom=270):
    rec=[]
    close=100.0
    for j in range(n):
        op=close
        if j==boom:
            cl=103.5;hi=104.0;lo=op-0.02
        elif j==boom+1:
            cl=103.3;hi=104.1;lo=102.6
        else:
            cl=close;hi=max(op,cl)+0.02;lo=min(op,cl)-0.02
        rec.append(dict(timestamp=str(start+j*m.TF1),open=str(op),high=str(hi),
                        low=str(lo),close=str(cl)))
        close=cl
    return rec


def minute_arrays(bars):
    t=[int(x['timestamp']) for x in bars]
    vals=tuple([float(x[k]) for x in bars] for k in ('open','high','low','close'))
    return t,vals


class FeatureTests(unittest.TestCase):
    def setUp(self):
        self.t,self.a=minute_arrays(tape())
        self.ext=m.Extremes(self.a[1],self.a[2])

    def test_lookback_no_future_leak(self):
        ix=255
        before=m.feature_at(ix,self.t,self.a,self.ext,15)
        amended=[x.copy() for x in self.a]
        for j in range(ix,len(self.t)):
            amended[0][j]=1000.;amended[1][j]=1500.;amended[2][j]=0.5;amended[3][j]=1300.
        after=m.feature_at(ix,self.t,tuple(amended),m.Extremes(amended[1],amended[2]),15)
        self.assertEqual(before,after)

    def test_base_excludes_recent_30(self):
        arr=[list(x) for x in self.a]
        arr[1][245]=120.
        f=m.feature_at(255,self.t,tuple(arr),m.Extremes(arr[1],arr[2]),15)
        self.assertAlmostEqual(f['base_high_prior_4h_ex_last30'],100.02)

    def test_feature_at_60m_lookback_and_monotone(self):
        feat=m.feature_at(285,self.t,self.a,self.ext,15)
        self.assertEqual(feat['last15_direction'],'UP')
        self.assertEqual(feat['base_position'],'ESCAPED_BASE')
        self.assertGreater(feat['w60_net_pct'],0)
        self.assertEqual(set(m.SFX),set(feat))

    def test_zero_path_is_not_false_strength(self):
        self.assertEqual(m.rolling_force([100]*16),(0.,0.,0.))

    def test_negative_efficiency_has_negative_sign(self):
        self.assertEqual(m.rolling_force([104,103,102,101])[1],-1.)

    def test_last_window_direction_is_causal(self):
        before=m.feature_at(255,self.t,self.a,self.ext,15)
        after=m.feature_at(285,self.t,self.a,self.ext,15)
        self.assertEqual(before['last15_direction'],'FLAT')
        self.assertEqual(after['last15_direction'],'UP')

    def test_flat_base_is_valid_and_ratio_is_missing(self):
        f=m.feature_at(240,self.t,self.a,m.Extremes([100.]*len(self.t),[100.]*len(self.t)),15)
        self.assertEqual(f['base_range_pct'],0)
        self.assertEqual(f['recent60_range_vs_base'],'')

    def test_inverted_base_is_rejected(self):
        with self.assertRaises(ValueError):
            m.feature_at(240,self.t,self.a,m.Extremes([99.]*len(self.t),[101.]*len(self.t)),15)

    def test_non_cadence_rejected(self):
        with self.assertRaises(ValueError):m.feature_at(256,self.t,self.a,self.ext,15)

    def test_first_hit_tree_and_range(self):
        self.assertAlmostEqual(self.ext.range(0,200)[0],100.02)
        self.assertEqual(self.ext.first_barrier(260,300,103,98),270)
        self.assertIsNone(self.ext.first_barrier(0,260,103,98))


class OutcomeTests(unittest.TestCase):
    def setUp(self):
        self.t,self.a=minute_arrays(tape())
        self.ends=m.continuous_runs(self.t)[1]
        self.ext=m.Extremes(self.a[1],self.a[2])

    def test_tp_first_for_eligible_snapshot(self):
        val=m.label_at(255,self.t,self.a,self.ends,self.ext,m.SCENARIOS[0])
        self.assertEqual(val[0],'TP_FIRST')
        self.assertEqual(val[1],self.t[270])
        self.assertEqual(val[2],16)

    def test_no_lookahead_in_early_closed_prefix(self):
        idx=255
        a=m.feature_at(idx,self.t,self.a,self.ext,15)
        later=m.label_at(idx,self.t,self.a,self.ends,self.ext,m.SCENARIOS[0])
        self.assertEqual(a['w15_net_pct'],0.)
        self.assertEqual(later[0],'TP_FIRST')

    def test_ambiguous_1m_not_assumed_tp(self):
        arr=[list(x) for x in self.a]
        arr[1][270]=105;arr[2][270]=97
        state=m.label_at(255,self.t,tuple(arr),self.ends,m.Extremes(arr[1],arr[2]),m.SCENARIOS[1])
        self.assertEqual(state[0],'AMBIGUOUS_1M')
        self.assertEqual(state[3],'')

    def test_gap_open_precedes_both_wicks(self):
        arr=[list(x) for x in self.a]
        for k,v in [(0,105),(1,106),(2,97),(3,104)]:arr[k][270]=v
        state=m.label_at(255,self.t,tuple(arr),self.ends,m.Extremes(arr[1],arr[2]),m.SCENARIOS[1])
        self.assertEqual(state[0],'TP_GAP_OPEN')
        self.assertAlmostEqual(state[3],4.9)

    def test_sl_first(self):
        arr=[list(x) for x in self.a]
        arr[2][270]=97
        arr[1][270]=101
        state=m.label_at(255,self.t,tuple(arr),self.ends,m.Extremes(arr[1],arr[2]),m.SCENARIOS[1])
        self.assertEqual(state[0],'SL_FIRST')

    def test_tail_censor_not_failure(self):
        state=m.label_at(600,self.t,self.a,self.ends,self.ext,m.SCENARIOS[1])
        self.assertEqual(state[0],'CENSORED_TAIL')

    def test_missing_future_minute_is_censored(self):
        t,self_a=self.t[:270]+self.t[271:],tuple(x[:270]+x[271:] for x in self.a)
        ends=m.continuous_runs(t)[1]
        state=m.label_at(255,t,self_a,ends,m.Extremes(self_a[1],self_a[2]),m.SCENARIOS[1])
        self.assertEqual(state[0],'CENSORED_GAP')

    def test_prefix_missing_excludes_snapshot_without_fill(self):
        t=self.t[:250]+self.t[251:]
        ix=t.index(T0+255*m.TF1)
        begins,_=m.continuous_runs(t)
        self.assertGreater(begins[ix-1],ix-m.BASE_BARS)

    def test_split_is_symbol_stable(self):
        self.assertEqual(m.split_for_symbol('AAVEUSDT'),'LEGACY_RESERVED')
        self.assertEqual(m.split_for_symbol('FIGHTUSDT'),'LEGACY_RESERVED')
        self.assertEqual(m.split_for_symbol('DYDXUSDT'),'LEGACY_EXAMINED_DISCOVERY')
        self.assertEqual(m.split_for_symbol('NEWCOINUSDT'),m.split_for_symbol('NEWCOINUSDT'))


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.args=SimpleNamespace(output=None,scope='pilot15',cadence_min=15,
            cohort=m.COHORT,map=m.MAP,minute=m.MINUTE,minute_manifest=m.MINUTE_MANIFEST)
        self.p=m.paths(self.root,self.args)

    def fixture(self):
        a=[];b=[]
        for symbol in ('AAVEUSDT','DYDXUSDT'):
            for ix in (0,1):
                p=T0+(240+ix*30)*m.TF1
                r=dict(attempt_id=symbol+'_'+str(ix),symbol=symbol,level_id='level_'+symbol,
                     pre_closed_ts=str(p),breakout_closed_ts=str(p+m.TF30),
                     pre_pattern_24='OTHER',pre_pattern_48='OTHER',
                     pre_pattern_96='ASCENDING_CLEAN',zone_low='99',zone_high='100',atr_pre='1')
                a.append(r)
                b.append({k:r[k] for k in ('attempt_id','symbol','level_id',
                                         'pre_closed_ts','breakout_closed_ts','zone_low','zone_high','atr_pre')}|
                         dict(episode_id='episode_'+symbol+'_'+str(ix),episode_ordinal='1',
                              is_first_in_episode='1',family_1p0='ASCENSO_ESCALONADO'))
        write_csv(self.p['cohort'],a)
        write_json(self.p['cohort'].with_name('manifest.json'),
                   {'version':'HLL_V282_PRE_COHORT','mode':'OFFLINE_READ_ONLY',
                    'original_attempts':4,'symbols':2})
        write_csv(self.p['map'],b)
        write_json(self.p['map'].with_name('manifest.json'),
                   {'version':'HLL_OVERLAP_AUDIT_V1',
                    'source_sha256':{'cohort':m.sha(self.p['cohort'])},
                    'outputs_sha256':{'attempt_episode_map.csv':m.sha(self.p['map'])}})
        for sym in ('AAVEUSDT','DYDXUSDT'):
            fp=self.p['minute']/(sym+'_1m.csv')
            write_csv(fp,tape())
            write_json(self.p['minute_manifest']/(sym+'.json'),
                       {'sha256':m.sha(fp),'source_sha256':m.sha(self.p['cohort']),'bars':650})
        return m.source_check(self.p,('AAVEUSDT','DYDXUSDT'),require_all=False)

    def test_end_to_end_preserves_holdout_sealing_and_links(self):
        symbols,links,csha,msha=self.fixture()
        m.run(self.p,symbols,links,csha,msha,15)
        snap=list(m.csv_rows(self.p['output']/'force_snapshots_CAUSAL.csv'))
        outcomes=list(m.csv_rows(self.p['output']/'force_outcomes_DISCOVERY_ONLY.csv'))
        context=list(m.csv_rows(self.p['output']/'hll_crossup_links_CONTEXT_ONLY.csv'))
        hll_cases=list(m.csv_rows(self.p['output']/'hll_crossup_force_cases_DISCOVERY_ONLY.csv'))
        hll_groups=list(m.csv_rows(self.p['output']/'hll_structure_force_groups_DISCOVERY_ONLY.csv'))
        self.assertEqual(len(hll_cases),6) # 2 DYDX first-episode attempts * 3 scenarios
        self.assertTrue(hll_groups)
        self.assertTrue(all(r['symbol']=='DYDXUSDT' for r in hll_cases))
        self.assertTrue(snap and outcomes and context)
        self.assertEqual({r['symbol'] for r in snap},{'DYDXUSDT','AAVEUSDT'})
        self.assertEqual({r['symbol'] for r in outcomes},{'DYDXUSDT'})
        self.assertEqual(len(outcomes),3*len([r for r in snap if r['symbol']=='DYDXUSDT']))
        self.assertTrue(all(r['no_hll_conditioning']=='YES' for r in snap))
        self.assertTrue(all(r['link_use'].startswith('RETROSPECTIVE_') for r in context))
        self.assertTrue(any(x['snapshot_link_status']=='MATCHED_CLOCK_GRID' for x in context))
        man=m.json_read(self.p['output']/'manifest.json')
        self.assertTrue(man['holdout_sealed'])
        for name,h in man['output_sha256'].items():
            self.assertEqual(m.sha(self.p['output']/name),h)

    def test_no_overwrite(self):
        symbols,links,csha,msha=self.fixture()
        m.run(self.p,symbols,links,csha,msha,15)
        with self.assertRaises(FileExistsError):m.run(self.p,symbols,links,csha,msha,15)

    def test_cohort_tamper_rejected(self):
        self.fixture()
        with self.p['cohort'].open('a',encoding='utf8') as f:f.write('\n')
        with self.assertRaises(ValueError):
            m.source_check(self.p,('AAVEUSDT','DYDXUSDT'),require_all=False)

    def test_minute_tamper_rejected(self):
        symbols,links,csha,msha=self.fixture()
        with (self.p['minute']/'DYDXUSDT_1m.csv').open('a',encoding='utf8') as f:f.write('\n')
        with self.assertRaises(ValueError):m.run(self.p,symbols,links,csha,msha,15)
        self.assertFalse(self.p['output'].exists())

    def test_candidate_universe_independent_of_hll_crossup(self):
        symbols,links,csha,msha=self.fixture()
        m.run(self.p,symbols,links,csha,msha,15)
        a=list(m.csv_rows(self.p['output']/'force_snapshots_CAUSAL.csv'))
        ids={(x['symbol'],x['snapshot_ts']) for x in a}
        # Number of candidates far exceeds the four historical CROSS_UP attempts.
        self.assertGreater(len(ids),20)
        self.assertEqual(len(ids),len(a))
        self.assertNotIn('attempt_id',a[0])

    def test_detect_bad_crossup_clock(self):
        self.fixture()
        self.p['map'].unlink()
        f=[dict(x) for x in m.csv_rows(self.p['cohort'])]
        b=[dict(attempt_id=x['attempt_id'],symbol=x['symbol'],level_id=x['level_id'],
                episode_id='ep'+x['attempt_id'],pre_closed_ts=x['pre_closed_ts'],
                breakout_closed_ts=x['pre_closed_ts'],is_first_in_episode='1',family_1p0='OTHER') for x in f]
        write_csv(self.p['map'],b)
        manifest=self.p['map'].with_name('manifest.json')
        meta=m.json_read(manifest);meta['outputs_sha256']['attempt_episode_map.csv']=m.sha(self.p['map']);write_json(manifest,meta)
        with self.assertRaises(ValueError):m.source_check(self.p,('AAVEUSDT','DYDXUSDT'),require_all=False)


if __name__=='__main__':unittest.main()
