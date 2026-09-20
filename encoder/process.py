"""
Контроллеры внешних процессов FFmpeg.

Зона ответственности модуля:
    * CapabilityProbe - опрос возможностей сборки FFmpeg в фоновом потоке;
    * FFmpegTaskRunner - последовательное выполнение многошаговых заданий
      (двухпроходный GIF, конвертация, склейка фрагментов);
    * ScreenRecorder - управление живой записью экрана с паузой, корректной
      остановкой и аварийным сохранением материала.

Ключевые решения модуля:
    * Ни один вызов не блокирует поток интерфейса: обмен с процессами ведётся
      через сигналы QProcess, опрос возможностей вынесен в QThreadPool.
    * Остановка выполняется отправкой символа "q" в stdin: FFmpeg досбрасывает
      буферы и закрывает контейнер. Принудительное завершение применяется
      только после истечения времени ожидания, эскалацией SIGTERM -> SIGKILL.
    * Пауза реализована через разбиение записи на фрагменты с последующей
      склейкой демультиплексором concat без перекодирования. Вариант с
      SIGSTOP отвергнут: остановленный процесс продолжает получать метки
      времени от источника, и пауза превращается в застывший кадр.
    * Аргументы передаются списками, оболочка в цепочке запуска не участвует,
      поэтому пробелы и спецсимволы в путях не требуют экранирования.
"""

from __future__ import annotations

import os
import shutil
from collections import deque
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Sequence

from PySide6.QtCore import (
    QElapsedTimer,
    QObject,
    QProcess,
    QProcessEnvironment,
    QRunnable,
    QThreadPool,
    QTimer,
    Signal,
    SignalInstance,
)

from core.runtime import child_environment
from encoder.ffmpeg import (
    FFmpegCapabilities,
    ProgressParser,
    ProgressReport,
    probe_capabilities,
    summarize_log,
)
from encoder.profiles import FFmpegStep

# Время ожидания штатного завершения после отправки символа "q".
# Длинные записи с большим буфером закрываются не мгновенно.
STOP_GRACE_MS = 10_000
# Время ожидания после SIGTERM, по истечении которого применяется SIGKILL.
TERMINATE_GRACE_MS = 5_000
# Период обновления счётчика длительности записи в интерфейсе.
ELAPSED_TICK_MS = 200
# Глубина хранимого хвоста журнала stderr для диагностики сбоев.
LOG_TAIL_SIZE = 200

# Пороги предупреждения о пропуске кадров. Один-два кадра теряются при
# инициализации захвата всегда, и сообщать об этом пользователю не следует.
DROP_WARNING_ABSOLUTE = 30
DROP_WARNING_RATIO = 0.05

# Коды возврата, считающиеся успешными при запрошенной остановке.
# При выходе по команде "q" часть сборок FFmpeg возвращает 255, что
# ошибкой не является.
GRACEFUL_EXIT_CODES: frozenset[int] = frozenset({0, 255})


# ===========================================================================
# Состояния
# ===========================================================================


class RecorderState(str, Enum):
    """Состояние контроллера записи, отображаемое иконкой в трее."""

    IDLE = "idle"
    STARTING = "starting"
    RECORDING = "recording"
    PAUSED = "paused"
    STOPPING = "stopping"
    PROCESSING = "processing"
    FINISHED = "finished"
    FAILED = "failed"

    @property
    def label(self) -> str:
        """Человекочитаемое описание состояния для подсказки трея."""
        return {
            RecorderState.IDLE: "Ожидание",
            RecorderState.STARTING: "Запуск захвата",
            RecorderState.RECORDING: "Идёт запись",
            RecorderState.PAUSED: "Пауза",
            RecorderState.STOPPING: "Завершение записи",
            RecorderState.PROCESSING: "Обработка",
            RecorderState.FINISHED: "Готово",
            RecorderState.FAILED: "Ошибка",
        }[self]

    @property
    def is_busy(self) -> bool:
        """Признак активной работы, запрещающей запуск второй записи."""
        return self in (
            RecorderState.STARTING,
            RecorderState.RECORDING,
            RecorderState.PAUSED,
            RecorderState.STOPPING,
            RecorderState.PROCESSING,
        )


# ===========================================================================
# Служебные функции запуска процессов
# ===========================================================================


