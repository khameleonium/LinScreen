"""
Проверки модулей, специфичных для Wayland.

Проверяется логика, не требующая живого композитора: разбор значений
шины D-Bus, расчёт вырезаемой области видеопотока, сборка команд
GStreamer и FFmpeg, преобразование сочетаний клавиш и перечня окон.
"""

from __future__ import annotations

import os
import tempfile
import threading
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QRect  # noqa: E402
from PySide6.QtGui import QGuiApplication, QImage  # noqa: E402

from backends.wayland import screencast  # noqa: E402
from backends.wayland.windows import parse_windows  # noqa: E402
from capture.windows import object_at  # noqa: E402
from core import bus_service, portal  # noqa: E402
from backends.wayland.hotkeys import key_code, kwin_variants, to_portal_trigger  # noqa: E402
from encoder.process import release_fifo_reader  # noqa: E402

_application = QGuiApplication.instance() or QGuiApplication([])


class ConvertTest(unittest.TestCase):
    """Приведение значений шины к обычным типам Python."""

    def test_vardict_unwraps_variants(self) -> None:
        """Варианты словаря a{sv} разворачиваются в значения."""
        raw = {"uri": ("s", "file:///tmp/a.png"), "count": ("u", 3)}
        self.assertEqual(portal.vardict(raw), {"uri": "file:///tmp/a.png", "count": 3})

    def test_nested_streams(self) -> None:
        """Перечень потоков a(ua{sv}) разворачивается вместе со свойствами."""
        raw = {
            "streams": (
                "a(ua{sv})",
                [(82, {"size": ("(ii)", (1920, 1080)), "source_type": ("u", 1)})],
            )
        }
        converted = portal.vardict(raw)
        self.assertEqual(
            converted["streams"], [(82, {"size": (1920, 1080), "source_type": 1})]
        )

    def test_struct_with_string_is_not_variant(self) -> None:
        """Структура со строковым первым полем не принимается за вариант."""
        value = portal.convert("(ss)", ("a", "b"))
        self.assertEqual(value, ("a", "b"))

    def test_split_types(self) -> None:
        """Сигнатура делится на полные типы верхнего уровня."""
        self.assertEqual(portal._split_types("sa{sv}(ii)u"), ["s", "a{sv}", "(ii)", "u"])


class CropPlanTest(unittest.TestCase):
    """Расчёт вырезаемой области потока монитора."""

    def test_region_inside_monitor(self) -> None:
        """Область переводится в точки потока, стороны становятся чётными."""
        plan = screencast.plan_crop(QRect(400, 150, 801, 501), QRect(0, 0, 1920, 1080), 1920, 1080)
        self.assertEqual((plan.left, plan.top, plan.width, plan.height), (400, 150, 800, 500))
        self.assertEqual(plan.right, 1920 - 400 - 800)
        self.assertEqual(plan.bottom, 1080 - 150 - 500)

    def test_second_monitor_offset(self) -> None:
        """Координаты отсчитываются от начала монитора, а не рабочего стола."""
        plan = screencast.plan_crop(
            QRect(2000, 100, 400, 300), QRect(1920, 0, 1920, 1080), 1920, 1080
        )
        self.assertEqual((plan.left, plan.top), (80, 100))

    def test_scaled_monitor(self) -> None:
        """При масштабе 2 область и поток вдвое больше логических размеров."""
        plan = screencast.plan_crop(QRect(10, 20, 100, 50), QRect(0, 0, 1280, 720), 2560, 1440)
        self.assertEqual((plan.source_width, plan.source_height), (2560, 1440))
        self.assertEqual((plan.left, plan.top, plan.width, plan.height), (20, 40, 200, 100))

    def test_fractional_scale(self) -> None:
        """Масштаб 1,25: логические 1536×864 при потоке 1920×1080."""
        plan = screencast.plan_crop(
            QRect(400, 240, 321, 241), QRect(0, 0, 1536, 864), 1920, 1080
        )
        self.assertEqual((plan.left, plan.top), (500, 300))
        self.assertEqual((plan.width, plan.height), (400, 300))
        self.assertLessEqual(plan.left + plan.width, plan.source_width)

    def test_region_clipped_to_monitor(self) -> None:
        """Часть области за пределами монитора отбрасывается."""
        plan = screencast.plan_crop(QRect(1800, 900, 400, 400), QRect(0, 0, 1920, 1080), 1920, 1080)
        self.assertEqual((plan.width, plan.height), (120, 180))
        self.assertEqual((plan.right, plan.bottom), (0, 0))


