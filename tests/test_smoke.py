"""Дымовые тесты без роутера и Telegram: парсинг, валидация, форматирование."""

from __future__ import annotations

import tempfile
import unittest
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from tplinkrouterc6u import Connection

from routerbot.scheduler import parse_days, parse_time

from routerbot.c80_client import (
    ArcherC80Client,
    ClientDevice,
    RouterCommandError,
    find_device,
    validate_psk,
    validate_ssid,
)
from routerbot.config import ConfigError, load_settings
from routerbot.formatting import (
    format_clients,
    format_ddns,
    format_device,
    format_device_detail,
    format_logs,
    format_ports,
    format_reservations,
    format_stats,
    format_wifi_view,
    fmt_speed_limit,
    fmt_traffic,
    fmt_uptime,
)

WIFI_BLOCK = (
    '00000\r\n'
    'id 33|1,1,0\r\n'
    'uUnit 0\r\n'
    'cSsid Home_WiFi 5\r\n'
    'bEnable 1\r\n'
    'bBcastSsid 1\r\n'
    'cPskSecret qwerty123\r\n'
    'SecurityType 2'
)

DEVICES_BLOCK = (
    '00000\r\n'
    'id 13|1,0,0\r\n'
    'ip 0 192.168.0.100\r\n'
    'ip 1 192.168.0.101\r\n'
    'ip 2 0.0.0.0\r\n'
    'mac 0 aa-bb-cc-dd-ee-01\r\n'
    'mac 1 aa-bb-cc-dd-ee-02\r\n'
    'mac 2 00-00-00-00-00-00\r\n'
    'name 0 iPhone-Masha\r\n'
    'name 1 \r\n'
    'type 0 1\r\n'
    'type 1 0\r\n'
    'online 0 1\r\n'
    'online 1 1\r\n'
    'blocked 0 0\r\n'
    'blocked 1 1\r\n'
    'up 0 30\r\n'
    'down 0 200\r\n'
    'up 1 5\r\n'
    'down 1 7\r\n'
    'upLimit 0 204800\r\n'
    'upLimit 1 204800\r\n'
    'downLimit 0 1048576\r\n'
    'downLimit 1 20480\r\n'
    'totalVal 0 450\r\n'
    'totalVal 1 13\r\n'
    'totalUnit 0 2\r\n'
    'totalUnit 1 2\r\n'
    'duration 0 3730\r\n'
    'duration 1 120\r\n'
    'aveRssi 0 46\r\n'
    'aveRssi 1 0'
)


def make_client(blocks: dict) -> ArcherC80Client:
    client = ArcherC80Client.__new__(ArcherC80Client)
    client._return_data_block = lambda block_id: blocks
    return client


class WifiParsingTest(unittest.TestCase):
    def test_get_wifi_config(self):
        client = make_client({'33|1,1,0': WIFI_BLOCK.split('\r\n')})
        cfg = client.get_wifi_config(Connection.HOST_2G)
        self.assertTrue(cfg.enabled)
        self.assertTrue(cfg.broadcast)
        self.assertEqual(cfg.ssid, 'Home_WiFi 5')  # SSID с пробелом целиком
        self.assertEqual(cfg.password, 'qwerty123')

    def test_get_wifi_config_no_block(self):
        client = make_client({})
        with self.assertRaises(RouterCommandError):
            client.get_wifi_config(Connection.HOST_2G)


