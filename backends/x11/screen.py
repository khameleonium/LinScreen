"""
Захват содержимого экрана и подготовка видеовходов FFmpeg (X11).

Модуль решает две задачи:
    * получение растрового снимка экрана, монитора, области или активного
      окна средствами Qt либо внешних утилит;
    * сборка аргументов входа FFmpeg для записи выбранной области.

Снимок всего виртуального рабочего стола делается до показа оверлея
выделения: пользователь выбирает область на замороженном изображении,
поэтому сам оверлей в кадр не попадает, а содержимое экрана не успевает
измениться между выделением и сохранением.
"""

from __future__ import annotations

from core.i18n import tr

import os
import shutil
import subprocess
from PySide6.QtCore import QPoint, QRect
from PySide6.QtGui import QGuiApplication, QImage, QPainter, QScreen

from capture.screen import CaptureBackendError, crop_desktop_image, virtual_geometry
from encoder.profiles import VideoInput

# Размер очереди пакетов видеовхода: при просадке диска кадры не теряются.
VIDEO_QUEUE_SIZE = "1024"


# ===========================================================================
# Геометрия в физических точках
# ===========================================================================


def device_pixel_ratio() -> float:
    """
    Наибольший коэффициент масштабирования среди мониторов.

    Координаты Qt логические, а внешние утилиты и FFmpeg работают с
    физическими пикселями, поэтому коэффициент требуется для пересчёта.
    """
    ratios = [screen.devicePixelRatio() for screen in QGuiApplication.screens()]
    return max(ratios) if ratios else 1.0


def to_device_rect(rect: QRect, ratio: float | None = None) -> QRect:
    """Пересчёт прямоугольника из логических координат в физические."""
    scale = ratio if ratio is not None else device_pixel_ratio()
    if scale == 1.0:
        return QRect(rect)
    return QRect(
        int(rect.x() * scale),
        int(rect.y() * scale),
        int(rect.width() * scale),
        int(rect.height() * scale),
    )


# ===========================================================================
# Получение растрового снимка
# ===========================================================================


def grab_screen(screen: QScreen) -> QImage:
    """Снимок содержимого одного монитора."""
    # Нулевой идентификатор окна означает корневое окно, то есть весь экран.
    pixmap = screen.grabWindow(0)
    return pixmap.toImage()


def grab_virtual_desktop() -> QImage:
    """
    Снимок всего виртуального рабочего стола.

    Мониторы снимаются по отдельности и собираются в общее изображение:
    такой порядок корректно обрабатывает конфигурации с разным разрешением
    и разным масштабированием.
    """
    screens = QGuiApplication.screens()
    if not screens:
        raise CaptureBackendError(tr("Графические экраны не обнаружены"))

    if len(screens) == 1:
        # Единственный монитор: снимок экрана уже является снимком всего
        # рабочего стола. Сборка холста заняла бы ещё столько же памяти
        # и лишний проход отрисовки, что на слабом оборудовании заметно.
        return grab_screen(screens[0])

    origin = virtual_geometry().topLeft()
    ratio = device_pixel_ratio()
    canvas_rect = to_device_rect(virtual_geometry(), ratio)

    canvas = QImage(canvas_rect.size(), QImage.Format.Format_ARGB32_Premultiplied)
    # Фон заливается чёрным: в конфигурациях уступом часть холста
    # не перекрывается ни одним монитором.
    canvas.fill(0xFF000000)

    painter = QPainter(canvas)
    try:
        for screen in screens:
            image = grab_screen(screen)
            geometry = screen.geometry()
            target = QPoint(
                int((geometry.x() - origin.x()) * ratio),
                int((geometry.y() - origin.y()) * ratio),
            )
            painter.drawImage(target, image)
    finally:
        # Художник закрывается в любом случае: незакрытый объект блокирует
        # дальнейшие операции с изображением.
        painter.end()
    return canvas


def grab_rect(rect: QRect) -> QImage:
    """Снимок произвольной области по логическим координатам."""
    return crop_desktop_image(grab_virtual_desktop(), rect)


# ===========================================================================
# Определение активного окна
# ===========================================================================


