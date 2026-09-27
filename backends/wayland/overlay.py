"""
Полноэкранный оверлей выделения области.

Оверлей показывается поверх замороженного снимка рабочего стола: содержимое
экрана снимается до его появления, поэтому сам оверлей в кадр не попадает,
а картинка не меняется между выделением и сохранением.

В Wayland приложение не может расположить окно в произвольных координатах
и растянуть его на несколько мониторов. Поэтому на каждом мониторе
открывается отдельное полноэкранное окно (поверхность), а общее состояние
выделения хранит управляющий объект в логических координатах рабочего
стола. Протягивание через границу мониторов работает: пока кнопка мыши
нажата, композитор продолжает доставлять события окну, где началось
нажатие, в том числе с координатами за его пределами.
"""

from __future__ import annotations

from core.i18n import tr

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, Qt, Signal
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
    QScreen,
    QWheelEvent,
)
from PySide6.QtWidgets import QWidget

from capture.screen import crop_desktop_image
from capture.windows import InterfaceObject, object_at
from ui.magnifier import Magnifier, MagnifierOptions

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


class RegionOverlay(QObject):
    """Выбор прямоугольной области на замороженном снимке всех мониторов."""

    # Область выбрана: передаются логические координаты рабочего стола.
    selected = Signal(QRect)
    # Выбор отменён клавишей Esc или правой кнопкой мыши.
    cancelled = Signal()
    # Радиус лупы изменён колесом мыши: передаётся новое значение.
    magnifierRadiusChanged = Signal(int)

    def __init__(
        self,
        desktop: QImage,
        virtual_rect: QRect,
        hint: str = tr("Выделите область: ЛКМ — выбор, Esc — отмена, Enter — весь экран"),
        objects: list[InterfaceObject] | None = None,
        parent: QObject | None = None,
        magnifier: MagnifierOptions | None = None,
    ) -> None:
        super().__init__(parent)
        # Лупа у указателя и последнее известное положение указателя в
        # координатах рабочего стола. До первого движения мыши положение
        # неизвестно: Wayland не сообщает его приложению заранее.
        self._magnifier = Magnifier(desktop, virtual_rect, magnifier or MagnifierOptions())
        self._cursor: QPoint | None = None
        self._desktop = desktop
        self._virtual_rect = virtual_rect
        self._hint = hint
        # Снимок объектов интерфейса, собранный до показа оверлея.
        self._objects = objects or []
        # Границы объекта под курсором в координатах рабочего стола.
        self._hovered: QRect | None = None
        self._origin = QPoint()
        self._current = QPoint()
        self._selecting = False
        self._finished = False
        self._surfaces: list[_OverlaySurface] = [
            _OverlaySurface(self, screen) for screen in QGuiApplication.screens()
        ]

    # ------------------------------------------------------------ свойства

    @property
    def desktop(self) -> QImage:
        """Замороженный снимок рабочего стола."""
        return self._desktop

    @property
    def hint(self) -> str:
        """Текст подсказки по управлению."""
        return self._hint

    @property
    def hovered(self) -> QRect | None:
        """Объект под курсором в координатах рабочего стола."""
        return self._hovered

    @property
    def is_selecting(self) -> bool:
        """Признак идущего протягивания."""
        return self._selecting

    @property
    def magnifier(self) -> Magnifier:
        """Лупа у указателя."""
        return self._magnifier

    @property
    def cursor(self) -> QPoint | None:
        """Положение указателя в координатах рабочего стола."""
        return self._cursor

    def selection_rect(self) -> QRect:
        """Текущий прямоугольник выделения в координатах рабочего стола."""
        return QRect(self._origin, self._current).normalized()

    def set_objects(self, objects: list[InterfaceObject]) -> None:
        """
        Передача перечня объектов интерфейса после его сбора.

        Сбор выполняется асинхронно, поэтому оверлей может быть показан
        раньше, чем перечень готов.
        """
        self._objects = objects

    # ------------------------------------------------------------ показ

    def show_overlay(self) -> None:
        """Показ поверхностей на всех мониторах."""
        primary = QGuiApplication.primaryScreen()
        focused: _OverlaySurface | None = None
        for surface in self._surfaces:
            surface.show_on_screen()
            if surface.screen_ref is primary:
                focused = surface
        target = focused or (self._surfaces[0] if self._surfaces else None)
        if target is not None:
            # Фокус клавиатуры нужен для клавиш Esc и Enter. В Wayland
            # приложение лишь просит о нём, решение принимает композитор.
            target.raise_()
            target.activateWindow()

    def _update_all(self) -> None:
        """Перерисовка всех поверхностей."""
        for surface in self._surfaces:
            surface.update()

    def _update_areas(self, areas: list[QRect]) -> None:
        """Перерисовка участков рабочего стола на затронутых поверхностях."""
        for surface in self._surfaces:
            surface.update_area(areas)

    def cursor_bounds(self) -> QRect:
        """Границы монитора под указателем: лупа не выходит за них."""
        if self._cursor is not None:
            for surface in self._surfaces:
                if surface.desktop_rect.contains(self._cursor):
                    return surface.desktop_rect
        return QRect(self._virtual_rect)

    def _lens_area(self) -> QRect | None:
        """Участок, занимаемый лупой, либо пустое значение."""
        if not self._magnifier.enabled or self._cursor is None:
            return None
        return self._magnifier.area(self._cursor, self.cursor_bounds())

    def _move_cursor(self, position: QPoint | None) -> None:
        """Смена положения указателя с перерисовкой прежней и новой лупы."""
        previous = self._lens_area()
        self._cursor = position
        current = self._lens_area()
        self._update_areas([area for area in (previous, current) if area is not None])

    def wheel(self, delta: int) -> None:
        """Изменение радиуса лупы колесом мыши."""
        if not self._magnifier.enabled or delta == 0:
            return
        previous = self._lens_area()
        # Один щелчок колеса даёт 120 единиц; сенсорная панель - меньше за
        # событие, но хотя бы один шаг засчитывается.
        steps = delta // 120 if abs(delta) >= 120 else (1 if delta > 0 else -1)
        if self._magnifier.adjust_radius(steps):
            current = self._lens_area()
            self._update_areas([area for area in (previous, current) if area is not None])
            self.magnifierRadiusChanged.emit(self._magnifier.radius)

    def leave(self) -> None:
        """Указатель покинул поверхность: лупа скрывается до его возвращения."""
        self._move_cursor(None)

    # ------------------------------------------------------------ события

    def press(self, position: QPoint, button: Qt.MouseButton) -> None:
        """Нажатие кнопки мыши в координатах рабочего стола."""
        if button == Qt.MouseButton.RightButton:
            self._finish(None)
            return
        if button == Qt.MouseButton.LeftButton:
            self._selecting = True
            self._origin = position
            self._current = position
            self._update_all()

    def move(self, position: QPoint) -> None:
        """Перемещение указателя в координатах рабочего стола."""
        self._move_cursor(position)
        if not self._selecting:
            self._update_hovered(position)
            return
        self._current = position
        self._update_all()

    def release(self, position: QPoint, button: Qt.MouseButton) -> None:
        """Отпускание кнопки мыши."""
        if button != Qt.MouseButton.LeftButton or not self._selecting:
            return
        self._selecting = False
        self._current = position
        selection = self.selection_rect()
        if selection.width() < MINIMUM_SIZE or selection.height() < MINIMUM_SIZE:
            # Нажатие без протягивания: снимается объект под курсором,
            # если он определён. Собственноручное выделение имеет
            # преимущество, поэтому проверка выполняется только здесь.
            moved = (position - self._origin).manhattanLength()
            if moved <= CLICK_TOLERANCE and self._hovered is not None:
                self._finish(QRect(self._hovered))
                return
            # Щелчок мимо объекта: выделение сбрасывается, оверлей
            # остаётся открытым.
            self._origin = QPoint()
            self._current = QPoint()
            self._hovered = None
            self._update_all()
            return
        self._finish(selection)

    def key(self, key: int, screen: QScreen) -> None:
        """Обработка клавиш отмены и выбора экрана целиком."""
        if key == Qt.Key.Key_Escape:
            self._finish(None)
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            # Выбирается монитор под указателем, а если указатель ещё не
            # двигался - монитор, окно которого получило фокус. Фокус в
            # Wayland назначает композитор, поэтому указатель надёжнее.
            # Снимок всех мониторов сразу доступен отдельным пунктом меню.
            if self._cursor is not None:
                self._finish(self.cursor_bounds())
            else:
                self._finish(screen.geometry())

    def _update_hovered(self, position: QPoint) -> None:
        """Обновление границ объекта под курсором."""
        if not self._objects:
            return
        found = object_at(self._objects, position)
        if found == self._hovered:
            return
        self._hovered = found
        self._update_all()

    def _finish(self, selection: QRect | None) -> None:
        """Завершение работы оверлея с отправкой результата."""
        if self._finished:
            return
        self._finished = True
        for surface in self._surfaces:
            surface.hide()
            surface.deleteLater()
        self._surfaces = []

        if selection is None:
            self.cancelled.emit()
        else:
            self.selected.emit(selection)
        self.deleteLater()


