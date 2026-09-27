"""
Точка входа приложения LinScreen.

Приложение работает в системном трее и не имеет главного окна, поэтому
закрытие любого окна не должно завершать работу процесса.

В собранном виде терминал отсутствует, и необработанная ошибка осталась бы
незамеченной. Поэтому такие ошибки записываются в файл журнала и, по
возможности, показываются пользователю окном сообщения.
"""

from __future__ import annotations

from core.i18n import tr
from core.identity import APPLICATION_ID, APPLICATION_SLUG

import fcntl
import os
import signal
import socket
import sys
import traceback
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Any

from PySide6.QtCore import QSocketNotifier, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from app import LinScreenApplication
from ui.tray import TrayIcon

APPLICATION_VERSION = "1.0"

# Ключи командной строки, передающие действие работающему экземпляру.
# Их можно назначить на клавиши средствами рабочего стола, если портал
# глобальных сочетаний недоступен.
COMMAND_LINE_ACTIONS: dict[str, str] = {
    "--screenshot": "screenshot_region",
    "--screenshot-full": "screenshot_fullscreen",
    "--screenshot-window": "screenshot_window",
    "--record": "record_toggle",
    "--pause": "record_toggle_pause",
    "--settings": "open_settings",
    "--check-system": "check_system",
    "--quit": "quit",
}

# Число уже показанных сообщений об ошибке. Окно показывается только для
# первой: повторяющийся сбой в обработчике событий иначе завалил бы экран
# диалогами и сделал бы работу невозможной.
_reported_errors = 0

# Объекты обработки сигналов операционной системы. Ссылки удерживаются на
# время работы приложения: уведомитель и сокеты не должны быть удалены.
_signal_objects: list[object] = []


def lock_file_path() -> Path:
    """Путь файла блокировки, удерживаемого работающим приложением."""
    base = os.environ.get("XDG_RUNTIME_DIR") or os.environ.get("XDG_CACHE_HOME")
    if not base:
        base = str(Path.home() / ".cache")
    return Path(base) / f"{APPLICATION_SLUG}.lock"


def acquire_single_instance() -> object | None:
    """
    Захват признака единственного запущенного приложения.

    Второй запущенный экземпляр перехватывал бы те же горячие клавиши и
    показывал бы собственный оверлей поверх первого, из-за чего каждое
    действие выполнялось бы дважды. Возвращается удерживаемый файл либо
    пустое значение, если приложение уже работает.

    Блокировка снимается ядром при завершении процесса, поэтому
    аварийное завершение не оставляет её висящей.
    """
    path = lock_file_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("w")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return None

    try:
        handle.write(f"{os.getpid()}\n")
        handle.flush()
    except OSError:
        # Запись номера процесса носит справочный характер.
        pass
    return handle


def crash_log_path() -> Path:
    """Путь файла с записями о необработанных ошибках."""
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / APPLICATION_SLUG / "crash.log"


def record_crash(
    kind: type[BaseException],
    value: BaseException,
    trace: TracebackType | None,
) -> None:
    """
    Запись необработанной ошибки в файл и показ сообщения пользователю.

    Ошибка внутри обработчика события не прекращает работу приложения:
    цикл событий продолжается. Поэтому запись ведётся всегда, а окно
    показывается только для первой ошибки за запуск - иначе повторяющийся
    сбой сделал бы работу невозможной.

    Прерывание с клавиатуры обрабатывается отдельно: это штатный способ
    завершения работы, а не сбой.
    """
    global _reported_errors

    if issubclass(kind, KeyboardInterrupt):
        QApplication.quit()
        return

    path = crash_log_path()
    report = "".join(traceback.format_exception(kind, value, trace))
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Записи добавляются в конец: история сбоев сохраняется целиком.
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"\n===== {stamp} =====\n{report}")
    except OSError:
        # Невозможность записи не должна порождать вторую ошибку.
        pass

    _reported_errors += 1
    if _reported_errors > 1:
        # Последующие ошибки попадают только в файл.
        return

    if QApplication.instance() is not None:
        try:
            QMessageBox.critical(
                None,
                "LinScreen",
                tr(
                    "Произошла ошибка:\n{0}\n\nРабота продолжается. Подробности записаны в "
                    "файл:\n{1}\n\nДальнейшие ошибки будут записываться без показа этого окна."
                ).format(value, path),
            )
        except Exception:  # noqa: BLE001 - обработчик ошибок не вправе падать
            pass


