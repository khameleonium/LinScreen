"""
Генератор профилей кодирования и аргументов командной строки FFmpeg.

Зона ответственности модуля:
    * описание поддерживаемых контейнеров, видео- и аудиокодеков;
    * сборка готовых списков аргументов для записи экрана;
    * сборка многошаговых заданий для анимированных форматов
      (двухпроходный GIF с палитрой, Animated WebP, APNG).

Принципы, заложенные в модуль:
    * Ни одна функция не запускает процессы и не обращается к диску, поэтому
      вызовы безопасны из GUI-потока. Реальный запуск выполняет encoder/process.py.
    * Аргументы всегда возвращаются списками строк. Склейка команды в одну
      строку через пробел не применяется, что исключает поломку на путях с
      пробелами, кавычками и кириллицей.
    * Внутрь графа фильтров пути не подставляются: палитра GIF передаётся
      обычным входом "-i", поэтому экранировать двоеточия и запятые,
      имеющие в filtergraph специальное значение, не требуется.
"""

from __future__ import annotations

from core.i18n import tr

import shlex
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Sequence

# Порядковый индекс входа с видео: видеопоток всегда подключается первым,
# аудиовходы нумеруются начиная с единицы.
VIDEO_INPUT_INDEX = 0


# ===========================================================================
# Перечисления предметной области
# ===========================================================================


class Container(str, Enum):
    """Поддерживаемые контейнеры и анимированные форматы вывода."""

    MKV = "mkv"
    MP4 = "mp4"
    WEBM = "webm"
    MOV = "mov"
    AVI = "avi"
    GIF = "gif"
    WEBP = "webp"
    APNG = "apng"

    @property
    def extension(self) -> str:
        """Расширение файла для сохранения."""
        # Анимированный PNG принято хранить с обычным расширением png:
        # браузеры и просмотрщики опознают анимацию по содержимому файла,
        # а расширение apng большинством программ не поддерживается.
        if self is Container.APNG:
            return "png"
        return self.value

    @property
    def muxer(self) -> str:
        """Имя мультиплексора FFmpeg для явного флага "-f"."""
        # Явное указание мультиплексора снимает зависимость от расширения
        # файла, которое пользователь может изменить в настройках.
        return {
            Container.MKV: "matroska",
            Container.MP4: "mp4",
            Container.WEBM: "webm",
            Container.MOV: "mov",
            Container.AVI: "avi",
            Container.GIF: "gif",
            Container.WEBP: "webp",
            Container.APNG: "apng",
        }[self]

    @property
    def is_animation(self) -> bool:
        """Признак безголосого анимированного формата."""
        return self in (Container.GIF, Container.WEBP, Container.APNG)

    @property
    def supports_multitrack_audio(self) -> bool:
        """Признак поддержки нескольких независимых аудиодорожек."""
        # Matroska и MOV/MP4 штатно хранят несколько дорожек с метаданными.
        # WebM на практике ограничен одной дорожкой, AVI многодорожечность
        # тянет плохо, поэтому для них выполняется автоматический откат к amix.
        return self in (Container.MKV, Container.MP4, Container.MOV)

    @property
    def survives_crash(self) -> bool:
        """Признак устойчивости к аварийному обрыву записи."""
        # Matroska пишется потоково и читается даже без финализации заголовка.
        return self is Container.MKV


class VideoCodec(str, Enum):
    """Видеокодеки, доступные для записи экрана."""

    H264 = "h264"
    H265 = "h265"
    AV1 = "av1"
    VP9 = "vp9"
    FFV1 = "ffv1"
    PRORES = "prores"
    MPEG4 = "mpeg4"
    # Кодеки анимированных форматов выделены отдельно: они не имеют
    # ни битрейта, ни пресетов в привычном понимании.
    GIF = "gif"
    WEBP_ANIM = "webp_anim"
    APNG = "apng"

    @property
    def encoder(self) -> str:
        """Имя энкодера FFmpeg, подставляемое в "-c:v"."""
        return {
            VideoCodec.H264: "libx264",
            VideoCodec.H265: "libx265",
            VideoCodec.AV1: "libsvtav1",
            VideoCodec.VP9: "libvpx-vp9",
            VideoCodec.FFV1: "ffv1",
            VideoCodec.PRORES: "prores_ks",
            VideoCodec.MPEG4: "mpeg4",
            VideoCodec.GIF: "gif",
            VideoCodec.WEBP_ANIM: "libwebp_anim",
            VideoCodec.APNG: "apng",
        }[self]

    @property
    def default_pix_fmt(self) -> str:
        """Формат пикселей по умолчанию для совместимости с плеерами."""
        return {
            VideoCodec.H264: "yuv420p",
            VideoCodec.H265: "yuv420p",
            VideoCodec.AV1: "yuv420p",
            VideoCodec.VP9: "yuv420p",
            # FFV1 хранит кадр без потерь, поэтому субдискретизация не нужна.
            VideoCodec.FFV1: "bgr0",
            VideoCodec.PRORES: "yuv422p10le",
            VideoCodec.MPEG4: "yuv420p",
            VideoCodec.GIF: "pal8",
            VideoCodec.WEBP_ANIM: "yuva420p",
            VideoCodec.APNG: "rgba",
        }[self]

    @property
    def is_lossless(self) -> bool:
        """Признак математически обратимого кодирования."""
        return self in (VideoCodec.FFV1, VideoCodec.APNG)


class AudioCodec(str, Enum):
    """Аудиокодеки, сочетаемые с поддерживаемыми контейнерами."""

    OPUS = "opus"
    AAC = "aac"
    FLAC = "flac"
    PCM = "pcm"
    MP3 = "mp3"

    @property
    def encoder(self) -> str:
        """Имя энкодера FFmpeg, подставляемое в "-c:a"."""
        return {
            AudioCodec.OPUS: "libopus",
            AudioCodec.AAC: "aac",
            AudioCodec.FLAC: "flac",
            AudioCodec.PCM: "pcm_s16le",
            AudioCodec.MP3: "libmp3lame",
        }[self]

    @property
    def uses_bitrate(self) -> bool:
        """Признак кодека с управлением через битрейт."""
        return self in (AudioCodec.OPUS, AudioCodec.AAC, AudioCodec.MP3)


class AudioRole(str, Enum):
    """Назначение аудиовхода, влияет на метаданные дорожки."""

    SYSTEM = "system"
    MICROPHONE = "microphone"


