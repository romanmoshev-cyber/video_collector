from __future__ import annotations

import asyncio
import hashlib
import importlib
import importlib.util
import logging
import random
import re
import shutil
import subprocess
import sys
import time
from urllib.parse import urlparse
from dataclasses import dataclass
from pathlib import Path
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
MAX_TELEGRAM_THUMB_SIZE = 200 * 1024
STALE_DOWNLOAD_DIR_AGE_SEC = 6 * 60 * 60
ProgressCB = Callable[[dict[str, Any]], Awaitable[None]]


def _format_bytes(value: Any) -> str:
    try:
        size = float(value or 0)
    except (TypeError, ValueError):
        return '0 Б'
    units = ('Б', 'КБ', 'МБ', 'ГБ')
    idx = 0
    while size >= 1024 and idx < len(units) - 1:
        size /= 1024
        idx += 1
    if idx == 0:
        return f'{int(size)} {units[idx]}'
    return f'{size:.1f} {units[idx]}'


def _video_meta_summary(width: Any, height: Any, duration: Any, size: Any) -> dict[str, Any]:
    try:
        w = int(width or 0)
    except (TypeError, ValueError):
        w = 0
    try:
        h = int(height or 0)
    except (TypeError, ValueError):
        h = 0
    try:
        d = int(float(duration or 0))
    except (TypeError, ValueError):
        d = 0
    try:
        file_size = int(size or 0)
    except (TypeError, ValueError):
        file_size = 0
    return {
        'width': w,
        'height': h,
        'resolution': f'{w}×{h}' if w and h else 'неизвестно',
        'duration': d,
        'size': file_size,
        'size_human': _format_bytes(file_size),
    }


def _video_meta_summary_from_info(info: dict[str, Any], file_path: Path) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = [info]
    for key in ('requested_downloads', 'requested_formats', 'formats'):
        values = info.get(key)
        if isinstance(values, list):
            candidates.extend(item for item in values if isinstance(item, dict))

    width = height = None
    for item in candidates:
        width = item.get('width')
        height = item.get('height')
        if width and height:
            break
    return _video_meta_summary(width, height, info.get('duration'), file_path.stat().st_size)


def _make_transfer_progress_callback(
    progress_cb: Optional[ProgressCB],
    event_type: str,
    base_event: dict[str, Any],
    *,
    min_interval_sec: float = 1.5,
):
    if progress_cb is None:
        return None

    loop = asyncio.get_running_loop()
    last_emit = 0.0

    def on_progress(current: int, total: int) -> None:
        nonlocal last_emit
        now = time.monotonic()
        is_done = bool(total and current >= total)
        if not is_done and now - last_emit < min_interval_sec:
            return
        last_emit = now
        percent = round((current / total) * 100, 1) if total else 0.0
        event = {
            **base_event,
            'type': event_type,
            'current': int(current or 0),
            'total': int(total or 0),
            'percent': percent,
            'current_human': _format_bytes(current),
            'total_human': _format_bytes(total),
        }
        loop.create_task(progress_cb(event))

    return on_progress


@dataclass
class ScanOptions:
    mode: str
    chat_ids: Optional[set[int]]
    order: str


def _since_dt(mode: str) -> Optional[datetime]:
    now = datetime.now(timezone.utc)
    if mode == 'day':
        return now - timedelta(days=1)
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


def _reject_reason(w: int, h: int, duration: int, size: int) -> str | None:
    if h <= w:
        return 'not_vertical'
    if duration < 180:
        return 'too_short'
    if w < 900:
        return 'too_narrow'
    min_size = (duration / 60.0) * (10 * MB)
    if size < min_size:
        return 'too_small'
    return None


def _matches_rules(w: int, h: int, duration: int, size: int) -> bool:
    return _reject_reason(w, h, duration, size) is None


def _iter_messages_kwargs(opts: ScanOptions, since: Optional[datetime], min_id: int) -> dict[str, Any]:
    reverse = opts.order == 'old_to_new'
    kwargs: dict[str, Any] = {
        'limit': None,
        'min_id': min_id,
        'reverse': reverse,
    }
    # Telethon reverses offset_date semantics together with reverse=True:
    # in normal order it returns messages older than the date, but in
    # oldest-to-newest order it returns messages newer than the date. Without
    # this bound, period scans in old_to_new mode walk the entire chat history
    # before reaching the requested day/week/month window.
    if since is not None and reverse:
        kwargs['offset_date'] = since
    return kwargs


def _period_scan_action(msg_dt: datetime, since: Optional[datetime], reverse: bool) -> str:
    if since is None:
        return 'keep'
    msg_dt = msg_dt if msg_dt.tzinfo else msg_dt.replace(tzinfo=timezone.utc)
    msg_dt = msg_dt.astimezone(timezone.utc)
    if msg_dt >= since:
        return 'keep'
    return 'skip' if reverse else 'stop'


def _video_attributes_from_values(width: Any, height: Any, duration: Any) -> list[DocumentAttributeVideo] | None:
    try:
        w = int(width or 0)
        h = int(height or 0)
        d = float(duration or 0)
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0 or d <= 0:
        return None
    return [DocumentAttributeVideo(duration=d, w=w, h=h, supports_streaming=True)]


