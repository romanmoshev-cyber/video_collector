from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scanner import MB, STALE_DOWNLOAD_DIR_AGE_SEC, ScanOptions, Scanner, _iter_messages_kwargs, _period_scan_action, _reject_reason, _video_orientation


class VideoRulesTest(unittest.TestCase):
    def test_detects_both_orientations_and_skips_square(self) -> None:
        self.assertEqual(_video_orientation(1080, 1920), 'vertical')
        self.assertEqual(_video_orientation(1920, 1080), 'horizontal')
        self.assertIsNone(_video_orientation(1080, 1080))
        self.assertEqual(_reject_reason(1080, 1080, 180, 30 * MB), 'square')

    def test_accepts_qualified_vertical_and_horizontal_video(self) -> None:
        self.assertIsNone(_reject_reason(1080, 1920, 180, 30 * MB))
        self.assertIsNone(_reject_reason(1280, 720, 180, 30 * MB))

    def test_horizontal_video_requires_720_pixel_height(self) -> None:
        self.assertEqual(_reject_reason(1280, 719, 180, 30 * MB), 'too_low_resolution')


class IterMessagesKwargsTest(unittest.TestCase):
    def test_new_to_old_period_does_not_use_offset_date(self) -> None:
        since = datetime(2026, 5, 9, tzinfo=timezone.utc)
        kwargs = _iter_messages_kwargs(ScanOptions(mode='week', chat_ids=None, order='new_to_old'), since, min_id=0)

        self.assertEqual(kwargs, {'limit': None, 'min_id': 0, 'reverse': False})

    def test_old_to_new_period_starts_from_requested_window(self) -> None:
        since = datetime(2026, 5, 9, tzinfo=timezone.utc)
        kwargs = _iter_messages_kwargs(ScanOptions(mode='week', chat_ids=None, order='old_to_new'), since, min_id=0)

        self.assertEqual(kwargs['limit'], None)
        self.assertEqual(kwargs['min_id'], 0)
        self.assertTrue(kwargs['reverse'])
        self.assertIs(kwargs['offset_date'], since)

    def test_old_to_new_all_history_has_no_date_bound(self) -> None:
        kwargs = _iter_messages_kwargs(ScanOptions(mode='all', chat_ids=None, order='old_to_new'), None, min_id=123)

        self.assertEqual(kwargs, {'limit': None, 'min_id': 123, 'reverse': True})


class PeriodScanActionTest(unittest.TestCase):
    def test_new_to_old_stops_after_window(self) -> None:
        since = datetime(2026, 5, 9, tzinfo=timezone.utc)
        old_message = datetime(2026, 5, 8, 23, tzinfo=timezone.utc)

        self.assertEqual(_period_scan_action(old_message, since, reverse=False), 'stop')

    def test_old_to_new_skips_until_window(self) -> None:
        since = datetime(2026, 5, 9, tzinfo=timezone.utc)
        old_message = datetime(2026, 5, 8, 23, tzinfo=timezone.utc)

        self.assertEqual(_period_scan_action(old_message, since, reverse=True), 'skip')

    def test_message_inside_window_is_kept(self) -> None:
        since = datetime(2026, 5, 9, tzinfo=timezone.utc)
        new_message = datetime(2026, 5, 10, tzinfo=timezone.utc)

        self.assertEqual(_period_scan_action(new_message, since, reverse=False), 'keep')
        self.assertEqual(_period_scan_action(new_message, since, reverse=True), 'keep')


class DownloadDirCleanupTest(unittest.TestCase):
    def test_disk_check_removes_only_stale_work_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            download_dir = Path(tmp)
            stale_dir = download_dir / 'external_stale'
            fresh_dir = download_dir / 'external_fresh'
            stale_dir.mkdir()
            fresh_dir.mkdir()
            old_ts = time.time() - STALE_DOWNLOAD_DIR_AGE_SEC - 60
            os.utime(stale_dir, (old_ts, old_ts))

            scanner = Scanner.__new__(Scanner)
            scanner.download_dir = download_dir
            scanner.min_free_disk = 10**9 * MB

            with self.assertRaisesRegex(RuntimeError, 'MIN_FREE_DISK_MB'):
                scanner._ensure_download_dir_ready()

            self.assertFalse(stale_dir.exists())
            self.assertTrue(fresh_dir.exists())


if __name__ == '__main__':
    unittest.main()
