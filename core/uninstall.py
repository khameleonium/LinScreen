"""
Удаление следов программы из системы.

Программа добавляет в систему то, что остаётся и после её закрытия:
    * ярлык в меню приложений и файл автозапуска;
    * значок в каталоге значков пользователя;
    * сочетания клавиш в настройках KDE (служба kglobalaccel);
    * разрешения в хранилище порталов: снимки экрана без подтверждения
      (GNOME) и сохранённые разрешения на захват экрана.

Модуль убирает всё это, в том числе следы прежних отдельных редакций, по
просьбе пользователя: командой «linscreen --uninstall» либо кнопкой в
настройках. Удаление затрагивает обе графические системы сразу: сочетания
KDE и разрешения порталов остаются от работы в Wayland, даже если удаление
запущено из сеанса X11.

Функции модуля блокирующие и выполняются в фоновом потоке либо из
командной строки.
"""

from __future__ import annotations

from core.i18n import tr

import json
import os
import shutil
from pathlib import Path

from core import portal
from core.identity import APPLICATION_ID, APPLICATION_SLUG, LEGACY_WAYLAND_SLUG

# Идентификатор прежней отдельной Wayland-редакции.
LEGACY_WAYLAND_ID = "io.github.khameleonium.LinScreenWayland"

# Хранилище разрешений порталов.
PERMISSION_STORE_SERVICE = "org.freedesktop.impl.portal.PermissionStore"
PERMISSION_STORE_PATH = "/org/freedesktop/impl/portal/PermissionStore"
PERMISSION_STORE_INTERFACE = "org.freedesktop.impl.portal.PermissionStore"


def _home(variable: str, default: str) -> Path:
    """Каталог по стандарту XDG."""
    return Path(os.environ.get(variable) or str(Path.home() / default))


def _remove_file(path: Path, removed: list[str]) -> None:
    """Удаление файла с записью в отчёт."""
    try:
        if path.is_file() or path.is_symlink():
            path.unlink()
            removed.append(str(path))
    except OSError:
        pass


def _remove_shortcuts(removed: list[str]) -> None:
    """Удаление сочетаний из настроек KDE, если служба сочетаний работает."""
    from backends.wayland.hotkeys import remove_all_shortcuts

    try:
        components = remove_all_shortcuts()
    except portal.PortalError:
        return
    for component in components:
        removed.append(tr("сочетания клавиш KDE: {0}").format(component))


def _stored_tokens() -> list[str]:
    """Метки разрешений на захват экрана, сохранённые программой."""
    tokens: list[str] = []
    config = _home("XDG_CONFIG_HOME", ".config")
    for slug in (APPLICATION_SLUG, LEGACY_WAYLAND_SLUG):
        try:
            data = json.loads((config / slug / "screencast-tokens.json").read_text("utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            tokens += [value for value in data.values() if isinstance(value, str) and value]
    return sorted(set(tokens))


def _remove_permissions(removed: list[str]) -> None:
    """Удаление разрешений из хранилища порталов."""
    for app_id in (APPLICATION_ID, LEGACY_WAYLAND_ID):
        try:
            portal.call(
                PERMISSION_STORE_PATH,
                PERMISSION_STORE_INTERFACE,
                "DeletePermission",
                "sss",
                ("screenshot", "screenshot", app_id),
                service=PERMISSION_STORE_SERVICE,
            )
            removed.append(tr("разрешение на снимки экрана: {0}").format(app_id))
        except portal.PortalError:
            continue
    for token in _stored_tokens():
        try:
            portal.call(
                PERMISSION_STORE_PATH,
                PERMISSION_STORE_INTERFACE,
                "Delete",
                "ss",
                ("screencast", token),
                service=PERMISSION_STORE_SERVICE,
            )
            removed.append(tr("разрешение на захват экрана: {0}").format(token))
        except portal.PortalError:
            continue


def uninstall(purge: bool = False) -> list[str]:
    """
    Удаление следов программы; возвращается перечень удалённого.

    Признак purge удаляет также настройки и журналы. Сам файл программы
    (образ AppImage или каталог исходных текстов) не трогается.
    """
    removed: list[str] = []
    _remove_shortcuts(removed)
    _remove_permissions(removed)

    applications = _home("XDG_DATA_HOME", ".local/share") / "applications"
    autostart = _home("XDG_CONFIG_HOME", ".config") / "autostart"
    icons = _home("XDG_DATA_HOME", ".local/share") / "icons" / "hicolor" / "256x256" / "apps"
    for name in (APPLICATION_ID, LEGACY_WAYLAND_ID, "linscreen"):
        _remove_file(applications / f"{name}.desktop", removed)
        _remove_file(autostart / f"{name}.desktop", removed)
        _remove_file(icons / f"{name}.png", removed)

    if purge:
        for base, default in (("XDG_CONFIG_HOME", ".config"), ("XDG_CACHE_HOME", ".cache")):
            for slug in (APPLICATION_SLUG, LEGACY_WAYLAND_SLUG):
                directory = _home(base, default) / slug
                if directory.is_dir():
                    shutil.rmtree(directory, ignore_errors=True)
                    removed.append(str(directory))
    return removed