def _video_attributes_from_info(info: dict[str, Any]) -> list[DocumentAttributeVideo] | None:
    candidates: list[dict[str, Any]] = [info]
    for key in ('requested_downloads', 'requested_formats'):
        values = info.get(key)
        if isinstance(values, list):
            candidates.extend(item for item in values if isinstance(item, dict))
    for item in candidates:
        attrs = _video_attributes_from_values(item.get('width'), item.get('height'), item.get('duration') or info.get('duration'))
        if attrs:
            return attrs
    return None


def _find_ffmpeg() -> str | None:
    system_ffmpeg = shutil.which('ffmpeg')
    if system_ffmpeg:
        return system_ffmpeg

    if importlib.util.find_spec('imageio_ffmpeg') is None:
        return None

    imageio_ffmpeg = importlib.import_module('imageio_ffmpeg')
    bundled_ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    return str(bundled_ffmpeg) if bundled_ffmpeg else None


def _generate_video_thumbnail(video_path: Path, work_dir: Path, source_id: str, duration: Any = None) -> Path | None:
    ffmpeg_path = _find_ffmpeg()
    if not ffmpeg_path:
        log.debug('Cannot generate video thumbnail without ffmpeg')
        return None

    try:
        duration_sec = float(duration or 0)
    except (TypeError, ValueError):
        duration_sec = 0
    if duration_sec:
        seek_points = [max(0.0, min(duration_sec - 0.1, duration_sec * part)) for part in (0.2, 0.35, 0.5, 0.7)]
    else:
        seek_points = [1.0, 3.0, 5.0]

    thumb_path = work_dir / f'{source_id}_thumb.jpg'
    max_size = MAX_TELEGRAM_THUMB_SIZE
    last_size = 0
    for seek_sec in seek_points:
        for side in (320, 288, 256, 224, 192, 160, 128, 96):
            for quality in (8, 12, 16, 20, 24, 28, 31):
                cmd = [
                    ffmpeg_path,
                    '-y',
                    '-ss',
                    f'{seek_sec:.3f}',
                    '-i',
                    str(video_path),
                    '-map',
                    '0:v:0',
                    '-frames:v',
                    '1',
                    '-vf',
                    f'scale={side}:{side}:force_original_aspect_ratio=decrease:force_divisible_by=2,format=yuvj420p',
                    '-q:v',
                    str(quality),
                    str(thumb_path),
                ]
                completed = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
                if completed.returncode != 0 or not thumb_path.exists() or thumb_path.stat().st_size <= 0:
                    continue
                last_size = thumb_path.stat().st_size
                if last_size <= max_size:
                    log.debug('Generated thumbnail %s (%s bytes, seek %.3fs, side %s)', thumb_path, last_size, seek_sec, side)
                    return thumb_path

    if thumb_path.exists() and thumb_path.stat().st_size > 0:
        log.warning(
            'Generated thumbnail is too large for Telegram custom cover: %s bytes (limit %s bytes)',
            last_size or thumb_path.stat().st_size,
            max_size,
        )
    return None


def _is_valid_thumbnail(path: Path | None) -> bool:
    return bool(path and path.exists() and 0 < path.stat().st_size <= MAX_TELEGRAM_THUMB_SIZE)


async def _download_message_thumbnail(client: TelegramClient, msg: Message, work_dir: Path, source_id: str) -> Path | None:
    """Download the original Telegram video cover when it has a static thumbnail."""
    document = getattr(getattr(msg, 'media', None), 'document', None)
    thumbs = list(getattr(document, 'thumbs', None) or [])
    if not thumbs:
        return None

    static_thumbs = [thumb for thumb in thumbs if thumb.__class__.__name__ != 'VideoSize']
    if not static_thumbs:
        return None

    def thumb_score(thumb: Any) -> int:
        size = getattr(thumb, 'size', None)
        if size:
            return int(size)
        return int(getattr(thumb, 'w', 0) or 0) * int(getattr(thumb, 'h', 0) or 0)

    for thumb in sorted(static_thumbs, key=thumb_score, reverse=True):
        thumb_path = work_dir / f'{source_id}_original_thumb.jpg'
        try:
            downloaded = await client.download_media(msg, file=str(thumb_path), thumb=thumb)
        except Exception as e:
            log.debug('Failed to download original thumbnail for msg_id=%s: %s', getattr(msg, 'id', None), e.__class__.__name__)
            continue
        if not downloaded:
            continue
        downloaded_path = Path(downloaded)
        if _is_valid_thumbnail(downloaded_path):
            log.debug('Using original Telegram thumbnail %s (%s bytes)', downloaded_path, downloaded_path.stat().st_size)
            return downloaded_path
        if downloaded_path.exists():
            log.debug('Skipping original thumbnail %s: size=%s bytes', downloaded_path, downloaded_path.stat().st_size)
            downloaded_path.unlink(missing_ok=True)
    return None


async def _prepare_video_thumbnail(
    client: TelegramClient,
    msg: Message | None,
    video_path: Path,
    work_dir: Path,
    source_id: str,
    duration: Any = None,
) -> Path | None:
    if msg is not None:
        original_thumb = await _download_message_thumbnail(client, msg, work_dir, source_id)
        if original_thumb:
            return original_thumb

    generated_thumb = await asyncio.to_thread(
        _generate_video_thumbnail,
        video_path,
        work_dir,
        source_id,
        duration,
    )
    return generated_thumb if _is_valid_thumbnail(generated_thumb) else None


