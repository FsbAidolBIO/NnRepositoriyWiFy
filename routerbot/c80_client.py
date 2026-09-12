"""Расширенный клиент для TP-Link Archer C80.

Библиотека tplinkrouterc6u умеет для C80 логин, статус, вкл/выкл Wi-Fi и reboot.
Здесь добавлено то, чего не хватает для бота:
  - чтение настроек Wi-Fi (SSID, пароль, вещание);
  - смена SSID и пароля Wi-Fi (поля cSsid / cPskSecret блока 33);
  - список клиентов с флагом блокировки и блокировка/разблокировка (поле blocked блока 13).

Запись в прошивку C80 выполняется тем же зашифрованным запросом code=1,
которым штатный веб-интерфейс меняет настройки: тело вида
    id 33|1,1,0
    cSsid MyNetwork
шифруется AES-сессией, подписывается RSA и отправляется на роутер.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import unquote

from tplinkrouterc6u import Connection
from tplinkrouterc6u.client.c80 import TplinkC80Router
from tplinkrouterc6u.common.exception import ClientException

# Блоки конфигурации Wi-Fi в протоколе C80 (см. ответы роутера и тесты библиотеки)
WIFI_BLOCKS: dict[Connection, str] = {
    Connection.HOST_2G: '33|1,1,0',
    Connection.HOST_5G: '33|2,1,0',
    Connection.GUEST_2G: '33|1,2,0',
    Connection.GUEST_5G: '33|2,2,0',
}

DEVICES_BLOCK = '13|1,0,0'
RESERVATIONS_BLOCK = '12|1,0,0'
LOGS_BLOCK = '2|1,0,0'
WAN_BLOCK = '23|1,0,0'
DMZ_BLOCK = '18|1,0,0'
UPNP_BLOCK = '19|1,0,0'
DDNS_BLOCK = '38|1,0,0'
DDNS_STATUS_BLOCK = '39|1,0,0'
ZERO_MAC = '00-00-00-00-00-00'
ZERO_IP = '0.0.0.0'

# Заводские значения лимитов в прошивке C80, Kbps (= "без ограничений")
DEFAULT_DOWN_LIMIT_KBPS = 1048576   # 1024 Мбит/с
DEFAULT_UP_LIMIT_KBPS = 204800      # 200 Мбит/с

# totalUnit: 0=Б, 1=КБ, 2=МБ, 3=ГБ
_TRAFFIC_UNIT_BYTES = (1, 1024, 1024 ** 2, 1024 ** 3)

_DEVICE_TYPES = {
    0: Connection.WIRED,
    1: Connection.HOST_2G,
    2: Connection.GUEST_2G,
    3: Connection.HOST_5G,
    4: Connection.GUEST_5G,
    13: Connection.IOT_2G,
    14: Connection.IOT_5G,
}

SUCCESS_CODE = '00000'


class RouterCommandError(ClientException):
    """Роутер отклонил команду (вернул код ошибки вместо 00000)."""


@dataclass
class WifiConfig:
    connection: Connection
    enabled: bool | None = None
    ssid: str | None = None
    password: str | None = None
    broadcast: bool | None = None  # False = скрытая сеть


@dataclass
class ClientDevice:
    index: int
    mac: str
    ip: str | None
    name: str
    connection: Connection
    online: bool
    blocked: bool
    up_speed: int | None = None       # счётчик трафика в КБ, как отдаёт роутер
    down_speed: int | None = None     # (C80 отдаёт накопленное, а не текущую скорость)
    up_limit: int | None = None       # лимит отдачи, Kbps
    down_limit: int | None = None     # лимит загрузки, Kbps
    total_traffic: float | None = None  # суммарный трафик за сессию, байт
    duration: int | None = None       # сколько секунд онлайн
    signal: int | None = None         # aveRssi

    @property
    def speed_limited(self) -> bool:
        limited_down = self.down_limit not in (None, DEFAULT_DOWN_LIMIT_KBPS)
        limited_up = self.up_limit not in (None, DEFAULT_UP_LIMIT_KBPS)
        return limited_down or limited_up


def find_device(devices: list[ClientDevice], query: str) -> ClientDevice | None:
    """Ищет устройство по MAC (любой формат) или подстроке имени (без регистра)."""
    query = query.strip().lower()
    mac_query = query.replace(':', '-')
    by_mac = [d for d in devices if d.mac == mac_query]
    if len(by_mac) == 1:
        return by_mac[0]
    by_name = [d for d in devices if d.name and query in d.name.lower()]
    if len(by_name) == 1:
        return by_name[0]
    return None


@dataclass
class Reservation:
    """Статическая привязка IP к MAC (Address Reservation)."""
    index: int
    mac: str
    ip: str
    name: str
    enabled: bool


@dataclass
class LogEntry:
    index: int
    level: int | None
    time: str | None
    msg: str


@dataclass
class DdnsEntry:
    index: int
    enabled: bool | None = None
    username: str | None = None
    domain: str | None = None
    status: int | None = None


@dataclass
class DdnsInfo:
    mode: int | None = None
    entries: list[DdnsEntry] | None = None


def validate_ip(value: str) -> str:
    value = value.strip()
    parts = value.split('.')
    if len(parts) != 4:
        raise ValueError('IP в формате 192.168.0.55')
    for part in parts:
        if not part.isdigit() or not 0 <= int(part) <= 255:
            raise ValueError('IP в формате 192.168.0.55')
    return value


def validate_ssid(ssid: str) -> str:
    ssid = _clean(ssid)
    if not 1 <= len(ssid) <= 32:
        raise ValueError('SSID должен быть от 1 до 32 символов.')
    return ssid


def validate_psk(password: str) -> str:
    password = _clean(password)
    if 8 <= len(password) <= 63:
        return password
    if len(password) == 64 and all(c in '0123456789abcdefABCDEF' for c in password):
        return password  # 64-символьный HEX-ключ
    raise ValueError('Пароль Wi-Fi должен быть 8–63 символа (либо 64-символьный HEX-ключ).')


def _clean(value: str) -> str:
    return value.strip().replace('\r', ' ').replace('\n', ' ')


class ArcherC80Client(TplinkC80Router):
    """TplinkC80Router + чтение/запись настроек Wi-Fi и управление клиентами."""

    # ------------------------------------------------------------------ Wi-Fi

    def get_wifi_config(self, wifi: Connection) -> WifiConfig:
        block_id = WIFI_BLOCKS[wifi]
        lines = self._return_data_block(block_id).get(block_id)
        if not lines:
            raise RouterCommandError(f'Роутер не вернул блок {block_id}')

        values: dict[str, str] = {}
        for line in lines:
            if line == SUCCESS_CODE or line.startswith('id '):
                continue
            key, _, value = line.partition(' ')  # значение может содержать пробелы
            values[key] = value

        return WifiConfig(
            connection=wifi,
            enabled=self._to_bool(values.get('bEnable')),
            ssid=values.get('cSsid') or None,
            password=values.get('cPskSecret') or None,
            broadcast=self._to_bool(values.get('bBcastSsid')),
        )

    def set_wifi_state(self, wifi: Connection, enable: bool) -> None:
        self._write_fields(WIFI_BLOCKS[wifi], {'bEnable': '1' if enable else '0'})

    def set_wifi_ssid(self, wifi: Connection, ssid: str) -> None:
        self._write_fields(WIFI_BLOCKS[wifi], {'cSsid': validate_ssid(ssid)})

    def set_wifi_password(self, wifi: Connection, password: str) -> None:
        self._write_fields(WIFI_BLOCKS[wifi], {'cPskSecret': validate_psk(password)})

    # -------------------------------------------------------------- клиенты

    def get_devices(self) -> list[ClientDevice]:
        lines = self._return_data_block(DEVICES_BLOCK).get(DEVICES_BLOCK)
        if not lines:
            return []

        rows = self._parse_indexed(lines)
        devices: list[ClientDevice] = []
        for index, row in rows.items():
            mac = (row.get('mac') or '').lower()
            if not mac or mac == ZERO_MAC:
                continue
            conn = Connection.UNKNOWN
            if row.get('online') == '1':
                try:
                    conn = _DEVICE_TYPES.get(int(row.get('type', '-1')), Connection.UNKNOWN)
                except ValueError:
                    pass
            devices.append(ClientDevice(
                index=index,
                mac=mac,
                ip=row.get('ip') or None,
                name=row.get('name') or '',
                connection=conn,
                online=row.get('online') == '1',
                blocked=row.get('blocked') == '1',
                up_speed=self._to_int(row.get('up')),
                down_speed=self._to_int(row.get('down')),
                up_limit=self._to_int(row.get('upLimit')),
                down_limit=self._to_int(row.get('downLimit')),
                total_traffic=self._to_traffic(row.get('totalVal'), row.get('totalUnit')),
                duration=self._to_int(row.get('duration')),
                signal=self._to_int(row.get('aveRssi')),
            ))
        return sorted(devices, key=lambda d: (not d.online, d.blocked, d.name.lower(), d.mac))

    def set_device_blocked(self, mac: str, blocked: bool) -> ClientDevice:
        """Блокирует/разблокирует устройство по MAC. Возвращает найденное устройство."""
        normalized = mac.lower().replace(':', '-')
        devices = self.get_devices()
        device = next((d for d in devices if d.mac == normalized), None)
        if device is None:
            raise RouterCommandError(f'Устройство {mac} не найдено в списке роутера.')
        self._write_fields(DEVICES_BLOCK, {f'blocked {device.index}': '1' if blocked else '0'})
        return device

    def set_device_speed_limit(self, mac: str,
                               down_kbps: int | None = None,
                               up_kbps: int | None = None) -> ClientDevice:
        """Ставит лимит скорости устройству, Kbps. None — не трогать это направление."""
        if down_kbps is None and up_kbps is None:
            raise ValueError('Укажи хотя бы один лимит (загрузка или отдача).')
        for value, label in ((down_kbps, 'загрузки'), (up_kbps, 'отдачи')):
            if value is not None and not 64 <= value <= 10_000_000:
                raise ValueError(f'Лимит {label}: от 64 до 10 000 000 Kbps.')

        normalized = mac.lower().replace(':', '-')
        devices = self.get_devices()
        device = next((d for d in devices if d.mac == normalized), None)
        if device is None:
            raise RouterCommandError(f'Устройство {mac} не найдено в списке роутера.')

        fields = {}
        if down_kbps is not None:
            fields[f'downLimit {device.index}'] = str(down_kbps)
        if up_kbps is not None:
            fields[f'upLimit {device.index}'] = str(up_kbps)
        self._write_fields(DEVICES_BLOCK, fields)
        return device

    def clear_device_speed_limit(self, mac: str) -> ClientDevice:
        """Возвращает заводские лимиты (= снять ограничение)."""
        return self.set_device_speed_limit(mac, DEFAULT_DOWN_LIMIT_KBPS, DEFAULT_UP_LIMIT_KBPS)

    # --------------------------------------------------- статический DHCP

    def get_reservations(self) -> list[Reservation]:
        lines = self._return_data_block(RESERVATIONS_BLOCK).get(RESERVATIONS_BLOCK)
        if not lines:
            return []
        reservations = []
        for index, row in self._parse_indexed(lines).items():
            mac = (row.get('mac') or '').lower()
            if not mac or mac == ZERO_MAC:
                continue
            reservations.append(Reservation(
                index=index,
                mac=mac,
                ip=row.get('ip') or '',
                name=row.get('name') or '',
                enabled=row.get('dhcpsEnable', '1') == '1',
            ))
        return sorted(reservations, key=lambda r: r.ip.split('.')[-1].zfill(3))

    def add_reservation(self, mac: str, ip: str, name: str = '', enabled: bool = True) -> Reservation:
        """Прикрепляет IP к MAC. Если привязка на этот MAC есть — обновляет её."""
        ip = validate_ip(ip)
        normalized = mac.lower().replace(':', '-')
        existing = self.get_reservations()
        clash = next((r for r in existing if r.ip == ip and r.mac != normalized), None)
        if clash:
            raise RouterCommandError(
                f'IP {ip} уже закреплён за {clash.mac}. Сначала удали ту привязку.')

        entry = next((r for r in existing if r.mac == normalized), None)
        index = entry.index if entry else ((max(r.index for r in existing) + 1) if existing else 0)
        self._write_fields(RESERVATIONS_BLOCK, {
            f'mac {index}': normalized,
            f'ip {index}': ip,
            f'name {index}': _clean(name)[:64],
            f'dhcpsEnable {index}': '1' if enabled else '0',
        })
        return Reservation(index, normalized, ip, name, enabled)

    def remove_reservation(self, mac: str) -> Reservation:
        normalized = mac.lower().replace(':', '-')
        entry = next((r for r in self.get_reservations() if r.mac == normalized), None)
        if entry is None:
            raise RouterCommandError(f'Привязка для {mac} не найдена.')
        # прошивка не удаляет строки, а обнуляет: mac=нули, ip=0.0.0.0
        self._write_fields(RESERVATIONS_BLOCK, {
            f'mac {entry.index}': ZERO_MAC,
            f'ip {entry.index}': ZERO_IP,
            f'name {entry.index}': '',
            f'dhcpsEnable {entry.index}': '0',
        })
        return entry

    # ---------------------------------------------------------------- логи

    def get_logs(self, count: int = 12) -> tuple[list[LogEntry], list[str]]:
        """Системный лог. Возвращает (записи, сырой хвост если формат не распознан)."""
        lines = self.read_module(LOGS_BLOCK) or []
        rows = self._parse_indexed(lines)
        entries: list[LogEntry] = []
        for index, row in rows.items():
            msg = row.get('msg') or row.get('msgInfo') or row.get('content')
            if not msg:
                continue
            entries.append(LogEntry(
                index=index,
                level=self._to_int(row.get('level')),
                time=row.get('time') or row.get('date') or None,
                msg=unquote(msg.strip()),  # роутер шлёт текст URL-encoded (%20 = пробел)
            ))
        entries.sort(key=lambda e: e.index, reverse=True)
        if entries:
            return entries[:count], []
        # формат записи не угадали — вернём сырой хвост, чтобы было что читать
        data_lines = [l for l in lines if l != SUCCESS_CODE and l.strip()]
        return [], data_lines[-count:]

    # ------------------------------------------------------------ live-стат

    def get_wan_speed(self) -> tuple[int | None, int | None]:
        """Текущая скорость WAN из блока 23: (inRates, outRates)."""
        lines = self.read_module(WAN_BLOCK) or []
        values = self._scalars(lines)
        return self._to_int(values.get('inRates')), self._to_int(values.get('outRates'))

    # ----------------------------------------------------------- DMZ / UPnP

    def get_dmz(self) -> tuple[bool | None, str | None]:
        lines = self.read_module(DMZ_BLOCK) or []
        values = self._scalars(lines)
        return self._to_bool(values.get('dmzEnable')), values.get('dmzClient') or None

    def set_dmz(self, ip: str | None) -> None:
        if ip is None:
            self._write_fields(DMZ_BLOCK, {'dmzEnable': '0'})
        else:
            self._write_fields(DMZ_BLOCK, {'dmzEnable': '1', 'dmzClient': validate_ip(ip)})

    def get_upnp(self) -> bool | None:
        lines = self.read_module(UPNP_BLOCK) or []
        return self._to_bool(self._scalars(lines).get('igdEnable'))

    def set_upnp(self, enable: bool) -> None:
        self._write_fields(UPNP_BLOCK, {'igdEnable': '1' if enable else '0'})

    # ----------------------------------------------------------------- DDNS

    def get_ddns(self) -> DdnsInfo:
        lines = self.read_module(DDNS_BLOCK) or []
        rows = self._parse_indexed(lines)
        status_rows = self._parse_indexed(self.read_module(DDNS_STATUS_BLOCK) or [])
        mode = self._to_int(self._scalars(lines).get('mode'))
        entries = []
        for index, row in rows.items():
            entries.append(DdnsEntry(
                index=index,
                enabled=self._to_bool(row.get('enable')),
                username=row.get('username') or None,
                domain=row.get('domainName') or None,
                status=self._to_int((status_rows.get(index) or {}).get('status')),
            ))
        return DdnsInfo(mode=mode, entries=sorted(entries, key=lambda e: e.index))

    def set_ddns_enable(self, index: int, enable: bool) -> None:
        self._write_fields(DDNS_BLOCK, {f'enable {index}': '1' if enable else '0'})

    # --------------------------------------------------------------- probe

    def read_module(self, block_id: str) -> list[str] | None:
        """Сырой разбор блока протокола (для диагностики/--probe-blocks)."""
        return self._return_data_block(block_id).get(block_id)

    @staticmethod
    def _scalars(lines: list[str]) -> dict[str, str]:
        """Скалярные поля 'ключ значение' (без индекса)."""
        values: dict[str, str] = {}
        for line in lines:
            if line == SUCCESS_CODE or line.startswith('id '):
                continue
            key, _, value = line.partition(' ')
            values[key] = value
        return values

    # --------------------------------------------------------------- утилиты

    def _write_fields(self, block_id: str, fields: dict[str, str]) -> None:
        lines = [f'id {block_id}'] + [f'{key} {value}' for key, value in fields.items()]
        body = self._encrypt_body('\r\n'.join(lines))
        response = self.request(1, 0, True, data=body)
        text = response.text if hasattr(response, 'text') else str(response)
        result = self._decrypt_data(text)
        if not result.startswith(SUCCESS_CODE):
            code = (result.splitlines() or ['<пустой ответ>'])[0].strip()
            raise RouterCommandError(f'Роутер отклонил команду (код {code}).')

    @staticmethod
    def _parse_indexed(lines: list[str]) -> dict[int, dict[str, str]]:
        """Разбор 'ключ индекс значение' -> {индекс: {ключ: значение}}.

        Строго 3 части строки: иначе скаляр вроде 'mode 2' принялся бы за
        индексированное поле. Пустые значения выглядят как 'key N ' (с пробелом).
        """
        rows: dict[int, dict[str, str]] = {}
        for line in lines:
            if line == SUCCESS_CODE or line.startswith('id '):
                continue
            parts = line.split(' ', 2)
            if len(parts) != 3:
                continue
            key, index, value = parts
            try:
                idx = int(index)
            except ValueError:
                continue
            rows.setdefault(idx, {})[key] = value
        return rows

    @staticmethod
    def _to_bool(value: str | None) -> bool | None:
        if value is None:
            return None
        return value == '1'

    @staticmethod
    def _to_int(value: str | None) -> int | None:
        try:
            return int(value) if value is not None else None
        except ValueError:
            return None

    @staticmethod
    def _to_traffic(value: str | None, unit: str | None) -> float | None:
        """totalVal + totalUnit (0=Б,1=КБ,2=МБ,3=ГБ) -> байты."""
        if not value:
            return None
        try:
            val = float(value)
            unit_idx = int(unit) if unit else 0
        except ValueError:
            return None
        if not 0 <= unit_idx < len(_TRAFFIC_UNIT_BYTES):
            return None
        return val * _TRAFFIC_UNIT_BYTES[unit_idx]