def build_process_environment() -> QProcessEnvironment:
    """
    Окружение дочернего процесса FFmpeg.

    Переменные сессии (DISPLAY, WAYLAND_DISPLAY, XDG_RUNTIME_DIR,
    PULSE_SERVER) наследуются без изменений: без них захват экрана и звука
    работать не будет. Локаль приводится к C, чтобы сообщения журнала и
    поток прогресса оставались машиночитаемыми.
    """
    # Окружение строится из системного с восстановлением путей поиска
    # библиотек: внутри собранного приложения они подменены на вложенные.
    environment = QProcessEnvironment()
    for name, value in child_environment().items():
        environment.insert(name, value)
    environment.insert("LC_ALL", "C")
    environment.insert("LANG", "C")
    return environment


def create_process(owner: QObject) -> QProcess:
    """Создание преднастроенного объекта процесса FFmpeg."""
    process = QProcess(owner)
    # Каналы разделяются: stdout несёт машиночитаемый прогресс,
    # stderr - журнал работы кодировщика.
    process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
    process.setProcessEnvironment(build_process_environment())
    return process


def start_process(process: QProcess, args: Sequence[str]) -> None:
    """
    Запуск процесса по списку аргументов.

    Программа и аргументы задаются раздельно: сборка строки команды с
    последующим разбором оболочкой не применяется, поэтому пути с пробелами,
    кавычками и национальными символами передаются без искажений.
    """
    process.setProgram(args[0])
    process.setArguments(list(args[1:]))
    # Режим чтения и записи обязателен: канал stdin используется для
    # передачи команды остановки.
    process.start(QProcess.OpenModeFlag.ReadWrite)


def request_graceful_stop(
    process: QProcess,
    grace_ms: int = STOP_GRACE_MS,
) -> None:
    """
    Запрос штатного завершения процесса FFmpeg.

    Последовательность эскалации: команда "q" в stdin, затем SIGTERM,
    затем SIGKILL. Каждая следующая ступень применяется только если
    процесс не завершился за отведённое время.
    """
    if process.state() == QProcess.ProcessState.NotRunning:
        return

    # Штатный способ остановки: FFmpeg прерывает цикл кодирования, дописывает
    # хвост буферов и корректно финализирует заголовки контейнера.
    process.write(b"q")

    def escalate_to_terminate() -> None:
        """Мягкое принудительное завершение по истечении ожидания."""
        if process.state() == QProcess.ProcessState.NotRunning:
            return
        # SIGTERM перехватывается обработчиком FFmpeg и тоже приводит к
        # закрытию файла, но без гарантии полного сброса буферов.
        process.terminate()
        _schedule(process, TERMINATE_GRACE_MS, escalate_to_kill)

    def escalate_to_kill() -> None:
        """Последняя ступень: снятие зависшего процесса."""
        if process.state() != QProcess.ProcessState.NotRunning:
            # SIGKILL оставляет файл незакрытым; читаемым он останется
            # только в контейнере Matroska.
            process.kill()

    _schedule(process, grace_ms, escalate_to_terminate)


def _schedule(process: QProcess, delay_ms: int, action: Callable[[], None]) -> None:
    """
    Отложенный вызов, привязанный ко времени жизни процесса.

    Таймер создаётся потомком объекта процесса и уничтожается вместе с ним.
    Это исключает обращение к уже удалённому объекту: штатно завершившийся
    процесс освобождается раньше, чем истекает срок ожидания эскалации.
    """
    timer = QTimer(process)
    timer.setSingleShot(True)
    timer.timeout.connect(action)
    timer.start(delay_ms)


def _concat_quote(path: Path) -> str:
    """
    Подготовка пути для файла списка демультиплексора concat.

    Формат списка требует заключения пути в одинарные кавычки; внутренняя
    одинарная кавычка записывается последовательностью '\\''.
    """
    escaped = str(path).replace("'", "'\\''")
    return f"file '{escaped}'"


# ===========================================================================
# Асинхронный опрос возможностей FFmpeg
# ===========================================================================


class CapabilityProbe(QObject):
    """
    Фоновый опрос возможностей установленной сборки FFmpeg.

    Опрос выполняет пять коротких запусков бинарника, что занимает заметное
    время на медленных дисках, поэтому работа вынесена в пул потоков, а
    результат приходит сигналом в поток интерфейса.
    """

    # Успешный результат опроса: объект FFmpegCapabilities.
    ready = Signal(object)
    # Текст ошибки при отсутствии бинарника или сбое опроса.
    failed = Signal(str)

    def start(self, ffmpeg_path: str | None = None) -> None:
        """Постановка задачи опроса в глобальный пул потоков."""
        QThreadPool.globalInstance().start(_ProbeTask(self, ffmpeg_path))


