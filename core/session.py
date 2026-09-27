"""
Определение типа графической сессии и доступных системных утилит.

Модуль определяет тип сессии (X11 или Wayland), по которому выбирается
набор модулей графической системы, окружение рабочего стола и наличие
внешних утилит: GStreamer доставляет кадры экрана из PipeWire в Wayland,
pactl перечисляет звуковые устройства.

Результат собирается один раз при старте и далее используется всеми
модулями без повторного опроса. Модуль не зависит от Qt: сведения
требуются ещё до создания приложения.
"""

from __future__ import annotations

from core.i18n import tr

import os
import shutil
from dataclasses import dataclass, field
from enum import Enum

# Утилиты, наличие которых проверяется при старте. Отсутствие любой из них
# сообщается пользователю, но не прерывает работу: снимки экрана возможны
# и без средств записи.
OPTIONAL_TOOLS: tuple[str, ...] = (
    "ffmpeg",  # кодирование видео
    "gst-launch-1.0",  # доставка кадров экрана из PipeWire
    "pactl",  # перечисление звуковых устройств PulseAudio и PipeWire
    "wl-copy",  # запасной путь копирования в буфер обмена Wayland
)


class SessionType(str, Enum):
    """Тип графического сервера текущей сессии."""

    X11 = "x11"
    WAYLAND = "wayland"
    UNKNOWN = "unknown"

    @property
    def label(self) -> str:
        """Название для показа в окне настроек."""
        return {
            SessionType.X11: "X11",
            SessionType.WAYLAND: "Wayland",
            SessionType.UNKNOWN: tr("Не определён"),
        }[self]


class Desktop(str, Enum):
    """
    Окружение рабочего стола.

    Различие учитывается только для необязательных улучшений, которые
    предоставляет конкретный композитор. Основные возможности работают
    через порталы одинаково во всех окружениях.
    """

    KDE = "kde"
    GNOME = "gnome"
    OTHER = "other"


@dataclass(frozen=True)
class DesktopSession:
    """Снимок сведений о текущей графической сессии."""

    session_type: SessionType
    # Название окружения рабочего стола в нижнем регистре: kde, gnome, sway.
    desktop: str
    # Имя сокета композитора Wayland.
    wayland_display: str
    # Дисплей X11 (в сессии Wayland - дисплей XWayland).
    display: str = ""
    # Множество найденных вспомогательных утилит.
    tools: frozenset[str] = field(default_factory=frozenset)

    def has_tool(self, name: str) -> bool:
        """Проверка наличия вспомогательной утилиты."""
        return name in self.tools

    @property
    def is_wayland(self) -> bool:
        """Признак сессии Wayland."""
        return self.session_type is SessionType.WAYLAND

    @property
    def is_x11(self) -> bool:
        """Признак сессии X11."""
        return self.session_type is SessionType.X11

    @property
    def desktop_kind(self) -> Desktop:
        """Семейство окружения рабочего стола."""
        if "kde" in self.desktop or "plasma" in self.desktop:
            return Desktop.KDE
        if "gnome" in self.desktop or "unity" in self.desktop:
            return Desktop.GNOME
        return Desktop.OTHER


def _detect_session_type() -> SessionType:
    """Определение типа графического сервера по переменным окружения."""
    # Переменная XDG_SESSION_TYPE выставляется менеджером входа и является
    # наиболее достоверным источником.
    declared = os.environ.get("XDG_SESSION_TYPE", "").strip().lower()
    if declared in ("wayland", "x11"):
        return SessionType(declared)

    # Запасной путь: наличие сокета композитора важнее наличия DISPLAY,
    # поскольку в Wayland обычно работает и прослойка XWayland.
    if os.environ.get("WAYLAND_DISPLAY"):
        return SessionType.WAYLAND
    if os.environ.get("DISPLAY"):
        return SessionType.X11
    return SessionType.UNKNOWN


def _detect_desktop() -> str:
    """Определение окружения рабочего стола."""
    # Переменная может содержать перечисление вида "ubuntu:GNOME",
    # поэтому значения объединяются: признак окружения может стоять
    # в любой позиции.
    raw = os.environ.get("XDG_CURRENT_DESKTOP") or os.environ.get("DESKTOP_SESSION") or ""
    parts = [part.lower() for part in raw.replace(";", ":").split(":") if part]
    return ":".join(parts) if parts else "unknown"


def detect_session() -> DesktopSession:
    """Сбор полных сведений о текущей сессии."""
    found = frozenset(tool for tool in OPTIONAL_TOOLS if shutil.which(tool))
    return DesktopSession(
        session_type=_detect_session_type(),
        desktop=_detect_desktop(),
        wayland_display=os.environ.get("WAYLAND_DISPLAY", ""),
        display=os.environ.get("DISPLAY", ""),
        tools=found,
    )


def shortcut_settings_command(kind: Desktop) -> list[str] | None:
    """
    Команда открытия настроек сочетаний клавиш рабочего стола.

    Возвращается первая найденная в системе команда либо пустое значение.
    """
    candidates: dict[Desktop, list[list[str]]] = {
        Desktop.KDE: [["systemsettings", "kcm_keys"], ["kcmshell5", "kcm_keys"]],
        Desktop.GNOME: [["gnome-control-center", "keyboard"]],
        Desktop.OTHER: [],
    }
    for command in candidates[kind]:
        if shutil.which(command[0]):
            return command
    return None