def install_signal_handling(controller: LinScreenApplication) -> None:
    """
    Штатное завершение по сигналам операционной системы.

    Интерпретатор обрабатывает сигналы только при исполнении своего кода,
    а приложение почти всё время проводит в цикле событий Qt. Вместо
    периодического пробуждения, расходующего заряд и процессорное время,
    применяется пара сокетов: интерпретатор записывает в неё номер
    сигнала, а Qt пробуждается на готовности к чтению.

    Обрабатываются прерывание с терминала и запрос завершения при выходе
    из сеанса: в обоих случаях незаконченная запись корректно завершается.
    """
    receiver, sender = socket.socketpair()
    receiver.setblocking(False)
    sender.setblocking(False)
    signal.set_wakeup_fd(sender.fileno())

    notifier = QSocketNotifier(receiver.fileno(), QSocketNotifier.Type.Read)

    def on_signal() -> None:
        """Обработка полученного сигнала в потоке интерфейса."""
        try:
            receiver.recv(1024)
        except OSError:
            pass
        controller.quit()

    notifier.activated.connect(lambda *_: on_signal())

    # Обработчики оставляются пустыми: действие выполняет уведомитель,
    # которому сигнал доставляется через пару сокетов.
    for number in (signal.SIGINT, signal.SIGTERM):
        signal.signal(number, lambda *_: None)

    _signal_objects.extend((receiver, sender, notifier))


def _check_wayland(session: Any, problems: list[str]) -> None:
    """Проверка составных частей, нужных только в сессии Wayland."""
    from backends.wayland import gstreamer

    kit = gstreamer.select()
    missing_elements = kit.missing
    print(
        tr("GStreamer и PipeWire: {0}").format(
            (tr("вложенный") if kit.bundled else tr("системный"))
            if not missing_elements
            else tr("НЕТ")
        )
    )
    if missing_elements:
        problems.append(
            tr(
                "Недоступны элементы GStreamer: {0}. Запись экрана невозможна; требуются "
                "пакеты gstreamer1.0-tools, gstreamer1.0-plugins-base, gstreamer1.0-plugins-good "
                "и gstreamer1.0-pipewire."
            ).format(", ".join(missing_elements))
        )

    from core import autostart, portal

    # Ярлык создаётся до первого обращения к порталу: по нему портал
    # регистрирует приложение.
    autostart.ensure_application_entry()
    for interface, title in (
        (portal.SCREENSHOT_INTERFACE, tr("снимки экрана")),
        (portal.SCREENCAST_INTERFACE, tr("запись экрана")),
        (portal.SHORTCUTS_INTERFACE, tr("глобальные клавиши")),
    ):
        version = portal.interface_version(interface)
        state = tr("версия {0}").format(version) if version is not None else tr("НЕТ")
        print(tr("Портал «{0}»: {1}").format(title, state))
        if version is None:
            problems.append(tr("Портал не предоставляет возможность: {0}.").format(title))
    registration = portal.registration_error()
    print(
        tr("Регистрация приложения у портала: {0}").format(
            tr("выполнена") if not registration else registration
        )
    )

    from backends.wayland import kwin

    if session.desktop_kind.value == "kde":
        print(
            tr("Скрипты KWin (подсветка окон, рамка записи): {0}").format(
                tr("доступны") if kwin.is_available() else tr("недоступны")
            )
        )


