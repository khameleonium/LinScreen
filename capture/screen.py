"""
Общие сведения о захвате экрана: режимы, мониторы, вырезание областей.

Способ получения снимка и видеопотока зависит от графической системы и
реализован в наборах модулей backends/x11 и backends/wayland. Здесь -
то, что одинаково для обеих: геометрия мониторов в логических
координатах и вырезание области из снимка рабочего стола.

Снимок делается до показа оверлея выделения: пользователь выбирает
область на замороженном изображении, поэтому сам оверлей в кадр не
попадает, а содержимое экрана не меняется между выделением и сохранением.
"""

from __future__ import annotations

from core.i18n import tr

from dataclasses import dataclass
from enum import Enum

from PySide6.QtCore import QRect
from PySide6.QtGui import QGuiApplication, QImage, QScreen


class CaptureMode(str, Enum):
    """Режимы выбора снимаемой области."""

    REGION = "region"
    FULLSCREEN = "fullscreen"
    WINDOW = "window"
    MONITOR = "monitor"

    @property
    def label(self) -> str:
        """Название режима для меню трея."""
        return {
            CaptureMode.REGION: tr("Выделенная область"),
            CaptureMode.FULLSCREEN: tr("Весь экран"),
            CaptureMode.WINDOW: tr("Объект под курсором"),
            CaptureMode.MONITOR: tr("Отдельный монитор"),
        }[self]


class CaptureBackendError(RuntimeError):
    """Снимок экрана получить не удалось."""


class CaptureCancelled(RuntimeError):
    """Пользователь отказался от снимка в диалоге портала."""


@dataclass(frozen=True)
class MonitorInfo:
    """Сведения об отдельном мониторе."""

    name: str
    geometry: QRect
    is_primary: bool

    @property
    def title(self) -> str:
        """Подпись монитора для меню выбора."""
        size = f"{self.geometry.width()}×{self.geometry.height()}"
        suffix = tr(" (основной)") if self.is_primary else ""
        return f"{self.name} — {size}{suffix}"


# ===========================================================================
# Геометрия рабочего стола
# ===========================================================================


def list_monitors() -> list[MonitorInfo]:
    """Перечень подключённых мониторов с их логической геометрией."""
    primary = QGuiApplication.primaryScreen()
    monitors: list[MonitorInfo] = []
    for screen in QGuiApplication.screens():
        monitors.append(
            MonitorInfo(
                name=screen.name() or tr("Экран"),
                geometry=screen.geometry(),
                is_primary=screen is primary,
            )
        )
    return monitors


def virtual_geometry() -> QRect:
    """
    Объединённая геометрия всех мониторов.

    Композитор сообщает положение каждого монитора в общем пространстве
    координат, поэтому начало может быть отрицательным, если
    дополнительный монитор расположен левее или выше основного.
    """
    region = QRect()
    for screen in QGuiApplication.screens():
        region = region.united(screen.geometry())
    return region


def screen_for_rect(rect: QRect) -> QScreen | None:
    """
    Монитор, на который приходится большая часть области.

    Портал выдаёт видеопоток одного монитора, поэтому запись области,
    растянутой на два экрана, ведётся по тому, где её больше.
    """
    best: QScreen | None = None
    best_area = -1
    for screen in QGuiApplication.screens():
        common = screen.geometry().intersected(rect)
        area = common.width() * common.height() if not common.isEmpty() else 0
        if area > best_area:
            best, best_area = screen, area
    return best or QGuiApplication.primaryScreen()


def desktop_scale(desktop: QImage, virtual_rect: QRect | None = None) -> float:
    """
    Отношение физических точек снимка к логическим координатам Qt.

    Коэффициент вычисляется по фактическому размеру снимка, а не по
    настройкам экранов: портал может вернуть изображение в любом
    масштабе, и только такой расчёт гарантирует точное совпадение.
    """
    area = virtual_rect if virtual_rect is not None else virtual_geometry()
    if area.width() <= 0:
        return 1.0
    return desktop.width() / area.width()


def crop_desktop_image(desktop: QImage, rect: QRect) -> QImage:
    """Вырезание области из снимка рабочего стола по логическим координатам."""
    area = virtual_geometry()
    scale = desktop_scale(desktop, area)
    # Координаты приводятся к началу снимка и к его точкам.
    local = QRect(
        round((rect.x() - area.x()) * scale),
        round((rect.y() - area.y()) * scale),
        round(rect.width() * scale),
        round(rect.height() * scale),
    )
    # Область ограничивается границами снимка: геометрия окна может выходить
    # за пределы экрана, и без ограничения в кадр попали бы пустые поля.
    bounded = local.intersected(desktop.rect())
    if bounded.isEmpty():
        return desktop.copy()
    return desktop.copy(bounded)
