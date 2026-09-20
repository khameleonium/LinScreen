"""
Иконка в системном трее и главное меню приложения.

Иконка отражает текущее состояние: ожидание, запись, пауза или обработка.
Значки рисуются программно, что избавляет от внешних файлов ресурсов и
позволяет менять цвет индикатора без перезагрузки темы оформления.
"""

from __future__ import annotations

import os

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QAction, QActionGroup, QBrush, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from capture.screen import CaptureMode, list_monitors
from encoder.ffmpeg import format_timecode
from encoder.process import RecorderState
from encoder.profiles import AudioMode

# Размер отрисовываемого значка в точках.
ICON_SIZE = 64

# Цвет индикатора для каждого состояния контроллера записи.
STATE_COLORS: dict[RecorderState, str] = {
    RecorderState.IDLE: "#c8ccd2",
    RecorderState.STARTING: "#4a90e2",
    RecorderState.RECORDING: "#e04a4a",
    RecorderState.PAUSED: "#e0a23c",
    RecorderState.STOPPING: "#4a90e2",
    RecorderState.PROCESSING: "#4a90e2",
    RecorderState.FINISHED: "#5cb85c",
    RecorderState.FAILED: "#e04a4a",
}


def render_tray_icon(state: RecorderState) -> QIcon:
    """Отрисовка значка трея для указанного состояния."""
    pixmap = QPixmap(ICON_SIZE, ICON_SIZE)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    # Корпус камеры рисуется нейтральным цветом, объектив - цветом состояния.
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(QColor("#4c5158")))
    painter.drawRoundedRect(QRectF(4, 14, 56, 40), 8, 8)
    painter.drawRoundedRect(QRectF(20, 8, 24, 10), 4, 4)

    painter.setBrush(QBrush(QColor(STATE_COLORS.get(state, "#c8ccd2"))))
    painter.drawEllipse(QPointF(32, 34), 13, 13)

    if state is RecorderState.PAUSED:
        # На паузе объектив перечёркивается двумя полосами.
        painter.setBrush(QBrush(QColor("#2b2e33")))
        painter.drawRect(QRectF(27, 27, 4, 14))
        painter.drawRect(QRectF(34, 27, 4, 14))
    painter.end()
    return QIcon(pixmap)


