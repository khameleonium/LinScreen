"""
Сведения о способе запуска приложения.

Приложение работает в двух видах: из исходных текстов и в виде единого
собранного файла. Во втором случае интерпретатор и вложенные бинарники
распаковываются во временный каталог, путь к которому сборщик записывает
в переменную sys._MEIPASS. Модуль скрывает это различие от остального кода.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def is_appimage() -> bool:
    """Признак запуска из смонтированного образа AppImage."""
    return bool(os.environ.get("APPIMAGE"))


def appimage_path() -> Path | None:
    """Путь к исполняемому файлу AppImage на диске пользователя."""
    path = os.environ.get("APPIMAGE")
    return Path(path).resolve() if path else None


def is_frozen() -> bool:
    """Признак запуска из собранного исполняемого файла."""
    return bool(getattr(sys, "frozen", False))


def bundle_dir() -> Path | None:
    """
    Каталог с вложенными в сборку файлами.

    Пустое значение означает запуск из исходных текстов, при котором
    вложений не существует.
    """
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) if base else None


def executable_path() -> Path:
    """
    Путь, которым приложение запускается повторно.

    Для собранного файла это он сам, для исходных текстов - интерпретатор
    вместе с точкой входа, что учитывается вызывающей стороной.
    Символические ссылки не раскрываются через resolve(), чтобы сохранить
    путь к интерпретатору виртуального окружения.
    """
    return Path(sys.executable).absolute()


# Переменные, которые сборщик подменяет на время работы приложения,
# сохраняя исходные значения в одноимённых переменных с суффиксом _ORIG.
_REPLACED_VARIABLES = (
    "LD_LIBRARY_PATH",
    "LD_PRELOAD",
    "QT_PLUGIN_PATH",
    "QML2_IMPORT_PATH",
    "GST_PLUGIN_PATH",
)


def child_environment(source: dict[str, str] | None = None) -> dict[str, str]:
    """
    Окружение для запуска системных утилит из собранного приложения.

    Сборщик подменяет пути поиска библиотек, чтобы приложение пользовалось
    вложенными копиями. Системная утилита, унаследовав такое окружение,
    загрузила бы чужие библиотеки и отказалась работать, поэтому исходные
    значения восстанавливаются.
    """
    environment = dict(source if source is not None else os.environ)
    if not is_frozen():
        return environment

    for name in _REPLACED_VARIABLES:
        original = environment.pop(f"{name}_ORIG", None)
        if original is not None:
            # Исходное значение возвращается на место подменённого.
            environment[name] = original
        else:
            # Исходного значения не было: подменённое подлежит удалению.
            environment.pop(name, None)
    return environment


def project_root() -> Path:
    """Корневой каталог проекта при запуске из исходных текстов."""
    return Path(__file__).resolve().parent.parent
