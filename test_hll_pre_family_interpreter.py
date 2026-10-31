import csv
import json
import math
import sys
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0,str(Path(__file__).resolve().parent))
import hll_pre_family_interpreter as m

PRE=1787587200000
AID='test_1'

def fake_rows():
    summary=[];pivots=[];legs=[];recovery=[];resistance=[];ohlc=[]
    for scale in m.SCALES:
        src={'attempt_id':AID,'symbol':'TESTUSDT','level_id':'lev1','pre_closed_ts':str(PRE),'scale_atr':str(scale)}
        summary.append({**src,'status':'COMPLETE_PRE','closed_bars':'96',
            'terminal_leg_direction':'UP','terminal_leg_provisional':'1','level_group_attempts':'1',
            'last_confirmed_low_age_bars':'4'})
        for i, (kind,price) in enumerate([('LOW',1.0),('HIGH',1.2),('LOW',1.1),('HIGH',1.3),('LOW',1.15),('HIGH',1.4)]):
            pivots.append({**src,'pivot_index':str(i+1),'pivot_type':kind,'pivot_price_close':str(price),
                'pivot_open_ts':str(PRE-(25-i*3)*m.BAR),'confirmed_known_closed_ts':str(PRE-(23-i*3)*m.BAR),
                'status':'CONFIRMED_PRE'})
        legs.append({**src,'leg_index':'1','leg_direction':'DOWN','status':'CONFIRMED_PRE',
            'start_pivot_known_closed_ts':str(PRE-10*m.BAR),'end_extreme_open_ts':str(PRE-9*m.BAR),
            'end_extreme_confirmed_known_closed_ts':str(PRE-8*m.BAR)})
        recovery.append({**src,'candidate_index':'1','is_latest_confirmed_low':'1','net_change_from_low_atr':'2.0',
            'gap_reduction_atr':'1.3','later_lowest_close_undercut':'0',
            'anchor_low_known_closed_ts':str(PRE-11*m.BAR),
            'anchor_low_open_ts':str(PRE-13*m.BAR), 'anchor_is_window_boundary':'0'})
    for window in m.WINDOWS:
        attrs={'attempt_id':AID,'symbol':'TESTUSDT','level_id':'lev1','pre_closed_ts':str(PRE),
               'window_bars':str(window),'status':'COMPLETE_PRE'}
        resistance.append({**attrs,'touch_episodes':'3','intercontact_trough_last_minus_first_atr':'0.8',
          'intercontact_troughs':'3','zone_center_close_crossings':'1','touch_bars_share':'0.3'})
        ohlc.append({**attrs,'net_close_return_pct':'1.1'})
    return {'summary':summary,'pivots':pivots,'legs':legs,'recovery':recovery,
            'resistance':resistance,'ohlc':ohlc}

class FamilyTests(unittest.TestCase):
    def test_three_lows_and_highs(self):
        rows=fake_rows()
        ret,counts=m.build(rows)
        self.assertEqual(len(ret),3)
        self.assertEqual(set(x['family_candidate'] for x in ret),{'ASCENSO_ESCALONADO'})
        self.assertEqual(counts['ASCENSO_ESCALONADO'],3)

    def test_experimental_not_good_bad(self):
        self.assertFalse(any(word in m.FIELDS for word in ('good','bad','profitable','buy','visual_label')))
        self.assertNotIn('BUENA',str(m.FIELDS))

    def test_labels_cannot_change_morphology(self):
        a=fake_rows(); b=deepcopy(a)
        b['summary'][0]['visual_label']='MALA'
        b['summary'][1]['visual_label']='BUENA'
        self.assertEqual(m.build(a)[0],m.build(b)[0])

    def test_order_invariance(self):
        a=fake_rows();b=deepcopy(a)
        for v in b.values(): v.reverse()
        self.assertEqual(m.build(a)[0],m.build(b)[0])

    def test_pivot_future_rejected(self):
        r=fake_rows();r['pivots'][0]['confirmed_known_closed_ts']=str(PRE+m.BAR)
        with self.assertRaisesRegex(ValueError,'Future pivot'):m.build(r)

    def test_leg_future_rejected(self):
        r=fake_rows();r['legs'][0]['end_extreme_confirmed_known_closed_ts']=str(PRE+m.BAR)
        with self.assertRaisesRegex(ValueError,'Future confirmed leg'):m.build(r)

    def test_recovery_future_rejected(self):
        r=fake_rows();r['recovery'][0]['anchor_low_known_closed_ts']=str(PRE+m.BAR)
        with self.assertRaisesRegex(ValueError,'Future recovery'):m.build(r)

    def test_missing_v3_window_rejected(self):
        r=fake_rows();r['resistance'].pop()
        with self.assertRaisesRegex(ValueError,'Expected all V2/V3'):m.build(r)

    def test_duplicate_v3_rejected(self):
        r=fake_rows();r['resistance'].append(r['resistance'][-1].copy())
        with self.assertRaisesRegex(ValueError,'Duplicado'):m.build(r)

    def test_wrong_pre_rejected(self):
        r=fake_rows();r['ohlc'][0]['pre_closed_ts']=str(PRE+m.BAR)
        with self.assertRaisesRegex(ValueError,'Mismatched'):m.build(r)

    def test_mixed_pivots_return_unknown(self):
        vals=[{'pivot_price_close':'1'},{'pivot_price_close':'2'},{'pivot_price_close':'1.5'}]
        self.assertEqual(m.trend(vals)[0],'MIXTA')

    def test_no_intercontact_is_missing_not_down(self):
        r=fake_rows();
        for v in r['resistance']:v['intercontact_trough_last_minus_first_atr']=''
        result,_=m.build(r)
        self.assertEqual(result[0]['intercontact_trough_direction_48'],'SIN_DATOS')
        self.assertNotIn('inferior',result[0]['contradicting_observations'])

    def test_repeated_range_does_not_mean_bad_trade(self):
        x=m.morph_family(low_trend='MIXTA', high_trend='MIXTA',recent_recovery='SUBE',
                         intercontact='BAJA',contact_count=3,has_confirmed_down=True)
        self.assertEqual(x[0],'OSCILACION_EN_RESISTENCIA')

    def test_positive_intercontact_recovery(self):
        x=m.morph_family(low_trend='MIXTA',high_trend='MIXTA',recent_recovery='SUBE',
                         intercontact='SUBE',contact_count=3,has_confirmed_down=True)
        self.assertEqual(x[0],'RECONSTRUCCION_ENTRE_CONTACTOS')

    def test_no_fit_constants(self):
        # Finite categorical rules, no optimization or label-specific attempt IDs.
        import inspect
        src=inspect.getsource(m.morph_family)
        self.assertNotIn('attempt_id',src)
        self.assertNotIn('visual_label',src)
        self.assertNotIn('tp_',src)

    def test_output_no_duplicate_scales(self):
        result,_=m.build(fake_rows())
        self.assertEqual(len({(r['attempt_id'],r['scale_atr']) for r in result}),len(result))

    def test_real_core15_if_present(self):
        b=Path('reports/horizontal_level_second_ascent_pre/core15_phase_reconstruction_pre_v1/phase_summary_pre.csv')
        if not b.is_file():self.skipTest('No están montados los reportes del VPS')
        rows={k:m.read(p) for k,p in m.SOURCES.items()}
        result,_=m.build(rows)
        self.assertEqual(len(result),150)
        self.assertEqual(len({r['attempt_id'] for r in result}),50)
        self.assertTrue(all(r['status']=='DESCRIPTIVE_PRE_ONLY' for r in result))

if __name__=='__main__':unittest.main()