class CommandTest(unittest.TestCase):
    """Сборка команд поставщика кадров и входа FFmpeg."""

    def setUp(self) -> None:
        self.crop = screencast.plan_crop(QRect(0, 0, 640, 480), QRect(0, 0, 1920, 1080), 1920, 1080)
        self.fifo = Path("/run/user/1000/linscreen-x/frames.fifo")

    def test_gstreamer_command(self) -> None:
        """Команда содержит соединение, узел, вырезание и постоянную частоту."""
        command = screencast.build_gstreamer_command(7, 82, self.crop, 30, self.fifo)
        self.assertEqual(command[0], "gst-launch-1.0")
        self.assertIn("fd=7", command)
        self.assertIn("path=82", command)
        self.assertIn("right=1280", command)
        self.assertIn("video/x-raw,format=BGRx,width=1920,height=1080", command)
        self.assertIn("video/x-raw,format=BGRx,width=640,height=480", command)
        # Приведение к полному размеру предшествует вырезанию.
        self.assertLess(command.index("videoscale"), command.index("videocrop"))
        self.assertIn("video/x-raw,framerate=30/1", command)
        self.assertIn(f"location={self.fifo}", command)

    def test_rawvideo_input(self) -> None:
        """Вход FFmpeg объявляет тот же размер и формат, что отдаёт GStreamer."""
        args = screencast.build_rawvideo_input(self.fifo, self.crop, 30, "1024")
        self.assertEqual(args[:2], ["-f", "rawvideo"])
        self.assertIn("640x480", args)
        self.assertIn("bgr0", args)
        self.assertEqual(args[-2:], ["-i", str(self.fifo)])


class FifoTest(unittest.TestCase):
    """Освобождение FFmpeg, ожидающего открытия канала."""

    def test_reader_is_released(self) -> None:
        """Читатель канала получает конец потока после освобождения."""
        with tempfile.TemporaryDirectory() as directory:
            fifo = Path(directory) / "frames.fifo"
            os.mkfifo(fifo)
            received: list[bytes] = []

            def reader() -> None:
                with open(fifo, "rb") as stream:
                    received.append(stream.read())

            thread = threading.Thread(target=reader)
            thread.start()
            # Читатель может ещё не успеть открыть канал: попытки повторяются.
            for _ in range(100):
                release_fifo_reader(fifo)
                thread.join(0.05)
                if not thread.is_alive():
                    break
            self.assertFalse(thread.is_alive())
            self.assertEqual(received, [b""])

    def test_without_reader_nothing_happens(self) -> None:
        """Без читателя освобождение ничего не делает и не ждёт."""
        with tempfile.TemporaryDirectory() as directory:
            fifo = Path(directory) / "frames.fifo"
            os.mkfifo(fifo)
            release_fifo_reader(fifo)


class HotkeyTest(unittest.TestCase):
    """Преобразование сочетаний клавиш."""

    def test_portal_trigger(self) -> None:
        """Сочетание приводится к формату спецификации XDG shortcuts."""
        self.assertEqual(to_portal_trigger("Ctrl+Alt+R"), "CTRL+ALT+r")
        self.assertEqual(to_portal_trigger("Shift+Print"), "SHIFT+Print")
        self.assertEqual(to_portal_trigger("Super+F5"), "LOGO+F5")
        self.assertEqual(to_portal_trigger(""), "")

    def test_key_code_roundtrip(self) -> None:
        """Пустое сочетание не имеет кода, непустое - имеет."""
        self.assertEqual(key_code(""), 0)
        self.assertNotEqual(key_code("Ctrl+Alt+J"), 0)

    def test_kwin_variants(self) -> None:
        """Для Shift с буквой и Print добавляется вариант, который видит KWin 5."""
        self.assertEqual(kwin_variants(key_code("Ctrl+Alt+Shift+J")), [key_code("Ctrl+Alt+J")])
        self.assertEqual(kwin_variants(key_code("Shift+Print")), [key_code("SysReq")])
        self.assertEqual(kwin_variants(key_code("Ctrl+Alt+R")), [])
        self.assertEqual(kwin_variants(key_code("Shift+F5")), [])

    def test_reserved_combinations(self) -> None:
        """Сочетания переключения терминалов не назначаются."""
        from ui.widgets.hotkey_edit import is_reserved

        self.assertTrue(is_reserved(["Ctrl", "Alt"], "F9"))
        self.assertTrue(is_reserved(["Ctrl", "Alt", "Shift"], "F1"))
        self.assertFalse(is_reserved(["Ctrl", "Shift"], "F9"))
        self.assertFalse(is_reserved(["Ctrl", "Alt"], "J"))


