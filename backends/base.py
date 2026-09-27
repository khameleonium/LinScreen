"""
Общий интерфейс набора модулей графической системы.

Приложение работает с экраном только через этот интерфейс. Реализации:
    * backends/x11 - прямой доступ к X-серверу: снимок средствами Qt,
      запись демультиплексором x11grab, дерево окон python-xlib, перехват
      клавиш pynput;
    * backends/wayland - порталы рабочего стола: снимок и видеопоток через
      xdg-desktop-portal и PipeWire, клавиши через GlobalShortcuts,
      вспомогательные скрипты KWin в KDE Plasma.

Набор выбирается один раз при запуске по типу сессии (см. backends/__init__.py)
и дальше не меняется. Модули другого набора не импортируются вовсе.
"""

from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import QObject, QRect, Signal
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QWidget

from capture.screen import crop_desktop_image
from core.session import DesktopSession

# Обработчик готового снимка: изображение рабочего стола и функция
# вырезания области из него. Функция вырезания позволяет набору модулей
# брать область из более чёткого источника, чем общий снимок.
SnapshotHandler = Callable[[QImage, Callable[[QRect], QImage]], None]


class VideoSource(QObject):
    """
    Источник видео для одной записи.

    После open() испускается ready с описанием видеовхода FFmpeg либо
    cancelled/failed. Свойство feeder - подготовка поставщика кадров для
    каждого фрагмента записи или пустое значение, если FFmpeg захватывает
    экран сам.
    """

    # Источник готов: передаётся описание видеовхода FFmpeg.
    ready = Signal(object)
    # Пользователь отказался в диалоге выбора экрана.
    cancelled = Signal()
    # Источник открыть не удалось: передаётся причина.
    failed = Signal(str)

    @property
    def recorded_rect(self) -> QRect:
        """Фактически записываемая область в логических координатах."""
        raise NotImplementedError

    @property
    def feeder(self) -> Any:
        """Подготовка поставщика кадров либо пустое значение."""
        return None

    def open(self, region: QRect, fps: int, show_cursor: bool) -> None:
        """Открытие источника для области."""
        raise NotImplementedError

    def close(self) -> None:
        """Освобождение источника по окончании записи."""


class RecordingFrame:
    """Рамка вокруг записываемой области."""

    def show_for(self, area: QRect, color: QColor) -> None:
        """Показ рамки вокруг области."""

    def set_color(self, color: QColor) -> None:
        """Смена цвета показанной рамки."""

    def hide(self) -> None:
        """Скрытие рамки."""

    def is_shown(self) -> bool:
        """Признак показанной рамки."""
        return False


class Backend(QObject):
    """Набор модулей графической системы."""

    # Сообщение для пользователя: заголовок, текст, признак записи только в журнал.
    notice = Signal(str, str, bool)

    # Обозначение набора: x11 или wayland.
    name = ""

    def __init__(self, session: DesktopSession, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._session = session

    # ------------------------------------------------------------ снимки

    def grab_desktop(
        self,
        owner: QObject,
        on_ready: SnapshotHandler,
        on_error: Callable[[str], None],
        hide_cursor: bool,
    ) -> None:
        """Снимок всего рабочего стола; результат передаётся обработчикам."""
        raise NotImplementedError

    @staticmethod
    def plain_crop(desktop: QImage) -> Callable[[QRect], QImage]:
        """Вырезание области из общего снимка рабочего стола."""
        return lambda rect: crop_desktop_image(desktop, rect)

    @property
    def detects_objects(self) -> bool:
        """Признак возможности найти окно под указателем."""
        return False

    def list_objects(self, limit_rect: QRect | None = None) -> list[Any]:
        """Перечень видимых окон (блокирующий вызов, выполняется в фоне)."""
        return []

    def create_overlay(
        self, desktop: QImage, virtual_rect: QRect, hint: str, magnifier: Any
    ) -> Any:
        """Оверлей выделения области."""
        raise NotImplementedError

    # ------------------------------------------------------------ запись

    def recording_possible(self, ffmpeg_path: str, capabilities: object) -> bool:
        """Признак принципиальной возможности записи."""
        raise NotImplementedError

    def create_video_source(self, parent: QObject) -> VideoSource:
        """Источник видео для новой записи."""
        raise NotImplementedError

    def create_frame(self, owner: QObject) -> RecordingFrame:
        """Рамка вокруг записываемой области."""
        return RecordingFrame()

    def place_recorder_bar(self, owner: QObject, bar: QWidget, position: tuple[int, int]) -> None:
        """Размещение панели записи в углу экрана."""
        bar.move(*position)

    # ------------------------------------------------------------ прочее

    def create_hotkey_manager(self, parent: QObject) -> Any:
        """Менеджер глобальных сочетаний клавиш."""
        raise NotImplementedError

    def copy_image(self, owner: QObject, image: QImage) -> None:
        """Помещение изображения в буфер обмена."""
        from encoder.images import copy_image_to_clipboard

        copy_image_to_clipboard(image)

    def is_missing_component_error(self, message: str) -> bool:
        """Признак сбоя из-за недостающего компонента системы."""
        return False
