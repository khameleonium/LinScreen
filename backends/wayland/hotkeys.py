"""
Глобальные сочетания клавиш через портал GlobalShortcuts.

В Wayland приложение получает нажатия клавиш только в своих окнах:
перехват клавиатуры целиком запрещён протоколом ради безопасности.
Глобальные сочетания регистрирует рабочий стол по просьбе приложения
через портал org.freedesktop.portal.GlobalShortcuts. Приложение сообщает
перечень действий с желаемыми сочетаниями, а окончательное назначение
остаётся за пользователем и хранится в настройках рабочего стола.

Портал поддерживают KDE Plasma (с версии 5.27) и GNOME (с версии 48).
Plasma 5.27 реализует раннюю редакцию спецификации и с порталом версии
1.18 и новее действий не принимает. Для неё применяется служба сочетаний
KDE (kglobalaccel) напрямую - ей же пользуется и сам портал KDE, поэтому
сочетания видны и настраиваются в «Параметрах системы» как обычно.

При отсутствии обоих путей действия вызываются командой приложения из
командной строки (см. main.py): такую команду можно назначить на клавишу
в настройках любого рабочего стола.

Сигналы портала принимает поток подписки (core/portal.py), в поток
интерфейса они доставляются очередью событий Qt.
"""

from __future__ import annotations

from core.i18n import tr

import time
from typing import Any, cast

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QKeySequence

from core import portal
from core.actions import action_titles
from core.identity import APPLICATION_ID
from core.session import Desktop, DesktopSession
from core.workers import run_async

# Служба глобальных сочетаний KDE.
KGLOBALACCEL_SERVICE = "org.kde.kglobalaccel"
KGLOBALACCEL_PATH = "/kglobalaccel"
KGLOBALACCEL_INTERFACE = "org.kde.KGlobalAccel"
KGLOBALACCEL_COMPONENT_INTERFACE = "org.kde.kglobalaccel.Component"
# Имя компонента - идентификатор приложения без суффикса .desktop: для
# компонентов с таким суффиксом служба не отправляет сигнал, а пытается
# запустить одноимённое действие из ярлыка приложения.
KGLOBALACCEL_COMPONENT = APPLICATION_ID
KGLOBALACCEL_COMPONENT_TITLE = "LinScreen"
# Признаки вызова setShortcut: действие присутствует (работает), и
# сохранённое службой сочетание не подставляется вместо переданного.
KGLOBALACCEL_SET_PRESENT = 2
KGLOBALACCEL_NO_AUTOLOADING = 4

# Компоненты прежней отдельной Wayland-редакции. Они удерживают свои
# сочетания, и служба не отдаёт те же клавиши новому компоненту, поэтому
# при регистрации прежние компоненты удаляются.
LEGACY_KGLOBALACCEL_COMPONENTS = (
    "io.github.khameleonium.LinScreenWayland",
    "io.github.khameleonium.LinScreenWayland.desktop",
)

# Способы регистрации сочетаний.
BACKEND_PORTAL = "portal"
BACKEND_KGLOBALACCEL = "kglobalaccel"

# Пауза перед повторной попыткой, если служба сочетаний KDE ещё не запущена.
KGLOBALACCEL_RETRY_SEC = 5.0

# Наименьший промежуток между двумя срабатываниями одного действия.
# Часть реализаций присылает повторное событие при удержании клавиши, и
# без этого промежутка одно нажатие выполняло бы действие несколько раз.
REPEAT_GUARD_SEC = 0.4

# Обозначения модификаторов по спецификации XDG shortcuts.
_MODIFIERS: dict[str, str] = {
    "ctrl": "CTRL",
    "control": "CTRL",
    "alt": "ALT",
    "shift": "SHIFT",
    "super": "LOGO",
    "meta": "LOGO",
    "win": "LOGO",
}