class _ProbeTask(QRunnable):
    """Задача пула потоков, выполняющая блокирующий опрос."""

    def __init__(self, owner: CapabilityProbe, ffmpeg_path: str | None) -> None:
        super().__init__()
        self._owner = owner
        self._ffmpeg_path = ffmpeg_path

    def run(self) -> None:
        """Точка входа рабочего потока."""
        try:
            capabilities: FFmpegCapabilities = probe_capabilities(self._ffmpeg_path)
        except Exception as error:  # noqa: BLE001 - причина уходит в интерфейс
            # Сигнал испускается из рабочего потока и доставляется потоку
            # владельца через очередь событий Qt.
            self._emit(self._owner.failed, str(error))
            return
        self._emit(self._owner.ready, capabilities)

    @staticmethod
    def _emit(signal: SignalInstance, payload: object) -> None:
        """Безопасное испускание сигнала уже уничтоженного владельца."""
        try:
            signal.emit(payload)
        except RuntimeError:
            # Владелец удалён во время работы задачи: результат не нужен.
            pass


# ===========================================================================
# Выполнение многошаговых заданий
# ===========================================================================


class FFmpegTaskRunner(QObject):
    """
    Последовательный исполнитель списка шагов FFmpeg.

    Применяется для заданий без живого захвата: двухпроходной сборки GIF,
    конвертации в WebP и APNG, склейки фрагментов записи. Шаги выполняются
    строго по очереди, поскольку каждый следующий может использовать
    результат предыдущего.
    """

    # Номер текущего шага, общее число шагов и его подпись.
    stepStarted = Signal(int, int, str)
    # Очередной отчёт о прогрессе текущего шага.
    progress = Signal(object)
    # Строка журнала FFmpeg.
    log = Signal(str)
    # Все шаги выполнены успешно.
    finished = Signal()
    # Текст ошибки выполнения.
    failed = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._steps: list[FFmpegStep] = []
        self._index = 0
        self._process: QProcess | None = None
        self._parser = ProgressParser()
        self._log_tail: deque[str] = deque(maxlen=LOG_TAIL_SIZE)
        # Файлы, удаляемые после завершения задания независимо от исхода.
        self._temporary: set[Path] = set()
        self._cancelled = False

    @property
    def is_running(self) -> bool:
        """Признак выполняющегося задания."""
        return self._process is not None

    def start(self, steps: Sequence[FFmpegStep]) -> None:
        """Запуск задания. Повторный запуск во время работы запрещён."""
        if self.is_running:
            raise RuntimeError("Задание уже выполняется")
        if not steps:
            # Пустой список считается успешно выполненным заданием:
            # вызывающая сторона получит сигнал завершения без запусков.
            self.finished.emit()
            return

        self._steps = list(steps)
        self._index = 0
        self._cancelled = False
        self._log_tail.clear()
        # Список временных файлов собирается заранее: при сбое на втором
        # шаге палитру первого прохода всё равно требуется удалить.
        self._temporary = {path for step in self._steps for path in step.temporary}
        self._start_step()

    def cancel(self) -> None:
        """Отмена задания с остановкой текущего процесса."""
        if not self.is_running:
            return
        self._cancelled = True
        if self._process is not None:
            request_graceful_stop(self._process)

    # -------------------------------------------------------- внутренняя часть

    def _start_step(self) -> None:
        """Запуск очередного шага задания."""
        step = self._steps[self._index]
        self._parser.reset()

        process = create_process(self)
        process.readyReadStandardOutput.connect(self._on_stdout)
        process.readyReadStandardError.connect(self._on_stderr)
        process.finished.connect(self._on_finished)
        process.errorOccurred.connect(self._on_error)
        self._process = process

        self.stepStarted.emit(self._index + 1, len(self._steps), step.label)
        # Команда пишется в журнал приложения: это единственное место,
        # где список аргументов превращается в строку, и только для чтения.
        self.log.emit(f"$ {step.printable}")
        start_process(process, step.args)

    def _on_stdout(self) -> None:
        """Разбор машиночитаемого потока прогресса."""
        if self._process is None:
            return
        chunk = bytes(self._process.readAllStandardOutput().data()).decode("utf-8", "replace")
        for report in self._parser.feed(chunk):
            self.progress.emit(report)

    def _on_stderr(self) -> None:
        """Накопление журнала работы кодировщика."""
        if self._process is None:
            return
        chunk = bytes(self._process.readAllStandardError().data()).decode("utf-8", "replace")
        for line in chunk.splitlines():
            stripped = line.strip()
            if stripped:
                self._log_tail.append(stripped)
                self.log.emit(stripped)

    def _on_error(self, error: QProcess.ProcessError) -> None:
        """Обработка сбоя самого запуска процесса."""
        if error == QProcess.ProcessError.FailedToStart:
            # Отдельная ветка: сигнал finished в этом случае не приходит.
            self._fail("Не удалось запустить FFmpeg: проверьте путь к бинарнику.")

    def _on_finished(self, exit_code: int, status: QProcess.ExitStatus) -> None:
        """Обработка завершения шага и переход к следующему."""
        self._detach_process()

        if self._cancelled:
            self._cleanup_temporary()
            self._steps.clear()
            self.failed.emit("Задание отменено")
            return

        crashed = status == QProcess.ExitStatus.CrashExit
        if crashed or exit_code not in GRACEFUL_EXIT_CODES:
            self._fail(
                f"Шаг «{self._steps[self._index].label}» завершился с кодом {exit_code}.\n"
                f"{summarize_log(self._log_tail)}"
            )
            return

        self._index += 1
        if self._index < len(self._steps):
            self._start_step()
            return

        # Все шаги пройдены: промежуточные файлы больше не нужны.
        self._cleanup_temporary()
        self.finished.emit()

    def _fail(self, message: str) -> None:
        """Завершение задания с ошибкой и очисткой временных файлов."""
        self._detach_process()
        self._cleanup_temporary()
        self.failed.emit(message)

    def _detach_process(self) -> None:
        """Отключение обработчиков и освобождение объекта процесса."""
        process, self._process = self._process, None
        if process is not None:
            # Блокировка сигналов исключает повторную обработку событий уже
            # завершённого процесса. Массовое отключение соединений в этой
            # привязке Qt не поддерживается, поэтому применяется блокировка.
            process.blockSignals(True)
            process.deleteLater()

    def _cleanup_temporary(self) -> None:
        """Удаление промежуточных файлов задания."""
        for path in self._temporary:
            try:
                path.unlink(missing_ok=True)
            except OSError as error:
                # Неудача удаления не является причиной срыва задания:
                # достаточно записи в журнал.
                self.log.emit(f"Не удалось удалить временный файл {path}: {error}")
        self._temporary.clear()


