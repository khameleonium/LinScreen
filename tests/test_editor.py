"""Проверки сцены редактора аннотаций."""

from __future__ import annotations

import os
import unittest

# Проверки выполняются без графического сервера: платформа подменяется до
# создания приложения Qt.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, QRectF  # noqa: E402
from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from editor.canvas import AnnotationScene  # noqa: E402
from editor.items import ArrowItem, BlurItem, FrameItem, LabelItem, StepItem  # noqa: E402
from editor.tools import Tool  # noqa: E402

_application = QApplication.instance() or QApplication([])


def make_image(width: int = 400, height: int = 300) -> QImage:
    """Однотонное изображение для сцены."""
    image = QImage(width, height, QImage.Format.Format_ARGB32)
    image.fill(0xFF204060)
    return image


class SceneHistoryTest(unittest.TestCase):
    """Учёт действий, отмена и возврат."""

    def setUp(self) -> None:
        """Сцена с пустым снимком."""
        self.scene = AnnotationScene(make_image())

    def add_arrow(self) -> ArrowItem:
        """Добавление стрелки на сцену."""
        item = ArrowItem(QColor(255, 0, 0), 3)
        item.set_line(QPointF(10, 10), QPointF(100, 100))
        self.scene._register(item)
        return item

    def test_added_item_is_recorded(self) -> None:
        """Добавленный элемент попадает в историю."""
        self.add_arrow()
        self.assertTrue(self.scene.can_undo)
        self.assertFalse(self.scene.can_redo)

    def test_undo_and_redo(self) -> None:
        """Отмена убирает элемент, возврат ставит его обратно."""
        item = self.add_arrow()
        self.scene.undo()
        self.assertNotIn(item, self.scene.items())
        self.assertTrue(self.scene.can_redo)
        self.scene.redo()
        self.assertIn(item, self.scene.items())

    def test_new_action_clears_redo(self) -> None:
        """Новое действие делает историю линейной."""
        self.add_arrow()
        self.scene.undo()
        self.add_arrow()
        self.assertFalse(self.scene.can_redo)

    def test_remove_selected_is_undoable(self) -> None:
        """Удаление выбранного элемента отменяется возвратом."""
        item = self.add_arrow()
        item.setSelected(True)
        self.scene.remove_selected()
        self.assertNotIn(item, self.scene.items())
        self.assertTrue(self.scene.can_redo)
        self.scene.redo()
        self.assertIn(item, self.scene.items())

    def test_remove_without_selection_does_nothing(self) -> None:
        """Без выделения удаление не меняет состав сцены."""
        self.add_arrow()
        self.scene.remove_selected()
        self.assertTrue(self.scene.can_undo)
        self.assertFalse(self.scene.can_redo)

    def test_clear_resets_step_counter(self) -> None:
        """Очистка сбрасывает нумерацию шагов."""
        self.scene.settings.step_counter = 5
        self.add_arrow()
        self.scene.clear_annotations()
        self.assertEqual(self.scene.settings.step_counter, 1)
        self.assertFalse(self.scene.can_undo)


class SceneRenderTest(unittest.TestCase):
    """Экспорт сцены в изображение."""

    def test_result_matches_source_size(self) -> None:
        """Размер результата совпадает с исходным снимком."""
        scene = AnnotationScene(make_image(640, 480))
        result = scene.render_result()
        self.assertEqual(result.size(), scene.source_image.size())

    def test_annotation_changes_pixels(self) -> None:
        """Нарисованная рамка изменяет точки изображения."""
        scene = AnnotationScene(make_image())
        before = scene.render_result()
        frame = FrameItem(QColor(255, 255, 0), 6)
        frame.setRect(QRectF(20, 20, 200, 150))
        scene._register(frame)
        after = scene.render_result()
        self.assertNotEqual(before.pixel(20, 20), after.pixel(20, 20))

    def test_blur_replaces_area(self) -> None:
        """Размытие заменяет содержимое выбранной области."""
        image = make_image()
        # Контрастная полоса, исчезающая после размытия.
        for x in range(0, 400, 2):
            for y in range(100, 140):
                image.setPixel(x, y, 0xFFFFFFFF)
        scene = AnnotationScene(image)
        item = BlurItem(image, QRectF(0, 100, 400, 40), factor=20)
        scene._register(item)
        result = scene.render_result()
        self.assertNotEqual(image.pixel(1, 120), result.pixel(1, 120))

    def test_empty_label_is_discarded(self) -> None:
        """Пустая надпись удаляется при завершении ввода."""
        scene = AnnotationScene(make_image())
        scene.tool = Tool.TEXT
        label = LabelItem(QColor(255, 255, 255), 12)
        scene._register(label)
        scene._editing_label = label
        scene._finish_label_editing()
        self.assertFalse(scene.can_undo)

    def test_step_counter_increases(self) -> None:
        """Каждый нумератор получает следующий по порядку номер."""
        scene = AnnotationScene(make_image())
        scene._create_step(QPointF(10, 10))
        scene._create_step(QPointF(50, 50))
        numbers = [item._number for item in scene.items() if isinstance(item, StepItem)]
        self.assertEqual(sorted(numbers), [1, 2])


if __name__ == "__main__":
    unittest.main()
