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
