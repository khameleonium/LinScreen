"""
Определение границ объектов интерфейса под указателем мыши.

Модуль строит снимок дерева окон X11: для каждого видимого окна
вычисляются границы в координатах экрана. Снимок делается один раз перед
показом оверлея выделения, после чего поиск объекта под курсором ведётся
в памяти — обращаться к X-серверу на каждое перемещение мыши слишком
накладно.

Порядок записей повторяет порядок отрисовки: родитель предшествует своим
потомкам, а окна одного уровня следуют снизу вверх. Поэтому поиск ведётся
с конца перечня: первым совпадением оказывается самый верхний и самый
вложенный объект под курсором, как того и ждёт пользователь.

В сессии Wayland дерево окон приложению недоступно; перечень окажется
пустым, и оверлей продолжит работать без автоматического выделения.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QPoint, QRect

# Наименьший размер объекта, попадающего в перечень. Отсекает служебные
# окна нулевого размера и точечные элементы, выделять которые бессмысленно.
MINIMUM_SIZE = 24

# Предельная глубина обхода дерева. Современные наборы виджетов рисуют
# содержимое окна сами и вложенных окон почти не создают, поэтому большая
# глубина лишь замедлила бы сбор.
MAXIMUM_DEPTH = 4


@dataclass(frozen=True)
class InterfaceObject:
    """Прямоугольник объекта интерфейса в координатах экрана."""

    rect: QRect
    # Глубина вложенности: ноль соответствует окну верхнего уровня.
    depth: int

    @property
    def area(self) -> int:
        """Площадь объекта, применяется при сравнении совпадений."""
        return self.rect.width() * self.rect.height()


def list_objects(limit_rect: QRect | None = None) -> list[InterfaceObject]:
    """
    Снимок видимых объектов интерфейса.

    Возвращается перечень в порядке отрисовки. Пустой перечень означает,
    что дерево окон недоступно: в этом случае автоматическое выделение не
    применяется, а ручное продолжает работать.
    """
    try:
        from Xlib import X, display as xdisplay
    except ImportError:
        return []

    try:
        connection = xdisplay.Display()
    except Exception:  # noqa: BLE001 - отсутствие X11 не является ошибкой
        return []

    objects: list[InterfaceObject] = []
    try:
        root = connection.screen().root
        _collect(root, 0, 0, 0, objects, limit_rect, X.IsViewable)
    except Exception:  # noqa: BLE001 - неполный обход лучше отказа
        pass
    finally:
        try:
            connection.close()
        except Exception:  # noqa: BLE001 - соединение уже закрыто
            pass
    return objects


def _collect(
    window: object,
    offset_x: int,
    offset_y: int,
    depth: int,
    objects: list[InterfaceObject],
    limit_rect: QRect | None,
    viewable: int,
) -> None:
    """Рекурсивный обход дерева окон с накоплением границ."""
    if depth > MAXIMUM_DEPTH:
        return

    try:
        children = window.query_tree().children  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - окно могло исчезнуть во время обхода
        return

    # Потомки перечисляются снизу вверх; порядок сохраняется, а поиск
    # ведётся с конца перечня.
    for child in children:
        try:
            attributes = child.get_attributes()
            if attributes.map_state != viewable:
                # Свёрнутые и скрытые окна выделять незачем.
                continue
            geometry = child.get_geometry()
        except Exception:  # noqa: BLE001 - окно исчезло между запросами
            continue

        x = offset_x + geometry.x
        y = offset_y + geometry.y
        rect = QRect(x, y, geometry.width, geometry.height)

        if geometry.width >= MINIMUM_SIZE and geometry.height >= MINIMUM_SIZE:
            if limit_rect is None or rect.intersects(limit_rect):
                objects.append(InterfaceObject(rect, depth))

        _collect(child, x, y, depth + 1, objects, limit_rect, viewable)


def object_at(objects: list[InterfaceObject], point: QPoint) -> QRect | None:
    """
    Границы объекта под указанной точкой.

    Среди объектов, накрывающих точку, выбирается наименьший по площади:
    такое правило даёт наиболее точное выделение и не даёт победить
    служебным окнам во весь экран, которые окружение рабочего стола
    держит поверх прочих. При равной площади предпочитается объект,
    отрисованный позже, то есть лежащий выше.
    """
    best: InterfaceObject | None = None
    for item in objects:
        if not item.rect.contains(point):
            continue
        if best is None or item.area <= best.area:
            best = item
    return QRect(best.rect) if best is not None else None
