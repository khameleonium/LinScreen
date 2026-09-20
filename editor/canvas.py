"""
Холст редактора аннотаций на базе графической сцены Qt.

Снимок помещается в сцену нижним слоем, аннотации добавляются поверх него
отдельными элементами. Итоговое изображение получается отрисовкой сцены в
растр, поэтому порядок наложения и качество совпадают с тем, что видит
пользователь на экране.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QImage, QPainter, QPixmap, QWheelEvent
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsScene,
    QGraphicsSceneMouseEvent,
    QGraphicsView,
    QWidget,
)

from editor.items import ArrowItem, BlurItem, FrameItem, LabelItem, StepItem, StrokeItem
from editor.tools import Tool, ToolSettings

# Пределы масштабирования просмотра.
MIN_ZOOM = 0.1
MAX_ZOOM = 8.0


class AnnotationScene(QGraphicsScene):
    """Сцена со снимком и пользовательскими аннотациями."""

    # Изменение состава аннотаций: используется для обновления панели.
    contentChanged = Signal()

    def __init__(self, image: QImage, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._image = image
        self.setSceneRect(QRectF(image.rect()))
        # Снимок добавляется первым и остаётся под всеми аннотациями.
        self._background = self.addPixmap(QPixmap.fromImage(image))
        self._background.setZValue(-1000)

        self.tool = Tool.ARROW
        self.settings = ToolSettings.default()

        # Стеки отмены и возврата хранят сами элементы сцены: повторное
        # добавление восстанавливает аннотацию вместе со всеми параметрами.
        self._history: list[QGraphicsItem] = []
        self._undone: list[QGraphicsItem] = []

        self._active_item: QGraphicsItem | None = None
        self._origin = QPointF()
        self._editing_label: LabelItem | None = None

    # ---------------------------------------------------------------- состав

    @property
    def source_image(self) -> QImage:
        """Исходный снимок без аннотаций."""
        return self._image

    @property
    def can_undo(self) -> bool:
        """Признак наличия действий для отмены."""
        return bool(self._history)

    @property
    def can_redo(self) -> bool:
        """Признак наличия отменённых действий для возврата."""
        return bool(self._undone)

    def undo(self) -> None:
        """Отмена последней аннотации."""
        if not self._history:
            return
        item = self._history.pop()
        self.removeItem(item)
        self._undone.append(item)
        self.contentChanged.emit()

    def redo(self) -> None:
        """Возврат последней отменённой аннотации."""
        if not self._undone:
            return
        item = self._undone.pop()
        self.addItem(item)
        self._history.append(item)
        self.contentChanged.emit()

    def remove_selected(self) -> None:
        """
        Удаление выбранных аннотаций.

        Элемент остаётся в стеке возврата, поэтому удаление отменяется
        так же, как и любое другое действие.
        """
        removed = [item for item in self.selectedItems() if item in self._history]
        if not removed:
            return
        for item in removed:
            self.removeItem(item)
            self._history.remove(item)
            self._undone.append(item)
        self.contentChanged.emit()

    def clear_annotations(self) -> None:
        """Удаление всех аннотаций со снимка."""
        for item in self._history:
            self.removeItem(item)
        self._history.clear()
        self._undone.clear()
        self.settings.step_counter = 1
        self.contentChanged.emit()

    def _register(self, item: QGraphicsItem) -> None:
        """Добавление элемента на сцену с записью в историю."""
        self.addItem(item)
        self._history.append(item)
        # Новая аннотация отменяет ранее отменённые: линейная история
        # понятнее пользователю, чем ветвящаяся.
        self._undone.clear()
        self.contentChanged.emit()

    # -------------------------------------------------------------- рисование

    def mousePressEvent(self, event: QGraphicsSceneMouseEvent) -> None:  # noqa: N802
        """Начало нового элемента аннотации."""
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return

        # Незавершённый ввод текста закрывается при щелчке в стороне.
        self._finish_label_editing()

        position = event.scenePos()
        self._origin = position
        # Общий тип позволяет хранить в одной переменной элементы разных
        # инструментов: дальнейшая обработка ведётся по фактическому типу.
        created: QGraphicsItem | None = None

        if self.tool is Tool.ARROW:
            arrow = ArrowItem(self.settings.color, self.settings.width)
            arrow.set_line(position, position)
            created = arrow
        elif self.tool is Tool.RECT:
            frame = FrameItem(self.settings.color, self.settings.width)
            frame.setRect(QRectF(position, position))
            created = frame
        elif self.tool in (Tool.PENCIL, Tool.MARKER):
            stroke = StrokeItem(
                self.settings.color, self.settings.width, marker=self.tool is Tool.MARKER
            )
            stroke.start(position)
            created = stroke
        elif self.tool is Tool.BLUR:
            created = BlurItem(
                self._image, QRectF(position, position), self.settings.blur_factor
            )
        elif self.tool is Tool.TEXT:
            self._create_label(position)
        elif self.tool is Tool.STEP:
            self._create_step(position)
        else:
            super().mousePressEvent(event)

        if created is not None:
            self._active_item = created
            self._register(created)

    def mouseMoveEvent(self, event: QGraphicsSceneMouseEvent) -> None:  # noqa: N802
        """Изменение геометрии создаваемого элемента."""
        item = self._active_item
        if item is None:
            super().mouseMoveEvent(event)
            return

        position = event.scenePos()
        if isinstance(item, ArrowItem):
            item.set_line(self._origin, position)
        elif isinstance(item, FrameItem):
            item.setRect(QRectF(self._origin, position).normalized())
        elif isinstance(item, StrokeItem):
            item.extend(position)
        elif isinstance(item, BlurItem):
            item.set_area(QRectF(self._origin, position).normalized())

    def mouseReleaseEvent(self, event: QGraphicsSceneMouseEvent) -> None:  # noqa: N802
        """Завершение создания элемента."""
        item = self._active_item
        self._active_item = None
        if item is None:
            super().mouseReleaseEvent(event)
            return

        # Вырожденные элементы нулевого размера удаляются: чаще всего они
        # появляются от случайного щелчка мимо цели.
        if isinstance(item, (FrameItem, BlurItem)):
            bounds = item.boundingRect()
            if bounds.width() < 3 or bounds.height() < 3:
                self.removeItem(item)
                if item in self._history:
                    self._history.remove(item)
                self.contentChanged.emit()

    def _create_label(self, position: QPointF) -> None:
        """Создание текстовой надписи в режиме ввода."""
        label = LabelItem(self.settings.color, self.settings.font_size)
        label.setPos(position)
        self._register(label)
        label.start_editing()
        self._editing_label = label

    def _create_step(self, position: QPointF) -> None:
        """Размещение очередного кружка нумератора."""
        step = StepItem(self.settings.step_counter, self.settings.color)
        step.setPos(position)
        self._register(step)
        # Счётчик увеличивается после размещения: следующий щелчок даст
        # следующий по порядку номер.
        self.settings.step_counter += 1

    def _finish_label_editing(self) -> None:
        """Завершение ввода текста и удаление пустой надписи."""
        label = self._editing_label
        self._editing_label = None
        if label is None:
            return
        label.finish_editing()
        if not label.toPlainText().strip():
            # Пустая надпись бесполезна и удаляется вместе с записью в истории.
            self.removeItem(label)
            if label in self._history:
                self._history.remove(label)
            self.contentChanged.emit()

    # ---------------------------------------------------------------- экспорт

    def render_result(self) -> QImage:
        """
        Отрисовка сцены в готовое изображение.

        Перед отрисовкой снимается выделение элементов: пунктирные рамки
        выделения являются частью интерфейса и в файл попадать не должны.
        """
        self._finish_label_editing()
        self.clearSelection()
        self.clearFocus()

        result = QImage(self._image.size(), QImage.Format.Format_ARGB32)
        result.fill(0)
        painter = QPainter(result)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        try:
            self.render(painter, QRectF(result.rect()), self.sceneRect())
        finally:
            painter.end()
        return result


class AnnotationView(QGraphicsView):
    """Просмотр сцены с масштабированием колесом мыши."""

    def __init__(self, scene: AnnotationScene, parent: QWidget | None = None) -> None:
        super().__init__(scene, parent)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        # Масштабирование ведётся относительно указателя мыши.
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self._zoom = 1.0

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802 - имя из Qt
        """Масштабирование просмотра колесом мыши."""
        step = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        target = self._zoom * step
        if not MIN_ZOOM <= target <= MAX_ZOOM:
            return
        self._zoom = target
        self.scale(step, step)

    def fit_to_window(self) -> None:
        """Подгонка масштаба под размер окна."""
        self.resetTransform()
        self._zoom = 1.0
        scene = self.scene()
        if scene is None:
            return
        # Уменьшение применяется только к крупным снимкам: мелкие
        # изображения растягивать не требуется.
        area = self.viewport().rect()
        bounds = scene.sceneRect()
        if bounds.width() > area.width() or bounds.height() > area.height():
            self.fitInView(bounds, Qt.AspectRatioMode.KeepAspectRatio)
            self._zoom = self.transform().m11()
