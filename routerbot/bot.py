"""Telegram-бот для управления TP-Link Archer C80."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.filters import BaseFilter, Command, CommandObject, CommandStart
from aiogram.types import BotCommand, CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv
from tplinkrouterc6u import Connection
from tplinkrouterc6u.common.exception import ClientException

from .c80_client import (
    DEFAULT_DOWN_LIMIT_KBPS,
    DEFAULT_UP_LIMIT_KBPS,
    ArcherC80Client,
    ClientDevice,
    RouterCommandError,
    find_device,
    validate_ip,
    validate_psk,
    validate_ssid,
)
from .config import ConfigError, load_settings
from .formatting import (
    CONNECTION_NAMES,
    format_clients,
    format_ddns,
    format_device,
    format_device_detail,
    format_keys_alert,
    format_logs,
    format_ports,
    format_reservations,
    format_stats,
    format_status,
    format_wifi_view,
    fmt_speed_limit,
)
from .scheduler import ACTIONS, TaskStore, parse_days, parse_time, scheduler_loop
from .service import RouterService
from .wol import send_wol

log = logging.getLogger(__name__)

BOT: Router = Router()

# Диапазоны для /setssid и /setpass
BANDS: dict[str, Connection] = {
    '2': Connection.HOST_2G, '2g': Connection.HOST_2G, '24': Connection.HOST_2G, '2.4': Connection.HOST_2G,
    '5': Connection.HOST_5G, '5g': Connection.HOST_5G,
    'g2': Connection.GUEST_2G, 'g2g': Connection.GUEST_2G, 'guest2': Connection.GUEST_2G, 'г2': Connection.GUEST_2G,
    'g5': Connection.GUEST_5G, 'g5g': Connection.GUEST_5G, 'guest5': Connection.GUEST_5G, 'г5': Connection.GUEST_5G,
}

TOGGLEABLE = {
    c.value: c
    for c in (Connection.HOST_2G, Connection.HOST_5G, Connection.GUEST_2G, Connection.GUEST_5G)
}

MENU_TEXT = (
    '🤖 <b>Бот управления Archer C80</b>\n\n'
    '<b>📡 Основное</b>\n'
    '/status — состояние роутера\n'
    '/wifi — Wi-Fi: вкл/выкл, пароли, SSID\n'
    '/clients — устройства: блок, лимит, WoL, закреп IP\n'
    '/stats — трафик по устройствам\n'
    '/reboot — перезагрузка\n\n'
    '<b>🛠 Сеть</b>\n'
    '/reservations — статические IP (MAC → IP)\n'
    '/reserve &lt;mac/имя&gt; &lt;ip&gt; [имя] — закрепить IP\n'
    '/wol &lt;mac/имя&gt; — разбудить устройство\n'
    '/limit &lt;mac/имя&gt; &lt;Мбит&gt; [отдача] — лимит скорости\n'
    '/ports — UPnP и DMZ\n'
    '/ddns — динамический DNS\n'
    '/logs — журнал роутера\n\n'
    '<b>⏰ Расписание</b>\n'
    '/task — задачи по времени (напр. /task add reboot 04:00)\n\n'
    '<b>✏️ Wi-Fi настройки</b>\n'
    '/setssid 2g|5g|g2g|g5g &lt;имя&gt;\n'
    '/setpass 2g|5g|g2g|g5g &lt;пароль&gt;\n\n'
    '/help — это меню'
)

# Пресеты лимита скорости (загрузка, отдача) в Мбит/с
LIMIT_PRESETS_MBPS = ((5, 2), (20, 10), (50, 25))


# ------------------------------------------------------------------ фильтры

class AdminOnly(BaseFilter):
    async def __call__(self, event: Message | CallbackQuery, admin_ids: set[int]) -> bool:
        user = getattr(event, 'from_user', None)
        return bool(user) and user.id in admin_ids


# ------------------------------------------------------------------ helpers

async def _edit_safe(message: Message, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as exc:
        if 'message is not modified' not in str(exc).lower():
            raise


def _err_text(exc: Exception) -> str:
    from html import escape
    return (
        '❌ <b>Не удалось выполнить операцию.</b>\n'
        f'<code>{escape(str(exc))}</code>\n\n'
        'Проверь, что роутер на связи, пароль в .env верный и никто не сидит '
        'в веб-интерфейсе роутера одновременно с ботом (одна сессия!).'
    )


# ------------------------------------------------------------------- меню

def kb_main() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text='📊 Статус', callback_data='status')
    kb.button(text='📶 Wi-Fi', callback_data='wifi')
    kb.button(text='👥 Устройства', callback_data='clients')
    kb.button(text='📈 Статистика', callback_data='stats')
    kb.button(text='♻️ Перезагрузка', callback_data='reboot')
    kb.adjust(2, 2, 1)
    return kb.as_markup()


def kb_wifi(configs: dict | None = None) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    labels = {
        Connection.HOST_2G: '2.4 ГГц',
        Connection.HOST_5G: '5 ГГц',
        Connection.GUEST_2G: 'Гость 2.4',
        Connection.GUEST_5G: 'Гость 5',
    }
    for conn, label in labels.items():
        state = ''
        if configs and conn in configs and configs[conn].enabled is not None:
            state = '🟢→выкл' if configs[conn].enabled else '⚪→вкл'
        kb.button(text=f'{label} {state}'.strip(), callback_data=f'wifi:toggle:{conn.value}')
    kb.button(text='🔑 Показать пароли', callback_data='wifi:keys')
    kb.button(text='🔄 Обновить', callback_data='wifi')
    kb.button(text='⬅️ Меню', callback_data='menu')
    kb.adjust(2, 2, 2, 1)
    return kb.as_markup()


def kb_reboot() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text='✅ Да, перезагрузить', callback_data='reboot:yes')
    kb.button(text='❌ Отмена', callback_data='menu')
    kb.adjust(2)
    return kb.as_markup()


async def show_wifi(message: Message, service: RouterService) -> None:
    def job(c: ArcherC80Client) -> dict[Connection, Any]:
        return {conn: c.get_wifi_config(conn) for conn in TOGGLEABLE.values()}

    try:
        configs = await service.run(job)
    except ClientException as exc:
        await _edit_safe(message, _err_text(exc), kb_main())
        return
    await _edit_safe(message, format_wifi_view(configs), kb_wifi(configs))


async def show_clients(message: Message, service: RouterService) -> None:
    try:
        devices = await service.run(lambda c: c.get_devices())
    except ClientException as exc:
        await _edit_safe(message, _err_text(exc), kb_main())
        return
    await _edit_safe(message, format_clients(devices), kb_clients(devices))


def kb_clients(devices: list[ClientDevice], max_buttons: int = 12) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    shown = 0
    for d in devices:
        if shown >= max_buttons:
            break
        mark = '🔒' if d.blocked else ('⛔' if d.speed_limited else ('🟢' if d.online else '⚪️'))
        short = (d.name or d.mac)[:22]
        kb.button(text=f'{mark} {short}', callback_data=f'dev:{d.mac}')
        shown += 1
    kb.button(text='🔄 Обновить', callback_data='clients')
    kb.button(text='⬅️ Меню', callback_data='menu')
    kb.adjust(*([1] * shown), 2)
    return kb.as_markup()


def kb_device(d: ClientDevice) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    widths: list[int] = []

    first = 0
    if d.blocked:
        kb.button(text='🔓 Разблокировать', callback_data=f'unblk:{d.mac}')
        first += 1
    elif d.online:
        kb.button(text='🔒 Заблокировать', callback_data=f'blk:ask:{d.mac}')
        first += 1
    if d.ip and not d.blocked:
        kb.button(text='📌 Закрепить IP', callback_data=f'resv:{d.mac}')
        first += 1
    if not d.online:
        kb.button(text='⚡ Разбудить (WoL)', callback_data=f'wol:{d.mac}')
        first += 1
    if first:
        widths.append(first)

    if d.online and not d.blocked:
        for down_m, up_m in LIMIT_PRESETS_MBPS:
            kb.button(
                text=f'⛔ {down_m} Мбит',
                callback_data=f'lim:{d.mac}:{down_m * 1024}:{up_m * 1024}',
            )
        widths.append(len(LIMIT_PRESETS_MBPS))
    if d.speed_limited:
        kb.button(text='♾️ Снять лимит', callback_data=f'limrm:{d.mac}')
        widths.append(1)
    kb.button(text='🔄 Обновить', callback_data=f'dev:{d.mac}')
    kb.button(text='⬅️ Список', callback_data='clients')
    widths.append(2)
    kb.adjust(*widths)
    return kb.as_markup()


async def show_device(message: Message, service: RouterService, mac: str) -> None:
    try:
        devices = await service.run(lambda c: c.get_devices())
    except ClientException as exc:
        await _edit_safe(message, _err_text(exc), kb_main())
        return
    device = next((d for d in devices if d.mac == mac), None)
    if device is None:
        await message.answer(f'❔ Устройство <code>{mac}</code> пропало из списка роутера.')
        await _edit_safe(message, format_clients(devices), kb_clients(devices))
        return
    await _edit_safe(message, format_device_detail(device), kb_device(device))


# ------------------------------------------------------------------ /start

@BOT.message(CommandStart())
@BOT.message(Command('help'))
async def cmd_start(message: Message, admin_ids: set[int]) -> None:
    user = message.from_user
    if user and user.id in admin_ids:
        await message.answer(MENU_TEXT, reply_markup=kb_main())
    else:
        uid = user.id if user else '—'
        await message.answer(
            '⛔ <b>Доступ запрещён.</b>\n\n'
            f'Твой Telegram ID: <code>{uid}</code>\n'
            'Добавь его в .env как <code>ADMIN_IDS=...</code> и перезапусти бота.'
        )


# ------------------------------------------------------------------ /status

async def _status_text(service: RouterService) -> str:
    def job(c: ArcherC80Client) -> str:
        fw = c.get_firmware()
        status = c.get_status()
        try:
            ipv4 = c.get_ipv4_status()
        except Exception:
            ipv4 = None
        try:
            wan_speed = c.get_wan_speed()
        except Exception:
            wan_speed = None
        return format_status(fw, status, ipv4, wan_speed)
    return await service.run(job)


@BOT.message(AdminOnly(), Command('status'))
async def cmd_status(message: Message, service: RouterService) -> None:
    wait = await message.answer('⏳ Опрашиваю роутер…')
    try:
        text = await _status_text(service)
    except ClientException as exc:
        text = _err_text(exc)
    await _edit_safe(wait, text, kb_main())


@BOT.callback_query(AdminOnly(), F.data == 'status')
async def cb_status(call: CallbackQuery, service: RouterService) -> None:
    await call.answer('Опрашиваю роутер…')
    try:
        text = await _status_text(service)
    except ClientException as exc:
        text = _err_text(exc)
    await _edit_safe(call.message, text, kb_main())


# ----------------------------------------------------------------- меню nav

@BOT.callback_query(AdminOnly(), F.data == 'menu')
async def cb_menu(call: CallbackQuery) -> None:
    await call.answer()
    await _edit_safe(call.message, MENU_TEXT, kb_main())


# -------------------------------------------------------------------- /wifi

@BOT.message(AdminOnly(), Command('wifi'))
async def cmd_wifi(message: Message, service: RouterService) -> None:
    wait = await message.answer('⏳ Читаю настройки Wi-Fi…')
    await show_wifi(wait, service)


@BOT.callback_query(AdminOnly(), F.data == 'wifi')
async def cb_wifi(call: CallbackQuery, service: RouterService) -> None:
    await call.answer()
    await show_wifi(call.message, service)


@BOT.callback_query(AdminOnly(), F.data == 'wifi:keys')
async def cb_wifi_keys(call: CallbackQuery, service: RouterService) -> None:
    try:
        configs = await service.run(
            lambda c: {conn: c.get_wifi_config(conn) for conn in TOGGLEABLE.values()}
        )
    except ClientException as exc:
        await call.answer(f'❌ Ошибка: {exc}'[:190], show_alert=True)
        return
    await call.answer('🔑 Пароли Wi-Fi:\n' + format_keys_alert(configs), show_alert=True)


@BOT.callback_query(AdminOnly(), F.data.startswith('wifi:toggle:'))
async def cb_wifi_toggle(call: CallbackQuery, service: RouterService) -> None:
    conn = TOGGLEABLE.get(call.data.rsplit(':', 1)[-1])
    if conn is None:
        await call.answer('Неизвестная сеть', show_alert=True)
        return

    await call.answer()

    def job(c: ArcherC80Client) -> bool:
        cfg = c.get_wifi_config(conn)
        want = not bool(cfg.enabled)
        c.set_wifi_state(conn, want)
        return want

    try:
        new_state = await service.run(job)
    except ClientException as exc:
        await _edit_safe(call.message, _err_text(exc), kb_main())
        return
    name = CONNECTION_NAMES.get(conn, conn.value)
    note = 'включена 🟢' if new_state else 'выключена ⚪'
    await call.message.answer(f'✅ Сеть «{name}» теперь {note}.')
    await show_wifi(call.message, service)


# -------------------------------------------------------------- setssid/setpass

def _parse_band_and_value(command: CommandObject, usage: str) -> tuple[Connection, str]:
    args = (command.args or '').strip()
    if not args or ' ' not in args:
        raise ValueError(usage)
    band_raw, value = args.split(None, 1)
    band = BANDS.get(band_raw.lower())
    if band is None:
        raise ValueError(usage)
    return band, value.strip()


SETSSID_USAGE = 'Использование: <code>/setssid 2g|5g|g2g|g5g Новое_имя_сети</code>'
SETPASS_USAGE = 'Использование: <code>/setpass 2g|5g|g2g|g5g новый_пароль</code> (8–63 символа)'


@BOT.message(AdminOnly(), Command('setssid'))
async def cmd_setssid(message: Message, command: CommandObject, service: RouterService) -> None:
    try:
        band, ssid = _parse_band_and_value(command, SETSSID_USAGE)
        ssid = validate_ssid(ssid)
    except ValueError as exc:
        await message.answer(f'⚠️ {exc}')
        return
    try:
        await service.run(lambda c: c.set_wifi_ssid(band, ssid))
    except ClientException as exc:
        await message.answer(_err_text(exc), reply_markup=kb_main())
        return
    await message.answer(
        f'✅ SSID «{CONNECTION_NAMES.get(band, band.value)}» изменён на <b>{ssid}</b>.\n'
        'Клиенты этой сети переподключатся к новому имени.',
        reply_markup=kb_main(),
    )


@BOT.message(AdminOnly(), Command('setpass'))
async def cmd_setpass(message: Message, command: CommandObject, service: RouterService) -> None:
    try:
        band, password = _parse_band_and_value(command, SETPASS_USAGE)
        password = validate_psk(password)
    except ValueError as exc:
        await message.answer(f'⚠️ {exc}')
        return
    try:
        await service.run(lambda c: c.set_wifi_password(band, password))
    except ClientException as exc:
        await message.answer(_err_text(exc), reply_markup=kb_main())
        return
    # удаляем сообщение, чтобы пароль не висел в чате
    deleted = True
    try:
        await message.delete()
    except TelegramBadRequest:
        deleted = False
    note = 'Сообщение с паролём удалено.' if deleted else '⚠️ Удали своё сообщение с паролём вручную!'
    await message.answer(
        f'✅ Пароль сети «{CONNECTION_NAMES.get(band, band.value)}» изменён. {note}\n'
        'Подключённые устройства будут отключены — нужно ввести новый пароль.',
        reply_markup=kb_main(),
    )


# ----------------------------------------------------------------- /clients

@BOT.message(AdminOnly(), Command('clients'))
async def cmd_clients(message: Message, service: RouterService) -> None:
    wait = await message.answer('⏳ Запрашиваю список устройств…')
    await show_clients(wait, service)


@BOT.callback_query(AdminOnly(), F.data == 'clients')
async def cb_clients(call: CallbackQuery, service: RouterService) -> None:
    await call.answer()
    await show_clients(call.message, service)


@BOT.callback_query(AdminOnly(), F.data.startswith('dev:'))
async def cb_device_detail(call: CallbackQuery, service: RouterService) -> None:
    await call.answer()
    await show_device(call.message, service, call.data.removeprefix('dev:'))


@BOT.callback_query(AdminOnly(), F.data.startswith('lim:'))
async def cb_limit(call: CallbackQuery, service: RouterService) -> None:
    parts = call.data.split(':')
    if len(parts) != 4:
        await call.answer('Битая команда', show_alert=True)
        return
    _, mac, down_raw, up_raw = parts
    down_kbps = int(down_raw) if down_raw != '0' else None
    up_kbps = int(up_raw) if up_raw != '0' else None
    await call.answer('Применяю лимит…')
    try:
        await service.run(lambda c: c.set_device_speed_limit(mac, down_kbps, up_kbps))
    except (ClientException, ValueError) as exc:
        await _edit_safe(call.message, _err_text(exc), kb_main())
        return
    await call.message.answer(
        f'⛔ Лимит для <code>{mac}</code>: '
        f'⬇️ {fmt_speed_limit(down_kbps, DEFAULT_DOWN_LIMIT_KBPS)} · '
        f'⬆️ {fmt_speed_limit(up_kbps, DEFAULT_UP_LIMIT_KBPS)}'
    )
    await show_device(call.message, service, mac)


@BOT.callback_query(AdminOnly(), F.data.startswith('limrm:'))
async def cb_limit_remove(call: CallbackQuery, service: RouterService) -> None:
    mac = call.data.removeprefix('limrm:')
    await call.answer('Снимаю лимит…')
    try:
        await service.run(lambda c: c.clear_device_speed_limit(mac))
    except ClientException as exc:
        await _edit_safe(call.message, _err_text(exc), kb_main())
        return
    await call.message.answer(f'♾️ Лимит скорости для <code>{mac}</code> снят.')
    await show_device(call.message, service, mac)


@BOT.message(AdminOnly(), Command('limit'))
async def cmd_limit(message: Message, command: CommandObject, service: RouterService) -> None:
    usage = ('Использование:\n'
             '<code>/limit &lt;mac или имя&gt; &lt;загрузка Мбит&gt; [отдача Мбит]</code>\n'
             'По умолчанию отдача = половина загрузки. Снять лимит: <code>/limit &lt;mac/имя&gt; off</code>\n'
             'Пример: <code>/limit iphone 20 5</code>')
    args = (command.args or '').split()
    if len(args) < 2:
        await message.answer(f'⚠️ {usage}')
        return
    query, down_raw, *rest = args
    off = down_raw.lower() in ('off', '0', 'no', 'снять', 'unlim')
    try:
        down_mbps = None if off else float(down_raw.replace(',', '.'))
        up_mbps = float(rest[0].replace(',', '.')) if rest else (
            round(down_mbps / 2, 1) if down_mbps else None)
        if down_mbps is not None and (down_mbps <= 0 or down_mbps > 10000):
            raise ValueError
    except ValueError:
        await message.answer(f'⚠️ Скорость — число от 0 до 10000 Мбит.\n{usage}')
        return
    down_kbps = int(round(down_mbps * 1024)) if down_mbps else None
    up_kbps = int(round(up_mbps * 1024)) if up_mbps else None

    def job(c: ArcherC80Client) -> ClientDevice:
        device = find_device(c.get_devices(), query)
        if device is None:
            raise RouterCommandError(
                f'Устройство «{query}» не найдено. Возьми MAC или часть имени из /clients.')
        if off:
            c.clear_device_speed_limit(device.mac)
        else:
            c.set_device_speed_limit(device.mac, down_kbps, up_kbps)
        return device

    try:
        device = await service.run(job)
    except ClientException as exc:
        await message.answer(_err_text(exc), reply_markup=kb_main())
        return
    name = device.name or device.mac
    if off:
        await message.answer(f'♾️ Лимит скорости для <b>{name}</b> снят.', reply_markup=kb_main())
    else:
        await message.answer(
            f'⛔ Лимит для <b>{name}</b>: ⬇️ {down_mbps:g} Мбит/с · ⬆️ {up_mbps:g} Мбит/с',
            reply_markup=kb_main(),
        )


@BOT.message(AdminOnly(), Command('stats'))
async def cmd_stats(message: Message, service: RouterService) -> None:
    wait = await message.answer('⏳ Собираю статистику…')
    await show_stats(wait, service)


@BOT.callback_query(AdminOnly(), F.data == 'stats')
async def cb_stats(call: CallbackQuery, service: RouterService) -> None:
    await call.answer()
    await show_stats(call.message, service)


async def show_stats(message: Message, service: RouterService) -> None:
    kb = InlineKeyboardBuilder()
    kb.button(text='🔄 Обновить', callback_data='stats')
    kb.button(text='⬅️ Меню', callback_data='menu')
    kb.adjust(2)
    try:
        devices = await service.run(lambda c: c.get_devices())
    except ClientException as exc:
        await _edit_safe(message, _err_text(exc), kb_main())
        return
    await _edit_safe(message, format_stats(devices), kb.as_markup())


@BOT.callback_query(AdminOnly(), F.data.startswith('blk:ask:'))
async def cb_block_ask(call: CallbackQuery, service: RouterService) -> None:
    mac = call.data.removeprefix('blk:ask:')
    await call.answer()
    kb = InlineKeyboardBuilder()
    kb.button(text='✅ Да, заблокировать', callback_data=f'blk:yes:{mac}')
    kb.button(text='❌ Отмена', callback_data='clients')
    kb.adjust(2)
    await _edit_safe(
        call.message,
        f'🔒 Заблокировать устройство <code>{mac}</code>?\n'
        'Оно потеряет доступ к интернету через роутер.',
        kb.as_markup(),
    )


@BOT.callback_query(AdminOnly(), F.data.startswith('blk:yes:'))
async def cb_block_yes(call: CallbackQuery, service: RouterService) -> None:
    mac = call.data.removeprefix('blk:yes:')
    await call.answer('Блокирую…')
    try:
        device = await service.run(lambda c: c.set_device_blocked(mac, True))
    except ClientException as exc:
        await _edit_safe(call.message, _err_text(exc), kb_main())
        return
    await call.message.answer(f'🔒 Заблокировано:\n{format_device(device)}')
    await show_clients(call.message, service)


@BOT.callback_query(AdminOnly(), F.data.startswith('unblk:'))
async def cb_unblock(call: CallbackQuery, service: RouterService) -> None:
    mac = call.data.removeprefix('unblk:')
    await call.answer('Разблокирую…')
    try:
        device = await service.run(lambda c: c.set_device_blocked(mac, False))
    except ClientException as exc:
        await _edit_safe(call.message, _err_text(exc), kb_main())
        return
    await call.message.answer(f'🔓 Разблокировано:\n{format_device(device)}')
    await show_clients(call.message, service)


# -------------------------------------------------------------------- /wol

async def _do_wol(query: str, service: RouterService) -> ClientDevice:
    devices = await service.run(lambda c: c.get_devices())
    device = find_device(devices, query)
    if device is None:
        raise RouterCommandError(f'Устройство «{query}» не найдено в /clients.')
    await asyncio.to_thread(send_wol, device.mac)
    return device


@BOT.message(AdminOnly(), Command('wol'))
async def cmd_wol(message: Message, command: CommandObject, service: RouterService) -> None:
    query = (command.args or '').strip()
    if not query:
        await message.answer('⚠️ Использование: <code>/wol &lt;mac или имя&gt;</code> '
                             '(из /clients). Работает, если на устройстве включён Wake-on-LAN.')
        return
    try:
        device = await _do_wol(query, service)
    except (ClientException, ValueError) as exc:
        await message.answer(_err_text(exc), reply_markup=kb_main())
        return
    await message.answer(
        f'⚡ Magic-пакет отправлен <b>{device.name or device.mac}</b> (<code>{device.mac}</code>).\n'
        'Если не проснулось — включи Wake-on-LAN на самом устройстве (обычно работает по кабелю).',
        reply_markup=kb_main(),
    )


@BOT.callback_query(AdminOnly(), F.data.startswith('wol:'))
async def cb_wol(call: CallbackQuery, service: RouterService) -> None:
    await call.answer('Шлю magic-пакет…')
    try:
        device = await _do_wol(call.data.removeprefix('wol:'), service)
    except (ClientException, ValueError) as exc:
        await _edit_safe(call.message, _err_text(exc), kb_main())
        return
    await call.message.answer(f'⚡ Magic-пакет отправлен: <b>{device.name or device.mac}</b>')


# ----------------------------------------------------------- /reservations

@BOT.message(AdminOnly(), Command('reservations'))
async def cmd_reservations(message: Message, service: RouterService) -> None:
    wait = await message.answer('⏳ Читаю привязки…')
    await show_reservations(wait, service)


@BOT.message(AdminOnly(), Command('reserve'))
async def cmd_reserve(message: Message, command: CommandObject, service: RouterService) -> None:
    usage = 'Использование: <code>/reserve &lt;mac/имя&gt; &lt;ip&gt; [имя]</code>\nПример: <code>/reserve iphone 192.168.0.55 Телефон</code>'
    args = (command.args or '').split()
    if len(args) < 2:
        await message.answer(f'⚠️ {usage}')
        return
    query, ip_raw, *name_parts = args
    try:
        ip = validate_ip(ip_raw)
    except ValueError as exc:
        await message.answer(f'⚠️ {exc}')
        return
    custom_name = ' '.join(name_parts)

    def job(c: ArcherC80Client) -> tuple:
        device = find_device(c.get_devices(), query)
        if device is None:
            raise RouterCommandError(f'Устройство «{query}» не найдено в /clients.')
        reservation = c.add_reservation(device.mac, ip, custom_name or device.name)
        return device, reservation

    try:
        device, reservation = await service.run(job)
    except (ClientException, ValueError) as exc:
        await message.answer(_err_text(exc), reply_markup=kb_main())
        return
    await message.answer(
        f'📌 <b>{device.name or device.mac}</b> теперь всегда получает '
        f'<code>{reservation.ip}</code>. Изменение применится при следующем подключении устройства.',
        reply_markup=kb_main(),
    )


async def show_reservations(message: Message, service: RouterService) -> None:
    try:
        reservations = await service.run(lambda c: c.get_reservations())
    except ClientException as exc:
        await _edit_safe(message, _err_text(exc), kb_main())
        return
    kb = InlineKeyboardBuilder()
    for r in reservations[:12]:
        kb.button(text=f'❌ {r.name or r.mac} ({r.ip})', callback_data=f'rsdel:{r.mac}')
    kb.button(text='🔄 Обновить', callback_data='reservations')
    kb.button(text='⬅️ Меню', callback_data='menu')
    kb.adjust(*([1] * min(len(reservations), 12)), 2)
    await _edit_safe(message, format_reservations(reservations), kb.as_markup())


@BOT.callback_query(AdminOnly(), F.data == 'reservations')
async def cb_reservations(call: CallbackQuery, service: RouterService) -> None:
    await call.answer()
    await show_reservations(call.message, service)


@BOT.callback_query(AdminOnly(), F.data.startswith('resv:'))
async def cb_reserve_from_card(call: CallbackQuery, service: RouterService) -> None:
    mac = call.data.removeprefix('resv:')
    await call.answer('Закрепляю текущий IP…')

    def job(c: ArcherC80Client) -> tuple:
        device = find_device(c.get_devices(), mac)
        if device is None or not device.ip:
            raise RouterCommandError('У устройства нет IP — нечего закреплять.')
        return device, c.add_reservation(device.mac, device.ip, device.name)

    try:
        device, reservation = await service.run(job)
    except ClientException as exc:
        await _edit_safe(call.message, _err_text(exc), kb_main())
        return
    await call.message.answer(
        f'📌 <b>{device.name or device.mac}</b> закреплён за <code>{reservation.ip}</code>.'
    )


@BOT.callback_query(AdminOnly(), F.data.startswith('rsdel:'))
async def cb_reservation_del(call: CallbackQuery, service: RouterService) -> None:
    mac = call.data.removeprefix('rsdel:')
    await call.answer('Удаляю…')
    try:
        entry = await service.run(lambda c: c.remove_reservation(mac))
    except ClientException as exc:
        await _edit_safe(call.message, _err_text(exc), kb_main())
        return
    await call.message.answer(f'🗑 Привязка {entry.name or ""} <code>{entry.ip}</code> ← <code>{mac}</code> удалена.')
    await show_reservations(call.message, service)


# ----------------------------------------------------------------- /task

TASK_USAGE = (
    'Использование:\n'
    '<code>/task</code> — список задач\n'
    '<code>/task add &lt;действие&gt; &lt;ЧЧ:ММ&gt; [дни]</code> — добавить\n'
    '<code>/task del &lt;id&gt;</code> — удалить\n\n'
    f'Действия: {", ".join(ACTIONS)}\n'
    'Дни (опционально): пн,вт,ср,чт,пт,сб,вс через запятую. Без дней — ежедневно.\n'
    'Пример: <code>/task add reboot 04:00</code> · <code>/task add wifi5_off 23:00 пт,сб</code>'
)


def kb_tasks(store: TaskStore) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    tasks = store.all()
    for t in tasks[:15]:
        kb.button(text=f'❌ {t.time} {t.describe()}', callback_data=f'task:del:{t.id}')
    kb.button(text='🔄 Обновить', callback_data='tasks')
    kb.button(text='⬅️ Меню', callback_data='menu')
    kb.adjust(*([1] * min(len(tasks), 15)), 2)
    return kb.as_markup()


def format_tasks(store: TaskStore) -> str:
    tasks = store.all()
    if not tasks:
        return '⏰ Задач нет.\nДобавь, например: <code>/task add reboot 04:00</code>'
    lines = ['<b>⏰ Задачи по расписанию</b> (время сервера, где крутится бот)', '']
    for t in tasks:
        lines.append(f'• <code>{t.id}</code> · {t.time} — {t.describe()}')
    lines.append('')
    lines.append('Удалить — кнопка ниже или <code>/task del &lt;id&gt;</code>.')
    return '\n'.join(lines)


@BOT.message(AdminOnly(), Command('task'))
async def cmd_task(message: Message, command: CommandObject, task_store: TaskStore) -> None:
    args = (command.args or '').split()
    if not args:
        await message.answer(format_tasks(task_store), reply_markup=kb_tasks(task_store))
        return
    if args[0] == 'add' and len(args) >= 3:
        action = args[1]
        try:
            time = parse_time(args[2])
            days = parse_days(args[3] if len(args) > 3 else '')
            task = task_store.add(action, time, days)
        except ValueError as exc:
            await message.answer(f'⚠️ {exc}\n\n{TASK_USAGE}')
            return
        await message.answer(f'✅ Задача <code>{task.id}</code>: {task.describe()}',
                             reply_markup=kb_tasks(task_store))
        return
    if args[0] == 'del' and len(args) == 2:
        task = task_store.remove(args[1])
        if task:
            await message.answer(f'🗑 Задача <code>{task.id}</code> удалена.',
                                 reply_markup=kb_tasks(task_store))
        else:
            await message.answer('⚠️ Задача с таким id не найдена.')
        return
    await message.answer(f'⚠️ {TASK_USAGE}')


@BOT.callback_query(AdminOnly(), F.data == 'tasks')
async def cb_tasks(call: CallbackQuery, task_store: TaskStore) -> None:
    await call.answer()
    await _edit_safe(call.message, format_tasks(task_store), kb_tasks(task_store))


@BOT.callback_query(AdminOnly(), F.data.startswith('task:del:'))
async def cb_task_del(call: CallbackQuery, task_store: TaskStore) -> None:
    task = task_store.remove(call.data.removeprefix('task:del:'))
    await call.answer('Удалено' if task else 'Уже нет такой задачи', show_alert=not task)
    await _edit_safe(call.message, format_tasks(task_store), kb_tasks(task_store))


# -------------------------------------------------------------------- /logs

async def show_logs(message: Message, service: RouterService, count: int = 12,
                    clean: bool = False) -> None:
    try:
        entries, raw_tail = await service.run(lambda c: c.get_logs(count))
    except ClientException as exc:
        await _edit_safe(message, _err_text(exc), kb_main())
        return
    filtered = 0
    if clean and entries:
        before = len(entries)
        entries = [e for e in entries if 'dhcps' not in e.msg.lower()]
        filtered = before - len(entries)
    kb = InlineKeyboardBuilder()
    if clean:
        kb.button(text='📢 Весь лог', callback_data='logs:24')
        kb.button(text='🔄 Обновить', callback_data='logs:24:clean')
    else:
        kb.button(text='🔁 Ещё 24', callback_data='logs:24')
        kb.button(text='🤫 Без DHCP-шума', callback_data='logs:24:clean')
        kb.button(text='🔄 Обновить', callback_data='logs:12')
    kb.button(text='⬅️ Меню', callback_data='menu')
    kb.adjust(2, 1) if clean else kb.adjust(3, 1)
    await _edit_safe(message, format_logs(entries, raw_tail, count, filtered), kb.as_markup())


@BOT.message(AdminOnly(), Command('logs'))
async def cmd_logs(message: Message, service: RouterService) -> None:
    wait = await message.answer('⏳ Читаю лог…')
    await show_logs(wait, service)


@BOT.callback_query(AdminOnly(), F.data.startswith('logs:'))
async def cb_logs(call: CallbackQuery, service: RouterService) -> None:
    await call.answer()
    parts = call.data.split(':')
    try:
        count = int(parts[1])
    except (ValueError, IndexError):
        count = 12
    await show_logs(call.message, service, count, clean=('clean' in parts))


# ------------------------------------------------------------- /ports /dmz

@BOT.message(AdminOnly(), Command('ports'))
async def cmd_ports(message: Message, service: RouterService) -> None:
    wait = await message.answer('⏳ Опрашиваю роутер…')
    await show_ports(wait, service)


@BOT.callback_query(AdminOnly(), F.data == 'ports')
async def cb_ports(call: CallbackQuery, service: RouterService) -> None:
    await call.answer()
    await show_ports(call.message, service)


async def show_ports(message: Message, service: RouterService) -> None:
    kb = InlineKeyboardBuilder()
    kb.button(text='🔁 Переключить UPnP', callback_data='ports:upnp')
    kb.button(text='🔄 Обновить', callback_data='ports')
    kb.button(text='⬅️ Меню', callback_data='menu')
    kb.adjust(1, 2)
    try:
        upnp, (dmz_on, dmz_ip) = await service.run(lambda c: (c.get_upnp(), c.get_dmz()))
    except ClientException as exc:
        await _edit_safe(message, _err_text(exc), kb_main())
        return
    await _edit_safe(message, format_ports(upnp, dmz_on, dmz_ip), kb.as_markup())


@BOT.callback_query(AdminOnly(), F.data == 'ports:upnp')
async def cb_ports_upnp(call: CallbackQuery, service: RouterService) -> None:
    await call.answer('Переключаю…')

    def job(c: ArcherC80Client) -> bool:
        want = not bool(c.get_upnp())
        c.set_upnp(want)
        return want

    try:
        new_state = await service.run(job)
    except ClientException as exc:
        await _edit_safe(call.message, _err_text(exc), kb_main())
        return
    await call.message.answer(f'🔁 UPnP теперь {"🟢 включён" if new_state else "⚪ выключен"}.')
    await show_ports(call.message, service)


@BOT.message(AdminOnly(), Command('dmz'))
async def cmd_dmz(message: Message, command: CommandObject, service: RouterService) -> None:
    arg = (command.args or '').strip().lower()
    if not arg:
        await message.answer('⚠️ <code>/dmz 192.168.0.55</code> — направить весь внешний трафик на устройство\n'
                             '<code>/dmz off</code> — выключить DMZ')
        return
    try:
        ip = None if arg in ('off', '0', 'выкл') else validate_ip(arg)
    except ValueError as exc:
        await message.answer(f'⚠️ {exc}')
        return
    try:
        await service.run(lambda c: c.set_dmz(ip))
    except ClientException as exc:
        await message.answer(_err_text(exc), reply_markup=kb_main())
        return
    if ip:
        await message.answer(f'🟢 DMZ включён → <code>{ip}</code>.\n'
                             '⚠️ Устройство теперь полностью торчит наружу — выключай, когда не надо!',
                             reply_markup=kb_main())
    else:
        await message.answer('⚪ DMZ выключен.', reply_markup=kb_main())


# -------------------------------------------------------------------- /ddns

@BOT.message(AdminOnly(), Command('ddns'))
async def cmd_ddns(message: Message, service: RouterService) -> None:
    wait = await message.answer('⏳ Читаю настройки DDNS…')
    await show_ddns(wait, service)


@BOT.callback_query(AdminOnly(), F.data == 'ddns')
async def cb_ddns(call: CallbackQuery, service: RouterService) -> None:
    await call.answer()
    await show_ddns(call.message, service)


async def show_ddns(message: Message, service: RouterService) -> None:
    try:
        info = await service.run(lambda c: c.get_ddns())
    except ClientException as exc:
        await _edit_safe(message, _err_text(exc), kb_main())
        return
    kb = InlineKeyboardBuilder()
    for e in (info.entries or [])[:4]:
        label = '🔴 выкл' if e.enabled else '🟢 вкл'
        kb.button(text=f'[{e.index}] {label} запись {e.domain or e.index}',
                  callback_data=f'ddns:toggle:{e.index}')
    kb.button(text='🔄 Обновить', callback_data='ddns')
    kb.button(text='⬅️ Меню', callback_data='menu')
    kb.adjust(1)
    await _edit_safe(message, format_ddns(info), kb.as_markup())


@BOT.callback_query(AdminOnly(), F.data.startswith('ddns:toggle:'))
async def cb_ddns_toggle(call: CallbackQuery, service: RouterService) -> None:
    try:
        index = int(call.data.rsplit(':', 1)[-1])
    except ValueError:
        await call.answer('Битая команда', show_alert=True)
        return
    await call.answer('Переключаю…')

    def job(c: ArcherC80Client) -> bool:
        info = c.get_ddns()
        entry = next((e for e in (info.entries or []) if e.index == index), None)
        want = not bool(entry.enabled if entry else False)
        c.set_ddns_enable(index, want)
        return want

    try:
        new_state = await service.run(job)
    except ClientException as exc:
        await _edit_safe(call.message, _err_text(exc), kb_main())
        return
    await call.message.answer(f'🔁 Запись DDNS [{index}] теперь {"🟢 включена" if new_state else "⚪ выключена"}.')
    await show_ddns(call.message, service)


# ------------------------------------------------------------------ /reboot

REBOOT_CONFIRM_TEXT = (
    '♻️ <b>Перезагрузить роутер?</b>\n\n'
    'Интернет пропадёт на 1–2 минуты, бот на это время будет недоступен от роутера.'
)


@BOT.message(AdminOnly(), Command('reboot'))
async def cmd_reboot(message: Message) -> None:
    await message.answer(REBOOT_CONFIRM_TEXT, reply_markup=kb_reboot())


@BOT.callback_query(AdminOnly(), F.data == 'reboot')
async def cb_reboot_ask(call: CallbackQuery) -> None:
    await call.answer()
    await _edit_safe(call.message, REBOOT_CONFIRM_TEXT, kb_reboot())


@BOT.callback_query(AdminOnly(), F.data == 'reboot:yes')
async def cb_reboot_yes(call: CallbackQuery, service: RouterService) -> None:
    await call.answer()
    try:
        await service.run(lambda c: c.reboot())
        text = ('♻️ Команда отправлена — роутер перезагружается.\n'
                'Он вернётся в сеть через 1–2 минуты.')
    except ClientException:
        # после команды reboot соединение часто обрывается — это ожидаемо
        text = ('♻️ Команда перезагрузки отправлена (соединение оборвалось — '
                'это нормально при ребуте).\nРоутер вернётся через 1–2 минуты.')
    await _edit_safe(call.message, text, kb_main())


# ---------------------------------------------------------------- fallback

@BOT.message(~AdminOnly())
async def forbidden(message: Message) -> None:
    uid = message.from_user.id if message.from_user else '—'
    await message.answer(
        f'⛔ Нет доступа. Твой ID: <code>{uid}</code> — добавь в .env как ADMIN_IDS.'
    )


@BOT.callback_query(~AdminOnly())
async def forbidden_cb(call: CallbackQuery) -> None:
    await call.answer('⛔ Нет доступа', show_alert=True)


@BOT.message()
async def unknown(message: Message) -> None:
    await message.answer('Не понял 🤷 Нажми /help для списка команд.', reply_markup=kb_main())


@BOT.callback_query()
async def unknown_cb(call: CallbackQuery) -> None:
    await call.answer()


# -------------------------------------------------------------------- main

def _make_session(proxy: str | None):
    """Сессия с опциональным прокси до api.telegram.org (если провайдер его режет)."""
    if not proxy:
        return None
    if proxy.startswith(('socks://', 'socks4://', 'socks5://')):
        try:
            import aiohttp_socks  # noqa: F401
        except ImportError:
            raise SystemExit(
                'Для socks-прокси нужен пакет aiohttp-socks:\n'
                '  pip install aiohttp-socks\n'
                'или используй http-прокси (TG_PROXY=http://...)')
    log.info('Telegram API ходит через прокси: %s', proxy)
    from aiogram.client.session.aiohttp import AiohttpSession
    return AiohttpSession(proxy=proxy)


async def _set_commands_with_retry(bot: Bot, attempts: int = 5, pause: float = 5.0) -> None:
    """set_my_commands с ретраями: обрывы до Telegram бывают плавающими (DPI)."""
    commands = [
        BotCommand(command='status', description='📊 Статус роутера'),
        BotCommand(command='wifi', description='📶 Настройки Wi-Fi'),
        BotCommand(command='clients', description='👥 Устройства, блокировка, лимиты'),
        BotCommand(command='stats', description='📈 Статистика трафика'),
        BotCommand(command='reservations', description='📌 Статические IP'),
        BotCommand(command='wol', description='⚡ Разбудить устройство'),
        BotCommand(command='task', description='⏰ Задачи по расписанию'),
        BotCommand(command='logs', description='🧾 Лог роутера'),
        BotCommand(command='ports', description='🛠 UPnP и DMZ'),
        BotCommand(command='ddns', description='🌐 DDNS'),
        BotCommand(command='setssid', description='✏️ Сменить имя Wi-Fi'),
        BotCommand(command='setpass', description='🔑 Сменить пароль Wi-Fi'),
        BotCommand(command='limit', description='⛔ Лимит скорости устройству'),
        BotCommand(command='reboot', description='♻️ Перезагрузка'),
        BotCommand(command='help', description='❓ Помощь'),
    ]
    for attempt in range(1, attempts + 1):
        try:
            await bot.set_my_commands(commands)
            return
        except TelegramNetworkError as exc:
            log.warning('Соединение с Telegram оборвано (попытка %d/%d): %s', attempt, attempts, exc)
            if attempt == attempts:
                raise SystemExit(
                    'Не смог достучаться до api.telegram.org.\n'
                    'Похоже, провайдер режет Bot API. Варианты:\n'
                    '  1) пропиши прокси в .env:  TG_PROXY=http://host:port (или socks5://...)\n'
                    '  2) запусти бота за VPN\n'
                    '  3) подними PPTP/L2TP VPN на самом роутере — трафик всей сети пойдёт через VPN')
            await asyncio.sleep(pause)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
    )
    load_dotenv()
    try:
        settings = load_settings()
    except ConfigError as exc:
        raise SystemExit(f'Ошибка конфигурации: {exc}')

    if not settings.admin_ids:
        log.warning('ADMIN_IDS пуст: бот всем будет сообщать их ID и отказывать в доступе. '
                    'Напиши боту /start, узнай свой ID и впиши в .env.')

    service = RouterService(settings)
    task_store = TaskStore('tasks.json')
    session = _make_session(settings.tg_proxy)
    bot = Bot(token=settings.bot_token, session=session,
              default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(BOT)
    dp['service'] = service
    dp['admin_ids'] = settings.admin_ids
    dp['task_store'] = task_store

    async def notify(text: str) -> None:
        for admin_id in settings.admin_ids:
            try:
                await bot.send_message(admin_id, text)
            except Exception:
                log.debug('Не смог отправить уведомление админу %s', admin_id, exc_info=True)

    scheduler_task = asyncio.create_task(scheduler_loop(service, task_store, notify=notify))

    await _set_commands_with_retry(bot)
    log.info('Бот запущен. Роутер: %s, админов: %s', settings.router_host, len(settings.admin_ids))
    try:
        await dp.start_polling(bot, allowed_updates=['message', 'callback_query'])
    finally:
        scheduler_task.cancel()


if __name__ == '__main__':
    main()
