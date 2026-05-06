from __future__ import annotations

import asyncio
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from telethon import TelegramClient

from config import load_config
from control_bot import run_control_bot
from db import DB
from scanner import Scanner
from watchdog import Heartbeat, WatchdogKiller, heartbeat_loop


def setup_logging(level: str, logs_dir: Path) -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = logs_dir / 'app.log'

    formatter = logging.Formatter(
        '%(asctime)s | %(levelname)s | %(name)s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers.clear()

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    file_handler = RotatingFileHandler(log_file, maxBytes=5 * 1024 * 1024, backupCount=5, encoding='utf-8')
    file_handler.setFormatter(formatter)

    root.addHandler(stream_handler)
    root.addHandler(file_handler)


async def start_mtproto_client(client: TelegramClient, phone: str, password: str) -> None:
    await client.connect()
    if await client.is_user_authorized():
        await client.get_me()
        return

    print('Нужна первичная авторизация Telethon.')
    sent = await client.send_code_request(phone)
    code = input('Введи код из Telegram: ').strip()
    try:
        await client.sign_in(phone=phone, code=code, phone_code_hash=sent.phone_code_hash)
    except Exception as e:
        if e.__class__.__name__ == 'SessionPasswordNeededError':
            if not password:
                raise RuntimeError('Включена 2FA, но TG_2FA_PASSWORD пустой.')
            await client.sign_in(password=password)
        else:
            raise
    await client.get_me()


async def async_main() -> None:
    cfg = load_config()
    setup_logging(cfg.log_level, cfg.logs_dir)
    log = logging.getLogger('main')

    Path(cfg.session_name).parent.mkdir(parents=True, exist_ok=True)
    heartbeat = Heartbeat(cfg.heartbeat_path)
    heartbeat.beat(status='boot')

    watchdog = WatchdogKiller(
        heartbeat=heartbeat,
        timeout_sec=cfg.watchdog_timeout_sec,
        check_interval_sec=cfg.watchdog_check_interval_sec,
    )
    watchdog.start()

    db = DB(cfg.database_path)
    await db.connect()

    client = TelegramClient(
        cfg.session_name,
        cfg.api_id,
        cfg.api_hash,
        timeout=cfg.connect_timeout_sec,
        request_retries=cfg.request_retries,
        connection_retries=cfg.connection_retries,
        retry_delay=1,
        auto_reconnect=True,
        sequential_updates=False,
        flood_sleep_threshold=cfg.flood_sleep_threshold,
        receive_updates=False,
    )

    try:
        await start_mtproto_client(client, cfg.phone, cfg.tg_2fa_password)
        me = await client.get_me()
        log.info('Telethon authorized as id=%s username=%s', getattr(me, 'id', None), getattr(me, 'username', None))

        scanner = Scanner(
            client=client,
            db=db,
            heartbeat=heartbeat,
            excluded_chat_ids=cfg.excluded_chat_ids,
            target_bot_username=cfg.target_bot_username,
            max_all=cfg.max_all,
            max_period=cfg.max_period,
            forward_delay_sec=cfg.forward_delay_sec,
            forward_jitter_sec=cfg.forward_jitter_sec,
            dialog_delay_sec=cfg.dialog_delay_sec,
            max_flood_wait_sec=cfg.max_flood_wait_sec,
            dry_run_delete=cfg.dry_run_delete,
        )

        hb_task = asyncio.create_task(heartbeat_loop(heartbeat, interval_sec=10))
        try:
            await run_control_bot(
                bot_token=cfg.bot_token,
                allowed_users=cfg.allowed_users,
                scanner=scanner,
                heartbeat=heartbeat,
                progress_edit_interval_sec=cfg.progress_edit_interval_sec,
            )
        finally:
            hb_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await hb_task
    finally:
        watchdog.stop()
        await client.disconnect()
        await db.close()
        heartbeat.beat(status='shutdown')


if __name__ == '__main__':
    import contextlib
    asyncio.run(async_main())
