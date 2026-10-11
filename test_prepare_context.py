import importlib.util
from pathlib import Path
import pandas as pd
import numpy as np
p = Path(__file__).with_name('prepare_v2_sep_context.py')
spec=importlib.util.spec_from_file_location('prep',p)
mod=importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
a=mod.SEP_START
s1='BTCUSDT';s2='ETHUSDT';s3='XUSDT'
def df(closes):
    arr=[]
    for i,c in enumerate(closes):
        ts=a+i*mod.FOUR_H
        arr.append({'timestamp':ts,'open':c,'high':c,'low':c,'close':c,'volume':1,'close_time':ts+mod.FOUR_H-1})
    return pd.DataFrame(arr)
raw={s1:df([100,101,103,102]), s2:df([100,101,101,102]),s3:df([100,99,98,97])}
market=mod._build_market([s1,s2,s3],raw)
assert market.iloc[1]['valid_symbols']==3
assert market.iloc[1]['positive_symbols']==2
assert abs(market.iloc[1]['market_breadth_4h'] -66.6667) < .0001
assert abs(market.iloc[2]['breadth_delta_4h']-(-33.3334)) < .00011
assert abs(market.iloc[2]['btc_return_delta_4h'] - round(100*(103/101-1),4) + round(100*(101/100-1),4)) < 1e-6
# entry in 8h-12h period can only use 4h-8h candle (open at 4h)
events=pd.DataFrame({'entry_timestamp':[a+8*3600_000+60_000], 'retest_timestamp':[a+8*3600_000], 'side_adjusted_strength_vs_btc_4h':[1.1]})
joined=mod._enrich_events(events,market)
assert joined.iloc[0]['historical_4h_closed_boundary']==a+mod.FOUR_H
print('PASS: BTC change, 4h breadth, causal closed boundary, merging')
