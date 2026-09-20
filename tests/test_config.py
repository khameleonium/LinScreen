"""Проверки менеджера настроек."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.config import ConfigManager, Settings, sanitize_filename


class ConfigPersistenceTest(unittest.TestCase):
    """Чтение и запись файла настроек."""

    def setUp(self) -> None:
        """Временный каталог для файла конфигурации."""
        self._directory = tempfile.TemporaryDirectory()
        self.path = Path(self._directory.name) / "config.json"

    def tearDown(self) -> None:
        """Удаление временного каталога."""
        self._directory.cleanup()

    def test_defaults_when_file_is_absent(self) -> None:
        """Отсутствие файла не является ошибкой."""
        manager = ConfigManager(self.path)
        settings = manager.load()
        self.assertEqual(settings.video.profile_id, "mkv_h264")

    def test_round_trip(self) -> None:
        """Сохранённые значения читаются обратно без искажений."""
        manager = ConfigManager(self.path)
        manager.load()
        manager.settings.video.fps = 60
        manager.settings.paths.image_template = "Кадр %H-%M-%S"
        manager.save()

        restored = ConfigManager(self.path)
        restored.load()
        self.assertEqual(restored.settings.video.fps, 60)
        self.assertEqual(restored.settings.paths.image_template, "Кадр %H-%M-%S")

    def test_damaged_file_does_not_break_startup(self) -> None:
        """Повреждённый файл заменяется значениями по умолчанию."""
        self.path.write_text("{это не json", encoding="utf-8")
        settings = ConfigManager(self.path).load()
        self.assertEqual(settings.video.profile_id, "mkv_h264")

    def test_unknown_keys_are_ignored(self) -> None:
        """Незнакомые ключи от прежних версий не мешают чтению."""
        payload = {"video": {"fps": 24, "неизвестный_ключ": 1}, "постороннее": True}
        self.path.write_text(json.dumps(payload), encoding="utf-8")
        settings = ConfigManager(self.path).load()
        self.assertEqual(settings.video.fps, 24)

    def test_wrong_value_type_is_rejected(self) -> None:
        """Значение неподходящего типа отбрасывается."""
        self.path.write_text(json.dumps({"video": {"fps": "шестьдесят"}}), encoding="utf-8")
        settings = ConfigManager(self.path).load()
        self.assertEqual(settings.video.fps, Settings().video.fps)

    def test_save_is_atomic(self) -> None:
        """После записи временный файл не остаётся на диске."""
        manager = ConfigManager(self.path)
        manager.load()
        manager.save()
        self.assertTrue(self.path.is_file())
        self.assertFalse(self.path.with_suffix(".tmp").exists())


class OutputPathTest(unittest.TestCase):
    """Формирование имён сохраняемых файлов."""

    def setUp(self) -> None:
        """Настройки с каталогами во временном месте."""
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)
        self.manager = ConfigManager(self.root / "config.json")
        self.manager.load()
        self.manager.settings.paths.images_dir = str(self.root / "снимки")
        self.manager.settings.paths.image_template = "Снимок"

    def tearDown(self) -> None:
        """Удаление временного каталога."""
        self._directory.cleanup()

    def test_existing_file_is_not_overwritten(self) -> None:
        """При совпадении имён добавляется порядковый номер."""
        first = self.manager.build_image_path("png")
        first.parent.mkdir(parents=True, exist_ok=True)
        first.write_bytes(b"")
        second = self.manager.build_image_path("png")
        self.assertNotEqual(first, second)
        self.assertIn("(1)", second.name)

    def test_extension_is_normalised(self) -> None:
        """Точка в расширении не удваивается."""
        self.assertEqual(self.manager.build_image_path(".webp").suffix, ".webp")

    def test_separator_is_removed_from_name(self) -> None:
        """Разделитель каталогов в шаблоне имени не уводит файл в сторону."""
        self.assertEqual(sanitize_filename("папка/файл"), "папка_файл")

    def test_empty_name_gets_substitute(self) -> None:
        """Пустое имя заменяется значением по умолчанию."""
        self.assertEqual(sanitize_filename("   "), "capture")


if __name__ == "__main__":
    unittest.main()
