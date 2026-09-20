#!/usr/bin/env bash
# Сборка единого исполняемого файла LinScreen.
#
# Результат: dist/linscreen - один файл, не требующий ни Python, ни Qt,
# ни пакета ffmpeg. Запуск сборки: ./build.sh
#
# Переменная LINSCREEN_BUNDLE_FFMPEG=0 собирает облегчённый вариант,
# использующий системный ffmpeg (файл меньше примерно на 80 МБ).

set -euo pipefail
cd "$(dirname "$0")"

VENV=".venv"
# Сборка BtbN выбрана из-за поддержки PulseAudio: без неё запись звука
# невозможна. Зависит только от glibc, поэтому работает в любом
# современном дистрибутиве.
FFMPEG_URL="https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-n8.1-latest-linux64-gpl-8.1.tar.xz"

# Окружение сборки создаётся при первом запуске.
if [ ! -x "$VENV/bin/python" ]; then
    echo "Создание окружения…"
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install -q --upgrade pip
    "$VENV/bin/pip" install -q -r requirements.txt pyinstaller
fi

# Статическая сборка FFmpeg загружается однократно и вкладывается в файл.
if [ "${LINSCREEN_BUNDLE_FFMPEG:-1}" != "0" ] && [ ! -x vendor/ffmpeg ]; then
    echo "Загрузка статической сборки FFmpeg…"
    mkdir -p vendor
    curl -L -o vendor/ffmpeg.tar.xz "$FFMPEG_URL"
    tar -xJf vendor/ffmpeg.tar.xz -C vendor
    mv vendor/ffmpeg-*/bin/ffmpeg vendor/ffmpeg
    rm -rf vendor/ffmpeg-n* vendor/ffmpeg.tar.xz
    chmod +x vendor/ffmpeg
fi

echo "Сборка…"
rm -rf build dist
"$VENV/bin/pyinstaller" --noconfirm --clean linscreen.spec

echo
echo "Готово: $(realpath dist/linscreen) ($(du -h dist/linscreen | cut -f1))"
echo "Проверка: dist/linscreen --check"
