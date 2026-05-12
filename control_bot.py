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
    last_empty_page: int = 0
    report_period: str = 'all'
    report_page: int = 0
    excluded_page: int = 0


def _is_allowed(user_id: int, allowed: set[int]) -> bool:
    return (not allowed) or (user_id in allowed)


def _mode_label(mode: str) -> str:
    labels = {
        'all': 'всё',
        'month': 'месяц',
        'week': 'неделя',
        'day': 'сутки',
        'new': 'только новые',
    }
    return labels.get(mode, mode)


def _order_label(order: str) -> str:
    labels = {
        'new_to_old': 'новые → старые',
        'old_to_new': 'старые → новые',
    }
    return labels.get(order, order)


def _short_title(name: str, limit: int = 18) -> str:
    name = ' '.join((name or 'Без названия').split())
    return name if len(name) <= limit else f'{name[:limit - 1]}…'


def _main_text(st: UserState, is_scanning: bool) -> str:
    status = 'идёт сканирование' if is_scanning else 'готов к запуску'
    return (
        '🎬 <b>Video Collector</b>\n'
        f'Статус: <b>{status}</b>\n'
        f'Период: <b>{escape(_mode_label(st.mode))}</b> · '
        f'Порядок: <b>{escape(_order_label(st.order))}</b>\n'
        f'Выбрано чатов: <b>{len(st.selected_chats)}</b>\n\n'
        'Главное меню теперь находится на нижней клавиатуре Telegram. '
        'Его можно скрыть обычной стрелкой «назад» на смартфоне.'
    )


def _main_reply_kb(is_scanning: bool) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text='🔎 Все чаты'), KeyboardButton(text='📌 Выбрать')],
        [KeyboardButton(text='⏱ Период'), KeyboardButton(text='🔁 Порядок')],
        [KeyboardButton(text='📊 Отчёты'), KeyboardButton(text='📄 Статус')],
        [KeyboardButton(text='🧹 Пустые'), KeyboardButton(text='🚫 Исключения')],
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
    msg = (
        '📄 <b>Статус</b>\n'
        f'Период: <b>{escape(_mode_label(st.mode))}</b>\n'
        f'Порядок: <b>{escape(_order_label(st.order))}</b>\n'
        f'Выбрано чатов: <b>{len(st.selected_chats)}</b>\n'
        f'Скан сейчас: <b>{"да" if is_scanning else "нет"}</b>\n'
        f'Heartbeat age: <b>{heartbeat.age():.1f} сек</b>'
    )
    if st.last_run_at:
        msg += f'\nПоследний запуск: <b>{time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.last_run_at))}</b>'
    return msg


def _help_text() -> str:
    return (
        'ℹ️ <b>Команды</b>\n'
        '/start — открыть главное меню.\n'
        '/status — показать состояние процесса и heartbeat.\n'
        '/help — краткая справка.\n\n'
        'Главный сценарий: выбери период, выбери чаты или запусти поиск по всем, затем смотри отчёт. '
        'Для безопасной проверки удаления пустых чатов включи <code>DRY_RUN_DELETE=true</code>.'
    )


def _mode_kb(current: str):
    items = [('all', 'Всё'), ('month', 'Месяц'), ('week', 'Неделя'), ('day', 'Сутки'), ('new', 'Новые')]
    kb = InlineKeyboardBuilder()
    for code, label in items:
        mark = '✅ ' if code == current else ''
        kb.button(text=f'{mark}{label}', callback_data=f'mode:set:{code}')
    kb.button(text='⬅️ Назад', callback_data='back:main')
    kb.adjust(2, 2, 1, 1)
    return kb.as_markup()


def _order_kb(current: str):
    items = [('new_to_old', '⬇️ Новые'), ('old_to_new', '⬆️ Старые')]
    kb = InlineKeyboardBuilder()
    for code, label in items:
        mark = '✅ ' if code == current else ''
        kb.button(text=f'{mark}{label}', callback_data=f'order:set:{code}')
    kb.button(text='⬅️ Назад', callback_data='back:main')
    kb.adjust(2, 1)
    return kb.as_markup()


