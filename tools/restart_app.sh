#!/bin/bash
# Перезапуск программы из исходных текстов (инструмент разработчика).
# Процесс останавливается по номеру из файла блокировки: поиск по имени
# команды задел бы и саму оболочку, в тексте которой есть это имя.
# Использование: tools/restart_app.sh [stop] ; вывод пишется в $LOG.
cd "$(dirname "$0")/.."
LOG="${LOG:-/tmp/linscreen-dev.log}"
LOCK="${XDG_RUNTIME_DIR:-/tmp}/linscreen.lock"
if [ -f "$LOCK" ]; then
  PID=$(head -1 "$LOCK")
  if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
    kill -INT "$PID"
    for _ in $(seq 1 50); do kill -0 "$PID" 2>/dev/null || break; sleep 0.1; done
  fi
fi
[ "$1" = "stop" ] && exit 0
setsid .venv/bin/python main.py >"$LOG" 2>&1 &
sleep 3
echo "started, log: $LOG"
