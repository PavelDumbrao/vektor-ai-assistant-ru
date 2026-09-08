import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / 'server/tools/history_backfill.py'
spec = importlib.util.spec_from_file_location('history_backfill', MODULE)
hb = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(hb)


class HistoryBackfillTests(unittest.TestCase):
    def setUp(self):
        hb.OWNER_ID = '42'
        self.settings = {'tenant_id': 'client', 'source_id': 'telegram_business'}
        self.key = b'k' * 64

    def test_flatten_text_supports_rich_export(self):
        value = ['hello ', {'type': 'bold', 'text': 'world'}]
        self.assertEqual(hb.flatten_text(value), 'hello world')

    def test_single_chat_normalizes_deterministically(self):
        data = {
            'name': 'Old manager',
            'id': 'manager-chat',
            'messages': [
                {'id': 1, 'type': 'message', 'date': '2026-09-01T10:00:00+00:00',
                 'from': 'Owner', 'from_id': 'user42', 'text': 'out'},
                {'id': 2, 'type': 'message', 'date': '2026-09-01T10:01:00+00:00',
                 'from': 'Manager', 'from_id': 'user99', 'text': 'in', 'reply_to_message_id': 1},
            ],
        }
        source = Path('/tmp/result.json')
        rows1, summaries1 = hb.normalize_export(source, data, self.key, self.settings)
        rows2, summaries2 = hb.normalize_export(source, data, self.key, self.settings)
        self.assertEqual(rows1, rows2)
        self.assertEqual(summaries1, summaries2)
        self.assertEqual([r['direction'] for r in rows1], ['outgoing', 'incoming'])
        self.assertLessEqual(rows1[0]['chat_id'], -(2**61))
        self.assertTrue(rows1[0]['source_ref'].startswith('chat:'))
        self.assertTrue(rows1[0]['message_ref'].startswith('message:'))
        self.assertEqual(rows1[1]['reply_to_message_id'], 1)
        self.assertEqual(len(summaries1), 1)
        self.assertEqual(summaries1[0]['messages'], 2)

    def test_multi_chat_export_keeps_sources_separate(self):
        data = {'chats': {'list': [
            {'name': 'A', 'id': 1, 'messages': [{'id': 1, 'type': 'message', 'text': 'a'}]},
            {'name': 'B', 'id': 2, 'messages': [{'id': 1, 'type': 'message', 'text': 'b'}]},
        ]}}
        rows, summaries = hb.normalize_export(Path('/tmp/result.json'), data, self.key, self.settings)
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(summaries), 2)
        self.assertNotEqual(rows[0]['source_ref'], rows[1]['source_ref'])
        self.assertNotEqual(rows[0]['chat_id'], rows[1]['chat_id'])


if __name__ == '__main__':
    unittest.main()
