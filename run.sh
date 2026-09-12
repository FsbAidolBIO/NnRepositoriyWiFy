#!/usr/bin/env bash
# Запуск бота одним кликом/одной командой: ./run.sh
# Просто запускает правильный питон из venv, больше ничего.
set -e
cd "$(dirname "$0")"
exec .venv/bin/python -m routerbot "$@"