def self_test() -> int:
    """
    Проверка работоспособности установленных составных частей.

    Вызывается ключом --check и предназначена для быстрой диагностики:
    выводит найденный FFmpeg, доступные кодеки, звуковые источники и тип
    сессии, после чего завершает работу.
    """
    from capture.audio import list_audio_devices
    from core.config import ConfigManager
    from core.i18n import set_language
    from core.runtime import bundle_dir, is_frozen
    from core.session import detect_session
    from encoder.ffmpeg import FFmpegNotFoundError, find_ffmpeg, probe_capabilities

    # Язык берётся из настроек: вывод проверки читает тот же человек,
    # который пользуется приложением.
    configuration = ConfigManager()
    configuration.load()
    set_language(configuration.settings.general.language)

    print(f"LinScreen {APPLICATION_VERSION}")
    print(
        tr("Режим запуска: {0}").format(
            tr("собранный файл") if is_frozen() else tr("исходные тексты")
        )
    )
    if bundle_dir() is not None:
        print(tr("Каталог вложений: {0}").format(bundle_dir()))

    from backends import platform_name

    session = detect_session()
    platform = platform_name(session)
    print(tr("Сессия: {0}, окружение: {1}").format(session.session_type.label, session.desktop))
    print(tr("Набор модулей графической системы: {0}").format(platform))

    try:
        binary = find_ffmpeg()
    except FFmpegNotFoundError as error:
        print(tr("FFmpeg: НЕ НАЙДЕН — {0}").format(error))
        return 1

    print(f"FFmpeg: {binary}")
    capabilities = probe_capabilities(binary)
    print(tr("Версия FFmpeg: {0}").format(capabilities.version))
    codecs = [
        name
        for name in ("libx264", "libx265", "libsvtav1", "libvpx-vp9", "ffv1", "prores_ks")
        if capabilities.has_encoder(name)
    ]
    print(tr("Видеокодеки: {0}").format(", ".join(codecs) or tr("отсутствуют")))
    if platform == "x11":
        print(
            tr("Захват экрана X11: {0}").format(
                tr("да") if capabilities.can_capture_x11 else tr("нет")
            )
        )
    else:
        print(
            tr("Приём кадров экрана (rawvideo): {0}").format(
                tr("да") if capabilities.can_read_screen_stream else tr("нет")
            )
        )
    print(
        tr("Захват звука PulseAudio: {0}").format(
            tr("да") if capabilities.can_capture_pulse else tr("НЕТ")
        )
    )
    print(
        tr("Палитра GIF: {0}").format(tr("да") if capabilities.can_build_gif_palette else tr("нет"))
    )

    from encoder.images import is_avif_available

    print(tr("Сохранение в AVIF: {0}").format(tr("да") if is_avif_available() else tr("нет")))

    devices = list_audio_devices()
    print(tr("Звуковых источников: {0}").format(len(devices)))
    for device in devices:
        kind = tr("монитор") if device.is_monitor else tr("вход")
        print(f"  [{kind}] {device.description}")

    problems = capabilities.missing_essentials(platform == "x11")
    if platform == "wayland":
        _check_wayland(session, problems)

    # Проверка составных частей, попадающих в сборку: их отсутствие
    # проявилось бы только при попытке воспользоваться соответствующей
    # возможностью, что заметно позже момента запуска.
    modules = [
        ("jeepney", tr("обмен с порталами рабочего стола")),
        ("PIL", tr("сохранение изображений")),
    ]
    if platform == "x11":
        modules += [
            ("pynput", tr("глобальные клавиши")),
            ("Xlib", tr("определение активного окна")),
        ]
    for module, purpose in modules:
        try:
            __import__(module)
            print(tr("Модуль {0}: есть ({1})").format(module, purpose))
        except ImportError:
            problems.append(tr("Недоступен модуль {0}: не работают {1}.").format(module, purpose))

    # Проверка оконной подсистемы: создание приложения и отрисовка значка.
    try:
        from PySide6.QtWidgets import QApplication, QSystemTrayIcon

        probe = QApplication.instance() or QApplication([])
        from ui.tray import render_tray_icon
        from encoder.process import RecorderState

        icon = render_tray_icon(RecorderState.IDLE)
        print(
            tr("Графическая подсистема: работает, значок {0} px").format(
                icon.availableSizes()[0].width()
            )
        )
        available = QSystemTrayIcon.isSystemTrayAvailable()
        print(tr("Системный трей: {0}").format(tr("доступен") if available else tr("НЕДОСТУПЕН")))
        del probe
    except Exception as error:  # noqa: BLE001 - причина выводится пользователю
        problems.append(tr("Оконная подсистема недоступна: {0}").format(error))

    for problem in problems:
        print(tr("ОГРАНИЧЕНИЕ: {0}").format(problem))
    print(
        tr("Проверка завершена: ") + (tr("есть ограничения") if problems else tr("всё в порядке"))
    )
    return 0


def apply_platform_argument(arguments: list[str]) -> None:
    """
    Явный выбор набора модулей ключом --platform x11|wayland.

    Ключ передаётся через переменную окружения: её читает выбор набора
    модулей, а вспомогательные процессы наследуют выбор.
    """
    from backends import PLATFORM_VARIABLE

    for index, argument in enumerate(arguments):
        value = ""
        if argument.startswith("--platform="):
            value = argument.split("=", 1)[1]
        elif argument == "--platform" and index + 1 < len(arguments):
            value = arguments[index + 1]
        if value.lower() in ("x11", "wayland"):
            os.environ[PLATFORM_VARIABLE] = value.lower()


def configure_qt_platform(platform: str) -> None:
    """
    Платформа Qt под выбранный набор модулей.

    Платформа задаётся явно: в сессии Wayland при наличии XWayland часть
    сборок выбирает прослойку X11, и порталы работают с чужим окном.
    """
    if platform == "wayland":
        os.environ.setdefault("QT_QPA_PLATFORM", "wayland")
        # Окна виджетов отрисовываются через OpenGL, а не в разделяемой
        # памяти: связка Qt 6.11 и KWin 5.27 искажает первые кадры окон,
        # переданные через разделяемую память. Значение 0 в окружении
        # возвращает прежний способ.
        os.environ.setdefault("QT_WIDGETS_RHI", "1")
    else:
        os.environ.setdefault("QT_QPA_PLATFORM", "xcb")


