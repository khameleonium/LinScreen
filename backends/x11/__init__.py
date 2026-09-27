"""
Набор модулей для сессий X11.

Приложение имеет прямой доступ к X-серверу: снимок экрана делает Qt,
запись ведёт FFmpeg демультиплексором x11grab, окна под указателем
находятся обходом дерева окон (python-xlib), сочетания клавиш
перехватываются библиотекой pynput, а окна приложения ставятся в нужное
место без участия композитора.
"""

from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import QObject, QRect, QTimer
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QWidget

from backends.base import Backend, RecordingFrame, SnapshotHandler, VideoSource
from capture.screen import CaptureBackendError


class X11VideoSource(VideoSource):
    """Видеовход x11grab: FFmpeg захватывает экран сам."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._rect = QRect()

    @property
    def recorded_rect(self) -> QRect:
        """Записываемая область в логических координатах."""
        return QRect(self._rect)

    def open(self, region: QRect, fps: int, show_cursor: bool) -> None:
        """Сборка аргументов входа; результат передаётся после возврата."""
        from backends.x11.screen import build_video_input

        self._rect = QRect(region)
        try:
            video_input = build_video_input(region, fps=fps, show_cursor=show_cursor)
        except CaptureBackendError as error:
            message = str(error)
            QTimer.singleShot(0, lambda: self.failed.emit(message))
            return
        # Сигнал испускается после возврата из open(): получатель успевает
        # подключиться и сохранить ссылку на источник.
        QTimer.singleShot(0, lambda: self.ready.emit(video_input))


class X11Frame(RecordingFrame):
    """Рамка записи: окно X11 вне управления менеджера окон."""

    def __init__(self) -> None:
        from backends.x11.region_frame import RegionFrame

        self._window = RegionFrame()

    def show_for(self, area: QRect, color: QColor) -> None:
        """Показ рамки вокруг области."""
        self._window.show_for(area, color)

    def set_color(self, color: QColor) -> None:
        """Смена цвета рамки."""
        self._window.set_color(color)

    def hide(self) -> None:
        """Скрытие рамки."""
        self._window.hide()

    def is_shown(self) -> bool:
        """Признак показанной рамки."""
        return self._window.isVisible()


class X11Backend(Backend):
    """Набор модулей для X11."""

    name = "x11"

    def grab_desktop(
        self,
        owner: QObject,
        on_ready: SnapshotHandler,
        on_error: Callable[[str], None],
        hide_cursor: bool,
    ) -> None:
        """
        Снимок рабочего стола средствами Qt.

        Снимок X11 не содержит указателя мыши, поэтому признак hide_cursor
        не требуется. Экраны Qt доступны только из потока интерфейса, а
        снимок занимает доли секунды, поэтому он делается здесь же.
        """
        from backends.x11.screen import grab_virtual_desktop

        try:
            desktop = grab_virtual_desktop()
        except CaptureBackendError as error:
            on_error(str(error))
            return
        on_ready(desktop, self.plain_crop(desktop))

    @property
    def detects_objects(self) -> bool:
        """Дерево окон X11 доступно всегда."""
        return True

    def list_objects(self, limit_rect: QRect | None = None) -> list[Any]:
        """Перечень видимых окон и их элементов."""
        from backends.x11.windows import list_objects

        return list_objects(limit_rect)

    def create_overlay(
        self, desktop: QImage, virtual_rect: QRect, hint: str, magnifier: Any
    ) -> Any:
        """Оверлей на весь рабочий стол одним окном."""
        from backends.x11.overlay import RegionOverlay

        return RegionOverlay(desktop, virtual_rect, hint, magnifier=magnifier)

    def recording_possible(self, ffmpeg_path: str, capabilities: object) -> bool:
        """Нужен FFmpeg с демультиплексором x11grab."""
        if not ffmpeg_path:
            return False
        return bool(getattr(capabilities, "can_capture_x11", True))

    def create_video_source(self, parent: QObject) -> VideoSource:
        """Видеовход x11grab."""
        return X11VideoSource(parent)

    def create_frame(self, owner: QObject) -> RecordingFrame:
        """Рамка записи окном X11."""
        return X11Frame()

    def place_recorder_bar(self, owner: QObject, bar: QWidget, position: tuple[int, int]) -> None:
        """В X11 окно ставится в нужное место напрямую."""
        bar.move(*position)

    def create_hotkey_manager(self, parent: QObject) -> Any:
        """Перехват клавиш библиотекой pynput."""
        from backends.x11.hotkeys import HotkeyManager

        return HotkeyManager(parent)
