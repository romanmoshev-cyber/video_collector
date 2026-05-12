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
from telethon.tl.types import DocumentAttributeVideo, Message, MessageMediaDocument

from db import DB
from watchdog import Heartbeat

log = logging.getLogger('scanner')
ProgressCB = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class ScanOptions:
    mode: str
    chat_ids: Optional[set[int]]
    order: str


@dataclass(frozen=True)
class VideoFilter:
    vertical_only: bool = True
    exact_width: int = 0
    exact_height: int = 0
    min_width: int = 900
    min_height: int = 0
    max_width: int = 0
    max_height: int = 0
    min_duration_sec: float = 180.0
    max_duration_sec: float = 0.0
    min_size_mb: float = 0.0
    max_size_mb: float = 0.0
    min_size_mb_per_minute: float = 10.0


def _since_dt(mode: str) -> Optional[datetime]:
    now = datetime.now(timezone.utc)
    if mode == 'day':
        return now - timedelta(days=1)
    if mode == 'week':
        return now - timedelta(days=7)
    if mode == 'month':
        return now - timedelta(days=30)
    return None


SKIPPED_DIALOG_NAMES = {'избранное', 'saved messages'}
SKIPPED_DIALOG_NAME_PARTS = {'vertical'}


def _dialog_name(dialog: Dialog) -> str:
    return (dialog.name or '').strip()


def _dialog_type_label(dialog: Dialog) -> str:
    return 'группа' if dialog.is_group else 'канал' if dialog.is_channel else 'чат'


def _should_scan_dialog(dialog: Dialog, target_bot_username: str = '') -> bool:
    if not (dialog.is_group or dialog.is_channel):
        return False
    if getattr(dialog.entity, 'self', False):
        return False
    username = (getattr(dialog.entity, 'username', None) or '').casefold().lstrip('@')
    if target_bot_username and username == target_bot_username.casefold().lstrip('@'):
        return False
    name = _dialog_name(dialog).casefold()
    if name in SKIPPED_DIALOG_NAMES:
        return False
    return not any(part in name for part in SKIPPED_DIALOG_NAME_PARTS)


EXCLUDED_CHATS_KEY = 'excluded_chats:manual'
CONNECTION_RECOVERY_ATTEMPTS = 3
CONNECTION_RECOVERY_BASE_DELAY_SEC = 2.0


def _is_connection_error(error: BaseException) -> bool:
    return isinstance(error, (ConnectionError, OSError))


