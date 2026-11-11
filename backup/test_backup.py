"""Check that the main n8n backup includes committed SQLite WAL changes."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('cortex_backup', Path(__file__).with_name('backup.py'))
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


class MainN8nBackupTest(unittest.TestCase):
    def test_live_wal_snapshot_preserves_workflows_and_encrypted_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'live'
            project = root / 'project'
            work = root / 'snapshot'
            for path in (source, project, work):
                path.mkdir()
            (source / 'config').write_text(json.dumps({'encryptionKey': 'test-key'}))
            for name in ('.env', 'docker-compose.yml', 'Dockerfile', 'README.md', 'build_workflow.py', 'import.sh'):
                (project / name).write_text('test fixture')
            live = sqlite3.connect(source / 'database.sqlite')
            try:
                live.execute('PRAGMA journal_mode=WAL')
                live.execute('CREATE TABLE workflow_entity(id text,name text)')
                live.execute('CREATE TABLE credentials_entity(id text,data text)')
                live.execute("INSERT INTO workflow_entity VALUES('ReelsFactoryV2ES1','Reels Factory')")
                live.execute("INSERT INTO credentials_entity VALUES('original-key','encrypted-value')")
                live.commit()
                self.assertGreater((source / 'database.sqlite-wal').stat().st_size, 0)
                counts = backup.snapshot_main_n8n(work, source, project)
                self.assertEqual(counts, {'workflows': 1, 'credentials': 1})
                with sqlite3.connect(work / 'main-n8n/database.sqlite') as restored:
                    self.assertEqual(restored.execute('PRAGMA quick_check').fetchone()[0], 'ok')
                    self.assertEqual(restored.execute('SELECT id,name FROM workflow_entity').fetchall(), [('ReelsFactoryV2ES1', 'Reels Factory')])
                    self.assertEqual(restored.execute('SELECT data FROM credentials_entity').fetchone()[0], 'encrypted-value')
                self.assertEqual(live.execute('SELECT count(*) FROM workflow_entity').fetchone()[0], 1)
                self.assertEqual((work / 'main-n8n/config').read_bytes(), (source / 'config').read_bytes())
                self.assertFalse((work / 'main-n8n/database.sqlite-wal').exists())
            finally:
                live.close()


if __name__ == '__main__':
    unittest.main()