class DevicesParsingTest(unittest.TestCase):
    def test_get_devices(self):
        client = make_client({'13|1,0,0': DEVICES_BLOCK.split('\r\n')})
        devices = client.get_devices()
        # устройство с нулевым MAC отфильтровано
        self.assertEqual(len(devices), 2)
        iphone = next(d for d in devices if d.mac == 'aa-bb-cc-dd-ee-01')
        blocked = next(d for d in devices if d.mac == 'aa-bb-cc-dd-ee-02')
        self.assertTrue(iphone.online)
        self.assertFalse(iphone.blocked)
        self.assertEqual(iphone.connection, Connection.HOST_2G)
        self.assertEqual(iphone.down_speed, 200)
        self.assertTrue(blocked.blocked)
        self.assertEqual(blocked.connection, Connection.WIRED)

    def test_extended_fields(self):
        client = make_client({'13|1,0,0': DEVICES_BLOCK.split('\r\n')})
        devices = client.get_devices()
        iphone = next(d for d in devices if d.mac == 'aa-bb-cc-dd-ee-01')
        pc = next(d for d in devices if d.mac == 'aa-bb-cc-dd-ee-02')
        # дефолтные лимиты = "без ограничений"
        self.assertFalse(iphone.speed_limited)
        self.assertEqual(iphone.up_limit, 204800)
        # у второго устройства downLimit 20480 -> лимит активен
        self.assertTrue(pc.speed_limited)
        self.assertEqual(pc.down_limit, 20480)
        # трафик: 450 при unit=2 (МБ) -> байты
        self.assertEqual(iphone.total_traffic, 450 * 1024 ** 2)
        self.assertEqual(iphone.duration, 3730)
        self.assertEqual(iphone.signal, 46)


class FindDeviceTest(unittest.TestCase):
    DEV = ClientDevice(
        index=0, mac='aa-bb-cc-dd-ee-01', ip='192.168.0.100', name='iPhone-Masha',
        connection=Connection.HOST_2G, online=True, blocked=False,
    )

    def test_by_mac_dash_or_colon(self):
        self.assertIs(find_device([self.DEV], 'aa-bb-cc-dd-ee-01'), self.DEV)
        self.assertIs(find_device([self.DEV], 'AA:BB:CC:DD:EE:01'), self.DEV)

    def test_by_name_substring(self):
        self.assertIs(find_device([self.DEV], 'iphone'), self.DEV)
        self.assertIs(find_device([self.DEV], 'MASHA'), self.DEV)

    def test_not_found(self):
        self.assertIsNone(find_device([self.DEV], 'samsung'))
        self.assertIsNone(find_device([], 'iphone'))


class SpeedLimitTest(unittest.TestCase):
    def _client(self, reply='00000\r\n'):
        client = ArcherC80Client.__new__(ArcherC80Client)
        client._encrypt_body = lambda text: text
        client._decrypt_data = lambda text: text
        client._return_data_block = lambda bid: {'13|1,0,0': DEVICES_BLOCK.split('\r\n')}
        captured = {}

        def fake_request(code, asyn, use_token=False, data=None):
            captured['body'] = data
            return SimpleNamespace(text=reply)

        client.request = fake_request
        client._captured = captured
        return client

    def test_set_limit_writes_indexed_fields(self):
        client = self._client()
        device = client.set_device_speed_limit('AA:BB:CC:DD:EE:01', down_kbps=20480, up_kbps=5120)
        self.assertEqual(device.mac, 'aa-bb-cc-dd-ee-01')
        body = client._captured['body']
        self.assertIn('downLimit 0 20480', body)  # устройство с индексом 0
        self.assertIn('upLimit 0 5120', body)

    def test_clear_limit_restores_defaults(self):
        client = self._client()
        client.clear_device_speed_limit('aa-bb-cc-dd-ee-01')
        body = client._captured['body']
        self.assertIn('downLimit 0 1048576', body)
        self.assertIn('upLimit 0 204800', body)

    def test_validation_and_missing_device(self):
        client = self._client()
        with self.assertRaises(ValueError):
            client.set_device_speed_limit('aa-bb-cc-dd-ee-01')  # ни одного лимита
        with self.assertRaises(ValueError):
            client.set_device_speed_limit('aa-bb-cc-dd-ee-01', down_kbps=1)  # слишком мало
        with self.assertRaises(RouterCommandError):
            client.set_device_speed_limit('aa-bb-cc-dd-ee-99', down_kbps=1024)


class WriteFieldsTest(unittest.TestCase):
    def _client(self, reply: str) -> ArcherC80Client:
        client = ArcherC80Client.__new__(ArcherC80Client)
        client._encrypt_body = lambda text: text
        captured = {}

        def fake_request(code, asyn, use_token=False, data=None):
            captured['body'] = data
            return SimpleNamespace(text=reply)

        client.request = fake_request
        client._captured = captured
        return client

    def test_set_ssid_ok(self):
        client = self._client('00000\r\nid 33|1,1,0')
        # _decrypt_data вернёт текст как есть, т.к. он начинается с '00000'
        client._decrypt_data = lambda text: text
        client.set_wifi_ssid(Connection.HOST_2G, 'New_Name')
        body = client._captured['body']
        self.assertIn('id 33|1,1,0', body)
        self.assertIn('cSsid New_Name', body)

    def test_router_rejects(self):
        client = self._client('00001\r\nerror')
        client._decrypt_data = lambda text: text
        with self.assertRaises(RouterCommandError):
            client.set_wifi_state(Connection.HOST_5G, False)


