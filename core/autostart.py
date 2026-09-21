"""
Управление автоматическим запуском приложения при входе в систему.

Запуск обеспечивается файлом .desktop в каталоге автозапуска по стандарту
XDG. Файл создаётся по текущим путям интерпретатора и точки входа, поэтому
переносить приложение можно свободно: достаточно переключить настройку заново.
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path

from core.i18n import tr
from core.runtime import (
    appimage_path,
    executable_path,
    is_appimage,
    is_frozen,
    project_root,
)

APPLICATION_ID = "linscreen"
DESKTOP_FILE_NAME = f"{APPLICATION_ID}.desktop"


def _config_home() -> Path:
    """Корневой каталог пользовательских настроек по стандарту XDG."""
    return Path(os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config"))


def _data_home() -> Path:
    """Корневой каталог пользовательских данных по стандарту XDG."""
    return Path(os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share"))


def menu_entry_file() -> Path:
    """Путь ярлыка в меню приложений."""
    return _data_home() / "applications" / DESKTOP_FILE_NAME


def autostart_dir() -> Path:
    """Каталог автозапуска по стандарту XDG."""
    return _config_home() / "autostart"


def autostart_file() -> Path:
    """Путь файла автозапуска приложения."""
    return autostart_dir() / DESKTOP_FILE_NAME


def launch_command() -> list[str]:
    """
    Команда повторного запуска приложения.

    Для образа AppImage используется путь к файлу самого образа. Для обычного
    собранного файла это он сам, для исходных текстов - текущий интерпретатор
    вместе с точкой входа, благодаря чему сохраняется запуск из виртуального
    окружения.
    """
    if is_appimage():
        image = appimage_path()
        if image is not None and image.is_file():
            return [str(image)]
    if is_frozen():
        return [str(executable_path())]
    return [str(executable_path()), str(project_root() / "main.py")]


def entry_point() -> tuple[str, Path]:
    """Интерпретатор и точка входа при запуске из исходных текстов."""
    return str(executable_path()), project_root() / "main.py"


def desktop_entry(icon: str = "camera-photo", autostart: bool = True) -> str:
    """
    Содержимое файла .desktop.

    Один и тот же вид записи используется и для автозапуска, и для ярлыка
    в меню приложений; различаются только поля автозапуска.
    """
    # Команда собирается с экранированием: путь может содержать пробелы,
    # а spec файла .desktop разбирает строку по правилам оболочки.
    command = shlex.join(launch_command())
    entry = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=LinScreen\n"
        f"GenericName={tr('Снимки экрана и запись видео')}\n"
        f"Comment={tr('Снимки экрана, запись видео и редактор аннотаций')}\n"
        f"Exec={command}\n"
        f"Icon={icon}\n"
        "Terminal=false\n"
        "StartupNotify=false\n"
        "Categories=Utility;Graphics;AudioVideo;\n"
        "Keywords=screenshot;screencast;снимок;запись;экран;\n"
    )
    if autostart:
        # Небольшая задержка даёт системному трею появиться до запуска.
        entry += "X-GNOME-Autostart-enabled=true\nX-GNOME-Autostart-Delay=3\n"
    return entry


def menu_entry_installed() -> bool:
    """Признак установленного ярлыка в меню приложений."""
    return menu_entry_file().is_file()


def install_menu_entry(icon: str = "camera-photo") -> Path:
    """Создание ярлыка приложения в меню рабочего стола."""
    target = menu_entry_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(desktop_entry(icon=icon, autostart=False), encoding="utf-8")
    target.chmod(0o755)
    return target


def remove_menu_entry() -> None:
    """Удаление ярлыка из меню приложений."""
    try:
        menu_entry_file().unlink(missing_ok=True)
    except OSError:
        # Отсутствие прав на удаление не должно прерывать работу настроек.
        pass


def set_menu_entry(enabled: bool, icon: str = "camera-photo") -> None:
    """Переключение наличия ярлыка в меню приложений."""
    if enabled:
        install_menu_entry(icon)
    else:
        remove_menu_entry()


def is_enabled() -> bool:
    """Признак включённого автозапуска."""
    return autostart_file().is_file()


def enable(icon: str = "camera-photo") -> Path:
    """Включение автозапуска с перезаписью прежнего файла."""
    target = autostart_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(desktop_entry(icon=icon), encoding="utf-8")
    # Файлу назначается признак исполняемости: часть окружений без него
    # отказывается запускать запись автозапуска.
    target.chmod(0o755)
    return target


def disable() -> None:
    """Отключение автозапуска."""
    target = autostart_file()
    try:
        target.unlink(missing_ok=True)
    except OSError:
        # Отсутствие прав на удаление не должно прерывать работу настроек.
        pass


def set_enabled(enabled: bool, icon: str = "camera-photo") -> None:
    """Переключение автозапуска в заданное состояние."""
    if enabled:
        enable(icon)
    else:
        disable()
