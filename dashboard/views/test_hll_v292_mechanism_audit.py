"""Causal synthetic regression V2.9.2, independent of real Binance archive."""
import json
import tempfile
import unittest
from pathlib import Path
import numpy as np
import pandas as pd

from dashboard.views.hll_v292_mechanism_audit import (
 TF,MIN,pre_touch_audit,subtype,pre_features,survival,run_audit,
 summarize,blind_sample,matched_controls,main,digest,
)
from dashboard.views.hll_v292_inspector_overlay import overlay_pre_contacts,read_v292


def bars(count=200):
    i=np.arange(count)
    closes=10+i*.01
    return pd.DataFrame(dict(timestamp=i*TF,open=closes,close=closes,
                              high=closes+.3,low=closes-.3))


def episode():
    return dict(attempt_id='original_1',symbol='TESTUSDT',level_id='LEVEL_X',
     pre_closed_ts=100*TF,breakout_closed_ts=101*TF,zone_low=10.65,zone_high=10.8,
     atr_pre=.5,flip_confirmed_closed_ts=104*TF,post_outcome='FLIP_CONFIRMADO',
     v26_status='FLIP_SUSTAINED_4B',
     **{f'pre_pattern_{w}':'ASCENDING_CLEAN' for w in (24,48,96)},
     **{f'pre_pattern_v291_{w}':'ASCENDING_CLEAN' for w in (24,48,96)},
     **{f'trendline{w}_broken':False for w in (24,48,96)},
     **{f'pre{w}_pivot_lows_json':'[]' for w in (24,48,96)})


def touches():
    return pd.DataFrame([
      dict(symbol='TESTUSDT',level_id='LEVEL_X',side='BELOW',test_id='T1',
           touch_closed_ts=85*TF,depth_atr=.7,rejection_atr=.3,
           resolver_type='DEFENSE_CONFIRMED',resolver_known_ts=105*TF),
      dict(symbol='TESTUSDT',level_id='LEVEL_X',side='BELOW',test_id='T2',
           touch_closed_ts=95*TF,depth_atr=.3,rejection_atr=.2,
           resolver_type='DEFENSE_CONFIRMED',resolver_known_ts=97*TF),
      dict(symbol='TESTUSDT',level_id='LEVEL_X',side='ABOVE',test_id='T3',
           touch_closed_ts=97*TF,depth_atr=.2,rejection_atr=.1,
           resolver_type='DEFENSE_CONFIRMED',resolver_known_ts=98*TF),
      dict(symbol='TESTUSDT',level_id='LEVEL_X',side='BELOW',test_id='T4',
           touch_closed_ts=105*TF,depth_atr=.2,rejection_atr=.1,
           resolver_type='LOSS_CONFIRMED',resolver_known_ts=106*TF),
    ])


def minute_bars(n=120):
    i=np.arange(n)
    t=104*TF+MIN+i*MIN
    close=np.full(n,11.,float)
    close[10:20]=10.2;close[20]=11.
    return pd.DataFrame(dict(timestamp=t,open=np.full(n,11.),close=close,
     high=np.maximum(close,11.)+.4,low=np.minimum(close,11.)-.4))