class AudioMode(str, Enum):
    """Схема работы со звуком, выбираемая пользователем перед стартом."""

    NONE = "none"
    SYSTEM = "system"
    MICROPHONE = "microphone"
    MIX = "mix"
    SEPARATE = "separate"

    @property
    def label(self) -> str:
        """Человекочитаемое название для выпадающего списка интерфейса."""
        return {
            AudioMode.NONE: tr("Без звука"),
            AudioMode.SYSTEM: tr("Системный звук"),
            AudioMode.MICROPHONE: tr("Микрофон"),
            AudioMode.MIX: tr("Микс обоих"),
            AudioMode.SEPARATE: tr("Раздельные дорожки"),
        }[self]


# ===========================================================================
# Структуры данных: входы и профили
# ===========================================================================


@dataclass(frozen=True)
class VideoInput:
    """
    Описание готового видеовхода FFmpeg.

    Аргументы формируются модулем capture/screen.py и приходят сюда уже
    собранными для прямого захвата X11 (x11grab).
    """

    # Полный список аргументов входа, включая "-f", параметры и "-i".
    args: Sequence[str]
    # Частота кадров источника: используется как ориентир для GOP и анимаций.
    fps: int = 30
    # Геометрия источника в пикселях, если она известна заранее.
    width: int | None = None
    height: int | None = None
    # Признак необходимости выравнивания сторон до чётных значений.
    # Выделенная мышью область почти всегда имеет нечётный размер, а
    # кодеки с субдискретизацией 4:2:0 такой кадр не принимают.
    needs_even_padding: bool = True


@dataclass(frozen=True)
class AudioInput:
    """Описание готового аудиовхода FFmpeg с метаданными дорожки."""

    # Полный список аргументов входа, например: -f pulse -i alsa_output...monitor
    args: Sequence[str]
    # Назначение источника, определяет заголовок дорожки в контейнере.
    role: AudioRole = AudioRole.SYSTEM
    # Заголовок дорожки, попадающий в метаданные контейнера.
    title: str = ""
    # Код языка по ISO 639-2, значение "und" означает "не определён".
    language: str = "und"

    @property
    def track_title(self) -> str:
        """Итоговый заголовок дорожки с подстановкой значения по умолчанию."""
        if self.title:
            return self.title
        return "System Audio" if self.role is AudioRole.SYSTEM else "Microphone"


@dataclass(frozen=True)
class VideoProfile:
    """Набор параметров кодирования одного видеопрофиля."""

    identifier: str
    title: str
    container: Container
    video_codec: VideoCodec
    audio_codec: AudioCodec = AudioCodec.OPUS

    # Управление качеством. Для CRF-кодеков задействуется crf, для ProRes и
    # MPEG-4 - qscale, для потоковых сценариев может задаваться битрейт.
    crf: int | None = 23
    qscale: int | None = None
    video_bitrate: str | None = None

    # Обобщённое имя пресета скорости. Преобразуется в числовые значения
    # для тех кодеков, которые не понимают словесных названий.
    preset: str = "medium"
    tune: str | None = None

    pix_fmt: str | None = None
    # Интервал ключевых кадров в кадрах. Нулевое значение оставляет решение
    # энкодеру. Короткий GOP облегчает перемотку и спасает битые файлы.
    keyint: int = 120
    force_cfr: bool = True

    audio_bitrate: str = "160k"
    audio_sample_rate: int = 48000
    audio_channels: int = 2

    # Дополнительные аргументы, добавляемые пользователем в настройках.
    extra_video_args: tuple[str, ...] = ()
    extra_output_args: tuple[str, ...] = ()

    @property
    def is_animation(self) -> bool:
        """Признак анимированного профиля без звуковой части."""
        return self.container.is_animation


@dataclass(frozen=True)
class AnimationProfile:
    """Параметры конвертации записи в анимированный формат."""

    identifier: str
    title: str
    container: Container
    video_codec: VideoCodec

    # Частота кадров результата: GIF редко имеет смысл выше 15-20 кадров.
    fps: int = 15
    # Ширина результата в пикселях, высота вычисляется пропорционально.
    # Значение None отключает масштабирование.
    width: int | None = 800
    # Число цветов палитры для GIF (максимум 256 по спецификации формата).
    max_colors: int = 256
    # Алгоритм псевдосмешивания цветов при наложении палитры.
    dither: str = "bayer"
    bayer_scale: int = 5
    # Режим сбора статистики палитры: diff учитывает только меняющиеся
    # области кадра, что заметно улучшает картинку экранных записей.
    palette_stats_mode: str = "diff"
    # Число повторов: ноль означает бесконечное зацикливание.
    loop: int = 0

    # Параметры, относящиеся к WebP и APNG.
    lossless: bool = False
    quality: int = 80
    compression_level: int = 5

    extra_output_args: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProfileOverrides:
    """
    Уточнения параметров профиля, заданные пользователем вручную.

    Пустое значение поля означает сохранение значения профиля, поэтому
    набор применяется поверх любого профиля без его переписывания.
    """

    video_codec: VideoCodec | None = None
    audio_codec: AudioCodec | None = None
    # Способ управления качеством: "crf" либо "bitrate".
    rate_mode: str = ""
    crf: int | None = None
    video_bitrate: str = ""
    preset: str = ""
    keyint: int | None = None
    pix_fmt: str = ""
    audio_bitrate: str = ""
    audio_sample_rate: int | None = None
    audio_channels: int | None = None
    # Дополнительные аргументы в виде строки командной оболочки.
    extra_args: str = ""

    @property
    def is_empty(self) -> bool:
        """Признак отсутствия каких-либо уточнений."""
        return self == ProfileOverrides()


@dataclass(frozen=True)
class FFmpegStep:
    """
    Один шаг задания FFmpeg.

    Многопроходные задания (например, GIF) описываются списком таких шагов,
    что позволяет контроллеру процесса отображать прогресс вида "проход 1 из 2"
    и убирать промежуточные файлы после завершения.
    """

    label: str
    args: list[str]
    # Файлы, подлежащие удалению после завершения всего задания.
    temporary: tuple[Path, ...] = ()

    @property
    def printable(self) -> str:
        """Безопасное для журнала строковое представление команды."""
        # Применяется исключительно для вывода в лог: на исполнение всегда
        # уходит исходный список аргументов без оболочки.
        return shlex.join(self.args)


# ===========================================================================
# Таблицы соответствий для кодеков с нестандартной шкалой пресетов
# ===========================================================================

