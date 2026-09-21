"""Проверки сцены редактора аннотаций."""

from __future__ import annotations

import os
import unittest

# Проверки выполняются без графического сервера: платформа подменяется до
# создания приложения Qt.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, QRectF, QSize  # noqa: E402
from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from editor.canvas import AnnotationScene  # noqa: E402
from editor.icons import render_tool_icon  # noqa: E402
from editor.items import (  # noqa: E402
    ArrowItem,
    BlurItem,
    CropOverlayItem,
    FrameItem,
    LabelItem,
    StepItem,
)
from editor.tools import Tool  # noqa: E402
from editor.window import EditorWindow  # noqa: E402

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


class ToolIconsTest(unittest.TestCase):
    """Проверка генерации векторных пиктограмм инструментов."""

    def test_all_tools_have_valid_icons(self) -> None:
        """Для каждого инструмента генерируется непустая пиктограмма."""
        for tool in Tool:
            icon = render_tool_icon(tool, size=22)
            self.assertFalse(icon.isNull())
            pixmap = icon.pixmap(22, 22)
            self.assertFalse(pixmap.isNull())
            self.assertEqual(pixmap.width(), 22)
            self.assertEqual(pixmap.height(), 22)

    def test_icon_caching(self) -> None:
        """Повторный запрос возвращает кэшированный экземпляр."""
        icon1 = render_tool_icon(Tool.CROP, size=24)
        icon2 = render_tool_icon(Tool.CROP, size=24)
        self.assertIs(icon1, icon2)


class CropToolTest(unittest.TestCase):
    """Проверка работы инструмента кадрирования."""

    def setUp(self) -> None:
        self.image = make_image(400, 300)
        self.scene = AnnotationScene(self.image)

    def test_crop_changes_size_and_emits_signal(self) -> None:
        """Кадрирование уменьшает размер снимка и отправляет сигнал."""
        resized_sizes: list[QSize] = []
        self.scene.imageResized.connect(resized_sizes.append)

        crop_rect = QRectF(50, 40, 200, 150)
        self.scene.apply_crop(crop_rect)

        self.assertEqual(self.scene.source_image.width(), 200)
        self.assertEqual(self.scene.source_image.height(), 150)
        self.assertEqual(self.scene.sceneRect(), QRectF(0, 0, 200, 150))
        self.assertEqual(len(resized_sizes), 1)
        self.assertEqual(resized_sizes[0], QSize(200, 150))

    def test_crop_undo_and_redo(self) -> None:
        """Отмена кадрирования восстанавливает исходный размер и аннотации."""
        arrow = ArrowItem(QColor(255, 0, 0), 3)
        arrow.set_line(QPointF(10, 10), QPointF(50, 50))
        self.scene._register(arrow)

        self.scene.apply_crop(QRectF(0, 0, 100, 100))
        self.assertEqual(self.scene.source_image.size(), QSize(100, 100))

        # Отмена возвращает исходный снимок 400x300 и стрелку
        self.scene.undo()
        self.assertEqual(self.scene.source_image.size(), QSize(400, 300))
        self.assertIn(arrow, self.scene.items())

        # Повтор кадрирования снова обрезает растр
        self.scene.redo()
        self.assertEqual(self.scene.source_image.size(), QSize(100, 100))

    def test_crop_switch_tool_cancels_overlay(self) -> None:
        """Смена инструмента убирает оверлей кадрирования."""
        self.scene.tool = Tool.CROP
        overlay = CropOverlayItem(self.scene.sceneRect())
        overlay.set_crop_rect(QRectF(10, 10, 100, 100))
        self.scene._crop_overlay = overlay
        self.scene.addItem(overlay)

        self.assertTrue(self.scene.is_cropping)
        self.scene.tool = Tool.ARROW
        self.assertFalse(self.scene.is_cropping)
        self.assertIsNone(self.scene._crop_overlay)

    def test_crop_overlay_bounding_rect(self) -> None:
        """Охватывающий прямоугольник оверлея включает сцену и выделение."""
        overlay = CropOverlayItem(QRectF(0, 0, 400, 300))
        overlay.set_crop_rect(QRectF(50, 50, 100, 100))
        bounds = overlay.boundingRect()
        self.assertTrue(bounds.contains(QPointF(50, 50)))
        self.assertEqual(overlay.crop_rect(), QRectF(50, 50, 100, 100))


class EditorWindowTest(unittest.TestCase):
    """Проверка интерфейса окна редактора аннотаций."""

    def setUp(self) -> None:
        self.window = EditorWindow(make_image(500, 400))

    def tearDown(self) -> None:
        self.window.close()

    def test_tool_actions_have_icons_and_tooltips(self) -> None:
        """Все кнопки инструментов имеют значки и подсказки."""
        self.assertEqual(len(self.window._tool_actions), 8)
        for tool, action in self.window._tool_actions.items():
            self.assertFalse(action.icon().isNull(), f"Значок отсутствует у {tool}")
            self.assertTrue(len(action.toolTip()) > 0, f"Подсказка отсутствует у {tool}")
            self.assertIn(tool.label, action.toolTip())

    def test_escape_cancels_crop_before_close(self) -> None:
        """Esc сбрасывает режим обрезки без закрытия окна."""
        self.window.show()
        self.window._activate_tool(Tool.CROP)
        overlay = CropOverlayItem(self.window._scene.sceneRect())
        overlay.set_crop_rect(QRectF(10, 10, 100, 100))
        self.window._scene._crop_overlay = overlay
        self.window._scene.addItem(overlay)

        self.assertTrue(self.window._scene.is_cropping)
        self.window._handle_close_or_cancel()
        self.assertFalse(self.window._scene.is_cropping)
        self.assertTrue(self.window.isVisible())


if __name__ == "__main__":
    unittest.main()
