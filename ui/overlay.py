"""
Полноэкранный оверлей выделения области.

Оверлей показывается поверх замороженного снимка рабочего стола: содержимое
экрана снимается до его появления, поэтому сам оверлей в кадр не попадает,
а картинка не меняется между выделением и сохранением.

Окно занимает объединённую геометрию всех мониторов и работает как в режиме
снимка, так и в режиме выбора области для записи.
"""

from __future__ import annotations

from core.i18n import tr

from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QGuiApplication,
    QImage,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import QWidget

from capture.windows import InterfaceObject, object_at

# Прозрачность затемнения незанятой части экрана.
DIM_ALPHA = 130
# Минимальный размер области: случайный щелчок не должен давать снимок
# размером в один пиксель.
MINIMUM_SIZE = 8
# Смещение указателя, до которого нажатие считается щелчком, а не
# протягиванием. Без допуска дрожание руки отменяло бы автоматический
# выбор объекта под курсором.
CLICK_TOLERANCE = 4

# Цвет рамки выделения. Зелёный выбран как признак подготовки: во время
# самой записи рамка становится красной, см. ui/region_frame.py.
SELECTION_COLOR = QColor(80, 200, 110)
# Цвет затемнения незанятой части экрана.
DIM_COLOR = QColor(0, 0, 0, DIM_ALPHA)


