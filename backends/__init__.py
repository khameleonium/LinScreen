"""
Выбор набора модулей графической системы.

Набор выбирается один раз при запуске по типу сессии. Для отладки его
можно задать явно ключом --platform x11|wayland или переменной окружения
LINSCREEN_PLATFORM. Модули невыбранного набора не импортируются.
"""

from __future__ import annotations

import os

from PySide6.QtCore import QObject

from backends.base import Backend
from core.session import DesktopSession

# Переменная окружения, задающая набор модулей явно.
PLATFORM_VARIABLE = "LINSCREEN_PLATFORM"


def platform_name(session: DesktopSession) -> str:
    """Обозначение набора модулей: x11 или wayland."""
    forced = os.environ.get(PLATFORM_VARIABLE, "").strip().lower()
    if forced in ("x11", "wayland"):
        return forced
    return "wayland" if session.is_wayland else "x11"


def create_backend(session: DesktopSession, parent: QObject | None = None) -> Backend:
    """Набор модулей для текущей сессии."""
    if platform_name(session) == "wayland":
        from backends.wayland import WaylandBackend

        return WaylandBackend(session, parent)
    from backends.x11 import X11Backend

    return X11Backend(session, parent)
