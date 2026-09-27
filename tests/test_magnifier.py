"""Проверки лупы у указателя при выделении области."""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QRect  # noqa: E402
from PySide6.QtGui import QColor, QGuiApplication, QImage  # noqa: E402

from ui.magnifier import (  # noqa: E402
    CURSOR_GAP,
    MAX_RADIUS,
    MIN_RADIUS,
    Magnifier,
    MagnifierOptions,
)

_application = QGuiApplication.instance() or QGuiApplication([])


def _desktop(width: int, height: int) -> QImage:
    """Снимок рабочего стола с одной отмеченной точкой."""
    image = QImage(width, height, QImage.Format.Format_ARGB32)
    image.fill(QColor(10, 20, 30))
    return image


class MagnifierTest(unittest.TestCase):
    """Расчёт положения лупы, её радиуса и цвета точки."""

    def setUp(self) -> None:
        self.bounds = QRect(0, 0, 1920, 1080)
        self.magnifier = Magnifier(
            _desktop(1920, 1080), self.bounds, MagnifierOptions(radius=70, show_info=False)
        )

    def test_lens_right_and_below_cursor(self) -> None:
        """Лупа располагается правее и ниже указателя."""
        center = self.magnifier.lens_center(QPoint(100, 100), self.bounds)
        self.assertEqual(center, QPoint(100 + 70 + CURSOR_GAP, 100 + 70 + CURSOR_GAP))

    def test_lens_flips_at_edges(self) -> None:
        """У правого и нижнего края лупа переходит на другую сторону."""
        center = self.magnifier.lens_center(QPoint(1900, 1070), self.bounds)
        self.assertLess(center.x(), 1900)
        self.assertLess(center.y(), 1070)

    def test_radius_limits(self) -> None:
        """Радиус меняется шагами и не выходит за пределы."""
        self.assertTrue(self.magnifier.adjust_radius(1))
        self.assertEqual(self.magnifier.radius, 80)
        self.magnifier.adjust_radius(1000)
        self.assertEqual(self.magnifier.radius, MAX_RADIUS)
        self.assertFalse(self.magnifier.adjust_radius(1))
        self.magnifier.adjust_radius(-1000)
        self.assertEqual(self.magnifier.radius, MIN_RADIUS)

    def test_area_contains_lens(self) -> None:
        """Участок перерисовки охватывает весь круг лупы."""
        cursor = QPoint(500, 400)
        center = self.magnifier.lens_center(cursor, self.bounds)
        area = self.magnifier.area(cursor, self.bounds)
        self.assertTrue(area.contains(QRect(center.x() - 70, center.y() - 70, 140, 140)))

    def test_color_with_fractional_scale(self) -> None:
        """Цвет точки берётся с учётом масштаба снимка 1,25."""
        desktop = _desktop(1920, 1080)
        desktop.setPixelColor(1250, 625, QColor(255, 0, 0))
        magnifier = Magnifier(desktop, QRect(0, 0, 1536, 864), MagnifierOptions())
        self.assertEqual(magnifier.color_at(QPoint(1000, 500)), QColor(255, 0, 0))

    def test_color_with_desktop_offset(self) -> None:
        """Координаты отсчитываются от начала рабочего стола."""
        desktop = _desktop(200, 100)
        desktop.setPixelColor(10, 10, QColor(0, 255, 0))
        magnifier = Magnifier(desktop, QRect(-100, 0, 200, 100), MagnifierOptions())
        self.assertEqual(magnifier.color_at(QPoint(-90, 10)), QColor(0, 255, 0))
        self.assertEqual(magnifier.color_at(QPoint(500, 500)), QColor(0, 0, 0))


if __name__ == "__main__":
    unittest.main()
