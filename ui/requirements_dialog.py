"""
Окно «Компоненты системы».

Показывает, каких частей рабочего стола не хватает для работы приложения,
и предлагает два способа исправления:
    * автоматическая установка: пакеты ставятся пакетным менеджером через
      pkexec (система сама спросит пароль администратора), затем
      выполняются команды без повышения прав - например, включение
      расширения трея;
    * ручная: те же команды показаны выделяемым текстом, их можно
      скопировать и выполнить в терминале.

Команды выполняются последовательно через QProcess, вывод попадает в окно;
интерфейс при этом не блокируется.
"""

from __future__ import annotations

from core.i18n import tr

from PySide6.QtCore import QProcess, QProcessEnvironment, Qt, Signal
from PySide6.QtGui import QFont, QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core import requirements
from core.requirements import Report


class RequirementsDialog(QDialog):
    """Окно с перечнем недостающих компонентов и способами их получить."""

    # Запрошена повторная проверка системы.
    recheckRequested = Signal()
    # Автоматическое исправление завершено (успешно или нет).
    fixFinished = Signal(bool)
    # Изменён признак проверки при запуске.
    checkOnStartChanged = Signal(bool)

    def __init__(
        self, report: Report, check_on_start: bool, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Компоненты системы — LinScreen"))
        self.resize(720, 460)
        self._report = report
        self._queue: list[list[str]] = []
        self._process: QProcess | None = None
        self._failed = False

        self._summary = QLabel()
        self._summary.setWordWrap(True)
        self._summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        commands_title = QLabel(tr("Команды для терминала (текст можно выделить и скопировать):"))
        self._commands = QPlainTextEdit()
        self._commands.setReadOnly(True)
        font = QFont("monospace")
        font.setStyleHint(QFont.StyleHint.Monospace)
        self._commands.setFont(font)
        self._commands.setMaximumHeight(110)

        self._output = QPlainTextEdit()
        self._output.setReadOnly(True)
        self._output.setFont(font)
        self._output.setPlaceholderText(tr("Здесь появится ход автоматической установки."))

        self._check_on_start = QCheckBox(tr("Проверять компоненты при запуске"))
        self._check_on_start.setChecked(check_on_start)
        self._check_on_start.toggled.connect(self.checkOnStartChanged)

        self._copy_button = QPushButton(tr("Скопировать команды"))
        self._install_button = QPushButton(tr("Установить автоматически"))
        self._recheck_button = QPushButton(tr("Проверить снова"))
        close_button = QPushButton(tr("Закрыть"))
        self._copy_button.clicked.connect(self._copy)
        self._install_button.clicked.connect(self._install)
        self._recheck_button.clicked.connect(self.recheckRequested)
        close_button.clicked.connect(self.close)

        buttons = QHBoxLayout()
        buttons.addWidget(self._copy_button)
        buttons.addWidget(self._install_button)
        buttons.addWidget(self._recheck_button)
        buttons.addStretch(1)
        buttons.addWidget(close_button)

        layout = QVBoxLayout(self)
        layout.addWidget(self._summary)
        layout.addWidget(commands_title)
        layout.addWidget(self._commands)
        layout.addWidget(self._output, 1)
        layout.addWidget(self._check_on_start)
        layout.addLayout(buttons)

        self.set_report(report)

    # ---------------------------------------------------------------- отчёт

    def set_report(self, report: Report) -> None:
        """Показ результата проверки."""
        self._report = report
        if not report.problems:
            self._summary.setText(tr("Все нужные компоненты системы на месте."))
            self._commands.setPlainText("")
        else:
            lines = [tr("Для полной работы LinScreen в системе не хватает компонентов:")]
            for problem in report.problems:
                lines.append("• " + problem.title)
                if problem.note:
                    lines.append("  " + problem.note)
            if report.packages and not report.install_command:
                lines.append(
                    tr(
                        "Пакетный менеджер не распознан. Установите пакеты средствами "
                        "дистрибутива: {0}"
                    ).format(", ".join(report.packages))
                )
            self._summary.setText("\n".join(lines))
            self._commands.setPlainText(report.script())
        self._update_buttons()

    def _update_buttons(self) -> None:
        """Доступность кнопок по отчёту и ходу установки."""
        busy = self._process is not None
        has_fix = bool(self._report.install_command or self._report.user_commands)
        needs_root = bool(self._report.install_command)
        can_elevate = requirements.graphical_sudo() is not None
        self._install_button.setEnabled(not busy and has_fix and (can_elevate or not needs_root))
        if needs_root and not can_elevate:
            self._install_button.setToolTip(
                tr("Не найдена утилита pkexec: выполните команды в терминале вручную.")
            )
        self._copy_button.setEnabled(bool(self._commands.toPlainText()))
        self._recheck_button.setEnabled(not busy)

    def _copy(self) -> None:
        """Копирование команд в буфер обмена."""
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self._commands.toPlainText())
        self._append(tr("Команды скопированы в буфер обмена."))

    # -------------------------------------------------------- установка

    def _install(self) -> None:
        """Запуск автоматического исправления."""
        queue: list[list[str]] = []
        if self._report.install_command:
            sudo = requirements.graphical_sudo()
            if sudo is None:
                return
            # pkexec сам покажет системный запрос пароля администратора.
            queue.append([sudo, *self._report.install_command])
        queue.extend(self._report.user_commands)
        self._queue = queue
        self._failed = False
        self._output.clear()
        self._run_next()

    def _run_next(self) -> None:
        """Выполнение очередной команды из очереди."""
        if not self._queue:
            self._process = None
            self._update_buttons()
            self._append(
                tr("Готово.") if not self._failed else tr("Исправление завершилось с ошибками.")
            )
            self.fixFinished.emit(not self._failed)
            return
        command = self._queue.pop(0)
        process = QProcess(self)
        environment = QProcessEnvironment()
        for name, value in requirements.child_environment_for_user_commands().items():
            environment.insert(name, value)
        process.setProcessEnvironment(environment)
        process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        process.readyReadStandardOutput.connect(lambda: self._read(process))
        process.finished.connect(lambda code, _status: self._on_finished(process, code))
        process.errorOccurred.connect(lambda _error: self._on_error(process))
        self._process = process
        self._update_buttons()
        self._append("$ " + " ".join(command))
        process.setProgram(command[0])
        process.setArguments(command[1:])
        process.start()

    def _read(self, process: QProcess) -> None:
        """Вывод команды в окно."""
        text = bytes(process.readAllStandardOutput().data()).decode("utf-8", "replace")
        for line in text.splitlines():
            if line.strip():
                self._append(line)

    def _on_finished(self, process: QProcess, code: int) -> None:
        """Завершение команды."""
        if self._process is None or process is not self._process:
            return
        if code != 0:
            self._failed = True
            # Код 126 означает отказ в запросе пароля: продолжать бессмысленно.
            if code in (126, 127):
                self._append(tr("Установка отменена или не разрешена."))
                self._queue = []
            else:
                self._append(tr("Команда завершилась с кодом {0}.").format(code))
        process.deleteLater()
        self._run_next()

    def _on_error(self, process: QProcess) -> None:
        """Команду не удалось запустить."""
        if self._process is None or process is not self._process:
            return
        if process.state() != QProcess.ProcessState.NotRunning:
            return
        self._failed = True
        self._append(tr("Не удалось запустить команду."))
        process.deleteLater()
        self._run_next()

    def _append(self, line: str) -> None:
        """Добавление строки в журнал установки."""
        self._output.appendPlainText(line)
