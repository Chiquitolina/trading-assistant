import csv
import json
import tempfile
import unittest
from pathlib import Path
from dashboard.views.hll_v299_atlas_parity import assess, epoch_ms, load_rows, compare_rows, main

START = epoch_ms('2026-09-07T00:00:00Z')
END = epoch_ms('2026-10-01T00:00:00Z')


def putcsv(p, rows):
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open('w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader(); writer.writerows(rows)


def build(tmp, alter=False, more=False):
    b, n = tmp / 'baseline', tmp / 'extended'
    syms = [f'SYM{i:03d}USDT' for i in range(15)]
    for root, all_syms in [(b, syms), (n, syms + [f'EXT{i:03d}USDT' for i in range(275)])]:
        root.mkdir()
        (root / 'run_config.json').write_text(json.dumps({'version':'v21', 'config':{'left':2}, 'symbols':all_syms}))
        for sym in syms:
            formation = [{'level_id': sym+'lvl', 'formation_closed_ts':str(START),
                          'zone_low':'1.2', 'source_30m':'PATH1' if root == b else 'PATH2'}]
            events = [{'event_id':sym+'ev','observed_closed_ts':str(START+30*60*1000),
                       'close':'1.4','source_30m':'PATH1' if root == b else 'PATH2'}]
            if alter and root == n and sym == syms[0]:
                events[0]['close'] = '1.5'
            if more:
                events.append({'event_id':sym+'outside','observed_closed_ts':str(END+30*60*1000),
                               'close':'1.6', 'source_30m':'OTHER'})
            putcsv(root/'shards'/f'{sym}.formation.csv', formation)
            putcsv(root/'shards'/f'{sym}.events.csv', events)
    return b,n


class V299AtlasParity(unittest.TestCase):
    def test_15_exact_match_even_if_provenance_differs(self):
        with tempfile.TemporaryDirectory() as t:
            b,n=build(Path(t))
            result, rows, examples=assess(b,n,START,END)
            self.assertTrue(result['pass'])
            self.assertEqual(result['matched_symbols'],15)
            self.assertEqual(result['events_checked_original'],15)
            self.assertFalse(examples)

    def test_detect_changed_event(self):
        with tempfile.TemporaryDirectory() as t:
            b,n=build(Path(t),alter=True)
            result,_,examples=assess(b,n,START,END)
            self.assertFalse(result['pass'])
            self.assertEqual(result['mismatch_by_type']['events']['changed'],1)
            self.assertIn('close',examples[0]['fields'])

    def test_outside_window_ignored(self):
        with tempfile.TemporaryDirectory() as t:
            b,n=build(Path(t),more=True)
            result,_,_=assess(b,n,START,END)
            self.assertTrue(result['pass'])
            self.assertEqual(result['events_checked_extended'],15)

    def test_mismatched_version_blocks_parity(self):
        with tempfile.TemporaryDirectory() as t:
            b,n=build(Path(t))
            data=json.loads((n/'run_config.json').read_text())
            data['config']['left']=3
            (n/'run_config.json').write_text(json.dumps(data))
            result,_,_=assess(b,n,START,END)
            self.assertFalse(result['pass'])

    def test_output_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as t:
            b,n=build(Path(t))
            output=Path(t)/'output'
            args=['--run','--baseline',str(b),'--extended',str(n),'--output',str(output)]
            self.assertEqual(main(args),0)
            with self.assertRaises(SystemExit):
                main(args)

    def test_invalid_duplicate_key_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'x.csv'
            putcsv(p,[{'event_id':'x','observed_closed_ts':str(START)},
                      {'event_id':'x','observed_closed_ts':str(START)}])
            with self.assertRaises(ValueError):
                load_rows(p,'event_id','observed_closed_ts',START,END)


if __name__ == '__main__':
    unittest.main()