# SVT-AV1 принимает числовой пресет: чем больше значение, тем быстрее.
_SVT_AV1_PRESETS: dict[str, int] = {
    "ultrafast": 12,
    "superfast": 11,
    "veryfast": 10,
    "faster": 9,
    "fast": 8,
    "medium": 7,
    "slow": 5,
    "slower": 4,
    "veryslow": 2,
}

# libvpx-vp9 управляется параметром cpu-used: чем больше, тем быстрее.
_VP9_CPU_USED: dict[str, int] = {
    "ultrafast": 8,
    "superfast": 7,
    "veryfast": 6,
    "faster": 5,
    "fast": 4,
    "medium": 3,
    "slow": 2,
    "slower": 1,
    "veryslow": 0,
}

# Профили ProRes в терминах prores_ks: 0-proxy, 1-LT, 2-standard, 3-HQ.
_PRORES_PROFILES: dict[str, int] = {
    "proxy": 0,
    "lt": 1,
    "standard": 2,
    "hq": 3,
}

# Кодеки, для которых существует замена при отсутствии энкодера в сборке.
_ENCODER_FALLBACK: dict[VideoCodec, tuple[VideoCodec, ...]] = {
    VideoCodec.AV1: (VideoCodec.VP9, VideoCodec.H265, VideoCodec.H264),
    VideoCodec.H265: (VideoCodec.H264,),
    VideoCodec.VP9: (VideoCodec.AV1,),
    VideoCodec.PRORES: (VideoCodec.H264,),
}


# ===========================================================================
# Реестр профилей по умолчанию
# ===========================================================================

DEFAULT_VIDEO_PROFILES: tuple[VideoProfile, ...] = (
    # --- Matroska: основной контейнер приложения -------------------------
    VideoProfile(
        identifier="mkv_h264",
        title="MKV / H.264 (универсальный)",
        container=Container.MKV,
        video_codec=VideoCodec.H264,
        audio_codec=AudioCodec.OPUS,
        crf=20,
        preset="veryfast",
    ),
    VideoProfile(
        identifier="mkv_h265",
        title="MKV / H.265 (компактный)",
        container=Container.MKV,
        video_codec=VideoCodec.H265,
        audio_codec=AudioCodec.OPUS,
        crf=24,
        preset="fast",
    ),
    VideoProfile(
        identifier="mkv_av1",
        title="MKV / AV1 (максимальное сжатие)",
        container=Container.MKV,
        video_codec=VideoCodec.AV1,
        audio_codec=AudioCodec.OPUS,
        crf=32,
        preset="fast",
    ),
    VideoProfile(
        identifier="mkv_ffv1",
        title="MKV / FFV1 (без потерь)",
        container=Container.MKV,
        video_codec=VideoCodec.FFV1,
        # Без потерь в видео логично сочетается с FLAC в звуке.
        audio_codec=AudioCodec.FLAC,
        crf=None,
        preset="medium",
        # Каждый кадр ключевой: файл остаётся читаемым при любом обрыве.
        keyint=1,
    ),
    # --- MP4: совместимость с любым устройством --------------------------
    VideoProfile(
        identifier="mp4_h264",
        title="MP4 / H.264 + AAC",
        container=Container.MP4,
        video_codec=VideoCodec.H264,
        audio_codec=AudioCodec.AAC,
        crf=21,
        preset="veryfast",
    ),
    VideoProfile(
        identifier="mp4_h265",
        title="MP4 / H.265 + AAC",
        container=Container.MP4,
        video_codec=VideoCodec.H265,
        audio_codec=AudioCodec.AAC,
        crf=25,
        preset="fast",
    ),
    # --- WebM: публикация в вебе -----------------------------------------
    VideoProfile(
        identifier="webm_vp9",
        title="WebM / VP9 + Opus",
        container=Container.WEBM,
        video_codec=VideoCodec.VP9,
        audio_codec=AudioCodec.OPUS,
        crf=31,
        preset="fast",
    ),
    VideoProfile(
        identifier="webm_av1",
        title="WebM / AV1 + Opus",
        container=Container.WEBM,
        video_codec=VideoCodec.AV1,
        audio_codec=AudioCodec.OPUS,
        crf=32,
        preset="fast",
    ),
    # --- MOV: передача в монтажные программы ------------------------------
    VideoProfile(
        identifier="mov_prores",
        title="MOV / ProRes 422 HQ (монтаж)",
        container=Container.MOV,
        video_codec=VideoCodec.PRORES,
        audio_codec=AudioCodec.PCM,
        crf=None,
        preset="hq",
    ),
    VideoProfile(
        identifier="mov_h264",
        title="MOV / H.264 + AAC",
        container=Container.MOV,
        video_codec=VideoCodec.H264,
        audio_codec=AudioCodec.AAC,
        crf=21,
        preset="veryfast",
    ),
    # --- AVI: устаревшая совместимость ------------------------------------
    VideoProfile(
        identifier="avi_mpeg4",
        title="AVI / MPEG-4 (совместимость)",
        container=Container.AVI,
        video_codec=VideoCodec.MPEG4,
        audio_codec=AudioCodec.MP3,
        crf=None,
        qscale=3,
        preset="medium",
    ),
)

DEFAULT_ANIMATION_PROFILES: tuple[AnimationProfile, ...] = (
    AnimationProfile(
        identifier="gif",
        title="GIF (палитра, два прохода)",
        container=Container.GIF,
        video_codec=VideoCodec.GIF,
        fps=15,
        width=800,
    ),
    AnimationProfile(
        identifier="webp_anim",
        title="Animated WebP (лёгкий, с прозрачностью)",
        container=Container.WEBP,
        video_codec=VideoCodec.WEBP_ANIM,
        fps=20,
        width=1000,
        quality=80,
    ),
    AnimationProfile(
        identifier="apng",
        title="APNG (без потерь)",
        container=Container.APNG,
        video_codec=VideoCodec.APNG,
        fps=15,
        width=800,
        lossless=True,
    ),
)


# ===========================================================================
# Основной класс модуля
# ===========================================================================


