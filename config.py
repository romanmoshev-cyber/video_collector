from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / '.env')


def _csv_ints(name: str) -> set[int]:
    raw = os.getenv(name, '').strip()
    if not raw:
        return set()
    out: set[int] = set()
    for part in raw.split(','):
        part = part.strip()
        if not part:
            continue
        out.add(int(part))
    return out


def _int(name: str, default: int) -> int:
    value = os.getenv(name, str(default)).strip()
    try:
        return int(value)
    except Exception:
        return default


def _float(name: str, default: float) -> float:
    value = os.getenv(name, str(default)).strip()
    try:
        return float(value)
    except Exception:
        return default


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {'1', 'true', 'yes', 'on'}


@dataclass(frozen=True)
class Config:
    bot_token: str
    api_id: int
    api_hash: str
    phone: str
    tg_2fa_password: str
    session_name: str
    allowed_users: set[int]
    excluded_chat_ids: set[int]
    target_bot_username: str
    log_level: str
    database_path: Path
    logs_dir: Path
    heartbeat_path: Path
    downloads_dir: Path
    downloads_reserve_mb: int
    watchdog_timeout_sec: int
    watchdog_check_interval_sec: int
    progress_edit_interval_sec: float
    forward_delay_sec: float
    forward_jitter_sec: float
    dialog_delay_sec: float
    max_flood_wait_sec: int
    request_retries: int
    connection_retries: int
    flood_sleep_threshold: int
    connect_timeout_sec: int
    dry_run_delete: bool


def load_config() -> Config:
    bot_token = os.getenv('BOT_TOKEN', '').strip()
    api_id = _int('API_ID', 0)
    api_hash = os.getenv('API_HASH', '').strip()
    phone = os.getenv('PHONE', '').strip()
    tg_2fa_password = os.getenv('TG_2FA_PASSWORD', '').strip()
    session_name = os.getenv('SESSION_NAME', str(BASE_DIR / 'sessions' / 'collector')).strip()
    allowed_users = _csv_ints('ALLOWED_USERS')
    excluded_chat_ids = _csv_ints('EXCLUDED_CHAT_IDS')
    target_bot_username = os.getenv('TARGET_BOT_USERNAME', 'Content_Vertical_BOT').strip().lstrip('@')
    log_level = os.getenv('LOG_LEVEL', 'INFO').strip().upper()

    database_path = Path(os.getenv('DATABASE_PATH', str(BASE_DIR / 'data' / 'bot.sqlite3'))).resolve()
    logs_dir = Path(os.getenv('LOGS_DIR', str(BASE_DIR / 'logs'))).resolve()
    heartbeat_path = Path(os.getenv('HEARTBEAT_PATH', str(BASE_DIR / 'runtime' / 'heartbeat.json'))).resolve()
    downloads_dir = Path(os.getenv('DOWNLOADS_DIR', str(BASE_DIR / 'runtime' / 'downloads'))).resolve()

    downloads_reserve_mb = max(0, _int('DOWNLOADS_RESERVE_MB', 256))
    watchdog_timeout_sec = _int('WATCHDOG_TIMEOUT_SEC', 900)
    watchdog_check_interval_sec = _int('WATCHDOG_CHECK_INTERVAL_SEC', 15)
    progress_edit_interval_sec = _float('PROGRESS_EDIT_INTERVAL_SEC', 3.0)
    forward_delay_sec = _float('FORWARD_DELAY_SEC', 0.9)
    forward_jitter_sec = _float('FORWARD_JITTER_SEC', 0.45)
    dialog_delay_sec = _float('DIALOG_DELAY_SEC', 0.35)
    max_flood_wait_sec = _int('MAX_FLOOD_WAIT_SEC', 300)
    request_retries = _int('REQUEST_RETRIES', 5)
    connection_retries = _int('CONNECTION_RETRIES', 5)
    flood_sleep_threshold = _int('FLOOD_SLEEP_THRESHOLD', 60)
    connect_timeout_sec = _int('CONNECT_TIMEOUT_SEC', 15)
    dry_run_delete = _bool('DRY_RUN_DELETE', False)

    if not bot_token:
        raise ValueError('BOT_TOKEN is empty')
    if not api_id or not api_hash:
        raise ValueError('API_ID/API_HASH are empty')
    if not phone:
        raise ValueError('PHONE is empty (needed for first MTProto login)')

    return Config(
        bot_token=bot_token,
        api_id=api_id,
        api_hash=api_hash,
        phone=phone,
        tg_2fa_password=tg_2fa_password,
        session_name=session_name,
        allowed_users=allowed_users,
        excluded_chat_ids=excluded_chat_ids,
        target_bot_username=target_bot_username,
        log_level=log_level,
        database_path=database_path,
        logs_dir=logs_dir,
        heartbeat_path=heartbeat_path,
        downloads_dir=downloads_dir,
        downloads_reserve_mb=downloads_reserve_mb,
        watchdog_timeout_sec=watchdog_timeout_sec,
        watchdog_check_interval_sec=watchdog_check_interval_sec,
        progress_edit_interval_sec=progress_edit_interval_sec,
        forward_delay_sec=forward_delay_sec,
        forward_jitter_sec=forward_jitter_sec,
        dialog_delay_sec=dialog_delay_sec,
        max_flood_wait_sec=max_flood_wait_sec,
        request_retries=request_retries,
        connection_retries=connection_retries,
        flood_sleep_threshold=flood_sleep_threshold,
        connect_timeout_sec=connect_timeout_sec,
        dry_run_delete=dry_run_delete,
    )