# Названия клавиш в терминах xkb, которыми пользуется спецификация.
_KEY_NAMES: dict[str, str] = {
    "print": "Print",
    "printscreen": "Print",
    "prtsc": "Print",
    "space": "space",
    "tab": "Tab",
    "esc": "Escape",
    "escape": "Escape",
    "enter": "Return",
    "return": "Return",
    "insert": "Insert",
    "delete": "Delete",
    "home": "Home",
    "end": "End",
    "pageup": "Page_Up",
    "pagedown": "Page_Down",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
    "pause": "Pause",
}


def to_portal_trigger(combination: str) -> str:
    """
    Преобразование сочетания вида "Ctrl+Alt+R" в формат портала.

    Спецификация XDG shortcuts ожидает модификаторы прописными буквами и
    название клавиши по xkb: "CTRL+ALT+r", "SHIFT+Print", "LOGO+F5".
    """
    parts: list[str] = []
    chunks = [chunk.strip() for chunk in combination.split("+") if chunk.strip()]
    for index, chunk in enumerate(chunks):
        lowered = chunk.lower()
        is_last = index == len(chunks) - 1
        if lowered in _MODIFIERS and not is_last:
            parts.append(_MODIFIERS[lowered])
        elif lowered in _KEY_NAMES:
            parts.append(_KEY_NAMES[lowered])
        elif len(lowered) > 1 and lowered.startswith("f") and lowered[1:].isdigit():
            parts.append(lowered.upper())
        elif len(chunk) == 1:
            # Буквы указываются строчными: прописная означала бы символ,
            # набираемый с Shift, и сочетание не совпало бы.
            parts.append(chunk.lower())
        else:
            parts.append(chunk)
    return "+".join(parts)


def is_portal_available() -> bool:
    """Признак поддержки портала сочетаний клавиш (вызывается в фоне)."""
    return portal.interface_version(portal.SHORTCUTS_INTERFACE) is not None


def key_code(combination: str) -> int:
    """Код сочетания в представлении Qt (клавиша вместе с модификаторами)."""
    if not combination.strip():
        return 0
    sequence = QKeySequence(combination)
    if sequence.isEmpty():
        return 0
    # Первое сочетание последовательности — код клавиши с модификаторами.
    return int(sequence[0].toCombined())


def kwin_variants(code: int) -> list[int]:
    """
    Коды, под которыми KWin из Plasma 5 сообщает сочетание с Shift.

    KWin 5.27 в сеансе Wayland считает Shift израсходованным на выбор
    символа: Ctrl+Alt+Shift+J приходит как Ctrl+Alt+J, а Shift+Print - как
    SysReq. Сочетание регистрируется и в исходном виде (для версий, где
    ошибка исправлена), и в таком. Побочный эффект - вариант без Shift тоже
    срабатывает - меньшее зло, чем неработающая клавиша.
    """
    from PySide6.QtCore import Qt

    shift = int(Qt.KeyboardModifier.ShiftModifier.value)
    if not code & shift:
        return []
    modifiers_mask = int(Qt.KeyboardModifier.KeyboardModifierMask.value)
    key = code & ~modifiers_mask
    modifiers = code & modifiers_mask
    if int(Qt.Key.Key_A.value) <= key <= int(Qt.Key.Key_Z.value):
        return [modifiers & ~shift | key]
    if key == int(Qt.Key.Key_Print.value):
        return [modifiers & ~shift | int(Qt.Key.Key_SysReq.value)]
    return []


# Результат регистрации: способ, путь сеанса или компонента, назначенные
# сочетания и занятые другими приложениями {действие: (сочетание, владелец)}.
Registration = tuple[str, str, dict[str, str], dict[str, tuple[str, str]]]


def _register(mapping: dict[str, str], kde: bool, force: bool) -> Registration | None:
    """
    Регистрация действий подходящим способом.

    Пустое значение означает, что способов нет. Функция выполняется в
    фоновом потоке.
    """
    if kde and not _kglobalaccel_available():
        # Служба сочетаний KDE при входе в сеанс запускается не сразу.
        import time

        time.sleep(KGLOBALACCEL_RETRY_SEC)
    if kde and _kglobalaccel_available():
        # В KDE сочетания регистрируются в службе kglobalaccel напрямую,
        # портал не вызывается вовсе: портал KDE на каждый BindShortcuts
        # открывает «Параметры системы», а в Plasma 5.27 к тому же не
        # принимает действий от современных версий xdg-desktop-portal.
        path, assigned, conflicts = _bind_kglobalaccel(mapping, force)
        return BACKEND_KGLOBALACCEL, path, assigned, conflicts
    result = _bind(mapping)
    if result is None:
        return None
    return BACKEND_PORTAL, result[0], result[1], {}


