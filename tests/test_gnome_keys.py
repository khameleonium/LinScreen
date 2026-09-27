"""
Проверки записи сочетаний в пользовательские комбинации клавиш GNOME.

Утилита gsettings подменяется словарём в памяти: проверяется преобразование
сочетаний, сборка команды, обнаружение занятых клавиш и порядок записей в
списке комбинаций без обращения к настройкам системы.
"""

from __future__ import annotations

import os
import shlex
import unittest
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QGuiApplication  # noqa: E402

from backends.wayland import gnome_keys  # noqa: E402
from core.identity import BUS_NAME  # noqa: E402

_app = QGuiApplication.instance() or QGuiApplication([])


class FakeGsettings:
    """Хранилище значений gsettings в памяти."""

    def __init__(self, values: dict[tuple[str, str], str] | None = None) -> None:
        self.values: dict[tuple[str, str], str] = dict(values or {})
        self.values.setdefault((gnome_keys.MEDIA_KEYS_SCHEMA, gnome_keys.CUSTOM_LIST_KEY), "@as []")

    def __call__(self, *arguments: str) -> str:
        command, schema = arguments[0], arguments[1]
        if command == "get":
            return self.values.get((schema, arguments[2]), "''")
        if command == "set":
            self.values[(schema, arguments[2])] = arguments[3]
            return ""
        if command == "reset":
            self.values.pop((schema, arguments[2]), None)
            return ""
        if command == "list-recursively":
            return "\n".join(
                f"{name} {key} {value}"
                for (name, key), value in self.values.items()
                if name == schema
            )
        return ""


class ConvertTest(unittest.TestCase):
    def test_to_gnome(self) -> None:
        self.assertEqual(gnome_keys.to_gnome_accelerator("Print"), "Print")
        self.assertEqual(gnome_keys.to_gnome_accelerator("Shift+Print"), "<Shift>Print")
        self.assertEqual(gnome_keys.to_gnome_accelerator("Ctrl+Alt+R"), "<Control><Alt>r")
        self.assertEqual(gnome_keys.to_gnome_accelerator("Meta+F5"), "<Super>F5")
        self.assertEqual(gnome_keys.to_gnome_accelerator(""), "")

    def test_from_gnome(self) -> None:
        self.assertEqual(gnome_keys.from_gnome_accelerator("<Control><Alt>r"), "Ctrl+Alt+R")
        self.assertEqual(gnome_keys.from_gnome_accelerator("<Primary><Shift>Print"),
                         "Ctrl+Shift+Print")
        self.assertEqual(gnome_keys.from_gnome_accelerator("<Alt>Page_Up"), "Alt+PgUp")
        self.assertEqual(gnome_keys.from_gnome_accelerator(""), "")

    def test_round_trip(self) -> None:
        for combination in ("Print", "Alt+Print", "Ctrl+Alt+P", "Ctrl+Shift+F9"):
            accelerator = gnome_keys.to_gnome_accelerator(combination)
            self.assertEqual(gnome_keys.from_gnome_accelerator(accelerator), combination)


class EntryTest(unittest.TestCase):
    def test_path_and_action(self) -> None:
        path = gnome_keys.entry_path("screenshot_region")
        self.assertTrue(path.endswith("/linscreen-screenshot-region/"))
        self.assertEqual(gnome_keys.action_of(path), "screenshot_region")
        self.assertEqual(gnome_keys.action_of(path + "binding"), "screenshot_region")
        self.assertEqual(gnome_keys.action_of(gnome_keys.CUSTOM_DIR + "custom0/"), "")

    def test_command_calls_service_then_launches(self) -> None:
        command = gnome_keys.activation_command(
            "record_toggle", ["/opt/Lin Screen.AppImage"], "--record"
        )
        argv = shlex.split(command)
        self.assertEqual(argv[:2], ["sh", "-c"])
        script = argv[2]
        self.assertIn(BUS_NAME, script)
        self.assertIn("record_toggle", script)
        # Путь с пробелом остаётся одним аргументом после разбора оболочкой.
        self.assertTrue(script.endswith("exec '/opt/Lin Screen.AppImage' --record"))

    def test_changed_actions(self) -> None:
        message = SimpleNamespace(
            body=(gnome_keys.entry_path("screenshot_window") + "binding", [""], "tag")
        )
        self.assertEqual(gnome_keys.changed_actions(message), ["screenshot_window"])
        listing = SimpleNamespace(
            body=(gnome_keys.MEDIA_KEYS_PATH, ["custom-keybindings"], "tag")
        )
        self.assertEqual(gnome_keys.changed_actions(listing), ["*"])
        other = SimpleNamespace(body=("/org/gnome/desktop/interface/", ["font-name"], "t"))
        self.assertEqual(gnome_keys.changed_actions(other), [])


