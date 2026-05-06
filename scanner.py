from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional

from telethon import TelegramClient
from telethon.errors import FloodWaitError, RPCError
from telethon.tl.custom.dialog import Dialog
from telethon.tl.types import DocumentAttributeVideo, Message

from db import DB
from watchdog import Heartbeat

log = logging.getLogger('scanner')
MB = 1024 * 1024
ProgressCB = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class ScanOptions:
    mode: str
    chat_ids: Optional[set[int]]
    order: str


def _since_dt(mode: str) -> Optional[datetime]:
    now = datetime.now(timezone.utc)
    if mode == 'week':
        return now - timedelta(days=7)
    if mode == 'month':
        return now - timedelta(days=30)
    return None


def _extract_video_meta(msg: Message) -> tuple[int, int, int, int] | None:
    if not msg or not msg.media or not getattr(msg.media, 'document', None):
        return None
    doc = msg.media.document
    if not getattr(doc, 'attributes', None):
        return None

    w = h = duration = None
    for attr in doc.attributes:
        if isinstance(attr, DocumentAttributeVideo):
            w = getattr(attr, 'w', None)
            h = getattr(attr, 'h', None)
            duration = getattr(attr, 'duration', None)
            break

    if w is None or h is None or duration is None:
        return None

    size = int(getattr(doc, 'size', 0) or 0)
    return int(w), int(h), int(duration), size


def _matches_rules(w: int, h: int, duration: int, size: int) -> bool:
    if h <= w:
        return False
    if duration < 180:
        return False
    if w < 900:
        return False
    min_size = (duration / 60.0) * (10 * MB)
    return size >= min_size


def _limit_or_none(n: int) -> int | None:
    return None if n <= 0 else n


