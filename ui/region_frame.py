"""
Рамка вокруг записываемой области.

Во время записи пользователю важно видеть, что именно попадает в кадр.
Рамка рисуется **снаружи** указанной области: внутрь она не заходит и
потому сама в запись не попадает. Середина окна вырезана маской, поэтому
рамка не перекрывает содержимое и не мешает работе с окнами под ней.

Цвет отражает состояние: красный при записи, жёлтый на паузе. Зелёный
цвет применяется оверлеем выделения при подготовке, см. ui/overlay.py.
"""

from __future__ import annotations

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QPaintEvent, QPainter, QRegion
from PySide6.QtWidgets import QWidget

from core.i18n import tr

# Толщина рамки в точках. Достаточна, чтобы быть заметной, и мала, чтобы
# не закрывать содержимое соседних окон.
BORDER_WIDTH = 3

# Цвета состояний записи.
RECORDING_COLOR = QColor(224, 74, 74)
PAUSED_COLOR = QColor(224, 162, 60)


class RegionFrame(QWidget):
    """Прямоугольная рамка, обводящая записываемую область снаружи."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._color = RECORDING_COLOR

        # Окно не принимает ввод и не забирает фокус: оно лишь показывает
        # границы области и не должно мешать работе.
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.X11BypassWindowManagerHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowTitle(tr("Записываемая область"))

    def show_for(self, area: QRect, color: QColor = RECORDING_COLOR) -> None:
        """
        Показ рамки вокруг указанной области.

        Окно располагается на толщину рамки шире области со всех сторон,
        а середина вырезается маской. Благодаря этому сама область
        остаётся нетронутой и рамка не попадает в запись.
        """
        self._color = color
        outer = area.adjusted(-BORDER_WIDTH, -BORDER_WIDTH, BORDER_WIDTH, BORDER_WIDTH)
        self.setGeometry(outer)

        inner = QRect(
            BORDER_WIDTH,
            BORDER_WIDTH,
            max(0, outer.width() - BORDER_WIDTH * 2),
            max(0, outer.height() - BORDER_WIDTH * 2),
        )
        self.setMask(QRegion(self.rect()).subtracted(QRegion(inner)))

        self.show()
        self.raise_()
        self.update()

    def set_color(self, color: QColor) -> None:
        """Смена цвета рамки без изменения её положения."""
        if color == self._color:
            return
        self._color = color
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 - имя из Qt
        """Заливка окна цветом состояния; середину вырезает маска."""
        painter = QPainter(self)
        painter.fillRect(self.rect(), self._color)
        painter.end()