class BindTest(unittest.TestCase):
    def setUp(self) -> None:
        other = gnome_keys.CUSTOM_DIR + "custom0/"
        self.fake = FakeGsettings(
            {
                (gnome_keys.MEDIA_KEYS_SCHEMA, gnome_keys.CUSTOM_LIST_KEY): f"['{other}']",
                (f"{gnome_keys.CUSTOM_SCHEMA}:{other}", "binding"): "'<Super>t'",
                (f"{gnome_keys.CUSTOM_SCHEMA}:{other}", "name"): "'Терминал'",
                ("org.gnome.shell.keybindings", "show-screenshot-ui"): "['Print']",
                ("org.gnome.shell.keybindings", "screenshot"): "@as []",
            }
        )
        patcher = mock.patch.object(gnome_keys, "_gsettings", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.other = other

    def _paths(self) -> list[str]:
        return gnome_keys._parse_list(
            self.fake.values[(gnome_keys.MEDIA_KEYS_SCHEMA, gnome_keys.CUSTOM_LIST_KEY)]
        )

    def test_bind_writes_entries_and_reports_conflicts(self) -> None:
        mapping = {
            "screenshot_region": "Print",
            "screenshot_window": "Alt+Print",
            "record_toggle": "Meta+T",
            "record_toggle_pause": "",
        }
        commands = {action: f"cmd-{action}" for action in mapping}
        assigned, conflicts = gnome_keys.bind(mapping, commands)

        self.assertEqual(conflicts["screenshot_region"][0], "Print")
        self.assertIn("show-screenshot-ui", conflicts["screenshot_region"][1])
        self.assertEqual(conflicts["record_toggle"], ("Meta+T", "Терминал"))
        self.assertEqual(assigned["screenshot_window"], "Alt+Print")
        self.assertEqual(assigned["screenshot_region"], "")

        window = gnome_keys.entry_path("screenshot_window")
        self.assertEqual(self._paths(), [self.other, window])
        schema = f"{gnome_keys.CUSTOM_SCHEMA}:{window}"
        self.assertEqual(self.fake.values[(schema, "binding")], "'<Alt>Print'")
        self.assertEqual(self.fake.values[(schema, "command")], "'cmd-screenshot_window'")
        self.assertEqual(gnome_keys.read_binding("screenshot_window"), "Alt+Print")

    def test_remove_all_keeps_foreign_entries(self) -> None:
        gnome_keys.bind({"screenshot_window": "Alt+Print"}, {})
        self.assertEqual(gnome_keys.remove_all(), ["screenshot_window"])
        self.assertEqual(self._paths(), [self.other])
        self.assertEqual(gnome_keys.read_binding("screenshot_window"), "")

    def test_suspend_writes_are_ordered(self) -> None:
        gnome_keys.bind({"screenshot_window": "Alt+Print"}, {})
        gnome_keys.set_bindings({"screenshot_window": ""}, generation=10**6)
        self.assertEqual(gnome_keys.read_binding("screenshot_window"), "")
        # Более старая запись после новой пропускается.
        gnome_keys.set_bindings({"screenshot_window": "Alt+Print"}, generation=1)
        self.assertEqual(gnome_keys.read_binding("screenshot_window"), "")
        gnome_keys.set_bindings({"screenshot_window": "Alt+Print"}, generation=10**6 + 1)
        self.assertEqual(gnome_keys.read_binding("screenshot_window"), "Alt+Print")


if __name__ == "__main__":
    unittest.main()