class ValidationTest(unittest.TestCase):
    def test_ssid(self):
        self.assertEqual(validate_ssid(' My WiFi '), 'My WiFi')
        for bad in ('', 'x' * 33):
            with self.assertRaises(ValueError):
                validate_ssid(bad)

    def test_psk(self):
        self.assertEqual(validate_psk('password8'), 'password8')
        self.assertEqual(validate_psk('a' * 64), 'a' * 64)
        for bad in ('short', 'x' * 65, 'z' * 64):
            with self.assertRaises(ValueError):
                validate_psk(bad)

    def test_psk_newline_sanitized(self):
        self.assertNotIn('\n', validate_psk('qwer\nty78'))


class ConfigTest(unittest.TestCase):
    BASE = {
        'TG_BOT_TOKEN': '123:abc',
        'ROUTER_PASSWORD': 'secret',
        'ADMIN_IDS': '111, 222',
    }

    def test_ok(self):
        s = load_settings(dict(self.BASE))
        self.assertEqual(s.admin_ids, {111, 222})
        self.assertEqual(s.router_host, 'http://192.168.0.1')
        self.assertEqual(s.router_timeout, 30)
        self.assertIsNone(s.tg_proxy)

    def test_proxy_parsed(self):
        s = load_settings({**self.BASE, 'TG_PROXY': ' socks5://127.0.0.1:1080 '})
        self.assertEqual(s.tg_proxy, 'socks5://127.0.0.1:1080')

    def test_host_normalization(self):
        s = load_settings({**self.BASE, 'ROUTER_HOST': 'tplinkwifi.net/'})
        self.assertEqual(s.router_host, 'http://tplinkwifi.net')

    def test_missing_token(self):
        with self.assertRaises(ConfigError):
            load_settings({'ROUTER_PASSWORD': 'x'})

    def test_missing_password(self):
        with self.assertRaises(ConfigError):
            load_settings({'TG_BOT_TOKEN': '123:abc'})

    def test_bad_admin_ids(self):
        with self.assertRaises(ConfigError):
            load_settings({**self.BASE, 'ADMIN_IDS': 'abc'})


class FormattingTest(unittest.TestCase):
    DEVICE = ClientDevice(
        index=0, mac='aa-bb-cc-dd-ee-01', ip='192.168.0.100', name='<iPhone>',
        connection=Connection.HOST_5G, online=True, blocked=False, up_speed=10, down_speed=50,
    )

    def test_uptime(self):
        self.assertEqual(fmt_uptime(None), '—')
        self.assertEqual(fmt_uptime(0), '—')
        self.assertEqual(fmt_uptime(90061), '1 дн 1 ч 1 мин')
        self.assertEqual(fmt_uptime(59), '0 мин')

    def test_device_escapes_html(self):
        text = format_device(self.DEVICE)
        self.assertIn('&lt;iPhone&gt;', text)
        self.assertIn('192.168.0.100', text)

    def test_clients_empty(self):
        self.assertIn('нет', format_clients([]))

    def test_clients_groups(self):
        text = format_clients([self.DEVICE])
        self.assertIn('Онлайн', text)

    def test_wifi_view(self):
        from routerbot.c80_client import WifiConfig
        text = format_wifi_view({
            Connection.HOST_2G: WifiConfig(Connection.HOST_2G, True, 'Net', 'pw', True),
        })
        self.assertIn('Net', text)
        self.assertIn('🟢', text)

    def test_fmt_traffic(self):
        self.assertEqual(fmt_traffic(None), '—')
        self.assertEqual(fmt_traffic(512), '512 Б')
        self.assertEqual(fmt_traffic(450 * 1024 ** 2), '450.0 МБ')
        self.assertEqual(fmt_traffic(3.5 * 1024 ** 3), '3.5 ГБ')

    def test_fmt_speed_limit(self):
        self.assertEqual(fmt_speed_limit(None, 1024), 'без лимита')
        self.assertEqual(fmt_speed_limit(1048576, 1048576), 'без лимита')
        self.assertEqual(fmt_speed_limit(20480, 1048576), '20 Мбит/с')
        self.assertEqual(fmt_speed_limit(512, 1048576), '512 Кбит/с')

    def test_stats(self):
        d = ClientDevice(
            index=0, mac='aa-bb-cc-dd-ee-01', ip='192.168.0.100', name='iPhone',
            connection=Connection.HOST_2G, online=True, blocked=False,
            total_traffic=450 * 1024 ** 2, down_speed=200, up_speed=30,
        )
        text = format_stats([d])
        self.assertIn('450.0 МБ', text)
        self.assertIn('iPhone', text)
        self.assertIn('Топ', text)

    def test_device_detail_shows_limit(self):
        d = ClientDevice(
            index=0, mac='aa-bb-cc-dd-ee-01', ip='192.168.0.100', name='PC',
            connection=Connection.WIRED, online=True, blocked=False,
            down_limit=20480, up_limit=204800, total_traffic=None,
        )
        text = format_device_detail(d)
        self.assertIn('20 Мбит/с', text)
        self.assertIn('без лимита', text)


