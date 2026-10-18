"""Phase 4.2 strict regressions: one bounded window; observed lifecycle only."""
import csv
import tempfile
import unittest
from pathlib import Path

import pandas as pd
from dashboard.services import reaction_lab_policy_dashboard as p

S = "Runner Challenger 220"
B = p.POLICY_BASE
A = "DIRECTIONAL_STRENGTH_VS_BTC_4H_PROXY"
T = 1789938240001  # 20 September, 18:04 Argentina
NEXT = 1790283780001  # 24 September, 18:03 Argentina
XPLUS = "XPLUSDT|LONG|1789938180000"
TURBO = "TURBOUSDT|LONG|1789938180000"
VELVET = "VELVETUSDT|SHORT|1789945740000"


def competitions(next_time=NEXT):
    rows = []
    for tm, picks, free in (
        (T, (XPLUS, TURBO), (True, True)),
        (T+64*60000, ("", ""), (False, False)),
        (next_time, ("XPLUSDT|SHORT|1790283720000", "1000BONKUSDT|SHORT|1790283720000"), (True,True)),
    ):
        for policy, key, available in ((B, picks[0],free[0]), (A, picks[1],free[1])):
            rows.append({"strategy":S,"policy":policy,"ready_ms":tm,
                         "eligible_event_keys":XPLUS+";"+TURBO,
                         "order_submitted_event_key":key,
                         "slot_was_available_under_policy":available})
    return pd.DataFrame(rows)


def divergences():
    rows = []
    for key, offset, left, right, why_l, why_r in (
        (XPLUS,0,"ORDER_SUBMITTED","SKIPPED_SLOT_BUSY","TP","slot_busy_through_1789949400000"),
        (TURBO,0,"SKIPPED_SLOT_BUSY","ORDER_SUBMITTED","slot_busy_through_1789942380000","TIMEOUT"),
        ("DYDXUSDT|SHORT|1789939440000",21*60000,"SKIPPED_SLOT_BUSY","SKIPPED_SLOT_BUSY","slot_busy_through_1789942380000","slot_busy_through_1789949400000"),
        ("BONKUSDT|SHORT|1789942020000",64*60000,"SKIPPED_SLOT_BUSY","SKIPPED_SLOT_BUSY","slot_busy_through_1789942380000","slot_busy_through_1789949400000"),
        ("XPLUSDT|SHORT|1789942020000",64*60000,"SKIPPED_SLOT_BUSY","SKIPPED_SLOT_BUSY","slot_busy_through_1789942380000","slot_busy_through_1789949400000"),
        (VELVET,126*60000,"ORDER_SUBMITTED","SKIPPED_SLOT_BUSY","NO_FILL","slot_busy_through_1789949400000"),
        ("XPLUSDT|SHORT|1790283720000",NEXT-T,"ORDER_SUBMITTED","SKIPPED_SLOT_BUSY","SL","slot_busy_through_1790294640000"),
        ("1000BONKUSDT|SHORT|1790283720000",NEXT-T,"SKIPPED_SLOT_BUSY","ORDER_SUBMITTED","slot_busy_through_1790285220000","TIMEOUT"),
    ):
        rows.append({"strategy":S,"policy":A,"ready_ms":T+offset,"event_key":key,
                     "baseline_decision":left,"policy_decision":right,
                     "baseline_reason":why_l,"policy_reason":why_r})
    return pd.DataFrame(rows)


def decisions():
    result = []
    for r in divergences().to_dict("records"):
        for policy, side in ((B,"baseline"),(A,"policy")):
            result.append({"strategy":S, "policy":policy,"event_key":r["event_key"],
                           "ready_ms":r["ready_ms"],"decision":r[f"{side}_decision"],
                           "reason":r[f"{side}_reason"]})
    return pd.DataFrame(result)


