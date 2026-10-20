"""Run with: python3 -m unittest -v test_runner_discovery_dna"""
import unittest

import pandas as pd
from dashboard.services.runner_discovery_dna import (
    runner_labels, assign_families, family_report, compare_traits, trait_flag,
)
from reaction_lab_runner_discovery_v2 import first_favorable_hits, first_touch


class RunnerDnaTests(unittest.TestCase):
    def test_runner_is_not_tp_first(self):
        bars = [(i * 60000, 100, 100.1, 99.8, 100) for i in range(20)]
        bars[1] = (60000, 100, 100.1, 98.7, 100)  # SL before favor run
        bars[4] = (240000, 100, 103.1, 99.8, 100)
        self.assertEqual(first_touch(bars,side="LONG",price=100,target_pct=3)[0],"SL_FIRST")
        self.assertEqual(first_favorable_hits(bars,side="LONG",price=100)[3],5)

    def test_short_and_time_window(self):
        bars=[(i*60000, 100, 100.2, 99.8, 100) for i in range(15)]
        bars[8]=(8*60000,100,100.2,96.8,100)
        result=first_favorable_hits(bars,side="SHORT",price=100)
        self.assertEqual(result[3],9)
        self.assertIsNone(result[5])
        raw=pd.DataFrame({"quality":["COMPLETE_VALID"]*3,"observed_quality":["OBSERVED_PROXY"]*3,
           "observed_bars":[180,180,100], "obs_time_to_run_3pct":[9,None,20],
           "time_to_run_3pct":[6,5,4],"event_key":["a","b","c"]})
        a=runner_labels(raw,mode="observed",target=3,horizon=180)
        self.assertEqual(a["event_key"].tolist(),["a","b"])
        self.assertEqual(a["es_runner"].tolist(),[True,False])
        b=runner_labels(raw,mode="structural",target=3,horizon=180)
        self.assertEqual(b["es_runner"].tolist(),[True,True,True])
        with self.assertRaises(KeyError):
            runner_labels(raw.drop(columns="obs_time_to_run_3pct"),mode="observed",target=3,horizon=180)

    def test_families_are_exclusive_and_missing_not_negative(self):
        test=pd.DataFrame({
            "max_departure_pct":[3,3,0.5,0.5,0.5,0.5,None],
            "opposing_room_1h_pct":[3,0.5,3,0.5,0.5,0.5,3],
            "side_adjusted_strength_vs_btc_4h":[3,0.5,0.5,-3,0,3,None],
            "es_runner":[True,True,True,True,False,False,True],
        })
        labeled=assign_families(test)
        self.assertEqual(labeled["familia_adn"].tolist(),[
            "Impulso + Room + BTC","Solo impulso","Solo Room","Solo BTC",
            "Ninguno de los tres","Solo BTC","Contexto incompleto"])
        report=family_report(labeled)
        self.assertEqual(report["senales"].sum(),len(test))
        self.assertEqual(report["runners"].sum(),5)

    def test_prevalence_uses_factor_specific_denominators(self):
        frame=pd.DataFrame({
            "es_runner":[True,True,True,False,False,False],
            "max_departure_pct":[3,3,None,0.4,3,None],
            "opposing_room_1h_pct":[3,None,1,0.4,1,None],
        })
        report=compare_traits(frame,min_runners=1,min_controls=1)
        r=report.loc[report["Rasgo"].eq("Departure ≥2%")].iloc[0]
        self.assertEqual(r["runner_con_dato"],2)
        self.assertEqual(r["resto_con_dato"],2)
        self.assertEqual(r["runner_pct"],100)
        self.assertEqual(r["resto_pct"],50)

    def test_zero_and_missing(self):
        data=pd.Series([0,None,-2,2,2.2],dtype="Float64")
        known, yes=trait_flag(data,"abs",2)
        self.assertEqual(known.tolist(),[True,False,True,True,True])
        self.assertEqual(yes.tolist(),[False,False,True,True,True])

if __name__ == "__main__": unittest.main()
