from __future__ import annotations

import tempfile
from pathlib import Path
import sqlite3
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db import DB
from scanner import _video_meta_summary


class VideoMetaSummaryTest(unittest.TestCase):
    def test_formats_resolution_and_size(self) -> None:
        summary = _video_meta_summary(1080, 1920, 181.7, 25 * 1024 * 1024)

        self.assertEqual(summary['resolution'], '1080×1920')
        self.assertEqual(summary['duration'], 181)
        self.assertEqual(summary['size_human'], '25.0 МБ')

    def test_unknown_resolution_is_explicit(self) -> None:
        summary = _video_meta_summary(None, None, None, 0)

        self.assertEqual(summary['resolution'], 'неизвестно')
        self.assertEqual(summary['size_human'], '0 Б')


class LinkStatsDBTest(unittest.IsolatedAsyncioTestCase):
    async def test_link_upload_stats_are_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = DB(Path(tmp) / 'bot.sqlite3')
            await db.connect()
            try:
                await db.add_link_upload({
                    'link': 'https://example.com/video',
                    'source_name': 'example.com',
                    'title': 'Demo',
                    'resolution': '1080×1920',
                    'size': 10,
                    'duration': 20,
                    'target_message_id': 123,
                    'orientation': 'horizontal',
                    'status': 'ok',
                    'elapsed_sec': 3,
                    'created_at': 1,
                })
                await db.add_link_upload({
                    'link': 'https://example.com/broken',
                    'source_name': 'example.com',
                    'status': 'error',
                    'error': 'boom',
                    'created_at': 2,
                })

                stats = await db.get_link_stats()
            finally:
                await db.close()

        self.assertEqual(stats['total'], 2)
        self.assertEqual(stats['ok'], 1)
        self.assertEqual(stats['errors'], 1)
        self.assertEqual(stats['bytes_uploaded'], 10)
        self.assertEqual(stats['duration_sec'], 20)
        self.assertEqual(stats['recent'][0]['status'], 'error')
        self.assertEqual(stats['recent'][1]['orientation'], 'horizontal')

    async def test_existing_database_is_migrated_for_orientation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'bot.sqlite3'
            with sqlite3.connect(path) as conn:
                conn.execute(
                    'CREATE TABLE link_uploads ('
                    'id INTEGER PRIMARY KEY AUTOINCREMENT, link TEXT NOT NULL, source_name TEXT NOT NULL, '
                    'title TEXT, resolution TEXT, size INTEGER NOT NULL DEFAULT 0, '
                    'duration INTEGER NOT NULL DEFAULT 0, target_message_id INTEGER, status TEXT NOT NULL, '
                    'error TEXT, elapsed_sec INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL)'
                )

            db = DB(path)
            await db.connect()
            try:
                async with db.conn.execute('PRAGMA table_info(link_uploads)') as cur:
                    columns = {row['name'] for row in await cur.fetchall()}
            finally:
                await db.close()

        self.assertIn('orientation', columns)


if __name__ == '__main__':
    unittest.main()