def _pick_text(dialogs: list[dict[str, Any]], selected: set[int], page: int, per_page: int = 20) -> str:
    total = len(dialogs)
    pages = max(1, (total + per_page - 1) // per_page)
    return (
        '📌 <b>Выбор чатов</b>\n'
        f'Страница: <b>{page + 1} / {pages}</b> · всего: <b>{total}</b>\n'
        f'Выбрано: <b>{len(selected)}</b>\n\n'
        'Список сделан в 2 колонки. Можно быстро выбрать или снять всю текущую страницу.'
    )


def _pick_kb(dialogs: list[dict[str, Any]], selected: set[int], page: int, per_page: int = 20):
    kb = InlineKeyboardBuilder()
    start = page * per_page
    chunk = dialogs[start:start + per_page]

    for item in chunk:
        did = item['id']
        mark = '✅' if did in selected else '＋'
        kb.button(text=f'{mark} {_short_title(item.get("name") or "")}', callback_data=f'pick:toggle:{did}')

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
    kb.button(text='🔄 Обновить список', callback_data='pick:refresh')
    kb.button(text='🚀 Старт', callback_data='scan:selected')
    kb.button(text='🧹 Сброс всё', callback_data='pick:clear')
    kb.button(text='⬅️ В меню', callback_data='back:main')
    sizes.extend([2, 2, 2])
    kb.adjust(*sizes)
    return kb.as_markup()


def _empty_item_text(item: dict[str, Any], page: int, total: int) -> str:
    peer_type = item.get('type') or ('личный чат' if item.get('is_user') else 'канал' if item.get('is_channel') else 'группа')
    return (
        f'🧹 <b>Без подходящих видео</b>\n'
        f'#{page + 1} из {total}\n\n'
        f'<b>Название:</b> {escape(item.get("name") or "—")}\n'
        f'<b>ID:</b> <code>{item.get("id")}</code>\n'
        f'<b>Тип:</b> {peer_type}\n'
        f'<b>Проверено сообщений:</b> {item.get("checked", 0)}\n'
        f'<b>Подошло:</b> {item.get("matched", 0)}\n'
        f'<b>Видео найдено:</b> {item.get("video_found", 0)}\n'
        f'<b>Форварднуто:</b> {item.get("forwarded", 0)}'
    )


def _empty_item_kb(item: dict[str, Any], page: int, total: int):
    kb = InlineKeyboardBuilder()
    kb.button(text='🗑 Удалить/выйти', callback_data=f'empty:delete:confirm:{item["id"]}')
    kb.button(text='🚫 Исключить', callback_data=f'empty:exclude:{item["id"]}')
    kb.button(text='❌ Убрать из списка', callback_data=f'empty:forget:{item["id"]}')
    kb.button(text='🧨 Удалить/выйти из всех', callback_data='empty:delete_all:confirm')

    if page > 0:
        kb.button(text='⬅️', callback_data='empty:nav:prev')
    if page + 1 < total:
        kb.button(text='➡️', callback_data='empty:nav:next')

    kb.button(text='⬅️ В меню', callback_data='back:main')
    sizes = []
    sizes.extend([2, 2])
    nav_count = int(page > 0) + int(page + 1 < total)
    if nav_count:
        sizes.append(nav_count)
    sizes.append(1)
    kb.adjust(*sizes)
    return kb.as_markup()


def _confirm_delete_kb(chat_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text='✅ Да, удалить/выйти', callback_data=f'empty:delete:do:{chat_id}')
    kb.button(text='↩️ Отмена', callback_data='empty:list')
    kb.adjust(2)
    return kb.as_markup()


def _confirm_delete_all_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text='✅ Да, удалить/выйти из всех', callback_data='empty:delete_all:do')
    kb.button(text='↩️ Отмена', callback_data='empty:list')
    kb.adjust(2)
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


_REJECT_LABELS = {
    'not_vertical': 'не вертикальные',
    'too_short': 'короткие',
    'too_narrow': 'узкие',
    'too_small': 'маленький размер',
}

def _format_report(stats: Optional[dict[str, Any]]) -> str:
    if not stats:
        return 'Отчёта пока нет.'

    reject_reasons = stats.get('reject_reasons') or {}
    reject_lines = []
    for key, label in _REJECT_LABELS.items():
        value = int(reject_reasons.get(key, 0) or 0)
        if value:
            reject_lines.append(f'  • {label}: <b>{value}</b>')

    top_lines = []
    for item in (stats.get('top_chats') or [])[:5]:
        if not item.get('matched') and not item.get('forwarded'):
            continue
        name = _short_title(str(item.get('name') or item.get('id') or '—'), limit=24)
        top_lines.append(
            f'  • {escape(name)}: подошло <b>{item.get("matched", 0)}</b>, '
            f'отправлено <b>{item.get("forwarded", 0)}</b>'
        )

    lines = [
        '📊 <b>Отчёт сканирования</b>',
        f'Чатов: <b>{stats.get("dialogs", 0)}</b>',
        f'Проверено сообщений: <b>{stats.get("checked", 0)}</b>',
        f'Видео найдено: <b>{stats.get("video_found", stats.get("matched", 0))}</b>',
        f'Подошло видео: <b>{stats.get("matched", 0)}</b>',
        f'Отправлено: <b>{stats.get("forwarded", 0)}</b>',
        f'Пропущено как дубль: <b>{stats.get("skipped_already_forwarded", 0)}</b>',
        f'Ошибок: <b>{stats.get("errors", 0)}</b>',
        f'Пустых чатов: <b>{stats.get("empty_chats_count", 0)}</b>',
        f'Список пустых обновлён: <b>{"да" if stats.get("empty_chats_updated") else "нет"}</b>',
        f'Время: <b>{_format_duration(stats.get("elapsed_sec", 0))}</b>',
        'Статус: ⛔ остановлено' if stats.get('cancelled') else 'Статус: ✅ завершено',
    ]
    if reject_lines:
        lines.extend(['', '🚫 <b>Почему видео не подошли</b>', *reject_lines])
    if top_lines:
        lines.extend(['', '🏆 <b>Лучшие чаты</b>', *top_lines])
    return '\n'.join(lines)


