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
from aiogram.types import CallbackQuery, Message
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
    last_empty_page: int = 0


def _is_allowed(user_id: int, allowed: set[int]) -> bool:
    return (not allowed) or (user_id in allowed)


def _main_kb(is_scanning: bool):
    kb = InlineKeyboardBuilder()
    kb.button(text='🔎 Поиск во всех', callback_data='scan:allchats')
    kb.button(text='📌 Выбрать чаты/каналы', callback_data='pick:open')
    kb.button(text='⏱ Период', callback_data='mode:open')
    kb.button(text='🔁 Порядок', callback_data='order:open')
    kb.button(text='📊 Последний отчёт', callback_data='report:last')
    kb.button(text='🧹 Пустые каналы/группы', callback_data='empty:list')
    kb.button(text='📄 Статус', callback_data='status')
    if is_scanning:
        kb.button(text='⛔ Стоп', callback_data='scan:stop')
    kb.adjust(1)
    return kb.as_markup()


def _mode_kb(current: str):
    items = [('all', 'Всё'), ('month', 'Последний месяц'), ('week', 'Последняя неделя'), ('new', 'Только новые')]
    kb = InlineKeyboardBuilder()
    for code, label in items:
        mark = '✅ ' if code == current else ''
        kb.button(text=f'{mark}{label}', callback_data=f'mode:set:{code}')
    kb.button(text='⬅️ Назад', callback_data='back:main')
    kb.adjust(1)
    return kb.as_markup()


def _order_kb(current: str):
    items = [('new_to_old', 'Новые → старые'), ('old_to_new', 'Старые → новые')]
    kb = InlineKeyboardBuilder()
    for code, label in items:
        mark = '✅ ' if code == current else ''
        kb.button(text=f'{mark}{label}', callback_data=f'order:set:{code}')
    kb.button(text='⬅️ Назад', callback_data='back:main')
    kb.adjust(1)
    return kb.as_markup()


def _pick_kb(dialogs: list[dict[str, Any]], selected: set[int], page: int, per_page: int = 12):
    kb = InlineKeyboardBuilder()
    start = page * per_page
    chunk = dialogs[start:start + per_page]

    for item in chunk:
        did = item['id']
        name = item['name']
        mark = '✅' if did in selected else '➕'
        kb.button(text=f'{mark} {name[:32]}', callback_data=f'pick:toggle:{did}')

    if page > 0:
        kb.button(text='⬅️', callback_data='pick:page:prev')
    if start + per_page < len(dialogs):
        kb.button(text='➡️', callback_data='pick:page:next')

    kb.button(text='🚀 Старт (по выбранным)', callback_data='scan:selected')
    kb.button(text='🧹 Очистить выбор', callback_data='pick:clear')
    kb.button(text='⬅️ Назад', callback_data='back:main')
    kb.adjust(1)
    return kb.as_markup()


def _empty_item_text(item: dict[str, Any], page: int, total: int) -> str:
    link_line = f"<a href='{escape(item['link'])}'>Открыть канал/группу</a>" if item.get('link') else 'Публичной ссылки нет.'
    peer_type = 'канал' if item.get('is_channel') else 'группа'
    return (
        f'🧹 <b>Без подходящих видео</b>\n'
        f'#{page + 1} из {total}\n\n'
        f'<b>Название:</b> {escape(item.get("name") or "—")}\n'
        f'<b>ID:</b> <code>{item.get("id")}</code>\n'
        f'<b>Тип:</b> {peer_type}\n'
        f'<b>Проверено сообщений:</b> {item.get("checked", 0)}\n'
        f'<b>Подошло:</b> {item.get("matched", 0)}\n'
        f'<b>Форварднуто:</b> {item.get("forwarded", 0)}\n'
        f'{link_line}'
    )


def _empty_item_kb(item: dict[str, Any], page: int, total: int):
    kb = InlineKeyboardBuilder()
    if item.get('link'):
        kb.button(text='🔗 Открыть', url=item['link'])
    kb.button(text='🗑 Удалить/выйти', callback_data=f'empty:delete:confirm:{item["id"]}')

    if page > 0:
        kb.button(text='⬅️', callback_data='empty:nav:prev')
    if page + 1 < total:
        kb.button(text='➡️', callback_data='empty:nav:next')

    kb.button(text='⬅️ Назад', callback_data='back:main')
    kb.adjust(1)
    return kb.as_markup()