def uninstall_from_command_line(purge: bool) -> int:
    """
    Удаление следов программы из системы по ключу --uninstall.

    Работающий экземпляр сначала завершается: иначе он вернул бы ярлык и
    сочетания клавиш при следующей регистрации.
    """
    from core.bus_service import send_action
    from core.uninstall import uninstall

    if send_action("quit"):
        import time

        # Работающему экземпляру даётся время снять регистрацию.
        time.sleep(2)
    removed = uninstall(purge)
    if removed:
        print(tr("Удалено:"))
        for item in removed:
            print(f"  {item}")
    else:
        print(tr("Следов программы в системе не найдено."))
    return 0


def requested_action(arguments: list[str]) -> str | None:
    """Действие, переданное ключом командной строки, либо пустое значение."""
    for argument in arguments:
        if argument in COMMAND_LINE_ACTIONS:
            return COMMAND_LINE_ACTIONS[argument]
    return None


def main() -> int:
    """Запуск приложения и вход в цикл обработки событий."""
    if len(sys.argv) > 1 and sys.argv[1] == "--frame-helper":
        # Вспомогательный процесс рамки записи, см. ui/region_frame.py.
        from backends.wayland.region_frame import run_frame_helper

        return run_frame_helper(sys.argv[2:])
    if "--check" in sys.argv:
        apply_platform_argument(sys.argv)
        return self_test()
    if "--uninstall" in sys.argv:
        return uninstall_from_command_line("--purge" in sys.argv)
    if "--version" in sys.argv:
        print(f"LinScreen {APPLICATION_VERSION}")
        return 0

    from backends import PLATFORM_VARIABLE, platform_name
    from core import autostart
    from core.bus_service import claim_name, send_action
    from core.session import SessionType, detect_session

    apply_platform_argument(sys.argv)
    session = detect_session()
    action = requested_action(sys.argv[1:])

    # Ярлык приложения создаётся до первого обращения к шине: по нему
    # портал регистрирует приложение, а композитор находит значок окон.
    autostart.ensure_application_entry()

    # Имя службы на шине принадлежит работающему экземпляру. Если оно
    # занято, действие из командной строки передаётся ему, и процесс
    # завершается без показа окон.
    if not claim_name():
        # Повторный запуск без ключей (например, щелчком по значку в сетке
        # приложений) открывает настройки: без трея это единственный
        # видимый отклик работающего приложения.
        if send_action(action or "open_settings"):
            return 0
        print(
            tr("Приложение уже запущено: его значок находится в системном трее."),
            file=sys.stderr,
        )
        return 0

    sys.excepthook = record_crash

    configure_qt_platform(platform_name(session))
    # Идентификатор приложения передаётся композитору вместе с окнами:
    # по нему он находит ярлык, значок и название.
    QApplication.setDesktopFileName(APPLICATION_ID)
    application = QApplication(sys.argv)
    application.setApplicationName("LinScreen")
    application.setApplicationDisplayName("LinScreen")
    application.setOrganizationName("LinScreen")
    # Приложение живёт в трее: закрытие редактора или настроек работу
    # процесса не прекращает.
    application.setQuitOnLastWindowClosed(False)

    if session.session_type is SessionType.UNKNOWN and not os.environ.get(PLATFORM_VARIABLE):
        QMessageBox.critical(
            None,
            "LinScreen",
            tr(
                "Не удалось определить графическую систему (X11 или Wayland).\n\n"
                "Её можно указать явно ключом --platform x11 или --platform wayland."
            ),
        )
        return 1

    # Ссылка на файл блокировки удерживается до конца работы: закрытие
    # файла сняло бы признак единственного экземпляра.
    instance_lock = acquire_single_instance()
    if instance_lock is None:
        # Окно с сообщением здесь неуместно: запуск мог быть выполнен из
        # меню рабочего стола, и нажимать кнопку будет некому. Работающий
        # экземпляр уже виден значком в системном трее.
        print(
            tr("Приложение уже запущено: его значок находится в системном трее."),
            file=sys.stderr,
        )
        return 0
    _signal_objects.append(instance_lock)

    controller = LinScreenApplication()
    controller.start()
    if not TrayIcon.is_available():
        # Без трея приложение продолжает работать: действия доступны из
        # ярлыка в меню приложений, сочетаниями клавиш и командами.
        controller.report_missing_tray()
    install_signal_handling(controller)
    if action is not None:
        # Действие из командной строки первого запуска выполняется после
        # входа в цикл событий, когда значок уже показан.
        QTimer.singleShot(0, lambda: controller.perform_action(action))

    return application.exec()


if __name__ == "__main__":
    sys.exit(main())
