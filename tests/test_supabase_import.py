from contextlib import closing
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    'import_supabase', Path(__file__).resolve().parents[1] / 'scripts' / 'import_supabase.py')
importer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(importer)


class ImportTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / 'salesquest.db'
        self.state = {'leads': [{'id': 'test-lead'}], 'dbs': [], 'sequences': []}
        with closing(sqlite3.connect(self.database)) as conn:
            conn.execute('CREATE TABLE kv (k TEXT PRIMARY KEY, v TEXT)')
            conn.execute('INSERT INTO kv VALUES (?, ?)', ('state', json.dumps(self.state)))
            conn.execute('INSERT INTO kv VALUES (?, ?)', ('config', 'must-not-be-uploaded'))
            conn.commit()
        self.secret = 'sb_secret_test_fixture'

    def test_preview_does_not_modify_database_or_create_backup(self):
        before = self.database.read_bytes()
        self.assertEqual(importer.read_state(self.database), self.state)
        self.assertEqual(self.database.read_bytes(), before)
        self.assertFalse((self.database.parent / 'backups').exists())

    def test_apply_backs_up_imports_only_state_and_verifies_every_field(self):
        responses = [[], {'rev': 1, 'already_imported': False}, [{'v': json.dumps(self.state)}]]
        with patch.object(importer, 'request_json', side_effect=responses) as request:
            result = importer.apply_import(self.database, importer.PROJECT_URL, self.secret)
        self.assertTrue(result['verified'])
        self.assertEqual(importer.read_state(Path(result['backup'])), self.state)
        self.assertEqual(request.call_args_list[1].args[3], {'p_state': self.state})
        self.assertEqual(importer.read_state(self.database), self.state)

    def test_wrong_project_or_missing_server_key_cannot_transmit_data(self):
        with patch.object(importer, 'request_json') as request:
            for url, key in [('https://other.supabase.co', self.secret), (importer.PROJECT_URL, '')]:
                with self.assertRaises(ValueError):
                    importer.apply_import(self.database, url, key)
            request.assert_not_called()

    def test_rejected_import_does_not_modify_local_database(self):
        with patch.object(importer, 'request_json', side_effect=[[], RuntimeError('Cloud data exists')]):
            with self.assertRaisesRegex(RuntimeError, 'Cloud data exists'):
                importer.apply_import(self.database, importer.PROJECT_URL, self.secret)
        self.assertEqual(importer.read_state(self.database), self.state)
        self.assertEqual(len(list((self.database.parent / 'backups').glob('*.db'))), 1)

    def test_remote_mismatch_is_reported_instead_of_claiming_success(self):
        with patch.object(importer, 'request_json', side_effect=[[], {'rev': 1}, [{'v': '{"leads": []}'}]]):
            with self.assertRaisesRegex(RuntimeError, 'verification failed'):
                importer.apply_import(self.database, importer.PROJECT_URL, self.secret)


if __name__ == '__main__':
    unittest.main()
