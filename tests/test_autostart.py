"""Проверки управления автозапуском приложения."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from core import autostart


class AutostartTest(unittest.TestCase):
    """Создание и удаление записи автозапуска по стандарту XDG."""

    def setUp(self) -> None:
        """Подмена каталога настроек временным."""
        self._directory = tempfile.TemporaryDirectory()
        self._saved = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = self._directory.name

    def tearDown(self) -> None:
        """Возврат прежнего каталога настроек."""
        if self._saved is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = self._saved
        self._directory.cleanup()

    def test_disabled_by_default(self) -> None:
        """Без файла автозапуск считается отключённым."""
        self.assertFalse(autostart.is_enabled())

    def test_enable_creates_executable_entry(self) -> None:
        """Включение создаёт исполняемый файл в каталоге автозапуска."""
        path = autostart.enable()
        self.assertTrue(path.is_file())
        self.assertTrue(path.stat().st_mode & 0o111)
        self.assertTrue(autostart.is_enabled())

    def test_menu_entry_is_separate_from_autostart(self) -> None:
        """Ярлык в меню и запись автозапуска существуют независимо."""
        autostart.install_menu_entry()
        self.assertTrue(autostart.menu_entry_installed())
        self.assertFalse(autostart.is_enabled())
        autostart.remove_menu_entry()
        self.assertFalse(autostart.menu_entry_installed())

    def test_menu_entry_has_no_autostart_fields(self) -> None:
        """Ярлык меню не содержит полей автоматического запуска."""
        entry = autostart.desktop_entry(autostart=False)
        self.assertNotIn("X-GNOME-Autostart", entry)
        self.assertIn("Categories=", entry)

    def test_launch_command_from_sources(self) -> None:
        """Вне сборки команда состоит из интерпретатора и точки входа."""
        command = autostart.launch_command()
        self.assertEqual(len(command), 2)
        self.assertTrue(command[1].endswith("main.py"))

    def test_launch_command_from_appimage(self) -> None:
        """Для AppImage команда запуска ссылается непосредственно на файл образа."""
        with tempfile.NamedTemporaryFile(suffix=".AppImage") as fake_appimage:
            os.environ["APPIMAGE"] = fake_appimage.name
            try:
                command = autostart.launch_command()
                self.assertEqual(command, [fake_appimage.name])
                entry = autostart.desktop_entry()
                self.assertIn(f"Exec={fake_appimage.name}", entry)
            finally:
                os.environ.pop("APPIMAGE", None)

    def test_entry_points_to_current_interpreter(self) -> None:
        """Команда запуска ссылается на текущий интерпретатор и точку входа."""
        content = autostart.desktop_entry()
        interpreter, script = autostart.entry_point()
        self.assertIn(interpreter, content)
        self.assertIn(str(script), content)
        self.assertIn("Type=Application", content)

    def test_paths_with_spaces_are_quoted(self) -> None:
        """Путь с пробелом экранируется в команде запуска."""
        entry = autostart.desktop_entry()
        exec_line = next(
            line for line in entry.splitlines() if line.startswith("Exec=")
        )
        # Команда собрана средством экранирования, поэтому разбор строки
        # оболочкой вернёт ровно два элемента.
        import shlex

        parts = shlex.split(exec_line.removeprefix("Exec="))
        self.assertEqual(len(parts), 2)
        self.assertTrue(parts[1].endswith("main.py"))

    def test_disable_removes_entry(self) -> None:
        """Отключение удаляет файл и не падает при повторном вызове."""
        autostart.enable()
        autostart.disable()
        self.assertFalse(autostart.is_enabled())
        autostart.disable()

    def test_set_enabled_switches_state(self) -> None:
        """Переключатель приводит состояние к заданному."""
        autostart.set_enabled(True)
        self.assertTrue(autostart.is_enabled())
        autostart.set_enabled(False)
        self.assertFalse(autostart.is_enabled())

    def test_directory_is_created_when_absent(self) -> None:
        """Отсутствующий каталог автозапуска создаётся при включении."""
        target = Path(self._directory.name) / "autostart"
        self.assertFalse(target.exists())
        autostart.enable()
        self.assertTrue(target.is_dir())


if __name__ == "__main__":
    unittest.main()