class BandParseTest(unittest.TestCase):
    def test_parse(self):
        from routerbot.bot import BANDS, _parse_band_and_value

        cmd = SimpleNamespace(args='5g NewPassword123')
        band, value = _parse_band_and_value(cmd, 'usage')
        self.assertEqual(band, Connection.HOST_5G)
        self.assertEqual(value, 'NewPassword123')

        cmd = SimpleNamespace(args='g2g')
        with self.assertRaises(ValueError):
            _parse_band_and_value(cmd, 'usage')

        self.assertIs(BANDS['2.4'], Connection.HOST_2G)
        self.assertIs(BANDS['г5'], Connection.GUEST_5G)


class MtprotoCheckerTest(unittest.TestCase):
    def test_parse_link(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'tools'))
        from check_mtproto import parse_target

        host, port = parse_target(
            'https://t.me/proxy?server=live.lovely.lat&port=443&secret=ee1234')
        self.assertEqual((host, port), ('live.lovely.lat', 443))

        host, port = parse_target(' example.com:8443 ')
        self.assertEqual((host, port), ('example.com', 8443))

        self.assertIsNone(parse_target('просто текст'))
        self.assertIsNone(parse_target('https://t.me/proxy?port=443'))
        self.assertIsNone(parse_target(''))


class WolTest(unittest.TestCase):
    def test_magic_packet(self):
        from routerbot.wol import WOL_PORT, build_magic_packet
        self.assertEqual(WOL_PORT, 9)
        packet = build_magic_packet('AA-BB-CC-DD-EE-01')
        self.assertEqual(len(packet), 102)
        self.assertEqual(packet[:6], b'\xff' * 6)
        mac = bytes.fromhex('aabbccddee01')
        self.assertEqual(packet[6:12], mac)
        self.assertEqual(packet[96:102], mac)

    def test_bad_mac(self):
        from routerbot.wol import build_magic_packet
        for bad in ('`,;p', 'aa-bb-cc-dd-ee', 'zz-bb-cc-dd-ee-01', '00:00:00:00:00:00'):
            with self.assertRaises(ValueError, msg=bad):
                build_magic_packet(bad)


RESERVATIONS_BLOCK = (
    '00000\r\n'
    'id 12|1,0,0\r\n'
    'ip 0 192.168.0.55\r\n'
    'ip 1 192.168.0.60\r\n'
    'mac 0 aa-bb-cc-dd-ee-01\r\n'
    'mac 1 aa-bb-cc-dd-ee-02\r\n'
    'name 0 iPhone\r\n'
    'name 1 TV\r\n'
    'dhcpsEnable 0 1\r\n'
    'dhcpsEnable 1 0'
)


