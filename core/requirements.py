"""
Проверка системных компонентов, которые нельзя вложить в приложение.

Всё, что возможно, вложено в образ AppImage: FFmpeg, GStreamer с
клиентом PipeWire, утилита буфера обмена. Остаются части самого рабочего
стола - их может предоставить только система:
    * бэкенд порталов (xdg-desktop-portal-gnome, -kde) - без него нет
      снимков экрана и записи;
    * расширение системного трея GNOME - без него нет значка в панели;
    * gjs в GNOME - без него не работает служба уведомлений оболочки.

Модуль определяет, чего не хватает, и готовит для каждого недостатка
способ исправления: команду установки пакетов под пакетный менеджер
дистрибутива (выполняется с правами администратора через pkexec) либо
команду без повышения прав (включение уже установленного расширения).

Проверка обращается к шине D-Bus и запускает внешние утилиты, поэтому
вызывается только из фонового потока.
"""

from __future__ import annotations

from core.i18n import tr

import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from core import portal
from core.identity import APPLICATION_ID
from core.session import Desktop, DesktopSession

# Хранилище разрешений порталов и запись о снимках экрана без вопросов.
PERMISSION_STORE_SERVICE = "org.freedesktop.impl.portal.PermissionStore"
PERMISSION_STORE_PATH = "/org/freedesktop/impl/portal/PermissionStore"
PERMISSION_STORE_INTERFACE = "org.freedesktop.impl.portal.PermissionStore"

# Идентификаторы расширения трея: исходное и вариант из Ubuntu.
APPINDICATOR_UUIDS = ("appindicatorsupport@rgcjonas.gmail.com", "ubuntu-appindicators@ubuntu.com")

# Имена пакетов по менеджерам. Ключ - обозначение компонента.
PACKAGES: dict[str, dict[str, list[str]]] = {
    "portal-gnome": {
        "apt": ["xdg-desktop-portal", "xdg-desktop-portal-gnome"],
        "dnf": ["xdg-desktop-portal", "xdg-desktop-portal-gnome"],
        "pacman": ["xdg-desktop-portal", "xdg-desktop-portal-gnome"],
        "zypper": ["xdg-desktop-portal", "xdg-desktop-portal-gnome"],
    },
    "portal-kde": {
        "apt": ["xdg-desktop-portal", "xdg-desktop-portal-kde"],
        "dnf": ["xdg-desktop-portal", "xdg-desktop-portal-kde"],
        "pacman": ["xdg-desktop-portal", "xdg-desktop-portal-kde"],
        "zypper": ["xdg-desktop-portal", "xdg-desktop-portal-kde"],
    },
    "appindicator": {
        "apt": ["gnome-shell-extension-appindicator"],
        "dnf": ["gnome-shell-extension-appindicator"],
        "pacman": ["gnome-shell-extension-appindicator"],
        "zypper": ["gnome-shell-extension-appindicator"],
    },
    "gjs": {
        "apt": ["gjs"],
        "dnf": ["gjs"],
        "pacman": ["gjs"],
        "zypper": ["gjs"],
    },
    "gstreamer": {
        "apt": [
            "gstreamer1.0-tools",
            "gstreamer1.0-plugins-base",
            "gstreamer1.0-plugins-good",
            "gstreamer1.0-pipewire",
        ],
        "dnf": ["gstreamer1-plugins-base", "gstreamer1-plugins-good", "pipewire-gstreamer"],
        "pacman": ["gstreamer", "gst-plugins-base", "gst-plugins-good", "gst-plugin-pipewire"],
        "zypper": ["gstreamer-utils", "gstreamer-plugins-good", "pipewire-gstreamer"],
    },
    "ffmpeg": {
        "apt": ["ffmpeg"],
        "dnf": ["ffmpeg-free"],
        "pacman": ["ffmpeg"],
        "zypper": ["ffmpeg"],
    },
}

# Команды установки по менеджерам; имена пакетов добавляются в конец.
INSTALL_COMMANDS: dict[str, list[str]] = {
    "apt": ["apt-get", "install", "-y"],
    "dnf": ["dnf", "install", "-y"],
    "pacman": ["pacman", "-S", "--needed", "--noconfirm"],
    "zypper": ["zypper", "--non-interactive", "install"],
}


@dataclass(frozen=True)
class Problem:
    """Недостающий компонент и способ его получить."""

    # Обозначение компонента (ключ таблицы PACKAGES либо особое имя).
    key: str
    # Что не работает без компонента - для показа пользователю.
    title: str
    # Пакеты для установки; пустой перечень - установка не требуется.
    packages: tuple[str, ...] = ()
    # Команды, выполняемые без прав администратора после установки.
    user_commands: tuple[tuple[str, ...], ...] = ()
    # Требуется ли после исправления перезапуск приложения или сеанса.
    note: str = ""


