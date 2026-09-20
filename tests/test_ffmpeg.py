"""Проверки разбора вывода FFmpeg."""

from __future__ import annotations

import unittest

from encoder.ffmpeg import (
    ProgressParser,
    format_timecode,
    looks_like_error,
    parse_demuxers,
    parse_encoders,
    parse_filters,
    parse_version,
    summarize_log,
)

ENCODERS_OUTPUT = """Encoders:
 V..... = Video
 A..... = Audio
 ------
 V....D libx264              libx264 H.264 / AVC
 V....D libx265              libx265 H.265 / HEVC
 A....D libopus              libopus Opus
 V..... ffv1                 FFmpeg video codec #1
"""

FORMATS_OUTPUT = """File formats:
 D. = Demuxing supported
 .E = Muxing supported
 --
 D  x11grab         X11 screen capture
 DE matroska,webm   Matroska
  D pulse           Pulse audio input
"""

# Вывод версий до восьмой: три символа в столбце признаков.
FILTERS_OUTPUT = """Filters:
  T.. = Timeline support
  ... palettegen        V->V       Find the optimal palette.
  ... paletteuse        VV->V      Use a palette.
  T.. pipewiregrab      |->V       Capture PipeWire.
"""

# Вывод восьмой версии: столбец признаков сокращён до двух символов.
FILTERS_OUTPUT_V8 = """Filters:
  T.. = Timeline support
  .S. = Slice threading
  V = Video input/output
  ------
 TS aap               AA->A      Apply Affine Projection algorithm.
 .. palettegen        V->V       Find the optimal palette for a stream.
 .. paletteuse        VV->V      Use a palette to downsample a stream.
"""


class OutputParsingTest(unittest.TestCase):
    """Разбор справочных таблиц FFmpeg."""

    def test_legend_is_not_taken_for_encoder(self) -> None:
        """Строки легенды не попадают в перечень энкодеров."""
        encoders = parse_encoders(ENCODERS_OUTPUT)
        self.assertEqual(encoders, frozenset({"libx264", "libx265", "libopus", "ffv1"}))

    def test_format_aliases_are_expanded(self) -> None:
        """Перечисление форматов через запятую разворачивается в элементы."""
        formats = parse_demuxers(FORMATS_OUTPUT)
        self.assertIn("matroska", formats)
        self.assertIn("webm", formats)
        self.assertIn("x11grab", formats)
        self.assertIn("pulse", formats)

    def test_filters_are_recognised(self) -> None:
        """Фильтры палитры и захвата PipeWire распознаются."""
        filters = parse_filters(FILTERS_OUTPUT)
        self.assertEqual(filters, frozenset({"palettegen", "paletteuse", "pipewiregrab"}))

    def test_filters_of_eighth_version(self) -> None:
        """Сокращённый столбец признаков восьмой версии распознаётся."""
        filters = parse_filters(FILTERS_OUTPUT_V8)
        self.assertIn("palettegen", filters)
        self.assertIn("paletteuse", filters)
        self.assertIn("aap", filters)
        # Строки легенды и разделитель в перечень не попадают.
        self.assertNotIn("=", filters)
        self.assertNotIn("------", filters)

    def test_version_is_extracted(self) -> None:
        """Номер версии извлекается из первой строки вывода."""
        self.assertEqual(
            parse_version("ffmpeg version 6.1.1-3ubuntu5 Copyright (c) 2000-2023"),
            "6.1.1-3ubuntu5",
        )

    def test_unknown_version_does_not_raise(self) -> None:
        """Неожиданный вывод не приводит к исключению."""
        self.assertEqual(parse_version("нечто постороннее"), "unknown")


class ProgressParsingTest(unittest.TestCase):
    """Разбор потока прогресса, приходящего частями."""

    def test_block_split_across_chunks(self) -> None:
        """Блок, разорванный на границе порции, собирается целиком."""
        parser = ProgressParser()
        self.assertEqual(parser.feed("frame=120\nfps=30.0\nout_time_us=4000000\nprogre"), [])
        reports = parser.feed("ss=continue\n")
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0].frame, 120)
        self.assertAlmostEqual(reports[0].seconds, 4.0)
        self.assertFalse(reports[0].is_final)

    def test_final_report_is_marked(self) -> None:
        """Завершающий блок помечается особым признаком."""
        parser = ProgressParser()
        reports = parser.feed("out_time_us=1000000\nprogress=end\n")
        self.assertTrue(reports[0].is_final)

    def test_unavailable_values_do_not_break_parsing(self) -> None:
        """Значения N/A приводятся к нулю без исключения."""
        parser = ProgressParser()
        report = parser.feed("bitrate=N/A\nspeed=N/A\nframe=N/A\nprogress=continue\n")[0]
        self.assertEqual(report.frame, 0)
        self.assertEqual(report.bitrate_kbits, 0.0)

    def test_units_are_stripped(self) -> None:
        """Единицы измерения отбрасываются при разборе."""
        parser = ProgressParser()
        report = parser.feed("bitrate=2500.5kbits/s\nspeed=1.02x\nprogress=continue\n")[0]
        self.assertAlmostEqual(report.bitrate_kbits, 2500.5)
        self.assertAlmostEqual(report.speed, 1.02)

    def test_legacy_field_name_is_supported(self) -> None:
        """Устаревшее имя поля длительности обрабатывается наравне с новым."""
        parser = ProgressParser()
        report = parser.feed("out_time_ms=2500000\nprogress=continue\n")[0]
        self.assertAlmostEqual(report.seconds, 2.5)

    def test_timecode_formatting(self) -> None:
        """Длительность выводится в виде часов, минут и секунд."""
        self.assertEqual(format_timecode(3725), "01:02:05")
        self.assertEqual(format_timecode(-5), "00:00:00")


class LogAnalysisTest(unittest.TestCase):
    """Выделение причины сбоя из журнала кодировщика."""

    def test_error_lines_are_preferred(self) -> None:
        """В выжимку попадают строки с признаками ошибки."""
        summary = summarize_log(
            ["frame= 10", "Unknown encoder 'libsvtav1'", "Conversion failed!"]
        )
        self.assertIn("Unknown encoder", summary)
        self.assertNotIn("frame= 10", summary)

    def test_tail_is_used_without_errors(self) -> None:
        """При отсутствии явных ошибок берутся последние строки."""
        summary = summarize_log(["первая", "вторая", "третья"], limit=2)
        self.assertEqual(summary, "вторая\nтретья")

    def test_error_detection(self) -> None:
        """Распознавание строк с сообщениями об ошибке."""
        self.assertTrue(looks_like_error("Permission denied"))
        self.assertFalse(looks_like_error("frame=  42 fps=30"))


if __name__ == "__main__":
    unittest.main()
