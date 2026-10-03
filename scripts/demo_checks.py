"""Release/demo contract probes; no provider or external database connections."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import package_public
import release_scan


class ReleaseChecks(unittest.TestCase):
    def test_forbidden_inputs_are_refused(self):
        samples=[('/Users/'+'example-owner/','local_user_path'),
                 ('private'+'_runtime','private_marker'), ('stock'+'_secret','private_marker'),
                 ('sk-'+'a'*32,'credential'), ('600'+'001.SH','security_identifier')]
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            for value,rule in samples:
                p=root/'README.md';p.write_text(value)
                self.assertIn(rule,{item['rule'] for item in release_scan.inspect_file(p,'README.md')})
                with self.assertRaises(ValueError):package_public.public_files(root)

    def test_database_credential_and_large_files_are_refused(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            for name,data,rule in [('config.url',b'not a real connection','forbidden_file'),
                                   ('data.txt',b'SQLite format 3\0','database_binary'),
                                   ('too-large.txt',b'a'*(release_scan.MAX_BYTES+1),'large_file')]:
                p=root/name;p.write_bytes(data)
                self.assertIn(rule,{item['rule'] for item in release_scan.inspect_file(p,name)})

    def test_allowlist_excludes_answers_history_and_runtime(self):
        for name in ['work/answers.json','.git/config','docs/evidence/raw.txt','objects/item.json',
                     'eval/answers.json','eval/hidden/results.json']:
            self.assertFalse(package_public.allowed(name),name)
        for name in ['README.md','src/analysis_agent/runtime.py','apps/web/package-lock.json',
                     'docs/release/media/demo.mp4','eval/results/b2/summary.json']:
            self.assertTrue(package_public.allowed(name),name)

    def test_destination_cannot_be_reused(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/'source';root.mkdir();(root/'README.md').write_text('Public synthetic example')
            target=Path(d)/'release';package_public.package(root,target)
            with self.assertRaises(ValueError):package_public.package(root,target)
            self.assertTrue((target/'PUBLIC_MANIFEST.json').is_file())

    def test_fixture_only_never_instantiates_real_provider(self):
        import demo_runtime
        with patch.object(demo_runtime,'DeepSeekProvider',side_effect=AssertionError('paid provider forbidden')):
            self.assertEqual(demo_runtime.optional_real(None,True)['status'],'NOT RUN')

    def test_fixture_report_uses_accepted_cells_and_row_metadata(self):
        import demo_runtime,json
        query=demo_runtime.fixture_response({'messages':[{}, {'content':json.dumps({'accepted_evidence':[]})}]})
        self.assertEqual(query['tool_ref'],'compare@2')
        context={'accepted_evidence':[{'evidence_id':'synthetic-evidence','tool_ref':'compare@2',
            'rows':[{'orders':12,'refunded_orders':3,'refund_rate':'0.25'}],
            'row_references':[{'row':0,'entity':{'window':'current'},'time_range':{'start':'a','end':'b'}}],
            'units':{'orders':'orders','refunded_orders':'orders','refund_rate':'ratio'}}]}
        response=demo_runtime.fixture_response({'messages':[{}, {'content':json.dumps(context)}]})
        facts=response['report']['facts'];self.assertEqual(len(facts),3)
        self.assertEqual(facts[2]['value'],'0.25');self.assertIsInstance(facts[0]['value'],int)
        self.assertEqual(facts[0]['inputs'][0]['evidence_id'],'synthetic-evidence')

if __name__=='__main__':unittest.main(verbosity=2)