def _active_window_rect_xlib() -> QRect | None:
    """Геометрия активного окна средствами протокола X11."""
    try:
        from Xlib import X, display as xdisplay  # локальный импорт: только для X11
    except ImportError:
        return None

    try:
        connection = xdisplay.Display()
        root = connection.screen().root
        # Идентификатор активного окна публикуется менеджером окон
        # в свойстве корневого окна согласно спецификации EWMH.
        active = root.get_full_property(
            connection.intern_atom("_NET_ACTIVE_WINDOW"), X.AnyPropertyType
        )
        if active is None or not active.value:
            return None

        window = connection.create_resource_object("window", active.value[0])
        geometry = window.get_geometry()
        # Координаты окна переводятся в систему координат корневого окна.
        absolute = window.translate_coords(root, 0, 0)
        x, y = absolute.x, absolute.y
        width, height = geometry.width, geometry.height

        # Рамка окна хранится отдельным свойством и в геометрию не входит.
        extents = window.get_full_property(
            connection.intern_atom("_NET_FRAME_EXTENTS"), X.AnyPropertyType
        )
        if extents is not None and len(extents.value) >= 4:
            left, right, top, bottom = extents.value[:4]
            x -= left
            y -= top
            width += left + right
            height += top + bottom

        connection.close()
        return QRect(x, y, width, height)
    except Exception:  # noqa: BLE001 - любая ошибка означает откат на другой способ
        return None


def _active_window_rect_xdotool() -> QRect | None:
    """Геометрия активного окна через утилиту xdotool."""
    if shutil.which("xdotool") is None:
        return None
    try:
        completed = subprocess.run(
            ["xdotool", "getactivewindow", "getwindowgeometry", "--shell"],
            capture_output=True,
            text=True,
            timeout=5,
            shell=False,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None

    # Вывод состоит из строк вида X=100, пригодных для прямого разбора.
    values: dict[str, int] = {}
    for line in completed.stdout.splitlines():
        key, _, value = line.partition("=")
        if value.strip().lstrip("-").isdigit():
            values[key.strip()] = int(value)
    if not {"X", "Y", "WIDTH", "HEIGHT"} <= values.keys():
        return None
    return QRect(values["X"], values["Y"], values["WIDTH"], values["HEIGHT"])


def active_window_rect() -> QRect | None:
    """
    Геометрия активного окна.

    Способы перебираются по убыванию надёжности; при неудаче возвращается
    пустое значение, и вызывающая сторона снимает экран целиком.
    """
    return _active_window_rect_xlib() or _active_window_rect_xdotool()


# ===========================================================================
# Подготовка видеовхода FFmpeg
# ===========================================================================


def build_video_input(
    rect: QRect,
    fps: int = 30,
    show_cursor: bool = True,
) -> VideoInput:
    """
    Сборка аргументов входа FFmpeg для записи указанной области.

    В сессии X11 применяется прямой захват корневого окна через x11grab.
    """
    ratio = device_pixel_ratio()
    device_rect = to_device_rect(rect, ratio)
    # Ширина и высота приводятся к чётным значениям: кодеки с
    # субдискретизацией 4:2:0 нечётный кадр не принимают, а обрезка
    # на входе дешевле последующего дополнения фильтром.
    width = max(2, device_rect.width() - device_rect.width() % 2)
    height = max(2, device_rect.height() - device_rect.height() % 2)

    display = os.environ.get("DISPLAY", ":0")

    return VideoInput(
        args=[
            "-f",
            "x11grab",
            "-framerate",
            str(fps),
            # Отрисовка указателя мыши выполняется самим захватчиком.
            "-draw_mouse",
            "1" if show_cursor else "0",
            "-thread_queue_size",
            VIDEO_QUEUE_SIZE,
            "-video_size",
            f"{width}x{height}",
            # Формат источника: дисплей и смещение начала области.
            "-i",
            f"{display}+{device_rect.x()},{device_rect.y()}",
        ],
        fps=fps,
        width=width,
        height=height,
        # Размер уже приведён к чётному, дополнение фильтром не требуется.
        needs_even_padding=False,
    )
