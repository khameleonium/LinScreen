"""
Связывание модулей приложения в единый рабочий цикл.

Класс LinScreenApplication выступает посредником между источниками команд
(значок трея, глобальные клавиши), слоями захвата и кодирования и окнами
интерфейса. Сами модули друг о друге не знают, что позволяет заменять
бэкенды захвата и кодирования независимо.
"""

from __future__ import annotations

from core.i18n import set_language, tr

from pathlib import Path

import os
from collections import deque
from datetime import datetime
from typing import Callable, TextIO, cast

from PySide6.QtCore import QObject, QRect, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QGuiApplication, QImage
from PySide6.QtWidgets import QApplication, QFileDialog, QWidget

from capture.audio import AudioDevice, list_audio_devices, resolve_audio_inputs
from capture.portal import (
    PipeWireStream,
    ScreenCastPortal,
    ScreenshotPortal,
    is_screencast_available,
    is_screenshot_available,
)
from capture.windows import list_objects
from capture.screen import (
    CaptureBackendError,
    CaptureMode,
    build_pipewire_video_input,
    build_video_input,
    crop_desktop_image,
    grab_virtual_desktop,
    grab_with_grim,
    list_monitors,
    virtual_geometry,
)
from core.config import ConfigManager
from core.hotkeys import HotkeyManager
from core.session import detect_session
from core.workers import run_async
from editor.window import EditorWindow
from encoder.ffmpeg import FFmpegNotFoundError, find_ffmpeg
from encoder.images import (
    ImageSettings,
    copy_image_to_clipboard,
    extension_for,
    is_avif_available,
    save_image,
)
from encoder.process import CapabilityProbe, RecorderState, RecordingJob, ScreenRecorder
from encoder.profiles import (
    AudioCodec,
    AudioMode,
    ProfileOverrides,
    VideoCodec,
    VideoProfileManager,
)
from ui.overlay import RegionOverlay
from ui.recorder_bar import RecorderBar
from ui.region_frame import PAUSED_COLOR, RECORDING_COLOR, RegionFrame
from ui.settings import SettingsDialog
from ui.log_window import LogWindow
from ui.tray import TrayIcon

# Предельный размер файла журнала. Приложение работает сутками, и
# неограниченный файл со временем занял бы заметное место.
LOG_FILE_LIMIT = 1024 * 1024

# Пауза между скрытием окон и запуском захвата. Composited-окружению
# требуется время на перерисовку, иначе окно попадёт в первые кадры.
HIDE_SETTLE_MS = 300


def log_file_path() -> Path:
    """Путь файла журнала работы внешних процессов."""
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "linscreen" / "linscreen.log"