# ===========================================================================
# Живая запись экрана
# ===========================================================================


@dataclass
class RecordingJob:
    """
    Описание задания записи экрана.

    Команда захвата собирается вызываемым объектом make_step, а не передаётся
    готовой: при использовании паузы запись разбивается на несколько
    фрагментов, и для каждого требуется собственный путь вывода.
    """

    # Итоговый файл, который увидит пользователь.
    output_path: Path
    # Сборщик команды захвата для очередного фрагмента.
    make_step: Callable[[Path], FFmpegStep]
    # Путь к бинарнику, используемый на этапе склейки фрагментов.
    ffmpeg_path: str = "ffmpeg"
    # Расширение файлов фрагментов. Matroska выбрана за устойчивость к
    # аварийному обрыву и за возможность склейки без перекодирования.
    segment_suffix: str = ".mkv"
    # Мультиплексор, применяемый при склейке фрагментов.
    segment_muxer: str = "matroska"
    # Необязательная постобработка: получает путь исходной записи и путь
    # результата, возвращает шаги FFmpeg. Используется для GIF, WebP и APNG.
    post_process: Callable[[Path, Path], list[FFmpegStep]] | None = None
    # Каталог для фрагментов. По умолчанию создаётся рядом с результатом,
    # что обеспечивает перемещение готового файла в пределах одной
    # файловой системы, то есть мгновенно и атомарно.
    work_dir: Path | None = None