def _kglobalaccel_call(method: str, signature: str | None = None, body: tuple = ()) -> tuple:
    """Вызов метода службы сочетаний KDE."""
    return portal.call(
        KGLOBALACCEL_PATH,
        KGLOBALACCEL_INTERFACE,
        method,
        signature,
        body,
        service=KGLOBALACCEL_SERVICE,
    )


def first_key_text(keys: Any) -> str:
    """
    Первое сочетание из ответа службы в текстовом виде.

    Служба передаёт сочетания либо списком кодов (ai), либо списком
    последовательностей (a(ai)), где сочетание - первый код
    последовательности.
    """
    for item in keys or []:
        code: Any = item
        # Структура (ai) приходит кортежем со списком кодов внутри.
        while isinstance(code, (list, tuple)):
            code = code[0] if code else 0
        if isinstance(code, int) and code:
            return QKeySequence(code).toString()
    return ""


def unregister_component(component: str) -> bool:
    """
    Удаление всех сочетаний компонента из службы сочетаний KDE.

    После удаления компонент исчезает из «Параметров системы». Возвращается
    признак того, что у компонента были действия.
    """
    names: set[str] = {*action_titles(), "_launch"}
    try:
        (actions,) = _kglobalaccel_call("allActionsForComponent", "as", ([component],))
        names |= {str(item[1]) for item in actions if len(item) > 1}
    except portal.PortalError:
        pass
    removed = False
    for name in sorted(names):
        try:
            (done,) = _kglobalaccel_call("unregister", "ss", (component, name))
            removed = removed or bool(done)
        except portal.PortalError:
            continue
    # Без действий компонент остаётся пустой записью в «Параметрах системы»;
    # очистка удаляет и её.
    path = "/component/" + "".join(char if char.isalnum() else "_" for char in component)
    try:
        portal.call(
            path,
            KGLOBALACCEL_COMPONENT_INTERFACE,
            "cleanUp",
            service=KGLOBALACCEL_SERVICE,
        )
    except portal.PortalError:
        pass
    return removed


def remove_all_shortcuts() -> list[str]:
    """
    Удаление сочетаний LinScreen из настроек KDE (текущих и прежней редакции).

    Возвращаются имена компонентов, у которых были сочетания. Функция
    выполняется в фоновом потоке либо из командной строки.
    """
    if not _kglobalaccel_available():
        return []
    return [
        component
        for component in (KGLOBALACCEL_COMPONENT, *LEGACY_KGLOBALACCEL_COMPONENTS)
        if unregister_component(component)
    ]


def _owner_of(code: int) -> str:
    """Название приложения и действия, которому служба отдала клавишу."""
    try:
        (action_id,) = _kglobalaccel_call("action", "i", (code,))
    except portal.PortalError:
        return ""
    parts = [str(item) for item in action_id]
    if len(parts) >= 4 and (parts[2] or parts[3]):
        return f"{parts[2] or parts[0]}: {parts[3] or parts[1]}"
    return parts[0] if parts else ""


def _kglobalaccel_available() -> bool:
    """Признак доступности службы сочетаний KDE."""
    try:
        portal.call(
            KGLOBALACCEL_PATH,
            KGLOBALACCEL_INTERFACE,
            "getComponent",
            "s",
            ("kwin",),
            service=KGLOBALACCEL_SERVICE,
            timeout=3.0,
        )
    except portal.PortalError:
        return False
    return True


