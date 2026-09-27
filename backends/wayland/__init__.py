"""
Набор модулей для сессий Wayland.

Приложение изолировано от остальных: снимки, видеопоток и глобальные
сочетания клавиш выдаёт рабочий стол через порталы xdg-desktop-portal.
Возможности конкретного композитора используются как необязательное
улучшение: в KDE Plasma скрипты KWin находят окно под указателем и ставят
рамку и панель записи в нужное место, в прочих окружениях рамку рисует
вспомогательный процесс через XWayland.
"""

from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import QObject, QRect, QTimer
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QWidget

from backends.base import Backend, RecordingFrame, SnapshotHandler, VideoSource
from capture.screen import list_monitors, virtual_geometry
from core.i18n import tr
from core.session import Desktop, DesktopSession
from core.workers import run_async

# Пауза между показом окна и просьбой к композитору разместить его: окно
# должно успеть появиться в списке окон композитора.
PLACE_DELAY_MS = 150


class KWinFrame(RecordingFrame):
    """Рамка записи, которую размещает KWin."""

    def __init__(self, owner: QObject) -> None:
        from backends.wayland.region_frame import RegionFrame

        self._owner = owner
        self._window = RegionFrame()

    def show_for(self, area: QRect, color: QColor) -> None:
        """Показ рамки и просьба к KWin поставить её на место."""
        from backends.wayland import kwin

        if self._window.isVisible():
            # Продолжение после паузы: рамка уже на месте.
            self._window.set_color(color)
            return
        window = self._window
        window.show_for(area, color)
        outer = window.outer_rect
        QTimer.singleShot(
            PLACE_DELAY_MS,
            lambda: run_async(
                self._owner,
                kwin.place_window,
                lambda found: window.set_placed(bool(found)),
                lambda _text: window.set_placed(False),
                window.caption,
                (outer.x(), outer.y(), outer.width(), outer.height()),
            ),
        )

    def set_color(self, color: QColor) -> None:
        """Смена цвета рамки."""
        self._window.set_color(color)

    def hide(self) -> None:
        """Скрытие рамки."""
        self._window.hide()

    def is_shown(self) -> bool:
        """Признак показанной рамки."""
        return self._window.isVisible()


class XWaylandRecordingFrame(RecordingFrame):
    """Рамка записи во вспомогательном процессе XWayland."""

    def __init__(self) -> None:
        from backends.wayland.region_frame import XWaylandFrame

        self._frame = XWaylandFrame()

    def show_for(self, area: QRect, color: QColor) -> None:
        """Запуск процесса рамки либо смена цвета уже показанной."""
        if self._frame.isVisible():
            self._frame.set_color(color)
            return
        if self._frame.is_available():
            self._frame.show_for(area, color)

    def set_color(self, color: QColor) -> None:
        """Смена цвета рамки."""
        self._frame.set_color(color)

    def hide(self) -> None:
        """Завершение процесса рамки."""
        self._frame.hide()

    def is_shown(self) -> bool:
        """Признак показанной рамки."""
        return self._frame.isVisible()