def _confirm_delete_kb(chat_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text='✅ Да, удалить/выйти', callback_data=f'empty:delete:do:{chat_id}')
    kb.button(text='↩️ Отмена', callback_data='empty:list')
    kb.adjust(1)
    return kb.as_markup()


def _format_report(stats: Optional[dict[str, Any]]) -> str:
    if not stats:
        return 'Отчёта пока нет.'
    lines = [
        '📊 <b>Отчёт сканирования</b>',
        f'Чатов: <b>{stats.get("dialogs", 0)}</b>',
        f'Проверено сообщений: <b>{stats.get("checked", 0)}</b>',
        f'Подошло видео: <b>{stats.get("matched", 0)}</b>',
        f'Форварднуто: <b>{stats.get("forwarded", 0)}</b>',
        f'Пропущено как дубль: <b>{stats.get("skipped_already_forwarded", 0)}</b>',
        f'Ошибок: <b>{stats.get("errors", 0)}</b>',
        f'Пустых каналов/групп: <b>{stats.get("empty_chats_count", 0)}</b>',
        f'Время: <b>{stats.get("elapsed_sec", 0)} сек</b>',
        'Статус: ⛔ остановлено' if stats.get('cancelled') else 'Статус: ✅ завершено',
    ]
    return '\n'.join(lines)