def _sent_message_id(sent: Any) -> int | None:
    if isinstance(sent, list):
        sent = sent[0] if sent else None
    msg_id = getattr(sent, 'id', None)
    return int(msg_id) if msg_id else None


SKIPPED_DIALOG_NAMES = {'избранное', 'saved messages', 'подборки 18+'}
SKIPPED_DIALOG_PREFIXES = ('vertical',)


def _dialog_name(dialog: Dialog) -> str:
    return (dialog.name or '').strip()


def _should_scan_dialog(dialog: Dialog) -> bool:
    name = _dialog_name(dialog)
    normalized_name = name.casefold()

    if getattr(dialog.entity, 'self', False):
        return False
    if normalized_name in SKIPPED_DIALOG_NAMES:
        return False
    if normalized_name.startswith(SKIPPED_DIALOG_PREFIXES):
        return False
    return bool(dialog.is_group or dialog.is_channel)


EMPTY_CHATS_KEY = 'empty_chats:last_full_scan'
EXCLUDED_CHATS_KEY = 'excluded_chats:manual'
CONNECTION_RECOVERY_ATTEMPTS = 3
CONNECTION_RECOVERY_BASE_DELAY_SEC = 2.0
TELEGRAM_LINK_RE = re.compile(r'https?://(?:www\.)?t(?:elegram)?\.me/(?P<path>[^?\s]+)', re.IGNORECASE)


def _clean_url(url: str) -> str:
    return url.strip().rstrip(').,;')


