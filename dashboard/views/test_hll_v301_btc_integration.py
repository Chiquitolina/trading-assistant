import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

import numpy as np
import pandas as pd

from dashboard.views import hll_v301_btc_strength as m

class EndToEnd(TestCase):
    def test_offline_pipeline_small_archive(self):
        with TemporaryDirectory() as tmp:
            base=Path(tmp)/'reports'/'hll'
            candles=Path(tmp)/'candles'
            candles.mkdir(parents=True)
            entrydir=base/'entries_FIRST_PER_LEVEL'
            entrydir.mkdir(parents=True)
            times=np.arange(140,dtype='int64')*m.INTERVAL+1787184000000
            btc=100*np.cumprod(np.r_[1,1+.003*np.sin(np.arange(139)*.19)])
            alt=20*np.cumprod(np.r_[1,1+.004*np.sin(np.arange(139)*.19+.2)])
            pd.DataFrame({'timestamp':times,'close':btc}).to_csv(candles/'BTCUSDT_30m.csv',index=False)
            pd.DataFrame({'timestamp':times,'close':alt}).to_csv(candles/'AAAUSDT_30m.csv',index=False)
            entries=pd.DataFrame([{'entry_id':'e1','attempt_id':'a1','symbol':'AAAUSDT',
               'route':'FLIP_1M','entry_ts':int(times[-1]+m.INTERVAL),
               'entry_utc':pd.Timestamp(times[-1]+m.INTERVAL,unit='ms',tz='UTC').isoformat(),
               'entry_month':'2026-08','universe_group':'ADDITIONAL_275',
               'pre_pattern_48':'ASCENDING_CLEAN'}])
            entries.to_csv(entrydir/'entry_events.csv',index=False)
            scenarios=[]
            for (h,tp,sl) in m.CASES:
                scenarios.append({'entry_id':'e1','horizon_min':h,'tp_pct':tp,'sl_pct':sl,
                                  'result':'TP_FIRST','net_pct':tp-.1})
            pd.DataFrame(scenarios).to_csv(entrydir/'entry_scenarios_25.csv',index=False)
            (entrydir/'manifest.json').write_text(json.dumps({'scope':'FIRST_PER_LEVEL','entries':1}))
            with patch.object(m,'BASE',base),patch.object(m,'CANDLES',candles):
                m.run('FIRST_PER_LEVEL')
                assert len(pd.read_csv(base/'btc_strength_FIRST_PER_LEVEL'/'entry_features.csv'))==1
                paired=pd.read_csv(base/'btc_strength_FIRST_PER_LEVEL'/'paired_results.csv')
                self.assertEqual(len(paired),4)
                self.assertEqual(set(paired.btc_context_status),{'AVAILABLE'})
                manifest=json.loads((base/'btc_strength_FIRST_PER_LEVEL'/'manifest.json').read_text())
                self.assertEqual(manifest['context_available'],1)
