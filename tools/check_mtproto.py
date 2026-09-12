#!/usr/bin/env python3
"""Проверка MTProto прокси-ссылок t.me/proxy: какие живы прямо сейчас с твоей сети.

Внимание: MTProto-прокси подходят ТОЛЬКО приложению Telegram, боту для Bot API
нужен HTTP/SOCKS5 прокси (переменная TG_PROXY в .env). Эта утилита проверяет
доступность серверов по TCP — полезно понять, что вообще живо.

Использование:
    python tools/check_mtproto.py proxies.txt
    python tools/check_mtproto.py "https://t.me/proxy?server=x&port=443&secret=..." "host:8443"
"""

from __future__ import annotations

import concurrent.futures as cf
import re
import socket
import sys
import time
from urllib.parse import parse_qs, urlparse

LINK_RE = re.compile(r't\.me/proxy\?[^\s]+')
HOSTPORT_RE = re.compile(r'^[\w.\-]+:\d+$')


def parse_target(line: str) -> tuple[str, int] | None:
    """Достаёт (host, port) из t.me/proxy-ссылки или 'host:port'."""
    line = line.strip()
    if not line:
        return None
    m = LINK_RE.search(line)
    if m:
        qs = parse_qs(urlparse(m.group(0)).query)
        server = (qs.get('server') or [''])[0].rstrip('.')
        try:
            port = int((qs.get('port') or [''])[0])
        except ValueError:
            return None
        return (server, port) if server else None
    if HOSTPORT_RE.match(line):
        host, _, port = line.rpartition(':')
        return host.rstrip('.'), int(port)
    return None


def check(target: tuple[str, int], timeout: float = 6.0) -> tuple[tuple[str, int], float | None]:
    """TCP-коннект: возвращает (target, задержка_с или None если мёртв)."""
    host, port = target
    started = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return target, time.monotonic() - started
    except Exception:
        return target, None


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1

    raw_lines: list[str] = []
    for arg in sys.argv[1:]:
        try:  # аргумент — это файл?
            with open(arg, encoding='utf-8') as fh:
                raw_lines.extend(fh.readlines())
        except OSError:
            raw_lines.append(arg)  # нет — сама ссылка/host:port

    targets: list[tuple[str, int]] = []
    seen = set()
    for line in raw_lines:
        target = parse_target(line)
        if target and target not in seen:
            seen.add(target)
            targets.append(target)

    if not targets:
        print('Не нашёл ни одной ссылки/host:port во вводе.')
        return 1

    print(f'Проверяю {len(targets)} прокси...\n')
    alive = 0
    with cf.ThreadPoolExecutor(10) as pool:
        results = sorted(pool.map(check, targets), key=lambda r: (r[1] is None, r[1] or 999))
        for (host, port), latency in results:
            if latency is None:
                print(f'❌ {host}:{port} — недоступен')
            else:
                alive += 1
                print(f'✅ {host}:{port} — живой, {latency * 1000:.0f} мс')

    print(f'\nИтого живыми по TCP: {alive}/{len(targets)}')
    print('Напоминание: для бота эти ссылки не подходят (это MTProto),')
    print('ему нужен HTTP/SOCKS5 прокси (TG_PROXY в .env).')
    return 0 if alive else 2


if __name__ == '__main__':
    sys.exit(main())
