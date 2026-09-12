"""Запуск: python -m routerbot [--check | --probe-blocks]

  --check          проверить конфигурацию и доступность роутера, не запуская бота
  --probe-blocks   диагностика: сканирует блоки протокола роутера (0..80|1,0,0)
                   и печатает непустые. Нужен, чтобы найти блоки логов, DDNS,
                   проброса портов и т.п. Вывод можно отправить разработчику.
  --probe-block N  полный дамп одного блока (напр. --probe-block 20 или
                   --probe-block 33|1,1,0) — все строки без обрезки.
"""

from __future__ import annotations

import asyncio
import sys

from dotenv import load_dotenv


def _load_settings_or_exit():
    from .config import ConfigError, load_settings

    load_dotenv()
    try:
        return load_settings()
    except ConfigError as exc:
        print(f'❌ Конфигурация: {exc}')
        sys.exit(1)


def _check() -> int:
    from .service import check_router_reachable

    settings = _load_settings_or_exit()
    print('✅ Конфигурация OK')
    print(f'   Роутер:  {settings.router_host}')
    print(f'   Админов: {len(settings.admin_ids)}'
          + (' ⚠️  бот откажет всем — впиши свой ID!' if not settings.admin_ids else ''))
    print(f'   Токен TG: {settings.bot_token[:8]}…')

    try:
        target = asyncio.run(check_router_reachable(settings))
    except Exception as exc:
        print(f'❌ Роутер недоступен: {exc}')
        print('   Проверь, что ты в одной сети с роутером и адрес верный.')
        return 1
    print(f'✅ Роутер на связи ({target})')
    print('\nЕсли обе проверки прошли — запускай:  python -m routerbot')
    return 0


def _probe_blocks() -> int:
    """Скан блоков dsp-протокола: ищем, что ещё умеет прошивка."""
    settings = _load_settings_or_exit()

    async def _scan() -> None:
        from .service import RouterService
        service = RouterService(settings)

        def job(client):
            results = {}
            for idx in range(0, 81):
                block_id = f'{idx}|1,0,0'
                try:
                    lines = client.read_module(block_id)
                except Exception:
                    continue
                if lines:
                    results[block_id] = [l for l in lines if l != '00000']
            return results

        return await service.run(job)

    print(f'Сканирую блоки протокола на {settings.router_host} (1–2 минуты)...')
    try:
        results = asyncio.run(_scan())
    except Exception as exc:
        print(f'❌ Не получилось: {exc}')
        return 1

    print(f'\nНайдено блоков: {len(results)}\n')
    for block_id, lines in results.items():
        print(f'=== {block_id} ===')
        for line in lines[:14]:
            print(f'  {line}')
        if len(lines) > 14:
            print(f'  ... (+{len(lines) - 14} строк)')
    print('\nОтправь этот вывод разработчику бота — по нему добавим логи/DDNS/проброс портов.')
    return 0


def _probe_block(block_arg: str) -> int:
    """Полный дамп одного блока протокола."""
    settings = _load_settings_or_exit()
    block_id = block_arg if '|' in block_arg else f'{block_arg}|1,0,0'

    async def _read() -> None:
        from .service import RouterService
        return await RouterService(settings).run(lambda c: c.read_module(block_id))

    try:
        lines = asyncio.run(_read())
    except Exception as exc:
        print(f'❌ Не получилось: {exc}')
        return 1
    if not lines:
        print(f'Блок {block_id} пустой или не существует.')
        return 0
    print(f'=== {block_id} ({len(lines)} строк) ===')
    for line in lines:
        print(line)
    return 0


def _run() -> int:
    from .bot import main

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print('\nБот остановлен.')
    return 0


if __name__ == '__main__':
    if '--check' in sys.argv:
        sys.exit(_check())
    if '--probe-blocks' in sys.argv:
        sys.exit(_probe_blocks())
    if '--probe-block' in sys.argv:
        i = sys.argv.index('--probe-block')
        arg = sys.argv[i + 1] if i + 1 < len(sys.argv) else ''
        if not arg:
            print('Укажи номер блока: python -m routerbot --probe-block 20')
            sys.exit(1)
        sys.exit(_probe_block(arg))
    sys.exit(_run())
