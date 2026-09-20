#!/usr/bin/env bash
# Сборка LinScreen в единый исполняемый файл.
#
# По умолчанию собирается образ AppImage: он монтируется при запуске, а не
# распаковывается, поэтому стартует примерно за полсекунды вместо трёх и
# бережнее относится к слабому оборудованию. Это по-прежнему один файл,
# запускаемый двойным щелчком.
#
#   ./build.sh                        образ AppImage (по умолчанию)
#   LINSCREEN_FORMAT=onefile ./build.sh   один файл PyInstaller
#   LINSCREEN_FORMAT=dir ./build.sh       каталог без упаковки
#   LINSCREEN_BUNDLE_FFMPEG=0 ./build.sh  без вложенного ffmpeg
#
# Вложенная сборка FFmpeg выбрана с поддержкой PulseAudio и SVT-AV1,
# зависит только от glibc и работает в любом современном дистрибутиве.

set -euo pipefail
cd "$(dirname "$0")"

VENV=".venv"
FORMAT="${LINSCREEN_FORMAT:-appimage}"
FFMPEG_URL="https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-n8.1-latest-linux64-gpl-8.1.tar.xz"
APPIMAGETOOL_URL="https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage"

# Окружение сборки создаётся при первом запуске.
if [ ! -x "$VENV/bin/python" ]; then
    echo "Создание окружения…"
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install -q --upgrade pip
    "$VENV/bin/pip" install -q -r requirements.txt pyinstaller
fi

# Сборка FFmpeg загружается однократно и вкладывается в приложение.
if [ "${LINSCREEN_BUNDLE_FFMPEG:-1}" != "0" ] && [ ! -x vendor/ffmpeg ]; then
    echo "Загрузка сборки FFmpeg…"
    mkdir -p vendor
    curl -L -o vendor/ffmpeg.tar.xz "$FFMPEG_URL"
    tar -xJf vendor/ffmpeg.tar.xz -C vendor
    mv vendor/ffmpeg-*/bin/ffmpeg vendor/ffmpeg
    rm -rf vendor/ffmpeg-n* vendor/ffmpeg.tar.xz
    chmod +x vendor/ffmpeg
fi

rm -rf build dist

if [ "$FORMAT" = "onefile" ]; then
    echo "Сборка единого файла…"
    "$VENV/bin/pyinstaller" --noconfirm --clean linscreen.spec
    echo
    echo "Готово: $(realpath dist/linscreen) ($(du -h dist/linscreen | cut -f1))"
    echo "Проверка: dist/linscreen --check"
    exit 0
fi

echo "Сборка каталога…"
LINSCREEN_ONEDIR=1 "$VENV/bin/pyinstaller" --noconfirm --clean linscreen.spec

if [ "$FORMAT" = "dir" ]; then
    echo
    echo "Готово: $(realpath dist/linscreen)"
    exit 0
fi

# Инструмент упаковки загружается однократно.
if [ ! -x vendor/appimagetool ]; then
    echo "Загрузка упаковщика AppImage…"
    mkdir -p vendor
    curl -L -o vendor/appimagetool "$APPIMAGETOOL_URL"
    chmod +x vendor/appimagetool
fi

echo "Подготовка образа…"
APPDIR="build/AppDir"
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin" "$APPDIR/usr/share/icons/hicolor/256x256/apps"
cp -a dist/linscreen/. "$APPDIR/usr/bin/"

# Значок рисуется тем же кодом, что и значок в трее.
QT_QPA_PLATFORM=offscreen "$VENV/bin/python" - "$APPDIR/linscreen.png" <<'PYTHON'
import sys
from PySide6.QtWidgets import QApplication

application = QApplication(sys.argv)
from encoder.process import RecorderState
from ui.tray import render_tray_icon

render_tray_icon(RecorderState.IDLE).pixmap(256, 256).save(sys.argv[1], "PNG")
PYTHON
cp "$APPDIR/linscreen.png" "$APPDIR/usr/share/icons/hicolor/256x256/apps/linscreen.png"

cat > "$APPDIR/linscreen.desktop" <<'DESKTOP'
[Desktop Entry]
Type=Application
Name=LinScreen
GenericName=Снимки экрана и запись видео
Comment=Снимки экрана, запись видео и редактор аннотаций
Exec=linscreen
Icon=linscreen
Terminal=false
Categories=Utility;Graphics;AudioVideo;
Keywords=screenshot;screencast;снимок;запись;экран;
DESKTOP

cat > "$APPDIR/AppRun" <<'APPRUN'
#!/bin/sh
# Точка входа образа: запускает приложение из смонтированного образа.
HERE="$(dirname "$(readlink -f "$0")")"
exec "$HERE/usr/bin/linscreen" "$@"
APPRUN
chmod +x "$APPDIR/AppRun"

echo "Упаковка образа…"
ARCH=x86_64 vendor/appimagetool --no-appstream "$APPDIR" dist/LinScreen-x86_64.AppImage

echo
echo "Готово: $(realpath dist/LinScreen-x86_64.AppImage) ($(du -h dist/LinScreen-x86_64.AppImage | cut -f1))"
echo "Проверка: dist/LinScreen-x86_64.AppImage --check"
