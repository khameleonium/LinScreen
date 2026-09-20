"""Проверки преобразования сочетаний клавиш."""

from __future__ import annotations

import unittest

from core.hotkeys import to_pynput_sequence


class HotkeyConversionTest(unittest.TestCase):
    """Перевод записи вида "Ctrl+Alt+R" в формат библиотеки перехвата."""

    def test_single_special_key(self) -> None:
        """Клавиша снимка экрана переводится в специальное обозначение."""
        self.assertEqual(to_pynput_sequence("Print"), "<print_screen>")

    def test_modifiers_are_ordered_as_given(self) -> None:
        """Модификаторы сохраняют порядок записи."""
        self.assertEqual(to_pynput_sequence("Ctrl+Alt+R"), "<ctrl>+<alt>+r")

    def test_function_keys(self) -> None:
        """Функциональные клавиши заключаются в угловые скобки."""
        self.assertEqual(to_pynput_sequence("Ctrl+Shift+F5"), "<ctrl>+<shift>+<f5>")

    def test_super_is_translated(self) -> None:
        """Системная клавиша переводится в принятое библиотекой имя."""
        self.assertEqual(to_pynput_sequence("Super+S"), "<cmd>+s")

    def test_case_is_normalised(self) -> None:
        """Регистр записи не влияет на результат."""
        self.assertEqual(to_pynput_sequence("CTRL+alt+R"), "<ctrl>+<alt>+r")

    def test_empty_value_gives_empty_sequence(self) -> None:
        """Пустая настройка означает отключённое действие."""
        self.assertEqual(to_pynput_sequence(""), "")

    def test_stray_separators_are_skipped(self) -> None:
        """Лишние разделители не порождают пустых элементов."""
        self.assertEqual(to_pynput_sequence("Ctrl++R"), "<ctrl>+r")


if __name__ == "__main__":
    unittest.main()