class ScreenRecorder(QObject):
    """
    Контроллер живой записи экрана.

    Жизненный цикл: IDLE -> STARTING -> RECORDING -> (PAUSED) -> STOPPING
    -> PROCESSING -> FINISHED. Любой сбой переводит контроллер в FAILED,
    при этом уже записанные фрагменты сохраняются и по возможности
    собираются в готовый файл.
    """

    # Смена состояния: передаётся значение RecorderState.
    stateChanged = Signal(object)
    # Отчёт о прогрессе текущего фрагмента или шага постобработки.
    progress = Signal(object)
    # Суммарная длительность записи в миллисекундах без учёта пауз.
    elapsedChanged = Signal(int)
    # Строка журнала для окна диагностики.
    log = Signal(str)
    # Некритичное предупреждение, показываемое уведомлением.
    warning = Signal(str)
    # Успешное завершение: передаётся путь готового файла.
    finished = Signal(object)
    # Критическая ошибка с текстом причины.
    failed = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._state = RecorderState.IDLE
        self._job: RecordingJob | None = None
        self._process: QProcess | None = None
        self._parser = ProgressParser()
        self._log_tail: deque[str] = deque(maxlen=LOG_TAIL_SIZE)

        # Накопленные фрагменты записи в порядке их создания.
        self._segments: list[Path] = []
        self._segment_index = 0
        self._work_dir: Path | None = None

        # Флаги намерения: отличают запрошенную остановку от аварийного
        # завершения процесса захвата.
        self._pause_requested = False
        self._stop_requested = False
        # Признак уже показанного предупреждения о пропуске кадров:
        # сообщение не должно повторяться на каждом отчёте прогресса.
        self._drop_warned = False

        # Учёт длительности: таймер измеряет текущий отрезок, накопитель
        # хранит сумму завершённых отрезков между паузами.
        self._elapsed = QElapsedTimer()
        self._accumulated_ms = 0
        # Признак получения первого отчёта о прогрессе текущего фрагмента.
        # Запуск процесса FFmpeg занимает до половины секунды, и отсчёт
        # длительности до фактического начала захвата завышал показания.
        self._segment_capturing = False
        # Длительность материала текущего фрагмента по данным FFmpeg.
        self._segment_out_time_us = 0
        self._tick = QTimer(self)
        self._tick.setInterval(ELAPSED_TICK_MS)
        self._tick.timeout.connect(self._emit_elapsed)

        # Исполнитель этапов склейки и постобработки.
        self._runner = FFmpegTaskRunner(self)
        self._runner.progress.connect(self.progress)
        self._runner.log.connect(self.log)
        self._runner.finished.connect(self._on_postprocess_finished)
        self._runner.failed.connect(self._on_postprocess_failed)

    # ------------------------------------------------------------- свойства

    @property
    def state(self) -> RecorderState:
        """Текущее состояние контроллера."""
        return self._state

    def elapsed_ms(self) -> int:
        """Суммарная длительность записи без учёта времени на паузе."""
        current = self._elapsed.elapsed() if self._elapsed.isValid() else 0
        return self._accumulated_ms + int(current)

    # -------------------------------------------------------- команды записи

    def start(self, job: RecordingJob) -> None:
        """Начало новой записи."""
        if self._state.is_busy:
            raise RuntimeError("Запись уже выполняется")

        self._job = job
        self._segments = []
        self._segment_index = 0
        self._accumulated_ms = 0
        self._pause_requested = False
        self._stop_requested = False
        self._drop_warned = False
        self._log_tail.clear()

        try:
            # Создание каталогов выполняется до запуска захвата: отсутствие
            # прав на запись должно выясниться немедленно, а не после того,
            # как пользователь отснял материал.
            job.output_path.parent.mkdir(parents=True, exist_ok=True)
            self._work_dir = self._prepare_work_dir(job)
        except OSError as error:
            self._set_state(RecorderState.FAILED)
            self.failed.emit(f"Не удалось подготовить каталог записи: {error}")
            return

        self._set_state(RecorderState.STARTING)
        self._start_segment()

    def pause(self) -> None:
        """
        Постановка записи на паузу.

        Текущий фрагмент корректно закрывается, следующий начнётся с
        отдельного файла. Склейка выполняется при остановке записи.
        """
        if self._state is not RecorderState.RECORDING or self._process is None:
            return
        self._pause_requested = True
        self._set_state(RecorderState.STOPPING)
        request_graceful_stop(self._process)

    def resume(self) -> None:
        """Продолжение записи после паузы новым фрагментом."""
        if self._state is not RecorderState.PAUSED or self._job is None:
            return
        self._segment_index += 1
        self._set_state(RecorderState.STARTING)
        self._start_segment()

    def stop(self) -> None:
        """Штатная остановка записи с последующей сборкой результата."""
        if self._state is RecorderState.PAUSED:
            # Процесса захвата уже нет: можно сразу переходить к сборке.
            self._finalize()
            return
        if self._state not in (RecorderState.RECORDING, RecorderState.STARTING):
            return
        self._stop_requested = True
        self._set_state(RecorderState.STOPPING)
        if self._process is not None:
            request_graceful_stop(self._process)

    def abort(self, keep_segments: bool = False) -> None:
        """
        Немедленное прерывание записи.

        Операция необратима и по умолчанию удаляет отснятый материал,
        поэтому вызывается только после подтверждения пользователем.
        Аргумент keep_segments оставляет фрагменты в рабочем каталоге.
        """
        if self._process is not None:
            self._process.kill()
            self._detach_process()
        self._stop_tick()
        if not keep_segments:
            self._cleanup_work_dir()
        self._set_state(RecorderState.IDLE)

    # -------------------------------------------------- управление фрагментами

    def _prepare_work_dir(self, job: RecordingJob) -> Path:
        """Создание каталога для промежуточных фрагментов записи."""
        if job.work_dir is not None:
            work_dir = job.work_dir
        else:
            # Каталог располагается рядом с результатом: перемещение готового
            # файла остаётся операцией в пределах одной файловой системы.
            work_dir = job.output_path.parent / f".linscreen-{job.output_path.stem}"
        work_dir.mkdir(parents=True, exist_ok=True)
        return work_dir

    def _joined_path(self) -> Path:
        """Путь файла, получаемого склейкой фрагментов."""
        assert self._job is not None and self._work_dir is not None
        return self._work_dir / f"joined{self._job.segment_suffix}"

    def _segment_path(self) -> Path:
        """Путь очередного фрагмента записи."""
        assert self._job is not None and self._work_dir is not None
        name = f"segment_{self._segment_index:03d}{self._job.segment_suffix}"
        return self._work_dir / name

    def _start_segment(self) -> None:
        """Запуск процесса захвата для очередного фрагмента."""
        assert self._job is not None
        segment = self._segment_path()
        step = self._job.make_step(segment)
        self._parser.reset()
        self._segment_capturing = False
        self._segment_out_time_us = 0

        process = create_process(self)
        process.started.connect(self._on_started)
        process.readyReadStandardOutput.connect(self._on_stdout)
        process.readyReadStandardError.connect(self._on_stderr)
        process.finished.connect(self._on_segment_finished)
        process.errorOccurred.connect(self._on_error)
        self._process = process

        self.log.emit(f"$ {step.printable}")
        start_process(process, step.args)

    def _on_started(self) -> None:
        """Подтверждение фактического старта процесса захвата."""
        # Состояние меняется сразу, а отсчёт длительности начинается только
        # с первым отчётом о прогрессе, то есть с первым снятым кадром.
        self._set_state(RecorderState.RECORDING)

    def _on_stdout(self) -> None:
        """Разбор потока прогресса текущего фрагмента."""
        if self._process is None:
            return
        chunk = bytes(self._process.readAllStandardOutput().data()).decode("utf-8", "replace")
        for report in self._parser.feed(chunk):
            if not self._segment_capturing:
                # Первый отчёт означает, что захват действительно начался:
                # с этого момента ведётся отсчёт длительности записи.
                self._segment_capturing = True
                self._elapsed.restart()
                self._tick.start()
            if report.out_time_us > 0:
                # Длительность берётся у самого кодировщика: она точно
                # соответствует тому, что записано в файл.
                self._segment_out_time_us = report.out_time_us
            self.progress.emit(report)
            if self._drops_are_significant(report):
                # Устойчивый пропуск кадров означает, что источник не
                # успевает за заданной частотой; сообщение показывается
                # однократно за запись.
                self._warn_once_about_drops(report)

    def _on_stderr(self) -> None:
        """Накопление журнала процесса захвата."""
        if self._process is None:
            return
        chunk = bytes(self._process.readAllStandardError().data()).decode("utf-8", "replace")
        for line in chunk.splitlines():
            stripped = line.strip()
            if stripped:
                self._log_tail.append(stripped)
                self.log.emit(stripped)

    def _on_error(self, error: QProcess.ProcessError) -> None:
        """Обработка невозможности запуска процесса захвата."""
        if error == QProcess.ProcessError.FailedToStart:
            self._stop_tick()
            self._detach_process()
            self._set_state(RecorderState.FAILED)
            self.failed.emit(
                "Не удалось запустить FFmpeg. Требуется проверить установку "
                "пакета и путь к бинарнику в настройках."
            )

    def _on_segment_finished(self, exit_code: int, status: QProcess.ExitStatus) -> None:
        """Обработка завершения процесса захвата одного фрагмента."""
        self._stop_tick()
        # В накопитель переносится длительность, сообщённая кодировщиком:
        # время запуска процесса и сброса буферов в неё не входит.
        if self._segment_out_time_us > 0:
            self._accumulated_ms += self._segment_out_time_us // 1000
        elif self._elapsed.isValid():
            # Отчёты не поступили: применяется измеренное время работы.
            self._accumulated_ms += int(self._elapsed.elapsed())
        self._elapsed.invalidate()
        self._segment_capturing = False
        self._detach_process()

        segment = self._segment_path()
        # Фрагмент учитывается только при наличии данных: пустой файл
        # сломал бы склейку демультиплексором concat.
        if segment.exists() and segment.stat().st_size > 0:
            self._segments.append(segment)
        else:
            self.warning.emit("Фрагмент записи оказался пустым и пропущен.")

        expected = self._pause_requested or self._stop_requested
        crashed = status == QProcess.ExitStatus.CrashExit
        if not expected or (crashed and not self._stop_requested):
            # Аварийное завершение захвата. Материал не выбрасывается:
            # записанные фрагменты собираются в файл, а пользователь
            # получает предупреждение о неполноте записи.
            self.warning.emit(
                "Захват прерван неожиданно, выполняется аварийное сохранение.\n"
                + summarize_log(self._log_tail, limit=3)
            )
            self._finalize()
            return

        if self._pause_requested:
            self._pause_requested = False
            self._set_state(RecorderState.PAUSED)
            return

        self._finalize()

    # ------------------------------------------------------- сборка результата

    def _finalize(self) -> None:
        """Склейка фрагментов и запуск постобработки."""
        assert self._job is not None
        self._set_state(RecorderState.PROCESSING)

        if not self._segments:
            # Типовая причина - недоступный источник захвата или отсутствующий
            # энкодер, поэтому к сообщению прикладывается хвост журнала.
            reason = summarize_log(self._log_tail, limit=3)
            self._cleanup_work_dir()
            self._set_state(RecorderState.FAILED)
            self.failed.emit(
                "Записать материал не удалось: фрагменты отсутствуют."
                + (f"\n{reason}" if reason else "")
            )
            return

        steps: list[FFmpegStep] = []
        source = self._segments[0]

        if len(self._segments) > 1:
            # Несколько фрагментов означают использование паузы. Склейка
            # выполняется демультиплексором concat с копированием потоков:
            # перекодирования не происходит, качество не страдает.
            try:
                joined, concat_step = self._build_concat_step()
            except OSError as error:
                self._set_state(RecorderState.FAILED)
                self.failed.emit(f"Не удалось подготовить склейку фрагментов: {error}")
                return
            steps.append(concat_step)
            source = joined

        if self._job.post_process is not None:
            # Постобработка формирует итоговый файл самостоятельно,
            # например собирает GIF в два прохода.
            steps.extend(self._job.post_process(source, self._job.output_path))

        if steps:
            self._runner.start(steps)
            return

        # Единственный фрагмент без постобработки: достаточно перемещения.
        self._move_into_place(source)

    def _build_concat_step(self) -> tuple[Path, FFmpegStep]:
        """Подготовка файла списка и шага склейки фрагментов."""
        assert self._job is not None and self._work_dir is not None
        list_path = self._work_dir / "segments.txt"
        joined = self._joined_path()

        # Файл списка занимает несколько десятков байт, поэтому его запись
        # не создаёт заметной задержки в потоке интерфейса.
        list_path.write_text(
            "\n".join(_concat_quote(segment) for segment in self._segments) + "\n",
            encoding="utf-8",
        )

        args = [
            self._job.ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-y",
            "-progress",
            "pipe:1",
            "-nostats",
            "-f",
            "concat",
            # Разрешение абсолютных путей в файле списка.
            "-safe",
            "0",
            "-i",
            str(list_path),
            # Явное отображение всех потоков входа обязательно: без него
            # действует выбор по умолчанию, оставляющий по одному потоку
            # каждого типа, и вторая звуковая дорожка была бы потеряна.
            "-map",
            "0",
            # Копирование потоков без перекодирования: параметры фрагментов
            # совпадают, так как все они сняты одним профилем.
            "-c",
            "copy",
            "-f",
            self._job.segment_muxer,
            str(joined),
        ]
        step = FFmpegStep(
            label="Склейка фрагментов записи",
            args=args,
            temporary=(list_path,),
        )
        return joined, step

    def _on_postprocess_finished(self) -> None:
        """Успешное завершение склейки и постобработки."""
        assert self._job is not None
        if self._job.post_process is not None:
            # Итоговый файл уже записан шагами постобработки.
            self._complete(self._job.output_path)
            return
        # Постобработки не было: перемещается результат склейки.
        self._move_into_place(self._joined_path())

    def _on_postprocess_failed(self, message: str) -> None:
        """Сбой на этапе склейки или постобработки."""
        # Рабочий каталог намеренно сохраняется: фрагменты остаются
        # доступными пользователю для ручного восстановления.
        self._set_state(RecorderState.FAILED)
        self.failed.emit(
            f"{message}\n\nИсходные фрагменты сохранены в каталоге {self._work_dir}"
        )

    def _move_into_place(self, source: Path) -> None:
        """Перемещение готового файла из рабочего каталога к результату."""
        assert self._job is not None
        try:
            # Перемещение в пределах одной файловой системы атомарно и
            # не требует копирования данных.
            os.replace(source, self._job.output_path)
        except OSError:
            try:
                # Запасной путь на случай, если рабочий каталог был
                # переопределён и находится на другом носителе.
                shutil.move(str(source), str(self._job.output_path))
            except OSError as error:
                self._set_state(RecorderState.FAILED)
                self.failed.emit(f"Не удалось сохранить файл записи: {error}")
                return
        self._complete(self._job.output_path)

    def _complete(self, result: Path) -> None:
        """Завершение задания с очисткой рабочего каталога."""
        self._cleanup_work_dir()
        self._set_state(RecorderState.FINISHED)
        self.finished.emit(result)

    # -------------------------------------------------------- вспомогательное

    def _cleanup_work_dir(self) -> None:
        """Удаление рабочего каталога вместе с остатками фрагментов."""
        if self._work_dir is None:
            return
        try:
            shutil.rmtree(self._work_dir, ignore_errors=True)
        finally:
            self._work_dir = None
            self._segments = []

    def _detach_process(self) -> None:
        """Отключение обработчиков и освобождение объекта процесса."""
        process, self._process = self._process, None
        if process is not None:
            # Сигналы блокируются до удаления объекта: завершённый процесс
            # не должен повторно попадать в обработчики контроллера.
            process.blockSignals(True)
            process.deleteLater()

    def _emit_elapsed(self) -> None:
        """Периодическая отправка длительности записи в интерфейс."""
        self.elapsedChanged.emit(self.elapsed_ms())

    def _stop_tick(self) -> None:
        """Остановка таймера обновления длительности."""
        if self._tick.isActive():
            self._tick.stop()
        self._emit_elapsed()

    def _set_state(self, state: RecorderState) -> None:
        """Смена состояния с оповещением подписчиков."""
        if state is self._state:
            return
        self._state = state
        self.stateChanged.emit(state)

    @staticmethod
    def _drops_are_significant(report: ProgressReport) -> bool:
        """
        Оценка существенности пропуска кадров.

        Единичные потери при запуске захвата неизбежны, поэтому учитывается
        либо заметное абсолютное число пропусков, либо их устойчивая доля
        на достаточно длинной записи.
        """
        if report.drop_frames >= DROP_WARNING_ABSOLUTE:
            return True
        return report.frame > 100 and report.drop_frames > report.frame * DROP_WARNING_RATIO

    def _warn_once_about_drops(self, report: ProgressReport) -> None:
        """Однократное предупреждение о пропуске кадров при захвате."""
        if self._drop_warned:
            return
        # Признак сбрасывается при запуске следующей записи.
        self._drop_warned = True
        self.warning.emit(
            f"Захват не успевает за заданной частотой: пропущено кадров "
            f"{report.drop_frames}. Рекомендуется снизить частоту или разрешение."
        )