class ReservationTest(unittest.TestCase):
    def _client(self, reply='00000\r\n'):
        client = ArcherC80Client.__new__(ArcherC80Client)
        client._encrypt_body = lambda text: text
        client._decrypt_data = lambda text: text
        client._return_data_block = lambda bid: {'12|1,0,0': RESERVATIONS_BLOCK.split('\r\n')}
        captured = {}

        def fake_request(code, asyn, use_token=False, data=None):
            captured['body'] = data
            return SimpleNamespace(text=reply)

        client.request = fake_request
        client._captured = captured
        return client

    def test_parse(self):
        client = self._client()
        reservations = client.get_reservations()
        self.assertEqual(len(reservations), 2)
        first = reservations[0]
        self.assertEqual((first.index, first.mac, first.ip, first.name, first.enabled),
                         (0, 'aa-bb-cc-dd-ee-01', '192.168.0.55', 'iPhone', True))
        self.assertFalse(reservations[1].enabled)

    def test_add_new_index(self):
        client = self._client()
        client.add_reservation('AA:BB:CC:DD:EE:09', '192.168.0.99', 'Ноут', True)
        body = client._captured['body']
        # следующий свободный индекс — 2
        self.assertIn('mac 2 aa-bb-cc-dd-ee-09', body)
        self.assertIn('ip 2 192.168.0.99', body)
        self.assertIn('dhcpsEnable 2 1', body)

    def test_add_updates_existing_mac(self):
        client = self._client()
        client.add_reservation('aa-bb-cc-dd-ee-02', '192.168.0.77')
        body = client._captured['body']
        self.assertIn('mac 1 aa-bb-cc-dd-ee-02', body)  # тот же индекс
        self.assertIn('ip 1 192.168.0.77', body)

    def test_add_ip_clash(self):
        client = self._client()
        with self.assertRaises(RouterCommandError):
            client.add_reservation('aa-bb-cc-dd-ee-09', '192.168.0.55')  # IP занят

    def test_remove_zeroes_entry(self):
        client = self._client()
        removed = client.remove_reservation('aa-bb-cc-dd-ee-01')
        self.assertEqual(removed.ip, '192.168.0.55')
        body = client._captured['body']
        self.assertIn('mac 0 00-00-00-00-00-00', body)
        self.assertIn('ip 0 0.0.0.0', body)

    def test_validate_ip(self):
        from routerbot.c80_client import validate_ip
        self.assertEqual(validate_ip(' 192.168.0.55 '), '192.168.0.55')
        for bad in ('192.168.0', '192.168.0.256', 'abc.def.ghi.jkl', '192.168.0.1.1'):
            with self.assertRaises(ValueError, msg=bad):
                validate_ip(bad)


class SchedulerTest(unittest.TestCase):
    def setUp(self):
        from routerbot.scheduler import TaskStore
        self._tmp = tempfile.TemporaryDirectory()
        self.store = TaskStore(Path(self._tmp.name) / 'tasks.json')

    def tearDown(self):
        self._tmp.cleanup()

    def test_parse_time(self):
        self.assertEqual(parse_time('4:00'), '04:00')
        self.assertEqual(parse_time('23:59'), '23:59')
        for bad in ('24:00', '12.30', 'abc', '-1:30', '10:99'):
            with self.assertRaises(ValueError, msg=bad):
                parse_time(bad)

    def test_parse_days(self):
        self.assertIsNone(parse_days(''))
        self.assertIsNone(parse_days('ежедневно'))
        self.assertEqual(parse_days('пн,ср,пт'), [0, 2, 4])
        self.assertEqual(parse_days('mon,FRI'), [0, 4])
        with self.assertRaises(ValueError):
            parse_days('вторник')

    def test_add_list_remove(self):
        task = self.store.add('reboot', '04:00', None)
        self.assertIn('перезагрузка', task.describe())
        self.assertIn('ежедневно', task.describe())
        self.assertEqual(len(self.store.all()), 1)
        self.assertIsNotNone(self.store.remove(task.id))
        self.assertEqual(self.store.all(), [])
        self.assertIsNone(self.store.remove('nope'))

    def test_persistence(self):
        from routerbot.scheduler import TaskStore
        path = Path(self._tmp.name) / 'tasks.json'
        self.store.add('wifi5_off', '23:00', [4, 5])
        fresh = TaskStore(path)
        self.assertEqual(len(fresh.all()), 1)
        self.assertEqual(fresh.all()[0].days, [4, 5])

    def test_due_logic(self):
        task = self.store.add('reboot', '04:00', [0])  # понедельник
        monday = datetime(2026, 9, 7, 4, 0)  # 07.09.2026 — понедельник
        self.assertIn(task, self.store.due(monday))
        # воскресенье — задача только по понедельникам
        self.assertNotIn(task, self.store.due(datetime(2026, 9, 6, 4, 0)))
        # не в своё время
        self.assertNotIn(task, self.store.due(datetime(2026, 9, 7, 4, 1)))
        # уже выполнена в эту минуту
        self.store.mark_run(task, monday)
        self.assertNotIn(task, self.store.due(monday))

    def test_unknown_action_rejected(self):
        with self.assertRaises(ValueError):
            self.store.add('hack_nasa', '04:00', None)