def _bind_kglobalaccel(
    mapping: dict[str, str], force: bool
) -> tuple[str, dict[str, str], dict[str, tuple[str, str]]]:
    """
    Регистрация действий в службе сочетаний KDE.

    Без признака force служба сохраняет назначения, сделанные
    пользователем в «Параметрах системы», и переданное сочетание служит
    лишь начальным значением. С признаком force сочетания из настроек
    приложения заменяют сохранённые: так применяется их изменение в окне
    настроек LinScreen.
    """
    for legacy in LEGACY_KGLOBALACCEL_COMPONENTS:
        unregister_component(legacy)
    titles = action_titles()
    flags = KGLOBALACCEL_SET_PRESENT | (KGLOBALACCEL_NO_AUTOLOADING if force else 0)
    assigned: dict[str, str] = {}
    conflicts: dict[str, tuple[str, str]] = {}
    for action, combination in mapping.items():
        action_id = [
            KGLOBALACCEL_COMPONENT,
            action,
            KGLOBALACCEL_COMPONENT_TITLE,
            titles.get(action, action),
        ]
        portal.call(
            KGLOBALACCEL_PATH,
            KGLOBALACCEL_INTERFACE,
            "doRegister",
            "as",
            (action_id,),
            service=KGLOBALACCEL_SERVICE,
        )
        code = key_code(combination)
        requested = [code, *kwin_variants(code)] if code else []
        (keys,) = portal.call(
            KGLOBALACCEL_PATH,
            KGLOBALACCEL_INTERFACE,
            "setShortcut",
            "asaiu",
            (action_id, requested, flags),
            service=KGLOBALACCEL_SERVICE,
        )
        codes = [int(value) for value in keys if int(value)]
        assigned[action] = QKeySequence(codes[0]).toString() if codes else ""
        if code and code not in codes:
            # Служба не отдала клавишу: её держит другое приложение.
            owner = _owner_of(code)
            if owner and not owner.startswith(KGLOBALACCEL_COMPONENT):
                conflicts[action] = (combination, owner)
    (path,) = portal.call(
        KGLOBALACCEL_PATH,
        KGLOBALACCEL_INTERFACE,
        "getComponent",
        "s",
        (KGLOBALACCEL_COMPONENT,),
        service=KGLOBALACCEL_SERVICE,
    )
    return str(path), assigned, conflicts


def _bind(mapping: dict[str, str]) -> tuple[str, dict[str, str]] | None:
    """
    Создание сеанса и регистрация действий у портала.

    Возвращается путь сеанса и назначенные рабочим столом сочетания в
    человекочитаемом виде. Пустое значение означает отсутствие портала.
    Функция выполняется в фоновом потоке: портал может показать диалог
    подтверждения и ждать решения пользователя.
    """
    if not is_portal_available():
        return None

    titles = action_titles()
    shortcuts: list[tuple[str, dict[str, Any]]] = []
    for action, combination in mapping.items():
        options: dict[str, Any] = {"description": ("s", titles.get(action, action))}
        trigger = to_portal_trigger(combination)
        if trigger:
            options["preferred_trigger"] = ("s", trigger)
        shortcuts.append((action, options))

    session_token = portal.new_token()
    created = portal.request(
        portal.SHORTCUTS_INTERFACE,
        "CreateSession",
        "a{sv}",
        lambda token: (
            {
                "handle_token": ("s", token),
                "session_handle_token": ("s", session_token),
                # Перечень действий дублируется при создании сеанса: KDE
                # Plasma 5.27 следует ранней редакции спецификации и
                # принимает действия только здесь, игнорируя BindShortcuts.
                # Более новые реализации лишний параметр пропускают.
                "shortcuts": ("a(sa{sv})", shortcuts),
            },
        ),
    )
    session_path = str(created.get("session_handle", ""))
    if not session_path:
        raise portal.PortalError(tr("Портал не вернул идентификатор сеанса"))

    try:
        bound = portal.request(
            portal.SHORTCUTS_INTERFACE,
            "BindShortcuts",
            "oa(sa{sv})sa{sv}",
            lambda token: (session_path, shortcuts, "", {"handle_token": ("s", token)}),
        )
    except BaseException:
        portal.close_session(session_path)
        raise

    assigned: dict[str, str] = {}
    for action, properties in bound.get("shortcuts") or []:
        if isinstance(properties, dict):
            assigned[str(action)] = str(properties.get("trigger_description", ""))
    return session_path, assigned


