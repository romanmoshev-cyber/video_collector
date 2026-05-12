from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from html import escape
from typing import Any, Optional

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, KeyboardButton, Message, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from scanner import ScanOptions, Scanner
from watchdog import Heartbeat

log = logging.getLogger('control_bot')


@dataclass
class UserState:
    mode: str = 'new'
    order: str = 'new_to_old'
    selected_chats: set[int] = field(default_factory=set)
    page: int = 0
    dialogs_cache: list[dict[str, Any]] = field(default_factory=list)
    last_stats: Optional[dict[str, Any]] = None
    last_run_at: Optional[int] = None


def _is_allowed(user_id: int, allowed: set[int]) -> bool:
    return (not allowed) or (user_id in allowed)


def _mode_label(mode: str) -> str:
    return {
        'all': 'вся история',
        'month': 'месяц',
        'week': 'неделя',
        'day': 'сутки',
        'new': 'только новые',
    }.get(mode, mode)


def _order_label(order: str) -> str:
    return {'new_to_old': 'новые → старые', 'old_to_new': 'старые → новые'}.get(order, order)


def _short_title(name: str, limit: int = 20) -> str:
    name = ' '.join((name or 'Без названия').split())
    return name if len(name) <= limit else f'{name[:limit - 1]}…'


def _main_text(st: UserState, is_scanning: bool) -> str:
    status = 'идёт пересылка' if is_scanning else 'готов'
    return (
        '📨 <b>Forward Bot</b>\n'
        f'Статус: <b>{status}</b>\n'
        f'Период: <b>{escape(_mode_label(st.mode))}</b>\n'
        f'Порядок: <b>{escape(_order_label(st.order))}</b>\n'
        f'Выбрано каналов/групп: <b>{len(st.selected_chats)}</b>\n\n'
        'Бот пересылает сообщения только из каналов и групп в целевой бот/чат.'
    )


def _main_reply_kb(is_scanning: bool) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text='🔎 Все'), KeyboardButton(text='📌 Выбрать')],
        [KeyboardButton(text='⏱ Период'), KeyboardButton(text='🔁 Порядок')],
        [KeyboardButton(text='📄 Статус')],
    ]
    if is_scanning:
        rows.append([KeyboardButton(text='⛔ Стоп')])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=False)


def _scan_progress_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text='⛔ Стоп', callback_data='scan:stop')
    kb.adjust(1)
    return kb.as_markup()


def _status_text(st: UserState, heartbeat: Heartbeat, is_scanning: bool) -> str:
    text = (
        '📄 <b>Статус</b>\n'
        f'Пересылка сейчас: <b>{"да" if is_scanning else "нет"}</b>\n'
        f'Период: <b>{escape(_mode_label(st.mode))}</b>\n'
        f'Порядок: <b>{escape(_order_label(st.order))}</b>\n'
        f'Выбрано: <b>{len(st.selected_chats)}</b>\n'
        f'Heartbeat age: <b>{heartbeat.age():.1f} сек</b>'
    )
    if st.last_run_at:
        text += f'\nПоследний запуск: <b>{time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.last_run_at))}</b>'
    if st.last_stats:
        text += '\n\n' + _format_report(st.last_stats)
    return text


def _help_text() -> str:
    return (
        'ℹ️ <b>Команды</b>\n'
        '/start — открыть меню.\n'
        '/status — состояние процесса.\n'
        '/help — справка.\n\n'
        'Выбери период и источники: все каналы/группы или только отмеченные. '
        'Кнопка «Стоп» мягко останавливает текущую пересылку.'
    )


def _mode_kb(current: str):
    items = [('new', 'Новые'), ('day', 'Сутки'), ('week', 'Неделя'), ('month', 'Месяц'), ('all', 'Всё')]
    kb = InlineKeyboardBuilder()
    for code, label in items:
        kb.button(text=f'{"✅ " if code == current else ""}{label}', callback_data=f'mode:set:{code}')
    kb.button(text='⬅️ Назад', callback_data='back:main')
    kb.adjust(2, 2, 1, 1)
    return kb.as_markup()


def _order_kb(current: str):
    items = [('new_to_old', '⬇️ Новые'), ('old_to_new', '⬆️ Старые')]
    kb = InlineKeyboardBuilder()
    for code, label in items:
        kb.button(text=f'{"✅ " if code == current else ""}{label}', callback_data=f'order:set:{code}')
    kb.button(text='⬅️ Назад', callback_data='back:main')
    kb.adjust(2, 1)
    return kb.as_markup()


