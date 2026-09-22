"""
Окно настроек приложения.

Диалог редактирует копию значений и записывает их в менеджер конфигурации
только при подтверждении. Перечисление звуковых устройств выполняется в
фоновом потоке: обращение к звуковому серверу занимает заметное время и
подвесило бы открытие окна.
"""

from __future__ import annotations

from core.i18n import tr

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QMessageBox,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from capture.audio import AudioDevice, list_audio_devices
from core.autostart import (
    is_enabled as autostart_is_enabled,
    menu_entry_installed,
    set_enabled as autostart_set,
    set_menu_entry,
)
from core.config import ConfigManager
from core.i18n import available_languages
from core.session import DesktopSession
from core.workers import run_async
from encoder.images import FORMAT_TITLES
from encoder.profiles import (
    AudioCodec,
    AudioMode,
    ProfileOverrides,
    VideoCodec,
    VideoProfileManager,
)


class PathChooser(QWidget):
    """Поле пути с кнопкой выбора каталога."""

    def __init__(self, value: str, placeholder: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._edit = QLineEdit(value)
        self._edit.setPlaceholderText(placeholder)
        button = QPushButton(tr("Обзор…"))
        button.clicked.connect(self._choose)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._edit)
        layout.addWidget(button)

    def _choose(self) -> None:
        """Выбор каталога системным диалогом."""
        current = self._edit.text() or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, tr("Выбор каталога"), current)
        if chosen:
            self._edit.setText(chosen)

    def value(self) -> str:
        """Текущее значение поля."""
        return self._edit.text().strip()