_REPORT_PERIODS = [('all', 'Всё'), ('month', 'Месяц'), ('week', 'Неделя'), ('day', 'Сутки')]
_REPORT_PAGE_SIZE = 8


def _reports_menu_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text='📌 Последний отчёт', callback_data='report:last')
    for code, label in _REPORT_PERIODS:
        kb.button(text=f'📊 {label}', callback_data=f'report:view:{code}:0')
    kb.button(text='🚫 Исключения', callback_data='excluded:list')
    kb.button(text='⬅️ В меню', callback_data='back:main')
    kb.adjust(1, 2, 2, 1, 1)
    return kb.as_markup()


def _reports_menu_text() -> str:
    return (
        '📊 <b>Отчёты</b>\n\n'
        'Здесь есть последний общий отчёт и отдельные списки по периодам: всё, месяц, неделя, сутки. '
        'В строках отчёта показаны названия чатов, а ниже есть кнопки удаления и исключения.'
    )


def _report_rows_text(period: str, rows: list[dict[str, Any]], page: int) -> str:
    total = len(rows)
    pages = max(1, (total + _REPORT_PAGE_SIZE - 1) // _REPORT_PAGE_SIZE)
    start = page * _REPORT_PAGE_SIZE
    chunk = rows[start:start + _REPORT_PAGE_SIZE]
    total_video = sum(int(x.get('video_found', 0) or 0) for x in rows)
    total_matched = sum(int(x.get('matched', 0) or 0) for x in rows)
    total_checked = sum(int(x.get('checked', 0) or 0) for x in rows)
    lines = [
        f'📊 <b>Отчёт: {escape(_mode_label(period))}</b>',
        f'Страница: <b>{page + 1} / {pages}</b> · чатов: <b>{total}</b>',
        f'Проверено сообщений: <b>{total_checked}</b>',
        f'Видео: <b>{total_video}</b> · подошло: <b>{total_matched}</b>',
        '',
    ]
    if not rows:
        lines.append('Данных пока нет. Запусти сканирование за этот период, и бот сохранит статистику по каждому чату.')
        return '\n'.join(lines)
    for num, item in enumerate(chunk, start=start + 1):
        name = escape(str(item.get('name') or item.get('id') or '—'))
        title = name
        lines.append(
            f'{num}. {title}\n'
            f'   видео: <b>{item.get("video_found", 0)}</b> · подошло: <b>{item.get("matched", 0)}</b> · '
            f'отправлено: <b>{item.get("forwarded", 0)}</b> · сообщений: <b>{item.get("checked", 0)}</b>'
        )
    return '\n'.join(lines)


def _report_rows_kb(period: str, rows: list[dict[str, Any]], page: int):
    kb = InlineKeyboardBuilder()
    start = page * _REPORT_PAGE_SIZE
    chunk = rows[start:start + _REPORT_PAGE_SIZE]
    for offset, item in enumerate(chunk, start=1):
        num = start + offset
        chat_id = int(item['id'])
        kb.button(text=f'🗑 {num}', callback_data=f'report:delete:confirm:{period}:{page}:{chat_id}')
        kb.button(text=f'🚫 {num}', callback_data=f'report:exclude:{period}:{page}:{chat_id}')
    sizes = [2] * len(chunk)
    nav_count = 0
    if page > 0:
        kb.button(text='⬅️', callback_data=f'report:view:{period}:{page - 1}')
        nav_count += 1
    if start + _REPORT_PAGE_SIZE < len(rows):
        kb.button(text='➡️', callback_data=f'report:view:{period}:{page + 1}')
        nav_count += 1
    if nav_count:
        sizes.append(nav_count)
    kb.button(text='⬅️ Отчёты', callback_data='reports:menu')
    kb.button(text='🏠 Меню', callback_data='back:main')
    sizes.append(2)
    kb.adjust(*sizes)
    return kb.as_markup()


def _excluded_text(items: list[dict[str, Any]], page: int) -> str:
    total = len(items)
    pages = max(1, (total + _REPORT_PAGE_SIZE - 1) // _REPORT_PAGE_SIZE)
    start = page * _REPORT_PAGE_SIZE
    chunk = items[start:start + _REPORT_PAGE_SIZE]
    lines = [f'🚫 <b>Исключённые чаты</b>', f'Страница: <b>{page + 1} / {pages}</b> · всего: <b>{total}</b>', '']
    if not items:
        lines.append('Список пуст. Нажимай «Исключить» в отчётах или списке пустых, чтобы чат больше не проверялся.')
        return '\n'.join(lines)
    for num, item in enumerate(chunk, start=start + 1):
        name = escape(str(item.get('name') or item.get('id') or '—'))
        title = name
        lines.append(f'{num}. {title} · <code>{item.get("id")}</code>')
    return '\n'.join(lines)


def _excluded_kb(items: list[dict[str, Any]], page: int):
    kb = InlineKeyboardBuilder()
    start = page * _REPORT_PAGE_SIZE
    chunk = items[start:start + _REPORT_PAGE_SIZE]
    for offset, item in enumerate(chunk, start=1):
        num = start + offset
        kb.button(text=f'↩️ Вернуть {num}', callback_data=f'excluded:restore:{page}:{int(item["id"])}')
    sizes = [1] * len(chunk)
    nav_count = 0
    if page > 0:
        kb.button(text='⬅️', callback_data=f'excluded:page:{page - 1}')
        nav_count += 1
    if start + _REPORT_PAGE_SIZE < len(items):
        kb.button(text='➡️', callback_data=f'excluded:page:{page + 1}')
        nav_count += 1
    if nav_count:
        sizes.append(nav_count)
    kb.button(text='⬅️ Отчёты', callback_data='reports:menu')
    kb.button(text='🏠 Меню', callback_data='back:main')
    sizes.append(2)
    kb.adjust(*sizes)
    return kb.as_markup()


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
        empty = await scanner.get_saved_empty_chats()
        if st.last_stats is None:
            st.last_stats = {}
        st.last_stats['empty_chats'] = empty
        st.last_stats['empty_chats_count'] = len(empty)
        if not empty:
            await safe_edit_text(message, 'Список пустых чатов пока пуст.')
            return
        st.last_empty_page = max(0, min(st.last_empty_page, len(empty) - 1))
        item = empty[st.last_empty_page]
        await safe_edit_text(message, _empty_item_text(item, st.last_empty_page, len(empty)), reply_markup=_empty_item_kb(item, st.last_empty_page, len(empty)))

    async def render_period_report(message: Message, st: UserState, period: str, page: int):
        rows = await scanner.get_period_report(period)
        pages = max(1, (len(rows) + _REPORT_PAGE_SIZE - 1) // _REPORT_PAGE_SIZE)
        page = max(0, min(page, pages - 1))
        st.report_period = period
        st.report_page = page
        await safe_edit_text(message, _report_rows_text(period, rows, page), reply_markup=_report_rows_kb(period, rows, page))

    async def render_excluded(message: Message, st: UserState, page: int):
        items = await scanner.get_excluded_chats()
        pages = max(1, (len(items) + _REPORT_PAGE_SIZE - 1) // _REPORT_PAGE_SIZE)
        page = max(0, min(page, pages - 1))
        st.excluded_page = page
        await safe_edit_text(message, _excluded_text(items, page), reply_markup=_excluded_kb(items, page))

    @dp.message(F.text == '/start')
    async def start(m: Message):
        if not _is_allowed(m.from_user.id, allowed_users):
            return
        st = await get_state(m.from_user.id)
        heartbeat.beat(status='menu_open', user_id=m.from_user.id)
        await m.answer(_main_text(st, scan_lock.locked()), reply_markup=_main_reply_kb(is_scanning=scan_lock.locked()))

    @dp.message(F.text == '/help')
    async def help_command(m: Message):
        if not _is_allowed(m.from_user.id, allowed_users):
            return
        await m.answer(_help_text(), reply_markup=_main_reply_kb(is_scanning=scan_lock.locked()))

    @dp.message(F.text == '/status')
    async def status_command(m: Message):
        if not _is_allowed(m.from_user.id, allowed_users):
            return
        st = await get_state(m.from_user.id)
        await m.answer(_status_text(st, heartbeat, scan_lock.locked()), reply_markup=_main_reply_kb(scan_lock.locked()))

    @dp.message(F.text.in_({'📌 Выбрать', '⏱ Период', '🔁 Порядок', '📊 Отчёты', '🧹 Пустые', '🚫 Исключения', '📄 Статус'}))
    async def main_keyboard_buttons(m: Message):
        if not _is_allowed(m.from_user.id, allowed_users):
            return
        st = await get_state(m.from_user.id)
        text = m.text
        if text == '📌 Выбрать':
            if not st.dialogs_cache:
                await m.answer('Гружу список чатов…', reply_markup=_main_reply_kb(scan_lock.locked()))
                st.dialogs_cache = await scanner.list_dialogs()
                st.dialogs_cache.sort(key=lambda x: (x.get('name') or '').lower())
            st.page = 0
            await m.answer(
                _pick_text(st.dialogs_cache, st.selected_chats, st.page),
                reply_markup=_pick_kb(st.dialogs_cache, st.selected_chats, st.page),
            )
        elif text == '⏱ Период':
            await m.answer('⏱ <b>Период сканирования</b>\nВыбери, какие сообщения проверять:', reply_markup=_mode_kb(st.mode))
        elif text == '🔁 Порядок':
            await m.answer('🔁 <b>Порядок сканирования</b>\nС какой стороны читать историю:', reply_markup=_order_kb(st.order))
        elif text == '📊 Отчёты':
            await m.answer(_reports_menu_text(), reply_markup=_reports_menu_kb())
        elif text == '🧹 Пустые':
            msg = await m.answer('Открываю список пустых чатов…')
            await render_empty_list(msg, st)
        elif text == '🚫 Исключения':
            msg = await m.answer('Открываю исключения…')
            await render_excluded(msg, st, st.excluded_page)
        elif text == '📄 Статус':
            await m.answer(_status_text(st, heartbeat, scan_lock.locked()), reply_markup=_main_reply_kb(scan_lock.locked()))

    @dp.callback_query(F.data == 'back:main')
    async def back_main(c: CallbackQuery):
        if not _is_allowed(c.from_user.id, allowed_users):
            return
        st = await get_state(c.from_user.id)
        heartbeat.beat(status='back_main', user_id=c.from_user.id)
        await safe_edit_text(c.message, '🏠 Главное меню открыто на нижней клавиатуре Telegram.')
        await c.message.answer(_main_text(st, scan_lock.locked()), reply_markup=_main_reply_kb(is_scanning=scan_lock.locked()))
        await c.answer()

    @dp.callback_query(F.data == 'mode:open')
    async def mode_open(c: CallbackQuery):
        if not _is_allowed(c.from_user.id, allowed_users):
            return
        st = await get_state(c.from_user.id)
        await safe_edit_text(c.message, '⏱ <b>Период сканирования</b>\nВыбери, какие сообщения проверять:', reply_markup=_mode_kb(st.mode))
        await c.answer()

    @dp.callback_query(F.data.startswith('mode:set:'))
    async def mode_set(c: CallbackQuery):
        if not _is_allowed(c.from_user.id, allowed_users):
            return
        st = await get_state(c.from_user.id)
        st.mode = c.data.split(':')[-1]
        await safe_edit_text(c.message, f'⏱ Период: <b>{escape(_mode_label(st.mode))}</b>', reply_markup=_mode_kb(st.mode))
        await c.answer('Ок')

    @dp.callback_query(F.data == 'order:open')
    async def order_open(c: CallbackQuery):
        if not _is_allowed(c.from_user.id, allowed_users):
            return
        st = await get_state(c.from_user.id)
        await safe_edit_text(c.message, '🔁 <b>Порядок сканирования</b>\nС какой стороны читать историю:', reply_markup=_order_kb(st.order))
        await c.answer()

    @dp.callback_query(F.data.startswith('order:set:'))
    async def order_set(c: CallbackQuery):
        if not _is_allowed(c.from_user.id, allowed_users):
            return
        st = await get_state(c.from_user.id)
        st.order = c.data.split(':')[-1]
        await safe_edit_text(c.message, f'🔁 Порядок: <b>{escape(_order_label(st.order))}</b>', reply_markup=_order_kb(st.order))
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
            _pick_text(st.dialogs_cache, st.selected_chats, st.page),
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
        await safe_edit_text(
            c.message,
            _pick_text(st.dialogs_cache, st.selected_chats, st.page),
            reply_markup=_pick_kb(st.dialogs_cache, st.selected_chats, st.page),
        )
        await c.answer()

    @dp.callback_query(F.data == 'pick:clear')
    async def pick_clear(c: CallbackQuery):
        if not _is_allowed(c.from_user.id, allowed_users):
            return
        st = await get_state(c.from_user.id)
        st.selected_chats.clear()
        await safe_edit_text(
            c.message,
            _pick_text(st.dialogs_cache, st.selected_chats, st.page),
            reply_markup=_pick_kb(st.dialogs_cache, st.selected_chats, st.page),
        )
        await c.answer('Очищено')

    @dp.callback_query(F.data == 'pick:refresh')
    async def pick_refresh(c: CallbackQuery):
        if not _is_allowed(c.from_user.id, allowed_users):
            return
        st = await get_state(c.from_user.id)
        await c.answer('Обновляю список…', show_alert=False)
        old_selected = set(st.selected_chats)
        st.dialogs_cache = await scanner.list_dialogs()
        st.dialogs_cache.sort(key=lambda x: (x.get('name') or '').lower())
        existing_ids = {int(x['id']) for x in st.dialogs_cache}
        st.selected_chats.intersection_update(existing_ids)
        st.page = 0
        removed = len(old_selected) - len(st.selected_chats)
        await safe_edit_text(
            c.message,
            _pick_text(st.dialogs_cache, st.selected_chats, st.page) + (f'\n\nУбрано недоступных выбранных: <b>{removed}</b>' if removed else ''),
            reply_markup=_pick_kb(st.dialogs_cache, st.selected_chats, st.page),
        )

    @dp.callback_query(F.data.in_({'pick:page_select', 'pick:page_clear'}))
    async def pick_page_bulk(c: CallbackQuery):
        if not _is_allowed(c.from_user.id, allowed_users):
            return
        st = await get_state(c.from_user.id)
        start = st.page * 20
        page_ids = {int(x['id']) for x in st.dialogs_cache[start:start + 20]}
        if c.data == 'pick:page_select':
            st.selected_chats.update(page_ids)
            answer = f'Выбрано на странице: {len(page_ids)}'
        else:
            st.selected_chats.difference_update(page_ids)
            answer = f'Снято на странице: {len(page_ids)}'
        await safe_edit_text(
            c.message,
            _pick_text(st.dialogs_cache, st.selected_chats, st.page),
            reply_markup=_pick_kb(st.dialogs_cache, st.selected_chats, st.page),
        )
        await c.answer(answer)

    @dp.callback_query(F.data.startswith('pick:page:'))
    async def pick_page(c: CallbackQuery):
        if not _is_allowed(c.from_user.id, allowed_users):
            return
        st = await get_state(c.from_user.id)
        direction = c.data.split(':')[-1]
        pages = max(1, (len(st.dialogs_cache) + 19) // 20)
        if direction == 'next' and st.page + 1 < pages:
            st.page += 1
        elif direction == 'prev' and st.page > 0:
            st.page -= 1
        await safe_edit_text(
            c.message,
            _pick_text(st.dialogs_cache, st.selected_chats, st.page),
            reply_markup=_pick_kb(st.dialogs_cache, st.selected_chats, st.page),
        )
        await c.answer()

    @dp.callback_query(F.data == 'status')
    async def status(c: CallbackQuery):
        if not _is_allowed(c.from_user.id, allowed_users):
            return
        st = await get_state(c.from_user.id)
        await c.answer()
        await safe_edit_text(c.message, _status_text(st, heartbeat, scan_lock.locked()))

    @dp.callback_query(F.data == 'reports:menu')
    async def reports_menu(c: CallbackQuery):
        if not _is_allowed(c.from_user.id, allowed_users):
            return
        await c.answer()
        await safe_edit_text(c.message, _reports_menu_text(), reply_markup=_reports_menu_kb())

    @dp.callback_query(F.data == 'report:last')
    async def report_last(c: CallbackQuery):
        if not _is_allowed(c.from_user.id, allowed_users):
            return
        st = await get_state(c.from_user.id)
        await c.answer()
        await safe_edit_text(c.message, _format_report(st.last_stats), reply_markup=_reports_menu_kb())

    @dp.callback_query(F.data.startswith('report:view:'))
    async def report_view(c: CallbackQuery):
        if not _is_allowed(c.from_user.id, allowed_users):
            return
        st = await get_state(c.from_user.id)
        _, _, period, page_raw = c.data.split(':')
        await render_period_report(c.message, st, period, int(page_raw))
        await c.answer()

    @dp.callback_query(F.data.startswith('report:exclude:'))
    async def report_exclude(c: CallbackQuery):
        if not _is_allowed(c.from_user.id, allowed_users):
            return
        st = await get_state(c.from_user.id)
        _, _, period, page_raw, chat_id_raw = c.data.split(':')
        rows = await scanner.get_period_report(period)
        chat_id = int(chat_id_raw)
        item = next((x for x in rows if int(x['id']) == chat_id), {'id': chat_id, 'name': str(chat_id)})
        await scanner.exclude_chat(item)
        await render_period_report(c.message, st, period, int(page_raw))
        await c.answer('Чат исключён из будущих проверок')

    @dp.callback_query(F.data.startswith('report:delete:confirm:'))
    async def report_delete_confirm(c: CallbackQuery):
        if not _is_allowed(c.from_user.id, allowed_users):
            return
        _, _, _, period, page_raw, chat_id_raw = c.data.split(':')
        text = (
            '⚠️ <b>Подтверди удаление/выход</b>\n\n'
            f'ID: <code>{int(chat_id_raw)}</code>\n\n'
            'Это удалит диалог из аккаунта и выйдет из канала/группы, если Telegram это разрешает.'
        )
        kb = InlineKeyboardBuilder()
        kb.button(text='✅ Да, удалить/выйти', callback_data=f'report:delete:do:{period}:{page_raw}:{chat_id_raw}')
        kb.button(text='↩️ Отмена', callback_data=f'report:view:{period}:{page_raw}')
        kb.adjust(2)
        await safe_edit_text(c.message, text, reply_markup=kb.as_markup())
        await c.answer()

    @dp.callback_query(F.data.startswith('report:delete:do:'))
    async def report_delete_do(c: CallbackQuery):
        if not _is_allowed(c.from_user.id, allowed_users):
            return
        st = await get_state(c.from_user.id)
        _, _, _, period, page_raw, chat_id_raw = c.data.split(':')
        ok, info = await scanner.delete_dialog_by_id(int(chat_id_raw))
        await render_period_report(c.message, st, period, int(page_raw))
        await c.answer('Удалено' if ok else f'Ошибка: {info}', show_alert=not ok)

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

    @dp.callback_query(F.data.startswith('empty:exclude:'))
    async def empty_exclude(c: CallbackQuery):
        if not _is_allowed(c.from_user.id, allowed_users):
            return
        st = await get_state(c.from_user.id)
        chat_id = int(c.data.split(':')[-1])
        empty = await scanner.get_saved_empty_chats()
        item = next((x for x in empty if int(x.get('id', 0)) == chat_id), {'id': chat_id, 'name': str(chat_id)})
        await scanner.exclude_chat(item)
        remaining = await scanner.get_saved_empty_chats()
        if st.last_empty_page >= len(remaining) and st.last_empty_page > 0:
            st.last_empty_page -= 1
        await render_empty_list(c.message, st)
        await c.answer('Чат исключён из будущих проверок')

    @dp.callback_query(F.data.startswith('empty:forget:'))
    async def empty_forget(c: CallbackQuery):
        if not _is_allowed(c.from_user.id, allowed_users):
            return
        st = await get_state(c.from_user.id)
        chat_id = int(c.data.split(':')[-1])
        await scanner.forget_empty_chat(chat_id)
        empty = await scanner.get_saved_empty_chats()
        if st.last_stats is None:
            st.last_stats = {}
        st.last_stats['empty_chats'] = empty
        st.last_stats['empty_chats_count'] = len(empty)
        if st.last_empty_page >= len(empty) and st.last_empty_page > 0:
            st.last_empty_page -= 1
        await render_empty_list(c.message, st)
        await c.answer('Убрано из списка')

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
            await scanner.forget_empty_chat(chat_id)
            empty = await scanner.get_saved_empty_chats()
            if st.last_stats is None:
                st.last_stats = {}
            st.last_stats['empty_chats'] = empty
            st.last_stats['empty_chats_count'] = len(empty)
            if st.last_empty_page >= len(st.last_stats['empty_chats']) and st.last_empty_page > 0:
                st.last_empty_page -= 1
            if st.last_stats['empty_chats']:
                await render_empty_list(c.message, st)
            else:
                await safe_edit_text(c.message, f'✅ {escape(info)}', reply_markup=None)
        else:
            await c.message.answer(f'Не удалось удалить: {escape(info)}')
        await c.answer('Готово' if ok else 'Ошибка')

    @dp.callback_query(F.data == 'empty:delete_all:confirm')
    async def empty_delete_all_confirm(c: CallbackQuery):
        if not _is_allowed(c.from_user.id, allowed_users):
            return
        empty = await scanner.get_saved_empty_chats()
        if not empty:
            await c.answer('Список пуст.', show_alert=False)
            return
        text = (
            f'⚠️ <b>Подтверди массовое удаление/выход</b>\n\n'
            f'Будет обработано чатов: <b>{len(empty)}</b>.\n'
            f'Это удалит диалоги из аккаунта и выйдет из чатов, если Telegram это разрешает.'
        )
        await safe_edit_text(c.message, text, reply_markup=_confirm_delete_all_kb())
        await c.answer()

    @dp.callback_query(F.data == 'empty:delete_all:do')
    async def empty_delete_all_do(c: CallbackQuery):
        if not _is_allowed(c.from_user.id, allowed_users):
            return
        st = await get_state(c.from_user.id)
        empty = await scanner.get_saved_empty_chats()
        if not empty:
            await c.answer('Список пуст.', show_alert=False)
            return
        await c.answer('Удаляю…', show_alert=False)
        progress_msg = c.message
        deleted = 0
        failed: list[str] = []
        for idx, item in enumerate(list(empty), start=1):
            chat_id = int(item['id'])
            await safe_edit_text(
                progress_msg,
                f'🧨 Массовое удаление/выход\nЧат: <b>{idx} / {len(empty)}</b>\nУдалено: <b>{deleted}</b>\nОшибок: <b>{len(failed)}</b>',
            )
            ok, info = await scanner.delete_dialog_by_id(chat_id)
            if ok:
                deleted += 1
                await scanner.forget_empty_chat(chat_id)
            else:
                failed.append(f'{item.get("name") or chat_id}: {info}')
        remaining = await scanner.get_saved_empty_chats()
        if st.last_stats is None:
            st.last_stats = {}
        st.last_stats['empty_chats'] = remaining
        st.last_stats['empty_chats_count'] = len(remaining)
        st.last_empty_page = 0
        fail_text = ''
        if failed:
            fail_text = '\n\nНе удалось:\n' + '\n'.join(f'• {escape(x)}' for x in failed[:10])
            if len(failed) > 10:
                fail_text += f'\n…и ещё {len(failed) - 10}'
        await safe_edit_text(
            progress_msg,
            f'✅ Массовое удаление/выход завершено.\nУдалено: <b>{deleted}</b>\nОсталось в списке: <b>{len(remaining)}</b>{fail_text}',
            reply_markup=None,
        )

    @dp.callback_query(F.data == 'excluded:list')
    async def excluded_list(c: CallbackQuery):
        if not _is_allowed(c.from_user.id, allowed_users):
            return
        st = await get_state(c.from_user.id)
        await render_excluded(c.message, st, st.excluded_page)
        await c.answer()

    @dp.callback_query(F.data.startswith('excluded:page:'))
    async def excluded_page(c: CallbackQuery):
        if not _is_allowed(c.from_user.id, allowed_users):
            return
        st = await get_state(c.from_user.id)
        page = int(c.data.split(':')[-1])
        await render_excluded(c.message, st, page)
        await c.answer()

    @dp.callback_query(F.data.startswith('excluded:restore:'))
    async def excluded_restore(c: CallbackQuery):
        if not _is_allowed(c.from_user.id, allowed_users):
            return
        st = await get_state(c.from_user.id)
        _, _, page_raw, chat_id_raw = c.data.split(':')
        await scanner.restore_excluded_chat(int(chat_id_raw))
        await render_excluded(c.message, st, int(page_raw))
        await c.answer('Чат возвращён в проверки')

    async def start_scan(message: Message, user_id: int, st: UserState, kind: str, notify):
        if scan_lock.locked():
            await notify('Скан уже идёт. Нажми ⛔ Стоп.', True)
            return

        if kind == 'allchats':
            opts = ScanOptions(mode=st.mode, chat_ids=None, order=st.order)
            title = '🔎 Поиск во всех'
        elif kind == 'selected':
            if not st.selected_chats:
                await notify('Сначала выбери чаты.', True)
                return
            opts = ScanOptions(mode=st.mode, chat_ids=set(st.selected_chats), order=st.order)
            title = '📌 Поиск по выбранным'
        else:
            await notify('Неизвестная команда', True)
            return

        cancel_event.clear()
        await notify('Запускаю…', False)

        progress_msg = await message.answer(
            f'{title}\nПериод: <b>{escape(_mode_label(st.mode))}</b>\nПорядок: <b>{escape(_order_label(st.order))}</b>\nСтатус: старт…',
            disable_web_page_preview=True,
            reply_markup=_scan_progress_kb(),
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
                f'Период: <b>{escape(_mode_label(st.mode))}</b>\n'
                f'Порядок: <b>{escape(_order_label(st.order))}</b>\n'
                f'Чаты: <b>{state["chat_index"]} / {state["dialogs_total"]}</b>\n'
                f'Текущий: <b>{escape(str(state["chat_name"]))}</b>\n'
                f'Проверено: <b>{state["checked"]}</b>\n'
                f'Подошло: <b>{state["matched"]}</b>\n'
                f'Отправлено: <b>{state["forwarded"]}</b>'
                f'{fw_line}'
            )
            await safe_edit_text(progress_msg, text, reply_markup=_scan_progress_kb())

        origin_message = message

        await scan_lock.acquire()

        async def run_scan_task() -> None:
            try:
                st.last_run_at = int(time.time())
                heartbeat.beat(status='scan_locked', user_id=user_id)
                stats = await scanner.scan(opts, cancel_event=cancel_event, progress_cb=progress_cb)
            except asyncio.CancelledError:
                heartbeat.beat(status='scan_task_cancelled', user_id=user_id)
                raise
            except Exception as e:
                log.exception('scan task failed')
                stats = {
                    'dialogs': state['dialogs_total'],
                    'checked': state['checked'],
                    'video_found': 0,
                    'matched': state['matched'],
                    'forwarded': state['forwarded'],
                    'skipped_already_forwarded': 0,
                    'reject_reasons': {},
                    'top_chats': [],
                    'errors': 1,
                    'empty_chats_count': 0,
                    'empty_chats_updated': False,
                    'cancelled': True,
                    'elapsed_sec': int(time.time() - st.last_run_at) if st.last_run_at else 0,
                }
                heartbeat.beat(status='scan_failed', user_id=user_id, error=e.__class__.__name__)
                try:
                    await progress_msg.answer(f'❌ Скан упал с ошибкой: <code>{escape(e.__class__.__name__)}</code>')
                except Exception:
                    log.exception('failed to notify about scan failure')
            finally:
                if scan_lock.locked():
                    scan_lock.release()

            st.last_stats = stats
            st.last_empty_page = 0
            tail = '\n⛔ <b>Остановлено</b>' if stats.get('cancelled') else '\n✅ <b>Завершено</b>'
            await safe_edit_text(progress_msg, _format_report(stats) + tail)
            try:
                await origin_message.answer(_main_text(st, False), reply_markup=_main_reply_kb(is_scanning=False))
            except Exception:
                log.exception('failed to send menu after scan')

        asyncio.create_task(run_scan_task(), name=f'video-collector-scan-{user_id}')


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

    @dp.message(F.text == '⛔ Стоп')
    async def scan_stop_message(m: Message):
        if not _is_allowed(m.from_user.id, allowed_users):
            return
        if not scan_lock.locked():
            await m.answer('Скан сейчас не идёт.', reply_markup=_main_reply_kb(False))
            return
        cancel_event.set()
        heartbeat.beat(status='scan_stop_requested', user_id=m.from_user.id)
        await m.answer('⛔ Останавливаю сканирование…', reply_markup=_main_reply_kb(True))

    @dp.callback_query(F.data.startswith('scan:'))
    async def scan_handler(c: CallbackQuery):
        if not _is_allowed(c.from_user.id, allowed_users):
            return
        kind = c.data.split(':')[-1]
        if kind == 'stop':
            return
        st = await get_state(c.from_user.id)

        async def notify(text: str, show_alert: bool = False):
            await c.answer(text, show_alert=show_alert)

        await start_scan(c.message, c.from_user.id, st, kind, notify)

    @dp.message(F.text.in_(('🔎 Все чаты', '🚀 Старт выбранных')))
    async def scan_message_handler(m: Message):
        if not _is_allowed(m.from_user.id, allowed_users):
            return
        st = await get_state(m.from_user.id)
        kind = 'allchats' if m.text == '🔎 Все чаты' else 'selected'

        async def notify(text: str, show_alert: bool = False):
            await m.answer(text, reply_markup=_main_reply_kb(scan_lock.locked()))

        await start_scan(m, m.from_user.id, st, kind, notify)

    await dp.start_polling(bot, polling_timeout=10, handle_as_tasks=True, close_bot_session=True)