class _OverlaySurface(QWidget):
    """Полноэкранное окно оверлея на одном мониторе."""

    def __init__(self, owner: RegionOverlay, screen: QScreen) -> None:
        super().__init__(None)
        self._owner = owner
        self._screen = screen
        self._geometry = screen.geometry()
        # Подготовленное изображение монитора. Готовится однократно:
        # масштабирование на каждую перерисовку заметно замедляло бы
        # выделение на слабом оборудовании и при больших разрешениях.
        self._background = QPixmap()

        # Окно без рамки. Признаки обхода менеджера окон и удержания поверх
        # прочих в Wayland не действуют: перекрытие панелей обеспечивает
        # полноэкранный режим.
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self.setWindowTitle(tr("Выделение области"))
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

    @property
    def screen_ref(self) -> QScreen:
        """Монитор, которому принадлежит поверхность."""
        return self._screen

    @property
    def desktop_rect(self) -> QRect:
        """Геометрия поверхности в координатах рабочего стола."""
        return QRect(self._geometry)

    def update_area(self, areas: list[QRect]) -> None:
        """Перерисовка участков рабочего стола, приходящихся на поверхность."""
        for area in areas:
            common = area.intersected(self._geometry)
            if not common.isEmpty():
                self.update(self._to_local(common))

    def show_on_screen(self) -> None:
        """Показ поверхности на своём мониторе во весь экран."""
        self._prepare_background()
        # Привязка к монитору выполняется до показа: полноэкранное окно
        # композитор размещает на мониторе, указанном при создании.
        self.setScreen(self._screen)
        self.setGeometry(self._geometry)
        self.showFullScreen()

    def _prepare_background(self) -> None:
        """Вырезание части снимка, приходящейся на монитор."""
        if not self._background.isNull():
            return
        part = crop_desktop_image(self._owner.desktop, self._geometry)
        pixmap = QPixmap.fromImage(part)
        if self._geometry.width() > 0:
            # Коэффициент задаётся по фактическому размеру: снимок хранится
            # в физических точках, и отрисовка остаётся резкой при любом
            # масштабе экрана.
            pixmap.setDevicePixelRatio(part.width() / self._geometry.width())
        self._background = pixmap

    def _to_local(self, rect: QRect) -> QRect:
        """Перевод прямоугольника рабочего стола в координаты окна."""
        return rect.translated(-self._geometry.topLeft())

    def _to_global(self, event: QMouseEvent) -> QPoint:
        """Координаты события в системе координат рабочего стола."""
        return event.position().toPoint() + self._geometry.topLeft()

    # ------------------------------------------------------------ отрисовка

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 - имя из Qt
        """Отрисовка снимка, затемнения и текущей рамки выделения."""
        self._prepare_background()
        painter = QPainter(self)
        target = self.rect()
        painter.drawPixmap(0, 0, self._background)

        owner = self._owner
        selection = self._to_local(owner.selection_rect())
        hovered = owner.hovered

        # Рамка показывается только во время протягивания: до первого
        # нажатия прямоугольник вырожден и вместо него выводится подсказка.
        if owner.is_selecting and selection.width() > 1 and selection.height() > 1:
            _dim_around(painter, target, selection)
            painter.setPen(QPen(SELECTION_COLOR, 1))
            painter.drawRect(selection.adjusted(0, 0, -1, -1))
            _draw_size_label(painter, selection)
        elif hovered is not None and not owner.is_selecting:
            local = self._to_local(hovered)
            # Объект под курсором: яркость его области сохраняется,
            # границы обводятся пунктиром.
            _dim_around(painter, target, local)
            pen = QPen(SELECTION_COLOR, 2)
            pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.drawRect(local.adjusted(1, 1, -2, -2))
            _draw_size_label(painter, local)
        else:
            painter.fillRect(target, DIM_COLOR)
            if self._screen is QGuiApplication.primaryScreen() or len(
                QGuiApplication.screens()
            ) == 1:
                _draw_hint(painter, target, owner.hint)

        # Лупа рисуется последней, поверх рамки и подписи размера, и только
        # на мониторе, где находится указатель.
        cursor = owner.cursor
        if cursor is not None and self._geometry.contains(cursor):
            owner.magnifier.paint(
                painter, cursor, owner.cursor_bounds(), self._geometry.topLeft()
            )
        painter.end()

    # ------------------------------------------------------------ события

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        """Передача нажатия управляющему объекту."""
        self._owner.press(self._to_global(event), event.button())

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        """Передача перемещения управляющему объекту."""
        self._owner.move(self._to_global(event))

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        """Передача отпускания управляющему объекту."""
        self._owner.release(self._to_global(event), event.button())

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        """Передача нажатия клавиши управляющему объекту."""
        self._owner.key(event.key(), self._screen)

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        """Изменение радиуса лупы колесом мыши."""
        self._owner.wheel(event.angleDelta().y())

    def leaveEvent(self, event: QEvent) -> None:  # noqa: N802
        """Скрытие лупы при уходе указателя с поверхности."""
        # Во время протягивания указатель может выйти за пределы окна, но
        # события продолжают поступать сюда же - лупа при этом не гаснет.
        if not self._owner.is_selecting:
            self._owner.leave()


# ===========================================================================
# Отрисовка
# ===========================================================================


def _dim_around(painter: QPainter, target: QRect, area: QRect) -> None:
    """
    Затемнение экрана вокруг указанной области.

    Заливаются четыре полосы по краям: заливка всего экрана с последующим
    восстановлением яркости потребовала бы двух проходов по всем точкам
    вместо одного.
    """
    area = area.intersected(target)
    if area.isEmpty():
        painter.fillRect(target, DIM_COLOR)
        return
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


def _draw_size_label(painter: QPainter, selection: QRect) -> None:
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


def _draw_hint(painter: QPainter, area: QRect, hint: str) -> None:
    """Подсказка по управлению в центре монитора."""
    font = QFont()
    font.setPointSize(12)
    painter.setFont(font)
    metrics = painter.fontMetrics()
    width = metrics.horizontalAdvance(hint) + 24
    height = metrics.height() + 16
    box = QRect(
        area.center().x() - width // 2,
        area.center().y() - height // 2,
        width,
        height,
    )
    painter.fillRect(box, QColor(0, 0, 0, 200))
    painter.setPen(QColor(235, 235, 235))
    painter.drawText(box, Qt.AlignmentFlag.AlignCenter, hint)
