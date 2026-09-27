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
from core.identity import APPLICATION_ID
from core.runtime import (
    appimage_path,
    executable_path,
    is_appimage,
    is_frozen,
    project_root,
)

# Имя файла ярлыка совпадает с идентификатором приложения: по нему
# xdg-desktop-portal находит сведения о приложении при регистрации, а
# композитор - значок и название окон.
DESKTOP_FILE_NAME = f"{APPLICATION_ID}.desktop"

# Признак скрытого ярлыка. Ярлык в каталоге приложений нужен всегда, иначе
# портал не зарегистрирует приложение, но в меню он показывается только по
# выбору пользователя.
_HIDDEN_MARK = "NoDisplay=true"


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


def desktop_entry(
    icon: str = "camera-photo", autostart: bool = True, hidden: bool = False
) -> str:
    """
    Содержимое файла .desktop.

    Один и тот же вид записи используется и для автозапуска, и для ярлыка
    в меню приложений; различаются только поля автозапуска и видимость.
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
        # Класс окна для композитора совпадает с идентификатором приложения.
        f"StartupWMClass={APPLICATION_ID}\n"
        "Categories=Utility;Graphics;AudioVideo;\n"
        "Keywords=screenshot;screencast;снимок;запись;экран;\n"
    )
    # Действия ярлыка: в GNOME они доступны правым щелчком по значку в
    # сетке приложений и в панели задач. Без системного трея это основной
    # способ управления приложением.
    names = ";".join(name for name, _key, _title in desktop_actions())
    entry += f"Actions={names};\n"
    if hidden:
        entry += _HIDDEN_MARK + "\n"
    if autostart:
        # Небольшая задержка даёт системному трею появиться до запуска.
        entry += "X-GNOME-Autostart-enabled=true\nX-GNOME-Autostart-Delay=3\n"
    for name, key, title in desktop_actions():
        entry += (
            f"\n[Desktop Action {name}]\n"
            f"Name={title}\n"
            f"Exec={shlex.join([*launch_command(), key])}\n"
        )
    return entry


def desktop_actions() -> list[tuple[str, str, str]]:
    """Действия ярлыка: имя, ключ командной строки и подпись."""
    return [
        ("screenshot", "--screenshot", tr("Снимок области")),
        ("screenshot-full", "--screenshot-full", tr("Снимок всего экрана")),
        ("screenshot-window", "--screenshot-window", tr("Снимок окна под курсором")),
        ("record", "--record", tr("Запись области: старт и стоп")),
        ("pause", "--pause", tr("Пауза записи")),
        ("settings", "--settings", tr("Настройки…")),
        ("components", "--check-system", tr("Компоненты системы…")),
        ("quit", "--quit", tr("Выход")),
    ]


def menu_entry_installed() -> bool:
    """Признак видимого ярлыка в меню приложений."""
    path = menu_entry_file()
    try:
        return path.is_file() and _HIDDEN_MARK not in path.read_text(encoding="utf-8")
    except OSError:
        return False


def install_menu_entry(icon: str = "camera-photo", hidden: bool = False) -> Path:
    """Создание ярлыка приложения в каталоге приложений рабочего стола."""
    target = menu_entry_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        desktop_entry(icon=icon, autostart=False, hidden=hidden), encoding="utf-8"
    )
    target.chmod(0o755)
    return target


def remove_menu_entry(icon: str = "camera-photo") -> None:
    """
    Скрытие ярлыка из меню приложений.

    Файл не удаляется, а помечается скрытым: без него портал не
    зарегистрирует приложение, и перестанут работать горячие клавиши и
    сохранённые разрешения на захват экрана.
    """
    try:
        install_menu_entry(icon, hidden=True)
    except OSError:
        # Отсутствие прав на запись не должно прерывать работу настроек.
        pass


def ensure_application_entry(icon: str = "camera-photo") -> None:
    """
    Обеспечение наличия ярлыка, обязательного для работы с порталами.

    Существующий ярлык перезаписывается с сохранением видимости: путь к
    приложению мог измениться после переноса образа AppImage.
    """
    try:
        install_menu_entry(icon, hidden=not menu_entry_installed())
    except OSError:
        # Без ярлыка приложение продолжает работать, но порталы
        # воспринимают его безымянным.
        pass


def set_menu_entry(enabled: bool, icon: str = "camera-photo") -> None:
    """Переключение наличия ярлыка в меню приложений."""
    if enabled:
        install_menu_entry(icon)
    else:
        remove_menu_entry(icon)


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
