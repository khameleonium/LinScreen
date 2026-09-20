"""
Плавающая панель управления активной записью.

Панель показывает длительность записи и предоставляет кнопки паузы и
остановки. Она не перехватывает фокус и располагается в углу основного
монитора, чтобы не мешать снимаемому материалу.
"""

from __future__ import annotations

from core.i18n import tr

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPaintEvent
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from encoder.ffmpeg import format_timecode
from encoder.process import RecorderState

# Отступ панели от края экрана.
SCREEN_MARGIN = 24

_STYLE = """
QWidget#recorderBar { background: transparent; }
QLabel { color: #f0f0f0; font-size: 13px; }
QLabel#timer { font-size: 15px; font-weight: bold; }
QPushButton {
    color: #f0f0f0; background: #3a3f46; border: none;
    border-radius: 4px; padding: 5px 12px;
}
QPushButton:hover { background: #4a505a; }
QPushButton#stop { background: #b33a3a; }
QPushButton#stop:hover { background: #cc4444; }
"""


class RecorderBar(QWidget):
    """Компактная панель управления записью."""

    # Запрос паузы или продолжения записи.
    pauseRequested = Signal()
    # Запрос остановки записи.
    stopRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("recorderBar")
        # Панель не забирает фокус у снимаемого приложения и держится
        # поверх остальных окон.
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setStyleSheet(_STYLE)

        self._state_label = QLabel(tr("Идёт запись"))
        self._timer_label = QLabel("00:00:00")
        self._timer_label.setObjectName("timer")
        self._pause_button = QPushButton(tr("Пауза"))
        self._stop_button = QPushButton(tr("Стоп"))
        self._stop_button.setObjectName("stop")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 8, 14, 8)
        layout.setSpacing(12)
        layout.addWidget(self._state_label)
        layout.addWidget(self._timer_label)
        layout.addWidget(self._pause_button)
        layout.addWidget(self._stop_button)

        self._pause_button.clicked.connect(self.pauseRequested)
        self._stop_button.clicked.connect(self.stopRequested)

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 - имя из Qt
        """Отрисовка полупрозрачной подложки со скруглением."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor(28, 30, 34, 230))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(self.rect(), 8, 8)
        painter.end()

    def show_at_corner(self) -> None:
        """Показ панели в правом верхнем углу основного монитора."""
        self.adjustSize()
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            self.move(
                area.right() - self.width() - SCREEN_MARGIN,
                area.top() + SCREEN_MARGIN,
            )
        self.show()
        self.raise_()

    def update_elapsed(self, milliseconds: int) -> None:
        """Обновление показаний счётчика длительности."""
        self._timer_label.setText(format_timecode(milliseconds / 1000))

    def update_state(self, state: RecorderState) -> None:
        """Согласование подписей с текущим состоянием записи."""
        self._state_label.setText(state.label)
        # На паузе кнопка предлагает продолжить запись.
        self._pause_button.setText(
            tr("Продолжить") if state is RecorderState.PAUSED else tr("Пауза")
        )
        busy = state in (RecorderState.STOPPING, RecorderState.PROCESSING)
        self._pause_button.setEnabled(not busy)
        self._stop_button.setEnabled(not busy)
