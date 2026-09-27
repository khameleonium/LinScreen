"""
Объекты интерфейса под указателем мыши: общая часть.

Перечень окон собирает набор модулей графической системы: в X11 - обход
дерева окон (backends/x11/windows.py), в Wayland - скрипт KWin в KDE
Plasma (backends/wayland/windows.py). Здесь - представление объекта и
поиск объекта под точкой, одинаковые для обеих систем.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QPoint, QRect

# Наименьший размер объекта, попадающего в перечень. Отсекает служебные
# окна нулевого размера и точечные элементы, выделять которые бессмысленно.
MINIMUM_SIZE = 24


@dataclass(frozen=True)
class InterfaceObject:
    """Прямоугольник объекта интерфейса в координатах экрана."""

    rect: QRect
    # Глубина вложенности: ноль соответствует окну верхнего уровня.
    depth: int = 0
    # Порядок наложения окон верхнего уровня (Z-порядок): выше значение — ближе к зрителю.
    group: int = 0

    @property
    def area(self) -> int:
        """Площадь объекта, применяется при сравнении совпадений."""
        return self.rect.width() * self.rect.height()


def object_at(objects: list[InterfaceObject], point: QPoint) -> QRect | None:
    """
    Границы объекта под указанной точкой.

    Среди окон верхнего уровня выбирается самое верхнее в порядке наложения
    (наибольший group), накрывающее указанную точку. Окна, лежащие ниже в
    стеке, считаются перекрытыми и исключаются из рассмотрения.

    Внутри выбранной группы выбирается объект с наименьшей площадью
    (наиболее специфичный). При равной площади предпочитается объект с
    большей глубиной вложенности либо отрисованный позже.
    """
    matching: list[InterfaceObject] = [item for item in objects if item.rect.contains(point)]
    if not matching:
        return None

    topmost_group = max(item.group for item in matching)
    candidates = [item for item in matching if item.group == topmost_group]

    best: InterfaceObject | None = None
    for item in candidates:
        if (
            best is None
            or item.area < best.area
            or (item.area == best.area and item.depth >= best.depth)
        ):
            best = item
    return QRect(best.rect) if best is not None else None