class VideoProfileManager:
    """
    Хранилище профилей и сборщик аргументов FFmpeg.

    Класс намеренно не обращается к файловой системе и не запускает процессы:
    список доступных энкодеров передаётся снаружи, будучи заранее получен
    фоновым воркером через "ffmpeg -hide_banner -encoders". Такой подход
    исключает подвисание интерфейса при открытии окна настроек.
    """

    def __init__(
        self,
        ffmpeg_path: str = "ffmpeg",
        available_encoders: frozenset[str] | None = None,
        video_profiles: Sequence[VideoProfile] = DEFAULT_VIDEO_PROFILES,
        animation_profiles: Sequence[AnimationProfile] = DEFAULT_ANIMATION_PROFILES,
    ) -> None:
        # Путь к бинарнику допускает как имя из PATH, так и абсолютный путь,
        # заданный пользователем в настройках.
        self._ffmpeg_path = ffmpeg_path
        # Значение None означает "проверка не выполнялась": в этом режиме
        # доступными считаются все энкодеры, откат не применяется.
        self._available_encoders = available_encoders
        self._video_profiles = {p.identifier: p for p in video_profiles}
        self._animation_profiles = {p.identifier: p for p in animation_profiles}

    # ---------------------------------------------------------------- доступ

    @property
    def ffmpeg_path(self) -> str:
        """Путь к используемому бинарнику FFmpeg."""
        return self._ffmpeg_path

    def video_profiles(self) -> list[VideoProfile]:
        """Список всех зарегистрированных видеопрофилей."""
        return list(self._video_profiles.values())

    def animation_profiles(self) -> list[AnimationProfile]:
        """Список всех зарегистрированных профилей анимации."""
        return list(self._animation_profiles.values())

    def profiles_for_container(self, container: Container) -> list[VideoProfile]:
        """Профили, относящиеся к конкретному контейнеру."""
        return [p for p in self._video_profiles.values() if p.container is container]

    def video_profile(self, identifier: str) -> VideoProfile:
        """Поиск видеопрофиля по идентификатору."""
        try:
            return self._video_profiles[identifier]
        except KeyError as error:
            raise KeyError(tr("Неизвестный видеопрофиль: {0}").format(identifier)) from error

    def animation_profile(self, identifier: str) -> AnimationProfile:
        """Поиск профиля анимации по идентификатору."""
        try:
            return self._animation_profiles[identifier]
        except KeyError as error:
            raise KeyError(tr("Неизвестный профиль анимации: {0}").format(identifier)) from error

    def register(self, profile: VideoProfile) -> None:
        """Добавление или замена пользовательского профиля."""
        self._video_profiles[profile.identifier] = profile

    def set_available_encoders(self, encoders: frozenset[str] | None) -> None:
        """Обновление сведений о возможностях установленной сборки FFmpeg."""
        self._available_encoders = encoders

    # ------------------------------------------------------- проверки сборки

    def is_encoder_available(self, name: str) -> bool:
        """Проверка наличия энкодера в установленной сборке FFmpeg."""
        if self._available_encoders is None:
            # Сведения не собраны: ограничения не накладываются, реальную
            # ошибку в этом случае вернёт сам FFmpeg при запуске.
            return True
        return name in self._available_encoders

    def resolve(self, profile: VideoProfile) -> VideoProfile:
        """
        Подбор рабочего профиля с учётом возможностей сборки FFmpeg.

        При отсутствии нужного энкодера выполняется откат на ближайший
        доступный вариант из таблицы замен, что избавляет от ошибки запуска
        на дистрибутивах с урезанным FFmpeg.
        """
        if self.is_encoder_available(profile.video_codec.encoder):
            return self._resolve_audio(profile)

        for candidate in _ENCODER_FALLBACK.get(profile.video_codec, ()):
            if self.is_encoder_available(candidate.encoder):
                # Пресет и CRF переносятся как есть: шкалы кодеков различаются,
                # но приведение выполняется на этапе сборки аргументов.
                return self._resolve_audio(replace(profile, video_codec=candidate))

        # Подходящей замены не найдено, профиль возвращается без изменений,
        # а слой интерфейса покажет предупреждение из validate().
        return self._resolve_audio(profile)

    def _resolve_audio(self, profile: VideoProfile) -> VideoProfile:
        """Откат аудиокодека при его отсутствии в сборке."""
        if self.is_encoder_available(profile.audio_codec.encoder):
            return profile
        # AAC присутствует во FFmpeg всегда как встроенный энкодер,
        # поэтому используется как универсальный запасной вариант.
        return replace(profile, audio_codec=AudioCodec.AAC)

    def apply_overrides(self, profile: VideoProfile, overrides: ProfileOverrides) -> VideoProfile:
        """
        Наложение пользовательских уточнений на профиль.

        Возвращается новый профиль: исходный набор профилей остаётся
        неизменным и может быть выбран заново в любой момент.
        """
        if overrides.is_empty:
            return profile

        changes: dict[str, object] = {}
        if overrides.video_codec is not None:
            changes["video_codec"] = overrides.video_codec
        if overrides.audio_codec is not None:
            changes["audio_codec"] = overrides.audio_codec
        if overrides.preset:
            changes["preset"] = overrides.preset
        if overrides.pix_fmt:
            changes["pix_fmt"] = overrides.pix_fmt
        if overrides.keyint is not None:
            changes["keyint"] = overrides.keyint
        if overrides.audio_bitrate:
            changes["audio_bitrate"] = overrides.audio_bitrate
        if overrides.audio_sample_rate:
            changes["audio_sample_rate"] = overrides.audio_sample_rate
        if overrides.audio_channels:
            changes["audio_channels"] = overrides.audio_channels

        # Способы управления качеством исключают друг друга: заданный
        # битрейт отменяет постоянное качество и наоборот.
        if overrides.rate_mode == "bitrate" and overrides.video_bitrate:
            changes["video_bitrate"] = overrides.video_bitrate
            changes["crf"] = None
        elif overrides.rate_mode == "crf" and overrides.crf is not None:
            changes["crf"] = overrides.crf
            changes["video_bitrate"] = None

        if overrides.extra_args:
            # Строка разбирается по правилам оболочки, но исполняется без
            # неё: каждый аргумент попадает в список отдельным элементом.
            changes["extra_output_args"] = tuple(shlex.split(overrides.extra_args))

        return replace(profile, **changes)  # type: ignore[arg-type]

    def build_custom_command(
        self,
        template: str,
        video_input: VideoInput,
        output_path: Path,
        audio_inputs: Sequence[AudioInput] = (),
    ) -> list[str]:
        """
        Сборка команды по заданному пользователем образцу.

        Образец содержит подстановки, вместо которых подставляются
        подготовленные приложением значения:

            {ffmpeg}       путь к бинарнику вместе с общими флагами,
                           включая передачу прогресса и подавление
                           интерактивных вопросов;
            {video_input}  аргументы входа захвата экрана целиком;
            {audio_input}  аргументы всех звуковых входов целиком;
            {output}       путь к файлу результата;
            {fps}, {width}, {height}  отдельные значения захвата.

        Образец разбирается по правилам оболочки, после чего подстановки
        заменяются готовыми элементами списка. Сама оболочка при запуске
        не участвует, поэтому пробелы в путях безопасны.
        """
        if "{output}" not in template:
            raise ValueError(tr("В образце команды отсутствует подстановка {output}"))

        scalars = {
            "{fps}": str(video_input.fps),
            "{width}": str(video_input.width or 0),
            "{height}": str(video_input.height or 0),
        }
        audio_args: list[str] = []
        for audio_input in audio_inputs:
            audio_args += list(audio_input.args)

        command: list[str] = []
        for token in shlex.split(template):
            if token == "{ffmpeg}":
                command.append(self._ffmpeg_path)
                # Общие флаги добавляются принудительно: без них не работают
                # ни счётчик длительности, ни остановка по команде "q".
                command += self._global_args(overwrite=True, report_progress=True)
            elif token == "{video_input}":
                command += list(video_input.args)
            elif token == "{audio_input}":
                command += audio_args
            elif token == "{output}":
                command.append(str(output_path))
            else:
                for name, value in scalars.items():
                    token = token.replace(name, value)
                command.append(token)

        if not command:
            raise ValueError(tr("Образец команды пуст"))
        if command[0] != self._ffmpeg_path:
            # Образец без подстановки {ffmpeg} начинается сразу с аргументов.
            command = [self._ffmpeg_path, *command]
        return command

    def build_command_template(
        self,
        profile: VideoProfile,
        audio_mode: AudioMode = AudioMode.NONE,
        audio_sources: int = 0,
    ) -> str:
        """
        Образец ручной команды, повторяющий действие выбранного профиля.

        Служит отправной точкой для правки: пользователю не приходится
        составлять команду с нуля. Вместо аргументов входов и пути к файлу
        подставляются соответствующие обозначения.
        """
        video = VideoInput(args=["{video_input}"], fps=30, needs_even_padding=False)

        # Первый звуковой вход несёт обозначение целиком, остальные пусты:
        # обозначение раскрывается сразу во все выбранные источники, а
        # число входов влияет на нумерацию потоков в аргументах отображения.
        audio = tuple(
            AudioInput(
                args=["{audio_input}"] if index == 0 else [],
                role=AudioRole.SYSTEM if index == 0 else AudioRole.MICROPHONE,
            )
            for index in range(max(0, audio_sources))
        )

        command = self.build_record_command(
            profile,
            video,
            Path("{output}"),
            audio,
            audio_mode if audio else AudioMode.NONE,
            report_progress=False,
        )

        # Путь к бинарнику и общие флаги заменяются одним обозначением:
        # при сборке команды они подставляются принудительно.
        body = command[1:]
        skipped = self._global_args(overwrite=True, report_progress=False)
        if body[: len(skipped)] == skipped:
            body = body[len(skipped) :]

        # Обозначения оставляются без кавычек ради читаемости, остальные
        # аргументы экранируются: образец разбирается по правилам оболочки.
        parts = [
            token if token.startswith("{") and token.endswith("}") else shlex.quote(token)
            for token in body
        ]
        return "{ffmpeg} " + " ".join(parts)

    def build_custom_step(
        self,
        template: str,
        video_input: VideoInput,
        output_path: Path,
        audio_inputs: Sequence[AudioInput] = (),
    ) -> FFmpegStep:
        """Обёртка ручной команды в объект шага для контроллера процесса."""
        return FFmpegStep(
            label=tr("Запись по заданной команде"),
            args=self.build_custom_command(template, video_input, output_path, audio_inputs),
        )

    def validate(
        self,
        profile: VideoProfile,
        audio_mode: AudioMode = AudioMode.NONE,
    ) -> list[str]:
        """Список предупреждений о несовместимых сочетаниях настроек."""
        problems: list[str] = []

        if not self.is_encoder_available(profile.video_codec.encoder):
            problems.append(
                tr("Видеокодер {0} отсутствует в сборке FFmpeg.").format(
                    profile.video_codec.encoder
                )
            )
        if not self.is_encoder_available(profile.audio_codec.encoder):
            problems.append(
                tr("Аудиокодер {0} отсутствует в сборке FFmpeg.").format(
                    profile.audio_codec.encoder
                )
            )
        if audio_mode is AudioMode.SEPARATE and not profile.container.supports_multitrack_audio:
            problems.append(
                tr(
                    "Контейнер {0} не хранит несколько дорожек: звук будет сведён через amix."
                ).format(profile.container.value.upper())
            )
        if profile.container is Container.WEBM and profile.video_codec not in (
            VideoCodec.VP9,
            VideoCodec.AV1,
        ):
            problems.append(tr("WebM допускает только VP9 или AV1."))
        if profile.container is Container.WEBM and profile.audio_codec is not AudioCodec.OPUS:
            problems.append(tr("WebM допускает только звук Opus."))
        if not profile.container.survives_crash:
            problems.append(
                tr(
                    "Формат не восстанавливается после аварийного завершения: "
                    "для длительных записей предпочтителен MKV."
                )
            )
        return problems

    # ----------------------------------------------- сборка команды записи

    def build_record_command(
        self,
        profile: VideoProfile,
        video_input: VideoInput,
        output_path: Path,
        audio_inputs: Sequence[AudioInput] = (),
        audio_mode: AudioMode = AudioMode.NONE,
        *,
        overwrite: bool = True,
        report_progress: bool = True,
    ) -> list[str]:
        """
        Сборка полной команды записи экрана.

        Возвращается список аргументов, пригодный для прямой передачи в
        QProcess.start() или subprocess.Popen() без участия оболочки.
        """
        profile = self.resolve(profile)

        # Режим без звука приводит список аудиовходов к пустому независимо
        # от того, что было подготовлено ранее.
        if audio_mode is AudioMode.NONE or profile.is_animation:
            audio_inputs = ()

        args: list[str] = [self._ffmpeg_path]
        args += self._global_args(overwrite=overwrite, report_progress=report_progress)

        # Порядок входов критичен: индексы потоков в "-map" опираются на него.
        args += list(video_input.args)
        for audio_input in audio_inputs:
            args += list(audio_input.args)

        # Фильтры видео применяются к простому мапленному потоку и корректно
        # сосуществуют с filter_complex, обслуживающим звук.
        video_filters = self._video_filters(profile, video_input)
        if video_filters:
            args += ["-vf", ",".join(video_filters)]

        audio_filter, audio_maps, audio_codec_args, audio_meta = self._audio_section(
            profile, audio_inputs, audio_mode
        )
        args += audio_filter
        args += ["-map", f"{VIDEO_INPUT_INDEX}:v"]
        args += audio_maps
        args += self._video_codec_args(profile, video_input)
        args += audio_codec_args
        args += audio_meta
        args += self._container_args(profile)
        args += list(profile.extra_output_args)

        # Путь добавляется последним отдельным элементом списка: экранирование
        # не требуется, так как оболочка в цепочке запуска не участвует.
        args.append(str(output_path))
        return args

    def build_record_step(
        self,
        profile: VideoProfile,
        video_input: VideoInput,
        output_path: Path,
        audio_inputs: Sequence[AudioInput] = (),
        audio_mode: AudioMode = AudioMode.NONE,
    ) -> FFmpegStep:
        """Обёртка команды записи в объект шага для контроллера процесса."""
        return FFmpegStep(
            label=tr("Запись: {0}").format(tr(profile.title)),
            args=self.build_record_command(
                profile, video_input, output_path, audio_inputs, audio_mode
            ),
        )

    # --------------------------------------------- сборка заданий анимации

    def build_animation_steps(
        self,
        profile: AnimationProfile,
        source_path: Path,
        output_path: Path,
        work_dir: Path | None = None,
    ) -> list[FFmpegStep]:
        """
        Сборка задания конвертации записи в анимированный формат.

        Исходником выступает промежуточный файл записи (по умолчанию
        MKV/FFV1), что даёт максимальное качество палитры и позволяет
        переcобрать анимацию с другими параметрами без повторного захвата.
        """
        if profile.container is Container.GIF:
            return self._gif_steps(profile, source_path, output_path, work_dir)
        if profile.container is Container.WEBP:
            return [self._webp_step(profile, source_path, output_path)]
        if profile.container is Container.APNG:
            return [self._apng_step(profile, source_path, output_path)]
        raise ValueError(tr("Формат {0} не является анимированным").format(profile.container))

    def intermediate_profile(self) -> VideoProfile:
        """
        Профиль промежуточной записи перед сборкой анимации.

        Промежуточный файл пишется без потерь, чтобы генератор палитры
        работал с исходными цветами, а не с артефактами компрессии.
        """
        return self.resolve(self.video_profile("mkv_ffv1"))

    def _gif_steps(
        self,
        profile: AnimationProfile,
        source_path: Path,
        output_path: Path,
        work_dir: Path | None,
    ) -> list[FFmpegStep]:
        """Два прохода GIF: расчёт палитры и её наложение."""
        # Палитра размещается рядом с результатом либо во временном каталоге,
        # переданном вызывающей стороной.
        base_dir = work_dir if work_dir is not None else output_path.parent
        palette_path = base_dir / f".{output_path.stem}.palette.png"

        # Цепочка предобработки едина для обоих проходов: несовпадение
        # частоты кадров или масштаба между проходами испортит палитру.
        preprocess = self._animation_filters(profile)

        # Проход 1: анализ кадров и построение палитры до 256 цветов.
        # Режим stats_mode=diff отдаёт приоритет меняющимся областям кадра,
        # что критично для записей экрана со статичным фоном.
        pass_one_filter = (
            f"{preprocess},palettegen=max_colors={profile.max_colors}"
            f":stats_mode={profile.palette_stats_mode}"
        )
        pass_one = FFmpegStep(
            label=tr("GIF, проход 1 из 2: построение палитры"),
            args=[
                self._ffmpeg_path,
                *self._global_args(overwrite=True, report_progress=True),
                "-i",
                str(source_path),
                "-vf",
                pass_one_filter,
                # Результатом прохода является единственное изображение палитры.
                "-frames:v",
                "1",
                "-update",
                "1",
                "-f",
                "image2",
                str(palette_path),
            ],
            temporary=(palette_path,),
        )

        # Проход 2: наложение готовой палитры на те же кадры.
        # Палитра подключается вторым входом, поэтому её путь не попадает
        # внутрь строки фильтров и не требует экранирования.
        dither = profile.dither
        if dither.startswith("bayer"):
            dither = f"bayer:bayer_scale={profile.bayer_scale}"
        pass_two_filter = (
            f"[0:v]{preprocess}[frames];"
            f"[frames][1:v]paletteuse=dither={dither}"
            # diff_mode=rectangle перерисовывает только изменившийся
            # прямоугольник кадра, заметно уменьшая размер файла.
            ":diff_mode=rectangle:new=1[out]"
        )
        pass_two = FFmpegStep(
            label=tr("GIF, проход 2 из 2: наложение палитры"),
            args=[
                self._ffmpeg_path,
                *self._global_args(overwrite=True, report_progress=True),
                "-i",
                str(source_path),
                "-i",
                str(palette_path),
                "-filter_complex",
                pass_two_filter,
                "-map",
                "[out]",
                # Флаг transdiff включает дельта-кодирование кадров.
                "-gifflags",
                "+transdiff",
                "-loop",
                str(profile.loop),
                "-an",
                "-f",
                profile.container.muxer,
                *profile.extra_output_args,
                str(output_path),
            ],
            temporary=(palette_path,),
        )
        return [pass_one, pass_two]

    def _webp_step(
        self,
        profile: AnimationProfile,
        source_path: Path,
        output_path: Path,
    ) -> FFmpegStep:
        """Однопроходная сборка анимированного WebP."""
        # Энкодер libwebp_anim присутствует не во всех сборках, поэтому при
        # его отсутствии применяется обычный libwebp: мультиплексор webp
        # склеивает последовательность кадров в анимацию самостоятельно.
        encoder = profile.video_codec.encoder
        if not self.is_encoder_available(encoder):
            encoder = "libwebp"

        args = [
            self._ffmpeg_path,
            *self._global_args(overwrite=True, report_progress=True),
            "-i",
            str(source_path),
            "-vf",
            self._animation_filters(profile),
            "-c:v",
            encoder,
            "-lossless",
            "1" if profile.lossless else "0",
            # Параметр quality игнорируется в режиме без потерь.
            "-quality",
            str(profile.quality),
            "-compression_level",
            str(profile.compression_level),
            # Режим picture даёт лучший результат на снимках интерфейса.
            "-preset",
            "picture",
            "-loop",
            str(profile.loop),
            "-an",
            "-f",
            profile.container.muxer,
            *profile.extra_output_args,
            str(output_path),
        ]
        return FFmpegStep(label=tr("Сборка анимированного WebP"), args=args)

    def _apng_step(
        self,
        profile: AnimationProfile,
        source_path: Path,
        output_path: Path,
    ) -> FFmpegStep:
        """Однопроходная сборка APNG без потерь качества."""
        args = [
            self._ffmpeg_path,
            *self._global_args(overwrite=True, report_progress=True),
            "-i",
            str(source_path),
            "-vf",
            # Формат rgba сохраняет альфа-канал, если он присутствует.
            f"{self._animation_filters(profile)},format=rgba",
            "-c:v",
            "apng",
            # Значение plays=0 означает бесконечное повторение анимации.
            "-plays",
            str(profile.loop),
            "-f",
            profile.container.muxer,
            *profile.extra_output_args,
            str(output_path),
        ]
        return FFmpegStep(label=tr("Сборка APNG"), args=args)

    # ------------------------------------------------ внутренние сборщики

    def _global_args(self, *, overwrite: bool, report_progress: bool) -> list[str]:
        """Общие для всех заданий флаги FFmpeg."""
        args = ["-hide_banner", "-loglevel", "warning"]
        if overwrite:
            # Без этого флага процесс зависнет на интерактивном вопросе
            # о перезаписи существующего файла.
            args.append("-y")
        if report_progress:
            # Машиночитаемый прогресс уходит в stdout, журнал остаётся в
            # stderr. Флаг -nostdin намеренно не добавляется: остановка
            # записи выполняется отправкой символа "q" в stdin процесса.
            args += ["-progress", "pipe:1", "-nostats"]
        return args

    def _video_filters(self, profile: VideoProfile, video_input: VideoInput) -> list[str]:
        """Цепочка видеофильтров для записи."""
        filters: list[str] = []
        if video_input.needs_even_padding:
            # Дополнение до чётных сторон обязательно для форматов с
            # субдискретизацией 4:2:0, иначе FFmpeg откажется кодировать.
            filters.append("pad=ceil(iw/2)*2:ceil(ih/2)*2")
        pix_fmt = profile.pix_fmt or profile.video_codec.default_pix_fmt
        # Преобразование цветового пространства выполняется фильтром, а не
        # флагом -pix_fmt: так порядок операций в цепочке остаётся явным.
        filters.append(f"format={pix_fmt}")
        return filters

    def _animation_filters(self, profile: AnimationProfile) -> str:
        """Цепочка фильтров предобработки кадров для анимаций."""
        # Прореживание кадров выполняется до масштабирования: так фильтр
        # scale обрабатывает меньшее число кадров и работает быстрее.
        parts = [f"fps={profile.fps}"]
        if profile.width:
            # Выражение min(iw, ширина) запрещает увеличение: растягивание
            # кадра раздувает файл, не добавляя деталей. Запятая внутри
            # выражения экранируется, иначе разборщик графа фильтров примет
            # её за разделитель фильтров.
            # Значение -1 сохраняет пропорции, lanczos даёт чёткий результат
            # на тексте и элементах интерфейса.
            parts.append(f"scale=w=min(iw\\,{profile.width}):h=-1:flags=lanczos")
        return ",".join(parts)

    def _video_codec_args(self, profile: VideoProfile, video_input: VideoInput) -> list[str]:
        """Аргументы видеокодека с учётом его собственной шкалы настроек."""
        codec = profile.video_codec
        args: list[str] = ["-c:v", codec.encoder]

        if codec in (VideoCodec.H264, VideoCodec.H265):
            # Оба кодека семейства x26x понимают словесные пресеты и CRF.
            args += ["-preset", profile.preset]
            if profile.crf is not None:
                args += ["-crf", str(profile.crf)]
            if profile.tune:
                args += ["-tune", profile.tune]
            if profile.keyint:
                args += ["-g", str(profile.keyint)]
            if codec is VideoCodec.H265 and profile.container in (Container.MP4, Container.MOV):
                # Без тега hvc1 файл не открывается штатными плеерами Apple.
                args += ["-tag:v", "hvc1"]
        elif codec is VideoCodec.AV1:
            # SVT-AV1 принимает числовой пресет скорости.
            args += ["-preset", str(_SVT_AV1_PRESETS.get(profile.preset, 8))]
            if profile.crf is not None:
                args += ["-crf", str(profile.crf)]
            if profile.keyint:
                args += ["-g", str(profile.keyint)]
            # Режим tune=0 оптимизирует субъективное качество картинки.
            args += ["-svtav1-params", "tune=0"]
        elif codec is VideoCodec.VP9:
            if profile.crf is not None:
                # Нулевой битрейт переводит libvpx в чистый режим CRF.
                args += ["-crf", str(profile.crf), "-b:v", "0"]
            args += ["-cpu-used", str(_VP9_CPU_USED.get(profile.preset, 4))]
            # Многопоточность по строкам и тайлам ускоряет кодирование в разы.
            args += ["-row-mt", "1", "-tile-columns", "2", "-deadline", "good"]
            if profile.keyint:
                args += ["-g", str(profile.keyint)]
        elif codec is VideoCodec.FFV1:
            # Третья версия формата поддерживает срезы и контрольные суммы,
            # что позволяет восстановить файл после повреждения.
            args += [
                "-level",
                "3",
                "-coder",
                "1",
                "-context",
                "1",
                "-g",
                "1",
                "-slices",
                "24",
                "-slicecrc",
                "1",
            ]
        elif codec is VideoCodec.PRORES:
            args += ["-profile:v", str(_PRORES_PROFILES.get(profile.preset, 3))]
            # Идентификатор производителя требуется частью монтажных программ.
            args += ["-vendor", "apl0"]
        elif codec is VideoCodec.MPEG4:
            args += ["-qscale:v", str(profile.qscale if profile.qscale is not None else 3)]
            # Тег FourCC повышает шансы воспроизведения старыми плеерами.
            args += ["-vtag", "xvid"]

        if profile.video_bitrate:
            args += ["-b:v", profile.video_bitrate]
        if profile.force_cfr:
            # Постоянная частота кадров защищает от рассинхронизации звука
            # при просадках производительности захвата.
            args += ["-fps_mode", "cfr", "-r", str(video_input.fps)]
        args += list(profile.extra_video_args)
        return args

    def _audio_section(
        self,
        profile: VideoProfile,
        audio_inputs: Sequence[AudioInput],
        audio_mode: AudioMode,
    ) -> tuple[list[str], list[str], list[str], list[str]]:
        """
        Сборка звуковой части команды.

        Возвращает кортеж из четырёх списков: аргументы filter_complex,
        аргументы маппинга потоков, аргументы кодека и метаданные дорожек.
        """
        if not audio_inputs or audio_mode is AudioMode.NONE:
            # Явное отключение звука избавляет от пустой дорожки в контейнере.
            return [], [], ["-an"], []

        # Режим раздельных дорожек возможен только в подходящем контейнере;
        # иначе выполняется автоматический откат к сведению через amix.
        effective_mode = audio_mode
        if effective_mode is AudioMode.SEPARATE and not profile.container.supports_multitrack_audio:
            effective_mode = AudioMode.MIX
        # Сведение одного источника не имеет смысла и заменяется прямым мапом.
        if effective_mode is AudioMode.MIX and len(audio_inputs) < 2:
            effective_mode = AudioMode.SYSTEM

        codec_args = self._audio_codec_args(profile)
        filter_args: list[str] = []
        map_args: list[str] = []
        meta_args: list[str] = []

        if effective_mode is AudioMode.MIX:
            # Каждый вход предварительно пропускается через aresample с
            # компенсацией дрейфа: источники PulseAudio стартуют неодновременно
            # и имеют независимые тактовые генераторы.
            chains: list[str] = []
            labels: list[str] = []
            for position, _ in enumerate(audio_inputs):
                index = VIDEO_INPUT_INDEX + 1 + position
                label = f"a{position}"
                chains.append(f"[{index}:a]aresample=async=1:first_pts=0[{label}]")
                labels.append(f"[{label}]")
            # Параметр normalize=0 сохраняет исходную громкость источников
            # вместо деления уровня на число входов.
            chains.append(
                f"{''.join(labels)}amix=inputs={len(audio_inputs)}"
                ":duration=longest:dropout_transition=0:normalize=0[aout]"
            )
            filter_args = ["-filter_complex", ";".join(chains)]
            map_args = ["-map", "[aout]"]
            meta_args = ["-metadata:s:a:0", "title=Mixed Audio"]
        else:
            # Прямое подключение входов: по одной дорожке на источник.
            # В режимах SYSTEM и MICROPHONE список входов содержит один элемент,
            # в режиме SEPARATE - по одному на каждую дорожку контейнера.
            selected = audio_inputs if effective_mode is AudioMode.SEPARATE else audio_inputs[:1]
            for position, audio_input in enumerate(selected):
                index = VIDEO_INPUT_INDEX + 1 + position
                map_args += ["-map", f"{index}:a"]
                # Заголовок и язык дорожки записываются в метаданные,
                # благодаря чему плеер показывает осмысленные названия.
                meta_args += [
                    f"-metadata:s:a:{position}",
                    f"title={audio_input.track_title}",
                    f"-metadata:s:a:{position}",
                    f"language={audio_input.language}",
                ]
            if len(selected) > 1:
                # Первая дорожка помечается как выбираемая по умолчанию.
                meta_args += ["-disposition:a:0", "default"]

        return filter_args, map_args, codec_args, meta_args

    def _audio_codec_args(self, profile: VideoProfile) -> list[str]:
        """Аргументы аудиокодека, общие для всех дорожек вывода."""
        codec = profile.audio_codec
        args: list[str] = ["-c:a", codec.encoder]
        if codec.uses_bitrate:
            # Битрейт применяется к каждой дорожке вывода.
            args += ["-b:a", profile.audio_bitrate]
        args += ["-ar", str(profile.audio_sample_rate), "-ac", str(profile.audio_channels)]
        return args

    def _container_args(self, profile: VideoProfile) -> list[str]:
        """Флаги, специфичные для конкретного мультиплексора."""
        args: list[str] = ["-f", profile.container.muxer]
        if profile.container in (Container.MP4, Container.MOV):
            # Перенос индекса moov в начало файла делает воспроизведение
            # возможным сразу, без полной загрузки. Операция выполняется
            # при закрытии файла, поэтому корректная остановка обязательна.
            args += ["-movflags", "+faststart"]
        # Дополнительных флагов Matroska не требует: формат пишется
        # блоками и читается плеерами даже без финализации заголовка,
        # поэтому аварийно оборванная запись остаётся пригодной.
        return args


