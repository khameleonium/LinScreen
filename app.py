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
from typing import Any, Callable, TextIO, cast

from PySide6.QtCore import QObject, QRect, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QGuiApplication, QImage
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox, QWidget

from backends import create_backend
from capture.audio import AudioDevice, list_audio_devices, resolve_audio_inputs
from capture.screen import CaptureMode, crop_desktop_image, list_monitors, virtual_geometry
from core.bus_service import BusService
from core.config import ConfigManager
from core.identity import APPLICATION_SLUG
from core.session import detect_session
from core.workers import run_async
from editor.window import EditorWindow
from encoder.ffmpeg import FFmpegNotFoundError, find_ffmpeg
from encoder.images import (
    ImageSettings,
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
from ui.magnifier import MagnifierOptions
from ui.recorder_bar import RecorderBar
from ui.region_frame import PAUSED_COLOR, RECORDING_COLOR
from ui.settings import SettingsDialog
from ui.log_window import LogWindow
from ui.tray import TrayIcon

# Предельный размер файла журнала. Приложение работает сутками, и
# неограниченный файл со временем занял бы заметное место.
LOG_FILE_LIMIT = 1024 * 1024

# Пауза между скрытием окон и запуском захвата. Composited-окружению
# требуется время на перерисовку, иначе окно попадёт в первые кадры.
HIDE_SETTLE_MS = 300

# Пауза перед снимком экрана. Меню значка в трее закрывается с анимацией,
# и без паузы его тень успевала бы попасть в снимок.
MENU_SETTLE_MS = 200


def show_folder(uri: str) -> bool:
    """
    Показ каталога в файловом менеджере через org.freedesktop.FileManager1.

    Открытие по типу файла ненадёжно: в части систем каталоги сопоставлены
    не файловому менеджеру (например, анализатору дисков). Служба
    FileManager1 предоставляется самим файловым менеджером - Nautilus,
    Nemo, Dolphin - и открывает именно его. Возвращается признак успеха;
    при неудаче вызывающая сторона открывает каталог обычным способом.
    Функция блокирующая и вызывается в фоне.
    """
    from core import portal

    try:
        portal.call(
            "/org/freedesktop/FileManager1",
            "org.freedesktop.FileManager1",
            "ShowFolders",
            "ass",
            ([uri], ""),
            service="org.freedesktop.FileManager1",
            timeout=15.0,
        )
    except portal.PortalError:
        return False
    return True


def log_file_path() -> Path:
    """Путь файла журнала работы внешних процессов."""
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / APPLICATION_SLUG / "linscreen.log"


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
        # Набор модулей графической системы: X11 или Wayland.
        self._backend = create_backend(self._session, self)
        self._backend.notice.connect(self._on_backend_notice)
        self._hotkeys = self._backend.create_hotkey_manager(self)
        # Служба на шине принимает команды из командной строки и отчёты
        # скриптов композитора.
        self._bus_service = BusService(self)
        # Источник кадров текущей записи: сеанс портала живёт всю запись.
        self._video_source: Any = None
        self._recorder_bar = RecorderBar()
        self._probe = CapabilityProbe(self)

        # Окна редактора и оверлей удерживаются ссылками: без этого сборщик
        # мусора уничтожит их сразу после выхода из обработчика.
        self._editors: list[EditorWindow] = []
        self._overlay: Any = None
        # Рамка, обводящая записываемую область во время съёмки.
        self._region_frame = self._backend.create_frame(self)
        # Область текущей записи: нужна для показа рамки.
        self._recorded_rect: QRect | None = None
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

        # Признак изменения радиуса лупы, ещё не записанного в файл.
        self._magnifier_changed = False
        # Открытое окно «Компоненты системы».
        self._requirements_dialog: Any = None
        # Вырезание области из последнего снимка: набор модулей может брать
        # её из более чёткого источника, чем общий снимок.
        self._crop_snapshot: Callable[[QRect], QImage] | None = None

        self._connect_signals()

    # ------------------------------------------------------------- запуск

    def start(self) -> None:
        """Запуск приложения: показ значка и регистрация клавиш."""
        self._bus_service.start()
        self._tray.show()
        self._tray.set_recording_allowed(self._recording_possible())
        self._tray.set_audio_mode(self._audio_mode())
        self._apply_hotkeys()

        if self._ffmpeg_path:
            # Опрос возможностей сборки выполняется в фоне и не задерживает старт.
            self._probe.start(self._ffmpeg_path)
        if self._config.settings.general.check_system_on_start:
            # Недостающие компоненты системы проверяются в фоне; окно
            # показывается, только если чего-то не хватает.
            self.check_system(show_when_ok=False)

    def _resolve_ffmpeg(self) -> str:
        """Определение пути к бинарнику FFmpeg."""
        try:
            return find_ffmpeg(self._config.settings.general.ffmpeg_path or None)
        except FFmpegNotFoundError:
            # Отсутствие FFmpeg не мешает работе со снимками экрана.
            return ""

    def _recording_possible(self) -> bool:
        """Проверка принципиальной возможности записи в текущей сессии."""
        return self._backend.recording_possible(self._ffmpeg_path, self._capabilities)

    def _on_backend_notice(self, title: str, message: str, log_only: bool) -> None:
        """Сообщение набора модулей графической системы."""
        if log_only:
            self._append_log(f"{title}: {message}")
        else:
            self._notify(title, message)

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
        self._tray.systemCheckRequested.connect(lambda: self.check_system(show_when_ok=True))
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
        self._hotkeys.bound.connect(self._on_hotkeys_bound)
        self._hotkeys.conflicts.connect(self._on_hotkey_conflicts)
        self._hotkeys.changedExternally.connect(self._on_hotkey_changed_externally)
        self._bus_service.actionRequested.connect(self._on_hotkey)

        self._probe.ready.connect(self._on_capabilities)
        self._probe.failed.connect(lambda text: self._notify("FFmpeg", text, is_error=True))

    def _apply_hotkeys(self, force: bool = True) -> None:
        """
        Перерегистрация глобальных клавиш по текущим настройкам.

        Источник истины - настройки приложения: они применяются всегда, а
        изменения, сделанные в настройках рабочего стола, переносятся в них
        сразу (см. _on_hotkey_changed_externally). Иначе сочетание, однажды
        не назначенное из-за конфликта, осталось бы пустым навсегда.
        """
        self._hotkeys.apply(self._config.settings.hotkeys.as_mapping(), force)

    def _on_hotkey_changed_externally(self, action: str, combination: str) -> None:
        """Перенос сочетания, изменённого в настройках рабочего стола."""
        hotkeys = self._config.settings.hotkeys
        if not hasattr(hotkeys, action):
            return
        setattr(hotkeys, action, combination)
        self._config.save()
        self._append_log(
            tr("Сочетание изменено в настройках рабочего стола: {0} = {1}").format(
                action, combination or tr("не назначено")
            )
        )

    def _on_capabilities(self, capabilities: object) -> None:
        """Применение сведений о возможностях установленной сборки FFmpeg."""
        encoders = getattr(capabilities, "encoders", None)
        if encoders is None:
            return
        self._capabilities = capabilities
        # Профили, требующие отсутствующих энкодеров, будут заменены
        # доступными при сборке команды записи.
        self._profiles.set_available_encoders(encoders)
        problems = capabilities.missing_essentials(  # type: ignore[attr-defined]
            self._backend.name == "x11"
        )
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
            lambda desktop: self._finish_screenshot(self._crop(desktop, geometry))
        )

    def _grab_desktop_async(self, on_ready: Callable[[QImage], None]) -> None:
        """
        Снимок всего рабочего стола средствами набора модулей.

        Запрос выполняется после короткой паузы: меню значка в трее должно
        успеть исчезнуть с экрана.
        """

        def deliver(desktop: QImage, crop: Callable[[QRect], QImage]) -> None:
            """Приём снимка и способа вырезания области из него."""
            self._crop_snapshot = crop
            on_ready(desktop)

        def request() -> None:
            """Запрос снимка."""
            self._backend.grab_desktop(
                self,
                deliver,
                lambda text: self._on_capture_error(tr("Снимок экрана"), text),
                self._config.settings.images.hide_cursor,
            )

        QTimer.singleShot(MENU_SETTLE_MS, request)

    def _perform_screenshot(self, mode: CaptureMode) -> None:
        """Получение снимка согласно выбранному режиму."""
        self._grab_desktop_async(lambda desktop: self._use_desktop(mode, desktop))

    def _crop(self, desktop: QImage, rect: QRect) -> QImage:
        """Вырезание области снимка с наилучшей чёткостью."""
        if self._crop_snapshot is not None:
            return self._crop_snapshot(rect)
        return crop_desktop_image(desktop, rect)

    def _use_desktop(self, mode: CaptureMode, desktop: QImage) -> None:
        """Обработка полученного снимка рабочего стола под выбранный режим."""
        if mode is CaptureMode.REGION:
            self._select_region(
                desktop,
                tr("Выделите область для снимка: ЛКМ — выбор, Esc — отмена"),
                lambda rect: self._finish_screenshot(self._crop(desktop, rect)),
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
                lambda rect: self._finish_screenshot(self._crop(desktop, rect)),
            )
            return

        # Режим полного экрана возвращает снимок без обрезки.
        self._finish_screenshot(desktop)

    def _select_region(self, desktop: QImage, hint: str, handler: Callable[[QRect], None]) -> None:
        """Показ оверлея выделения области поверх замороженного снимка."""
        magnifier = self._config.settings.magnifier
        overlay = self._backend.create_overlay(
            desktop,
            virtual_geometry(),
            hint,
            MagnifierOptions(
                enabled=magnifier.enabled,
                radius=magnifier.radius,
                zoom=magnifier.zoom,
                show_info=magnifier.show_info,
            ),
        )
        # Радиус, выбранный колесом мыши, запоминается для следующих
        # выделений; файл записывается однократно, при закрытии оверлея.
        overlay.magnifierRadiusChanged.connect(self._remember_magnifier_radius)
        if self._backend.detects_objects:
            # Перечень окон собирается десятки миллисекунд. Сбор вынесен в
            # отдельный поток: оверлей показывается сразу, а подсветка окон
            # появляется мгновением позже, к началу движения мыши.
            run_async(
                self,
                self._backend.list_objects,
                lambda objects: self._fill_overlay_objects(overlay, objects),
                None,
                virtual_geometry(),
            )
        overlay.selected.connect(handler)
        overlay.cancelled.connect(lambda: setattr(self, "_overlay", None))
        overlay.selected.connect(lambda _rect: setattr(self, "_overlay", None))
        overlay.cancelled.connect(self._save_magnifier_radius)
        overlay.selected.connect(lambda _rect: self._save_magnifier_radius())
        self._overlay = overlay
        overlay.show_overlay()

    def _remember_magnifier_radius(self, radius: int) -> None:
        """Запоминание радиуса лупы с отложенной записью файла настроек."""
        self._config.settings.magnifier.radius = radius
        self._magnifier_changed = True

    def _save_magnifier_radius(self) -> None:
        """Запись изменённого радиуса лупы после закрытия оверлея."""
        if self._magnifier_changed:
            self._magnifier_changed = False
            self._config.save()

    @staticmethod
    def _fill_overlay_objects(overlay: Any, objects: object) -> None:
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
            self._backend.copy_image(self, image)

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
        self._backend.copy_image(self, image)
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

    def record_monitor(self, index: int) -> None:
        """Запись отдельного монитора по его порядковому номеру."""
        if self._recorder.state.is_busy:
            self._notify(tr("Запись"), tr("Запись уже выполняется"))
            return
        if not self._ffmpeg_path:
            self._notify(tr("Запись"), tr("FFmpeg не найден"), is_error=True)
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
        """
        Открытие источника видео перед сборкой задания записи.

        В Wayland источник открывается асинхронно: при первой записи
        композитор спрашивает разрешение у пользователя.
        """
        if self._video_source is not None:
            self._notify(tr("Запись"), tr("Запись уже выполняется"))
            return
        settings = self._config.settings.video
        source = self._backend.create_video_source(self)
        self._video_source = source
        source.ready.connect(lambda video_input: self._on_stream_ready(video_input, devices))
        source.cancelled.connect(self._on_stream_cancelled)
        source.failed.connect(self._on_stream_failed)
        source.open(cast(QRect, rect), settings.fps, settings.show_cursor)

    def _on_stream_cancelled(self) -> None:
        """Отказ пользователя в диалоге выбора экрана."""
        self._release_video_source()
        self._notify(tr("Запись"), tr("Запись отменена: доступ к экрану не предоставлен"))

    def _on_stream_failed(self, message: str) -> None:
        """Сбой открытия видеопотока."""
        self._release_video_source()
        self._on_capture_error(tr("Запись"), message)

    def _on_capture_error(self, title: str, message: str) -> None:
        """
        Сбой захвата экрана.

        Если причина в недостающем компоненте системы, вместо сообщения
        об ошибке открывается окно с командами исправления.
        """
        if self._backend.is_missing_component_error(message):
            self._append_log(f"{tr('ОШИБКА')}: {title}: {message}")
            self.check_system(show_when_ok=True)
            return
        self._notify(title, message, is_error=True)
        # Отказ портала часто вызван недостающим разрешением или
        # компонентом: проверка покажет окно исправления, если так и есть.
        self.check_system(show_when_ok=False)

    def _release_video_source(self) -> None:
        """Закрытие источника видео (в Wayland - сеанса захвата экрана)."""
        source, self._video_source = self._video_source, None
        if source is not None:
            source.close()
            source.deleteLater()

    def _on_stream_ready(self, video_input: object, devices: object) -> None:
        """Сборка задания записи по готовому видеопотоку и его запуск."""
        source = self._video_source
        if source is None:
            return
        # Прямоугольник запоминается для показа рамки во время записи.
        self._recorded_rect = source.recorded_rect
        audio_devices: list[AudioDevice] = devices if isinstance(devices, list) else []
        settings = self._config.settings.video

        audio_inputs = resolve_audio_inputs(
            self._audio_mode(),
            audio_devices,
            settings.system_device,
            settings.microphone_device,
        )
        audio_mode = self._audio_mode() if audio_inputs else AudioMode.NONE

        job = self._build_job(video_input, audio_inputs, audio_mode)
        if job is None:
            self._release_video_source()
            return
        # В Wayland кадры каждого фрагмента доставляет процесс GStreamer,
        # готовящийся источником по запросу контроллера записи.
        job.feeder = source.feeder

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
            self._release_video_source()
            self._notify(tr("Запись"), str(error), is_error=True)
            return

        if self._config.settings.general.hide_while_recording:
            # Панель управления записью скрыта, поэтому способ остановки
            # сообщается уведомлением.
            stop_key = self._config.settings.hotkeys.record_toggle
            hint = tr("клавиша {0}").format(stop_key) if stop_key else tr("меню значка в трее")
            self._notify(tr("Запись начата"), tr("Остановка: {0}").format(hint))
            return
        bar = self._recorder_bar
        position = bar.corner_position()
        bar.show_at_corner()
        self._backend.place_recorder_bar(self, bar, position)

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
            # Сеанс портала закрывается: значок демонстрации экрана исчезает
            # из панели композитора.
            self._release_video_source()
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
        frame = self._region_frame
        if state is RecorderState.RECORDING and area is not None:
            if any(area.contains(item.geometry) for item in list_monitors()):
                return
            # Набор модулей сам решает, как показать рамку; повторный показ
            # после паузы лишь возвращает ей цвет записи.
            frame.show_for(area, RECORDING_COLOR)
        elif state is RecorderState.PAUSED and frame.is_shown():
            frame.set_color(PAUSED_COLOR)
        elif state in (
            RecorderState.FINISHED,
            RecorderState.FAILED,
            RecorderState.IDLE,
            RecorderState.PROCESSING,
        ):
            frame.hide()

    def _on_elapsed(self, milliseconds: int) -> None:
        """Обновление счётчиков длительности."""
        self._tray.set_elapsed(milliseconds)
        self._recorder_bar.update_elapsed(milliseconds)

    def _on_recording_finished(self, path: object) -> None:
        """Оповещение об успешном завершении записи."""
        self._notify(tr("Запись сохранена"), str(path))
        if self._quit_after_recording:
            self.quit()

    def _on_recording_failed(self, message: str) -> None:
        """Оповещение о сбое записи."""
        self._notify(tr("Ошибка записи"), message, is_error=True)
        if self._quit_after_recording:
            self.quit()

    # ----------------------------------------------------------- служебное

    # ------------------------------------------------ компоненты системы

    def check_system(self, show_when_ok: bool = True) -> None:
        """
        Проверка недостающих компонентов системы с показом окна.

        При show_when_ok окно показывается и при полном порядке - так
        пользователь, открывший его вручную, получает ответ.
        """
        from core import requirements

        run_async(
            self,
            requirements.check,
            lambda report: self._on_system_checked(report, show_when_ok),
            lambda text: self._append_log(tr("Проверка системы не удалась: {0}").format(text)),
            self._session,
            TrayIcon.is_available(),
        )

    def _on_system_checked(self, report: object, show_when_ok: bool) -> None:
        """Показ итога проверки системы."""
        from core.requirements import Report
        from ui.requirements_dialog import RequirementsDialog

        if not isinstance(report, Report):
            return
        for problem in report.problems:
            self._append_log(tr("Не хватает компонента: {0}").format(problem.title))
        dialog = self._requirements_dialog
        if dialog is not None:
            dialog.set_report(report)
            if report.problems or show_when_ok:
                dialog.raise_()
                dialog.activateWindow()
            return
        if not report.problems and not show_when_ok:
            return
        dialog = RequirementsDialog(report, self._config.settings.general.check_system_on_start)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.recheckRequested.connect(lambda: self.check_system(show_when_ok=True))
        dialog.fixFinished.connect(self._on_system_fixed)
        dialog.checkOnStartChanged.connect(self._set_check_on_start)
        dialog.destroyed.connect(lambda: setattr(self, "_requirements_dialog", None))
        self._requirements_dialog = dialog
        dialog.show()

    def _set_check_on_start(self, enabled: bool) -> None:
        """Сохранение признака проверки системы при запуске."""
        self._config.settings.general.check_system_on_start = enabled
        self._config.save()

    def _on_system_fixed(self, _success: bool) -> None:
        """
        Повторная проверка после исправления.

        Значок трея показывается заново: расширение трея могло появиться,
        а зарегистрироваться в нём можно только повторным показом.
        """
        self._tray.hide()
        self._tray.show()
        # Порталу и оболочке нужно мгновение, чтобы подхватить изменения.
        QTimer.singleShot(1500, lambda: self.check_system(show_when_ok=True))

    def report_missing_tray(self) -> None:
        """
        Сообщение об отсутствии системного трея.

        Ярлык делается видимым в меню приложений: его действия (правый
        щелчок по значку) заменяют меню трея.
        """
        from core import autostart, notifications
        from core.session import Desktop

        from ui.tray import install_application_icon

        autostart.install_menu_entry(install_application_icon())
        hint = tr(
            "Системный трей недоступен. Действия — снимки, запись, настройки, выход — "
            "доступны правым щелчком по значку LinScreen в меню приложений."
        )
        if self._session.desktop_kind is Desktop.GNOME:
            hint += tr(
                " Чтобы значок появился в верхней панели, включите расширение "
                "«AppIndicator and KStatusNotifierItem Support» в приложении «Расширения»."
            )
        self._append_log(hint)
        if self._session.desktop_kind is Desktop.GNOME and (
            self._config.settings.general.check_system_on_start
        ):
            # В GNOME трей даёт расширение: окно компонентов системы
            # предложит его установить и включить.
            return
        title = tr("LinScreen работает без значка в трее")
        notifications.send(title, hint, persistent=True)
        # Окно дублирует уведомление: служба уведомлений бывает недоступна,
        # а без этого сообщения приложение выглядело бы незапущенным.
        box = QMessageBox(QMessageBox.Icon.Information, title, hint, QMessageBox.StandardButton.Ok)
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        box.setModal(False)
        box.show()
        self._tray_message = box

    def perform_action(self, action: str) -> None:
        """Выполнение действия по имени: из командной строки или клавиш."""
        self._on_hotkey(action)

    def _on_hotkey(self, action: str) -> None:
        """Выполнение действия, назначенного на сочетание клавиш."""
        handlers = {
            "screenshot_region": lambda: self.take_screenshot(CaptureMode.REGION),
            "screenshot_fullscreen": lambda: self.take_screenshot(CaptureMode.FULLSCREEN),
            "screenshot_window": lambda: self.take_screenshot(CaptureMode.WINDOW),
            "record_toggle": self.toggle_recording,
            "record_toggle_pause": self.toggle_pause,
            "open_settings": self.open_settings,
            "check_system": lambda: self.check_system(show_when_ok=True),
            "quit": self.quit,
        }
        handler = handlers.get(action)
        if handler is not None:
            handler()

    def _on_hotkeys_bound(self, assigned: object) -> None:
        """Запись в журнал сочетаний, назначенных рабочим столом."""
        if not isinstance(assigned, dict):
            return
        described = ", ".join(
            f"{action}: {trigger or tr('не назначено')}" for action, trigger in assigned.items()
        )
        self._append_log(tr("Сочетания клавиш зарегистрированы: {0}").format(described))
        general = self._config.settings.general
        if self._hotkeys.backend == "kglobalaccel" and not general.shortcuts_notice_shown:
            # Приложение добавило свои действия в настройки рабочего стола:
            # пользователь должен знать, где они и как их убрать.
            general.shortcuts_notice_shown = True
            self._config.save()
            self._notify(
                tr("Горячие клавиши"),
                tr(
                    "Сочетания LinScreen добавлены в «Параметры системы» → «Комбинации "
                    "клавиш» → «LinScreen»: там их можно изменить. Убрать их вместе с "
                    "другими следами программы можно в «Настройки» → «Общие» → «Удалить "
                    "программу из системы…» или командой «linscreen --uninstall»."
                ),
            )

    def _on_hotkey_conflicts(self, conflicts: object) -> None:
        """Сообщение о сочетаниях, занятых другими приложениями."""
        if not isinstance(conflicts, dict) or not conflicts:
            return
        from core.actions import action_titles

        titles = action_titles()
        lines = [
            tr("«{0}» — {1}: занято «{2}»").format(titles.get(action, action), key, owner)
            for action, (key, owner) in conflicts.items()
        ]
        self._notify(
            tr("Горячие клавиши"),
            tr(
                "Рабочий стол не назначил сочетания, занятые другими приложениями:\n{0}\n"
                "Освободите их в настройках клавиш рабочего стола либо выберите другие "
                "в «Настройки» → «Клавиши»."
            ).format("\n".join(lines)),
            is_error=True,
        )

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
            problems = self._capabilities.missing_essentials(  # type: ignore[attr-defined]
                self._backend.name == "x11"
            )
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
        dialog.uninstallRequested.connect(self.uninstall)
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
        self._apply_hotkeys(force=True)
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
        url = QUrl.fromLocalFile(str(directory))

        def fallback(shown: object = False) -> None:
            """Обычное открытие, если файловый менеджер не ответил."""
            if not shown:
                QDesktopServices.openUrl(url)

        run_async(self, show_folder, fallback, lambda _text: fallback(), url.toString())

    def uninstall(self, purge: bool) -> None:
        """Удаление следов программы из системы с последующим выходом."""
        from core.uninstall import uninstall

        # Сочетания снимаются до удаления, иначе служба рабочего стола
        # восстановит их при следующей регистрации.
        self._hotkeys.stop()
        run_async(self, uninstall, self._on_uninstalled, self._on_uninstalled, purge)

    def _on_uninstalled(self, result: object) -> None:
        """Отчёт об удалении и завершение работы."""
        if isinstance(result, list):
            text = tr("Удалено:\n{0}").format("\n".join(result)) if result else tr(
                "Следов программы в системе не найдено."
            )
        else:
            text = tr("Удаление завершилось с ошибкой: {0}").format(result)
        QMessageBox.information(None, tr("Удаление программы из системы"), text)
        self.quit()

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
        self._bus_service.stop()
        self._release_video_source()
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
