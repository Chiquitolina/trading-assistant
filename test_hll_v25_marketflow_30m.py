import unittest
from hll_v25_marketflow_30m import TF, ret, contextualize

class FlowTests(unittest.TestCase):
    def setUp(self):
        self.btc={i*TF:100+i/10 for i in range(50)}
        self.alts={f'TEST{i}USDT':{j*TF:30+j/10+i/100 for j in range(50)} for i in range(10)}
        self.event=dict(event_id='a',symbol='TEST0USDT',direction='LONG',stage='RESISTANCE_CROSS_UP',known_closed_ts=48*TF)
    def test_future_candle_does_not_change_past_context(self):
        a=contextualize(self.event,self.btc,self.alts)
        self.btc[49*TF]=100000000
        for v in self.alts.values():v[49*TF]=0.0001
        b=contextualize(self.event,self.btc,self.alts)
        self.assertEqual(a,b)
    def test_missing_intermediate_candle_rejects_return(self):
        del self.btc[47*TF]
        self.assertIsNone(ret(self.btc,48*TF,8))
    def test_risk_on_side_adjusted_symmetry(self):
        a=contextualize(self.event,self.btc,self.alts)
        self.assertEqual(a['market_regime'],'RISK_ON')
        self.assertEqual(a['side_flow'],'TAILWIND')
        b=contextualize({**self.event,'direction':'SHORT'},self.btc,self.alts)
        self.assertEqual(b['side_flow'],'HEADWIND')
    def test_missing_breadth_is_unavailable(self):
        self.alts={k:v for k,v in list(self.alts.items())[:2]}
        a=contextualize(self.event,self.btc,self.alts)
        self.assertEqual(a['side_flow'],'UNAVAILABLE')
    def test_no_24h_context_when_insufficient_prebars(self):
        a=contextualize({**self.event,'known_closed_ts':30*TF},self.btc,self.alts)
        self.assertEqual(a['btc_return_24h_pct'],'')

if __name__=='__main__':unittest.main()
