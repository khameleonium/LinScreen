"""Проверки выбора набора модулей графической системы."""

from __future__ import annotations

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRect  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from backends import PLATFORM_VARIABLE, create_backend, platform_name  # noqa: E402
from core.session import DesktopSession, SessionType  # noqa: E402

_application = QApplication.instance() or QApplication([])


def session(kind: SessionType, desktop: str = "kde") -> DesktopSession:
    """Сведения о сессии заданного типа."""
    return DesktopSession(kind, desktop, "", "")


class PlatformNameTest(unittest.TestCase):
    """Выбор набора модулей по типу сессии и явному указанию."""

    def test_by_session_type(self) -> None:
        """X11 и Wayland выбираются по типу сессии."""
        with mock.patch.dict(os.environ, {PLATFORM_VARIABLE: ""}):
            self.assertEqual(platform_name(session(SessionType.X11)), "x11")
            self.assertEqual(platform_name(session(SessionType.WAYLAND)), "wayland")

    def test_forced(self) -> None:
        """Явное указание имеет преимущество перед типом сессии."""
        with mock.patch.dict(os.environ, {PLATFORM_VARIABLE: "x11"}):
            self.assertEqual(platform_name(session(SessionType.WAYLAND)), "x11")

    def test_command_line(self) -> None:
        """Ключ --platform передаётся через переменную окружения."""
        from main import apply_platform_argument

        with mock.patch.dict(os.environ, {PLATFORM_VARIABLE: ""}):
            apply_platform_argument(["main.py", "--platform", "wayland"])
            self.assertEqual(os.environ[PLATFORM_VARIABLE], "wayland")
            apply_platform_argument(["main.py", "--platform=x11"])
            self.assertEqual(os.environ[PLATFORM_VARIABLE], "x11")


class BackendTest(unittest.TestCase):
    """Создание наборов модулей и их общие действия."""

    def test_create(self) -> None:
        """Для каждой системы создаётся свой набор модулей."""
        with mock.patch.dict(os.environ, {PLATFORM_VARIABLE: ""}):
            self.assertEqual(create_backend(session(SessionType.X11)).name, "x11")
            self.assertEqual(create_backend(session(SessionType.WAYLAND)).name, "wayland")

    def test_object_detection(self) -> None:
        """Окна под указателем находятся в X11 всегда, в Wayland - только в KDE."""
        with mock.patch.dict(os.environ, {PLATFORM_VARIABLE: ""}):
            self.assertTrue(create_backend(session(SessionType.X11)).detects_objects)
            self.assertTrue(create_backend(session(SessionType.WAYLAND, "kde")).detects_objects)
            self.assertFalse(
                create_backend(session(SessionType.WAYLAND, "gnome")).detects_objects
            )

    def test_x11_video_source(self) -> None:
        """Источник X11 сразу отдаёт вход x11grab нужного размера."""
        from backends.x11 import X11VideoSource

        source = X11VideoSource()
        received: list[object] = []
        source.ready.connect(received.append)
        with mock.patch.dict(os.environ, {"DISPLAY": ":0"}):
            source.open(QRect(10, 20, 301, 201), 30, True)
        _application.processEvents()
        self.assertEqual(len(received), 1)
        video_input = received[0]
        self.assertIn("x11grab", getattr(video_input, "args"))
        self.assertEqual(source.recorded_rect, QRect(10, 20, 301, 201))
        self.assertIsNone(source.feeder)

    def test_recording_possible(self) -> None:
        """Условия записи различаются по системам."""
        with mock.patch.dict(os.environ, {PLATFORM_VARIABLE: ""}):
            x11 = create_backend(session(SessionType.X11))
            wayland = create_backend(session(SessionType.WAYLAND))
        capabilities = mock.Mock(can_capture_x11=False, can_read_screen_stream=True)
        self.assertFalse(x11.recording_possible("/usr/bin/ffmpeg", capabilities))
        self.assertTrue(wayland.recording_possible("/usr/bin/ffmpeg", capabilities))
        self.assertFalse(wayland.recording_possible("", capabilities))


if __name__ == "__main__":
    unittest.main()


class UninstallTest(unittest.TestCase):
    """Удаление следов программы из системы."""

    def test_removes_files_and_keeps_program(self) -> None:
        """Ярлыки, автозапуск и значки удаляются; с purge - и настройки."""
        import tempfile
        from pathlib import Path

        import core.uninstall as module

        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            env = {
                "XDG_DATA_HOME": str(base / "data"),
                "XDG_CONFIG_HOME": str(base / "config"),
                "XDG_CACHE_HOME": str(base / "cache"),
            }
            files = [
                base / "data/applications/io.github.khameleonium.LinScreen.desktop",
                base / "data/applications/io.github.khameleonium.LinScreenWayland.desktop",
                base / "config/autostart/io.github.khameleonium.LinScreen.desktop",
                base / "data/icons/hicolor/256x256/apps/io.github.khameleonium.LinScreen.png",
                base / "config/linscreen/config.ini",
            ]
            for path in files:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("x")
            with mock.patch.dict(os.environ, env), mock.patch.object(
                module, "_remove_shortcuts"
            ), mock.patch.object(module, "_remove_permissions"):
                removed = module.uninstall(purge=False)
                self.assertEqual(len(removed), 4)
                self.assertTrue(files[-1].exists())
                module.uninstall(purge=True)
                self.assertFalse(files[-1].exists())