class SettingsDialog(QDialog):
    """Диалог настройки путей, форматов, кодеков и горячих клавиш."""

    # Настройки сохранены: приложению требуется перечитать значения.
    settingsSaved = Signal()
    # Набор сочетания клавиш начат или завершён: на это время глобальный
    # перехват приостанавливается.
    hotkeyCaptureChanged = Signal(bool)

    def __init__(
        self,
        config: ConfigManager,
        profiles: VideoProfileManager,
        session: DesktopSession,
        diagnostics: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._profiles = profiles
        self._session = session
        self._diagnostics = diagnostics
        self._devices: list[AudioDevice] = []

        self.setWindowTitle(tr("Настройки LinScreen"))
        self.setMinimumWidth(560)

        tabs = QTabWidget(self)
        tabs.addTab(self._build_general_tab(), tr("Общие"))
        tabs.addTab(self._build_image_tab(), tr("Снимки"))
        tabs.addTab(self._build_video_tab(), tr("Запись"))
        tabs.addTab(self._build_encoding_tab(), tr("Кодирование"))
        tabs.addTab(self._build_hotkey_tab(), tr("Клавиши"))

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText(tr("Сохранить"))
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(tr("Отмена"))
        buttons.accepted.connect(self._apply)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(tabs)
        layout.addWidget(buttons)

        # Список звуковых устройств подгружается после показа окна.
        run_async(self, list_audio_devices, self._fill_audio_devices)

    # -------------------------------------------------------------- вкладки

    def _build_general_tab(self) -> QWidget:
        """Вкладка общих параметров и каталогов."""
        settings = self._config.settings
        page = QWidget()
        form = QFormLayout(page)

        self._images_dir = PathChooser(settings.paths.images_dir, str(self._config.images_dir()))
        self._videos_dir = PathChooser(settings.paths.videos_dir, str(self._config.videos_dir()))
        form.addRow(tr("Каталог снимков:"), self._images_dir)
        form.addRow(tr("Каталог записей:"), self._videos_dir)

        self._image_template = QLineEdit(settings.paths.image_template)
        self._video_template = QLineEdit(settings.paths.video_template)
        # Шаблон обрабатывается функцией strftime, поэтому в подсказке
        # приводятся её управляющие последовательности.
        hint = tr("Допустимы подстановки strftime: %Y, %m, %d, %H, %M, %S")
        self._image_template.setToolTip(hint)
        self._video_template.setToolTip(hint)
        form.addRow(tr("Шаблон имени снимка:"), self._image_template)
        form.addRow(tr("Шаблон имени записи:"), self._video_template)

        self._ffmpeg_path = QLineEdit(settings.general.ffmpeg_path)
        self._ffmpeg_path.setPlaceholderText(tr("Определяется автоматически"))
        form.addRow(tr("Путь к FFmpeg:"), self._ffmpeg_path)

        self._copy_clipboard = QCheckBox(tr("Копировать снимок в буфер обмена"))
        self._copy_clipboard.setChecked(settings.general.copy_to_clipboard)
        form.addRow(self._copy_clipboard)

        self._open_editor = QCheckBox(tr("Открывать редактор после снимка"))
        self._open_editor.setChecked(settings.general.open_editor_after_capture)
        form.addRow(self._open_editor)

        self._notifications = QCheckBox(tr("Показывать всплывающие уведомления"))
        self._notifications.setChecked(settings.general.show_notifications)
        self._notifications.setToolTip(
            tr(
                "Отключение убирает всплывающие окна. Сообщения продолжают "
                "записываться в журнал и в файл журнала."
            )
        )
        form.addRow(self._notifications)

        self._hide_while_recording = QCheckBox(tr("Скрывать окна программы во время записи"))
        self._hide_while_recording.setChecked(settings.general.hide_while_recording)
        self._hide_while_recording.setToolTip(
            tr(
                "Панель записи и окна программы не попадут в кадр. "
                "Управление остаётся через значок в трее и горячие клавиши."
            )
        )
        form.addRow(self._hide_while_recording)

        self._language = QComboBox()
        # Порядок выбора: определение по окружению и найденные словари.
        self._language.addItem(tr("Как в системе"), "auto")
        for code, title in available_languages().items():
            self._language.addItem(title, code)
        index = self._language.findData(settings.general.language)
        self._language.setCurrentIndex(max(0, index))
        self._language.setToolTip(tr("Язык применяется сразу после сохранения настроек."))
        form.addRow(tr("Язык интерфейса:"), self._language)

        self._delay = QSpinBox()
        self._delay.setRange(0, 15000)
        self._delay.setSingleStep(250)
        self._delay.setSuffix(tr(" мс"))
        self._delay.setValue(settings.general.capture_delay_ms)
        form.addRow(tr("Задержка перед снимком:"), self._delay)

        self._autostart = QCheckBox(tr("Запускать при входе в систему"))
        # Состояние читается с диска: файл автозапуска мог быть изменён
        # вне приложения.
        self._autostart.setChecked(autostart_is_enabled())
        form.addRow(self._autostart)

        self._menu_entry = QCheckBox(tr("Показывать в меню приложений"))
        self._menu_entry.setChecked(menu_entry_installed())
        form.addRow(self._menu_entry)

        # Перенос настроек: файл целиком копируется в выбранное место и
        # обратно, что позволяет держать несколько наборов и переносить
        # их между машинами.
        transfer = QHBoxLayout()
        transfer.setContentsMargins(0, 0, 0, 0)
        export_button = QPushButton(tr("Экспорт настроек…"))
        export_button.clicked.connect(self._export_settings)
        import_button = QPushButton(tr("Импорт настроек…"))
        import_button.clicked.connect(self._import_settings)
        transfer.addWidget(export_button)
        transfer.addWidget(import_button)
        transfer_widget = QWidget()
        transfer_widget.setLayout(transfer)
        form.addRow(tr("Перенос настроек:"), transfer_widget)

        location = QLabel(tr("Файл настроек: {0}").format(self._config.path))
        location.setWordWrap(True)
        location.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        form.addRow(location)

        summary = QLabel(
            tr("Тип сессии: {0}").format(self._session.session_type.label)
            + (f"\n{self._diagnostics}" if self._diagnostics else "")
        )
        summary.setWordWrap(True)
        form.addRow(summary)
        return page

    def _export_settings(self) -> None:
        """Сохранение копии настроек в выбранный файл."""
        # Перед выгрузкой применяются значения из полей окна, иначе в
        # копию попали бы прежние настройки.
        self._collect()
        suggested = str(Path.home() / tr("linscreen-настройки.ini"))
        chosen, _filter = QFileDialog.getSaveFileName(
            self, tr("Экспорт настроек"), suggested, tr("Файлы настроек (*.ini)")
        )
        if not chosen:
            return
        try:
            self._config.export_to(Path(chosen))
        except OSError as error:
            QMessageBox.warning(
                self, tr("Экспорт настроек"), tr("Не удалось записать: {0}").format(error)
            )
            return
        QMessageBox.information(self, tr("Экспорт настроек"), tr("Сохранено: {0}").format(chosen))

    def _import_settings(self) -> None:
        """Чтение настроек из выбранного файла с закрытием окна."""
        chosen, _filter = QFileDialog.getOpenFileName(
            self, tr("Импорт настроек"), str(Path.home()), tr("Файлы настроек (*.ini *.conf)")
        )
        if not chosen:
            return
        try:
            self._config.import_from(Path(chosen))
        except (OSError, ValueError) as error:
            QMessageBox.warning(
                self, tr("Импорт настроек"), tr("Не удалось прочитать: {0}").format(error)
            )
            return

        # Поля окна отражают прежние значения, поэтому окно закрывается:
        # приложение применяет прочитанные настройки целиком.
        QMessageBox.information(self, tr("Импорт настроек"), tr("Настройки прочитаны и применены."))
        self.settingsSaved.emit()
        self.accept()

    def _build_image_tab(self) -> QWidget:
        """Вкладка параметров сохранения снимков."""
        settings = self._config.settings.images
        page = QWidget()
        form = QFormLayout(page)

        self._image_format = QComboBox()
        for key, title in FORMAT_TITLES.items():
            self._image_format.addItem(tr(title), key)
        index = self._image_format.findData(settings.image_format)
        self._image_format.setCurrentIndex(max(0, index))
        form.addRow(tr("Формат снимков:"), self._image_format)

        self._png_compression = QSpinBox()
        self._png_compression.setRange(0, 9)
        self._png_compression.setValue(settings.png_compression)
        self._png_compression.setToolTip(tr("0 — без сжатия, 9 — наименьший размер файла"))
        form.addRow(tr("Сжатие PNG:"), self._png_compression)

        self._jpeg_quality = QSpinBox()
        self._jpeg_quality.setRange(1, 100)
        self._jpeg_quality.setValue(settings.jpeg_quality)
        form.addRow(tr("Качество JPEG:"), self._jpeg_quality)

        self._webp_quality = QSpinBox()
        self._webp_quality.setRange(1, 99)
        self._webp_quality.setValue(settings.webp_quality)
        form.addRow(tr("Качество WEBP:"), self._webp_quality)

        self._webp_lossless = QCheckBox(tr("WEBP без потерь"))
        self._webp_lossless.setChecked(settings.webp_lossless)
        form.addRow(self._webp_lossless)

        self._avif_quality = QSpinBox()
        self._avif_quality.setRange(1, 100)
        self._avif_quality.setValue(settings.avif_quality)
        form.addRow(tr("Качество AVIF:"), self._avif_quality)
        return page

    def _build_video_tab(self) -> QWidget:
        """Вкладка параметров записи экрана."""
        settings = self._config.settings.video
        page = QWidget()
        form = QFormLayout(page)

        self._profile = QComboBox()
        for profile in self._profiles.video_profiles():
            # Профиль, кодера которого нет в сборке, будет заменён на
            # доступный. Замена показывается заранее, а не обнаруживается
            # пользователем по содержимому готового файла.
            resolved = self._profiles.resolve(profile)
            # Названия профилей объявлены на уровне модуля, поэтому
            # переводятся в месте показа.
            title = tr(profile.title)
            if resolved.video_codec is not profile.video_codec:
                title += tr(" → будет записан как {0}").format(resolved.video_codec.encoder)
            self._profile.addItem(title, profile.identifier)
        for animation in self._profiles.animation_profiles():
            self._profile.addItem(
                tr("Анимация: {0}").format(tr(animation.title)), animation.identifier
            )
        index = self._profile.findData(settings.profile_id)
        self._profile.setCurrentIndex(max(0, index))
        form.addRow(tr("Профиль записи:"), self._profile)

        self._fps = QSpinBox()
        self._fps.setRange(1, 144)
        self._fps.setValue(settings.fps)
        form.addRow(tr("Частота кадров:"), self._fps)

        self._show_cursor = QCheckBox(tr("Записывать указатель мыши"))
        self._show_cursor.setChecked(settings.show_cursor)
        form.addRow(self._show_cursor)

        self._audio_mode = QComboBox()
        for mode in AudioMode:
            self._audio_mode.addItem(mode.label, mode.value)
        index = self._audio_mode.findData(settings.audio_mode)
        self._audio_mode.setCurrentIndex(max(0, index))
        form.addRow(tr("Источник звука:"), self._audio_mode)

        self._system_device = QComboBox()
        self._microphone_device = QComboBox()
        for combo in (self._system_device, self._microphone_device):
            combo.addItem(tr("Устройство по умолчанию"), "")
        form.addRow(tr("Системный звук:"), self._system_device)
        form.addRow(tr("Микрофон:"), self._microphone_device)
        return page

    def _build_encoding_tab(self) -> QWidget:
        """
        Вкладка ручной настройки кодирования.

        Значения уточняют выбранный профиль: пункт «как в профиле»
        оставляет параметр таким, каким его задаёт профиль записи.
        """
        settings = self._config.settings.encoding
        page = QWidget()
        form = QFormLayout(page)

        # Название профиля и пояснение: без них непонятно, о каком именно
        # профиле идёт речь в вариантах «как в профиле».
        self._profile_title = QLabel()
        self._profile_title.setWordWrap(True)
        form.addRow(self._profile_title)

        explanation = QLabel(
            tr(
                "Значения ниже уточняют выбранный профиль записи. Вариант "
                "«как в профиле» показывает в скобках, что именно задаёт сам "
                "профиль. Профиль выбирается на вкладке «Запись»."
            )
        )
        explanation.setWordWrap(True)
        form.addRow(explanation)

        # --- Видео ---
        self._video_codec = QComboBox()
        self._video_codec.addItem(tr("Как в профиле"), "")
        for codec in (
            VideoCodec.H264,
            VideoCodec.H265,
            VideoCodec.AV1,
            VideoCodec.VP9,
            VideoCodec.FFV1,
            VideoCodec.PRORES,
            VideoCodec.MPEG4,
        ):
            available = self._profiles.is_encoder_available(codec.encoder)
            title = codec.encoder + ("" if available else tr(" (нет в сборке)"))
            self._video_codec.addItem(title, codec.value)
        index = self._video_codec.findData(settings.video_codec)
        self._video_codec.setCurrentIndex(max(0, index))
        form.addRow(tr("Энкодер видео:"), self._video_codec)

        # Способы управления качеством исключают друг друга, как и в
        # прочих программах записи экрана.
        self._rate_crf = QRadioButton(tr("Постоянное качество (CRF)"))
        self._rate_bitrate = QRadioButton(tr("Заданный битрейт"))
        self._rate_crf.setChecked(settings.rate_mode != "bitrate")
        self._rate_bitrate.setChecked(settings.rate_mode == "bitrate")

        self._crf = QSpinBox()
        self._crf.setRange(0, 63)
        self._crf.setSpecialValueText(tr("как в профиле"))
        self._crf.setValue(settings.crf)
        self._crf.setToolTip(
            tr(
                "Меньше значение — выше качество и больше файл. "
                "Типичные значения: 18–23 для H.264, 24–28 для H.265, 30–36 для AV1."
            )
        )

        self._video_bitrate = QLineEdit(settings.video_bitrate)
        self._video_bitrate.setPlaceholderText(tr("например 8000k"))

        quality_row = QHBoxLayout()
        quality_row.setContentsMargins(0, 0, 0, 0)
        quality_row.addWidget(self._rate_crf)
        quality_row.addWidget(self._crf)
        quality_row.addWidget(self._rate_bitrate)
        quality_row.addWidget(self._video_bitrate)
        quality_widget = QWidget()
        quality_widget.setLayout(quality_row)
        form.addRow(tr("Качество:"), quality_widget)

        self._rate_crf.toggled.connect(self._update_rate_controls)
        self._update_rate_controls()

        self._preset = QComboBox()
        self._preset.setEditable(True)
        self._preset.addItems(
            [
                "",
                "ultrafast",
                "superfast",
                "veryfast",
                "faster",
                "fast",
                "medium",
                "slow",
                "slower",
                "veryslow",
            ]
        )
        self._preset.setCurrentText(settings.preset)
        self._preset.setToolTip(
            tr(
                "Скорость кодирования. Пустое поле — значение профиля. "
                "Для ProRes применяются названия proxy, lt, standard, hq."
            )
        )
        form.addRow(tr("Пресет скорости:"), self._preset)

        self._keyint = QSpinBox()
        self._keyint.setRange(0, 600)
        self._keyint.setSpecialValueText(tr("как в профиле"))
        self._keyint.setValue(settings.keyint)
        self._keyint.setToolTip(tr("Интервал ключевых кадров. Меньше — точнее перемотка."))
        form.addRow(tr("Ключевые кадры:"), self._keyint)

        self._pix_fmt = QComboBox()
        self._pix_fmt.setEditable(True)
        self._pix_fmt.addItems(["", "yuv420p", "yuv422p", "yuv444p", "yuv422p10le", "bgr0"])
        self._pix_fmt.setCurrentText(settings.pix_fmt)
        form.addRow(tr("Формат пикселей:"), self._pix_fmt)

        # --- Звук ---
        self._audio_codec = QComboBox()
        self._audio_codec.addItem(tr("Как в профиле"), "")
        for audio_codec in AudioCodec:
            self._audio_codec.addItem(audio_codec.encoder, audio_codec.value)
        index = self._audio_codec.findData(settings.audio_codec)
        self._audio_codec.setCurrentIndex(max(0, index))
        form.addRow(tr("Энкодер звука:"), self._audio_codec)

        self._audio_bitrate = QLineEdit(settings.audio_bitrate)
        self._audio_bitrate.setPlaceholderText(tr("как в профиле, например 160k"))
        form.addRow(tr("Битрейт звука:"), self._audio_bitrate)

        self._audio_rate = QSpinBox()
        self._audio_rate.setRange(0, 192000)
        self._audio_rate.setSingleStep(8000)
        self._audio_rate.setSpecialValueText(tr("как в профиле"))
        self._audio_rate.setValue(settings.audio_sample_rate)
        form.addRow(tr("Частота дискретизации:"), self._audio_rate)

        self._audio_channels = QSpinBox()
        self._audio_channels.setRange(0, 8)
        self._audio_channels.setSpecialValueText(tr("как в профиле"))
        self._audio_channels.setValue(settings.audio_channels)
        form.addRow(tr("Каналов звука:"), self._audio_channels)

        # --- Дополнительные аргументы и ручная команда ---
        self._extra_args = QLineEdit(settings.extra_args)
        self._extra_args.setPlaceholderText(tr("например -tune zerolatency -x264-params keyint=60"))
        self._extra_args.setToolTip(
            tr(
                "Добавляются в конец команды перед путём к файлу. "
                "Разбираются по правилам оболочки."
            )
        )
        form.addRow(tr("Дополнительные аргументы:"), self._extra_args)

        self._use_custom = QCheckBox(tr("Использовать свою команду"))
        self._use_custom.setChecked(settings.use_custom_command)
        self._use_custom.toggled.connect(self._update_custom_controls)
        self._use_custom.toggled.connect(self._refresh_encoding_info)
        form.addRow(self._use_custom)

        self._custom_command = QPlainTextEdit(settings.custom_command)
        self._custom_command.setMinimumHeight(70)
        self._custom_command.setMaximumHeight(110)
        font = QFont("monospace")
        font.setStyleHint(QFont.StyleHint.TypeWriter)
        self._custom_command.setFont(font)
        form.addRow(self._custom_command)

        hint = QLabel(
            tr(
                "Подстановки: {ffmpeg} — путь к кодировщику с общими флагами, "
                "{video_input} — аргументы захвата экрана, {audio_input} — все "
                "выбранные источники звука, {output} — путь к файлу, "
                "{fps}, {width}, {height} — параметры захвата. "
                "Остальные параметры этой вкладки при ручной команде не применяются."
            )
        )
        hint.setWordWrap(True)
        form.addRow(hint)

        self._preview = QPlainTextEdit()
        self._preview.setReadOnly(True)
        self._preview.setMaximumHeight(96)
        preview_font = QFont("monospace")
        preview_font.setStyleHint(QFont.StyleHint.TypeWriter)
        preview_font.setPointSize(8)
        self._preview.setFont(preview_font)
        self._preview.setToolTip(
            tr("Команда, которая будет выполнена с учётом профиля и уточнений.")
        )
        form.addRow(tr("Итоговая команда:"), self._preview)

        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        fill = QPushButton(tr("Подставить текущую команду"))
        fill.clicked.connect(self._fill_command_template)
        reset = QPushButton(tr("Сбросить параметры кодирования"))
        reset.clicked.connect(self._reset_encoding)
        buttons.addWidget(fill)
        buttons.addWidget(reset)
        container = QWidget()
        container.setLayout(buttons)
        form.addRow(container)

        # Любое изменение полей отражается в сведениях о профиле и в
        # предпросмотре команды, поэтому подписка оформляется на все.
        self._video_codec.currentIndexChanged.connect(self._refresh_encoding_info)
        self._audio_codec.currentIndexChanged.connect(self._refresh_encoding_info)
        self._crf.valueChanged.connect(self._refresh_encoding_info)
        self._keyint.valueChanged.connect(self._refresh_encoding_info)
        self._audio_rate.valueChanged.connect(self._refresh_encoding_info)
        self._audio_channels.valueChanged.connect(self._refresh_encoding_info)
        self._video_bitrate.textChanged.connect(self._refresh_encoding_info)
        self._audio_bitrate.textChanged.connect(self._refresh_encoding_info)
        self._extra_args.textChanged.connect(self._refresh_encoding_info)
        self._preset.currentTextChanged.connect(self._refresh_encoding_info)
        self._pix_fmt.currentTextChanged.connect(self._refresh_encoding_info)
        self._rate_crf.toggled.connect(self._refresh_encoding_info)
        # Смена профиля и источника звука задаётся на вкладке «Запись».
        self._profile.currentIndexChanged.connect(self._refresh_encoding_info)
        self._audio_mode.currentIndexChanged.connect(self._refresh_encoding_info)

        self._update_custom_controls()
        self._refresh_encoding_info()
        return page

    def _selected_profile(self):  # type: ignore[no-untyped-def]
        """
        Выбранный на вкладке «Запись» профиль либо пустое значение.

        Для профилей анимации видеопрофиль не определён: запись ведётся
        промежуточным профилем без потерь с последующей сборкой.
        """
        identifier = str(self._profile.currentData())
        animation_ids = {item.identifier for item in self._profiles.animation_profiles()}
        if identifier in animation_ids:
            return None
        try:
            return self._profiles.video_profile(identifier)
        except KeyError:
            return None

    def _current_overrides(self) -> ProfileOverrides:
        """
        Уточнения, набранные в полях окна.

        Значения берутся из виджетов, а не из сохранённых настроек: это
        позволяет показывать итоговую команду до нажатия кнопки сохранения.
        """

        def as_enum(value, kind):  # type: ignore[no-untyped-def]
            """Преобразование значения поля в элемент перечисления."""
            if not value:
                return None
            try:
                return kind(value)
            except ValueError:
                return None

        return ProfileOverrides(
            video_codec=as_enum(self._video_codec.currentData(), VideoCodec),
            audio_codec=as_enum(self._audio_codec.currentData(), AudioCodec),
            rate_mode="crf" if self._rate_crf.isChecked() else "bitrate",
            crf=self._crf.value() or None,
            video_bitrate=self._video_bitrate.text().strip(),
            preset=self._preset.currentText().strip(),
            keyint=self._keyint.value() or None,
            pix_fmt=self._pix_fmt.currentText().strip(),
            audio_bitrate=self._audio_bitrate.text().strip(),
            audio_sample_rate=self._audio_rate.value() or None,
            audio_channels=self._audio_channels.value() or None,
            extra_args=self._extra_args.text().strip(),
        )

    def _audio_source_count(self) -> tuple[AudioMode, int]:
        """Выбранный режим звука и число задействованных источников."""
        try:
            mode = AudioMode(str(self._audio_mode.currentData()))
        except ValueError:
            mode = AudioMode.NONE
        sources = {AudioMode.NONE: 0, AudioMode.SEPARATE: 2, AudioMode.MIX: 2}.get(mode, 1)
        return mode, sources

    def _refresh_encoding_info(self) -> None:
        """Обновление сведений о профиле и предпросмотра команды."""
        profile = self._selected_profile()
        if profile is None:
            identifier = str(self._profile.currentData())
            self._profile_title.setText(
                tr(
                    "<b>Профиль записи:</b> {0}<br>Анимация собирается в несколько проходов: "
                    "сначала запись без потерь, затем сборка. Параметры ниже к ней не применяются."
                ).format(self._profile.currentText())
            )
            self._preview.setPlainText(
                tr(
                    "Для профиля анимации единой команды записи не существует: используется "
                    "промежуточная запись и сборка формата {0}."
                ).format(identifier)
            )
            return

        container = profile.container.value.upper()
        self._profile_title.setText(
            tr(
                "<b>Профиль записи:</b> {0}<br>Контейнер {1}, видео {2}, звук {3}, качество CRF "
                "{4}, пресет {5}, ключевые кадры {6}"
            ).format(
                tr(profile.title),
                container,
                profile.video_codec.encoder,
                profile.audio_codec.encoder,
                profile.crf if profile.crf is not None else "—",
                profile.preset,
                profile.keyint,
            )
        )

        # Подписи полей дополняются значениями профиля: пользователю видно,
        # что именно подразумевает вариант «как в профиле».
        pix_fmt = profile.pix_fmt or profile.video_codec.default_pix_fmt
        self._video_codec.setItemText(
            0, tr("Как в профиле ({0})").format(profile.video_codec.encoder)
        )
        self._audio_codec.setItemText(
            0, tr("Как в профиле ({0})").format(profile.audio_codec.encoder)
        )
        self._crf.setSpecialValueText(
            tr("как в профиле ({0})").format(
                profile.crf if profile.crf is not None else tr("не задан")
            )
        )
        self._keyint.setSpecialValueText(tr("как в профиле ({0})").format(profile.keyint))
        self._audio_rate.setSpecialValueText(
            tr("как в профиле ({0})").format(profile.audio_sample_rate)
        )
        self._audio_channels.setSpecialValueText(
            tr("как в профиле ({0})").format(profile.audio_channels)
        )
        self._video_bitrate.setPlaceholderText(
            tr("как в профиле ({0})").format(profile.video_bitrate or tr("не задан"))
        )
        self._audio_bitrate.setPlaceholderText(
            tr("как в профиле ({0})").format(profile.audio_bitrate)
        )
        line_edit = self._preset.lineEdit()
        if line_edit is not None:
            line_edit.setPlaceholderText(tr("как в профиле ({0})").format(profile.preset))
        pix_edit = self._pix_fmt.lineEdit()
        if pix_edit is not None:
            pix_edit.setPlaceholderText(tr("как в профиле ({0})").format(pix_fmt))

        if self._use_custom.isChecked():
            self._preview.setPlainText(
                tr(
                    "Используется собственная команда из поля выше; "
                    "параметры этой вкладки не применяются."
                )
            )
            return

        mode, sources = self._audio_source_count()
        adjusted = self._profiles.apply_overrides(profile, self._current_overrides())
        resolved = self._profiles.resolve(adjusted)
        note = ""
        if resolved.video_codec is not adjusted.video_codec:
            note = tr("\n\nЭнкодер {0} отсутствует в сборке и будет заменён на {1}.").format(
                adjusted.video_codec.encoder, resolved.video_codec.encoder
            )
        self._preview.setPlainText(
            self._profiles.build_command_template(resolved, mode, sources) + note
        )

    def _update_rate_controls(self) -> None:
        """Согласование полей качества с выбранным способом."""
        use_crf = self._rate_crf.isChecked()
        self._crf.setEnabled(use_crf)
        self._video_bitrate.setEnabled(not use_crf)

    def _update_custom_controls(self) -> None:
        """Блокировка параметров, не применяемых при ручной команде."""
        custom = self._use_custom.isChecked()
        self._custom_command.setEnabled(custom)
        for widget in (
            self._video_codec,
            self._crf,
            self._video_bitrate,
            self._rate_crf,
            self._rate_bitrate,
            self._preset,
            self._keyint,
            self._pix_fmt,
            self._audio_codec,
            self._audio_bitrate,
            self._audio_rate,
            self._audio_channels,
            self._extra_args,
        ):
            widget.setEnabled(not custom)
        if not custom:
            self._update_rate_controls()

    def _fill_command_template(self) -> None:
        """Подстановка команды, соответствующей текущим настройкам."""
        identifier = str(self._profile.currentData())
        animation_ids = {item.identifier for item in self._profiles.animation_profiles()}
        if identifier in animation_ids:
            # Анимация собирается в несколько проходов, и единой команды
            # записи для неё не существует.
            self._custom_command.setPlainText(
                tr(
                    "# Для анимаций ручная команда не применяется: "
                    "запись ведётся профилем без потерь с последующей сборкой."
                )
            )
            return

        profile = self._profiles.video_profile(identifier)
        try:
            mode = AudioMode(str(self._audio_mode.currentData()))
        except ValueError:
            mode = AudioMode.NONE
        sources = {AudioMode.NONE: 0, AudioMode.SEPARATE: 2, AudioMode.MIX: 2}.get(mode, 1)
        self._custom_command.setPlainText(
            self._profiles.build_command_template(profile, mode, sources)
        )

    def _reset_encoding(self) -> None:
        """Возврат параметров кодирования к значениям профиля."""
        self._video_codec.setCurrentIndex(0)
        self._rate_crf.setChecked(True)
        self._crf.setValue(0)
        self._video_bitrate.clear()
        self._preset.setCurrentText("")
        self._keyint.setValue(0)
        self._pix_fmt.setCurrentText("")
        self._audio_codec.setCurrentIndex(0)
        self._audio_bitrate.clear()
        self._audio_rate.setValue(0)
        self._audio_channels.setValue(0)
        self._extra_args.clear()
        self._use_custom.setChecked(False)
        self._custom_command.clear()

    def _build_hotkey_tab(self) -> QWidget:
        """Вкладка настройки глобальных сочетаний клавиш."""
        from ui.widgets.hotkey_edit import HotkeyEdit

        settings = self._config.settings.hotkeys
        page = QWidget()
        form = QFormLayout(page)

        titles = {
            "screenshot_region": tr("Снимок области:"),
            "screenshot_fullscreen": tr("Снимок всего экрана:"),
            "screenshot_window": tr("Снимок активного окна:"),
            "record_toggle": tr("Запись области (старт и стоп):"),
            "record_toggle_pause": tr("Пауза записи:"),
        }
        self._hotkey_fields: dict[str, HotkeyEdit] = {}
        for name, title in titles.items():
            field = HotkeyEdit(getattr(settings, name))
            field.captureChanged.connect(self.hotkeyCaptureChanged)
            self._hotkey_fields[name] = field
            form.addRow(title, field)

        return page

    # -------------------------------------------------------------- действия

    def _fill_audio_devices(self, devices: object) -> None:
        """Заполнение списков звуковых устройств результатом опроса."""
        if not isinstance(devices, list):
            return
        self._devices = devices
        settings = self._config.settings.video

        for device in devices:
            target = self._system_device if device.is_monitor else self._microphone_device
            target.addItem(device.title, device.name)

        # Ранее выбранные устройства восстанавливаются по системному имени.
        for combo, name in (
            (self._system_device, settings.system_device),
            (self._microphone_device, settings.microphone_device),
        ):
            index = combo.findData(name)
            if index >= 0:
                combo.setCurrentIndex(index)

    def _apply(self) -> None:
        """Перенос введённых значений в конфигурацию и сохранение."""
        self._collect()

        # Автозапуск и ярлык хранятся файлами в системе, а не в
        # конфигурации, поэтому применяются отдельно от прочих настроек.
        from ui.tray import install_application_icon

        icon = install_application_icon()
        autostart_set(self._autostart.isChecked(), icon)
        set_menu_entry(self._menu_entry.isChecked(), icon)

        self._config.save()
        self.settingsSaved.emit()
        self.accept()

    def _collect(self) -> None:
        """Перенос значений полей окна в набор настроек без сохранения."""
        settings = self._config.settings

        settings.paths.images_dir = self._images_dir.value()
        settings.paths.videos_dir = self._videos_dir.value()
        settings.paths.image_template = self._image_template.text().strip()
        settings.paths.video_template = self._video_template.text().strip()

        settings.general.ffmpeg_path = self._ffmpeg_path.text().strip()
        settings.general.copy_to_clipboard = self._copy_clipboard.isChecked()
        settings.general.open_editor_after_capture = self._open_editor.isChecked()
        settings.general.show_notifications = self._notifications.isChecked()
        settings.general.hide_while_recording = self._hide_while_recording.isChecked()
        settings.general.capture_delay_ms = self._delay.value()
        settings.general.language = str(self._language.currentData() or "ru")

        settings.images.image_format = str(self._image_format.currentData())
        settings.images.png_compression = self._png_compression.value()
        settings.images.jpeg_quality = self._jpeg_quality.value()
        settings.images.webp_quality = self._webp_quality.value()
        settings.images.webp_lossless = self._webp_lossless.isChecked()
        settings.images.avif_quality = self._avif_quality.value()

        encoding = settings.encoding
        encoding.video_codec = str(self._video_codec.currentData() or "")
        encoding.audio_codec = str(self._audio_codec.currentData() or "")
        encoding.rate_mode = "crf" if self._rate_crf.isChecked() else "bitrate"
        encoding.crf = self._crf.value()
        encoding.video_bitrate = self._video_bitrate.text().strip()
        encoding.preset = self._preset.currentText().strip()
        encoding.keyint = self._keyint.value()
        encoding.pix_fmt = self._pix_fmt.currentText().strip()
        encoding.audio_bitrate = self._audio_bitrate.text().strip()
        encoding.audio_sample_rate = self._audio_rate.value()
        encoding.audio_channels = self._audio_channels.value()
        encoding.extra_args = self._extra_args.text().strip()
        encoding.use_custom_command = self._use_custom.isChecked()
        encoding.custom_command = self._custom_command.toPlainText().strip()

        settings.video.profile_id = str(self._profile.currentData())
        settings.video.fps = self._fps.value()
        settings.video.show_cursor = self._show_cursor.isChecked()
        settings.video.audio_mode = str(self._audio_mode.currentData())
        settings.video.system_device = str(self._system_device.currentData() or "")
        settings.video.microphone_device = str(self._microphone_device.currentData() or "")

        for name, field in self._hotkey_fields.items():
            setattr(settings.hotkeys, name, field.text().strip())