class Scanner:
    def __init__(
        self,
        client: TelegramClient,
        db: DB,
        heartbeat: Heartbeat,
        excluded_chat_ids: set[int],
        target_bot_username: str,
        max_all: int,
        max_period: int,
        forward_delay_sec: float,
        forward_jitter_sec: float,
        dialog_delay_sec: float,
        max_flood_wait_sec: int,
        dry_run_delete: bool,
    ):
        self.client = client
        self.db = db
        self.heartbeat = heartbeat
        self.excluded = excluded_chat_ids
        self.target_bot_username = target_bot_username
        self.max_all = max_all
        self.max_period = max_period
        self.forward_delay_sec = max(0.3, forward_delay_sec)
        self.forward_jitter_sec = max(0.0, forward_jitter_sec)
        self.dialog_delay_sec = max(0.0, dialog_delay_sec)
        self.max_flood_wait_sec = max_flood_wait_sec
        self.dry_run_delete = dry_run_delete
        self._delete_lock = asyncio.Lock()

    async def list_dialogs(self) -> list[dict[str, Any]]:
        dialogs: list[dict[str, Any]] = []
        async for d in self.client.iter_dialogs(limit=10000, ignore_migrated=True):
            if d.id in self.excluded:
                continue
            link = None
            username = getattr(d.entity, 'username', None)
            if username:
                link = f'https://t.me/{username}'
            dialogs.append(
                {
                    'id': int(d.id),
                    'name': d.name or str(d.id),
                    'is_user': bool(d.is_user),
                    'is_group': bool(d.is_group),
                    'is_channel': bool(d.is_channel),
                    'link': link,
                    'username': username,
                }
            )
        return dialogs

    async def delete_dialog_by_id(self, chat_id: int) -> tuple[bool, str]:
        async with self._delete_lock:
            try:
                entity = await self.client.get_entity(chat_id)
                if self.dry_run_delete:
                    return True, 'DRY_RUN_DELETE=1, удаление не выполнялось.'
                await self.client.delete_dialog(entity)
                self.heartbeat.beat(status='delete_dialog', chat_id=chat_id)
                await asyncio.sleep(1.5)
                return True, 'Диалог удалён/чат покинут.'
            except FloodWaitError as e:
                seconds = int(getattr(e, 'seconds', 1))
                if seconds > self.max_flood_wait_sec:
                    return False, f'FloodWait {seconds} сек — слишком долго, удаление отменено.'
                await asyncio.sleep(seconds + 1)
                return False, f'FloodWait {seconds} сек — попробуй ещё раз позже.'
            except RPCError as e:
                return False, f'RPC ошибка: {e.__class__.__name__}'
            except Exception as e:
                log.exception('delete_dialog_by_id failed chat_id=%s', chat_id)
                return False, f'Ошибка: {e.__class__.__name__}'

    async def scan(
        self,
        opts: ScanOptions,
        cancel_event: asyncio.Event,
        progress_cb: Optional[ProgressCB] = None,
    ) -> dict[str, Any]:
        start_ts = time.time()
        target = await self.client.get_entity(self.target_bot_username)

        dialogs: list[Dialog] = []
        async for d in self.client.iter_dialogs(limit=10000, ignore_migrated=True):
            if d.id in self.excluded:
                continue
            if opts.chat_ids is not None and d.id not in opts.chat_ids:
                continue
            dialogs.append(d)

        since = _since_dt(opts.mode)
        total_checked = 0
        total_matched = 0
        total_forwarded = 0
        total_errors = 0
        total_skipped = 0
        cancelled = False
        empty_chats: list[dict[str, Any]] = []

        if progress_cb:
            await progress_cb({'type': 'init', 'dialogs_total': len(dialogs), 'mode': opts.mode, 'order': opts.order})

        log.info('Scan init: dialogs=%d mode=%s order=%s', len(dialogs), opts.mode, opts.order)
        self.heartbeat.beat(status='scan_start', dialogs_total=len(dialogs), mode=opts.mode, order=opts.order)

        for idx, dialog in enumerate(dialogs, start=1):
            chat_id = int(dialog.id)
            if cancel_event.is_set():
                cancelled = True
                break

            dialog_name = dialog.name or str(chat_id)
            per_limit = self.max_all if opts.mode == 'all' else self.max_period
            limit = _limit_or_none(per_limit)
            min_id = 0
            reverse = opts.order == 'old_to_new'
            max_id_seen = 0
            checked_in_chat = 0
            matched_in_chat = 0
            forwarded_in_chat = 0
            hard_stopped_by_date = False

            if opts.mode == 'new':
                min_id = await self.db.get_last_scanned(chat_id)
                max_id_seen = min_id

            if progress_cb:
                await progress_cb({
                    'type': 'chat_start',
                    'chat_index': idx,
                    'dialogs_total': len(dialogs),
                    'chat_id': chat_id,
                    'chat_name': dialog_name,
                })

            try:
                async for msg in self.client.iter_messages(chat_id, limit=limit, min_id=min_id, reverse=reverse):
                    self.heartbeat.beat(
                        status='scan_message',
                        chat_id=chat_id,
                        chat_name=dialog_name,
                        chat_index=idx,
                        dialogs_total=len(dialogs),
                        checked=total_checked,
                        matched=total_matched,
                        forwarded=total_forwarded,
                    )

                    if cancel_event.is_set():
                        cancelled = True
                        break

                    if not msg:
                        continue

                    checked_in_chat += 1
                    total_checked += 1

                    if msg.id and msg.id > max_id_seen:
                        max_id_seen = msg.id

                    if since and msg.date:
                        msg_dt = msg.date.replace(tzinfo=timezone.utc)
                        if msg_dt < since and not reverse:
                            hard_stopped_by_date = True
                            break
                        if msg_dt < since and reverse:
                            continue

                    meta = _extract_video_meta(msg)
                    if not meta:
                        if progress_cb and checked_in_chat % 1000 == 0:
                            await progress_cb({'type': 'tick', 'chat_id': chat_id, 'checked': total_checked, 'matched': total_matched, 'forwarded': total_forwarded})
                        continue

                    w, h, duration, size = meta
                    if not _matches_rules(w, h, duration, size):
                        continue

                    matched_in_chat += 1
                    total_matched += 1

                    if await self.db.was_forwarded(chat_id, msg.id):
                        total_skipped += 1
                        continue

                    try:
                        await self.client.forward_messages(target, msg)
                    except FloodWaitError as e:
                        seconds = int(getattr(e, 'seconds', 1))
                        if progress_cb:
                            await progress_cb({'type': 'floodwait', 'chat_id': chat_id, 'seconds': seconds})
                        if seconds > self.max_flood_wait_sec:
                            total_errors += 1
                            log.warning('FloodWait too long: %ss chat_id=%s msg_id=%s', seconds, chat_id, msg.id)
                            cancelled = True
                            cancel_event.set()
                            break
                        await asyncio.sleep(seconds + 1)
                        await self.client.forward_messages(target, msg)

                    await self.db.mark_forwarded(chat_id, msg.id, int(time.time()))
                    forwarded_in_chat += 1
                    total_forwarded += 1

                    log.info(
                        'Forwarded: chat_id=%s msg_id=%s duration=%ss w=%s h=%s size=%s',
                        chat_id,
                        msg.id,
                        duration,
                        w,
                        h,
                        size,
                    )

                    if progress_cb:
                        await progress_cb({
                            'type': 'forward',
                            'chat_id': chat_id,
                            'chat_name': dialog_name,
                            'msg_id': msg.id,
                            'checked': total_checked,
                            'matched': total_matched,
                            'forwarded': total_forwarded,
                        })

                    delay = self.forward_delay_sec + random.uniform(0, self.forward_jitter_sec)
                    await asyncio.sleep(delay)

            except FloodWaitError as e:
                total_errors += 1
                wait_s = int(getattr(e, 'seconds', 1))
                log.warning('FloodWait %ss on chat_id=%s', wait_s, chat_id)
                if progress_cb:
                    await progress_cb({'type': 'floodwait', 'chat_id': chat_id, 'seconds': wait_s})
                if wait_s > self.max_flood_wait_sec:
                    cancelled = True
                    cancel_event.set()
                    break
                await asyncio.sleep(wait_s + 1)
            except RPCError:
                total_errors += 1
                log.exception('RPC error on chat_id=%s', chat_id)
                if progress_cb:
                    await progress_cb({'type': 'error', 'chat_id': chat_id, 'error': 'rpc_error'})
            except Exception:
                total_errors += 1
                log.exception('Unexpected error on chat_id=%s', chat_id)
                if progress_cb:
                    await progress_cb({'type': 'error', 'chat_id': chat_id, 'error': 'exception'})
            finally:
                if opts.mode == 'new' and max_id_seen > min_id:
                    await self.db.set_last_scanned(chat_id, max_id_seen, int(time.time()))

            if checked_in_chat > 0 and matched_in_chat == 0:
                username = getattr(dialog.entity, 'username', None)
                link = f'https://t.me/{username}' if username else None
                empty_chats.append(
                    {
                        'id': chat_id,
                        'name': dialog_name,
                        'link': link,
                        'username': username,
                        'checked': checked_in_chat,
                        'matched': matched_in_chat,
                        'forwarded': forwarded_in_chat,
                        'by_date_stop': hard_stopped_by_date,
                        'is_group': bool(dialog.is_group),
                        'is_channel': bool(dialog.is_channel),
                    }
                )

            if progress_cb:
                await progress_cb({
                    'type': 'chat_done',
                    'chat_id': chat_id,
                    'chat_name': dialog_name,
                    'chat_index': idx,
                    'dialogs_total': len(dialogs),
                    'checked': total_checked,
                    'matched': total_matched,
                    'forwarded': total_forwarded,
                })

            await asyncio.sleep(self.dialog_delay_sec)
            if cancelled:
                break

        elapsed = int(time.time() - start_ts)
        result = {
            'dialogs': len(dialogs),
            'checked': total_checked,
            'matched': total_matched,
            'forwarded': total_forwarded,
            'skipped_already_forwarded': total_skipped,
            'errors': total_errors,
            'empty_chats_count': len(empty_chats),
            'empty_chats': empty_chats,
            'cancelled': cancelled,
            'elapsed_sec': elapsed,
        }

        log.info('Scan finish: %s', result)
        self.heartbeat.beat(status='scan_done', result_summary={k: result[k] for k in ('dialogs', 'checked', 'matched', 'forwarded', 'errors', 'empty_chats_count', 'cancelled', 'elapsed_sec')})
        if progress_cb:
            await progress_cb({'type': 'done', **result})
        return result