# ===========================================================================
# Служебная точка входа для ручной проверки формируемых команд
# ===========================================================================

if __name__ == "__main__":  # pragma: no cover
    # Демонстрация: выводит команды, которые будут переданы FFmpeg.
    manager = VideoProfileManager()
    demo_video = VideoInput(
        args=["-f", "x11grab", "-framerate", "30", "-video_size", "1920x1080", "-i", ":0.0+0,0"],
        fps=30,
    )
    demo_audio = (
        AudioInput(
            args=["-f", "pulse", "-i", "alsa_output.pci-0000_00_1f.3.analog-stereo.monitor"],
            role=AudioRole.SYSTEM,
            language="und",
        ),
        AudioInput(
            args=["-f", "pulse", "-i", "alsa_input.pci-0000_00_1f.3.analog-stereo"],
            role=AudioRole.MICROPHONE,
            language="und",
        ),
    )

    for mode in (AudioMode.SEPARATE, AudioMode.MIX):
        command = manager.build_record_command(
            manager.video_profile("mkv_h264"),
            demo_video,
            Path("/tmp/Запись экрана.mkv"),
            demo_audio,
            mode,
        )
        print(f"\n# MKV, режим звука: {mode.label}\n{shlex.join(command)}")

    mp4_command = manager.build_record_command(
        manager.video_profile("mp4_h264"),
        demo_video,
        Path("/tmp/demo.mp4"),
        demo_audio[:1],
        AudioMode.SYSTEM,
    )
    print(f"\n# MP4 + faststart\n{shlex.join(mp4_command)}")

    for step in manager.build_animation_steps(
        manager.animation_profile("gif"),
        Path("/tmp/intermediate.mkv"),
        Path("/tmp/demo.gif"),
    ):
        print(f"\n# {step.label}\n{step.printable}")
