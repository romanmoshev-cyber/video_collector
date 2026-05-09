from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scanner import ScanOptions, _iter_messages_kwargs


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


if __name__ == '__main__':
    unittest.main()
