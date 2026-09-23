"""Планировщик задач бота: крона на роутере нет, поэтому свой.

Задачи хранятся в tasks.json рядом с проектом и выполняются, пока запущен бот
(с systemd-автостартом = всегда). Время — локальное время машины, где работает бот.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from tplinkrouterc6u import Connection

from .c80_client import ArcherC80Client
from .service import RouterService

log = logging.getLogger(__name__)

WEEKDAYS = {
    'mon': 0, 'tue': 1, 'wed': 2, 'thu': 3, 'fri': 4, 'sat': 5, 'sun': 6,
    'пн': 0, 'вт': 1, 'ср': 2, 'чт': 3, 'пт': 4, 'сб': 5, 'вс': 6,
}
WEEKDAY_MARKS = ('пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс')

# действие -> (описание, client-функция)
ACTIONS: dict[str, tuple[str, Callable[[ArcherC80Client], None]]] = {
    'reboot': ('перезагрузка роутера', lambda c: c.reboot()),
    'wifi2_on': ('вкл Wi-Fi 2.4 ГГц', lambda c: c.set_wifi_state(Connection.HOST_2G, True)),
    'wifi2_off': ('выкл Wi-Fi 2.4 ГГц', lambda c: c.set_wifi_state(Connection.HOST_2G, False)),
    'wifi5_on': ('вкл Wi-Fi 5 ГГц', lambda c: c.set_wifi_state(Connection.HOST_5G, True)),
    'wifi5_off': ('выкл Wi-Fi 5 ГГц', lambda c: c.set_wifi_state(Connection.HOST_5G, False)),
    'guest2_on': ('вкл гостевую 2.4 ГГц', lambda c: c.set_wifi_state(Connection.GUEST_2G, True)),
    'guest2_off': ('выкл гостевую 2.4 ГГц', lambda c: c.set_wifi_state(Connection.GUEST_2G, False)),
    'guest5_on': ('вкл гостевую 5 ГГц', lambda c: c.set_wifi_state(Connection.GUEST_5G, True)),
    'guest5_off': ('выкл гостевую 5 ГГц', lambda c: c.set_wifi_state(Connection.GUEST_5G, False)),
}


@dataclass
class Task:
    id: str
    action: str
    time: str                # 'ЧЧ:ММ'
    days: list[int] | None = None   # None = ежедневно; 0=пн..6=вс
    last_run: str | None = None     # 'ГГГГ-ММ-ДДTЧЧ:ММ' метка последнего выполнения

    def describe(self) -> str:
        desc = ACTIONS.get(self.action, (self.action,))[0]
        days = 'ежедневно' if not self.days else ','.join(WEEKDAY_MARKS[d] for d in sorted(self.days))
        return f'{desc} в {self.time} ({days})'


def parse_days(raw: str) -> list[int] | None:
    """'пн,ср,пт' / 'mon,wed' -> [0,2,4]. Пустая строка -> None (ежедневно)."""
    raw = raw.strip().lower()
    if not raw or raw in ('daily', 'ежедневно'):
        return None
    days = []
    for chunk in raw.split(','):
        chunk = chunk.strip()
        if chunk not in WEEKDAYS:
            raise ValueError(f'Неизвестный день «{chunk}». Дни: пн,вт,ср,чт,пт,сб,вс')
        days.append(WEEKDAYS[chunk])
    return sorted(set(days))


def parse_time(raw: str) -> str:
    try:
        hour, minute = raw.split(':')
        hour, minute = int(hour), int(minute)
    except ValueError as exc:
        raise ValueError('Время в формате ЧЧ:ММ, например 04:00') from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError('Время в формате ЧЧ:ММ, например 04:00')
    return f'{hour:02d}:{minute:02d}'


class TaskStore:
    def __init__(self, path: str | Path = 'tasks.json'):
        self._path = Path(path)
        self._tasks: dict[str, Task] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding='utf-8'))
            self._tasks = {t['id']: Task(**t) for t in data}
        except (json.JSONDecodeError, TypeError, KeyError) as exc:
            log.warning('Не смог прочитать %s (%s), начинаю с пустого списка', self._path, exc)
            self._tasks = {}

    def _save(self) -> None:
        self._path.write_text(
            json.dumps([asdict(t) for t in self._tasks.values()], ensure_ascii=False, indent=2),
            encoding='utf-8',
        )

    def add(self, action: str, time: str, days: list[int] | None) -> Task:
        if action not in ACTIONS:
            raise ValueError(f'Действие «{action}» неизвестно. Доступные: {", ".join(ACTIONS)}')
        task = Task(id=secrets.token_hex(2), action=action, time=time, days=days)
        while task.id in self._tasks:
            task.id = secrets.token_hex(2)
        self._tasks[task.id] = task
        self._save()
        return task

    def remove(self, task_id: str) -> Task | None:
        task = self._tasks.pop(task_id, None)
        if task:
            self._save()
        return task

    def all(self) -> list[Task]:
        return sorted(self._tasks.values(), key=lambda t: (t.time, t.action))

    def due(self, now: datetime) -> list[Task]:
        marker = now.strftime('%Y-%m-%dT%H:%M')
        ready = []
        for task in self._tasks.values():
            if task.time != now.strftime('%H:%M') or task.last_run == marker:
                continue
            if task.days is not None and now.weekday() not in task.days:
                continue
            ready.append(task)
        return ready

    def mark_run(self, task: Task, now: datetime) -> None:
        task.last_run = now.strftime('%Y-%m-%dT%H:%M')
        self._save()


async def run_task(service: RouterService, task: Task) -> None:
    _, fn = ACTIONS[task.action]
    await service.run(fn)


async def scheduler_loop(service: RouterService, store: TaskStore,
                         notify: Callable[[str], None] | None = None,
                         interval: float = 30.0) -> None:
    log.info('Планировщик запущен, задач: %d', len(store.all()))
    while True:
        await asyncio.sleep(interval)
        try:
            now = datetime.now()
            for task in store.due(now):
                try:
                    await run_task(service, task)
                    log.info('Задача %s (%s) выполнена', task.id, task.describe())
                    if notify:
                        notify(f'✅ По расписанию: {task.describe()}')
                except Exception:
                    log.exception('Задача %s (%s) упала', task.id, task.describe())
                    if notify:
                        notify(f'❌ Задача {task.id} не выполнилась — см. лог бота')
                finally:
                    store.mark_run(task, now)
        except Exception:
            log.exception('Ошибка в цикле планировщика')