class RegionOverlay(QWidget):
    """Окно выбора прямоугольной области на замороженном снимке экрана."""

    # Область выбрана: передаются логические координаты рабочего стола.
    selected = Signal(QRect)
    # Выбор отменён клавишей Esc или правой кнопкой мыши.
    cancelled = Signal()

    def __init__(
        self,
        desktop: QImage,
        virtual_rect: QRect,
        hint: str = tr("Выделите область: ЛКМ — выбор, Esc — отмена, Enter — весь экран"),
        objects: list[InterfaceObject] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._desktop = desktop
        self._virtual_rect = virtual_rect
        self._hint = hint
        # Снимок объектов интерфейса, собранный до показа оверлея: сам
        # оверлей занимает весь экран и иначе оказался бы единственным
        # совпадением под курсором.
        self._objects = objects or []
        # Границы объекта под курсором.
        self._hovered: QRect | None = None
        self._origin = QPoint()
        self._current = QPoint()
        self._selecting = False
        self._finished = False
        # Подготовленное изображение экрана в размере окна. Готовится
        # однократно: масштабирование на каждую перерисовку заметно
        # замедляло бы выделение области на слабом оборудовании и при
        # больших разрешениях.
        self._background = QPixmap()

        # Окно без рамки, поверх всех прочих и вне управления менеджером окон:
        # только так оверлей перекрывает панели и всплывающие подсказки.
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.X11BypassWindowManagerHint
            | Qt.WindowType.Tool
        )
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setMouseTracking(True)
        self.setGeometry(virtual_rect)

    def set_objects(self, objects: list[InterfaceObject]) -> None:
        """
        Передача перечня объектов интерфейса после его сбора.

        Сбор выполняется в отдельном потоке, поэтому оверлей может быть
        показан раньше, чем перечень готов.
        """
        self._objects = objects

    def _prepare_background(self) -> None:
        """Подготовка изображения экрана к быстрой отрисовке."""
        if not self._background.isNull():
            return
        pixmap = QPixmap.fromImage(self._desktop)
        if pixmap.size() != self.size():
            # Снимок хранится в физических пикселях: при масштабировании
            # экрана его размер отличается от размера окна.
            pixmap = pixmap.scaled(
                self.size(),
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        self._background = pixmap

    def show_overlay(self) -> None:
        """Показ оверлея с захватом ввода."""
        self._prepare_background()
        self.show()
        self.raise_()
        self.activateWindow()
        # Явный захват клавиатуры нужен из-за обхода менеджера окон:
        # без него нажатия продолжат получать окна под оверлеем.
        self.grabKeyboard()

    # ------------------------------------------------------------ отрисовка

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 - имя из Qt
        """Отрисовка снимка, затемнения и текущей рамки выделения."""
        self._prepare_background()
        painter = QPainter(self)
        target = self.rect()
        # Изображение уже приведено к размеру окна, поэтому вывод сводится
        # к простому переносу точек без пересчёта.
        painter.drawPixmap(0, 0, self._background)

        selection = self._selection_rect()

        # Рамка показывается только во время протягивания: до первого
        # нажатия прямоугольник вырожден и вместо него выводится подсказка.
        if self._selecting and selection.width() > 1 and selection.height() > 1:
            # Затемняются только полосы вокруг выбранной области: заливка
            # всего экрана с последующим восстановлением яркости требует
            # двух проходов по всем точкам вместо одного.
            dim = DIM_COLOR
            painter.fillRect(QRect(0, 0, target.width(), selection.top()), dim)
            painter.fillRect(
                QRect(
                    0,
                    selection.bottom() + 1,
                    target.width(),
                    target.height() - selection.bottom() - 1,
                ),
                dim,
            )
            painter.fillRect(QRect(0, selection.top(), selection.left(), selection.height()), dim)
            painter.fillRect(
                QRect(
                    selection.right() + 1,
                    selection.top(),
                    target.width() - selection.right() - 1,
                    selection.height(),
                ),
                dim,
            )

            painter.setPen(QPen(SELECTION_COLOR, 1))
            painter.drawRect(selection.adjusted(0, 0, -1, -1))
            self._draw_size_label(painter, selection)
        elif self._hovered is not None:
            # Объект под курсором: яркость его области восстанавливается,
            # границы обводятся пунктиром.
            self._dim_around(painter, target, self._hovered)
            pen = QPen(SELECTION_COLOR, 2)
            pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.drawRect(self._hovered.adjusted(1, 1, -2, -2))
            self._draw_size_label(painter, self._hovered)
        else:
            painter.fillRect(target, DIM_COLOR)
            self._draw_hint(painter)
        painter.end()

    @staticmethod
    def _dim_around(painter: QPainter, target: QRect, area: QRect) -> None:
        """
        Затемнение экрана вокруг указанной области.

        Заливаются четыре полосы по краям: заливка всего экрана с
        последующим восстановлением яркости потребовала бы двух проходов
        по всем точкам вместо одного.
        """
        painter.fillRect(QRect(0, 0, target.width(), area.top()), DIM_COLOR)
        painter.fillRect(
            QRect(0, area.bottom() + 1, target.width(), target.height() - area.bottom() - 1),
            DIM_COLOR,
        )
        painter.fillRect(QRect(0, area.top(), area.left(), area.height()), DIM_COLOR)
        painter.fillRect(
            QRect(area.right() + 1, area.top(), target.width() - area.right() - 1, area.height()),
            DIM_COLOR,
        )

    def _draw_size_label(self, painter: QPainter, selection: QRect) -> None:
        """Подпись с размерами области рядом с рамкой выделения."""
        text = f"{selection.width()} × {selection.height()}"
        font = QFont()
        font.setPointSize(10)
        painter.setFont(font)

        metrics = painter.fontMetrics()
        width = metrics.horizontalAdvance(text) + 12
        height = metrics.height() + 6
        # Подпись размещается над областью, а при нехватке места - под ней.
        top = selection.top() - height - 4
        if top < 0:
            top = selection.bottom() + 4
        box = QRect(selection.left(), top, width, height)

        painter.fillRect(box, QColor(0, 0, 0, 190))
        painter.setPen(QColor(255, 255, 255))
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, text)

    def _draw_hint(self, painter: QPainter) -> None:
        """Подсказка по управлению в центре основного монитора."""
        screen = QGuiApplication.primaryScreen()
        area = screen.geometry() if screen is not None else self.rect()
        # Координаты приводятся к системе координат окна.
        local = area.translated(-self._virtual_rect.topLeft())

        font = QFont()
        font.setPointSize(12)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        width = metrics.horizontalAdvance(self._hint) + 24
        height = metrics.height() + 16
        box = QRect(
            local.center().x() - width // 2,
            local.center().y() - height // 2,
            width,
            height,
        )
        painter.fillRect(box, QColor(0, 0, 0, 200))
        painter.setPen(QColor(235, 235, 235))
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, self._hint)

    # -------------------------------------------------------------- события

    def _selection_rect(self) -> QRect:
        """Текущий прямоугольник выделения в координатах окна."""
        return QRect(self._origin, self._current).normalized()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        """Начало выделения либо отмена по правой кнопке."""
        if event.button() == Qt.MouseButton.RightButton:
            self._finish(None)
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self._selecting = True
            self._origin = event.position().toPoint()
            self._current = self._origin
            self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        """Изменение размеров области либо поиск объекта под курсором."""
        if not self._selecting:
            self._update_hovered(event.position().toPoint())
            return
        previous = self._selection_rect()
        self._current = event.position().toPoint()
        # Перерисовывается только затронутая часть окна: прежняя и новая
        # области вместе с запасом под рамку и подпись размера.
        region = previous.united(self._selection_rect())
        self.update(region.adjusted(-80, -40, 80, 40))

    def _update_hovered(self, position: QPoint) -> None:
        """Обновление границ объекта под курсором."""
        if not self._objects:
            return
        # Координаты окна приводятся к координатам рабочего стола: снимок
        # объектов собран именно в них.
        found = object_at(self._objects, position + self._virtual_rect.topLeft())
        hovered = found.translated(-self._virtual_rect.topLeft()) if found else None
        if hovered == self._hovered:
            return

        previous = self._hovered
        self._hovered = hovered
        # Перерисовывается объединение прежней и новой областей с запасом
        # под рамку и подпись размера.
        region = QRect()
        for part in (previous, hovered):
            if part is not None:
                region = QRect(part) if region.isNull() else region.united(part)
        if region.isNull():
            self.update()
        else:
            self.update(region.adjusted(-80, -40, 80, 40))

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        """Завершение выделения."""
        if event.button() != Qt.MouseButton.LeftButton or not self._selecting:
            return
        self._selecting = False
        selection = self._selection_rect()
        if selection.width() < MINIMUM_SIZE or selection.height() < MINIMUM_SIZE:
            # Нажатие без протягивания: снимается объект под курсором,
            # если он определён. Собственноручное выделение имеет
            # преимущество, поэтому проверка выполняется только здесь.
            moved = (event.position().toPoint() - self._origin).manhattanLength()
            if moved <= CLICK_TOLERANCE and self._hovered is not None:
                self._finish(QRect(self._hovered))
                return
            # Щелчок мимо объекта: выделение сбрасывается, оверлей
            # остаётся открытым.
            self._origin = QPoint()
            self._current = QPoint()
            self._hovered = None
            self.update()
            return
        self._finish(selection)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        """Обработка клавиш отмены и выбора всего экрана."""
        if event.key() == Qt.Key.Key_Escape:
            self._finish(None)
        elif event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            # Выбор всей видимой области рабочего стола.
            self._finish(QRect(QPoint(0, 0), self.size()))

    def _finish(self, selection: QRect | None) -> None:
        """Завершение работы оверлея с отправкой результата."""
        if self._finished:
            return
        self._finished = True
        self.releaseKeyboard()
        self.hide()

        if selection is None:
            self.cancelled.emit()
        else:
            # Координаты окна переводятся в координаты рабочего стола.
            self.selected.emit(selection.translated(self._virtual_rect.topLeft()))
        self.deleteLater()
