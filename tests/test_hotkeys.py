"""Проверки преобразования сочетаний клавиш."""

from __future__ import annotations

import unittest

from core.hotkeys import (
    _x11_key_name,
    parse_combination,
    to_pynput_sequence,
    to_pynput_sequences,
)


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


class ExactMatchTest(unittest.TestCase):
    """Разбор сочетания на модификаторы и основную клавишу."""

    def test_plain_key_has_no_modifiers(self) -> None:
        """Сочетание без модификаторов не должно иметь их в наборе."""
        parsed = parse_combination("Print")
        self.assertEqual(parsed.modifiers, frozenset())
        self.assertIn("print_screen", parsed.keys)

    def test_modifiers_are_collected(self) -> None:
        """Все модификаторы сочетания попадают в набор."""
        parsed = parse_combination("Ctrl+Alt+R")
        self.assertEqual(parsed.modifiers, frozenset({"ctrl", "alt"}))
        self.assertIn("r", parsed.keys)

    def test_same_key_differs_by_modifiers(self) -> None:
        """Одна клавиша с разными модификаторами даёт разные сочетания."""
        plain = parse_combination("Print")
        with_ctrl = parse_combination("Ctrl+Print")
        self.assertNotEqual(plain.modifiers, with_ctrl.modifiers)
        self.assertEqual(plain.keys & with_ctrl.keys, frozenset({"print_screen"}))

    def test_shift_adds_upper_level_symbols(self) -> None:
        """Для сочетаний с Shift учитываются символы верхнего уровня."""
        parsed = parse_combination("Shift+Print")
        self.assertEqual(parsed.modifiers, frozenset({"shift"}))
        # Кроме основного обозначения перечень содержит числовые коды.
        self.assertIn("print_screen", parsed.keys)

    def test_function_key(self) -> None:
        """Функциональная клавиша распознаётся как основная."""
        parsed = parse_combination("Ctrl+Shift+F9")
        self.assertEqual(parsed.modifiers, frozenset({"ctrl", "shift"}))
        self.assertIn("f9", parsed.keys)

    def test_empty_combination_is_invalid(self) -> None:
        """Пустая настройка не порождает пригодного сочетания."""
        self.assertFalse(parse_combination("").is_valid)

    def test_lone_modifier_is_a_key(self) -> None:
        """Сочетание из одного модификатора считается клавишей."""
        parsed = parse_combination("Shift")
        self.assertEqual(parsed.modifiers, frozenset())
        self.assertTrue(parsed.is_valid)


if __name__ == "__main__":
    unittest.main()