class ReservationsFormattingTest(unittest.TestCase):
    def test_empty_with_hint(self):
        self.assertIn('/reserve', format_reservations([]))

    def test_lines(self):
        from routerbot.c80_client import Reservation
        text = format_reservations([
            Reservation(0, 'aa-bb-cc-dd-ee-01', '192.168.0.55', '<TV>', True),
        ])
        self.assertIn('192.168.0.55', text)
        self.assertIn('&lt;TV&gt;', text)
        self.assertIn('🟢', text)


LOGS_BLOCK_SAMPLE = (
    '00000\r\n'
    'id 2|1,0,0\r\n'
    'num 23\r\n'
    'level 0 3\r\n'
    'level 1 1\r\n'
    'level 2 3\r\n'
    'time 0 00:10:22\r\n'
    'time 1 00:12:05\r\n'
    'time 2 00:12:06\r\n'
    'msg 0 DHCP assigned 192.168.0.222 to aa-bb-cc-dd-ee-01\r\n'
    'msg 1 [dhcps]Lease%20host%20name%20not%20found.\r\n'
    'msg 2 WiFi client <iphone> connected'
)

LOGS_STRANGE_FORMAT = (
    '00000\r\n'
    'id 2|1,0,0\r\n'
    'num 5\r\n'
    'level 0 3\r\n'
    'blob AAAA1111\r\n'
    'blob BBBB2222'
)

WAN_BLOCK_SAMPLE = (
    '00000\r\n'
    'id 23|1,0,0\r\n'
    'ip 100.117.86.10\r\n'
    'status 1\r\n'
    'upTime 151080\r\n'
    'inRates 33631\r\n'
    'outRates 62727'
)

DMZ_BLOCK_SAMPLE = '00000\r\nid 18|1,0,0\r\ndmzEnable 1\r\ndmzClient 192.168.0.55'
UPNP_BLOCK_SAMPLE = '00000\r\nid 19|1,0,0\r\nigdEnable 1'

DDNS_BLOCK_SAMPLE = (
    '00000\r\n'
    'id 38|1,0,0\r\n'
    'mode 2\r\n'
    'enable 0 1\r\n'
    'enable 1 0\r\n'
    'username 0 myuser\r\n'
    'username 1 \r\n'
    'domainName 0 myhome.ddns.net\r\n'
    'status 0 0\r\n'
    'status 1 0'
)
DDNS_STATUS_SAMPLE = '00000\r\nid 39|1,0,0\r\nstatus 0 1\r\nstatus 1 0'