class LocalConsequencesTests(unittest.TestCase):
    def test_exact_20_sep_six_hours_and_two_on_24_sep(self):
        local, later, prior, end, boundary = p.scoped_consequences(
            divergences(), competitions(),S,A,T,6)
        self.assertEqual(len(local),6)
        self.assertEqual(len(later),2)
        self.assertEqual((local["change_type"]=="Decisión distinta").sum(),3)
        self.assertEqual((local["change_type"]=="Misma decisión / reserva o resultado distinto").sum(),3)
        self.assertFalse(prior)
        self.assertEqual(end,T+6*3600000)
        self.assertEqual(boundary,NEXT)
        self.assertTrue((later["ready_ms"]==NEXT).all())

    def test_fresh_group_both_busy_does_not_cut(self):
        self.assertEqual(p.next_fresh_competition(competitions(),S,A,T),NEXT)

    def test_next_free_choice_cuts_strictly_before_boundary(self):
        earlier=T+3*3600000
        # Move later new selection to +3h, keeping the earlier busy group.
        ds=divergences()
        local, later, _, end, boundary = p.scoped_consequences(
            ds,competitions(earlier),S,A,T,6)
        self.assertEqual(end,earlier)
        self.assertEqual(boundary,earlier)
        self.assertEqual(len(local),6)
        self.assertEqual(len(later),2)

    def test_had_prior_cannot_be_attributed(self):
        local, later, prior, end, boundary = p.scoped_consequences(
            divergences(),competitions(),S,A,NEXT,6)
        self.assertTrue(prior)
        self.assertEqual(len(local),2)
        self.assertEqual(len(later),0)

    def test_pairs_strictly_one_per_event_and_excluded_boundary(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            frame=decisions()
            frame.to_csv(root/"policy_decisions.csv",index=False)
            filtered=p.load_decisions_window(root,S,[B,A],T,T+6*3600000)
            pair=p.paired_window_decisions(filtered, alternative=A)
            self.assertEqual(len(pair),6)
            self.assertEqual(int(pair["differs"].sum()),6)
            self.assertEqual(int(pair["decision_changed"].sum()),3)
            # Next distinct selection at exact window end is excluded.
            pair2=p.load_decisions_window(root,S,[B,A],T,NEXT)
            self.assertEqual(len(pair2),12)
            with self.assertRaisesRegex(ValueError,"duplicad"):
                p.paired_window_decisions(pd.concat([filtered,filtered.iloc[[0]]]),alternative=A)

    def test_observed_occupancy_from_exact_events(self):
        data = []
        for policy, key, end in ((B,XPLUS,1789942380000),(A,TURBO,1789949400000)):
            data.extend([
                {"policy":policy,"event_key":key,"kind":"ORDER_PLACED","timestamp_ms":T},
                {"policy":policy,"event_key":key,"kind":"ORDER_FILLED","timestamp_ms":T+60000},
                {"policy":policy,"event_key":key,"kind":"POSITION_EXIT","timestamp_ms":end},
            ])
        segments,unknown=p.build_observed_slot_segments(pd.DataFrame(data),T,T+6*3600000)
        self.assertEqual(unknown,0)
        self.assertEqual(len(segments),4)
        base_end=segments[segments["policy"]==B]["end_ms"].max()
        alt_end=segments[segments["policy"]==A]["end_ms"].max()
        self.assertEqual((alt_end-base_end)/60000,117.0)
        self.assertEqual(set(segments["stage"]),{"Orden pendiente","Posición abierta"})

    def test_unobservable_slot_not_invented(self):
        segments,n=p.build_observed_slot_segments(pd.DataFrame([
            {"policy":B,"event_key":XPLUS,"kind":"ORDER_PLACED","timestamp_ms":T}
        ]),T,T+6*3600000)
        self.assertTrue(segments.empty)
        self.assertEqual(n,1)

    def test_actual_trades_vs_no_fill_and_blocked(self):
        paired=p.paired_window_decisions(decisions().query("ready_ms < @NEXT"),alternative=A)
        tr=pd.DataFrame([
            {"policy":B,"event_key":XPLUS,"status":"TP","net_pct":1.96},
            {"policy":A,"event_key":TURBO,"status":"TIMEOUT","net_pct":.6002},
        ])
        actual=p.actual_order_consequences(paired,tr,A).set_index("event_key")
        self.assertEqual(len(actual),3)
        self.assertEqual(actual.loc[VELVET,"baseline"],"NO_FILL")
        self.assertEqual(actual.loc[VELVET,"policy"],"No enviada")
        self.assertTrue(pd.isna(actual.loc[VELVET,"baseline_net_pct"]))
        self.assertEqual(actual.loc[XPLUS,"baseline"],"TP")
        self.assertEqual(actual.loc[TURBO,"policy"],"TIMEOUT")

    def test_trade_crosses_right_chart_edge_not_lost(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)
            with (folder/"policy_events.csv").open("w",newline="") as f:
                w=csv.DictWriter(f,fieldnames=["strategy","policy","event_key","kind","timestamp_ms","status"])
                w.writeheader()
                for kind,ts in (("ORDER_PLACED",T), ("ORDER_FILLED",T+60000),
                                ("POSITION_EXIT",T+3*3600000)):
                    w.writerow({"strategy":S,"policy":B,"event_key":XPLUS,
                                "kind":kind,"timestamp_ms":ts,"status":"TP"})
            end=T+3600000
            data=p.load_policy_event_window(folder,S,[B,A],T,end)
            self.assertEqual(len(data),3)
            seg,unknown=p.build_observed_slot_segments(data,T,end)
            self.assertEqual(unknown,0)
            self.assertEqual(seg["end_ms"].max(),end)

    def test_unresolved_position_is_not_fabricated_release(self):
        data=pd.DataFrame([
            {"policy":B,"event_key":XPLUS,"kind":"ORDER_PLACED","timestamp_ms":T},
            {"policy":B,"event_key":XPLUS,"kind":"ORDER_FILLED","timestamp_ms":T+60000},
            {"policy":B,"event_key":XPLUS,"kind":"UNRESOLVED_POSITION","timestamp_ms":T+60000},
        ])
        seg,n=p.build_observed_slot_segments(data,T,T+3600000)
        self.assertTrue(seg.empty)
        self.assertEqual(n,1)

    def test_source_events_stream_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)
            with (folder/"policy_events.csv").open("w",newline="") as f:
                w=csv.DictWriter(f,fieldnames=["strategy","policy","event_key","kind","timestamp_ms","status"])
                w.writeheader()
                for policy,key,kind,ms in ((B,XPLUS,"ORDER_PLACED",T),
                                           (B,XPLUS,"ORDER_EXPIRED",T+60000),
                                           (A,TURBO,"ORDER_PLACED",T),
                                           (A,TURBO,"ORDER_EXPIRED",T+60000),
                                           (B,"OTHER","ORDER_PLACED",T-25*3600000)):
                    w.writerow({"strategy":S,"policy":policy,"event_key":key,"kind":kind,
                                "timestamp_ms":ms,"status":"NO_FILL"})
            ev=p.load_policy_event_window(folder,S,[B,A],T,T+3600000)
            self.assertEqual(len(ev),4)
            segments,n=p.build_observed_slot_segments(ev,T,T+3600000)
            self.assertEqual(n,0)
            self.assertEqual(len(segments),2)
            self.assertTrue((segments["stage"]=="Orden pendiente").all())


if __name__ == "__main__":
    unittest.main()