@dataclass
class Report:
    """Итог проверки системы."""

    package_manager: str = ""
    problems: list[Problem] = field(default_factory=list)

    @property
    def packages(self) -> list[str]:
        """Все пакеты для установки без повторов, в порядке появления."""
        seen: list[str] = []
        for problem in self.problems:
            for package in problem.packages:
                if package not in seen:
                    seen.append(package)
        return seen

    @property
    def install_command(self) -> list[str]:
        """Команда установки пакетов (без pkexec и sudo) либо пустой перечень."""
        base = INSTALL_COMMANDS.get(self.package_manager)
        if not base or not self.packages:
            return []
        return [*base, *self.packages]

    @property
    def user_commands(self) -> list[list[str]]:
        """Команды без прав администратора, выполняемые после установки."""
        commands: list[list[str]] = []
        for problem in self.problems:
            for command in problem.user_commands:
                if list(command) not in commands:
                    commands.append(list(command))
        return commands

    def script(self) -> str:
        """Команды исправления в виде текста для копирования в терминал."""
        lines: list[str] = []
        if self.install_command:
            lines.append("sudo " + shell_join(self.install_command))
        for command in self.user_commands:
            lines.append(shell_join(command))
        return "\n".join(lines)


def shell_join(command: list[str]) -> str:
    """
    Команда одной строкой для терминала.

    Аргумент с одинарными кавычками заключается в двойные, если в нём нет
    символов, особых для оболочки внутри двойных кавычек: запись "['yes']"
    читается проще, чем то, что даёт shlex.
    """
    parts: list[str] = []
    for argument in command:
        if "'" in argument and not any(char in argument for char in '"$`\\!'):
            parts.append(f'"{argument}"')
        else:
            parts.append(shlex.quote(argument))
    return " ".join(parts)


def detect_package_manager() -> str:
    """Пакетный менеджер дистрибутива: apt, dnf, pacman, zypper либо пустая строка."""
    for name in ("apt-get", "dnf", "pacman", "zypper"):
        if shutil.which(name):
            return "apt" if name == "apt-get" else name
    return ""


def _packages(key: str, manager: str) -> tuple[str, ...]:
    """Пакеты компонента для менеджера."""
    return tuple(PACKAGES.get(key, {}).get(manager, []))


# ===========================================================================
# Отдельные проверки
# ===========================================================================


def _check_portal(session: DesktopSession, manager: str) -> Problem | None:
    """Бэкенд порталов, дающий снимки и видеопоток."""
    screenshot = portal.interface_version(portal.SCREENSHOT_INTERFACE)
    screencast = portal.interface_version(portal.SCREENCAST_INTERFACE)
    if screenshot is not None and screencast is not None:
        return None
    key = "portal-kde" if session.desktop_kind is Desktop.KDE else "portal-gnome"
    return Problem(
        key,
        tr("Снимки экрана и запись: нет бэкенда порталов рабочего стола"),
        _packages(key, manager),
        # Служба порталов уже запущена и не знает о новом бэкенде до
        # перезапуска.
        ((("systemctl", "--user", "restart", "xdg-desktop-portal")),),
    )


def has_screenshot_permission() -> bool:
    """
    Разрешение приложению делать снимки экрана без подтверждения.

    GNOME показывает окно подтверждения снимка только приложению, окно
    которого в фокусе. Приложение из трея фокуса не имеет, и без
    сохранённого разрешения каждый снимок отклоняется.
    """
    try:
        permissions, _data = portal.call(
            PERMISSION_STORE_PATH,
            PERMISSION_STORE_INTERFACE,
            "Lookup",
            "ss",
            ("screenshot", "screenshot"),
            service=PERMISSION_STORE_SERVICE,
        )
    except portal.PortalError:
        return False
    values = dict(permissions).get(APPLICATION_ID, [])
    return "yes" in list(values)


def grant_screenshot_command() -> tuple[str, ...]:
    """Команда записи разрешения на снимки экрана в хранилище порталов."""
    return (
        "gdbus",
        "call",
        "--session",
        "--dest",
        PERMISSION_STORE_SERVICE,
        "--object-path",
        PERMISSION_STORE_PATH,
        "--method",
        f"{PERMISSION_STORE_INTERFACE}.SetPermission",
        "screenshot",
        "true",
        "screenshot",
        APPLICATION_ID,
        "['yes']",
    )


