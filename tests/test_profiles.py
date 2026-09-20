"""Проверки сборки аргументов командной строки FFmpeg."""

from __future__ import annotations

import unittest
from pathlib import Path

from encoder.profiles import (
    AudioInput,
    AudioMode,
    AudioRole,
    Container,
    VideoCodec,
    VideoInput,
    VideoProfileManager,
)

# Полный набор энкодеров типовой сборки FFmpeg.
FULL_ENCODERS = frozenset(
    {
        "libx264", "libx265", "libsvtav1", "libvpx-vp9", "ffv1", "prores_ks",
        "mpeg4", "libopus", "aac", "flac", "pcm_s16le", "libmp3lame",
        "libwebp_anim", "apng", "gif",
    }
)


class ProfileCommandTest(unittest.TestCase):
    """Сборка команд записи для различных сочетаний настроек."""

    def setUp(self) -> None:
        """Подготовка менеджера профилей и типовых входов."""
        self.manager = VideoProfileManager(
            ffmpeg_path="/usr/bin/ffmpeg", available_encoders=FULL_ENCODERS
        )
        self.video = VideoInput(
            args=["-f", "x11grab", "-i", ":0+0,0"], fps=30, needs_even_padding=False
        )
        self.audio = (
            AudioInput(args=["-f", "pulse", "-i", "monitor"], role=AudioRole.SYSTEM),
            AudioInput(args=["-f", "pulse", "-i", "mic"], role=AudioRole.MICROPHONE),
        )

    def build(
        self,
        identifier: str,
        *,
        audio: tuple[AudioInput, ...] = (),
        mode: AudioMode = AudioMode.NONE,
        name: str = "out",
    ) -> list[str]:
        """Сборка команды записи для указанного профиля."""
        profile = self.manager.video_profile(identifier)
        return self.manager.build_record_command(
            profile,
            self.video,
            Path(f"/tmp/{name}.{profile.container.extension}"),
            audio,
            mode,
        )

    def test_stdin_remains_open_for_graceful_stop(self) -> None:
        """Флаг -nostdin недопустим: по каналу ввода передаётся команда остановки."""
        self.assertNotIn("-nostdin", self.build("mkv_h264"))

    def test_separate_tracks_are_mapped_with_titles(self) -> None:
        """Режим раздельных дорожек отображает оба входа и подписывает их."""
        command = self.build("mkv_h264", audio=self.audio, mode=AudioMode.SEPARATE)
        self.assertEqual(command.count("-map"), 3)
        self.assertIn("1:a", command)
        self.assertIn("2:a", command)
        self.assertIn("title=System Audio", command)
        self.assertIn("title=Microphone", command)
        # Сведение не применяется: дорожки остаются независимыми.
        self.assertNotIn("-filter_complex", command)

    def test_separate_tracks_fall_back_to_mix_in_webm(self) -> None:
        """Контейнер без многодорожечности получает сведённый звук."""
        command = self.build("webm_vp9", audio=self.audio, mode=AudioMode.SEPARATE)
        self.assertIn("-filter_complex", command)
        graph = command[command.index("-filter_complex") + 1]
        self.assertIn("amix=inputs=2", graph)
        # Компенсация расхождения тактовых генераторов источников.
        self.assertIn("aresample=async=1", graph)

    def test_mix_preserves_source_loudness(self) -> None:
        """Сведение не делит громкость на число источников."""
        command = self.build("mkv_h264", audio=self.audio, mode=AudioMode.MIX)
        graph = command[command.index("-filter_complex") + 1]
        self.assertIn("normalize=0", graph)

    def test_mp4_receives_faststart(self) -> None:
        """Контейнер MP4 всегда получает перенос индекса в начало файла."""
        command = self.build("mp4_h264")
        self.assertEqual(command[command.index("-movflags") + 1], "+faststart")

    def test_hevc_in_mp4_gets_compatibility_tag(self) -> None:
        """H.265 в MP4 помечается тегом hvc1 ради совместимости плееров."""
        command = self.build("mp4_h265")
        self.assertIn("-tag:v", command)
        self.assertIn("hvc1", command)

    def test_audio_disabled_when_mode_is_none(self) -> None:
        """Без звука входы игнорируются и добавляется явный запрет дорожки."""
        command = self.build("mkv_h264", audio=self.audio, mode=AudioMode.NONE)
        self.assertIn("-an", command)
        self.assertNotIn("monitor", command)

    def test_single_source_mode_uses_first_input_only(self) -> None:
        """Режим одного источника подключает ровно одну дорожку."""
        command = self.build("mkv_h264", audio=self.audio, mode=AudioMode.SYSTEM)
        self.assertEqual(command.count("-map"), 2)

    def test_output_path_is_passed_as_single_argument(self) -> None:
        """Путь с пробелами и кавычками передаётся одним элементом списка."""
        profile = self.manager.video_profile("mkv_h264")
        path = Path("/tmp/каталог с пробелом/Запись 'важная'.mkv")
        command = self.manager.build_record_command(profile, self.video, path)
        self.assertEqual(command[-1], str(path))

    def test_lossless_profile_has_every_frame_as_key(self) -> None:
        """Профиль без потерь пишет каждый кадр ключевым ради устойчивости."""
        command = self.build("mkv_ffv1")
        self.assertIn("-slicecrc", command)
        self.assertEqual(command[command.index("-g") + 1], "1")


