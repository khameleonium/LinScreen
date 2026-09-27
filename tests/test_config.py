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
        self.path = Path(self._directory.name) / "config.ini"

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
        manager.settings.general.show_notifications = False
        manager.save()

        restored = ConfigManager(self.path)
        restored.load()
        self.assertEqual(restored.settings.video.fps, 60)
        # Знак процента в шаблоне не должен толковаться как подстановка.
        self.assertEqual(restored.settings.paths.image_template, "Кадр %H-%M-%S")
        self.assertFalse(restored.settings.general.show_notifications)

    def test_file_contains_comments(self) -> None:
        """Файл снабжён примечаниями и заголовками разделов."""
        manager = ConfigManager(self.path)
        manager.load()
        manager.save()
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("[video]", text)
        self.assertIn("# Частота кадров захвата", text)
        # Логические значения записываются словами.
        self.assertIn("show_cursor = да", text)

    def test_command_with_placeholders_survives(self) -> None:
        """Образец команды с фигурными скобками читается без искажений."""
        manager = ConfigManager(self.path)
        manager.load()
        template = "{ffmpeg} {video_input} -c:v libx264 -crf 20 {output}"
        manager.settings.encoding.custom_command = template
        manager.save()

        restored = ConfigManager(self.path)
        restored.load()
        self.assertEqual(restored.settings.encoding.custom_command, template)

    def test_damaged_file_does_not_break_startup(self) -> None:
        """Повреждённый файл заменяется значениями по умолчанию."""
        self.path.write_text("совершенно не файл настроек", encoding="utf-8")
        settings = ConfigManager(self.path).load()
        self.assertEqual(settings.video.profile_id, "mkv_h264")

    def test_unknown_keys_are_ignored(self) -> None:
        """Незнакомые ключи и разделы от прежних версий не мешают чтению."""
        self.path.write_text(
            "[video]\nfps = 24\nнеизвестный_ключ = 1\n\n[постороннее]\nx = 1\n",
            encoding="utf-8",
        )
        settings = ConfigManager(self.path).load()
        self.assertEqual(settings.video.fps, 24)

    def test_wrong_value_type_is_rejected(self) -> None:
        """Значение неподходящего типа отбрасывается."""
        self.path.write_text("[video]\nfps = шестьдесят\n", encoding="utf-8")
        settings = ConfigManager(self.path).load()
        self.assertEqual(settings.video.fps, Settings().video.fps)

    def test_legacy_format_is_migrated(self) -> None:
        """Настройки прежнего формата переносятся при первом чтении."""
        legacy = self.path.with_name("config.json")
        legacy.write_text(
            json.dumps({"video": {"fps": 77}, "images": {"image_format": "webp"}}),
            encoding="utf-8",
        )
        manager = ConfigManager(self.path)
        settings = manager.load()
        self.assertEqual(settings.video.fps, 77)
        self.assertEqual(settings.images.image_format, "webp")
        # Файл нового формата создаётся сразу, прежний не удаляется.
        self.assertTrue(self.path.is_file())
        self.assertTrue(legacy.is_file())

    def test_previous_hotkey_name_is_accepted(self) -> None:
        """Сочетание, записанное под прежним именем, не теряется."""
        self.path.write_text(
            "[hotkeys]\nrecord_region = Ctrl+Alt+G\n", encoding="utf-8"
        )
        settings = ConfigManager(self.path).load()
        self.assertEqual(settings.hotkeys.record_toggle, "Ctrl+Alt+G")

    def test_current_hotkey_name_wins(self) -> None:
        """При наличии обоих имён используется текущее."""
        self.path.write_text(
            "[hotkeys]\nrecord_region = Ctrl+Alt+G\nrecord_toggle = Ctrl+Alt+H\n",
            encoding="utf-8",
        )
        settings = ConfigManager(self.path).load()
        self.assertEqual(settings.hotkeys.record_toggle, "Ctrl+Alt+H")

    def test_export_and_import(self) -> None:
        """Настройки переносятся через файл и накладываются на текущие."""
        source = ConfigManager(self.path)
        source.load()
        source.settings.video.fps = 48
        source.settings.hotkeys.record_toggle = "Ctrl+Alt+Q"
        transferred = self.path.with_name("перенос.ini")
        source.export_to(transferred)
        self.assertTrue(transferred.is_file())

        target = ConfigManager(self.path.with_name("другой.ini"))
        target.load()
        self.assertNotEqual(target.settings.video.fps, 48)
        target.import_from(transferred)
        self.assertEqual(target.settings.video.fps, 48)
        self.assertEqual(target.settings.hotkeys.record_toggle, "Ctrl+Alt+Q")
        # Прочитанные настройки сразу сохраняются в рабочий файл.
        self.assertTrue(target.path.is_file())

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
        self.manager = ConfigManager(self.root / "config.ini")
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
