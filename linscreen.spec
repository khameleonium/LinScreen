# -*- mode: python ; coding: utf-8 -*-
"""
Спецификация сборки единого исполняемого файла.

Сборка выполняется командой:
    .venv/bin/pyinstaller linscreen.spec

Результат складывается в каталог dist и представляет собой один файл,
не требующий ни установленного Python, ни библиотек Qt, ни пакета ffmpeg.

Переменная окружения LINSCREEN_BUNDLE_FFMPEG=0 собирает облегчённый
вариант, использующий системный ffmpeg.
"""

import os
from pathlib import Path

PROJECT = Path(os.getcwd())

# Вложенные бинарники. Статическая сборка ffmpeg не имеет зависимостей,
# поэтому работает в любом дистрибутиве без установки пакетов.
binaries = []
if os.environ.get("LINSCREEN_BUNDLE_FFMPEG", "1") != "0":
    bundled_ffmpeg = PROJECT / "vendor" / "ffmpeg"
    if bundled_ffmpeg.is_file():
        # Точка назначения "." помещает файл в корень распаковки,
        # где его ищет encoder/ffmpeg.py через bundle_dir().
        binaries.append((str(bundled_ffmpeg), "."))

# Модули Qt, не используемые приложением. Исключение сокращает размер
# результата более чем вдвое: сборщик иначе тянет весь комплект Qt.
excluded_qt = [
    "PySide6.Qt3DAnimation", "PySide6.Qt3DCore", "PySide6.Qt3DExtras",
    "PySide6.Qt3DInput", "PySide6.Qt3DLogic", "PySide6.Qt3DRender",
    "PySide6.QtBluetooth", "PySide6.QtCharts", "PySide6.QtDataVisualization",
    "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtLocation",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "PySide6.QtNetwork",
    "PySide6.QtNfc", "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets",
    "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtPositioning",
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuick3D",
    "PySide6.QtQuickControls2", "PySide6.QtQuickWidgets", "PySide6.QtRemoteObjects",
    "PySide6.QtScxml", "PySide6.QtSensors", "PySide6.QtSerialBus",
    "PySide6.QtSerialPort", "PySide6.QtSpatialAudio", "PySide6.QtSql",
    "PySide6.QtStateMachine", "PySide6.QtSvg", "PySide6.QtSvgWidgets",
    "PySide6.QtTest", "PySide6.QtTextToSpeech", "PySide6.QtUiTools",
    "PySide6.QtWebChannel", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineQuick",
    "PySide6.QtWebEngineWidgets", "PySide6.QtWebSockets", "PySide6.QtXml",
]

# Прочие крупные пакеты, попадающие в сборку по цепочке зависимостей.
excluded_other = [
    "tkinter", "unittest", "pytest", "mypy", "flake8", "PyInstaller",
    "numpy", "scipy", "matplotlib", "IPython", "pydoc_data",
]

analysis = Analysis(
    ["main.py"],
    pathex=[str(PROJECT)],
    binaries=binaries,
    datas=[],
    # Модуль подключается Pillow во время работы и не виден статическому
    # анализу импортов, поэтому указывается явно.
    hiddenimports=["pillow_avif"],
    hookspath=[],
    runtime_hooks=[],
    excludes=excluded_qt + excluded_other,
    noarchive=False,
)

pyz = PYZ(analysis.pure)

executable = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="linscreen",
    debug=False,
    bootloader_ignore_signals=False,
    # Сжатие исполняемого файла отключено: распаковка UPX заметно
    # задерживает запуск, а выигрыш в размере невелик.
    upx=False,
    strip=False,
    runtime_tmpdir=None,
    # Приложение живёт в трее и терминала не требует; сообщения о сбоях
    # пишутся в файл журнала, см. main.py.
    console=False,
    disable_windowed_traceback=False,
)