class Scanner:
    def __init__(
        self,
        client: TelegramClient,
        db: DB,
        heartbeat: Heartbeat,
        excluded_chat_ids: set[int],
        target_bot_username: str,
        forward_delay_sec: float,
        forward_jitter_sec: float,
        dialog_delay_sec: float,
        max_flood_wait_sec: int,
        video_filter: VideoFilter | None = None,
    ):
        self.client = client
        self.db = db
        self.heartbeat = heartbeat
        self.excluded = set(excluded_chat_ids)
        self.target_bot_username = target_bot_username
        self.forward_delay_sec = max(0.1, forward_delay_sec)
        self.forward_jitter_sec = max(0.0, forward_jitter_sec)
        self.dialog_delay_sec = max(0.0, dialog_delay_sec)
        self.max_flood_wait_sec = max_flood_wait_sec
        self.video_filter = video_filter or VideoFilter()

    @staticmethod
    def _video_document(msg: Message) -> Any | None:
        media = getattr(msg, 'media', None)
        document = getattr(media, 'document', None) if isinstance(media, MessageMediaDocument) else getattr(msg, 'document', None)
        if not document:
            return None
        if getattr(msg, 'video', None):
            return document
        mime_type = (getattr(document, 'mime_type', '') or '').casefold()
        return document if mime_type.startswith('video/') else None

    @staticmethod
    def _video_dimensions_and_duration(document: Any) -> tuple[int, int, float]:
        for attr in getattr(document, 'attributes', []) or []:
            if isinstance(attr, DocumentAttributeVideo):
                width = int(getattr(attr, 'w', 0) or 0)
                height = int(getattr(attr, 'h', 0) or 0)
                duration = float(getattr(attr, 'duration', 0) or 0)
                return width, height, duration
        return 0, 0, 0.0

    def _is_video_message(self, msg: Message) -> bool:
        document = self._video_document(msg)
        if not document:
            return False

        width, height, duration = self._video_dimensions_and_duration(document)
        size = int(getattr(document, 'size', 0) or 0)
        rules = self.video_filter

        if rules.vertical_only and (not width or not height or height <= width):
            return False
        if rules.exact_width > 0 and width != rules.exact_width:
            return False
        if rules.exact_height > 0 and height != rules.exact_height:
            return False
        if rules.min_width > 0 and width < rules.min_width:
            return False
        if rules.min_height > 0 and height < rules.min_height:
            return False
        if rules.max_width > 0 and width > rules.max_width:
            return False
        if rules.max_height > 0 and height > rules.max_height:
            return False
        if rules.min_duration_sec > 0 and duration < rules.min_duration_sec:
            return False
        if rules.max_duration_sec > 0 and duration > rules.max_duration_sec:
            return False
        if rules.min_size_mb > 0 and size < int(rules.min_size_mb * 1024 * 1024):
            return False
        if rules.max_size_mb > 0 and size > int(rules.max_size_mb * 1024 * 1024):
            return False
        if rules.min_size_mb_per_minute > 0:
            if duration <= 0:
                return False
            size_mb_per_minute = (size / 1024 / 1024) / (duration / 60)
            if size_mb_per_minute < rules.min_size_mb_per_minute:
                return False
        return True

    async def _ensure_connected(self) -> None:
        if self.client.is_connected():
            return
        log.warning('Telethon client is disconnected, reconnecting...')
        await self.client.connect()

    async def _recover_connection(self, context: str) -> bool:
        for attempt in range(1, CONNECTION_RECOVERY_ATTEMPTS + 1):
            try:
                await self._ensure_connected()
                await self.client.get_me()
                log.info('Telethon connection recovered after %s (attempt %s/%s)', context, attempt, CONNECTION_RECOVERY_ATTEMPTS)
                return True
            except Exception as e:
                delay = CONNECTION_RECOVERY_BASE_DELAY_SEC * attempt
                log.warning('Telethon reconnect failed after %s (attempt %s/%s): %s', context, attempt, CONNECTION_RECOVERY_ATTEMPTS, e.__class__.__name__)
                if attempt < CONNECTION_RECOVERY_ATTEMPTS:
                    await asyncio.sleep(delay)
        return False

    async def _run_with_connection_recovery(self, context: str, func: Callable[[], Awaitable[Any]]) -> Any:
        try:
            await self._ensure_connected()
            return await func()
        except Exception as e:
            if not _is_connection_error(e):
                raise
            log.warning('Telethon connection error during %s: %s', context, e.__class__.__name__)
            if not await self._recover_connection(context):
                raise
            return await func()

    async def _load_manual_excluded(self) -> list[dict[str, Any]]:
        items = await self.db.kv_get_json(EXCLUDED_CHATS_KEY, [])
        for item in items:
            self.excluded.add(int(item.get('id', 0)))
        return items

    async def exclude_chat(self, chat_id: int, name: str = '') -> None:
        items = await self._load_manual_excluded()
        chat_id = int(chat_id)
        self.excluded.add(chat_id)
        saved = [item for item in items if int(item.get('id', 0)) != chat_id]
        saved.append({'id': chat_id, 'name': name or str(chat_id), 'excluded_at': int(time.time())})
        await self.db.kv_set_json(EXCLUDED_CHATS_KEY, saved)

    async def _collect_dialogs(self, opts: ScanOptions | None = None) -> list[Dialog]:
        await self._load_manual_excluded()
        dialogs: list[Dialog] = []
        async for dialog in self.client.iter_dialogs(limit=None, ignore_migrated=True):
            if int(dialog.id) in self.excluded:
                continue
            if not _should_scan_dialog(dialog, self.target_bot_username):
                continue
            if opts and opts.chat_ids is not None and int(dialog.id) not in opts.chat_ids:
                continue
            dialogs.append(dialog)
        return dialogs

    async def list_dialogs(self) -> list[dict[str, Any]]:
        scan_dialogs = await self._run_with_connection_recovery('list_dialogs', lambda: self._collect_dialogs())
        items: list[dict[str, Any]] = []
        for dialog in scan_dialogs:
            username = getattr(dialog.entity, 'username', None)
            items.append({
                'id': int(dialog.id),
                'name': dialog.name or str(dialog.id),
                'type': _dialog_type_label(dialog),
                'is_group': bool(dialog.is_group),
                'is_channel': bool(dialog.is_channel),
                'link': f'https://t.me/{username}' if username else None,
                'username': username,
            })
        return items


    async def list_dialog_video_stats(
        self,
        mode: str = 'all',
        chat_ids: Optional[set[int]] = None,
        progress_cb: Optional[ProgressCB] = None,
    ) -> list[dict[str, Any]]:
        opts = ScanOptions(mode=mode, order='new_to_old', chat_ids=chat_ids)
        dialogs = await self._run_with_connection_recovery('stats_collect_dialogs', lambda: self._collect_dialogs(opts))
        since = _since_dt(mode)
        result: list[dict[str, Any]] = []

        if progress_cb:
            await progress_cb({'type': 'stats_init', 'dialogs_total': len(dialogs), 'mode': mode})

        for idx, dialog in enumerate(dialogs, start=1):
            chat_id = int(dialog.id)
            dialog_name = dialog.name or str(chat_id)
            videos_total = 0
            videos_matched = 0

            if progress_cb:
                await progress_cb({'type': 'stats_chat_start', 'chat_index': idx, 'dialogs_total': len(dialogs), 'chat_id': chat_id, 'chat_name': dialog_name})

            async for msg in self.client.iter_messages(chat_id, limit=None):
                if not isinstance(msg, Message) or not msg.id:
                    continue
                if since and msg.date:
                    msg_dt = msg.date if msg.date.tzinfo else msg.date.replace(tzinfo=timezone.utc)
                    msg_dt = msg_dt.astimezone(timezone.utc)
                    if msg_dt < since:
                        break
                if not self._is_video_message(msg):
                    continue

                videos_total += 1
                if not await self.db.was_forwarded(chat_id, msg.id):
                    videos_matched += 1

            item = {
                'id': chat_id,
                'name': dialog_name,
                'type': _dialog_type_label(dialog),
                'is_group': bool(dialog.is_group),
                'is_channel': bool(dialog.is_channel),
                'videos_total': videos_total,
                'videos_matched': videos_matched,
            }
            result.append(item)

            if progress_cb:
                await progress_cb({'type': 'stats_chat_done', 'chat_index': idx, 'dialogs_total': len(dialogs), **item})

        return result

    async def scan(
        self,
        opts: ScanOptions,
        cancel_event: asyncio.Event,
        progress_cb: Optional[ProgressCB] = None,
    ) -> dict[str, Any]:
        start_ts = time.time()
        target = await self._run_with_connection_recovery(
            f'get_entity:{self.target_bot_username}',
            lambda: self.client.get_entity(self.target_bot_username),
        )
        dialogs = await self._run_with_connection_recovery('collect_dialogs', lambda: self._collect_dialogs(opts))
        since = _since_dt(opts.mode)

        total_checked = 0
        total_forwarded = 0
        total_skipped = 0
        total_errors = 0
        cancelled = False

        if progress_cb:
            await progress_cb({'type': 'init', 'dialogs_total': len(dialogs), 'mode': opts.mode, 'order': opts.order})

        log.info('Forwarding init: dialogs=%d mode=%s order=%s', len(dialogs), opts.mode, opts.order)
        self.heartbeat.beat(status='forward_start', dialogs_total=len(dialogs), mode=opts.mode, order=opts.order)

        for idx, dialog in enumerate(dialogs, start=1):
            chat_id = int(dialog.id)
            if cancel_event.is_set():
                cancelled = True
                break

            dialog_name = dialog.name or str(chat_id)
            min_id = await self.db.get_last_scanned(chat_id) if opts.mode == 'new' else 0
            max_id_seen = min_id
            reverse = opts.order == 'old_to_new'
            chat_finished_cleanly = False

            if progress_cb:
                await progress_cb({'type': 'chat_start', 'chat_index': idx, 'dialogs_total': len(dialogs), 'chat_id': chat_id, 'chat_name': dialog_name})

            try:
                last_heartbeat_at = 0.0
                async for msg in self.client.iter_messages(chat_id, limit=None, min_id=min_id, reverse=reverse):
                    if cancel_event.is_set():
                        cancelled = True
                        break
                    if not isinstance(msg, Message) or not msg.id:
                        continue

                    total_checked += 1
                    max_id_seen = max(max_id_seen, int(msg.id))

                    now_monotonic = time.monotonic()
                    if now_monotonic - last_heartbeat_at >= 5.0:
                        last_heartbeat_at = now_monotonic
                        self.heartbeat.beat(status='forward_message', chat_id=chat_id, chat_name=dialog_name, chat_index=idx, dialogs_total=len(dialogs), checked=total_checked, forwarded=total_forwarded)

                    if since and msg.date:
                        msg_dt = msg.date if msg.date.tzinfo else msg.date.replace(tzinfo=timezone.utc)
                        msg_dt = msg_dt.astimezone(timezone.utc)
                        if msg_dt < since and not reverse:
                            break
                        if msg_dt < since and reverse:
                            continue

                    if not self._is_video_message(msg):
                        continue

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
                            cancelled = True
                            cancel_event.set()
                            break
                        await asyncio.sleep(seconds + 1)
                        await self.client.forward_messages(target, msg)
                    except RPCError as e:
                        total_errors += 1
                        log.warning('Skip message due RPC error: chat_id=%s msg_id=%s error=%s', chat_id, msg.id, e.__class__.__name__)
                        continue
                    except Exception as e:
                        if not _is_connection_error(e):
                            raise
                        if not await self._recover_connection(f'forward chat_id={chat_id} msg_id={msg.id}'):
                            raise
                        await self.client.forward_messages(target, msg)

                    await self.db.mark_forwarded(chat_id, msg.id, int(time.time()))
                    total_forwarded += 1

                    if progress_cb:
                        await progress_cb({'type': 'forward', 'chat_id': chat_id, 'chat_name': dialog_name, 'msg_id': msg.id, 'checked': total_checked, 'forwarded': total_forwarded})

                    await asyncio.sleep(self.forward_delay_sec + random.uniform(0, self.forward_jitter_sec))

                chat_finished_cleanly = not cancelled

            except FloodWaitError as e:
                total_errors += 1
                wait_s = int(getattr(e, 'seconds', 1))
                if wait_s > self.max_flood_wait_sec:
                    cancelled = True
                    cancel_event.set()
                    break
                await asyncio.sleep(wait_s + 1)
            except RPCError:
                total_errors += 1
                log.exception('RPC error on chat_id=%s', chat_id)
            except Exception as e:
                total_errors += 1
                if _is_connection_error(e):
                    log.warning('Connection error on chat_id=%s: %s', chat_id, e.__class__.__name__)
                    if not await self._recover_connection(f'chat_id={chat_id}'):
                        cancelled = True
                        cancel_event.set()
                        break
                else:
                    log.exception('Unexpected error on chat_id=%s', chat_id)
            finally:
                if opts.mode == 'new' and chat_finished_cleanly and max_id_seen > min_id:
                    await self.db.set_last_scanned(chat_id, max_id_seen, int(time.time()))

            if progress_cb:
                await progress_cb({'type': 'chat_done', 'chat_id': chat_id, 'chat_name': dialog_name, 'chat_index': idx, 'dialogs_total': len(dialogs), 'checked': total_checked, 'forwarded': total_forwarded})

            await asyncio.sleep(self.dialog_delay_sec)

        elapsed = int(time.time() - start_ts)
        result = {
            'dialogs': len(dialogs),
            'checked': total_checked,
            'forwarded': total_forwarded,
            'skipped_already_forwarded': total_skipped,
            'errors': total_errors,
            'cancelled': cancelled,
            'elapsed_sec': elapsed,
        }
        log.info('Forwarding finish: %s', result)
        self.heartbeat.beat(status='forward_done', result_summary=result)
        if progress_cb:
            await progress_cb({'type': 'done', **result})
        return result
