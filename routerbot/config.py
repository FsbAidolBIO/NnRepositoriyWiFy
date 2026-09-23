"""Настройки бота: загрузка из переменных окружения / .env."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


class ConfigError(Exception):
    """Ошибка конфигурации: понятное сообщение пользователю."""


@dataclass
class Settings:
    bot_token: str
    admin_ids: set[int]
    router_host: str
    router_password: str
    router_username: str = 'admin'
    router_timeout: int = 30
    verify_ssl: bool = False
    tg_proxy: str | None = None
    # менять не нужно:
    extra: dict = field(default_factory=dict)


def _parse_admin_ids(raw: str) -> set[int]:
    ids: set[int] = set()
    for chunk in raw.replace(';', ',').split(','):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            ids.add(int(chunk))
        except ValueError as exc:
            raise ConfigError(
                f'ADMIN_IDS: "{chunk}" не похоже на числовой Telegram ID.'
            ) from exc
    return ids


def _normalize_host(host: str) -> str:
    host = host.strip().rstrip('/')
    if not host:
        raise ConfigError('ROUTER_HOST пустой.')
    if not host.startswith(('http://', 'https://')):
        host = 'http://' + host
    return host


def load_settings(env: dict | None = None) -> Settings:
    """Читает настройки из env (по умолчанию os.environ)."""
    env = os.environ if env is None else env

    token = (env.get('TG_BOT_TOKEN') or '').strip()
    if not token:
        raise ConfigError(
            'Не задан TG_BOT_TOKEN. Создай бота через @BotFather, '
            'скопируй .env.example в .env и впиши токен.'
        )

    password = (env.get('ROUTER_PASSWORD') or '').strip()
    if not password:
        raise ConfigError(
            'Не задан ROUTER_PASSWORD — локальный пароль администратора роутера '
            '(тот, что вводится при входе в веб-интерфейс; НЕ TP-Link ID).'
        )

    try:
        timeout = int((env.get('ROUTER_TIMEOUT') or '30').strip())
    except ValueError as exc:
        raise ConfigError('ROUTER_TIMEOUT должен быть числом (секунды).') from exc

    return Settings(
        bot_token=token,
        admin_ids=_parse_admin_ids(env.get('ADMIN_IDS') or ''),
        router_host=_normalize_host(env.get('ROUTER_HOST') or 'http://192.168.0.1'),
        router_password=password,
        router_username=(env.get('ROUTER_USERNAME') or 'admin').strip() or 'admin',
        router_timeout=timeout,
        verify_ssl=(env.get('ROUTER_VERIFY_SSL') or '').strip().lower() in ('1', 'true', 'yes'),
        tg_proxy=(env.get('TG_PROXY') or '').strip() or None,
    )
