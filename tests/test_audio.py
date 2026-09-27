"""Проверки разбора перечня звуковых источников и сборки аудиовходов."""

from __future__ import annotations

import unittest

from capture.audio import (
    AudioDevice,
    _parse_json_sources,
    _parse_short_sources,
    _parse_verbose_sources,
    build_audio_input,
    default_microphone,
    default_system_source,
    resolve_audio_inputs,
)
from encoder.profiles import AudioMode, AudioRole

VERBOSE_OUTPUT = """Source #51
\tState: SUSPENDED
\tName: alsa_output.pci-0000_00_1f.3.analog-stereo.monitor
\tDescription: Monitor of Built-in Audio Аналоговый стерео
\tMonitor of Sink: alsa_output.pci-0000_00_1f.3.analog-stereo
Source #52
\tState: RUNNING
\tName: alsa_input.pci-0000_00_1f.3.analog-stereo
\tDescription: Built-in Audio Аналоговый стерео
\tMonitor of Sink: n/a
"""

# Часть сборок pactl не выводит не-ASCII строки в формате JSON и
# подставляет вместо описания заглушку.
JSON_OUTPUT = """[
  {"name": "alsa_output.x.monitor", "description": "(null)",
   "monitor_of_sink": null, "properties": {"device.description": "Встроенное аудио"}},
  {"name": "alsa_input.x", "description": "Микрофон", "monitor_of_sink": null,
   "properties": {}}
]"""

SHORT_OUTPUT = "51\talsa_output.x.monitor\tPipeWire\ts16le 2ch\tSUSPENDED\n"


class SourceParsingTest(unittest.TestCase):
    """Разбор трёх форматов вывода утилиты pactl."""

    def test_verbose_keeps_localised_names(self) -> None:
        """Подробный вывод сохраняет локализованные названия устройств."""
        devices = _parse_verbose_sources(
            VERBOSE_OUTPUT, "alsa_input.pci-0000_00_1f.3.analog-stereo"
        )
        self.assertEqual(len(devices), 2)
        self.assertIn("Аналоговый стерео", devices[0].description)

    def test_monitor_is_detected_by_linked_sink(self) -> None:
        """Монитор распознаётся по связанному устройству вывода."""
        devices = _parse_verbose_sources(VERBOSE_OUTPUT, "")
        self.assertTrue(devices[0].is_monitor)
        self.assertFalse(devices[1].is_monitor)

    def test_default_flag_is_applied(self) -> None:
        """Устройство по умолчанию помечается в перечне."""
        devices = _parse_verbose_sources(
            VERBOSE_OUTPUT, "alsa_input.pci-0000_00_1f.3.analog-stereo"
        )
        self.assertTrue(devices[1].is_default)
        self.assertIn("по умолчанию", devices[1].title)

    def test_json_falls_back_to_properties(self) -> None:
        """Заглушка вместо описания заменяется свойством устройства."""
        devices = _parse_json_sources(JSON_OUTPUT, "")
        self.assertEqual(devices[0].description, "Встроенное аудио")

    def test_json_detects_monitor_by_suffix(self) -> None:
        """Монитор распознаётся по суффиксу имени, если связь не указана."""
        devices = _parse_json_sources(JSON_OUTPUT, "")
        self.assertTrue(devices[0].is_monitor)
        self.assertFalse(devices[1].is_monitor)

    def test_short_output_is_supported(self) -> None:
        """Краткий табличный вывод разбирается по второму столбцу."""
        devices = _parse_short_sources(SHORT_OUTPUT, "")
        self.assertEqual(devices[0].name, "alsa_output.x.monitor")
        self.assertTrue(devices[0].is_monitor)


class InputSelectionTest(unittest.TestCase):
    """Подбор входов под выбранную схему работы со звуком."""

    def setUp(self) -> None:
        """Типовой набор из монитора и микрофона."""
        self.devices = [
            AudioDevice("monitor.a", "Системный звук", is_monitor=True),
            AudioDevice("mic.a", "Микрофон", is_monitor=False, is_default=True),
        ]

    def test_none_selects_nothing(self) -> None:
        """Без звука входы не собираются."""
        self.assertEqual(resolve_audio_inputs(AudioMode.NONE, self.devices), [])

    def test_system_selects_monitor(self) -> None:
        """Системный звук берётся с монитора устройства вывода."""
        inputs = resolve_audio_inputs(AudioMode.SYSTEM, self.devices)
        self.assertEqual(len(inputs), 1)
        self.assertIs(inputs[0].role, AudioRole.SYSTEM)

    def test_microphone_selects_input(self) -> None:
        """Режим микрофона выбирает устройство ввода."""
        inputs = resolve_audio_inputs(AudioMode.MICROPHONE, self.devices)
        self.assertIs(inputs[0].role, AudioRole.MICROPHONE)

    def test_separate_selects_both(self) -> None:
        """Раздельные дорожки требуют обоих источников."""
        inputs = resolve_audio_inputs(AudioMode.SEPARATE, self.devices)
        self.assertEqual(len(inputs), 2)

    def test_named_device_has_priority(self) -> None:
        """Устройство, выбранное в настройках, имеет приоритет."""
        devices = self.devices + [AudioDevice("monitor.b", "Второй выход", is_monitor=True)]
        inputs = resolve_audio_inputs(AudioMode.SYSTEM, devices, system_name="monitor.b")
        self.assertIn("monitor.b", inputs[0].args)

    def test_missing_device_falls_back_to_default(self) -> None:
        """Пропавшее устройство заменяется выбранным в системе."""
        inputs = resolve_audio_inputs(AudioMode.SYSTEM, self.devices, system_name="исчезло")
        self.assertIn("monitor.a", inputs[0].args)

    def test_empty_device_list_gives_no_inputs(self) -> None:
        """Отсутствие звуковых устройств не приводит к исключению."""
        self.assertEqual(resolve_audio_inputs(AudioMode.SEPARATE, []), [])

    def test_queue_size_is_enlarged(self) -> None:
        """Очередь пакетов увеличена: иначе теряется звук при записи видео."""
        args = list(build_audio_input(self.devices[0]).args)
        self.assertIn("-thread_queue_size", args)

    def test_fallback_device_is_offered(self) -> None:
        """Без утилиты pactl предлагается условный источник по умолчанию."""
        from capture.audio import DEFAULT_SOURCE_NAME, fallback_devices

        devices = fallback_devices()
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0].name, DEFAULT_SOURCE_NAME)
        # Монитор системного звука таким способом получить нельзя.
        self.assertFalse(devices[0].is_monitor)

    def test_defaults_are_found(self) -> None:
        """Источники по умолчанию определяются по типу устройства."""
        system = default_system_source(self.devices)
        microphone = default_microphone(self.devices)
        self.assertIsNotNone(system)
        self.assertIsNotNone(microphone)
        assert system is not None and microphone is not None
        self.assertEqual(system.name, "monitor.a")
        self.assertEqual(microphone.name, "mic.a")


if __name__ == "__main__":
    unittest.main()