class AnimationCommandTest(unittest.TestCase):
    """Сборка заданий для анимированных форматов."""

    def setUp(self) -> None:
        """Подготовка менеджера профилей."""
        self.manager = VideoProfileManager(available_encoders=FULL_ENCODERS)

    def test_gif_uses_two_passes_with_palette(self) -> None:
        """GIF собирается двумя проходами через палитру."""
        steps = self.manager.build_animation_steps(
            self.manager.animation_profile("gif"), Path("/tmp/src.mkv"), Path("/tmp/out.gif")
        )
        self.assertEqual(len(steps), 2)
        self.assertIn("palettegen", " ".join(steps[0].args))
        self.assertIn("paletteuse", " ".join(steps[1].args))

    def test_palette_is_marked_temporary(self) -> None:
        """Палитра первого прохода помечается к удалению в обоих шагах."""
        steps = self.manager.build_animation_steps(
            self.manager.animation_profile("gif"), Path("/tmp/src.mkv"), Path("/tmp/out.gif")
        )
        self.assertEqual(steps[0].temporary, steps[1].temporary)
        self.assertEqual(steps[0].temporary[0].suffix, ".png")

    def test_animation_never_upscales(self) -> None:
        """Кадр анимации не растягивается выше исходного размера."""
        steps = self.manager.build_animation_steps(
            self.manager.animation_profile("gif"), Path("/tmp/src.mkv"), Path("/tmp/out.gif")
        )
        self.assertIn("min(iw", " ".join(steps[0].args))

    def test_apng_is_saved_with_png_extension(self) -> None:
        """Анимированный PNG сохраняется с общеупотребимым расширением."""
        self.assertEqual(Container.APNG.extension, "png")
        self.assertEqual(Container.APNG.muxer, "apng")

    def test_intermediate_profile_is_lossless(self) -> None:
        """Промежуточная запись для анимации ведётся без потерь."""
        profile = self.manager.intermediate_profile()
        self.assertTrue(profile.video_codec.is_lossless)


class CapabilityTest(unittest.TestCase):
    """Учёт возможностей установленной сборки FFmpeg."""

    def test_missing_encoder_falls_back(self) -> None:
        """Отсутствие энкодера приводит к подбору доступной замены."""
        limited = VideoProfileManager(available_encoders=frozenset({"libx264", "aac"}))
        resolved = limited.resolve(limited.video_profile("mkv_av1"))
        self.assertIs(resolved.video_codec, VideoCodec.H264)
        self.assertEqual(resolved.audio_codec.encoder, "aac")

    def test_validation_reports_incompatible_combination(self) -> None:
        """Несовместимое сочетание контейнера и режима звука попадает в отчёт."""
        manager = VideoProfileManager(available_encoders=FULL_ENCODERS)
        problems = manager.validate(manager.video_profile("webm_vp9"), AudioMode.SEPARATE)
        self.assertTrue(any("дорож" in text for text in problems))

    def test_unknown_encoders_are_allowed_before_probe(self) -> None:
        """До опроса сборки ограничения не накладываются."""
        manager = VideoProfileManager()
        self.assertTrue(manager.is_encoder_available("libsvtav1"))


if __name__ == "__main__":
    unittest.main()
