import json, hashlib, tempfile, unittest
from pathlib import Path
from contextlib import redirect_stdout
import io
import pandas as pd
from audit_persistent_first_touch import analyze, NEAR,FAR,UNKNOWN
class OutcomesJoinTest(unittest.TestCase):
    def test_join_on_event_key_and_separate_missing(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d); p.mkdir(exist_ok=True)
            e=pd.DataFrame([
                {'event_key':'A|LONG|1','symbol':'A','side':'LONG','observation_type':'LIVE_OBSERVED','group_pre':NEAR,'date_utc':'2026-10-12'},
                {'event_key':'B|SHORT|2','symbol':'B','side':'SHORT','observation_type':'LIVE_OBSERVED','group_pre':FAR,'date_utc':'2026-10-13'},
                {'event_key':'C|LONG|3','symbol':'C','side':'LONG','observation_type':'LIVE_OBSERVED','group_pre':UNKNOWN,'date_utc':'2026-10-14'},
            ])
            ev=p/'event_labels.csv';e.to_csv(ev,index=False)
            h=hashlib.sha256(ev.read_bytes()).hexdigest()
            (p/'audit.json').write_text(json.dumps({'success':True,'events':3,'version':'test','audit_rule':'strict_response_close_before_retest_v1.1','outputs_sha256':{'event_labels.csv':h}}))
            ft=pd.DataFrame([
              {'event_key':'A|LONG|1','symbol':'A','side':'LONG','observation_type':'LIVE_OBSERVED','entry_mode':'OBSERVED_OPEN','tp_pct':1.0,'sl_pct':1.0,'result_180m':'TP_FIRST','result_360m':'TP_FIRST'},
              {'event_key':'C|LONG|3','symbol':'C','side':'LONG','observation_type':'LIVE_OBSERVED','entry_mode':'OBSERVED_OPEN','tp_pct':1.0,'sl_pct':1.0,'result_180m':'SL_FIRST','result_360m':'SL_FIRST'},
              {'event_key':'B|SHORT|2','symbol':'B','side':'SHORT','observation_type':'LIVE_OBSERVED','entry_mode':'STRUCTURAL_OPEN','tp_pct':1.0,'sl_pct':1.0,'result_180m':'SL_FIRST','result_360m':'SL_FIRST'}])
            fp=p/'first_touch_scenarios.csv';ft.to_csv(fp,index=False)
            r=analyze(p,fp)
            self.assertEqual(r['join']['eligible_without_scenario'],1)
            self.assertEqual(r['comparison']['360'][0]['TP'],1)
            self.assertEqual(r['comparison']['360'][1]['events'],0)
            self.assertEqual(r['join']['unknown_history_events'],1)
    def test_missing_360_fails_fast(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);pd.DataFrame([{'event_key':'A','symbol':'A','side':'LONG','observation_type':'LIVE_OBSERVED','group_pre':NEAR,'date_utc':'2026-10-12'}]).to_csv(p/'event_labels.csv',index=False)
            h=hashlib.sha256((p/'event_labels.csv').read_bytes()).hexdigest()
            (p/'audit.json').write_text(json.dumps({'success':True,'events':1,'audit_rule':'strict_response_close_before_retest_v1.1','outputs_sha256':{'event_labels.csv':h}}))
            pd.DataFrame([{'event_key':'A','symbol':'A','side':'LONG','observation_type':'LIVE_OBSERVED','entry_mode':'OBSERVED_OPEN','tp_pct':1,'sl_pct':1,'result_180m':'TP_FIRST'}]).to_csv(p/'first_touch_scenarios.csv',index=False)
            with self.assertRaisesRegex(ValueError,'result_360m'):
                analyze(p,p/'first_touch_scenarios.csv')
if __name__=='__main__':unittest.main()
