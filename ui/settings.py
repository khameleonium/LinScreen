"""
Окно настроек приложения.

Диалог редактирует копию значений и записывает их в менеджер конфигурации
только при подтверждении. Перечисление звуковых устройств выполняется в
фоновом потоке: обращение к звуковому серверу занимает заметное время и
подвесило бы открытие окна.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal
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
    QPushButton,
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
from core.session import DesktopSession
from core.workers import run_async
from encoder.images import FORMAT_TITLES
from encoder.profiles import AudioMode, VideoProfileManager


class PathChooser(QWidget):
    """Поле пути с кнопкой выбора каталога."""

    def __init__(self, value: str, placeholder: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._edit = QLineEdit(value)
        self._edit.setPlaceholderText(placeholder)
        button = QPushButton("Обзор…")
        button.clicked.connect(self._choose)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._edit)
        layout.addWidget(button)

    def _choose(self) -> None:
        """Выбор каталога системным диалогом."""
        current = self._edit.text() or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "Выбор каталога", current)
        if chosen:
            self._edit.setText(chosen)

    def value(self) -> str:
        """Текущее значение поля."""
        return self._edit.text().strip()


class SettingsDialog(QDialog):
    """Диалог настройки путей, форматов, кодеков и горячих клавиш."""

    # Настройки сохранены: приложению требуется перечитать значения.
    settingsSaved = Signal()

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

        self.setWindowTitle("Настройки LinScreen")
        self.setMinimumWidth(560)

        tabs = QTabWidget(self)
        tabs.addTab(self._build_general_tab(), "Общие")
        tabs.addTab(self._build_image_tab(), "Снимки")
        tabs.addTab(self._build_video_tab(), "Запись")
        tabs.addTab(self._build_hotkey_tab(), "Клавиши")

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Сохранить")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
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
        form.addRow("Каталог снимков:", self._images_dir)
        form.addRow("Каталог записей:", self._videos_dir)

        self._image_template = QLineEdit(settings.paths.image_template)
        self._video_template = QLineEdit(settings.paths.video_template)
        # Шаблон обрабатывается функцией strftime, поэтому в подсказке
        # приводятся её управляющие последовательности.
        hint = "Допустимы подстановки strftime: %Y, %m, %d, %H, %M, %S"
        self._image_template.setToolTip(hint)
        self._video_template.setToolTip(hint)
        form.addRow("Шаблон имени снимка:", self._image_template)
        form.addRow("Шаблон имени записи:", self._video_template)

        self._ffmpeg_path = QLineEdit(settings.general.ffmpeg_path)
        self._ffmpeg_path.setPlaceholderText("Определяется автоматически")
        form.addRow("Путь к FFmpeg:", self._ffmpeg_path)

        self._copy_clipboard = QCheckBox("Копировать снимок в буфер обмена")
        self._copy_clipboard.setChecked(settings.general.copy_to_clipboard)
        form.addRow(self._copy_clipboard)

        self._open_editor = QCheckBox("Открывать редактор после снимка")
        self._open_editor.setChecked(settings.general.open_editor_after_capture)
        form.addRow(self._open_editor)

        self._notifications = QCheckBox("Показывать уведомления")
        self._notifications.setChecked(settings.general.show_notifications)
        form.addRow(self._notifications)

        self._delay = QSpinBox()
        self._delay.setRange(0, 15000)
        self._delay.setSingleStep(250)
        self._delay.setSuffix(" мс")
        self._delay.setValue(settings.general.capture_delay_ms)
        form.addRow("Задержка перед снимком:", self._delay)

        self._autostart = QCheckBox("Запускать при входе в систему")
        # Состояние читается с диска: файл автозапуска мог быть изменён
        # вне приложения.
        self._autostart.setChecked(autostart_is_enabled())
        form.addRow(self._autostart)

        self._menu_entry = QCheckBox("Показывать в меню приложений")
        self._menu_entry.setChecked(menu_entry_installed())
        form.addRow(self._menu_entry)

        summary = QLabel(
            f"Тип сессии: {self._session.session_type.label}"
            + (f"\n{self._diagnostics}" if self._diagnostics else "")
        )
        summary.setWordWrap(True)
        form.addRow(summary)
        return page

    def _build_image_tab(self) -> QWidget:
        """Вкладка параметров сохранения снимков."""
        settings = self._config.settings.images
        page = QWidget()
        form = QFormLayout(page)

        self._image_format = QComboBox()
        for key, title in FORMAT_TITLES.items():
            self._image_format.addItem(title, key)
        index = self._image_format.findData(settings.image_format)
        self._image_format.setCurrentIndex(max(0, index))
        form.addRow("Формат снимков:", self._image_format)

        self._png_compression = QSpinBox()
        self._png_compression.setRange(0, 9)
        self._png_compression.setValue(settings.png_compression)
        self._png_compression.setToolTip("0 — без сжатия, 9 — наименьший размер файла")
        form.addRow("Сжатие PNG:", self._png_compression)

        self._jpeg_quality = QSpinBox()
        self._jpeg_quality.setRange(1, 100)
        self._jpeg_quality.setValue(settings.jpeg_quality)
        form.addRow("Качество JPEG:", self._jpeg_quality)

        self._webp_quality = QSpinBox()
        self._webp_quality.setRange(1, 99)
        self._webp_quality.setValue(settings.webp_quality)
        form.addRow("Качество WEBP:", self._webp_quality)

        self._webp_lossless = QCheckBox("WEBP без потерь")
        self._webp_lossless.setChecked(settings.webp_lossless)
        form.addRow(self._webp_lossless)

        self._avif_quality = QSpinBox()
        self._avif_quality.setRange(1, 100)
        self._avif_quality.setValue(settings.avif_quality)
        form.addRow("Качество AVIF:", self._avif_quality)
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
            title = profile.title
            if resolved.video_codec is not profile.video_codec:
                title += f" → будет записан как {resolved.video_codec.encoder}"
            self._profile.addItem(title, profile.identifier)
        for animation in self._profiles.animation_profiles():
            self._profile.addItem(f"Анимация: {animation.title}", animation.identifier)
        index = self._profile.findData(settings.profile_id)
        self._profile.setCurrentIndex(max(0, index))
        form.addRow("Профиль записи:", self._profile)

        self._fps = QSpinBox()
        self._fps.setRange(1, 144)
        self._fps.setValue(settings.fps)
        form.addRow("Частота кадров:", self._fps)

        self._show_cursor = QCheckBox("Записывать указатель мыши")
        self._show_cursor.setChecked(settings.show_cursor)
        form.addRow(self._show_cursor)

        self._audio_mode = QComboBox()
        for mode in AudioMode:
            self._audio_mode.addItem(mode.label, mode.value)
        index = self._audio_mode.findData(settings.audio_mode)
        self._audio_mode.setCurrentIndex(max(0, index))
        form.addRow("Источник звука:", self._audio_mode)

        self._system_device = QComboBox()
        self._microphone_device = QComboBox()
        for combo in (self._system_device, self._microphone_device):
            combo.addItem("Устройство по умолчанию", "")
        form.addRow("Системный звук:", self._system_device)
        form.addRow("Микрофон:", self._microphone_device)
        return page

    def _build_hotkey_tab(self) -> QWidget:
        """Вкладка настройки глобальных сочетаний клавиш."""
        from ui.widgets.hotkey_edit import HotkeyEdit

        settings = self._config.settings.hotkeys
        page = QWidget()
        form = QFormLayout(page)

        titles = {
            "screenshot_region": "Снимок области:",
            "screenshot_fullscreen": "Снимок всего экрана:",
            "screenshot_window": "Снимок активного окна:",
            "record_region": "Начать запись области:",
            "record_toggle_pause": "Пауза записи:",
            "record_stop": "Остановить запись:",
        }
        self._hotkey_fields: dict[str, HotkeyEdit] = {}
        for name, title in titles.items():
            field = HotkeyEdit(getattr(settings, name))
            self._hotkey_fields[name] = field
            form.addRow(title, field)

        if not self._session.supports_native_hotkeys:
            # В сессии Wayland перехват возможен только через портал,
            # о чём пользователя следует предупредить сразу.
            warning = QLabel(
                "Сессия Wayland: глобальные клавиши требуют портала "
                "GlobalShortcuts. Действия доступны через меню трея."
            )
            warning.setWordWrap(True)
            form.addRow(warning)
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
        settings = self._config.settings

        settings.paths.images_dir = self._images_dir.value()
        settings.paths.videos_dir = self._videos_dir.value()
        settings.paths.image_template = self._image_template.text().strip()
        settings.paths.video_template = self._video_template.text().strip()

        settings.general.ffmpeg_path = self._ffmpeg_path.text().strip()
        settings.general.copy_to_clipboard = self._copy_clipboard.isChecked()
        settings.general.open_editor_after_capture = self._open_editor.isChecked()
        settings.general.show_notifications = self._notifications.isChecked()
        settings.general.capture_delay_ms = self._delay.value()

        settings.images.image_format = str(self._image_format.currentData())
        settings.images.png_compression = self._png_compression.value()
        settings.images.jpeg_quality = self._jpeg_quality.value()
        settings.images.webp_quality = self._webp_quality.value()
        settings.images.webp_lossless = self._webp_lossless.isChecked()
        settings.images.avif_quality = self._avif_quality.value()

        settings.video.profile_id = str(self._profile.currentData())
        settings.video.fps = self._fps.value()
        settings.video.show_cursor = self._show_cursor.isChecked()
        settings.video.audio_mode = str(self._audio_mode.currentData())
        settings.video.system_device = str(self._system_device.currentData() or "")
        settings.video.microphone_device = str(self._microphone_device.currentData() or "")

        for name, field in self._hotkey_fields.items():
            setattr(settings.hotkeys, name, field.text().strip())

        # Автозапуск и ярлык хранятся файлами в системе, а не в
        # конфигурации, поэтому применяются отдельно от прочих настроек.
        from ui.tray import install_application_icon

        icon = install_application_icon()
        autostart_set(self._autostart.isChecked(), icon)
        set_menu_entry(self._menu_entry.isChecked(), icon)

        self._config.save()
        self.settingsSaved.emit()
        self.accept()
