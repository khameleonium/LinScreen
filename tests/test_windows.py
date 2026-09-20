"""Проверки поиска объекта интерфейса под курсором."""

from __future__ import annotations

import unittest

from PySide6.QtCore import QPoint, QRect

from capture.windows import InterfaceObject, object_at


def make(x: int, y: int, width: int, height: int, depth: int = 0) -> InterfaceObject:
    """Объект интерфейса с заданными границами."""
    return InterfaceObject(QRect(x, y, width, height), depth)


class ObjectSearchTest(unittest.TestCase):
    """Выбор объекта среди накрывающих указанную точку."""

    def test_smallest_object_wins(self) -> None:
        """Из вложенных объектов выбирается наименьший."""
        objects = [make(0, 0, 1920, 1080), make(100, 100, 800, 600), make(200, 200, 300, 200)]
        found = object_at(objects, QPoint(250, 250))
        self.assertEqual(found, QRect(200, 200, 300, 200))

    def test_full_screen_object_does_not_win(self) -> None:
        """Служебное окно во весь экран уступает обычному окну."""
        objects = [make(50, 50, 400, 300), make(0, 0, 1920, 1080)]
        self.assertEqual(object_at(objects, QPoint(100, 100)), QRect(50, 50, 400, 300))

    def test_point_outside_returns_nothing(self) -> None:
        """Вне всех объектов совпадения нет."""
        objects = [make(100, 100, 200, 200)]
        self.assertIsNone(object_at(objects, QPoint(50, 50)))

    def test_equal_area_prefers_later(self) -> None:
        """При равной площади предпочитается отрисованный позже."""
        objects = [make(0, 0, 100, 100), make(0, 0, 100, 100, depth=1)]
        found = object_at(objects, QPoint(10, 10))
        self.assertEqual(found, QRect(0, 0, 100, 100))

    def test_empty_list_is_safe(self) -> None:
        """Пустой перечень не приводит к ошибке."""
        self.assertIsNone(object_at([], QPoint(10, 10)))

    def test_result_is_a_copy(self) -> None:
        """Возвращается копия: изменение результата не трогает перечень."""
        objects = [make(10, 10, 100, 100)]
        found = object_at(objects, QPoint(20, 20))
        assert found is not None
        found.setWidth(5)
        self.assertEqual(objects[0].rect.width(), 100)

    def test_area_is_computed(self) -> None:
        """Площадь объекта считается по его границам."""
        self.assertEqual(make(0, 0, 40, 30).area, 1200)


if __name__ == "__main__":
    unittest.main()