class MechanismTests(unittest.TestCase):
    def test_pre_canonical_touches_are_not_any_overlap(self):
        row=episode(); events,clusters,state=pre_touch_audit(row,bars(),touches(),48)
        self.assertEqual(state,'COMPLETE_30M_PRE')
        self.assertEqual(len(events),2)
        self.assertEqual([x['contact_num'] for x in events],[1,2])
        self.assertEqual(events[0]['resolver_pre'],'PENDIENTE_AL_PRE')
        self.assertEqual(events[1]['resolver_pre'],'DEFENSE_CONFIRMED')
        self.assertGreaterEqual(clusters,0)
        self.assertTrue(all(x['touch_closed_ts']<=row['pre_closed_ts'] for x in events))

    def test_pre_unchanged_by_post_prices_outcomes(self):
        r=episode(); b=bars();t=touches()
        f,_=pre_features(r,b,[],t,48)
        altered=b.copy()
        altered.loc[100:,'close']=100000.
        alt=r.copy();alt['post_outcome']='SIN_FLIP_EN_VENTANA';alt['flip_confirmed_closed_ts']=np.nan
        other,_=pre_features(alt,altered,[],t,48)
        self.assertEqual(f,other)

    def test_missing_pre_history_is_not_clean(self):
        b=bars().drop(index=70)
        feat,_=pre_features(episode(),b,[],touches(),48)
        self.assertEqual(feat['mechanism'],'SIN_COBERTURA')

    def test_v_recovery_is_separate(self):
        r=episode();f={'coverage':True,'full_zone_crosses':0}
        b=bars(48);b['close']=np.r_[np.linspace(10,8,20),np.linspace(8,13,28)]
        b['low']=b.close-.2;b['high']=b.close+.2
        self.assertEqual(subtype(f,r,48,0,b),'RECUPERACION_V')

    def test_broken_trendline_takes_priority(self):
        r=episode();r['pre_pattern_v291_48']='ASCENDING_TREND_BROKEN'
        self.assertEqual(subtype({'coverage':True,'full_zone_crosses':0},r,48,3,bars(48)),
                         'DIRECTRIZ_ROTA')

    def test_complete_minute_survival_and_reclaim(self):
        r=episode();m=minute_bars()
        result=survival(r,m,120)
        self.assertEqual(result['path_state'],'COMPLETE_1M')
        self.assertFalse(result['survived_without_close_loss'])
        self.assertEqual(result['first_close_loss_ts'],int(m.timestamp.iloc[10])+MIN)
        self.assertEqual(result['first_reclaim_ts'],int(m.timestamp.iloc[20])+MIN)
        self.assertEqual(survival(r,m,720)['path_state'],'CENSORED_1M_TAIL')
        gap=m.drop(index=20).reset_index(drop=True)
        self.assertEqual(survival(r,gap,120)['path_state'],'CENSORED_1M_TAIL')
        # A discontiguous file with sufficiently many rows must be flagged GAP.
        extra=pd.concat([gap,pd.DataFrame([m.iloc[-1].to_dict()|{'timestamp':int(m.timestamp.iloc[-1])+MIN}])],ignore_index=True)
        self.assertEqual(survival(r,extra,120)['path_state'],'GAP_1M')

    def test_censored_survival_never_has_future_mfe(self):
        r=episode();m=minute_bars()[:30]
        result=survival(r,m,120)
        self.assertEqual(result['path_state'],'CENSORED_1M_TAIL')
        self.assertTrue(np.isnan(result['mfe_pct']))
        self.assertTrue(np.isnan(result['survived_without_close_loss']))
        self.assertEqual(survival(dict(r,flip_confirmed_closed_ts=None),m,120)['path_state'],'NO_FLIP')

    def test_blind_review_excludes_post_outcomes(self):
        row=episode();a=pd.DataFrame([dict(row,mechanism_24='ASCENSO_SOSTENIDO',
                                mechanism_96='ASCENSO_SOSTENIDO',mechanism_48='ASCENSO_SOSTENIDO',
                                touch_v26_independent_48=2,confirmed_pivots_48=3,
                                pivot_slope_atr_per_bar_48=.1)])
        sample=blind_sample(a)
        self.assertNotIn('post_outcome',sample)
        self.assertNotIn('flip_confirmed_closed_ts',sample)
        self.assertEqual(sample.iloc[0].revision_manual,'PENDIENTE')
        s=summarize(a)
        self.assertEqual(int(s.loc[(s.window==48)&(s.scope=='ALL'),'flip_confirmed'].iloc[0]),1)

    def test_match_same_class_month_without_censored_controls(self):
        base=episode()
        source=[]
        for label,aid,sym,level,time,outcome in [
           ('a','A','TESTUSDT','X',101*TF,'FLIP_CONFIRMADO'),
           ('b','B','TESTUSDT','Y',103*TF,'SIN_FLIP_EN_VENTANA'),
           ('c','C','OTHER','Z',104*TF,'INDETERMINADO'),
        ]:
            row=dict(base,attempt_id=aid,level_id=level,symbol=sym,
                     breakout_closed_ts=time,post_outcome=outcome,
                     mechanism_48='ASCENSO_SOSTENIDO',
                     touch_v26_independent_48=2,confirmed_pivots_48=3,
                     pivot_slope_atr_per_bar_48=.2,close_efficiency_48=.5,
                     pullback_depth_ratio_48=.8)
            source.append(row)
        pairs,unmatched=matched_controls(pd.DataFrame(source))
        self.assertEqual(len(pairs),1)
        self.assertEqual(pairs.iloc[0].control_id,'B')
        self.assertTrue(unmatched.empty)

    def test_plot_marks_contact_at_pre_only(self):
        import plotly.graph_objects as go
        touch,_,_=pre_touch_audit(episode(),bars(),touches(),48)
        fig=go.Figure()
        fig=overlay_pre_contacts(fig,pd.DataFrame(touch),100*TF)
        self.assertEqual(len(fig.data),1)
        self.assertEqual(list(fig.data[0].text),['1','2'])
        bad=pd.DataFrame(touch).copy();bad.loc[0,'touch_closed_ts']=101*TF
        with self.assertRaises(ValueError):overlay_pre_contacts(go.Figure(),bad,100*TF)

    def test_end_to_end_cli_preserves_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);cand=root/'candles';cand.mkdir()
            row=episode();cc=bars()
            cc.to_csv(cand/'TESTUSDT_30m.csv',index=False)
            mins=root/'minutes';mins.mkdir();minute_bars().to_csv(mins/'TESTUSDT_1m.csv',index=False)
            original={k:v for k,v in row.items() if not k.startswith('pre_pattern_v291_') and not k.startswith('trendline')}
            pre=root/'pre_cohort.csv';pd.DataFrame([original]).to_csv(pre,index=False)
            v291=root/'integrity';v291.mkdir()
            audit={'attempt_id':'original_1',**{f'pre_pattern_v291_{w}':'ASCENDING_CLEAN' for w in (24,48,96)},
                   **{f'trendline{w}_broken':False for w in (24,48,96)}}
            pd.DataFrame([audit]).to_csv(v291/'integrity_audit.csv',index=False)
            (v291/'manifest.json').write_text(json.dumps({'original_sha256':digest(pre)}))
            t=root/'touches.csv';touches().to_csv(t,index=False)
            out=root/'out'
            args=['--cohort',str(pre),'--integrity-dir',str(v291),
                  '--touches',str(t),'--candles-dir',str(cand),'--minute-dir',str(mins),
                  '--output-dir',str(out)]
            self.assertEqual(main(['--plan']+args),0)
            self.assertEqual(main(['--run']+args),0)
            self.assertEqual(len(pd.read_csv(out/'mechanism_attempts.csv')),1)
            self.assertEqual(len(pd.read_csv(out/'pre_contacts.csv')),6) # 2 contacts each 24/48/96
            self.assertEqual(len(pd.read_csv(out/'flip_survival_1m.csv')),3)
            self.assertTrue((out/'blind_review_sample.csv').is_file())
            self.assertTrue((out/'matched_controls_post.csv').is_file())
            self.assertEqual(pd.read_csv(pre).iloc[0].attempt_id,'original_1')
            overlay,touch,surv=read_v292(pre,out,pd.read_csv(pre))
            self.assertEqual(len(overlay),1)
            self.assertEqual(len(touch),6)
            self.assertEqual(len(surv),3)
            with self.assertRaises(SystemExit):main(['--run']+args)

if __name__=='__main__':unittest.main()