def _check_screenshot_permission(session: DesktopSession) -> Problem | None:
    """Разрешение на снимки экрана без подтверждения (GNOME)."""
    if session.desktop_kind is not Desktop.GNOME:
        return None
    if portal.interface_version(portal.SCREENSHOT_INTERFACE) is None:
        # Сначала нужен сам портал; разрешение проверяется после установки.
        return None
    if has_screenshot_permission():
        return None
    return Problem(
        "screenshot-permission",
        tr(
            "Снимки экрана: GNOME отклоняет снимки приложений без разрешения. "
            "Исправление выдаёт LinScreen разрешение делать снимки экрана без "
            "подтверждения (его можно отозвать в «Параметрах» → «Приложения»)"
        ),
        (),
        (grant_screenshot_command(),),
    )


def _extensions_state() -> dict[str, bool]:
    """Установленные расширения трея GNOME и признак их включения."""
    tool = shutil.which("gnome-extensions")
    if tool is None:
        return {}
    try:
        installed = subprocess.run(
            [tool, "list"], capture_output=True, text=True, timeout=10, check=False
        ).stdout.split()
        enabled = subprocess.run(
            [tool, "list", "--enabled"], capture_output=True, text=True, timeout=10, check=False
        ).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return {}
    return {uuid: uuid in enabled for uuid in APPINDICATOR_UUIDS if uuid in installed}


def _check_tray(session: DesktopSession, manager: str, tray_available: bool) -> Problem | None:
    """Системный трей; в GNOME его даёт расширение AppIndicator."""
    if tray_available or session.desktop_kind is not Desktop.GNOME:
        return None
    states = _extensions_state()
    title = tr("Значок в верхней панели: не включено расширение системного трея")
    if states:
        # Расширение установлено, но выключено: достаточно включить.
        uuid = next(iter(states))
        return Problem("appindicator", title, (), (("gnome-extensions", "enable", uuid),))
    uuid = APPINDICATOR_UUIDS[1] if manager == "apt" else APPINDICATOR_UUIDS[0]
    return Problem(
        "appindicator",
        title,
        _packages("appindicator", manager),
        (("gnome-extensions", "enable", uuid),),
        # Новое расширение оболочка видит только после повторного входа.
        tr("После установки расширения потребуется выйти из сеанса и войти снова."),
    )


def _check_gnome_notifications(session: DesktopSession, manager: str) -> Problem | None:
    """Служба уведомлений GNOME, работающая на gjs."""
    if session.desktop_kind is not Desktop.GNOME:
        return None
    if Path("/usr/bin/gjs").exists() or shutil.which("gjs"):
        return None
    return Problem(
        "gjs",
        tr("Уведомления рабочего стола: в GNOME не установлен gjs"),
        _packages("gjs", manager),
        (),
        tr("Уведомления заработают после повторного входа в сеанс."),
    )


def _check_gstreamer(manager: str) -> Problem | None:
    """GStreamer с модулем PipeWire, если не вложен в образ."""
    from backends.wayland import gstreamer

    if gstreamer.select().is_ready:
        return None
    return Problem(
        "gstreamer",
        tr("Запись экрана: нет GStreamer с модулем PipeWire"),
        _packages("gstreamer", manager),
    )


def _check_ffmpeg(manager: str) -> Problem | None:
    """FFmpeg, если не вложен в образ."""
    from encoder.ffmpeg import FFmpegNotFoundError, find_ffmpeg

    try:
        find_ffmpeg()
    except FFmpegNotFoundError:
        return Problem(
            "ffmpeg", tr("Запись экрана: не найден FFmpeg"), _packages("ffmpeg", manager)
        )
    return None


def check(session: DesktopSession, tray_available: bool) -> Report:
    """
    Полная проверка системы.

    Признак tray_available передаётся из потока интерфейса: наличие трея
    определяет Qt, обращаться к которому из фонового потока нельзя.
    """
    from backends import platform_name

    manager = detect_package_manager()
    report = Report(manager)
    checks = [_check_tray(session, manager, tray_available)]
    if platform_name(session) == "wayland":
        # Порталы, разрешение GNOME на снимки и GStreamer нужны только в
        # Wayland: в X11 экран захватывается напрямую.
        checks = [
            _check_portal(session, manager),
            _check_screenshot_permission(session),
            *checks,
            _check_gstreamer(manager),
        ]
    checks += [_check_gnome_notifications(session, manager), _check_ffmpeg(manager)]
    for problem in checks:
        if problem is not None:
            report.problems.append(problem)
    return report


def graphical_sudo() -> str | None:
    """Утилита выполнения команды с правами администратора из графики."""
    return shutil.which("pkexec")


def child_environment_for_user_commands() -> dict[str, str]:
    """Окружение команд без прав администратора."""
    from core.runtime import child_environment

    environment = child_environment()
    # Сообщения утилит выводятся в журнал: язык приводится к исходному.
    environment.setdefault("LANG", os.environ.get("LANG", "C.UTF-8"))
    return environment
