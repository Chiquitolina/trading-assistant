import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('vps', ROOT/'scripts/research_mirror_vps.py')
vps=importlib.util.module_from_spec(spec);spec.loader.exec_module(vps)
spec2=importlib.util.spec_from_file_location('pc', ROOT/'windows/descargar_research_pc.py')
pc=importlib.util.module_from_spec(spec2);spec2.loader.exec_module(pc)

class MirrorTests(unittest.TestCase):
    def test_secret_names_and_symlinks_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp); project=base/'project'; research=base/'research'
            (project/'reports').mkdir(parents=True);research.mkdir()
            (project/'.env').write_text('dontcopy')
            (project/'settings-api_key.json').write_text('dontcopy')
            (project/'reports'/'good.csv').write_text('x\n1\n')
            (project/'reports'/'link.csv').symlink_to(project/'.env')
            files=vps.discover(project,research,True)
            names={f.name for f,_ in files}
            self.assertIn('good.csv',names)
            self.assertNotIn('.env',names)
            self.assertNotIn('link.csv',names)
            self.assertNotIn('settings-api_key.json',names)

    def test_stream_end_to_end_sqlite_consistent_and_verify(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp); project=base/'project'; research=base/'research'
            dbdir=project/'reports/reaction_lab';dbdir.mkdir(parents=True)
            (project/'tests').mkdir(parents=True)
            (project/'tests'/'example.py').write_text('a=2\n')
            (project/'.env').write_text('SECRET=xxx\n')
            research.mkdir()
            (research/'runner_example').mkdir()
            (research/'runner_example'/'replay_events.csv').write_text('event_key\nX|LONG|123\n')
            db=dbdir/'scanner_events_v1.sqlite3'
            with sqlite3.connect(db) as con:
                con.execute('CREATE TABLE events(id INTEGER PRIMARY KEY, symbol TEXT)')
                con.execute("INSERT INTO events(symbol) VALUES ('BTCUSDT')")
            arch=base/'out.tar.gz'
            with arch.open('wb') as fh:
                proc=subprocess.run([sys.executable,str(ROOT/'scripts/research_mirror_vps.py'),'stream',
                    '--project',str(project),'--research-root',str(research),'--include-code'],stdout=fh,stderr=subprocess.PIPE)
            self.assertEqual(proc.returncode,0,proc.stderr.decode())
            manifest=pc.verified_manifest(arch)
            self.assertEqual(manifest['status'],'COMPLETE_PER_FILE')
            self.assertTrue(any(e['sqlite_consistent_backup'] for e in manifest['files']))
            with tarfile.open(arch,'r:gz') as t:
                self.assertNotIn('mirror/'+str(project/'.env').lstrip('/'),t.getnames())
                sqlite_member='mirror/'+str(db).lstrip('/')
                content=t.extractfile(sqlite_member).read()
            from tempfile import NamedTemporaryFile
            with NamedTemporaryFile(suffix='.sqlite3') as f:
                f.write(content);f.flush()
                with sqlite3.connect('file:'+f.name+'?mode=ro',uri=True) as con:
                    self.assertEqual(con.execute('SELECT symbol FROM events').fetchone()[0],'BTCUSDT')
            with arch.open('ab') as f:f.write(b'')

    def test_hash_tampering_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp)/'broken.tar.gz'
            payload=b'NOT_ORIGINAL'
            m={'files':[{'archive_path':'mirror/example.csv','bytes':len(payload),'sha256':'0'*64}], 'errors':[]}
            with tarfile.open(out,'w:gz') as tar:
                info=tarfile.TarInfo('mirror/example.csv');info.size=len(payload)
                tar.addfile(info,io.BytesIO(payload))
                blob=json.dumps(m).encode()
                info=tarfile.TarInfo('backup_manifest.json');info.size=len(blob)
                tar.addfile(info,io.BytesIO(blob))
            with self.assertRaisesRegex(ValueError,'CHECKSUM MISMATCH'):
                pc.verified_manifest(out)

if __name__=='__main__': unittest.main()
