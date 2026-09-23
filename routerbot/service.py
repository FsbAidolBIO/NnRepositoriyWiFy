"""Сервисный слой: синхронный клиент роутера внутри асинхронного бота.

Важные особенности Archer C80:
  - веб-интерфейс допускает ТОЛЬКО ОДНУ админ-сессию одновременно, поэтому все
    операции сериализуются через один lock;
  - на каждое действие выполняется login -> действие -> logout, чтобы не
    блокировать вход в обычную веб-морду.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, TypeVar

from tplinkrouterc6u.common.exception import ClientException

from .c80_client import ArcherC80Client, RouterCommandError
from .config import Settings

log = logging.getLogger(__name__)

T = TypeVar('T')

# Роутер отверг команду, потому что сессию выбил другой клиент (веб-морда,
# второй экземпляр бота). Лечится повторным логином.
SESSION_CONFLICT_CODE = '00006'


class RouterService:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._lock = asyncio.Lock()

    def _new_client(self) -> ArcherC80Client:
        return ArcherC80Client(
            host=self._settings.router_host,
            password=self._settings.router_password,
            username=self._settings.router_username,
            logger=log,
            verify_ssl=self._settings.verify_ssl,
            timeout=self._settings.router_timeout,
        )

    async def run(self, action: Callable[[ArcherC80Client], T]) -> T:
        """Выполняет action(client) с авторизованной сессией (в отдельном потоке).

        Одна операция за раз: роутер не переваривает параллельные сессии.
        """
        async with self._lock:
            return await asyncio.to_thread(self._run_sync, action)

    def _run_sync(self, action: Callable[[ArcherC80Client], T]) -> T:
        attempt = 0
        while True:
            attempt += 1
            client = self._new_client()
            try:
                client.authorize()
            except ClientException:
                log.warning('Не удалось авторизоваться на роутере', exc_info=True)
                raise
            try:
                return action(client)
            except RouterCommandError as exc:
                if SESSION_CONFLICT_CODE in str(exc) and attempt < 3:
                    log.warning('Сессию перехватили (код %s), переавторизуюсь '
                                'и повторяю (попытка %d/3)', SESSION_CONFLICT_CODE, attempt + 1)
                    time.sleep(1)
                    continue
                raise
            finally:
                try:
                    client.logout()
                except Exception:  # noqa: BLE001 - logout не должен ронять операцию
                    log.debug('Ошибка при logout (не критично)', exc_info=True)


async def check_router_reachable(settings: Settings) -> str:
    """Проверка доступности роутера без авторизации (для --check)."""
    import socket
    from urllib.parse import urlparse

    host = urlparse(settings.router_host).hostname or '192.168.0.1'
    port = urlparse(settings.router_host).port or (443 if settings.router_host.startswith('https') else 80)

    def _probe() -> str:
        with socket.create_connection((host, port), timeout=5):
            return f'{host}:{port}'

    result = await asyncio.to_thread(_probe)
    return result