def _ensure_ytdlp_available() -> None:
    if importlib.util.find_spec('yt_dlp') is not None:
        return

    app_dir = Path(__file__).resolve().parent
    requirements_file = app_dir / 'requirements.txt'
    install_target = ['-r', str(requirements_file)] if requirements_file.exists() else ['yt-dlp']
    command = [sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check', *install_target]
    log.warning('yt-dlp is missing in current Python; installing dependencies with: %s', ' '.join(command))
    result = subprocess.run(command, cwd=app_dir, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        details = (result.stderr or result.stdout or '').strip()
        raise RuntimeError(
            'Не установлен пакет yt-dlp в текущем Python, и автоматическая установка не удалась. '
            f'Команда: {" ".join(command)}. '
            f'Вывод: {details[-1200:]}'
        )
    importlib.invalidate_caches()
    if importlib.util.find_spec('yt_dlp') is None:
        raise RuntimeError(
            'yt-dlp был установлен pip без ошибки, но всё ещё не виден текущему Python. '
            f'Перезапусти сервис через ./run.sh или выполни: {" ".join(command)}'
        )


def _source_name_from_url(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.netloc or 'external').removeprefix('www.')
    return host or 'external'


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
        dry_run_delete: bool,
        download_dir: Path,
        ytdlp_cookies_file: Path | None,
        max_upload_size_mb: int,
        min_free_disk_mb: int,
    ):
        self.client = client
        self.db = db
        self.heartbeat = heartbeat
        self.excluded = excluded_chat_ids
        self.target_bot_username = target_bot_username
        self.forward_delay_sec = max(0.3, forward_delay_sec)
        self.forward_jitter_sec = max(0.0, forward_jitter_sec)
        self.dialog_delay_sec = max(0.0, dialog_delay_sec)
        self.max_flood_wait_sec = max_flood_wait_sec
        self.dry_run_delete = dry_run_delete
        self.download_dir = Path(download_dir)
        self.ytdlp_cookies_file = Path(ytdlp_cookies_file) if ytdlp_cookies_file else None
        self.max_upload_size = max(1, max_upload_size_mb) * MB
        self.min_free_disk = max(0, min_free_disk_mb) * MB
        self._delete_lock = asyncio.Lock()

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
                log.warning(
                    'Telethon reconnect failed after %s (attempt %s/%s): %s',
                    context,
                    attempt,
                    CONNECTION_RECOVERY_ATTEMPTS,
                    e.__class__.__name__,
                )
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

    def _cleanup_stale_downloads(self) -> int:
        if not self.download_dir.exists():
            return 0
        now = time.time()
        removed = 0
        for path in self.download_dir.iterdir():
            if not path.is_dir():
                continue
            try:
                age = now - path.stat().st_mtime
            except OSError:
                continue
            if age < STALE_DOWNLOAD_DIR_AGE_SEC:
                continue
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
        if removed:
            log.info('Removed %s stale download work dirs from %s', removed, self.download_dir)
        return removed

    def _ensure_download_dir_ready(self) -> None:
        self.download_dir.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(self.download_dir).free
        if free < self.min_free_disk:
            self._cleanup_stale_downloads()
            free = shutil.disk_usage(self.download_dir).free
        if free < self.min_free_disk:
            raise RuntimeError(
                f'Недостаточно свободного места в DOWNLOAD_DIR: {_format_bytes(free)}, '
                f'нужно минимум {_format_bytes(self.min_free_disk)}. '
                'Освободите место или уменьшите MIN_FREE_DISK_MB в .env.'
            )

    def _ensure_upload_size_allowed(self, file_path: Path) -> int:
        size = file_path.stat().st_size
        if size > self.max_upload_size:
            raise RuntimeError(
                f'Файл слишком большой для отправки: {size // MB} МБ, лимит MAX_UPLOAD_SIZE_MB={self.max_upload_size // MB}'
            )
        return size

    async def _collect_dialogs(self, opts: ScanOptions | None = None) -> list[Dialog]:
        for item in await self.db.kv_get_json(EXCLUDED_CHATS_KEY, []):
            self.excluded.add(int(item.get('id', 0)))
        dialogs: list[Dialog] = []
        async for d in self.client.iter_dialogs(limit=10000, ignore_migrated=True):
            if d.id in self.excluded:
                continue
            if not _should_scan_dialog(d):
                continue
            if opts and opts.chat_ids is not None and d.id not in opts.chat_ids:
                continue
            dialogs.append(d)
        return dialogs

    async def list_dialogs(self) -> list[dict[str, Any]]:
        scan_dialogs = await self._run_with_connection_recovery('list_dialogs', lambda: self._collect_dialogs())
        dialogs: list[dict[str, Any]] = []
        for d in scan_dialogs:
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

    async def get_saved_empty_chats(self) -> list[dict[str, Any]]:
        return await self.db.kv_get_json(EMPTY_CHATS_KEY, [])

    async def save_empty_chats(self, empty_chats: list[dict[str, Any]]) -> None:
        await self.db.kv_set_json(EMPTY_CHATS_KEY, empty_chats)

    async def update_empty_chats(self, empty_chats: list[dict[str, Any]], non_empty_chat_ids: set[int], replace: bool) -> None:
        if replace:
            await self.save_empty_chats(empty_chats)
            return
        saved = await self.get_saved_empty_chats()
        by_id = {int(item.get('id', 0)): item for item in saved}
        for chat_id in non_empty_chat_ids:
            by_id.pop(int(chat_id), None)
        for item in empty_chats:
            by_id[int(item['id'])] = item
        await self.save_empty_chats(sorted(by_id.values(), key=lambda x: (str(x.get('name') or '').casefold(), int(x.get('id', 0)))))

    async def forget_empty_chat(self, chat_id: int) -> None:
        empty_chats = await self.get_saved_empty_chats()
        await self.save_empty_chats([x for x in empty_chats if int(x.get('id', 0)) != chat_id])

    async def get_excluded_chats(self) -> list[dict[str, Any]]:
        saved = await self.db.kv_get_json(EXCLUDED_CHATS_KEY, [])
        configured = [{'id': chat_id, 'name': f'ID {chat_id}', 'source': 'config'} for chat_id in sorted(self.excluded)]
        by_id = {int(item['id']): item for item in configured}
        for item in saved:
            by_id[int(item['id'])] = item
        return sorted(by_id.values(), key=lambda x: (str(x.get('name') or '').casefold(), int(x.get('id', 0))))

    async def exclude_chat(self, chat: dict[str, Any]) -> None:
        chat_id = int(chat['id'])
        self.excluded.add(chat_id)
        saved = await self.db.kv_get_json(EXCLUDED_CHATS_KEY, [])
        by_id = {int(item['id']): item for item in saved}
        by_id[chat_id] = {
            'id': chat_id,
            'name': chat.get('name') or str(chat_id),
            'link': chat.get('link'),
            'excluded_at': int(time.time()),
            'source': 'manual',
        }
        await self.db.kv_set_json(EXCLUDED_CHATS_KEY, sorted(by_id.values(), key=lambda x: (str(x.get('name') or '').casefold(), int(x.get('id', 0)))))
        await self.forget_empty_chat(chat_id)

    async def restore_excluded_chat(self, chat_id: int) -> None:
        self.excluded.discard(int(chat_id))
        saved = await self.db.kv_get_json(EXCLUDED_CHATS_KEY, [])
        await self.db.kv_set_json(EXCLUDED_CHATS_KEY, [x for x in saved if int(x.get('id', 0)) != int(chat_id)])

    async def get_period_report(self, period: str) -> list[dict[str, Any]]:
        return await self.db.get_chat_stats(period)

    async def _resolve_telegram_video_link(self, link: str) -> tuple[Any, Message]:
        match = TELEGRAM_LINK_RE.search(_clean_url(link))
        if not match:
            raise ValueError('Поддерживаются ссылки вида https://t.me/channel/123 или https://t.me/c/123/456')

        path_parts = [part for part in match.group('path').strip('/').split('/') if part]
        if len(path_parts) >= 3 and path_parts[0] == 'c':
            chat_id = int(f'-100{path_parts[1]}')
            message_id = int(path_parts[2])
            entity = await self.client.get_entity(chat_id)
        elif len(path_parts) >= 2:
            if path_parts[0] == 's' and len(path_parts) >= 3:
                username = path_parts[1]
                message_id = int(path_parts[2])
            else:
                username = path_parts[0]
                message_id = int(path_parts[1])
            entity = await self.client.get_entity(username)
        else:
            raise ValueError('Не смог разобрать ссылку на сообщение с видео')

        msg = await self.client.get_messages(entity, ids=message_id)
        if not msg:
            raise ValueError('Сообщение по ссылке не найдено или аккаунту нет доступа')
        if not _extract_video_meta(msg):
            raise ValueError('По ссылке найдено сообщение без видео')
        return entity, msg

    async def _download_send_delete(
        self,
        target: Any,
        msg: Message,
        *,
        chat_id: int,
        chat_name: str,
        progress_cb: Optional[ProgressCB] = None,
    ) -> int:
        self._ensure_download_dir_ready()
        work_dir = self.download_dir / f'{abs(chat_id)}_{msg.id}_{int(time.time() * 1000)}'
        work_dir.mkdir(parents=True, exist_ok=True)
        downloaded_path: Path | None = None
        try:
            if progress_cb:
                await progress_cb({'type': 'download_start', 'chat_id': chat_id, 'chat_name': chat_name, 'msg_id': msg.id})
            self.heartbeat.beat(status='download_start', chat_id=chat_id, msg_id=msg.id)
            downloaded = await self.client.download_media(
                msg,
                file=str(work_dir),
                progress_callback=_make_transfer_progress_callback(
                    progress_cb,
                    'download_progress',
                    {'chat_id': chat_id, 'chat_name': chat_name, 'msg_id': msg.id},
                ),
            )
            if not downloaded:
                raise RuntimeError('download_media returned empty path')
            downloaded_path = Path(downloaded)
            meta = _extract_video_meta(msg)
            meta_event = _video_meta_summary(meta[0], meta[1], meta[2], downloaded_path.stat().st_size) if meta else {
                'size': downloaded_path.stat().st_size,
                'size_human': _format_bytes(downloaded_path.stat().st_size),
                'resolution': 'неизвестно',
            }
            if progress_cb:
                await progress_cb({
                    'type': 'download_done',
                    'chat_id': chat_id,
                    'chat_name': chat_name,
                    'msg_id': msg.id,
                    'local_path': str(downloaded_path),
                    **meta_event,
                })

            self._ensure_upload_size_allowed(downloaded_path)
            attributes = _video_attributes_from_values(meta[0], meta[1], meta[2]) if meta else None
            if progress_cb:
                await progress_cb({'type': 'thumbnail_start', 'chat_id': chat_id, 'chat_name': chat_name, 'msg_id': msg.id})
            thumb = await _prepare_video_thumbnail(
                self.client,
                msg,
                downloaded_path,
                work_dir,
                f'{abs(chat_id)}_{msg.id}',
                meta[2] if meta else None,
            )
            if progress_cb:
                await progress_cb({
                    'type': 'thumbnail_done',
                    'chat_id': chat_id,
                    'chat_name': chat_name,
                    'msg_id': msg.id,
                    'thumb_path': str(thumb) if thumb else None,
                    'thumb_size': thumb.stat().st_size if thumb and thumb.exists() else 0,
                    'thumb_size_human': _format_bytes(thumb.stat().st_size) if thumb and thumb.exists() else '0 Б',
                })
            if progress_cb:
                await progress_cb({
                    'type': 'upload_start',
                    'chat_id': chat_id,
                    'chat_name': chat_name,
                    'msg_id': msg.id,
                    'size': downloaded_path.stat().st_size,
                    'size_human': _format_bytes(downloaded_path.stat().st_size),
                    'thumb_path': str(thumb) if thumb else None,
                    'has_thumbnail': bool(thumb),
                })
            self.heartbeat.beat(status='upload_start', chat_id=chat_id, msg_id=msg.id)
            sent = await self.client.send_file(
                target,
                downloaded_path,
                force_document=False,
                supports_streaming=True,
                attributes=attributes,
                thumb=thumb,
                progress_callback=_make_transfer_progress_callback(
                    progress_cb,
                    'upload_progress',
                    {'chat_id': chat_id, 'chat_name': chat_name, 'msg_id': msg.id},
                ),
            )
            sent_msg_id = _sent_message_id(sent)
            if not sent_msg_id:
                raise RuntimeError('Telegram did not return a message id after upload')
            if progress_cb:
                await progress_cb({'type': 'upload_done', 'chat_id': chat_id, 'chat_name': chat_name, 'msg_id': msg.id, 'target_msg_id': sent_msg_id})
            self.heartbeat.beat(status='upload_done', chat_id=chat_id, msg_id=msg.id, target_msg_id=sent_msg_id)
            return sent_msg_id
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)
            if progress_cb:
                await progress_cb({'type': 'local_delete', 'chat_id': chat_id, 'chat_name': chat_name, 'msg_id': msg.id})

    async def _download_send_delete_with_retry(
        self,
        target: Any,
        msg: Message,
        *,
        chat_id: int,
        chat_name: str,
        progress_cb: Optional[ProgressCB] = None,
    ) -> int:
        try:
            return await self._download_send_delete(target, msg, chat_id=chat_id, chat_name=chat_name, progress_cb=progress_cb)
        except FloodWaitError as e:
            seconds = int(getattr(e, 'seconds', 1))
            if progress_cb:
                await progress_cb({'type': 'floodwait', 'chat_id': chat_id, 'seconds': seconds})
            if seconds > self.max_flood_wait_sec:
                raise
            await asyncio.sleep(seconds + 1)
            return await self._download_send_delete(target, msg, chat_id=chat_id, chat_name=chat_name, progress_cb=progress_cb)
        except Exception as e:
            if not _is_connection_error(e):
                raise
            log.warning('Connection error while download/upload chat_id=%s msg_id=%s: %s', chat_id, msg.id, e.__class__.__name__)
            if not await self._recover_connection(f'download/upload chat_id={chat_id} msg_id={msg.id}'):
                raise
            return await self._download_send_delete(target, msg, chat_id=chat_id, chat_name=chat_name, progress_cb=progress_cb)

    async def _send_local_file(
        self,
        target: Any,
        file_path: Path,
        *,
        source_id: str,
        source_name: str,
        attributes: list[DocumentAttributeVideo] | None = None,
        thumb: Path | None = None,
        progress_cb: Optional[ProgressCB] = None,
    ) -> int:
        self._ensure_upload_size_allowed(file_path)
        upload_event = {
            'type': 'upload_start',
            'source_id': source_id,
            'chat_name': source_name,
            'local_path': str(file_path),
            'size': file_path.stat().st_size,
            'size_human': _format_bytes(file_path.stat().st_size),
            'thumb_path': str(thumb) if thumb else None,
            'has_thumbnail': bool(thumb),
        }
        if progress_cb:
            await progress_cb(upload_event)
        self.heartbeat.beat(status='upload_start', source_id=source_id, source_name=source_name)
        sent = await self.client.send_file(
            target,
            file_path,
            force_document=False,
            supports_streaming=True,
            attributes=attributes,
            thumb=thumb,
            progress_callback=_make_transfer_progress_callback(
                progress_cb,
                'upload_progress',
                {'source_id': source_id, 'chat_name': source_name, 'local_path': str(file_path)},
            ),
        )
        sent_msg_id = _sent_message_id(sent)
        if not sent_msg_id:
            raise RuntimeError('Telegram did not return a message id after upload')
        if progress_cb:
            await progress_cb({
                'type': 'upload_done',
                'source_id': source_id,
                'chat_name': source_name,
                'local_path': str(file_path),
                'target_msg_id': sent_msg_id,
            })
        self.heartbeat.beat(status='upload_done', source_id=source_id, source_name=source_name, target_msg_id=sent_msg_id)
        return sent_msg_id

    async def _send_local_file_delete_with_retry(
        self,
        target: Any,
        file_path: Path,
        work_dir: Path,
        *,
        source_id: str,
        source_name: str,
        attributes: list[DocumentAttributeVideo] | None = None,
        thumb: Path | None = None,
        progress_cb: Optional[ProgressCB] = None,
    ) -> int:
        try:
            try:
                return await self._send_local_file(
                    target,
                    file_path,
                    source_id=source_id,
                    source_name=source_name,
                    attributes=attributes,
                    thumb=thumb,
                    progress_cb=progress_cb,
                )
            except FloodWaitError as e:
                seconds = int(getattr(e, 'seconds', 1))
                if progress_cb:
                    await progress_cb({'type': 'floodwait', 'source_id': source_id, 'seconds': seconds})
                if seconds > self.max_flood_wait_sec:
                    raise
                await asyncio.sleep(seconds + 1)
                return await self._send_local_file(
                    target,
                    file_path,
                    source_id=source_id,
                    source_name=source_name,
                    attributes=attributes,
                    thumb=thumb,
                    progress_cb=progress_cb,
                )
            except Exception as e:
                if not _is_connection_error(e):
                    raise
                log.warning('Connection error while uploading file source_id=%s: %s', source_id, e.__class__.__name__)
                if not await self._recover_connection(f'upload file source_id={source_id}'):
                    raise
                return await self._send_local_file(
                    target,
                    file_path,
                    source_id=source_id,
                    source_name=source_name,
                    attributes=attributes,
                    thumb=thumb,
                    progress_cb=progress_cb,
                )
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)
            if progress_cb:
                await progress_cb({'type': 'local_delete', 'source_id': source_id, 'chat_name': source_name})

    async def _download_external_video(
        self,
        url: str,
        *,
        work_dir: Path,
        source_name: str,
        progress_cb: Optional[ProgressCB] = None,
    ) -> tuple[Path, dict[str, Any]]:
        if progress_cb:
            await progress_cb({'type': 'download_start', 'source_id': url, 'chat_name': source_name})
        self.heartbeat.beat(status='download_start', source_id=url, source_name=source_name)

        def run_download() -> dict[str, Any]:
            _ensure_ytdlp_available()
            yt_dlp = importlib.import_module('yt_dlp')

            ffmpeg_path = _find_ffmpeg()
            if ffmpeg_path:
                download_format = 'bestvideo*+bestaudio/best'
                log.debug('Using ffmpeg for external video merge: %s', ffmpeg_path)
            else:
                download_format = 'best[ext=mp4]/best'
                log.warning(
                    'ffmpeg is not installed and bundled imageio-ffmpeg is unavailable; '
                    'downloading the best single-file stream instead of separate video/audio streams'
                )

            options = {
                'format': download_format,
                'outtmpl': str(work_dir / '%(extractor)s_%(id)s.%(ext)s'),
                'noplaylist': True,
                'playlist_items': '1',
                'quiet': True,
                'no_warnings': True,
                'restrictfilenames': True,
                'ignore_no_formats_error': False,
                'max_filesize': self.max_upload_size,
                'retries': 3,
                'fragment_retries': 3,
                'socket_timeout': 30,
            }
            if ffmpeg_path:
                options['merge_output_format'] = 'mp4'
                options['ffmpeg_location'] = ffmpeg_path
            if self.ytdlp_cookies_file:
                options['cookiefile'] = str(self.ytdlp_cookies_file)
            with yt_dlp.YoutubeDL(options) as ydl:
                return ydl.extract_info(url, download=True)

        info = await asyncio.to_thread(run_download)
        files = [path for path in work_dir.iterdir() if path.is_file()]
        if not files:
            raise RuntimeError('yt-dlp did not create a video file')
        downloaded_path = max(files, key=lambda path: path.stat().st_size)
        meta_event = _video_meta_summary_from_info(info, downloaded_path)
        if progress_cb:
            await progress_cb({
                'type': 'download_done',
                'source_id': url,
                'chat_name': source_name,
                'local_path': str(downloaded_path),
                'title': info.get('title'),
                **meta_event,
            })
        return downloaded_path, info

    async def _process_external_video_url(self, url: str, progress_cb: Optional[ProgressCB] = None) -> dict[str, Any]:
        clean_url = _clean_url(url)
        start_ts = time.time()
        source_name = _source_name_from_url(clean_url)
        source_id = hashlib.sha1(f'{clean_url}:{start_ts}'.encode('utf-8')).hexdigest()[:16]
        self._ensure_download_dir_ready()
        work_dir = self.download_dir / f'external_{source_id}'
        work_dir.mkdir(parents=True, exist_ok=True)
        target = await self._run_with_connection_recovery(
            f'get_entity:{self.target_bot_username}',
            lambda: self.client.get_entity(self.target_bot_username),
        )
        try:
            downloaded_path, info = await self._download_external_video(
                clean_url,
                work_dir=work_dir,
                source_name=source_name,
                progress_cb=progress_cb,
            )
            downloaded_size = downloaded_path.stat().st_size
            meta_summary = _video_meta_summary_from_info(info, downloaded_path)
            attributes = _video_attributes_from_info(info)
            duration = info.get('duration')
            if progress_cb:
                await progress_cb({'type': 'thumbnail_start', 'source_id': source_id, 'chat_name': source_name})
            thumb = await _prepare_video_thumbnail(
                self.client,
                None,
                downloaded_path,
                work_dir,
                source_id,
                duration,
            )
            if progress_cb:
                await progress_cb({
                    'type': 'thumbnail_done',
                    'source_id': source_id,
                    'chat_name': source_name,
                    'thumb_path': str(thumb) if thumb else None,
                    'thumb_size': thumb.stat().st_size if thumb and thumb.exists() else 0,
                    'thumb_size_human': _format_bytes(thumb.stat().st_size) if thumb and thumb.exists() else '0 Б',
                })
            target_msg_id = await self._send_local_file_delete_with_retry(
                target,
                downloaded_path,
                work_dir,
                source_id=source_id,
                source_name=source_name,
                attributes=attributes,
                thumb=thumb,
                progress_cb=progress_cb,
            )
            return {
                'link': clean_url,
                'chat_name': source_name,
                'title': info.get('title'),
                'extractor': info.get('extractor_key') or info.get('extractor'),
                'duration': int(info.get('duration') or 0),
                'width': meta_summary['width'],
                'height': meta_summary['height'],
                'resolution': meta_summary['resolution'],
                'size': downloaded_size,
                'size_human': meta_summary['size_human'],
                'target_message_id': target_msg_id,
                'elapsed_sec': int(time.time() - start_ts),
            }
        except Exception:
            shutil.rmtree(work_dir, ignore_errors=True)
            raise

    async def process_video_link(self, link: str, progress_cb: Optional[ProgressCB] = None) -> dict[str, Any]:
        clean_link = _clean_url(link)
        if not TELEGRAM_LINK_RE.search(clean_link):
            return await self._process_external_video_url(clean_link, progress_cb=progress_cb)

        start_ts = time.time()
        target = await self._run_with_connection_recovery(
            f'get_entity:{self.target_bot_username}',
            lambda: self.client.get_entity(self.target_bot_username),
        )
        entity, msg = await self._run_with_connection_recovery('resolve_video_link', lambda: self._resolve_telegram_video_link(clean_link))
        chat_id = int(getattr(entity, 'id', 0) or getattr(msg, 'chat_id', 0) or 0)
        chat_name = getattr(entity, 'title', None) or getattr(entity, 'username', None) or str(chat_id)
        meta = _extract_video_meta(msg)
        if not meta:
            raise ValueError('По ссылке найдено сообщение без видео')
        w, h, duration, size = meta
        target_msg_id = await self._download_send_delete_with_retry(target, msg, chat_id=chat_id, chat_name=chat_name, progress_cb=progress_cb)
        return {
            'link': clean_link,
            'chat_id': chat_id,
            'chat_name': chat_name,
            'message_id': int(msg.id),
            'width': w,
            'height': h,
            'duration': duration,
            'resolution': f'{w}×{h}',
            'size': size,
            'size_human': _format_bytes(size),
            'target_message_id': target_msg_id,
            'elapsed_sec': int(time.time() - start_ts),
        }

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
        target = await self._run_with_connection_recovery(
            f'get_entity:{self.target_bot_username}',
            lambda: self.client.get_entity(self.target_bot_username),
        )
        dialogs = await self._run_with_connection_recovery('collect_dialogs', lambda: self._collect_dialogs(opts))

        since = _since_dt(opts.mode)
        total_checked = 0
        total_matched = 0
        total_forwarded = 0
        total_errors = 0
        total_skipped = 0
        total_video_found = 0
        reject_reasons = {'not_vertical': 0, 'too_short': 0, 'too_narrow': 0, 'too_small': 0}
        per_chat_stats: list[dict[str, Any]] = []
        cancelled = False
        empty_chats: list[dict[str, Any]] = []
        non_empty_chat_ids: set[int] = set()
        can_replace_empty_list = opts.mode == 'all' and opts.chat_ids is None

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
            min_id = 0
            max_id_seen = 0
            checked_in_chat = 0
            matched_in_chat = 0
            forwarded_in_chat = 0
            video_found_in_chat = 0
            hard_stopped_by_date = False
            chat_had_error = False
            chat_finished_cleanly = False

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
                last_heartbeat_at = 0.0
                message_iter_kwargs = _iter_messages_kwargs(opts, since, min_id)
                async for msg in self.client.iter_messages(chat_id, **message_iter_kwargs):
                    now_monotonic = time.monotonic()
                    if checked_in_chat == 0 or now_monotonic - last_heartbeat_at >= 5.0:
                        last_heartbeat_at = now_monotonic
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

                    if msg.date:
                        period_action = _period_scan_action(msg.date, since, opts.order == 'old_to_new')
                        if period_action == 'stop':
                            hard_stopped_by_date = True
                            break
                        if period_action == 'skip':
                            continue

                    meta = _extract_video_meta(msg)
                    if not meta:
                        if progress_cb and checked_in_chat % 1000 == 0:
                            await progress_cb({'type': 'tick', 'chat_id': chat_id, 'checked': total_checked, 'matched': total_matched, 'forwarded': total_forwarded})
                        continue

                    total_video_found += 1
                    video_found_in_chat += 1
                    w, h, duration, size = meta
                    reject_reason = _reject_reason(w, h, duration, size)
                    if reject_reason:
                        reject_reasons[reject_reason] += 1
                        continue

                    matched_in_chat += 1
                    total_matched += 1

                    if await self.db.was_forwarded(chat_id, msg.id):
                        total_skipped += 1
                        continue

                    try:
                        await self._download_send_delete_with_retry(
                            target,
                            msg,
                            chat_id=chat_id,
                            chat_name=dialog_name,
                            progress_cb=progress_cb,
                        )
                    except FloodWaitError as e:
                        seconds = int(getattr(e, 'seconds', 1))
                        total_errors += 1
                        log.warning('FloodWait too long: %ss chat_id=%s msg_id=%s', seconds, chat_id, msg.id)
                        cancelled = True
                        cancel_event.set()
                        break

                    await self.db.mark_forwarded(chat_id, msg.id, int(time.time()))
                    forwarded_in_chat += 1
                    total_forwarded += 1

                    log.info(
                        'Uploaded: chat_id=%s msg_id=%s duration=%ss w=%s h=%s size=%s',
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

                chat_finished_cleanly = not cancelled

            except FloodWaitError as e:
                chat_had_error = True
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
                chat_had_error = True
                total_errors += 1
                log.exception('RPC error on chat_id=%s', chat_id)
                if progress_cb:
                    await progress_cb({'type': 'error', 'chat_id': chat_id, 'error': 'rpc_error'})
            except Exception as e:
                chat_had_error = True
                total_errors += 1
                if _is_connection_error(e):
                    log.warning('Connection error on chat_id=%s: %s', chat_id, e.__class__.__name__)
                    if progress_cb:
                        await progress_cb({'type': 'error', 'chat_id': chat_id, 'error': 'connection_error'})
                    if not await self._recover_connection(f'chat_id={chat_id}'):
                        cancelled = True
                        cancel_event.set()
                        break
                else:
                    log.exception('Unexpected error on chat_id=%s', chat_id)
                    if progress_cb:
                        await progress_cb({'type': 'error', 'chat_id': chat_id, 'error': 'exception'})
            finally:
                if opts.mode == 'new' and chat_finished_cleanly and max_id_seen > min_id:
                    await self.db.set_last_scanned(chat_id, max_id_seen, int(time.time()))

            if not chat_had_error:
                per_chat_stats.append(
                    {
                        'id': chat_id,
                        'name': dialog_name,
                        'checked': checked_in_chat,
                        'matched': matched_in_chat,
                        'video_found': video_found_in_chat,
                        'forwarded': forwarded_in_chat,
                        'link': f'https://t.me/{getattr(dialog.entity, "username", None)}' if getattr(dialog.entity, 'username', None) else None,
                    }
                )

            if not chat_had_error and matched_in_chat > 0:
                non_empty_chat_ids.add(chat_id)

            if not chat_had_error and matched_in_chat == 0:
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
                        'video_found': video_found_in_chat,
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
            'video_found': total_video_found,
            'matched': total_matched,
            'forwarded': total_forwarded,
            'skipped_already_forwarded': total_skipped,
            'reject_reasons': reject_reasons,
            'top_chats': sorted(per_chat_stats, key=lambda x: (x['matched'], x['forwarded'], x['checked']), reverse=True)[:10],
            'errors': total_errors,
            'empty_chats_count': len(empty_chats),
            'empty_chats_updated': bool(empty_chats or non_empty_chat_ids),
            'cancelled': cancelled,
            'elapsed_sec': elapsed,
        }

        await self.update_empty_chats(empty_chats, non_empty_chat_ids, replace=can_replace_empty_list and not cancelled)
        await self.db.upsert_chat_stats(opts.mode, per_chat_stats, int(time.time()))

        result_summary = {k: result[k] for k in ('dialogs', 'checked', 'matched', 'forwarded', 'errors', 'empty_chats_count', 'cancelled', 'elapsed_sec')}
        log.info('Scan finish: %s', result_summary)
        self.heartbeat.beat(status='scan_done', result_summary=result_summary)
        if progress_cb:
            await progress_cb({'type': 'done', **result})
        return result