class HotkeyEditTest(unittest.TestCase):
    """Запись буквенных клавиш независимо от раскладки."""

    def test_scan_codes(self) -> None:
        """Буква определяется по положению клавиши, а не по символу."""
        from ui.widgets.hotkey_edit import _layout_independent_name

        # Скан-код J: код evdev 36 плюс смещение 8.
        self.assertEqual(_layout_independent_name(44), "J")
        self.assertEqual(_layout_independent_name(10), "1")
        self.assertEqual(_layout_independent_name(9), "")


class WindowsTest(unittest.TestCase):
    """Разбор перечня окон, полученного от композитора."""

    def test_parse_keeps_stacking_order(self) -> None:
        """Позиция в перечне становится порядком наложения."""
        objects = parse_windows([[0, 1036, 1920, 44], [100, 100, 800, 600], "bad", [0, 0, 5, 5]])
        self.assertEqual([item.group for item in objects], [0, 1])
        self.assertEqual(objects[1].rect, QRect(100, 100, 800, 600))

    def test_topmost_window_wins(self) -> None:
        """Под точкой выбирается верхнее из перекрывающихся окон."""
        objects = parse_windows([[0, 0, 1920, 1036], [300, 200, 600, 400]])
        self.assertEqual(object_at(objects, QPoint(400, 300)), QRect(300, 200, 600, 400))
        self.assertEqual(object_at(objects, QPoint(10, 10)), QRect(0, 0, 1920, 1036))

    def test_not_a_list(self) -> None:
        """Непонятный ответ композитора даёт пустой перечень."""
        self.assertEqual(parse_windows(None), [])


class ScreenTest(unittest.TestCase):
    """Вырезание области из снимка рабочего стола."""

    def test_crop_uses_real_image_scale(self) -> None:
        """Масштаб берётся по фактическому размеру снимка."""
        from capture.screen import crop_desktop_image, virtual_geometry

        area = virtual_geometry()
        desktop = QImage(area.width() * 2, area.height() * 2, QImage.Format.Format_ARGB32)
        part = crop_desktop_image(desktop, QRect(area.x() + 10, area.y() + 10, 50, 40))
        self.assertEqual((part.width(), part.height()), (100, 80))


class CommandLineTest(unittest.TestCase):
    """Действия, передаваемые ключами командной строки."""

    def test_requested_action(self) -> None:
        """Ключ превращается в имя действия, прочие аргументы не мешают."""
        from main import requested_action

        self.assertEqual(requested_action(["--screenshot"]), "screenshot_region")
        self.assertEqual(requested_action(["-v", "--record"]), "record_toggle")
        self.assertIsNone(requested_action(["--unknown"]))


class ReportTest(unittest.TestCase):
    """Передача отчётов скриптов композитора ожидающему потоку."""

    def test_report_delivered_by_token(self) -> None:
        """Отчёт попадает только к ожидающему с той же меткой."""
        waiter = bus_service.expect_report("abc")
        bus_service._deliver_report("other", "[]")
        self.assertTrue(waiter.empty())
        bus_service._deliver_report("abc", "[1]")
        self.assertEqual(waiter.get_nowait(), "[1]")
        bus_service.forget_report("abc")


class DesktopEntryTest(unittest.TestCase):
    """Ярлык приложения, обязательный для порталов."""

    def test_hidden_entry(self) -> None:
        """Скрытый ярлык помечается NoDisplay и содержит класс окна."""
        from core.autostart import desktop_entry
        from core.identity import APPLICATION_ID

        text = desktop_entry(autostart=False, hidden=True)
        self.assertIn("NoDisplay=true", text)
        self.assertIn(f"StartupWMClass={APPLICATION_ID}", text)
        self.assertNotIn("NoDisplay", desktop_entry(autostart=False))


if __name__ == "__main__":
    unittest.main()


