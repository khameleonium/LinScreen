"""Проверки перевода строк интерфейса."""

from __future__ import annotations

import ast
import json
import pathlib
import re
import unittest

from core import i18n

# Файлы, строки которых подлежат переводу.
SOURCE_FILES = tuple(
    path
    for path in pathlib.Path(".").rglob("*.py")
    if ".venv" not in str(path)
    and not str(path).startswith(("build", "dist", "tests"))
)

CYRILLIC = re.compile(r"[А-Яа-яЁё]")


def collect_marked_strings() -> set[str]:
    """Строки, помеченные к переводу вызовом tr()."""
    found: set[str] = set()
    for path in SOURCE_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "tr"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                found.add(node.args[0].value)
    return found


class CatalogTest(unittest.TestCase):
    """Полнота и согласованность словаря перевода."""

    def setUp(self) -> None:
        """Чтение словаря английского языка."""
        path = pathlib.Path("locale/en.json")
        self.catalog = json.loads(path.read_text(encoding="utf-8"))
        self.entries = {
            key: value for key, value in self.catalog.items() if not key.startswith("_")
        }

    def test_every_marked_string_is_translated(self) -> None:
        """Каждая помеченная строка присутствует в словаре."""
        missing = sorted(collect_marked_strings() - set(self.entries))
        self.assertEqual(missing, [], f"без перевода: {missing[:5]}")

    def test_catalog_has_no_unused_entries(self) -> None:
        """Словарь не содержит записей, отсутствующих в коде."""
        marked = collect_marked_strings()
        # Часть строк объявлена на уровне модулей и переводится в месте
        # показа, поэтому вызова tr() с их текстом в коде нет. Названия
        # профилей при этом могут не содержать кириллицы вовсе.
        declared = set()
        for name in ("core/config.py", "encoder/profiles.py", "encoder/images.py"):
            tree = ast.parse(pathlib.Path(name).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    declared.add(node.value)
        unused = sorted(set(self.entries) - marked - declared)
        self.assertEqual(unused, [], f"лишние записи: {unused[:5]}")

    def test_placeholders_match(self) -> None:
        """Подстановки перевода совпадают с подстановками исходной строки."""
        pattern = re.compile(r"\{(\d+)[^}]*\}")
        for source, translated in self.entries.items():
            with self.subTest(source=source[:40]):
                self.assertEqual(
                    sorted(pattern.findall(source)),
                    sorted(pattern.findall(translated)),
                )

    def test_translations_are_not_empty(self) -> None:
        """Пустой перевод недопустим: он оставил бы надпись пустой."""
        for source, translated in self.entries.items():
            with self.subTest(source=source[:40]):
                self.assertTrue(translated.strip())

    def test_english_catalog_has_no_cyrillic(self) -> None:
        """Английский словарь не содержит непереведённых строк."""
        for source, translated in self.entries.items():
            # Исключение составляют строки, целиком состоящие из имён
            # параметров и обозначений форматов.
            with self.subTest(source=source[:40]):
                self.assertIsNone(CYRILLIC.search(translated), translated[:60])


class LanguageSelectionTest(unittest.TestCase):
    """Выбор языка и запасное поведение."""

    def tearDown(self) -> None:
        """Возврат к исходному языку."""
        i18n.set_language(i18n.SOURCE_LANGUAGE)

    def test_source_language_returns_original(self) -> None:
        """Исходный язык оставляет строки без изменений."""
        i18n.set_language("ru")
        self.assertEqual(i18n.tr("Готово"), "Готово")

    def test_english_translation_is_applied(self) -> None:
        """Выбор английского языка подменяет строки."""
        self.assertEqual(i18n.set_language("en"), "en")
        self.assertEqual(i18n.tr("Готово"), "Done")

    def test_unknown_language_falls_back(self) -> None:
        """Отсутствующий словарь возвращает приложение к исходному языку."""
        self.assertEqual(i18n.set_language("xx"), "ru")
        self.assertEqual(i18n.tr("Готово"), "Готово")

    def test_missing_string_returns_source(self) -> None:
        """Строка без перевода показывается на исходном языке."""
        i18n.set_language("en")
        self.assertEqual(i18n.tr("Строка без перевода"), "Строка без перевода")

    def test_available_languages_include_english(self) -> None:
        """Перечень языков собирается по наличию словарей."""
        languages = i18n.available_languages()
        self.assertIn("ru", languages)
        self.assertIn("en", languages)


if __name__ == "__main__":
    unittest.main()
