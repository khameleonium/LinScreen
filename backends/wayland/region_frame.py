"""
Рамка вокруг записываемой области.

Во время записи пользователю важно видеть, что именно попадает в кадр.
Рамка рисуется **снаружи** указанной области: внутрь она не заходит и
потому сама в запись не попадает. Окно прозрачно для мыши и не мешает
работе с окнами под ним.

В Wayland приложение не может само поставить окно в нужное место и
удержать его поверх прочих. Размещение выполняет композитор по просьбе
приложения (в KDE - скриптом KWin, см. backends/wayland/kwin.py), а окно находится
по уникальному заголовку. До подтверждения размещения рамка не рисуется,
чтобы не мелькнуть там, куда композитор поставил окно по умолчанию.

Цвет отражает состояние: красный при записи, жёлтый на паузе. Зелёный
цвет применяется оверлеем выделения при подготовке, см. ui/overlay.py.
"""

from __future__ import annotations

import secrets

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QPaintEvent, QPainter, QPen
from PySide6.QtWidgets import QWidget

from ui.region_frame import BORDER_WIDTH, PAUSED_COLOR, RECORDING_COLOR  # noqa: F401


class RegionFrame(QWidget):
    """Прямоугольная рамка, обводящая записываемую область снаружи."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._color = RECORDING_COLOR
        # Признак подтверждённого композитором размещения.
        self._placed = False
        self._outer = QRect()

        # Окно не принимает ввод и не забирает фокус: оно лишь показывает
        # границы области и не должно мешать работе.
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.WindowDoesNotAcceptFocus
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        # Заголовок уникален: по нему композитор находит окно среди прочих.
        self.setWindowTitle("LinScreen frame " + secrets.token_hex(4))

    @property
    def caption(self) -> str:
        """Заголовок окна, по которому его находит композитор."""
        return self.windowTitle()

    @property
    def outer_rect(self) -> QRect:
        """Геометрия окна рамки в логических координатах рабочего стола."""
        return QRect(self._outer)

    def show_for(self, area: QRect, color: QColor = RECORDING_COLOR) -> None:
        """
        Показ рамки вокруг указанной области.

        Окно шире области на толщину рамки со всех сторон, середина
        остаётся прозрачной. Положение окна задаёт композитор после
        вызова set_placed().
        """
        self._color = color
        self._placed = False
        self._outer = area.adjusted(-BORDER_WIDTH, -BORDER_WIDTH, BORDER_WIDTH, BORDER_WIDTH)
        self.resize(self._outer.size())
        self.show()
        self.update()

    def set_placed(self, placed: bool) -> None:
        """Подтверждение размещения окна композитором."""
        self._placed = placed
        if placed:
            self.update()
        else:
            # Разместить окно не удалось: рамка в случайном месте вводила
            # бы в заблуждение, поэтому она скрывается.
            self.hide()

    def set_color(self, color: QColor) -> None:
        """Смена цвета рамки без изменения её положения."""
        if color == self._color:
            return
        self._color = color
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 - имя из Qt
        """Отрисовка рамки по краю окна; середина остаётся прозрачной."""
        painter = QPainter(self)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        painter.fillRect(self.rect(), Qt.GlobalColor.transparent)
        if self._placed:
            pen = QPen(self._color, BORDER_WIDTH)
            pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
            painter.setPen(pen)
            half = BORDER_WIDTH / 2
            painter.drawRect(self.rect().toRectF().adjusted(half, half, -half, -half))
        painter.end()


# ===========================================================================
# Рамка через XWayland для композиторов без размещения окон
# ===========================================================================

# Ключ командной строки вспомогательного процесса рамки.
HELPER_ARGUMENT = "--frame-helper"


def run_frame_helper(arguments: list[str]) -> int:
    """
    Вспомогательный процесс рамки (запускается с ключом --frame-helper).

    Протокол Wayland не позволяет поставить окно в заданное место и
    удержать его поверх прочих, а GNOME не даёт для этого и собственных
    средств. Окно X11 без участия менеджера окон (override-redirect) в
    XWayland размещается точно по координатам и держится поверх обычных
    окон. Поэтому только рамка - и только там, где композитор сам не
    умеет размещать окна, - рисуется отдельным процессом через XWayland.

    Аргументы: x y ширина высота. Команды читаются из стандартного ввода
    построчно: "color R G B" меняет цвет, закрытие ввода завершает процесс.
    """
    import os
    import sys
    import threading

    os.environ["QT_QPA_PLATFORM"] = "xcb"
    from PySide6.QtCore import QObject, Signal
    from PySide6.QtWidgets import QApplication

    try:
        x, y, width, height = (int(value) for value in arguments[:4])
    except ValueError:
        return 2

    application = QApplication(sys.argv[:1])
    frame = RegionFrame()
    frame.setWindowFlag(Qt.WindowType.X11BypassWindowManagerHint, True)
    frame.show_for(QRect(x, y, width, height))
    frame.move(x - BORDER_WIDTH, y - BORDER_WIDTH)
    frame.set_placed(True)

    class Commands(QObject):
        """Доставка команд из потока чтения в поток интерфейса."""

        color = Signal(int, int, int)
        closed = Signal()

    commands = Commands()
    commands.color.connect(lambda r, g, b: frame.set_color(QColor(r, g, b)))
    commands.closed.connect(application.quit)

    def read() -> None:
        """Чтение команд до закрытия стандартного ввода."""
        for line in sys.stdin:
            parts = line.split()
            if len(parts) == 4 and parts[0] == "color":
                try:
                    commands.color.emit(int(parts[1]), int(parts[2]), int(parts[3]))
                except ValueError:
                    continue
        commands.closed.emit()

    threading.Thread(target=read, daemon=True).start()
    return application.exec()


class XWaylandFrame:
    """
    Рамка записи во вспомогательном процессе XWayland.

    Предоставляет тот же набор действий, что и RegionFrame, и применяется
    вместо неё, когда композитор не размещает окна по просьбе приложения.
    """

    def __init__(self) -> None:
        from PySide6.QtCore import QProcess

        self._process: QProcess | None = None
        self._color = RECORDING_COLOR

    @staticmethod
    def is_available() -> bool:
        """Признак работающего XWayland в сеансе."""
        import os

        return bool(os.environ.get("DISPLAY"))

    def isVisible(self) -> bool:  # noqa: N802 - совпадает с интерфейсом QWidget
        """Признак показанной рамки."""
        return self._process is not None

    def show_for(self, area: QRect, color: QColor = RECORDING_COLOR) -> None:
        """Запуск процесса рамки вокруг области."""
        import sys

        from PySide6.QtCore import QProcess

        from core.runtime import child_environment, is_frozen, project_root

        self.hide()
        self._color = color
        process = QProcess()
        environment = process.processEnvironment()
        for name, value in child_environment().items():
            environment.insert(name, value)
        # Для собранного приложения исходные пути библиотек не
        # восстанавливаются: процесс рамки - это оно же.
        if is_frozen():
            import os

            for name in ("LD_LIBRARY_PATH", "QT_PLUGIN_PATH"):
                if os.environ.get(name):
                    environment.insert(name, os.environ[name])
        process.setProcessEnvironment(environment)
        geometry = [str(area.x()), str(area.y()), str(area.width()), str(area.height())]
        if is_frozen():
            process.setProgram(sys.executable)
            process.setArguments([HELPER_ARGUMENT, *geometry])
        else:
            process.setProgram(sys.executable)
            process.setArguments([str(project_root() / "main.py"), HELPER_ARGUMENT, *geometry])
        process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        process.start()
        self._process = process
        self._send_color()

    def set_color(self, color: QColor) -> None:
        """Смена цвета рамки."""
        self._color = color
        self._send_color()

    def diagnostics(self) -> str:
        """Состояние процесса рамки и его вывод - для журнала."""
        process = self._process
        if process is None:
            return "not started"
        output = bytes(process.readAll().data()).decode("utf-8", "replace").strip()
        return f"state={process.state().name} error={process.error().name} {output}"

    def _send_color(self) -> None:
        """Передача цвета процессу рамки."""
        if self._process is not None:
            color = self._color
            self._process.write(f"color {color.red()} {color.green()} {color.blue()}\n".encode())

    def hide(self) -> None:
        """Завершение процесса рамки закрытием его стандартного ввода."""
        process, self._process = self._process, None
        if process is not None:
            process.closeWriteChannel()
            if not process.waitForFinished(1000):
                process.kill()
                process.waitForFinished(1000)