class RouterModulesTest(unittest.TestCase):
    def _client(self, blocks: dict[str, str]):
        client = ArcherC80Client.__new__(ArcherC80Client)
        client._encrypt_body = lambda text: text
        client._decrypt_data = lambda text: text
        client._return_data_block = lambda bid: {k: v.split('\r\n') for k, v in blocks.items()}
        captured = {}

        def fake_request(code, asyn, use_token=False, data=None):
            captured['body'] = data
            return SimpleNamespace(text='00000\r\n')

        client.request = fake_request
        client._captured = captured
        return client

    def test_logs_parsed_newest_first(self):
        client = self._client({'2|1,0,0': LOGS_BLOCK_SAMPLE})
        entries, raw = client.get_logs(12)
        self.assertEqual(raw, [])
        self.assertEqual(len(entries), 3)
        self.assertEqual(entries[0].msg, 'WiFi client <iphone> connected')  # idx=2 — новее
        self.assertEqual(entries[1].level, 1)
        self.assertEqual(entries[1].msg, '[dhcps]Lease host name not found.')  # %20 → пробел
        self.assertEqual(entries[0].time, '00:12:06')

    def test_logs_count_limit(self):
        client = self._client({'2|1,0,0': LOGS_BLOCK_SAMPLE})
        entries, _ = client.get_logs(1)
        self.assertEqual(len(entries), 1)

    def test_logs_raw_fallback(self):
        client = self._client({'2|1,0,0': LOGS_STRANGE_FORMAT})
        entries, raw = client.get_logs(12)
        self.assertEqual(entries, [])
        self.assertTrue(any('BBBB2222' in line for line in raw))

    def test_wan_speed(self):
        client = self._client({'23|1,0,0': WAN_BLOCK_SAMPLE})
        self.assertEqual(client.get_wan_speed(), (33631, 62727))

    def test_dmz_get_set(self):
        client = self._client({'18|1,0,0': DMZ_BLOCK_SAMPLE})
        self.assertEqual(client.get_dmz(), (True, '192.168.0.55'))
        client.set_dmz('192.168.0.60')
        self.assertIn('dmzEnable 1', client._captured['body'])
        self.assertIn('dmzClient 192.168.0.60', client._captured['body'])
        client.set_dmz(None)
        self.assertIn('dmzEnable 0', client._captured['body'])

    def test_upnp(self):
        client = self._client({'19|1,0,0': UPNP_BLOCK_SAMPLE})
        self.assertTrue(client.get_upnp())
        client.set_upnp(False)
        self.assertIn('igdEnable 0', client._captured['body'])

    def test_ddns_read(self):
        client = self._client({'38|1,0,0': DDNS_BLOCK_SAMPLE, '39|1,0,0': DDNS_STATUS_SAMPLE})
        info = client.get_ddns()
        self.assertEqual(info.mode, 2)
        self.assertEqual(len(info.entries), 2)
        e0 = info.entries[0]
        self.assertTrue(e0.enabled)
        self.assertEqual(e0.domain, 'myhome.ddns.net')
        self.assertEqual(e0.status, 1)  # статус подтянулся из блока 39
        self.assertFalse(info.entries[1].enabled)

    def test_ddns_toggle_writes_index(self):
        client = self._client({'38|1,0,0': DDNS_BLOCK_SAMPLE})
        client.set_ddns_enable(1, True)
        self.assertIn('enable 1 1', client._captured['body'])


class ModulesFormattingTest(unittest.TestCase):
    def test_logs_escape_and_icons(self):
        from routerbot.c80_client import LogEntry
        entries = [LogEntry(index=5, level=1, time='01:02:03', msg='WAN <error> happened')]
        text = format_logs(entries, [], 12)
        self.assertIn('🔴', text)
        self.assertIn('&lt;error&gt;', text)
        self.assertIn('01:02:03', text)

    def test_logs_dhcp_filtered_view(self):
        from routerbot.c80_client import LogEntry
        text = format_logs([LogEntry(2, 3, None, 'WAN up')], [], 24, filtered=23)
        self.assertIn('без DHCP-шума', text)
        self.assertIn('скрыто DHCP-строк: 23', text)
        only_noise = format_logs([], [], 24, filtered=24)
        self.assertIn('больше ничего нет', only_noise)

    def test_logs_raw_tail(self):
        text = format_logs([], ['num 5', 'blob AAAA'], 12)
        self.assertIn('сырой', text)
        self.assertIn('AAAA', text)

    def test_ports_view(self):
        text = format_ports(True, True, '192.168.0.55')
        self.assertIn('192.168.0.55', text)
        self.assertIn('🟢', text)

    def test_ddns_view(self):
        from routerbot.c80_client import DdnsEntry, DdnsInfo
        info = DdnsInfo(mode=2, entries=[DdnsEntry(0, True, 'u<ser>', 'x.ddns.net', 1)])
        text = format_ddns(info)
        self.assertIn('x.ddns.net', text)
        self.assertIn('&lt;', text)


if __name__ == '__main__':
    unittest.main()
