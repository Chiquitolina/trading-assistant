import tempfile
from pathlib import Path
import unittest
from datetime import datetime, timezone
from pre_reaction_1m_backfill_v0 import (ONE,parse_page,download_symbol,range_ms,write_symbol,read_symbol,months_for)

class Tests(unittest.TestCase):
    def api_rows(self,a,b):
        return [[str(t),'1','1.1','.9','1','10',str(t+ONE-1),'0','0','0','0','0'] for t in range(a,b,ONE)]
    def test_align(self):
        a,b=range_ms(10000*ONE,11440*ONE)
        self.assertEqual((b-a)//ONE,1981)
    def test_month_cross(self):
        a=int(datetime(2026,9,30,23,58,tzinfo=timezone.utc).timestamp()*1000)
        self.assertEqual(months_for(a,a+5*ONE),['2026-09','2026-10'])
    def test_parse(self):
        rows=parse_page(self.api_rows(10*ONE,12*ONE),'AAAUSDT',10*ONE,12*ONE)
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[0],(10*ONE,1.,1.1,.9,1.,10.))
    def test_bad_close_time(self):
        rows=self.api_rows(10*ONE,11*ONE);rows[0][6]='42'
        with self.assertRaises(RuntimeError):parse_page(rows,'AAAUSDT',10*ONE,11*ONE)
    def test_download_complete(self):
        def fetch(s,a,b):return parse_page(self.api_rows(a,b),s,a,b)
        result=download_symbol('AAAUSDT',10*ONE,1020*ONE,fetcher=fetch,delay=0)
        self.assertEqual(len(result),1010)
    def test_download_gap_refused(self):
        def fetch(s,a,b):return parse_page(self.api_rows(a,b)[:-1],s,a,b)
        with self.assertRaisesRegex(RuntimeError,'Incomplete'):download_symbol('AAAUSDT',10*ONE,20*ONE,fetcher=fetch,delay=0)
    def test_write_audit(self):
        with tempfile.TemporaryDirectory() as t:
            cache=Path(t)/'cache'
            a=int(datetime(2026,9,30,23,58,tzinfo=timezone.utc).timestamp()*1000);b=a+5*ONE
            rows=parse_page(self.api_rows(a,b),'AAAUSDT',a,b)
            self.assertEqual(write_symbol(cache,'AAAUSDT',a,b,rows)['status'],'DOWNLOADED_VERIFIED')
            frame,_=read_symbol(cache,'AAAUSDT',a,b)
            self.assertEqual(len(frame),5)
            with self.assertRaises(RuntimeError):read_symbol(cache,'AAAUSDT',a,b+ONE)
            self.assertEqual(write_symbol(cache,'AAAUSDT',a,b,rows)['status'],'EXISTING_VALID')
    def test_corrupt_hash(self):
        with tempfile.TemporaryDirectory() as t:
            cache=Path(t)/'cache';a=10*ONE;b=20*ONE
            rows=parse_page(self.api_rows(a,b),'AAAUSDT',a,b)
            write_symbol(cache,'AAAUSDT',a,b,rows)
            part=next((cache/'AAAUSDT').glob('*.gz'))
            with part.open('ab') as fd:fd.write(b'intrusion')
            with self.assertRaisesRegex(RuntimeError,'hash changed'):read_symbol(cache,'AAAUSDT',a,b)

if __name__=='__main__':unittest.main()
