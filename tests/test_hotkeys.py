"""Проверки преобразования сочетаний клавиш."""

from __future__ import annotations

import unittest

from core.hotkeys import _x11_key_name, to_pynput_sequence, to_pynput_sequences


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


class ShiftVariantTest(unittest.TestCase):
    """Учёт символов верхнего уровня клавиши."""

    def test_single_sequence_without_shift(self) -> None:
        """Без Shift символ клавиши не меняется, вариант один."""
        self.assertEqual(to_pynput_sequences("Print"), ["<print_screen>"])
        self.assertEqual(to_pynput_sequences("Ctrl+Alt+R"), ["<ctrl>+<alt>+r"])

    def test_shift_combination_keeps_base_first(self) -> None:
        """Основное обозначение остаётся первым в перечне."""
        sequences = to_pynput_sequences("Shift+Print")
        self.assertEqual(sequences[0], "<shift>+<print_screen>")

    def test_variants_share_modifiers(self) -> None:
        """Дополнительные варианты отличаются только символом клавиши."""
        sequences = to_pynput_sequences("Ctrl+Shift+Print")
        for sequence in sequences:
            self.assertTrue(sequence.startswith("<ctrl>+<shift>+"))

    def test_empty_combination_gives_nothing(self) -> None:
        """Пустая настройка не порождает обозначений."""
        self.assertEqual(to_pynput_sequences(""), [])

    def test_lone_shift_is_not_expanded(self) -> None:
        """Сочетание из одного модификатора вариантов не требует."""
        self.assertEqual(to_pynput_sequences("Shift"), ["<shift>"])

    def test_x11_names(self) -> None:
        """Названия клавиш переводятся в обозначения протокола X11."""
        self.assertEqual(_x11_key_name("print"), "Print")
        self.assertEqual(_x11_key_name("pageup"), "Prior")
        self.assertEqual(_x11_key_name("f7"), "F7")
        self.assertEqual(_x11_key_name("r"), "r")


if __name__ == "__main__":
    unittest.main()