class TrayIcon(QObject):
    """Значок трея с меню быстрых действий."""

    # Запрос снимка: передаётся режим выбора области.
    screenshotRequested = Signal(object)
    # Запрос снимка конкретного монитора по его порядковому номеру.
    monitorScreenshotRequested = Signal(int)
    # Запрос записи: передаётся режим выбора области.
    recordRequested = Signal(object)
    # Запрос записи конкретного монитора по его порядковому номеру.
    monitorRecordRequested = Signal(int)
    # Смена источника звука перед началом записи: передаётся значение
    # перечисления AudioMode.
    audioModeChanged = Signal(str)
    # Управление активной записью.
    pauseRequested = Signal()
    stopRequested = Signal()
    # Служебные действия.
    settingsRequested = Signal()
    logRequested = Signal()
    openFolderRequested = Signal()
    quitRequested = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._state = RecorderState.IDLE
        self._elapsed_ms = 0
        self._recording_allowed = True

        self._tray = QSystemTrayIcon(render_tray_icon(RecorderState.IDLE), self)
        self._menu = QMenu()
        self._build_menu()
        self._tray.setContextMenu(self._menu)
        self._tray.activated.connect(self._on_activated)
        self._update_tooltip()

    # ---------------------------------------------------------------- меню

    def _build_menu(self) -> None:
        """Сборка постоянной части меню."""
        self._menu.addSection("Снимок экрана")
        self._add_action(
            "Выделенная область",
            lambda: self.screenshotRequested.emit(CaptureMode.REGION),
        )
        self._add_action(
            "Весь экран",
            lambda: self.screenshotRequested.emit(CaptureMode.FULLSCREEN),
        )
        self._add_action("Активное окно", lambda: self.screenshotRequested.emit(CaptureMode.WINDOW))

        # Подменю мониторов пересобирается перед каждым показом: состав
        # подключённых экранов может измениться во время работы.
        self._monitor_menu = self._menu.addMenu("Отдельный монитор")
        self._menu.aboutToShow.connect(self._rebuild_monitor_menus)

        self._menu.addSection("Запись экрана")
        self._record_region_action = self._add_action(
            "Записать область", lambda: self.recordRequested.emit(CaptureMode.REGION)
        )
        self._record_full_action = self._add_action(
            "Записать весь экран", lambda: self.recordRequested.emit(CaptureMode.FULLSCREEN)
        )
        self._record_monitor_menu = self._menu.addMenu("Записать монитор")
        self._build_audio_menu()
        self._pause_action = self._add_action("Пауза", self.pauseRequested.emit)
        self._stop_action = self._add_action("Остановить запись", self.stopRequested.emit)

        self._menu.addSeparator()
        self._add_action("Открыть папку с файлами", self.openFolderRequested.emit)
        self._add_action("Журнал…", self.logRequested.emit)
        self._add_action("Настройки…", self.settingsRequested.emit)
        self._menu.addSeparator()
        self._add_action("Выход", self.quitRequested.emit)

        self._apply_state_to_menu()

    def _build_audio_menu(self) -> None:
        """
        Подменю быстрого выбора источника звука.

        Схема звука выбирается непосредственно перед запуском записи, без
        открытия окна настроек: это самая часто меняемая настройка.
        """
        self._audio_menu = self._menu.addMenu("Источник звука")
        group = QActionGroup(self._menu)
        group.setExclusive(True)
        self._audio_actions: dict[AudioMode, QAction] = {}

        for mode in AudioMode:
            action = QAction(mode.label, self._audio_menu)
            action.setCheckable(True)
            action.triggered.connect(
                lambda _checked=False, m=mode: self.audioModeChanged.emit(m.value)
            )
            group.addAction(action)
            self._audio_menu.addAction(action)
            self._audio_actions[mode] = action

    def set_audio_mode(self, mode: AudioMode) -> None:
        """Отметка текущего источника звука в подменю."""
        action = self._audio_actions.get(mode)
        if action is not None:
            action.setChecked(True)

    def _add_action(self, title: str, handler) -> QAction:  # type: ignore[no-untyped-def]
        """Добавление пункта меню с обработчиком."""
        action = QAction(title, self._menu)
        action.triggered.connect(lambda _checked=False: handler())
        self._menu.addAction(action)
        return action

    def _rebuild_monitor_menus(self) -> None:
        """
        Обновление списков мониторов перед показом меню.

        Состав подключённых экранов меняется без перезапуска приложения,
        поэтому оба подменю собираются заново при каждом открытии меню.
        """
        self._monitor_menu.clear()
        self._record_monitor_menu.clear()
        monitors = list_monitors()

        for index, monitor in enumerate(monitors):
            shot = QAction(monitor.title, self._monitor_menu)
            shot.triggered.connect(
                lambda _checked=False, i=index: self.monitorScreenshotRequested.emit(i)
            )
            self._monitor_menu.addAction(shot)

            record = QAction(monitor.title, self._record_monitor_menu)
            record.triggered.connect(
                lambda _checked=False, i=index: self.monitorRecordRequested.emit(i)
            )
            self._record_monitor_menu.addAction(record)

        # Пункты записи мониторов подчиняются общему запрету записи.
        self._record_monitor_menu.setEnabled(
            self._recording_allowed and not self._state.is_busy
        )

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        """Обработка щелчка по значку."""
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            # Одиночный щелчок повторяет наиболее частое действие -
            # снимок выделенной области.
            self.screenshotRequested.emit(CaptureMode.REGION)

    # -------------------------------------------------------------- состояние

    def show(self) -> None:
        """Показ значка в трее."""
        self._tray.show()

    def hide(self) -> None:
        """Скрытие значка перед завершением работы."""
        self._tray.hide()

    def set_recording_allowed(self, allowed: bool) -> None:
        """
        Разрешение или запрет пунктов записи.

        Запись блокируется при отсутствии FFmpeg либо при невозможности
        захвата в текущей сессии.
        """
        self._recording_allowed = allowed
        self._apply_state_to_menu()

    def set_state(self, state: RecorderState) -> None:
        """Обновление значка и меню под новое состояние."""
        self._state = state
        self._tray.setIcon(render_tray_icon(state))
        self._apply_state_to_menu()
        self._update_tooltip()

    def set_elapsed(self, milliseconds: int) -> None:
        """Обновление длительности записи в подсказке значка."""
        self._elapsed_ms = milliseconds
        self._update_tooltip()

    def _apply_state_to_menu(self) -> None:
        """Согласование доступности пунктов меню с состоянием."""
        busy = self._state.is_busy
        self._record_region_action.setEnabled(self._recording_allowed and not busy)
        self._record_full_action.setEnabled(self._recording_allowed and not busy)
        self._pause_action.setEnabled(
            self._state in (RecorderState.RECORDING, RecorderState.PAUSED)
        )
        self._pause_action.setText(
            "Продолжить запись" if self._state is RecorderState.PAUSED else "Пауза"
        )
        self._stop_action.setEnabled(
            self._state in (RecorderState.RECORDING, RecorderState.PAUSED)
        )
        self._record_monitor_menu.setEnabled(self._recording_allowed and not busy)
        # Источник звука меняется только между записями: смена схемы во
        # время записи потребовала бы перезапуска захвата.
        self._audio_menu.setEnabled(not busy)

    def _update_tooltip(self) -> None:
        """Подсказка со сведениями о состоянии и длительности."""
        text = f"LinScreen — {self._state.label}"
        if self._state in (RecorderState.RECORDING, RecorderState.PAUSED):
            text += f"\nДлительность: {format_timecode(self._elapsed_ms / 1000)}"
        self._tray.setToolTip(text)

    def notify(self, title: str, message: str, is_error: bool = False) -> None:
        """Показ уведомления рабочего стола через значок трея."""
        icon = (
            QSystemTrayIcon.MessageIcon.Critical
            if is_error
            else QSystemTrayIcon.MessageIcon.Information
        )
        self._tray.showMessage(title, message, icon, 5000)

    @staticmethod
    def is_available() -> bool:
        """Проверка поддержки системного трея текущим окружением."""
        return QSystemTrayIcon.isSystemTrayAvailable()


def install_application_icon() -> str:
    """
    Сохранение значка приложения в каталог значков пользователя.

    Ярлык в меню рабочего стола ссылается на значок по имени, поэтому
    изображение выкладывается в стандартный каталог тем оформления.
    Возвращается имя значка либо имя из системной темы, если сохранить
    файл не удалось.
    """
    from pathlib import Path

    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
    target = Path(base) / "icons" / "hicolor" / "256x256" / "apps" / "linscreen.png"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # Значок рисуется тем же кодом, что и в трее, но крупнее.
        pixmap = render_tray_icon(RecorderState.IDLE).pixmap(256, 256)
        if pixmap.save(str(target), "PNG"):
            return "linscreen"
    except OSError:
        pass
    # Запасной вариант: значок из системной темы оформления.
    return "camera-photo"
