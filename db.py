from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aiosqlite


class DB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute('PRAGMA journal_mode=WAL')
        await self.conn.execute('PRAGMA synchronous=NORMAL')
        await self.conn.execute('PRAGMA foreign_keys=ON')
        await self.conn.executescript(
            '''
            CREATE TABLE IF NOT EXISTS forwarded_messages (
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                forwarded_at INTEGER NOT NULL,
                PRIMARY KEY (chat_id, message_id)
            );

            CREATE TABLE IF NOT EXISTS scan_state (
                chat_id INTEGER PRIMARY KEY,
                last_scanned_message_id INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS kv_store (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS scan_chat_stats (
                period TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                chat_name TEXT NOT NULL,
                chat_link TEXT,
                checked INTEGER NOT NULL DEFAULT 0,
                video_found INTEGER NOT NULL DEFAULT 0,
                matched INTEGER NOT NULL DEFAULT 0,
                forwarded INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (period, chat_id)
            );

            CREATE TABLE IF NOT EXISTS link_uploads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                link TEXT NOT NULL,
                source_name TEXT NOT NULL,
                title TEXT,
                resolution TEXT,
                size INTEGER NOT NULL DEFAULT 0,
                duration INTEGER NOT NULL DEFAULT 0,
                target_message_id INTEGER,
                status TEXT NOT NULL,
                error TEXT,
                elapsed_sec INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL
            );
            '''
        )
        await self.conn.commit()

    async def close(self) -> None:
        if self.conn is not None:
            await self.conn.close()
            self.conn = None

    async def was_forwarded(self, chat_id: int, message_id: int) -> bool:
        assert self.conn is not None
        async with self.conn.execute(
            'SELECT 1 FROM forwarded_messages WHERE chat_id = ? AND message_id = ? LIMIT 1',
            (chat_id, message_id),
        ) as cur:
            row = await cur.fetchone()
        return row is not None

    async def mark_forwarded(self, chat_id: int, message_id: int, forwarded_at: int) -> None:
        assert self.conn is not None
        await self.conn.execute(
            'INSERT OR IGNORE INTO forwarded_messages(chat_id, message_id, forwarded_at) VALUES (?, ?, ?)',
            (chat_id, message_id, forwarded_at),
        )
        await self.conn.commit()

    async def get_last_scanned(self, chat_id: int) -> int:
        assert self.conn is not None
        async with self.conn.execute(
            'SELECT last_scanned_message_id FROM scan_state WHERE chat_id = ? LIMIT 1',
            (chat_id,),
        ) as cur:
            row = await cur.fetchone()
        return int(row['last_scanned_message_id']) if row else 0

    async def set_last_scanned(self, chat_id: int, last_scanned_message_id: int, updated_at: int) -> None:
        assert self.conn is not None
        await self.conn.execute(
            '''
            INSERT INTO scan_state(chat_id, last_scanned_message_id, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                last_scanned_message_id = excluded.last_scanned_message_id,
                updated_at = excluded.updated_at
            ''',
            (chat_id, last_scanned_message_id, updated_at),
        )
        await self.conn.commit()

    async def kv_set_json(self, key: str, value: Any) -> None:
        assert self.conn is not None
        await self.conn.execute(
            '''
            INSERT INTO kv_store(key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            ''',
            (key, json.dumps(value, ensure_ascii=False)),
        )
        await self.conn.commit()

    async def kv_get_json(self, key: str, default: Any = None) -> Any:
        assert self.conn is not None
        async with self.conn.execute('SELECT value FROM kv_store WHERE key = ? LIMIT 1', (key,)) as cur:
            row = await cur.fetchone()
        if not row:
            return default
        return json.loads(row['value'])

    async def upsert_chat_stats(self, period: str, rows: list[dict[str, Any]], updated_at: int) -> None:
        if not rows:
            return
        assert self.conn is not None
        await self.conn.executemany(
            """
            INSERT INTO scan_chat_stats(
                period, chat_id, chat_name, chat_link, checked, video_found, matched, forwarded, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(period, chat_id) DO UPDATE SET
                chat_name = excluded.chat_name,
                chat_link = excluded.chat_link,
                checked = excluded.checked,
                video_found = excluded.video_found,
                matched = excluded.matched,
                forwarded = excluded.forwarded,
                updated_at = excluded.updated_at
            """,
            [
                (
                    period,
                    int(row['id']),
                    str(row.get('name') or row['id']),
                    row.get('link'),
                    int(row.get('checked', 0) or 0),
                    int(row.get('video_found', 0) or 0),
                    int(row.get('matched', 0) or 0),
                    int(row.get('forwarded', 0) or 0),
                    updated_at,
                )
                for row in rows
            ],
        )
        await self.conn.commit()

    async def get_chat_stats(self, period: str, limit: int = 500) -> list[dict[str, Any]]:
        assert self.conn is not None
        async with self.conn.execute(
            """
            SELECT chat_id, chat_name, chat_link, checked, video_found, matched, forwarded, updated_at
            FROM scan_chat_stats
            WHERE period = ?
            ORDER BY matched DESC, forwarded DESC, video_found DESC, checked DESC, chat_name COLLATE NOCASE ASC
            LIMIT ?
            """,
            (period, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [
            {
                'id': int(row['chat_id']),
                'name': row['chat_name'],
                'link': row['chat_link'],
                'checked': int(row['checked']),
                'video_found': int(row['video_found']),
                'matched': int(row['matched']),
                'forwarded': int(row['forwarded']),
                'updated_at': int(row['updated_at']),
            }
            for row in rows
        ]

    async def add_link_upload(self, row: dict[str, Any]) -> None:
        assert self.conn is not None
        await self.conn.execute(
            """
            INSERT INTO link_uploads(
                link, source_name, title, resolution, size, duration, target_message_id, status, error, elapsed_sec, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(row.get('link') or ''),
                str(row.get('source_name') or row.get('chat_name') or '—'),
                row.get('title'),
                row.get('resolution'),
                int(row.get('size', 0) or 0),
                int(row.get('duration', 0) or 0),
                row.get('target_message_id'),
                str(row.get('status') or 'ok'),
                row.get('error'),
                int(row.get('elapsed_sec', 0) or 0),
                int(row.get('created_at', 0) or 0),
            ),
        )
        await self.conn.commit()

    async def get_link_stats(self) -> dict[str, Any]:
        assert self.conn is not None
        async with self.conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END) AS ok,
                SUM(CASE WHEN status != 'ok' THEN 1 ELSE 0 END) AS errors,
                COALESCE(SUM(CASE WHEN status = 'ok' THEN size ELSE 0 END), 0) AS bytes_uploaded,
                COALESCE(SUM(CASE WHEN status = 'ok' THEN duration ELSE 0 END), 0) AS duration_sec
            FROM link_uploads
            """
        ) as cur:
            totals = await cur.fetchone()

        async with self.conn.execute(
            """
            SELECT link, source_name, title, resolution, size, duration, target_message_id, status, error, elapsed_sec, created_at
            FROM link_uploads
            ORDER BY id DESC
            LIMIT 10
            """
        ) as cur:
            recent = await cur.fetchall()

        return {
            'total': int(totals['total'] or 0),
            'ok': int(totals['ok'] or 0),
            'errors': int(totals['errors'] or 0),
            'bytes_uploaded': int(totals['bytes_uploaded'] or 0),
            'duration_sec': int(totals['duration_sec'] or 0),
            'recent': [dict(row) for row in recent],
        }
