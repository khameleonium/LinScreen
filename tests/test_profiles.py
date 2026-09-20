"""Проверки сборки аргументов командной строки FFmpeg."""

from __future__ import annotations

import unittest
from pathlib import Path

from encoder.profiles import (
    AudioCodec,
    AudioInput,
    AudioMode,
    AudioRole,
    Container,
    ProfileOverrides,
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


class OverrideTest(unittest.TestCase):
    """Наложение пользовательских уточнений на профиль."""

    def setUp(self) -> None:
        """Менеджер профилей и типовой видеовход."""
        self.manager = VideoProfileManager(available_encoders=FULL_ENCODERS)
        self.profile = self.manager.video_profile("mkv_h264")
        self.video = VideoInput(args=["-i", ":0"], fps=30, needs_even_padding=False)

    def build(self, overrides: ProfileOverrides) -> list[str]:
        """Команда записи с наложенными уточнениями."""
        profile = self.manager.apply_overrides(self.profile, overrides)
        return self.manager.build_record_command(profile, self.video, Path("/tmp/out.mkv"))

    def test_empty_overrides_keep_profile(self) -> None:
        """Пустой набор уточнений оставляет профиль неизменным."""
        self.assertIs(
            self.manager.apply_overrides(self.profile, ProfileOverrides()), self.profile
        )

    def test_codec_is_replaced(self) -> None:
        """Выбранный кодек заменяет заданный профилем."""
        command = self.build(ProfileOverrides(video_codec=VideoCodec.H265))
        self.assertIn("libx265", command)
        self.assertNotIn("libx264", command)

    def test_bitrate_mode_removes_constant_quality(self) -> None:
        """Заданный битрейт отменяет постоянное качество."""
        command = self.build(ProfileOverrides(rate_mode="bitrate", video_bitrate="9000k"))
        self.assertIn("-b:v", command)
        self.assertEqual(command[command.index("-b:v") + 1], "9000k")
        self.assertNotIn("-crf", command)

    def test_constant_quality_removes_bitrate(self) -> None:
        """Постоянное качество отменяет заданный битрейт."""
        command = self.build(ProfileOverrides(rate_mode="crf", crf=30))
        self.assertEqual(command[command.index("-crf") + 1], "30")
        self.assertNotIn("-b:v", command)

    def test_extra_arguments_are_split_by_shell_rules(self) -> None:
        """Строка дополнительных аргументов разбирается как командная строка."""
        command = self.build(
            ProfileOverrides(extra_args='-tune zerolatency -metadata comment="проба связи"')
        )
        self.assertIn("-tune", command)
        self.assertIn("zerolatency", command)
        # Значение в кавычках остаётся одним аргументом.
        self.assertIn("comment=проба связи", command)

    def test_audio_parameters_are_applied(self) -> None:
        """Уточнения звука попадают в команду."""
        profile = self.manager.apply_overrides(
            self.profile,
            ProfileOverrides(
                audio_codec=AudioCodec.FLAC, audio_sample_rate=44100, audio_channels=1
            ),
        )
        audio = (AudioInput(args=["-i", "src"], role=AudioRole.SYSTEM),)
        command = self.manager.build_record_command(
            profile, self.video, Path("/tmp/out.mkv"), audio, AudioMode.SYSTEM
        )
        self.assertIn("flac", command)
        self.assertEqual(command[command.index("-ar") + 1], "44100")
        self.assertEqual(command[command.index("-ac") + 1], "1")


class CustomCommandTest(unittest.TestCase):
    """Сборка команды по заданному пользователем образцу."""

    def setUp(self) -> None:
        """Менеджер профилей и подготовленные входы."""
        self.manager = VideoProfileManager(
            ffmpeg_path="/usr/bin/ffmpeg", available_encoders=FULL_ENCODERS
        )
        self.video = VideoInput(
            args=["-f", "x11grab", "-i", ":0+0,0"], fps=25, width=640, height=480
        )
        self.audio = (AudioInput(args=["-f", "pulse", "-i", "monitor"]),)

    def build(self, template: str, audio: tuple[AudioInput, ...] = ()) -> list[str]:
        """Команда, собранная по образцу."""
        return self.manager.build_custom_command(
            template, self.video, Path("/tmp/каталог с пробелом/итог.mkv"), audio
        )

    def test_placeholders_are_expanded(self) -> None:
        """Обозначения заменяются подготовленными аргументами."""
        command = self.build("{ffmpeg} {video_input} -c:v libx264 {output}")
        self.assertIn("x11grab", command)
        self.assertIn("libx264", command)
        self.assertEqual(command[0], "/usr/bin/ffmpeg")

    def test_output_path_stays_single_argument(self) -> None:
        """Путь с пробелами остаётся одним элементом списка."""
        command = self.build("{ffmpeg} {video_input} {output}")
        self.assertEqual(command[-1], "/tmp/каталог с пробелом/итог.mkv")

    def test_audio_placeholder_expands_all_sources(self) -> None:
        """Обозначение звука раскрывается во все выбранные источники."""
        command = self.build("{ffmpeg} {video_input} {audio_input} {output}", self.audio)
        self.assertIn("pulse", command)
        self.assertIn("monitor", command)

    def test_global_flags_are_forced(self) -> None:
        """Флаги прогресса и перезаписи добавляются независимо от образца."""
        command = self.build("{ffmpeg} {video_input} {output}")
        self.assertIn("-progress", command)
        self.assertIn("-y", command)
        # Запрет чтения ввода недопустим: по нему передаётся остановка.
        self.assertNotIn("-nostdin", command)

    def test_scalar_placeholders(self) -> None:
        """Отдельные значения захвата подставляются внутрь аргументов."""
        command = self.build("{ffmpeg} {video_input} -r {fps} -s {width}x{height} {output}")
        self.assertIn("25", command)
        self.assertIn("640x480", command)

    def test_missing_output_is_rejected(self) -> None:
        """Образец без обозначения файла отвергается."""
        with self.assertRaises(ValueError):
            self.build("{ffmpeg} {video_input} -c:v libx264 out.mkv")

    def test_binary_is_added_when_absent(self) -> None:
        """Образец без обозначения кодировщика получает его первым элементом."""
        command = self.build("{video_input} -c:v libx264 {output}")
        self.assertEqual(command[0], "/usr/bin/ffmpeg")

    def test_template_matches_profile(self) -> None:
        """Образец, созданный по профилю, повторяет его параметры."""
        profile = self.manager.video_profile("mp4_h264")
        template = self.manager.build_command_template(profile)
        self.assertIn("{video_input}", template)
        self.assertIn("{output}", template)
        command = self.manager.build_custom_command(
            template, self.video, Path("/tmp/out.mp4")
        )
        self.assertIn("libx264", command)
        self.assertIn("+faststart", command)


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
