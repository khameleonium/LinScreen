"""
Определение типа графической сессии и доступных системных утилит.

Выбор бэкендов захвата, скриншотов и горячих клавиш целиком зависит от того,
работает ли пользователь в X11 или в Wayland, какое окружение рабочего стола
установлено и какие вспомогательные утилиты присутствуют в системе.
Результат определения собирается один раз при старте и далее используется
всеми модулями без повторного опроса.

Модуль не зависит от Qt: сведения требуются ещё до создания приложения.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from enum import Enum

# Утилиты, наличие которых проверяется при старте. Отсутствие любой из них
# не является ошибкой: приложение переключается на доступный способ работы.
OPTIONAL_TOOLS: tuple[str, ...] = (
    "ffmpeg",      # кодирование и захват
    "pactl",       # перечисление звуковых устройств PulseAudio и PipeWire
    "grim",        # снимок экрана в композиторах на базе wlroots
    "slurp",       # выделение области в композиторах на базе wlroots
    "scrot",       # снимок экрана в X11
    "maim",        # альтернативный снимок экрана в X11
    "xdotool",     # сведения об активном окне в X11
    "wl-copy",     # буфер обмена в Wayland
    "xclip",       # буфер обмена в X11
    "notify-send",  # уведомления рабочего стола
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
            SessionType.UNKNOWN: "Не определён",
        }[self]


@dataclass(frozen=True)
class DesktopSession:
    """Снимок сведений о текущей графической сессии."""

    session_type: SessionType
    # Название окружения рабочего стола в нижнем регистре: gnome, kde, sway.
    desktop: str
    # Значения переменных окружения, нужные для команд захвата.
    display: str
    wayland_display: str
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
    def supports_native_hotkeys(self) -> bool:
        """
        Признак возможности перехвата клавиш без участия портала.

        В X11 клавиши перехватываются напрямую через XGrabKey, в Wayland
        композитор такой возможности не предоставляет и требуется портал
        org.freedesktop.portal.GlobalShortcuts.
        """
        return self.is_x11

    @property
    def screenshot_backend(self) -> str:
        """Предпочтительный способ получения снимка экрана."""
        if self.is_x11:
            # Штатные средства Qt в X11 отдают содержимое корневого окна
            # без внешних утилит и дополнительных разрешений.
            return "qt"
        if self.has_tool("grim"):
            return "grim"
        # Универсальный, но требующий подтверждения пользователя способ.
        return "portal"

    @property
    def region_backend(self) -> str:
        """Предпочтительный способ выделения области экрана."""
        if self.is_x11:
            # Собственный оверлей приложения работает поверх всех окон.
            return "overlay"
        if self.has_tool("slurp"):
            return "slurp"
        return "overlay"

    @property
    def video_backend(self) -> str:
        """Предпочтительный способ захвата видеопотока."""
        if self.is_x11:
            # Прямой захват корневого окна средствами FFmpeg.
            return "x11grab"
        # В Wayland единственный универсальный путь - портал ScreenCast,
        # отдающий дескриптор потока PipeWire.
        return "portal"


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
    # поэтому берётся последний значимый элемент.
    raw = os.environ.get("XDG_CURRENT_DESKTOP") or os.environ.get("DESKTOP_SESSION") or ""
    parts = [part for part in raw.replace(";", ":").split(":") if part]
    return parts[-1].lower() if parts else "unknown"


def detect_session() -> DesktopSession:
    """Сбор полных сведений о текущей сессии."""
    found = frozenset(tool for tool in OPTIONAL_TOOLS if shutil.which(tool))
    return DesktopSession(
        session_type=_detect_session_type(),
        desktop=_detect_desktop(),
        display=os.environ.get("DISPLAY", ""),
        wayland_display=os.environ.get("WAYLAND_DISPLAY", ""),
        tools=found,
    )