class LinScreenApplication(QObject):
    """Управляющий объект приложения."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._session = detect_session()
        self._config = ConfigManager()
        self._config.load()
        # Язык выбирается до создания любых окон: строки интерфейса
        # переводятся в момент их создания.
        set_language(self._config.settings.general.language)

        self._ffmpeg_path = self._resolve_ffmpeg()
        self._profiles = VideoProfileManager(ffmpeg_path=self._ffmpeg_path or "ffmpeg")

        self._tray = TrayIcon(self)
        self._recorder = ScreenRecorder(self)
        self._hotkeys = HotkeyManager(self._session, self)
        self._recorder_bar = RecorderBar()
        self._probe = CapabilityProbe(self)

        # Окна редактора и оверлей удерживаются ссылками: без этого сборщик
        # мусора уничтожит их сразу после выхода из обработчика.
        self._editors: list[EditorWindow] = []
        self._overlay: RegionOverlay | None = None
        # Запрос к порталу асинхронный: ссылка удерживается до ответа.
        self._screenshot_portal: ScreenshotPortal | None = None
        # Рамка, обводящая записываемую область во время съёмки.
        self._region_frame = RegionFrame()
        # Область текущей записи: нужна для показа рамки.
        self._recorded_rect: QRect | None = None
        self._screencast_portal: ScreenCastPortal | None = None
        # Сведения о возможностях сборки FFmpeg, полученные фоновым опросом.
        self._capabilities: object | None = None
        # Журнал работы внешних процессов для самостоятельной диагностики.
        # Глубина ограничена: приложение работает сутками без перезапуска.
        self._log_lines: deque[str] = deque(maxlen=2000)
        self._log_window: LogWindow | None = None
        # Окна, скрытые на время записи и подлежащие возврату после неё.
        self._hidden_windows: list[QWidget] = []
        # Открытый файл журнала: запись каждой строки с повторным открытием
        # файла обходится в три системных вызова вместо одного.
        self._log_file: TextIO | None = None
        self._settings_dialog: SettingsDialog | None = None
        # Признак ожидания завершения записи перед выходом из приложения.
        self._quit_after_recording = False

        self._connect_signals()

    # ------------------------------------------------------------- запуск

    def start(self) -> None:
        """Запуск приложения: показ значка и регистрация клавиш."""
        self._tray.show()
        self._tray.set_recording_allowed(self._recording_possible())
        self._tray.set_audio_mode(self._audio_mode())
        self._apply_hotkeys()

        if self._ffmpeg_path:
            # Опрос возможностей сборки выполняется в фоне и не задерживает старт.
            self._probe.start(self._ffmpeg_path)
        else:
            self._notify(
                tr("FFmpeg не найден"),
                tr("Запись экрана недоступна. Требуется установить пакет ffmpeg."),
                is_error=True,
            )

        if not self._session.is_x11:
            self._notify(
                tr("Сессия Wayland"),
                tr("Доступны снимки экрана. Запись требует портала ScreenCast."),
            )

    def _resolve_ffmpeg(self) -> str:
        """Определение пути к бинарнику FFmpeg."""
        try:
            return find_ffmpeg(self._config.settings.general.ffmpeg_path or None)
        except FFmpegNotFoundError:
            # Отсутствие FFmpeg не мешает работе со снимками экрана.
            return ""

    def _recording_possible(self) -> bool:
        """
        Проверка принципиальной возможности записи в текущей сессии.

        В X11 достаточно наличия FFmpeg. В Wayland дополнительно требуются
        портал ScreenCast и фильтр pipewiregrab в сборке FFmpeg.
        """
        if not self._ffmpeg_path:
            return False
        if self._session.is_x11:
            return True
        return is_screencast_available()

    def _connect_signals(self) -> None:
        """Связывание источников команд с обработчиками."""
        self._tray.screenshotRequested.connect(self.take_screenshot)
        self._tray.monitorScreenshotRequested.connect(self.take_monitor_screenshot)
        self._tray.recordRequested.connect(self.start_recording)
        self._tray.monitorRecordRequested.connect(self.record_monitor)
        self._tray.audioModeChanged.connect(self.set_audio_mode)
        self._tray.pauseRequested.connect(self.toggle_pause)
        self._tray.stopRequested.connect(self.stop_recording)
        self._tray.settingsRequested.connect(self.open_settings)
        self._tray.logRequested.connect(self.open_log)
        self._tray.openImagesFolderRequested.connect(self.open_images_folder)
        self._tray.openVideosFolderRequested.connect(self.open_videos_folder)
        self._tray.quitRequested.connect(self.quit)

        self._recorder.stateChanged.connect(self._on_state_changed)
        self._recorder.elapsedChanged.connect(self._on_elapsed)
        self._recorder.finished.connect(self._on_recording_finished)
        self._recorder.failed.connect(self._on_recording_failed)
        self._recorder.warning.connect(lambda text: self._notify(tr("Запись"), text))
        self._recorder.log.connect(self._append_log)

        self._recorder_bar.pauseRequested.connect(self.toggle_pause)
        self._recorder_bar.stopRequested.connect(self.stop_recording)

        self._hotkeys.activated.connect(self._on_hotkey)
        self._hotkeys.unavailable.connect(lambda text: self._notify(tr("Горячие клавиши"), text))

        self._probe.ready.connect(self._on_capabilities)
        self._probe.failed.connect(lambda text: self._notify("FFmpeg", text, is_error=True))

    def _apply_hotkeys(self) -> None:
        """Перерегистрация глобальных клавиш по текущим настройкам."""
        self._hotkeys.apply(self._config.settings.hotkeys.as_mapping())

    def _on_capabilities(self, capabilities: object) -> None:
        """Применение сведений о возможностях установленной сборки FFmpeg."""
        encoders = getattr(capabilities, "encoders", None)
        if encoders is None:
            return
        self._capabilities = capabilities
        # Профили, требующие отсутствующих энкодеров, будут заменены
        # доступными при сборке команды записи.
        self._profiles.set_available_encoders(encoders)
        problems = capabilities.missing_essentials()  # type: ignore[attr-defined]
        if problems:
            self._notify(tr("Ограничения сборки FFmpeg"), problems[0])

    # ----------------------------------------------------------- снимки

    def take_screenshot(self, mode: object) -> None:
        """Снимок экрана в выбранном режиме с учётом задержки."""
        capture_mode = mode if isinstance(mode, CaptureMode) else CaptureMode.REGION
        delay = self._config.settings.general.capture_delay_ms
        if delay > 0:
            # Задержка позволяет раскрыть меню или подсказку до снимка.
            QTimer.singleShot(delay, lambda: self._perform_screenshot(capture_mode))
        else:
            self._perform_screenshot(capture_mode)

    def take_monitor_screenshot(self, index: int) -> None:
        """Снимок отдельного монитора по его порядковому номеру."""
        monitors = list_monitors()
        if not 0 <= index < len(monitors):
            return
        geometry = monitors[index].geometry
        self._grab_desktop_async(
            lambda desktop: self._finish_screenshot(crop_desktop_image(desktop, geometry))
        )

    def _grab_desktop_async(self, on_ready: Callable[[QImage], None]) -> None:
        """
        Снимок всего рабочего стола способом, пригодным для текущей сессии.

        В X11 содержимое корневого окна доступно немедленно, поэтому
        обработчик вызывается сразу. В Wayland снимок выдаёт портал после
        подтверждения пользователем, и результат приходит сигналом, из-за
        чего единый путь сделан асинхронным.
        """
        if self._session.is_x11:
            try:
                on_ready(grab_virtual_desktop())
            except CaptureBackendError as error:
                self._notify(tr("Снимок экрана"), str(error), is_error=True)
            return

        if is_screenshot_available():
            # Портал универсален для любого композитора Wayland, поэтому
            # проверяется раньше утилит конкретных окружений.
            portal = ScreenshotPortal(self)
            portal.captured.connect(on_ready)
            portal.captured.connect(lambda _image: self._release_portal())
            portal.cancelled.connect(self._release_portal)
            portal.failed.connect(self._on_portal_failed)
            self._screenshot_portal = portal
            portal.take()
            return

        if self._session.has_tool("grim"):
            try:
                on_ready(grab_with_grim())
            except CaptureBackendError as error:
                self._notify(tr("Снимок экрана"), str(error), is_error=True)
            return

        self._notify(
            tr("Снимок экрана"),
            tr("Нет доступного способа съёмки: требуется портал снимков " "либо утилита grim."),
            is_error=True,
        )

    def _on_portal_failed(self, text: str) -> None:
        """Сообщение об отказе портала снимков."""
        self._notify(tr("Снимок экрана"), text, is_error=True)
        self._release_portal()

    def _release_portal(self) -> None:
        """Освобождение ссылки на завершённый запрос портала."""
        self._screenshot_portal = None

    def _perform_screenshot(self, mode: CaptureMode) -> None:
        """Получение снимка согласно выбранному режиму."""
        self._grab_desktop_async(lambda desktop: self._use_desktop(mode, desktop))

    def _use_desktop(self, mode: CaptureMode, desktop: QImage) -> None:
        """Обработка полученного снимка рабочего стола под выбранный режим."""
        if mode is CaptureMode.REGION:
            self._select_region(
                desktop,
                tr("Выделите область для снимка: ЛКМ — выбор, Esc — отмена"),
                lambda rect: self._finish_screenshot(crop_desktop_image(desktop, rect)),
            )
            return

        if mode is CaptureMode.WINDOW:
            # Объект выбирается наведением: под курсором подсвечивается
            # окно либо его часть, щелчок снимает подсвеченное. Ручное
            # протягивание при этом остаётся доступным.
            self._select_region(
                desktop,
                tr(
                    "Наведите указатель на окно и щёлкните. "
                    "Протягивание задаёт область вручную, Esc — отмена"
                ),
                lambda rect: self._finish_screenshot(crop_desktop_image(desktop, rect)),
            )
            return

        # Режим полного экрана возвращает снимок без обрезки.
        self._finish_screenshot(desktop)

    def _select_region(self, desktop: QImage, hint: str, handler: Callable[[QRect], None]) -> None:
        """Показ оверлея выделения области поверх замороженного снимка."""
        overlay = RegionOverlay(desktop, virtual_geometry(), hint)
        # Перечень объектов интерфейса собирается обходом дерева окон и
        # занимает десятки миллисекунд. Сбор вынесен в отдельный поток:
        # оверлей показывается сразу, а подсветка объектов появляется
        # мгновением позже, к началу движения мыши.
        run_async(
            self,
            list_objects,
            lambda objects: self._fill_overlay_objects(overlay, objects),
            None,
            virtual_geometry(),
        )
        overlay.selected.connect(handler)
        overlay.cancelled.connect(lambda: setattr(self, "_overlay", None))
        overlay.selected.connect(lambda _rect: setattr(self, "_overlay", None))
        self._overlay = overlay
        overlay.show_overlay()

    @staticmethod
    def _fill_overlay_objects(overlay: RegionOverlay, objects: object) -> None:
        """Передача собранных объектов интерфейса открытому оверлею."""
        try:
            if isinstance(objects, list):
                overlay.set_objects(objects)
        except RuntimeError:
            # Оверлей успели закрыть до окончания сбора.
            pass

    def _finish_screenshot(self, image: QImage) -> None:
        """Обработка готового снимка согласно настройкам."""
        if image.isNull():
            self._notify(tr("Снимок экрана"), tr("Получено пустое изображение"), is_error=True)
            return

        if self._config.settings.general.copy_to_clipboard:
            # Буфер обмена доступен только из потока интерфейса.
            copy_image_to_clipboard(image)

        if self._config.settings.general.open_editor_after_capture:
            self._open_editor(image)
        else:
            self._save_image(image)

    def _open_editor(self, image: QImage) -> None:
        """Открытие редактора аннотаций для снимка."""
        window = EditorWindow(image)
        window.copyRequested.connect(self._copy_image)
        window.saveRequested.connect(self._save_image)
        window.saveAsRequested.connect(self._save_image_as)
        # Ссылка удерживается до закрытия окна пользователем.
        window.destroyed.connect(lambda: self._forget_editor(window))
        self._editors.append(window)
        window.show()
        window.raise_()
        window.activateWindow()

    def _forget_editor(self, window: EditorWindow) -> None:
        """Освобождение ссылки на закрытое окно редактора."""
        if window in self._editors:
            self._editors.remove(window)

    def _copy_image(self, image: QImage) -> None:
        """Копирование изображения в буфер обмена."""
        copy_image_to_clipboard(image)
        self._notify(tr("Буфер обмена"), tr("Изображение скопировано"))

    def _save_image(self, image: QImage) -> None:
        """Сохранение снимка в настроенный каталог."""
        settings = self._config.settings.images
        path = self._config.build_image_path(extension_for(settings.image_format))
        self._save_image_to(image, path, settings)

    def _save_image_as(self, image: QImage) -> None:
        """Сохранение снимка с выбором пути пользователем."""
        # Окно, запросившее сохранение, закрывается только после успешного
        # выбора пути: отмена диалога должна оставлять правки на месте.
        source = self.sender()
        settings = self._config.settings.images
        suggested = self._config.build_image_path(extension_for(settings.image_format))
        chosen, _filter = QFileDialog.getSaveFileName(
            None,
            tr("Сохранить снимок"),
            str(suggested),
            tr("Изображения (*.png *.jpg *.jpeg *.webp *.avif *.bmp)"),
        )
        if not chosen:
            return

        path = Path(chosen)
        # Формат определяется расширением выбранного файла, а не настройками.
        extension = path.suffix.lstrip(".").lower()
        image_format = {"jpg": "jpeg"}.get(extension, extension) or settings.image_format
        adjusted = ImageSettings(**{**settings.__dict__, "image_format": image_format})
        self._save_image_to(image, path, adjusted)
        if isinstance(source, EditorWindow):
            source.close()

    def _save_image_to(self, image: QImage, path: Path, settings: ImageSettings) -> None:
        """Запись изображения на диск в фоновом потоке."""
        run_async(
            self,
            save_image,
            lambda result: self._notify(tr("Снимок сохранён"), str(result)),
            lambda text: self._notify(tr("Снимок экрана"), text, is_error=True),
            image,
            path,
            settings,
        )

    # ------------------------------------------------------------- запись

    def start_recording(self, mode: object) -> None:
        """Запуск записи экрана в выбранном режиме."""
        if self._recorder.state.is_busy:
            self._notify(tr("Запись"), tr("Запись уже выполняется"))
            return
        if not self._ffmpeg_path:
            self._notify(tr("Запись"), tr("FFmpeg не найден"), is_error=True)
            return

        if not self._session.is_x11:
            # В Wayland область выбирает сам портал в своём диалоге,
            # поэтому режим захвата здесь не учитывается.
            self._start_portal_recording()
            return

        capture_mode = mode if isinstance(mode, CaptureMode) else CaptureMode.REGION
        if capture_mode is CaptureMode.REGION:
            self._grab_desktop_async(
                lambda desktop: self._select_region(
                    desktop,
                    tr("Выделите область для записи: ЛКМ — выбор, Esc — отмена"),
                    self._prepare_recording,
                )
            )
            return

        # Полноэкранная запись ведётся в границах основного монитора:
        # объединённая область нескольких экранов даёт кадр нестандартного
        # размера и зачастую не кодируется аппаратно.
        screen = QGuiApplication.primaryScreen()
        self._prepare_recording(screen.geometry() if screen else virtual_geometry())

    def _start_portal_recording(self) -> None:
        """Согласование захвата экрана через портал ScreenCast."""
        capabilities = self._capabilities
        # Сведения о сборке собираются фоновым опросом; до его завершения
        # запись не блокируется, ошибку в этом случае вернёт сам FFmpeg.
        has_filter = getattr(capabilities, "can_capture_pipewire", True)
        if not has_filter:
            self._notify(
                tr("Запись"),
                tr(
                    "Установленная сборка FFmpeg не содержит фильтра pipewiregrab. "
                    "Запись в Wayland требует FFmpeg версии 7.1 или новее."
                ),
                is_error=True,
            )
            return

        portal = ScreenCastPortal(self)
        portal.ready.connect(self._prepare_recording)
        portal.cancelled.connect(self._release_screencast)
        portal.failed.connect(self._on_screencast_failed)
        self._screencast_portal = portal
        portal.start(show_cursor=self._config.settings.video.show_cursor)

    def _on_screencast_failed(self, text: str) -> None:
        """Сообщение об отказе портала захвата."""
        self._notify(tr("Запись"), text, is_error=True)
        self._release_screencast()

    def _release_screencast(self) -> None:
        """Завершение сеанса портала захвата."""
        if self._screencast_portal is not None:
            self._screencast_portal.close_session()
            self._screencast_portal = None

    def record_monitor(self, index: int) -> None:
        """Запись отдельного монитора по его порядковому номеру."""
        if self._recorder.state.is_busy:
            self._notify(tr("Запись"), tr("Запись уже выполняется"))
            return
        if not self._ffmpeg_path:
            self._notify(tr("Запись"), tr("FFmpeg не найден"), is_error=True)
            return
        if not self._session.is_x11:
            # В Wayland выбор монитора выполняет сам портал.
            self._start_portal_recording()
            return

        monitors = list_monitors()
        if not 0 <= index < len(monitors):
            return
        self._prepare_recording(monitors[index].geometry)

    def _prepare_recording(self, rect: object) -> None:
        """Подготовка звуковых источников перед стартом записи."""
        audio_mode = self._audio_mode()
        if audio_mode is AudioMode.NONE:
            self._launch_recording(rect, [])
            return
        # Опрос звукового сервера выполняется в фоне: обращение к нему
        # занимает десятки миллисекунд и не должно задерживать интерфейс.
        run_async(
            self,
            list_audio_devices,
            lambda devices: self._launch_recording(rect, devices),
            lambda text: self._launch_recording(rect, []),
        )

    def _audio_mode(self) -> AudioMode:
        """Режим работы со звуком из настроек с защитой от неверного значения."""
        try:
            return AudioMode(self._config.settings.video.audio_mode)
        except ValueError:
            return AudioMode.NONE

    def _launch_recording(self, rect: object, devices: object) -> None:
        """Сборка задания записи и его запуск."""
        settings = self._config.settings.video
        # Прямоугольник запоминается для показа рамки во время записи.
        self._recorded_rect = QRect(rect) if isinstance(rect, QRect) else None
        audio_devices: list[AudioDevice] = devices if isinstance(devices, list) else []

        try:
            if isinstance(rect, PipeWireStream):
                # Поток портала уже согласован с пользователем, геометрия
                # задана композитором.
                video_input = build_pipewire_video_input(rect, fps=settings.fps)
            else:
                video_input = build_video_input(
                    self._session,
                    rect,  # type: ignore[arg-type]
                    fps=settings.fps,
                    show_cursor=settings.show_cursor,
                )
        except CaptureBackendError as error:
            self._notify(tr("Запись"), str(error), is_error=True)
            return

        audio_inputs = resolve_audio_inputs(
            self._audio_mode(),
            audio_devices,
            settings.system_device,
            settings.microphone_device,
        )
        audio_mode = self._audio_mode() if audio_inputs else AudioMode.NONE

        job = self._build_job(video_input, audio_inputs, audio_mode)
        if job is None:
            return

        if self._config.settings.general.hide_while_recording:
            # Окна убираются до начала захвата и возвращаются после него:
            # иначе они попали бы в первые кадры записи.
            self._hide_windows()
            QTimer.singleShot(HIDE_SETTLE_MS, lambda: self._begin_recording(job))
            return
        self._begin_recording(job)

    def _begin_recording(self, job: RecordingJob) -> None:
        """Запуск подготовленного задания записи."""
        try:
            self._recorder.start(job)
        except RuntimeError as error:
            self._restore_windows()
            self._notify(tr("Запись"), str(error), is_error=True)
            return

        if self._config.settings.general.hide_while_recording:
            # Панель управления записью скрыта, поэтому способ остановки
            # сообщается уведомлением.
            stop_key = self._config.settings.hotkeys.record_toggle
            hint = tr("клавиша {0}").format(stop_key) if stop_key else tr("меню значка в трее")
            self._notify(tr("Запись начата"), tr("Остановка: {0}").format(hint))
            return
        self._recorder_bar.show_at_corner()

    def _hide_windows(self) -> None:
        """Скрытие собственных окон приложения на время записи."""
        self._hidden_windows = []
        windows: list[QWidget] = [self._recorder_bar, *self._editors]
        if self._settings_dialog is not None:
            windows.append(self._settings_dialog)
        if self._log_window is not None:
            windows.append(self._log_window)

        for window in windows:
            try:
                if window.isVisible():
                    window.hide()
                    self._hidden_windows.append(window)
            except RuntimeError:
                # Окно уже уничтожено: возвращать нечего.
                continue

    def _restore_windows(self) -> None:
        """Возврат скрытых окон после завершения записи."""
        for window in self._hidden_windows:
            try:
                window.show()
            except RuntimeError:
                # Окно закрыли во время записи: восстановление не требуется.
                continue
        self._hidden_windows = []

    def _overrides(self) -> ProfileOverrides:
        """
        Пользовательские уточнения параметров кодирования.

        Значения приводятся к типам перечислений с защитой от неверного
        содержимого файла настроек: непонятное значение не применяется,
        и профиль остаётся без изменений.
        """
        settings = self._config.settings.encoding

        def as_enum(value, kind):  # type: ignore[no-untyped-def]
            """Преобразование строки настроек в значение перечисления."""
            if not value:
                return None
            try:
                return kind(value)
            except ValueError:
                return None

        return ProfileOverrides(
            video_codec=as_enum(settings.video_codec, VideoCodec),
            audio_codec=as_enum(settings.audio_codec, AudioCodec),
            rate_mode=settings.rate_mode,
            crf=settings.crf or None,
            video_bitrate=settings.video_bitrate,
            preset=settings.preset,
            keyint=settings.keyint or None,
            pix_fmt=settings.pix_fmt,
            audio_bitrate=settings.audio_bitrate,
            audio_sample_rate=settings.audio_sample_rate or None,
            audio_channels=settings.audio_channels or None,
            extra_args=settings.extra_args,
        )

    def _make_record_step(  # type: ignore[no-untyped-def]
        self, profile, video_input, audio_inputs, audio_mode
    ):
        """
        Сборщик команды захвата для очередного фрагмента.

        При включённой ручной команде сборка ведётся по образцу
        пользователя, иначе - по параметрам профиля.
        """
        encoding = self._config.settings.encoding
        if encoding.use_custom_command and encoding.custom_command.strip():
            template = encoding.custom_command

            def custom(segment):  # type: ignore[no-untyped-def]
                """Команда по заданному пользователем образцу."""
                return self._profiles.build_custom_step(
                    template, video_input, segment, audio_inputs
                )

            return custom

        def standard(segment):  # type: ignore[no-untyped-def]
            """Команда, собранная по параметрам профиля."""
            return self._profiles.build_record_step(
                profile, video_input, segment, audio_inputs, audio_mode
            )

        return standard

    def _build_job(  # type: ignore[no-untyped-def]
        self, video_input, audio_inputs, audio_mode
    ) -> RecordingJob | None:
        """Формирование задания записи под выбранный профиль."""
        identifier = self._config.settings.video.profile_id
        animation_ids = {item.identifier for item in self._profiles.animation_profiles()}

        if identifier in animation_ids:
            # Анимация собирается из промежуточной записи без потерь:
            # палитра GIF строится по исходным цветам, а не по артефактам.
            animation = self._profiles.animation_profile(identifier)
            base_profile = self._profiles.intermediate_profile()
            output = self._config.build_video_path(animation.container.extension)
            return RecordingJob(
                output_path=output,
                make_step=lambda segment: self._profiles.build_record_step(
                    base_profile, video_input, segment
                ),
                ffmpeg_path=self._ffmpeg_path,
                segment_suffix=f".{base_profile.container.extension}",
                segment_muxer=base_profile.container.muxer,
                post_process=lambda source, target: self._profiles.build_animation_steps(
                    animation, source, target
                ),
            )

        try:
            profile = self._profiles.video_profile(identifier)
        except KeyError:
            self._notify(
                tr("Запись"), tr("Неизвестный профиль: {0}").format(identifier), is_error=True
            )
            return None

        # Уточнения применяются поверх профиля: кодек, качество, пресет
        # и дополнительные аргументы задаются пользователем вручную.
        profile = self._profiles.apply_overrides(profile, self._overrides())
        output = self._config.build_video_path(profile.container.extension)

        encoding = self._config.settings.encoding
        if encoding.use_custom_command and encoding.custom_command.strip():
            # Ошибка в образце команды обнаруживается сразу, до запуска
            # захвата, а не по отсутствию файла в конце записи.
            try:
                self._profiles.build_custom_command(
                    encoding.custom_command, video_input, output, audio_inputs
                )
            except ValueError as error:
                self._notify(tr("Запись"), tr("Ошибка в команде: {0}").format(error), is_error=True)
                return None

        return RecordingJob(
            output_path=output,
            make_step=self._make_record_step(profile, video_input, audio_inputs, audio_mode),
            ffmpeg_path=self._ffmpeg_path,
            # Фрагменты пишутся тем же контейнером, что и результат: склейка
            # копированием потоков возможна только при совпадении форматов.
            segment_suffix=f".{profile.container.extension}",
            segment_muxer=profile.container.muxer,
        )

    def set_audio_mode(self, value: str) -> None:
        """Смена источника звука командой из меню трея."""
        try:
            mode = AudioMode(value)
        except ValueError:
            return
        self._config.settings.video.audio_mode = mode.value
        # Выбор сохраняется сразу: следующий запуск приложения должен
        # начинаться с того же источника.
        self._config.save()
        self._notify(tr("Источник звука"), mode.label)

    def toggle_recording(self) -> None:
        """
        Начало записи области либо остановка уже идущей.

        Одно сочетание клавиш обслуживает оба действия: во время записи
        собственные окна программы скрыты, и отдельная клавиша остановки
        только усложняла бы запоминание.
        """
        if self._recorder.state in (
            RecorderState.RECORDING,
            RecorderState.PAUSED,
            RecorderState.STARTING,
        ):
            self.stop_recording()
            return
        if self._recorder.state is RecorderState.PROCESSING:
            # Предыдущая запись ещё собирается: новая начнётся после неё.
            self._notify(tr("Запись"), tr("Идёт обработка предыдущей записи"))
            return
        self.start_recording(CaptureMode.REGION)

    def toggle_pause(self) -> None:
        """Переключение паузы записи."""
        if self._recorder.state is RecorderState.RECORDING:
            self._recorder.pause()
        elif self._recorder.state is RecorderState.PAUSED:
            self._recorder.resume()

    def stop_recording(self) -> None:
        """Остановка записи с последующей сборкой файла."""
        self._recorder.stop()

    def _on_state_changed(self, state: object) -> None:
        """Отражение состояния записи в интерфейсе."""
        if not isinstance(state, RecorderState):
            return
        self._tray.set_state(state)
        self._recorder_bar.update_state(state)
        self._update_region_frame(state)
        if state in (RecorderState.FINISHED, RecorderState.FAILED, RecorderState.IDLE):
            self._recorder_bar.hide()
            # Панель записи закрывается всегда, прочие окна возвращаются
            # в том виде, в каком были до начала записи.
            self._hidden_windows = [
                window for window in self._hidden_windows if window is not self._recorder_bar
            ]
            self._restore_windows()

    def _update_region_frame(self, state: RecorderState) -> None:
        """
        Показ рамки вокруг записываемой области.

        Рамка рисуется снаружи области и в запись не попадает. Для
        съёмки целого экрана она не показывается: там ей просто нет
        места, а границы кадра и так очевидны.
        """
        area = self._recorded_rect
        if state is RecorderState.RECORDING and area is not None:
            if area.contains(virtual_geometry()):
                return
            self._region_frame.show_for(area, RECORDING_COLOR)
        elif state is RecorderState.PAUSED and self._region_frame.isVisible():
            self._region_frame.set_color(PAUSED_COLOR)
        elif state in (
            RecorderState.FINISHED,
            RecorderState.FAILED,
            RecorderState.IDLE,
            RecorderState.PROCESSING,
        ):
            self._region_frame.hide()

    def _on_elapsed(self, milliseconds: int) -> None:
        """Обновление счётчиков длительности."""
        self._tray.set_elapsed(milliseconds)
        self._recorder_bar.update_elapsed(milliseconds)

    def _on_recording_finished(self, path: object) -> None:
        """Оповещение об успешном завершении записи."""
        self._release_screencast()
        self._notify(tr("Запись сохранена"), str(path))
        if self._quit_after_recording:
            self.quit()

    def _on_recording_failed(self, message: str) -> None:
        """Оповещение о сбое записи."""
        self._release_screencast()
        self._notify(tr("Ошибка записи"), message, is_error=True)
        if self._quit_after_recording:
            self.quit()

    # ----------------------------------------------------------- служебное

    def _on_hotkey(self, action: str) -> None:
        """Выполнение действия, назначенного на сочетание клавиш."""
        handlers = {
            "screenshot_region": lambda: self.take_screenshot(CaptureMode.REGION),
            "screenshot_fullscreen": lambda: self.take_screenshot(CaptureMode.FULLSCREEN),
            "screenshot_window": lambda: self.take_screenshot(CaptureMode.WINDOW),
            "record_toggle": self.toggle_recording,
            "record_toggle_pause": self.toggle_pause,
        }
        handler = handlers.get(action)
        if handler is not None:
            handler()

    def _append_log(self, line: str) -> None:
        """Накопление строки журнала с записью в файл и передачей в окно."""
        self._log_lines.append(line)
        if self._log_window is not None:
            self._log_window.append(line)
        self._write_log_file(line)

    def _write_log_file(self, line: str) -> None:
        """
        Дозапись строки в файл журнала.

        Файл нужен для разбора сбоев после закрытия приложения. Размер
        ограничен: при превышении предела файл начинается заново.
        """
        try:
            handle = self._log_handle()
            if handle is None:
                return
            stamp = datetime.now().strftime("%H:%M:%S")
            handle.write(f"{stamp} {line}\n")
            # Сброс на диск выполняется сразу: журнал нужен и после
            # аварийного завершения, когда закрыть файл уже некому.
            handle.flush()
        except OSError:
            # Журнал является вспомогательным средством: невозможность
            # записи не должна влиять на работу приложения.
            self._log_file = None

    def _log_handle(self) -> TextIO | None:
        """Открытый файл журнала с учётом предельного размера."""
        path = log_file_path()
        if self._log_file is not None:
            if self._log_file.tell() <= LOG_FILE_LIMIT:
                return self._log_file
            # Файл разросся: он начинается заново, чтобы не занимать место.
            self._log_file.close()
            self._log_file = None

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            mode = "w" if path.exists() and path.stat().st_size > LOG_FILE_LIMIT else "a"
            # Приведение типа требуется стабам: открытие текстового файла
            # описано в них обобщённым видом.
            self._log_file = cast(TextIO, path.open(mode, encoding="utf-8"))
        except OSError:
            return None
        return self._log_file

    def open_log(self) -> None:
        """Показ окна журнала работы внешних процессов."""
        if self._log_window is not None:
            self._log_window.raise_()
            self._log_window.activateWindow()
            return
        window = LogWindow(list(self._log_lines))
        window.finished.connect(lambda _result: self._close_log())
        self._log_window = window
        window.show()

    def _close_log(self) -> None:
        """Освобождение ссылки на закрытое окно журнала."""
        self._log_window = None

    def _diagnostics_text(self) -> str:
        """Краткие сведения о сборке FFmpeg для окна настроек."""
        if not self._ffmpeg_path:
            return tr("FFmpeg не найден: запись недоступна.")
        version = getattr(self._capabilities, "version", "")
        text = f"FFmpeg: {self._ffmpeg_path}" + (
            tr(", версия {0}").format(version) if version else ""
        )
        problems = []
        if self._capabilities is not None:
            problems = self._capabilities.missing_essentials()  # type: ignore[attr-defined]
        if not is_avif_available():
            problems.append(tr("Формат AVIF недоступен: не установлен pillow-avif-plugin."))
        return text + ("\n" + "\n".join(problems) if problems else "")

    def open_settings(self) -> None:
        """Показ окна настроек."""
        if self._settings_dialog is not None:
            # Повторный вызов поднимает уже открытое окно.
            self._settings_dialog.raise_()
            self._settings_dialog.activateWindow()
            return

        dialog = SettingsDialog(
            self._config, self._profiles, self._session, self._diagnostics_text()
        )
        dialog.settingsSaved.connect(self._on_settings_saved)
        dialog.hotkeyCaptureChanged.connect(self._on_hotkey_capture)
        dialog.finished.connect(lambda _result: self._close_settings())
        self._settings_dialog = dialog
        dialog.show()

    def _on_hotkey_capture(self, active: bool) -> None:
        """Приостановка перехвата на время набора нового сочетания."""
        if active:
            self._hotkeys.suspend()
        else:
            self._hotkeys.resume()

    def _close_settings(self) -> None:
        """Освобождение ссылки на закрытое окно настроек."""
        # Перехват возобновляется безусловно: окно могло быть закрыто во
        # время набора сочетания, и приостановка осталась бы навсегда.
        self._hotkeys.resume()
        self._settings_dialog = None

    def _on_settings_saved(self) -> None:
        """Применение изменённых настроек без перезапуска приложения."""
        self._ffmpeg_path = self._resolve_ffmpeg()
        self._profiles = VideoProfileManager(ffmpeg_path=self._ffmpeg_path or "ffmpeg")
        self._tray.set_recording_allowed(self._recording_possible())
        if self._ffmpeg_path:
            self._probe.start(self._ffmpeg_path)
        self._apply_hotkeys()
        # Язык мог измениться: меню значка собирается заново, прочие окна
        # создаются при открытии и получат новый язык сами.
        set_language(self._config.settings.general.language)
        self._tray.rebuild_menu()
        self._tray.set_audio_mode(self._audio_mode())
        self._notify(tr("Настройки"), tr("Изменения сохранены"))

    def open_images_folder(self) -> None:
        """Открытие каталога снимков в файловом менеджере."""
        self._open_folder(self._config.images_dir())

    def open_videos_folder(self) -> None:
        """Открытие каталога записей в файловом менеджере."""
        self._open_folder(self._config.videos_dir())

    def _open_folder(self, directory: Path) -> None:
        """Показ каталога в файловом менеджере рабочего стола."""
        try:
            # Каталог мог ещё не существовать: до первого сохранения он
            # не создаётся, а открыть его пользователь вправе и раньше.
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            self._notify(tr("Открытие папки"), str(error), is_error=True)
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory)))

    def quit(self) -> None:
        """Завершение работы приложения."""
        self._region_frame.hide()
        if self._recorder.state.is_busy and not self._quit_after_recording:
            # Незавершённая запись сначала корректно останавливается,
            # иначе файл останется без финализированного заголовка.
            self._quit_after_recording = True
            self._notify(tr("Выход"), tr("Завершение записи перед выходом…"))
            self._recorder.stop()
            return

        self._hotkeys.stop()
        self._tray.hide()
        if self._log_file is not None:
            # Файл журнала закрывается явно: содержимое уже сброшено, но
            # освобождение дескриптора относится к порядку завершения.
            try:
                self._log_file.close()
            except OSError:
                pass
            self._log_file = None
        QApplication.quit()

    def _notify(self, title: str, message: str, is_error: bool = False) -> None:
        """
        Показ уведомления с обязательной записью в журнал.

        Сообщение попадает в журнал независимо от настройки, поэтому
        отключение всплывающих окон не лишает пользователя сведений о
        происходящем: их видно в окне журнала и в файле.
        """
        mark = tr("ОШИБКА") if is_error else tr("Сообщение")
        self._append_log(f"{mark}: {title}: {message}")
        if self._config.settings.general.show_notifications:
            self._tray.notify(title, message, is_error)
