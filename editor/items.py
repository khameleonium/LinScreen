"""
Графические элементы аннотаций для сцены редактора.

Каждый элемент - самостоятельный объект сцены Qt, поэтому порядок
наложения, перемещение и удаление обеспечиваются штатными средствами
QGraphicsScene без собственной системы слоёв.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QLineF, QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetricsF,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
)
from PySide6.QtWidgets import (
    QGraphicsItem,
    QStyleOptionGraphicsItem,
    QGraphicsPathItem,
    QGraphicsPixmapItem,
    QGraphicsRectItem,
    QGraphicsTextItem,
    QWidget,
)

# Доля длины стрелки, отводимая под наконечник, и его предельный размер.
ARROW_HEAD_RATIO = 0.25
ARROW_HEAD_MAX = 28.0
# Радиус кружка нумератора шагов.
STEP_RADIUS = 15.0


def make_pen(color: QColor, width: int) -> QPen:
    """Перо со скруглениями, пригодное для всех линейных инструментов."""
    pen = QPen(color, width)
    # Скруглённые концы и соединения не дают ломаным линиям выглядеть рвано.
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return pen


class ArrowItem(QGraphicsItem):
    """Стрелка с треугольным наконечником."""

    def __init__(self, color: QColor, width: int) -> None:
        super().__init__()
        self._line = QLineF()
        self._color = color
        self._width = width
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)

    def set_line(self, start: QPointF, end: QPointF) -> None:
        """Задание координат стрелки с уведомлением сцены о перерисовке."""
        # Смена геометрии требует предварительного уведомления, иначе на
        # сцене остаются следы прежнего положения.
        self.prepareGeometryChange()
        self._line = QLineF(start, end)
        self.update()

    def boundingRect(self) -> QRectF:  # noqa: N802 - имя определено Qt
        """Охватывающий прямоугольник с запасом под наконечник и перо."""
        margin = max(ARROW_HEAD_MAX, self._width * 2)
        return (
            QRectF(self._line.p1(), self._line.p2())
            .normalized()
            .adjusted(-margin, -margin, margin, margin)
        )

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionGraphicsItem,
        widget: QWidget | None = None,
    ) -> None:
        """Отрисовка древка и наконечника стрелки."""
        if self._line.length() < 1:
            return
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        head = min(ARROW_HEAD_MAX, max(8.0, self._line.length() * ARROW_HEAD_RATIO))
        angle = math.atan2(-self._line.dy(), self._line.dx())
        tip = self._line.p2()

        # Древко укорачивается на длину наконечника, чтобы линия не
        # просвечивала сквозь острие при большой толщине пера.
        shaft_end = QPointF(
            tip.x() - math.cos(angle) * head * 0.8,
            tip.y() + math.sin(angle) * head * 0.8,
        )
        painter.setPen(make_pen(self._color, self._width))
        painter.drawLine(QLineF(self._line.p1(), shaft_end))

        # Наконечник строится поворотом на фиксированный угол в обе стороны.
        spread = math.radians(26)
        left = QPointF(
            tip.x() - math.cos(angle - spread) * head,
            tip.y() + math.sin(angle - spread) * head,
        )
        right = QPointF(
            tip.x() - math.cos(angle + spread) * head,
            tip.y() + math.sin(angle + spread) * head,
        )
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(self._color))
        painter.drawPolygon(QPolygonF([tip, left, right]))


class FrameItem(QGraphicsRectItem):
    """Прямоугольная рамка без заливки."""

    def __init__(self, color: QColor, width: int) -> None:
        super().__init__()
        self.setPen(make_pen(color, width))
        self.setBrush(Qt.BrushStyle.NoBrush)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)


class StrokeItem(QGraphicsPathItem):
    """Произвольная линия карандаша или маркера."""

    def __init__(self, color: QColor, width: int, marker: bool = False) -> None:
        super().__init__()
        pen_color = QColor(color)
        if marker:
            # Маркер полупрозрачен и заметно толще карандаша, за счёт чего
            # подсвечивает содержимое, а не закрашивает его.
            pen_color.setAlpha(110)
            width = max(width * 4, 12)
        self.setPen(make_pen(pen_color, width))
        self.setBrush(Qt.BrushStyle.NoBrush)
        self._path = QPainterPath()
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)

    def start(self, point: QPointF) -> None:
        """Начало новой линии."""
        self._path = QPainterPath(point)
        self.setPath(self._path)

    def extend(self, point: QPointF) -> None:
        """Продление линии до указанной точки."""
        self._path.lineTo(point)
        self.setPath(self._path)


class BlurItem(QGraphicsPixmapItem):
    """
    Замена участка изображения его размытой копией.

    Размытие выполняется уменьшением фрагмента с последующим увеличением:
    способ не требует внешних библиотек, работает мгновенно и полностью
    уничтожает исходные данные, что и требуется для скрытия сведений.
    """

    def __init__(self, source: QImage, area: QRectF, factor: int = 12) -> None:
        super().__init__()
        self._source = source
        self._factor = max(2, factor)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.set_area(area)

    def set_area(self, area: QRectF) -> None:
        """Пересчёт размытого фрагмента под новые границы области."""
        # Приведение к вещественному прямоугольнику допускает передачу
        # как сценических координат, так и целочисленных границ.
        rect = QRectF(area).normalized().toRect()
        # Область ограничивается размерами снимка: выход за край даёт
        # пустое изображение и незаметную для пользователя ошибку.
        rect = rect.intersected(self._source.rect())
        if rect.width() < 2 or rect.height() < 2:
            self.setPixmap(QPixmap())
            return

        fragment = self._source.copy(rect)
        small = fragment.scaled(
            max(1, rect.width() // self._factor),
            max(1, rect.height() // self._factor),
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        blurred = small.scaled(
            rect.width(),
            rect.height(),
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.setPixmap(QPixmap.fromImage(blurred))
        self.setPos(rect.topLeft())


class LabelItem(QGraphicsTextItem):
    """Текстовая надпись с контрастной подложкой."""

    PADDING = 5.0

    def __init__(self, color: QColor, font_size: int) -> None:
        super().__init__()
        font = QFont()
        font.setPointSize(font_size)
        font.setBold(True)
        self.setFont(font)
        self.setDefaultTextColor(color)
        # Подложка подбирается по яркости текста: светлый текст получает
        # тёмную основу и наоборот.
        self._background = (
            QColor(0, 0, 0, 160) if color.lightness() > 110 else QColor(255, 255, 255, 190)
        )
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)

    def start_editing(self) -> None:
        """Перевод надписи в режим ввода текста."""
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextEditorInteraction)
        self.setFocus(Qt.FocusReason.MouseFocusReason)

    def finish_editing(self) -> None:
        """Завершение ввода и возврат к обычному состоянию."""
        self.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)

    def boundingRect(self) -> QRectF:  # noqa: N802 - имя определено Qt
        """Границы с учётом подложки."""
        return (
            super()
            .boundingRect()
            .adjusted(-self.PADDING, -self.PADDING, self.PADDING, self.PADDING)
        )

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionGraphicsItem,
        widget: QWidget | None = None,
    ) -> None:
        """Отрисовка подложки и поверх неё самого текста."""
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QBrush(self._background))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(self.boundingRect(), 4, 4)
        # Штатная отрисовка текста вызывается после подложки. Состояние
        # параметра отрисовки не изменяется: признак выделения снимается
        # вызовом clearSelection() перед экспортом изображения.
        # Пустой виджет допустим при отрисовке вне области просмотра,
        # хотя подпись метода в стабах этого не отражает.
        super().paint(painter, option, widget)  # type: ignore[arg-type]


class StepItem(QGraphicsItem):
    """Круговой нумератор шагов с порядковым номером внутри."""

    def __init__(self, number: int, color: QColor) -> None:
        super().__init__()
        self._number = number
        self._color = color
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)

    def boundingRect(self) -> QRectF:  # noqa: N802 - имя определено Qt
        """Квадрат, описывающий кружок нумератора."""
        return QRectF(-STEP_RADIUS - 2, -STEP_RADIUS - 2, STEP_RADIUS * 2 + 4, STEP_RADIUS * 2 + 4)

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionGraphicsItem,
        widget: QWidget | None = None,
    ) -> None:
        """Отрисовка кружка, обводки и номера."""
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QBrush(self._color))
        # Белая обводка отделяет кружок от пёстрого фона снимка.
        painter.setPen(make_pen(QColor(255, 255, 255), 2))
        painter.drawEllipse(QPointF(0, 0), STEP_RADIUS, STEP_RADIUS)

        font = QFont()
        font.setPointSize(11)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor(255, 255, 255))

        text = str(self._number)
        metrics = QFontMetricsF(font)
        # Текст центрируется вручную: штатное выравнивание рассчитано на
        # прямоугольник, а не на окружность.
        painter.drawText(
            QPointF(-metrics.horizontalAdvance(text) / 2, metrics.capHeight() / 2),
            text,
        )


class CropOverlayItem(QGraphicsItem):
    """
    Интерактивный оверлей кадрирования снимка.

    Затемняет невыбранную область изображения полупрозрачной маской,
    выделяет область кадрирования контрастной рамкой, угловыми засечками
    и плашкой с текущими геометрическими размерами выделения.
    """

    def __init__(self, scene_rect: QRectF) -> None:
        super().__init__()
        self._scene_rect = scene_rect
        self._crop_rect = QRectF()
        # Оверлей должен находиться поверх всех аннотаций.
        self.setZValue(10000.0)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)

    def set_scene_rect(self, rect: QRectF) -> None:
        """Обновление границ всей сцены снимка."""
        self.prepareGeometryChange()
        self._scene_rect = rect
        self.update()

    def set_crop_rect(self, rect: QRectF) -> None:
        """Задание новой геометрии выделяемой области кадрирования."""
        self.prepareGeometryChange()
        self._crop_rect = rect
        self.update()

    def crop_rect(self) -> QRectF:
        """Нормализованный прямоугольник области кадрирования."""
        return self._crop_rect.normalized()

    def boundingRect(self) -> QRectF:  # noqa: N802 - имя определено Qt
        """Охватывающий прямоугольник оверлея."""
        return self._scene_rect.united(self._crop_rect.normalized()).adjusted(
            -20.0, -20.0, 20.0, 20.0
        )

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionGraphicsItem,
        widget: QWidget | None = None,
    ) -> None:
        """Отрисовка затемняющей маски, контура и бейджа с размерами."""
        r = self._crop_rect.normalized()

        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # 1. Затемняющая маска вокруг выбранного прямоугольника
        mask_path = QPainterPath()
        mask_path.addRect(self._scene_rect)
        if r.isValid() and r.width() >= 1.0 and r.height() >= 1.0:
            mask_path.addRect(r)
        mask_path.setFillRule(Qt.FillRule.OddEvenFill)
        painter.fillPath(mask_path, QBrush(QColor(0, 0, 0, 140)))

        if not r.isValid() or r.width() < 3.0 or r.height() < 3.0:
            return

        # 2. Пунктирная белая рамка границы обрезки
        dashed_pen = QPen(QColor(255, 255, 255, 230), 1.5, Qt.PenStyle.DashLine)
        painter.setPen(dashed_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(r)

        # 3. Контрастные угловые засечки
        corner_len = min(14.0, min(r.width(), r.height()) / 2.5)
        if corner_len > 2.0:
            corner_pen = QPen(QColor(255, 255, 255), 3.0)
            corner_pen.setCapStyle(Qt.PenCapStyle.SquareCap)
            painter.setPen(corner_pen)
            # Верхний левый угол
            painter.drawLine(QPointF(r.left(), r.top() + corner_len), QPointF(r.left(), r.top()))
            painter.drawLine(QPointF(r.left(), r.top()), QPointF(r.left() + corner_len, r.top()))
            # Верхний правый угол
            painter.drawLine(QPointF(r.right() - corner_len, r.top()), QPointF(r.right(), r.top()))
            painter.drawLine(QPointF(r.right(), r.top()), QPointF(r.right(), r.top() + corner_len))
            # Нижний левый угол
            painter.drawLine(
                QPointF(r.left(), r.bottom() - corner_len), QPointF(r.left(), r.bottom())
            )
            painter.drawLine(
                QPointF(r.left(), r.bottom()), QPointF(r.left() + corner_len, r.bottom())
            )
            # Нижний правый угол
            painter.drawLine(
                QPointF(r.right() - corner_len, r.bottom()), QPointF(r.right(), r.bottom())
            )
            painter.drawLine(
                QPointF(r.right(), r.bottom()), QPointF(r.right(), r.bottom() - corner_len)
            )

        # 4. Бейдж с текущими размерами в пикселях (Ш x В)
        badge_text = f"{int(round(r.width()))} × {int(round(r.height()))}"
        font = QFont()
        font.setPointSize(9)
        font.setBold(True)
        metrics = QFontMetricsF(font)
        text_width = metrics.horizontalAdvance(badge_text)
        text_height = metrics.capHeight()

        badge_w = text_width + 12.0
        badge_h = text_height + 8.0

        # Позиционирование бейджа: снизу от рамки или внутри неё, если снизу нет места
        badge_x = r.center().x() - badge_w / 2.0
        badge_y = r.bottom() + 6.0
        if badge_y + badge_h > self._scene_rect.bottom():
            badge_y = r.bottom() - badge_h - 6.0

        # Коррекция по горизонтали, чтобы бейдж не вылезал за границы сцены
        badge_x = max(
            self._scene_rect.left() + 2.0,
            min(badge_x, self._scene_rect.right() - badge_w - 2.0),
        )

        badge_rect = QRectF(badge_x, badge_y, badge_w, badge_h)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor(0, 0, 0, 190)))
        painter.drawRoundedRect(badge_rect, 3.0, 3.0)

        painter.setFont(font)
        painter.setPen(QColor(255, 255, 255))
        painter.drawText(
            QPointF(badge_x + 6.0, badge_y + badge_h - 4.0),
            badge_text,
        )
