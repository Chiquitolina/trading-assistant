import json
import tempfile
import unittest
from pathlib import Path
import pandas as pd
from hll_v26_cross_preflight import audit_symbol, main
from hll_v26_structure_lab import TF

class V26CrossPreflight(unittest.TestCase):
    def test_valid_and_invalid_sources(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);cand=root/'candles';atlas=root/'atlas';sh=atlas/'shards'
            cand.mkdir();sh.mkdir(parents=True)
            # A strict close above an immutable resistance zone.
            ts=1787184000000
            pd.DataFrame(dict(timestamp=[ts,ts+TF],close=[100.5,102.])).to_csv(cand/'XUSDT_30m.csv',index=False)
            pd.DataFrame([dict(level_id='L1',origin='HIGH',zone_low=99.,zone_high=101.)]).to_csv(sh/'XUSDT.formation.csv',index=False)
            ev=dict(level_id='L1',event_type='CROSS_UP',observed_closed_ts=ts+2*TF,
                    previous_relation='INSIDE',close_relation='ABOVE',close=102.,
                    zone_low=99.,zone_high=101.)
            pd.DataFrame([ev]).to_csv(sh/'XUSDT.events.csv',index=False)
            n,ok,errors=audit_symbol('XUSDT',atlas,cand)
            self.assertEqual((n,ok,len(errors)),(1,1,0))
            ev['close_relation']='INSIDE'
            pd.DataFrame([ev]).to_csv(sh/'XUSDT.events.csv',index=False)
            n,ok,errors=audit_symbol('XUSDT',atlas,cand)
            self.assertEqual((n,ok,len(errors)),(1,0,1))

if __name__=='__main__':unittest.main()