class SnapshotTest(unittest.TestCase):
    """Сборка снимка без указателя из кадров видеопотока."""

    def setUp(self) -> None:
        from PySide6.QtGui import QColor

        # Два монитора: слева 200x100 (масштаб 1), справа 100x50 логических
        # точек с кадром 200x100 (масштаб 2). Снимок портала - в масштабе 2.
        self.screens = [QRect(0, 0, 200, 100), QRect(200, 0, 100, 50)]
        self.desktop = QRect(0, 0, 300, 100)
        self.left = QImage(200, 100, QImage.Format.Format_ARGB32)
        self.left.fill(QColor(200, 30, 30))
        self.right = QImage(200, 100, QImage.Format.Format_ARGB32)
        self.right.fill(QColor(30, 30, 200))
        self.base = QImage(600, 200, QImage.Format.Format_ARGB32)
        self.base.fill(QColor(0, 0, 0))
        from PySide6.QtGui import QPainter

        painter = QPainter(self.base)
        painter.drawImage(QRect(0, 0, 400, 200), self.left)
        painter.drawImage(QRect(400, 0, 200, 100), self.right)
        painter.end()

    def test_match_by_content(self) -> None:
        """Кадры сопоставляются мониторам по содержимому, а не по порядку."""
        from backends.wayland.snapshot import match_frames, monitor_regions

        regions = monitor_regions(self.base, self.screens, self.desktop)
        self.assertEqual(regions, [QRect(0, 0, 400, 200), QRect(400, 0, 200, 100)])
        pairs = match_frames(self.base, [self.right, self.left], regions)
        self.assertEqual(sorted(pairs), [(0, 1), (1, 0)])

    def test_compose_frames_uses_largest_scale(self) -> None:
        """Снимок из кадров собирается в наибольшем масштабе мониторов."""
        from backends.wayland.snapshot import compose_frames

        image = compose_frames([self.right, self.left], [1, 0], self.screens, self.desktop)
        self.assertEqual((image.width(), image.height()), (600, 200))
        self.assertEqual(image.pixelColor(10, 10).blue(), 30)
        self.assertEqual(image.pixelColor(450, 10).blue(), 200)
        # Участок ниже правого монитора ничем не покрыт и остаётся чёрным.
        self.assertEqual(image.pixelColor(450, 150).red(), 0)

    def test_crop_native_keeps_resolution(self) -> None:
        """Область на одном мониторе вырезается из его родного кадра."""
        from backends.wayland.snapshot import SnapshotResult, crop_native

        result = SnapshotResult(
            self.base, 2, native=[(self.screens[0], self.left), (self.screens[1], self.right)]
        )
        part = crop_native(result, QRect(210, 10, 50, 20))
        assert part is not None
        self.assertEqual((part.width(), part.height()), (100, 40))
        # Область через границу мониторов из родного кадра не вырезается.
        self.assertIsNone(crop_native(result, QRect(150, 10, 100, 20)))


class SnapshotProportionsTest(unittest.TestCase):
    """Кадр монитора не сопоставляется всему рабочему столу."""

    def test_frame_never_matches_whole_desktop(self) -> None:
        """Два одинаковых монитора рядом: кадр 16:9 не подходит столу 32:9."""
        from PySide6.QtGui import QColor

        from backends.wayland.snapshot import match_frames

        base = QImage(3840, 1080, QImage.Format.Format_ARGB32)
        base.fill(QColor(10, 10, 40))
        frame = QImage(1920, 1080, QImage.Format.Format_ARGB32)
        frame.fill(QColor(10, 10, 40))
        regions = [QRect(0, 0, 1920, 1080), QRect(1920, 0, 1920, 1080), base.rect()]
        pairs = match_frames(base, [frame, frame], regions)
        self.assertNotIn(2, [region for _frame, region in pairs])
        self.assertEqual(len(pairs), 2)


class ForeignChangeTest(unittest.TestCase):
    """Разбор сочетаний из сигналов службы сочетаний KDE."""

    def test_both_formats(self) -> None:
        """Список кодов и список последовательностей дают одно сочетание."""
        from backends.wayland.hotkeys import first_key_text

        code = key_code("Alt+Shift+W")
        self.assertEqual(first_key_text([code]), "Alt+Shift+W")
        self.assertEqual(first_key_text([([code, 0, 0, 0],)]), "Alt+Shift+W")
        self.assertEqual(first_key_text([(code, 0, 0, 0)]), "Alt+Shift+W")
        self.assertEqual(first_key_text([]), "")