class HotkeyManager(QObject):
    """Регистрация действий у портала и приём их срабатываний."""

    # Срабатывание сочетания: передаётся имя действия из настроек.
    activated = Signal(str)
    # Сообщение о невозможности регистрации, показываемое пользователю.
    unavailable = Signal(str)
    # Регистрация выполнена: передаются назначенные рабочим столом
    # сочетания в виде {действие: описание}.
    bound = Signal(dict)
    # Сочетания, занятые другими приложениями: {действие: (сочетание, владелец)}.
    conflicts = Signal(dict)
    # Сочетание изменено в настройках рабочего стола: действие и новое
    # сочетание (пустое - снято).
    changedExternally = Signal(str, str)

    def __init__(self, session: DesktopSession, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._session = session
        self._session_path = ""
        # Подписка на изменения сочетаний в настройках KDE.
        self._changes_listener: portal.SignalListener | None = None
        # Способ регистрации: портал или служба сочетаний KDE.
        self._backend = ""
        self._listener: portal.SignalListener | None = None
        self._mapping: dict[str, str] = {}
        # Сочетания, с которыми выполнена действующая регистрация.
        self._bound_mapping: dict[str, str] | None = None
        self._assigned: dict[str, str] = {}
        # Время последнего срабатывания каждого действия.
        self._last_fired: dict[str, float] = {}
        # Признак приостановки: применяется при вводе нового сочетания.
        self._suspended = False
        # Номер регистрации: ответ на устаревший запрос отбрасывается.
        self._serial = 0

    @property
    def backend(self) -> str:
        """Способ регистрации: portal, kglobalaccel либо пустая строка."""
        return self._backend

    @property
    def is_active(self) -> bool:
        """Признак действующей регистрации у портала."""
        return bool(self._session_path)

    @property
    def assigned(self) -> dict[str, str]:
        """Назначенные рабочим столом сочетания по действиям."""
        return dict(self._assigned)

    def apply(self, mapping: dict[str, str], force: bool = False) -> None:
        """
        Регистрация набора действий.

        Портал допускает одну регистрацию на сеанс, поэтому при изменении
        набора прежний сеанс закрывается и открывается новый. Неизменный
        набор повторно не регистрируется: рабочий стол мог показать диалог
        подтверждения, и повторять его без причины незачем.
        """
        wanted = {name: value.strip() for name, value in mapping.items()}
        if wanted == self._bound_mapping and self.is_active and not force:
            return
        self.stop()
        self._mapping = wanted
        self._serial += 1
        serial = self._serial
        run_async(
            self,
            _register,
            lambda result: self._on_bound(serial, result),
            lambda text: self._on_failed(serial, text),
            wanted,
            self._session.desktop_kind is Desktop.KDE,
            force,
        )

    def _on_bound(self, serial: int, result: object) -> None:
        """Приём результата регистрации."""
        if serial != self._serial:
            # Пришёл ответ на устаревшую регистрацию: её сеанс не нужен.
            if isinstance(result, tuple) and result[0] == BACKEND_PORTAL:
                run_async(self, portal.close_session, lambda _r: None, None, result[1])
            return
        if result is None:
            self.unavailable.emit(
                tr(
                    "Рабочий стол не поддерживает глобальные сочетания клавиш через портал. "
                    "Назначьте на клавиши команды вида «linscreen --screenshot» в "
                    "настройках рабочего стола."
                )
            )
            return
        backend, session_path, assigned, conflicts = cast(Registration, result)
        self._backend = backend
        self._session_path = session_path
        self._bound_mapping = dict(self._mapping)
        self._assigned = dict(assigned)
        self._subscribe()
        self.bound.emit(dict(assigned))
        if conflicts:
            self.conflicts.emit(dict(conflicts))

    def _on_failed(self, serial: int, text: str) -> None:
        """Отказ портала в регистрации."""
        if serial != self._serial:
            return
        self.unavailable.emit(tr("Не удалось зарегистрировать клавиши: {0}").format(text))

    def _subscribe(self) -> None:
        """Подписка на срабатывания сочетаний."""
        from jeepney import MatchRule

        if self._backend == BACKEND_KGLOBALACCEL:
            rule = MatchRule(
                type="signal",
                interface=KGLOBALACCEL_COMPONENT_INTERFACE,
                member="globalShortcutPressed",
                path=self._session_path,
            )
        else:
            rule = MatchRule(
                type="signal",
                interface=portal.SHORTCUTS_INTERFACE,
                member="Activated",
                path=portal.PORTAL_PATH,
            )
        listener = portal.SignalListener(rule, self)
        listener.received.connect(self._on_activated)
        self._listener = listener
        if self._backend == BACKEND_KGLOBALACCEL:
            # Изменения, сделанные пользователем в «Параметрах системы»,
            # служба сообщает сигналом yourShortcutGotChanged (ai) либо, в
            # Plasma 5.27 и новее, yourShortcutsChanged (a(ai)); они
            # переносятся в настройки приложения, чтобы следующий запуск их
            # не затёр. Правило без имени сигнала принимает оба.
            changes = portal.SignalListener(
                MatchRule(
                    type="signal",
                    interface=KGLOBALACCEL_INTERFACE,
                    path=KGLOBALACCEL_PATH,
                ),
                self,
            )
            changes.received.connect(self._on_changed_externally)
            self._changes_listener = changes

    def _on_changed_externally(self, message: Any) -> None:
        """Перенос изменения сочетания из настроек рабочего стола."""
        body: tuple[Any, ...] = tuple(getattr(message, "body", ()))
        if len(body) < 2 or not body[0] or str(body[0][0]) != KGLOBALACCEL_COMPONENT:
            return
        action = str(body[0][1])
        if action not in self._mapping:
            return
        text = first_key_text(body[1])
        if self._mapping.get(action) == text:
            return
        self._mapping[action] = text
        if self._bound_mapping is not None:
            self._bound_mapping[action] = text
        self.changedExternally.emit(action, text)

    def _on_activated(self, message: Any) -> None:
        """Обработка срабатывания сочетания."""
        body: tuple[Any, ...] = tuple(getattr(message, "body", ()))
        # Первое поле - сеанс портала либо компонент службы KDE: сигнал
        # чужого или устаревшего сеанса пропускается.
        expected = (
            KGLOBALACCEL_COMPONENT
            if self._backend == BACKEND_KGLOBALACCEL
            else self._session_path
        )
        if len(body) < 2 or str(body[0]) != expected:
            return
        if self._suspended:
            return
        action = str(body[1])
        moment = time.monotonic()
        if moment - self._last_fired.get(action, 0.0) < REPEAT_GUARD_SEC:
            return
        self._last_fired[action] = moment
        self.activated.emit(action)

    def stop(self) -> None:
        """Снятие регистрации и прекращение приёма срабатываний."""
        if self._listener is not None:
            self._listener.stop()
            self._listener = None
        if self._changes_listener is not None:
            self._changes_listener.stop()
            self._changes_listener = None
        if self._session_path and self._backend == BACKEND_PORTAL:
            run_async(self, portal.close_session, lambda _r: None, None, self._session_path)
        # Регистрация в службе KDE не снимается: она хранит назначения
        # пользователя, а без работающего приложения сигнал просто некому
        # принять.
        self._session_path = ""
        self._backend = ""
        self._bound_mapping = None

    def suspend(self) -> None:
        """
        Временная приостановка срабатываний.

        Применяется при вводе нового сочетания в настройках: иначе
        нажатие клавиш в поле ввода выполнило бы назначенное действие.
        """
        self._suspended = True

    def resume(self) -> None:
        """Возобновление срабатываний после приостановки."""
        self._suspended = False