class WaylandBackend(Backend):
    """Набор модулей для Wayland."""

    name = "wayland"

    def __init__(self, session: DesktopSession, parent: QObject | None = None) -> None:
        super().__init__(session, parent)
        # Вспомогательные скрипты доступны только в KDE Plasma.
        self._kwin = session.desktop_kind is Desktop.KDE

    # ------------------------------------------------------------ снимки

    def grab_desktop(
        self,
        owner: QObject,
        on_ready: SnapshotHandler,
        on_error: Callable[[str], None],
        hide_cursor: bool,
    ) -> None:
        """
        Снимок рабочего стола через портал.

        При hide_cursor снимок дополняется кадрами видеопотока со скрытым
        указателем, а области на одном мониторе вырезаются из родного кадра.
        """
        from backends.wayland.screen import grab_virtual_desktop
        from backends.wayland.snapshot import (
            SnapshotResult,
            crop_native,
            grab_desktop_without_cursor,
        )
        from capture.screen import crop_desktop_image

        def deliver(result: object) -> None:
            """Передача снимка обработчику в потоке интерфейса."""
            if isinstance(result, SnapshotResult):
                self._report_snapshot(result)
                snapshot = result

                def crop(rect: QRect) -> QImage:
                    """Вырезание области из родного кадра монитора, если можно."""
                    native = crop_native(snapshot, rect)
                    return native if native is not None else crop_desktop_image(
                        snapshot.image, rect
                    )

                on_ready(result.image, crop)
            elif isinstance(result, QImage):
                on_ready(result, self.plain_crop(result))

        if hide_cursor:
            # Геометрия мониторов собирается здесь: обращаться к экранам Qt
            # из фонового потока нельзя.
            screens = [item.geometry for item in list_monitors()]
            run_async(
                owner, grab_desktop_without_cursor, deliver, on_error, screens, virtual_geometry()
            )
        else:
            run_async(owner, grab_virtual_desktop, deliver, on_error)

    def _report_snapshot(self, result: Any) -> None:
        """Сообщение о том, что указатель убрать не удалось."""
        if not result.problem or result.problem == "no-screencast":
            return
        if result.problem == "cancelled":
            self.notice.emit(
                tr("Снимок экрана"),
                tr(
                    "Доступ к экрану не предоставлен: снимок сделан с указателем мыши. "
                    "Снимать с указателем без вопросов можно, отключив «Снимать без "
                    "указателя мыши» на вкладке «Снимки»."
                ),
                False,
            )
        else:
            self.notice.emit(
                tr("Снимок экрана"),
                tr("Указатель мыши не убран со снимка: {0}").format(result.problem),
                True,
            )

    @property
    def detects_objects(self) -> bool:
        """Перечень окон выдаёт только KWin."""
        return self._kwin

    def list_objects(self, limit_rect: QRect | None = None) -> list[Any]:
        """Перечень видимых окон от KWin."""
        from backends.wayland.windows import list_objects

        return list_objects(limit_rect)

    def create_overlay(
        self, desktop: QImage, virtual_rect: QRect, hint: str, magnifier: Any
    ) -> Any:
        """Оверлей - по полноэкранному окну на каждый монитор."""
        from backends.wayland.overlay import RegionOverlay

        return RegionOverlay(desktop, virtual_rect, hint, magnifier=magnifier)

    # ------------------------------------------------------------ запись

    def recording_possible(self, ffmpeg_path: str, capabilities: object) -> bool:
        """
        Кадры доставляет GStreamer из потока PipeWire, кодирует FFmpeg.

        Наличие GStreamer проверяется при старте записи и окном «Компоненты
        системы»: он может быть вложен в образ приложения.
        """
        if not ffmpeg_path:
            return False
        return bool(getattr(capabilities, "can_read_screen_stream", True))

    def create_video_source(self, parent: QObject) -> VideoSource:
        """Источник кадров через портал ScreenCast."""
        from backends.wayland.screencast import ScreenCastSource

        return ScreenCastSource(parent)

    def create_frame(self, owner: QObject) -> RecordingFrame:
        """Рамка: в KDE её ставит KWin, в прочих окружениях - процесс XWayland."""
        if self._kwin:
            return KWinFrame(owner)
        return XWaylandRecordingFrame()

    def place_recorder_bar(self, owner: QObject, bar: QWidget, position: tuple[int, int]) -> None:
        """Панель в углу ставит KWin; прочие композиторы решают сами."""
        if not self._kwin:
            return
        from backends.wayland import kwin

        caption = bar.windowTitle()
        QTimer.singleShot(
            PLACE_DELAY_MS,
            lambda: run_async(
                owner, kwin.place_window, lambda _found: None, None, caption, position
            ),
        )

    # ------------------------------------------------------------ прочее

    def create_hotkey_manager(self, parent: QObject) -> Any:
        """Сочетания через портал GlobalShortcuts либо kglobalaccel."""
        from backends.wayland.hotkeys import HotkeyManager

        return HotkeyManager(self._session, parent)

    def copy_image(self, owner: QObject, image: QImage) -> None:
        """Буфер обмена через wl-copy: работает и без фокуса окна."""
        from core.clipboard import copy_image

        copy_image(owner, image)

    def is_missing_component_error(self, message: str) -> bool:
        """Сбой из-за отсутствия бэкенда порталов."""
        from backends.wayland.screen import missing_portal_message

        return message == missing_portal_message()