def _pick_text(dialogs: list[dict[str, Any]], selected: set[int], page: int, per_page: int = 20) -> str:
    pages = max(1, (len(dialogs) + per_page - 1) // per_page)
    return (
        '📌 <b>Выбор источников</b>\n'
        f'Страница: <b>{page + 1} / {pages}</b> · всего: <b>{len(dialogs)}</b>\n'
        f'Выбрано: <b>{len(selected)}</b>\n\n'
        'В списке только каналы и группы.'
    )


def _pick_kb(dialogs: list[dict[str, Any]], selected: set[int], page: int, per_page: int = 20):
    kb = InlineKeyboardBuilder()
    start = page * per_page
    chunk = dialogs[start:start + per_page]
    for item in chunk:
        did = int(item['id'])
        mark = '✅' if did in selected else '＋'
        type_icon = '👥' if item.get('is_group') else '📣'
        kb.button(text=f'{mark} {type_icon} {_short_title(item.get("name") or "")}', callback_data=f'pick:toggle:{did}')

    sizes = [2] * (len(chunk) // 2)
    if len(chunk) % 2:
        sizes.append(1)

    nav_count = 0
    if page > 0:
        kb.button(text='⬅️ Назад', callback_data='pick:page:prev')
        nav_count += 1
    if start + per_page < len(dialogs):
        kb.button(text='Вперёд ➡️', callback_data='pick:page:next')
        nav_count += 1
    if nav_count:
        sizes.append(nav_count)

    kb.button(text='✅ Выбрать страницу', callback_data='pick:page_select')
    kb.button(text='➖ Снять страницу', callback_data='pick:page_clear')
    kb.button(text='🔄 Обновить', callback_data='pick:refresh')
    kb.button(text='🚀 Старт выбранных', callback_data='scan:selected')
    kb.button(text='🧹 Сброс всё', callback_data='pick:clear')
    kb.button(text='⬅️ В меню', callback_data='back:main')
    sizes.extend([2, 2, 2])
    kb.adjust(*sizes)
    return kb.as_markup()


def _format_duration(seconds: int | float) -> str:
    seconds = int(seconds or 0)
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f'{hours}ч {minutes}м {sec}с'
    if minutes:
        return f'{minutes}м {sec}с'
    return f'{sec}с'


def _format_report(stats: dict[str, Any]) -> str:
    return (
        '📊 <b>Итог пересылки</b>\n'
        f'Источников: <b>{stats.get("dialogs", 0)}</b>\n'
        f'Проверено сообщений: <b>{stats.get("checked", 0)}</b>\n'
        f'Переслано: <b>{stats.get("forwarded", 0)}</b>\n'
        f'Уже были пересланы: <b>{stats.get("skipped_already_forwarded", 0)}</b>\n'
        f'Ошибок: <b>{stats.get("errors", 0)}</b>\n'
        f'Время: <b>{_format_duration(stats.get("elapsed_sec", 0))}</b>'
    )


async def run_control_bot(
    bot_token: str,
    allowed_users: set[int],
    scanner: Scanner,
    heartbeat: Heartbeat,
    progress_edit_interval_sec: float,
) -> None:
    bot = Bot(bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    states: dict[int, UserState] = {}
    scan_lock = asyncio.Lock()
    cancel_event = asyncio.Event()

    async def get_state(user_id: int) -> UserState:
        states.setdefault(user_id, UserState())
        return states[user_id]

    async def safe_edit_text(message: Message, text: str, reply_markup=None) -> None:
        try:
            await message.edit_text(text, reply_markup=reply_markup)
        except TelegramBadRequest as e:
            if 'message is not modified' not in str(e).lower():
                raise

    async def show_main(message: Message, st: UserState) -> None:
        await message.answer(_main_text(st, scan_lock.locked()), reply_markup=_main_reply_kb(scan_lock.locked()))

    async def refresh_dialogs(st: UserState) -> None:
        st.dialogs_cache = await scanner.list_dialogs()
        max_page = max(0, (len(st.dialogs_cache) - 1) // 20)
        st.page = min(st.page, max_page)

    @dp.message(F.text.in_(('/start', '⬅️ В меню')))
    async def cmd_start(m: Message):
        if not _is_allowed(m.from_user.id, allowed_users):
            return
        await show_main(m, await get_state(m.from_user.id))

    @dp.message(F.text == '/help')
    async def cmd_help(m: Message):
        if _is_allowed(m.from_user.id, allowed_users):
            await m.answer(_help_text(), reply_markup=_main_reply_kb(scan_lock.locked()))

    @dp.message(F.text.in_(('/status', '📄 Статус')))
    async def cmd_status(m: Message):
        if _is_allowed(m.from_user.id, allowed_users):
            await m.answer(_status_text(await get_state(m.from_user.id), heartbeat, scan_lock.locked()), reply_markup=_main_reply_kb(scan_lock.locked()))

    @dp.message(F.text == '⏱ Период')
    async def mode_menu(m: Message):
        if _is_allowed(m.from_user.id, allowed_users):
            st = await get_state(m.from_user.id)
            await m.answer('⏱ <b>Выбери период пересылки</b>', reply_markup=_mode_kb(st.mode))

    @dp.callback_query(F.data.startswith('mode:set:'))
    async def set_mode(c: CallbackQuery):
        if not _is_allowed(c.from_user.id, allowed_users):
            return
        st = await get_state(c.from_user.id)
        st.mode = c.data.rsplit(':', 1)[-1]
        await c.answer('Период обновлён')
        await safe_edit_text(c.message, '⏱ <b>Выбери период пересылки</b>', reply_markup=_mode_kb(st.mode))

    @dp.message(F.text == '🔁 Порядок')
    async def order_menu(m: Message):
        if _is_allowed(m.from_user.id, allowed_users):
            st = await get_state(m.from_user.id)
            await m.answer('🔁 <b>Выбери порядок</b>', reply_markup=_order_kb(st.order))

    @dp.callback_query(F.data.startswith('order:set:'))
    async def set_order(c: CallbackQuery):
        if not _is_allowed(c.from_user.id, allowed_users):
            return
        st = await get_state(c.from_user.id)
        st.order = c.data.rsplit(':', 1)[-1]
        await c.answer('Порядок обновлён')
        await safe_edit_text(c.message, '🔁 <b>Выбери порядок</b>', reply_markup=_order_kb(st.order))

    @dp.callback_query(F.data == 'back:main')
    async def back_main(c: CallbackQuery):
        if _is_allowed(c.from_user.id, allowed_users):
            await c.answer()
            await c.message.answer(_main_text(await get_state(c.from_user.id), scan_lock.locked()), reply_markup=_main_reply_kb(scan_lock.locked()))

    @dp.message(F.text == '📌 Выбрать')
    async def pick_menu(m: Message):
        if not _is_allowed(m.from_user.id, allowed_users):
            return
        st = await get_state(m.from_user.id)
        await refresh_dialogs(st)
        await m.answer(_pick_text(st.dialogs_cache, st.selected_chats, st.page), reply_markup=_pick_kb(st.dialogs_cache, st.selected_chats, st.page))

    @dp.callback_query(F.data.startswith('pick:'))
    async def pick_handler(c: CallbackQuery):
        if not _is_allowed(c.from_user.id, allowed_users):
            return
        st = await get_state(c.from_user.id)
        parts = c.data.split(':')
        action = parts[1]
        per_page = 20
        if not st.dialogs_cache:
            await refresh_dialogs(st)

        if action == 'toggle':
            chat_id = int(parts[2])
            if chat_id in st.selected_chats:
                st.selected_chats.remove(chat_id)
            else:
                st.selected_chats.add(chat_id)
            await c.answer()
        elif action == 'page':
            max_page = max(0, (len(st.dialogs_cache) - 1) // per_page)
            st.page = max(0, st.page - 1) if parts[2] == 'prev' else min(max_page, st.page + 1)
            await c.answer()
        elif action == 'page_select':
            for item in st.dialogs_cache[st.page * per_page:st.page * per_page + per_page]:
                st.selected_chats.add(int(item['id']))
            await c.answer('Страница выбрана')
        elif action == 'page_clear':
            for item in st.dialogs_cache[st.page * per_page:st.page * per_page + per_page]:
                st.selected_chats.discard(int(item['id']))
            await c.answer('Страница снята')
        elif action == 'refresh':
            await refresh_dialogs(st)
            await c.answer('Список обновлён')
        elif action == 'clear':
            st.selected_chats.clear()
            await c.answer('Выбор очищен')

        await safe_edit_text(c.message, _pick_text(st.dialogs_cache, st.selected_chats, st.page), reply_markup=_pick_kb(st.dialogs_cache, st.selected_chats, st.page))

    async def start_forward(message: Message, user_id: int, st: UserState, kind: str) -> None:
        if scan_lock.locked():
            await message.answer('Пересылка уже идёт.', reply_markup=_main_reply_kb(True))
            return
        if kind == 'selected' and not st.selected_chats:
            await message.answer('Сначала выбери хотя бы один канал или группу.', reply_markup=_main_reply_kb(False))
            return

        cancel_event.clear()
        opts = ScanOptions(mode=st.mode, order=st.order, chat_ids=None if kind == 'allchats' else set(st.selected_chats))
        progress_msg = await message.answer('🚀 Запускаю пересылку…', reply_markup=_scan_progress_kb())
        state = {'dialogs_total': 0, 'chat_index': 0, 'chat_name': '—', 'checked': 0, 'forwarded': 0, 'floodwait': None}
        last_edit = 0.0

        async def progress_cb(ev: dict[str, Any]) -> None:
            nonlocal last_edit
            event_type = ev.get('type')
            now = time.monotonic()
            if 'dialogs_total' in ev:
                state['dialogs_total'] = int(ev['dialogs_total'])
            if event_type in {'chat_start', 'chat_done'}:
                state['chat_index'] = int(ev.get('chat_index', state['chat_index']))
                state['chat_name'] = ev.get('chat_name', state['chat_name'])
            if 'checked' in ev:
                state['checked'] = int(ev['checked'])
            if 'forwarded' in ev:
                state['forwarded'] = int(ev['forwarded'])
            if event_type == 'floodwait':
                state['floodwait'] = ev.get('seconds')

            heartbeat.beat(status='forward_progress', event_type=event_type, chat_index=state['chat_index'], dialogs_total=state['dialogs_total'], checked=state['checked'], forwarded=state['forwarded'])
            if now - last_edit < progress_edit_interval_sec and event_type not in {'done', 'floodwait'}:
                return
            last_edit = now
            fw_line = f'\n⏳ FloodWait: <b>{state["floodwait"]} сек</b>' if state['floodwait'] else ''
            text = (
                '🚚 <b>Пересылка идёт</b>\n'
                f'Период: <b>{escape(_mode_label(st.mode))}</b>\n'
                f'Порядок: <b>{escape(_order_label(st.order))}</b>\n'
                f'Источники: <b>{state["chat_index"]} / {state["dialogs_total"]}</b>\n'
                f'Текущий: <b>{escape(str(state["chat_name"]))}</b>\n'
                f'Проверено: <b>{state["checked"]}</b>\n'
                f'Переслано: <b>{state["forwarded"]}</b>{fw_line}'
            )
            await safe_edit_text(progress_msg, text, reply_markup=_scan_progress_kb())

        await scan_lock.acquire()

        async def run_task() -> None:
            try:
                st.last_run_at = int(time.time())
                stats = await scanner.scan(opts, cancel_event=cancel_event, progress_cb=progress_cb)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception('forward task failed')
                stats = {'dialogs': state['dialogs_total'], 'checked': state['checked'], 'forwarded': state['forwarded'], 'skipped_already_forwarded': 0, 'errors': 1, 'cancelled': True, 'elapsed_sec': int(time.time() - st.last_run_at) if st.last_run_at else 0}
                await progress_msg.answer(f'❌ Ошибка пересылки: <code>{escape(e.__class__.__name__)}</code>')
            finally:
                if scan_lock.locked():
                    scan_lock.release()
            st.last_stats = stats
            await safe_edit_text(progress_msg, _format_report(stats) + ('\n⛔ <b>Остановлено</b>' if stats.get('cancelled') else '\n✅ <b>Завершено</b>'))
            await message.answer(_main_text(st, False), reply_markup=_main_reply_kb(False))

        asyncio.create_task(run_task(), name=f'forward-scan-{user_id}')

    @dp.callback_query(F.data == 'scan:stop')
    async def scan_stop(c: CallbackQuery):
        if not _is_allowed(c.from_user.id, allowed_users):
            return
        if not scan_lock.locked():
            await c.answer('Пересылка сейчас не идёт.')
            return
        cancel_event.set()
        heartbeat.beat(status='forward_stop_requested', user_id=c.from_user.id)
        await c.answer('Останавливаю…')

    @dp.message(F.text == '⛔ Стоп')
    async def scan_stop_message(m: Message):
        if not _is_allowed(m.from_user.id, allowed_users):
            return
        if not scan_lock.locked():
            await m.answer('Пересылка сейчас не идёт.', reply_markup=_main_reply_kb(False))
            return
        cancel_event.set()
        heartbeat.beat(status='forward_stop_requested', user_id=m.from_user.id)
        await m.answer('⛔ Останавливаю пересылку…', reply_markup=_main_reply_kb(True))

    @dp.callback_query(F.data == 'scan:selected')
    async def scan_selected(c: CallbackQuery):
        if _is_allowed(c.from_user.id, allowed_users):
            await c.answer()
            await start_forward(c.message, c.from_user.id, await get_state(c.from_user.id), 'selected')

    @dp.message(F.text == '🔎 Все')
    async def scan_all_message(m: Message):
        if _is_allowed(m.from_user.id, allowed_users):
            await start_forward(m, m.from_user.id, await get_state(m.from_user.id), 'allchats')

    await dp.start_polling(bot, polling_timeout=10, handle_as_tasks=True, close_bot_session=True)