async def run_control_bot(
    bot_token: str,
    allowed_users: set[int],
    scanner: Scanner,
    heartbeat: Heartbeat,
    progress_edit_interval_sec: float,
):
    bot = Bot(bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()

    states: dict[int, UserState] = {}
    scan_lock = asyncio.Lock()
    cancel_event = asyncio.Event()

    async def get_state(uid: int) -> UserState:
        if uid not in states:
            states[uid] = UserState()
        return states[uid]

    async def safe_edit_text(message: Message, text: str, reply_markup=None):
        try:
            await message.edit_text(text, reply_markup=reply_markup, disable_web_page_preview=True)
        except TelegramBadRequest:
            pass
        except Exception:
            log.exception('safe_edit_text failed')

    async def render_empty_list(message: Message, st: UserState):
        empty = (st.last_stats or {}).get('empty_chats') or []
        if not empty:
            await safe_edit_text(message, 'Список пустых каналов/групп пока пуст.', reply_markup=_main_kb(scan_lock.locked()))
            return
        st.last_empty_page = max(0, min(st.last_empty_page, len(empty) - 1))
        item = empty[st.last_empty_page]
        await safe_edit_text(message, _empty_item_text(item, st.last_empty_page, len(empty)), reply_markup=_empty_item_kb(item, st.last_empty_page, len(empty)))

    @dp.message(F.text == '/start')
    async def start(m: Message):
        if not _is_allowed(m.from_user.id, allowed_users):
            return
        heartbeat.beat(status='menu_open', user_id=m.from_user.id)
        await m.answer('Меню:', reply_markup=_main_kb(is_scanning=scan_lock.locked()))

    @dp.callback_query(F.data == 'back:main')
    async def back_main(c: CallbackQuery):
        if not _is_allowed(c.from_user.id, allowed_users):
            return
        heartbeat.beat(status='back_main', user_id=c.from_user.id)
        await safe_edit_text(c.message, 'Меню:', reply_markup=_main_kb(is_scanning=scan_lock.locked()))
        await c.answer()

    @dp.callback_query(F.data == 'mode:open')
    async def mode_open(c: CallbackQuery):
        if not _is_allowed(c.from_user.id, allowed_users):
            return
        st = await get_state(c.from_user.id)
        await safe_edit_text(c.message, 'Выбери период:', reply_markup=_mode_kb(st.mode))
        await c.answer()

    @dp.callback_query(F.data.startswith('mode:set:'))
    async def mode_set(c: CallbackQuery):
        if not _is_allowed(c.from_user.id, allowed_users):
            return
        st = await get_state(c.from_user.id)
        st.mode = c.data.split(':')[-1]
        await safe_edit_text(c.message, f'Период установлен: <b>{escape(st.mode)}</b>', reply_markup=_mode_kb(st.mode))
        await c.answer('Ок')

    @dp.callback_query(F.data == 'order:open')
    async def order_open(c: CallbackQuery):
        if not _is_allowed(c.from_user.id, allowed_users):
            return
        st = await get_state(c.from_user.id)
        await safe_edit_text(c.message, 'Выбери порядок сканирования:', reply_markup=_order_kb(st.order))
        await c.answer()

    @dp.callback_query(F.data.startswith('order:set:'))
    async def order_set(c: CallbackQuery):
        if not _is_allowed(c.from_user.id, allowed_users):
            return
        st = await get_state(c.from_user.id)
        st.order = c.data.split(':')[-1]
        await safe_edit_text(c.message, f'Порядок установлен: <b>{escape(st.order)}</b>', reply_markup=_order_kb(st.order))
        await c.answer('Ок')

    @dp.callback_query(F.data == 'pick:open')
    async def pick_open(c: CallbackQuery):
        if not _is_allowed(c.from_user.id, allowed_users):
            return
        st = await get_state(c.from_user.id)
        if not st.dialogs_cache:
            await c.answer('Гружу список…', show_alert=False)
            st.dialogs_cache = await scanner.list_dialogs()
            st.dialogs_cache.sort(key=lambda x: (x.get('name') or '').lower())
        st.page = 0
        await safe_edit_text(
            c.message,
            f'Выбери чаты/каналы (всего: <b>{len(st.dialogs_cache)}</b>):',
            reply_markup=_pick_kb(st.dialogs_cache, st.selected_chats, st.page),
        )
        await c.answer()

    @dp.callback_query(F.data.startswith('pick:toggle:'))
    async def pick_toggle(c: CallbackQuery):
        if not _is_allowed(c.from_user.id, allowed_users):
            return
        st = await get_state(c.from_user.id)
        did = int(c.data.split(':')[-1])
        if did in st.selected_chats:
            st.selected_chats.remove(did)
        else:
            st.selected_chats.add(did)
        try:
            await c.message.edit_reply_markup(reply_markup=_pick_kb(st.dialogs_cache, st.selected_chats, st.page))
        except TelegramBadRequest:
            pass
        await c.answer()

    @dp.callback_query(F.data == 'pick:clear')
    async def pick_clear(c: CallbackQuery):
        if not _is_allowed(c.from_user.id, allowed_users):
            return
        st = await get_state(c.from_user.id)
        st.selected_chats.clear()
        try:
            await c.message.edit_reply_markup(reply_markup=_pick_kb(st.dialogs_cache, st.selected_chats, st.page))
        except TelegramBadRequest:
            pass
        await c.answer('Очищено')

    @dp.callback_query(F.data.startswith('pick:page:'))
    async def pick_page(c: CallbackQuery):
        if not _is_allowed(c.from_user.id, allowed_users):
            return
        st = await get_state(c.from_user.id)
        direction = c.data.split(':')[-1]
        if direction == 'next':
            st.page += 1
        elif direction == 'prev' and st.page > 0:
            st.page -= 1
        try:
            await c.message.edit_reply_markup(reply_markup=_pick_kb(st.dialogs_cache, st.selected_chats, st.page))
        except TelegramBadRequest:
            pass
        await c.answer()

    @dp.callback_query(F.data == 'status')
    async def status(c: CallbackQuery):
        if not _is_allowed(c.from_user.id, allowed_users):
            return
        st = await get_state(c.from_user.id)
        await c.answer()
        msg = (
            f'Период: <b>{escape(st.mode)}</b>\n'
            f'Порядок: <b>{escape(st.order)}</b>\n'
            f'Выбрано чатов: <b>{len(st.selected_chats)}</b>\n'
            f'Скан сейчас: <b>{"да" if scan_lock.locked() else "нет"}</b>\n'
            f'Heartbeat age: <b>{heartbeat.age():.1f} сек</b>'
        )
        if st.last_run_at:
            msg += f'\nПоследний запуск: <b>{time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.last_run_at))}</b>'
        await c.message.answer(msg)

    @dp.callback_query(F.data == 'report:last')
    async def report_last(c: CallbackQuery):
        if not _is_allowed(c.from_user.id, allowed_users):
            return
        st = await get_state(c.from_user.id)
        await c.answer()
        await c.message.answer(_format_report(st.last_stats))

    @dp.callback_query(F.data == 'empty:list')
    async def empty_list(c: CallbackQuery):
        if not _is_allowed(c.from_user.id, allowed_users):
            return
        st = await get_state(c.from_user.id)
        await render_empty_list(c.message, st)
        await c.answer()

    @dp.callback_query(F.data.startswith('empty:nav:'))
    async def empty_nav(c: CallbackQuery):
        if not _is_allowed(c.from_user.id, allowed_users):
            return
        st = await get_state(c.from_user.id)
        empty = (st.last_stats or {}).get('empty_chats') or []
        if not empty:
            await c.answer('Список пуст.', show_alert=False)
            return
        direction = c.data.split(':')[-1]
        if direction == 'next' and st.last_empty_page + 1 < len(empty):
            st.last_empty_page += 1
        elif direction == 'prev' and st.last_empty_page > 0:
            st.last_empty_page -= 1
        await render_empty_list(c.message, st)
        await c.answer()

    @dp.callback_query(F.data.startswith('empty:delete:confirm:'))
    async def empty_delete_confirm(c: CallbackQuery):
        if not _is_allowed(c.from_user.id, allowed_users):
            return
        st = await get_state(c.from_user.id)
        chat_id = int(c.data.split(':')[-1])
        empty = (st.last_stats or {}).get('empty_chats') or []
        item = next((x for x in empty if int(x['id']) == chat_id), None)
        if not item:
            await c.answer('Элемент не найден.', show_alert=True)
            return
        text = (
            f'⚠️ <b>Подтверди удаление/выход</b>\n\n'
            f'{escape(item["name"])}\n'
            f'ID: <code>{chat_id}</code>\n\n'
            f'Это удалит диалог из аккаунта и выйдет из канала/группы, если Telegram это разрешает.'
        )
        await safe_edit_text(c.message, text, reply_markup=_confirm_delete_kb(chat_id))
        await c.answer()

    @dp.callback_query(F.data.startswith('empty:delete:do:'))
    async def empty_delete_do(c: CallbackQuery):
        if not _is_allowed(c.from_user.id, allowed_users):
            return
        st = await get_state(c.from_user.id)
        chat_id = int(c.data.split(':')[-1])
        ok, info = await scanner.delete_dialog_by_id(chat_id)
        if ok:
            empty = (st.last_stats or {}).get('empty_chats') or []
            st.last_stats['empty_chats'] = [x for x in empty if int(x['id']) != chat_id]
            st.last_stats['empty_chats_count'] = len(st.last_stats['empty_chats'])
            if st.last_empty_page >= len(st.last_stats['empty_chats']) and st.last_empty_page > 0:
                st.last_empty_page -= 1
            if st.last_stats['empty_chats']:
                await render_empty_list(c.message, st)
            else:
                await safe_edit_text(c.message, f'✅ {escape(info)}', reply_markup=_main_kb(scan_lock.locked()))
        else:
            await c.message.answer(f'Не удалось удалить: {escape(info)}')
        await c.answer('Готово' if ok else 'Ошибка')

    @dp.callback_query(F.data == 'scan:stop')
    async def scan_stop(c: CallbackQuery):
        if not _is_allowed(c.from_user.id, allowed_users):
            return
        if not scan_lock.locked():
            await c.answer('Скан сейчас не идёт.', show_alert=False)
            return
        cancel_event.set()
        heartbeat.beat(status='scan_stop_requested', user_id=c.from_user.id)
        await c.answer('Останавливаю…', show_alert=False)

    @dp.callback_query(F.data.startswith('scan:'))
    async def scan_handler(c: CallbackQuery):
        if not _is_allowed(c.from_user.id, allowed_users):
            return
        st = await get_state(c.from_user.id)
        kind = c.data.split(':')[-1]
        if kind == 'stop':
            return
        if scan_lock.locked():
            await c.answer('Скан уже идёт. Нажми ⛔ Стоп.', show_alert=True)
            return

        if kind == 'allchats':
            opts = ScanOptions(mode=st.mode, chat_ids=None, order=st.order)
            title = '🔎 Поиск во всех'
        elif kind == 'selected':
            if not st.selected_chats:
                await c.answer('Сначала выбери чаты.', show_alert=True)
                return
            opts = ScanOptions(mode=st.mode, chat_ids=set(st.selected_chats), order=st.order)
            title = '📌 Поиск по выбранным'
        else:
            await c.answer('Неизвестная команда', show_alert=True)
            return

        cancel_event.clear()
        await c.answer('Запускаю…')

        progress_msg = await c.message.answer(
            f'{title}\nПериод: <b>{escape(st.mode)}</b>\nПорядок: <b>{escape(st.order)}</b>\nСтатус: старт…',
            disable_web_page_preview=True,
        )

        last_edit = 0.0
        state = {'dialogs_total': 0, 'chat_index': 0, 'checked': 0, 'matched': 0, 'forwarded': 0, 'floodwait': None, 'chat_name': '—'}

        async def progress_cb(ev: dict[str, Any]):
            nonlocal last_edit
            now = time.time()
            event_type = ev.get('type')

            if event_type == 'init':
                state['dialogs_total'] = ev.get('dialogs_total', 0)
            elif event_type == 'chat_start':
                state['chat_index'] = ev.get('chat_index', 0)
                state['chat_name'] = ev.get('chat_name', '—')
            elif event_type in {'tick', 'forward', 'chat_done', 'done'}:
                state['checked'] = ev.get('checked', state['checked'])
                state['matched'] = ev.get('matched', state['matched'])
                state['forwarded'] = ev.get('forwarded', state['forwarded'])
                if ev.get('chat_name'):
                    state['chat_name'] = ev['chat_name']
            elif event_type == 'floodwait':
                state['floodwait'] = ev.get('seconds')

            heartbeat.beat(
                status='scan_progress',
                event_type=event_type,
                chat_index=state['chat_index'],
                dialogs_total=state['dialogs_total'],
                checked=state['checked'],
                matched=state['matched'],
                forwarded=state['forwarded'],
            )

            if now - last_edit < progress_edit_interval_sec and event_type not in {'done', 'floodwait'}:
                return
            last_edit = now

            fw_line = f'\n⏳ FloodWait: <b>{state["floodwait"]} сек</b>' if state['floodwait'] else ''
            text = (
                f'{title}\n'
                f'Период: <b>{escape(st.mode)}</b>\n'
                f'Порядок: <b>{escape(st.order)}</b>\n'
                f'Чаты: <b>{state["chat_index"]} / {state["dialogs_total"]}</b>\n'
                f'Текущий: <b>{escape(str(state["chat_name"]))}</b>\n'
                f'Проверено: <b>{state["checked"]}</b>\n'
                f'Подошло: <b>{state["matched"]}</b>\n'
                f'Форвард: <b>{state["forwarded"]}</b>'
                f'{fw_line}'
            )
            await safe_edit_text(progress_msg, text)

        async with scan_lock:
            st.last_run_at = int(time.time())
            heartbeat.beat(status='scan_locked', user_id=c.from_user.id)
            stats = await scanner.scan(opts, cancel_event=cancel_event, progress_cb=progress_cb)

        st.last_stats = stats
        st.last_empty_page = 0
        tail = '\n⛔ <b>Остановлено</b>' if stats.get('cancelled') else '\n✅ <b>Завершено</b>'
        await safe_edit_text(progress_msg, _format_report(stats) + tail)
        await c.message.answer('Меню:', reply_markup=_main_kb(is_scanning=False))

    await dp.start_polling(bot, polling_timeout=10, handle_as_tasks=True, close_bot_session=True)
