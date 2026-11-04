import hashlib
import tempfile
import unittest
from pathlib import Path
from hll_unified_v111_ui_install import run,SOURCE_REL,TARGET_REL

class UIInstallTests(unittest.TestCase):
    def setup_tree(self,root):
        source=root/SOURCE_REL;target=root/TARGET_REL
        source.parent.mkdir(parents=True);target.parent.mkdir(parents=True)
        source.write_bytes(b'new panel')
        target.write_bytes(b'original panel')
        old=hashlib.sha256(b'original panel').hexdigest()
        new=hashlib.sha256(b'new panel').hexdigest()
        return source,target,old,new
    def test_plan_is_read_only_apply_creates_backup_and_is_atomic(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);source,target,old,new=self.setup_tree(root)
            plan=run(root,expected_old=old,expected_new=new)
            self.assertEqual(plan['status'],'READY')
            self.assertEqual(target.read_bytes(),b'original panel')
            self.assertFalse(Path(plan['backup']).exists())
            applied=run(root,apply=True,expected_old=old,expected_new=new)
            self.assertEqual(applied['status'],'APPLIED')
            self.assertEqual(target.read_bytes(),b'new panel')
            self.assertEqual(Path(applied['backup']).read_bytes(),b'original panel')
    def test_unknown_installed_view_rejected_without_backup(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);source,target,old,new=self.setup_tree(root)
            target.write_bytes(b'personal changes')
            with self.assertRaisesRegex(ValueError,'DIFERENTE'):
                run(root,apply=True,expected_old=old,expected_new=new)
            self.assertEqual(target.read_bytes(),b'personal changes')
            self.assertEqual(len(list(target.parent.glob('*.bak-v111-*'))),0)
    def test_tampered_candidate_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);source,target,old,new=self.setup_tree(root)
            source.write_bytes(b'changed after packaging')
            with self.assertRaisesRegex(ValueError,'candidato'):
                run(root,apply=True,expected_old=old,expected_new=new)
            self.assertEqual(target.read_bytes(),b'original panel')
    def test_double_apply_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);source,target,old,new=self.setup_tree(root)
            run(root,apply=True,expected_old=old,expected_new=new)
            with self.assertRaisesRegex(ValueError,'ya está instalado'):
                run(root,apply=True,expected_old=old,expected_new=new)

if __name__=='__main__':unittest.main()
