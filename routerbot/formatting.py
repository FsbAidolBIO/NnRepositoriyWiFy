"""Чистые функции форматирования ответов бота (HTML для Telegram)."""

from __future__ import annotations

from html import escape

from tplinkrouterc6u import Connection
from tplinkrouterc6u.common.dataclass import Firmware, IPv4Status, Status

from .c80_client import (
    DEFAULT_DOWN_LIMIT_KBPS,
    DEFAULT_UP_LIMIT_KBPS,
    ClientDevice,
    DdnsInfo,
    LogEntry,
    Reservation,
    WifiConfig,
)


def onoff(flag: bool | None) -> str:
    if flag is None:
        return '❔'
    return '🟢 вкл' if flag else '⚪️ выкл'


def fmt_uptime(seconds: int | None) -> str:
    if not seconds or seconds < 0:
        return '—'
    days, rem = divmod(int(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f'{days} дн')
    if hours:
        parts.append(f'{hours} ч')
    if minutes or not parts:
        parts.append(f'{minutes} мин')
    return ' '.join(parts)


CONNECTION_NAMES = {
    Connection.HOST_2G: 'Wi-Fi 2.4 ГГц',
    Connection.HOST_5G: 'Wi-Fi 5 ГГц',
    Connection.GUEST_2G: 'Гостевая 2.4 ГГц',
    Connection.GUEST_5G: 'Гостевая 5 ГГц',
    Connection.IOT_2G: 'IoT 2.4 ГГц',
    Connection.IOT_5G: 'IoT 5 ГГц',
    Connection.WIRED: 'Кабель',
    Connection.UNKNOWN: '—',
}

CONNECTION_ICONS = {
    Connection.HOST_2G: '📶',
    Connection.HOST_5G: '📶',
    Connection.GUEST_2G: '👥',
    Connection.GUEST_5G: '👥',
    Connection.IOT_2G: '🏠',
    Connection.IOT_5G: '🏠',
    Connection.WIRED: '🔌',
    Connection.UNKNOWN: '❔',
}


def wifi_line(cfg: WifiConfig) -> str:
    name = CONNECTION_NAMES.get(cfg.connection, cfg.connection.value)
    ssid = f' «{escape(cfg.ssid)}»' if cfg.ssid else ''
    hidden = ' <i>(скрыта)</i>' if cfg.broadcast is False else ''
    return f'{onoff(cfg.enabled)}{ssid}{hidden} — {name}'


def format_wifi_view(configs: dict[Connection, WifiConfig]) -> str:
    lines = ['<b>📶 Беспроводные сети</b>', '']
    for conn in (Connection.HOST_2G, Connection.HOST_5G, Connection.GUEST_2G, Connection.GUEST_5G):
        cfg = configs.get(conn)
        lines.append('• ' + (wifi_line(cfg) if cfg else '<i>нет данных</i>'))
    lines.append('')
    lines.append('Кнопки ниже переключают сети. Пароли — кнопка «🔑 Показать пароли».')
    lines.append('Смена: <code>/setssid 2g Новое_имя</code> · <code>/setpass 5g новыйпароль</code>')
    return '\n'.join(lines)


def format_keys_alert(configs: dict[Connection, WifiConfig]) -> str:
    parts = []
    for conn in (Connection.HOST_2G, Connection.HOST_5G, Connection.GUEST_2G, Connection.GUEST_5G):
        cfg = configs.get(conn)
        label = CONNECTION_NAMES.get(conn, conn.value).replace('Wi-Fi ', '')
        pwd = (cfg.password if cfg and cfg.password else '—')
        parts.append(f'{label}: {pwd}')
    text = '\n'.join(parts)
    return text[:190]  # лимит alert в Telegram — 200 символов


def format_status(fw: Firmware, status: Status, ipv4: IPv4Status | None,
                  wan_speed: tuple[int | None, int | None] | None = None) -> str:
    lines = [
        f'🖥 <b>{escape(fw.model or "Роутер")}</b> ({escape(fw.hardware_version or "?")})',
        f'Прошивка: <code>{escape(fw.firmware_version or "?")}</code>',
        '',
    ]
    lines.append(f'🌍 WAN: <code>{status.wan_ipv4_addr or "—"}</code>'
                 + (f' ({escape(ipv4.wan_ipv4_conntype)})' if ipv4 and ipv4.wan_ipv4_conntype else ''))
    if wan_speed and any(v is not None for v in wan_speed):
        down, up = wan_speed
        lines.append(f'   Сейчас: ⬇️ {down if down is not None else "?"} · '
                     f'⬆️ {up if up is not None else "?"} <i>(сырые единицы роутера)</i>')
    if ipv4:
        dns = ', '.join(x for x in (ipv4.wan_ipv4_pridns, ipv4.wan_ipv4_snddns) if x) or '—'
        lines.append(f'   Шлюз: <code>{ipv4.wan_ipv4_gateway or "—"}</code> · DNS: {dns}')
    lines.append(f'🏠 LAN: <code>{status.lan_ipv4_addr or "—"}</code>')
    lines.append(f'⏱ Аптайм соединения: {fmt_uptime(status.wan_ipv4_uptime)}')
    lines.append('')
    lines.append(f'📶 2.4 ГГц: {onoff(status.wifi_2g_enable)} · 5 ГГц: {onoff(status.wifi_5g_enable)}')
    lines.append(f'👥 Гостевая 2.4: {onoff(status.guest_2g_enable)} · 5: {onoff(status.guest_5g_enable)}')
    if status.iot_2g_enable is not None or status.iot_5g_enable is not None:
        lines.append(f'🏠 IoT 2.4: {onoff(status.iot_2g_enable)} · 5: {onoff(status.iot_5g_enable)}')
    lines.append('')
    totals = [f'{status.clients_total} всего']
    details = [f'Wi-Fi: {status.wifi_clients_total}', f'кабель: {status.wired_total}']
    if status.guest_clients_total:
        details.append(f'гости: {status.guest_clients_total}')
    lines.append(f'👥 Клиентов: {totals[0]} ({", ".join(details)})')
    return '\n'.join(l for l in lines if l is not None)


def fmt_traffic(bytes_value: float | None) -> str:
    """Байты -> человекочитаемая строка."""
    if bytes_value is None:
        return '—'
    value = float(bytes_value)
    for unit in ('Б', 'КБ', 'МБ', 'ГБ', 'ТБ'):
        if value < 1024 or unit == 'ТБ':
            if unit in ('Б', 'КБ'):
                return f'{value:.0f} {unit}'
            return f'{value:.1f} {unit}'
        value /= 1024
    return f'{value:.1f} ТБ'


def fmt_speed_limit(kbps: int | None, default_kbps: int) -> str:
    """Лимит Kbps -> '10 Мбит/с' или 'без лимита'."""
    if kbps is None or kbps == default_kbps:
        return 'без лимита'
    if kbps >= 1024:
        return f'{kbps / 1024:g} Мбит/с'
    return f'{kbps} Кбит/с'


def fmt_bytes_total(kb: int | float) -> str:
    """КБ -> читаемое '2.3 ГБ'."""
    if kb >= 1024 * 1024:
        return f'{kb / 1024 / 1024:.1f} ГБ'
    if kb >= 1024:
        return f'{kb / 1024:.1f} МБ'
    return f'{round(kb)} КБ'


def format_device(d: ClientDevice) -> str:
    icon = CONNECTION_ICONS.get(d.connection, '❔')
    name = escape(d.name) if d.name else '<i>без имени</i>'
    status = '🔒 заблокировано' if d.blocked else ('🟢' if d.online else '⚪️ офлайн')
    if d.speed_limited:
        status += ' ⛔'
    traffic = ''
    if d.online and (d.down_speed or d.up_speed):
        traffic = (f' · ⬇️{fmt_bytes_total(d.down_speed or 0)}'
                   f' ⬆️{fmt_bytes_total(d.up_speed or 0)}')
    ip = f' · <code>{d.ip}</code>' if d.ip else ''
    return f'{icon} <b>{name}</b> {status}{ip}{traffic}\n   <code>{d.mac}</code>'


def format_device_detail(d: ClientDevice) -> str:
    icon = CONNECTION_ICONS.get(d.connection, '❔')
    name = escape(d.name) if d.name else '<i>без имени</i>'
    status = '🔒 заблокировано' if d.blocked else ('🟢 онлайн' if d.online else '⚪️ офлайн')
    lines = [
        f'{icon} <b>{name}</b>',
        f'Status: {status}',
        f'MAC: <code>{d.mac}</code>',
        f'IP: <code>{d.ip or "—"}</code> · подключение: {CONNECTION_NAMES.get(d.connection, "—")}',
    ]
    if d.online and (d.down_speed or d.up_speed):
        lines.append(f'Трафик: ⬇️ {fmt_bytes_total(d.down_speed or 0)}'
                     f' · ⬆️ {fmt_bytes_total(d.up_speed or 0)}')
    if d.signal is not None and d.connection != Connection.WIRED:
        lines.append(f'Сигнал Wi-Fi: {d.signal}')
    lines.append(f'Трафик за сессию: {fmt_traffic(d.total_traffic)}')
    if d.duration:
        lines.append(f'Онлайн уже: {fmt_uptime(d.duration)}')
    lines.append(
        f'Лимит: ⬇️ {fmt_speed_limit(d.down_limit, DEFAULT_DOWN_LIMIT_KBPS)} · '
        f'⬆️ {fmt_speed_limit(d.up_limit, DEFAULT_UP_LIMIT_KBPS)}'
    )
    return '\n'.join(lines)


LEVEL_ICONS = {0: '🔴', 1: '🔴', 2: '🟠', 3: '⚪'}


def format_logs(entries: list[LogEntry], raw_tail: list[str], count: int,
                filtered: int = 0) -> str:
    title = 'без DHCP-шума' if filtered else f'последние {count}'
    lines = [f'<b>🧾 Лог роутера</b> ({title})', '']
    if entries:
        for e in entries:
            icon = LEVEL_ICONS.get(e.level, '⚪')
            time = f'{escape(e.time)} — ' if e.time else ''
            lines.append(f'{icon} {time}{escape(e.msg)}')
        lines.append('')
        if filtered:
            lines.append(f'<i>🤫 скрыто DHCP-строк: {filtered}</i>')
        lines.append('<i>Уровни: 🔴 ошибка · 🟠 важное · ⚪ инфо. Подробнее: уровень в [скобках] на роутере.</i>')
        return '\n'.join(lines)
    if filtered:
        lines.append(f'🤫 Кроме DHCP-шума ({filtered} строк) больше ничего нет.')
        return '\n'.join(lines)
    if raw_tail:
        lines.append('<i>Необычный формат записей — показываю сырой хвост:</i>')
        lines.append('')
        lines.extend(f'<code>{escape(l)}</code>' for l in raw_tail)
        return '\n'.join(lines)
    return '🧾 Лог пуст.'


def format_ddns(info: DdnsInfo) -> str:
    lines = ['<b>🌐 DDNS</b>', '']
    if not info.entries:
        return '🌐 DDNS не настроен (записей нет).'
    lines.append(f'Режим: <code>{info.mode if info.mode is not None else "—"}</code>')
    for e in info.entries:
        state = onoff(e.enabled)
        user = escape(e.username) if e.username else '—'
        domain = escape(e.domain) if e.domain else '—'
        status = f' · статус <code>{e.status}</code>' if e.status is not None else ''
        lines.append(f'• [{e.index}] {state} · {domain} ({user}){status}')
    lines.append('')
    lines.append('<i>Настройка логина/домена — через веб-морду; здесь можно включать/выключать записи.</i>')
    return '\n'.join(lines)


def format_ports(upnp: bool | None, dmz_on: bool | None, dmz_ip: str | None) -> str:
    lines = ['<b>🛠 Порты и UPnP</b>', '']
    lines.append(f'UPnP: {onoff(upnp)}')
    dmz = f'🟢 вкл → <code>{escape(dmz_ip)}</code>' if dmz_on and dmz_ip else onoff(dmz_on)
    lines.append(f'DMZ: {dmz}')
    lines.append('')
    lines.append('DMZ: <code>/dmz 192.168.0.55</code> — всё наружу на устройство · '
                 '<code>/dmz off</code> — выключить')
    lines.append('<i>Полный проброс портов (Virtual Server) добавлю после уточнения формата блоков 20/21.</i>')
    return '\n'.join(lines)


def format_reservations(reservations: list[Reservation]) -> str:
    if not reservations:
        return ('📌 Статических привязок нет.\n'
                'Закрепи IP за устройством: <code>/reserve iphone 192.168.0.55</code>\n'
                'или кнопкой «Закрепить IP» в карточке устройства (/clients).')
    lines = ['📌 <b>Статические IP (MAC → IP)</b>', '']
    for r in reservations:
        state = '🟢' if r.enabled else '⚪️'
        name = escape(r.name) if r.name else '<i>без имени</i>'
        lines.append(f'{state} <b>{name}</b> · <code>{r.ip}</code> ← <code>{r.mac}</code>')
    lines.append('')
    lines.append('Добавить: <code>/reserve &lt;mac/имя&gt; &lt;ip&gt; [имя]</code> · удалить — кнопками ниже')
    return '\n'.join(lines)


def format_stats(devices: list[ClientDevice], top: int = 10) -> str:
    if not devices:
        return '📭 Устройств в списке роутера нет.'
    with_traffic = sorted(
        (d for d in devices if d.total_traffic),
        key=lambda d: d.total_traffic or 0,
        reverse=True,
    )
    total = sum(d.total_traffic or 0 for d in devices)
    online = sum(1 for d in devices if d.online)

    lines = [
        '<b>📊 Статистика трафика</b>',
        f'Онлайн устройств: {online} · суммарный трафик: <b>{fmt_traffic(total)}</b>',
        '',
    ]
    if not with_traffic:
        lines.append('<i>Счётчиков трафика пока нет — роутер накапливает их по ходу сессий.</i>')
        return '\n'.join(lines)

    lines.append('<b>Топ по трафику:</b>')
    for i, d in enumerate(with_traffic[:top], 1):
        name = escape(d.name) if d.name else d.mac
        mark = '🟢' if d.online else '⚪️'
        speeds = ''
        if d.online and (d.down_speed or d.up_speed):
            speeds = f' · ⬇️{fmt_bytes_total(d.down_speed or 0)} ⬆️{fmt_bytes_total(d.up_speed or 0)}'
        lines.append(f'{i}. {mark} <b>{name}</b> — {fmt_traffic(d.total_traffic)}{speeds}')
    if len(with_traffic) > top:
        lines.append(f'<i>…и ещё {len(with_traffic) - top}</i>')
    lines.append('')
    lines.append('<i>Счётчики роутер ведёт с момента включения/сброса. Текущая скорость — в /clients.</i>')
    return '\n'.join(lines)


def format_clients(devices: list[ClientDevice], max_shown: int = 30) -> str:
    if not devices:
        return '📭 Устройств в списке роутера нет.'
    online = [d for d in devices if d.online and not d.blocked]
    blocked = [d for d in devices if d.blocked]
    offline = [d for d in devices if not d.online and not d.blocked]

    lines = [f'<b>👥 Устройства</b> — онлайн: {len(online)}, '
             f'заблокировано: {len(blocked)}, офлайн: {len(offline)}', '']
    shown = 0
    for group, title in ((online, '🟢 Онлайн'), (blocked, '🔒 Заблокированные'), (offline, '⚪️ Офлайн')):
        if not group:
            continue
        lines.append(f'<b>{title}:</b>')
        for d in group:
            if shown >= max_shown:
                lines.append(f'<i>…и ещё {len(devices) - shown}, список сокращён.</i>')
                return '\n'.join(lines)
            lines.append(format_device(d))
            shown += 1
    return '\n'.join(lines)
