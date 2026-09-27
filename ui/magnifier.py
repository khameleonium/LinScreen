"""
Лупа у указателя при выделении области.

Лупа показывает увеличенный участок замороженного снимка вокруг указателя,
чтобы границу области можно было поставить с точностью до точки. Точки
снимка при увеличении не сглаживаются: каждая видна отдельным квадратом.
В центре лупы перекрестие отмечает точку под указателем, под лупой
выводятся её координаты и цвет.

Круг лупы смещён от указателя вправо и вниз, чтобы не закрывать место,
куда пользователь смотрит; у края монитора он переходит на другую сторону.
Радиус меняется колесом мыши.

Модуль не зависит от устройства оверлея: он получает снимок, положение
указателя и границы монитора в логических координатах рабочего стола, а
рисует в переданном художнике с заданным смещением.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath, QPen

# Пределы радиуса лупы в логических точках.
MIN_RADIUS = 30
MAX_RADIUS = 250
# Изменение радиуса за один щелчок колеса мыши.
RADIUS_STEP = 10
# Пределы кратности увеличения.
MIN_ZOOM = 2
MAX_ZOOM = 12

# Расстояние от указателя до края круга лупы.
CURSOR_GAP = 24
# Высота подписи под лупой и зазор до неё.
INFO_HEIGHT = 22
INFO_GAP = 6
# Запас вокруг лупы при перерисовке: обводка и сглаживание краёв.
PAINT_MARGIN = 4

# Цвета оформления: светлая обводка на тёмной окантовке видна на любом фоне.
OUTER_COLOR = QColor(20, 20, 20, 220)
RING_COLOR = QColor(245, 245, 245)
CROSS_COLOR = QColor(80, 200, 110)
INFO_BACKGROUND = QColor(0, 0, 0, 200)
INFO_TEXT = QColor(240, 240, 240)


@dataclass
class MagnifierOptions:
    """Параметры лупы из настроек."""

    enabled: bool = True
    radius: int = 70
    zoom: int = 4
    show_info: bool = True


def clamp_radius(radius: int) -> int:
    """Ограничение радиуса допустимыми пределами."""
    return max(MIN_RADIUS, min(MAX_RADIUS, radius))


def clamp_zoom(zoom: int) -> int:
    """Ограничение кратности увеличения допустимыми пределами."""
    return max(MIN_ZOOM, min(MAX_ZOOM, zoom))


class Magnifier:
    """Расчёт положения и отрисовка лупы над замороженным снимком."""

    def __init__(self, desktop: QImage, desktop_rect: QRect, options: MagnifierOptions) -> None:
        self._desktop = desktop
        # Логическая геометрия всего рабочего стола, которой соответствует
        # снимок целиком.
        self._desktop_rect = QRect(desktop_rect)
        # Отношение точек снимка к логическим координатам. Берётся по
        # фактическому размеру снимка: при дробном масштабе экрана
        # коэффициент системы неточен.
        self._scale = desktop.width() / desktop_rect.width() if desktop_rect.width() else 1.0
        self._enabled = options.enabled
        self._radius = clamp_radius(options.radius)
        self._zoom = clamp_zoom(options.zoom)
        self._show_info = options.show_info

    @property
    def enabled(self) -> bool:
        """Признак включённой лупы."""
        return self._enabled

    @property
    def radius(self) -> int:
        """Текущий радиус в логических точках."""
        return self._radius

    def adjust_radius(self, steps: int) -> bool:
        """
        Изменение радиуса на заданное число щелчков колеса.

        Возвращается признак того, что радиус изменился.
        """
        updated = clamp_radius(self._radius + steps * RADIUS_STEP)
        if updated == self._radius:
            return False
        self._radius = updated
        return True

    # ------------------------------------------------------------ геометрия

    def lens_center(self, cursor: QPoint, bounds: QRect) -> QPoint:
        """
        Центр круга лупы в логических координатах рабочего стола.

        Лупа располагается правее и ниже указателя. Если она не помещается
        в границы монитора, переносится на противоположную сторону по той
        оси, по которой не поместилась.
        """
        offset = self._radius + CURSOR_GAP
        info = INFO_GAP + INFO_HEIGHT if self._show_info else 0
        x = cursor.x() + offset
        if x + self._radius > bounds.right():
            x = cursor.x() - offset
        y = cursor.y() + offset
        if y + self._radius + info > bounds.bottom():
            y = cursor.y() - offset - info
        return QPoint(x, y)

    def area(self, cursor: QPoint, bounds: QRect) -> QRect:
        """Прямоугольник, занимаемый лупой вместе с подписью."""
        center = self.lens_center(cursor, bounds)
        size = self._radius * 2
        rect = QRect(center.x() - self._radius, center.y() - self._radius, size, size)
        if self._show_info:
            rect = rect.adjusted(0, 0, 0, INFO_GAP + INFO_HEIGHT)
            # Подпись может быть шире лупы малого радиуса.
            rect = rect.united(
                QRect(center.x() - 90, rect.bottom() - INFO_HEIGHT, 180, INFO_HEIGHT)
            )
        return rect.adjusted(-PAINT_MARGIN, -PAINT_MARGIN, PAINT_MARGIN, PAINT_MARGIN)

    # ------------------------------------------------------------ отрисовка

    def paint(self, painter: QPainter, cursor: QPoint, bounds: QRect, origin: QPoint) -> None:
        """
        Отрисовка лупы.

        Координаты cursor и bounds - логические координаты рабочего стола,
        origin - точка рабочего стола, соответствующая началу координат
        художника (левый верхний угол окна оверлея).
        """
        if not self._enabled:
            return
        center = self.lens_center(cursor, bounds) - origin
        radius = self._radius
        target = QRectF(center.x() - radius, center.y() - radius, radius * 2, radius * 2)

        # Участок снимка вокруг указателя: его ширина в логических точках
        # равна диаметру лупы, делённому на кратность увеличения.
        half = radius / self._zoom
        source_center = QPointF(
            (cursor.x() - self._desktop_rect.x() + 0.5) * self._scale,
            (cursor.y() - self._desktop_rect.y() + 0.5) * self._scale,
        )
        source = QRectF(
            source_center.x() - half * self._scale,
            source_center.y() - half * self._scale,
            half * 2 * self._scale,
            half * 2 * self._scale,
        )

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        # Увеличение без сглаживания: каждая точка снимка видна квадратом.
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)

        circle = QPainterPath()
        circle.addEllipse(target)
        painter.fillPath(circle, QColor(0, 0, 0))
        painter.setClipPath(circle)
        painter.drawImage(target, self._desktop, source)
        self._paint_cross(painter, target)
        painter.setClipping(False)

        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(OUTER_COLOR, 4))
        painter.drawEllipse(target)
        painter.setPen(QPen(RING_COLOR, 2))
        painter.drawEllipse(target)

        if self._show_info:
            self._paint_info(painter, cursor, target)
        painter.restore()

    def _paint_cross(self, painter: QPainter, target: QRectF) -> None:
        """Перекрестие с рамкой вокруг точки под указателем."""
        # Размер одной точки снимка внутри лупы.
        cell = self._zoom / self._scale
        center = target.center()
        cell_rect = QRectF(center.x() - cell / 2, center.y() - cell / 2, cell, cell)
        pen = QPen(CROSS_COLOR, 1)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawLine(
            QPointF(target.left(), center.y()), QPointF(cell_rect.left(), center.y())
        )
        painter.drawLine(
            QPointF(cell_rect.right(), center.y()), QPointF(target.right(), center.y())
        )
        painter.drawLine(QPointF(center.x(), target.top()), QPointF(center.x(), cell_rect.top()))
        painter.drawLine(
            QPointF(center.x(), cell_rect.bottom()), QPointF(center.x(), target.bottom())
        )
        painter.drawRect(cell_rect)

    def _paint_info(self, painter: QPainter, cursor: QPoint, target: QRectF) -> None:
        """Подпись с координатами и цветом точки под указателем."""
        color = self.color_at(cursor)
        text = f"{cursor.x()}, {cursor.y()}   {color.name().upper()}"
        font = QFont()
        font.setPointSize(9)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        swatch = INFO_HEIGHT - 8
        width = metrics.horizontalAdvance(text) + swatch + 18
        box = QRectF(
            target.center().x() - width / 2, target.bottom() + INFO_GAP, width, INFO_HEIGHT
        )
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(INFO_BACKGROUND)
        painter.drawRoundedRect(box, 4, 4)
        painter.setBrush(color)
        painter.setPen(QPen(RING_COLOR, 1))
        painter.drawRect(QRectF(box.left() + 5, box.top() + 4, swatch, swatch))
        painter.setPen(INFO_TEXT)
        painter.drawText(
            box.adjusted(swatch + 10, 0, -4, 0),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            text,
        )

    def color_at(self, cursor: QPoint) -> QColor:
        """Цвет снимка в точке под указателем."""
        x = int((cursor.x() - self._desktop_rect.x() + 0.5) * self._scale)
        y = int((cursor.y() - self._desktop_rect.y() + 0.5) * self._scale)
        if not self._desktop.rect().contains(x, y):
            return QColor(0, 0, 0)
        return self._desktop.pixelColor(x, y)
