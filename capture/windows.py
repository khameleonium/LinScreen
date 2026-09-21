"""
Определение границ объектов интерфейса под указателем мыши.

Модуль строит снимок дерева окон X11: для каждого видимого окна
вычисляются границы в координатах экрана. Снимок делается один раз перед
показом оверлея выделения, после чего поиск объекта под курсором ведётся
в памяти — обращаться к X-серверу на каждое перемещение мыши слишком
накладно.

Порядок записей повторяет порядок отрисовки: родитель предшествует своим
потомкам, а окна одного уровня следуют снизу вверх.

Свёрнутые окна в перечень не попадают. Признак отображения самого X11 для
этого непригоден: при работающем композиторе свёрнутое окно остаётся
отображённым, его просто не рисуют. Состояние берётся из свойств,
которыми обменивается с приложениями менеджер окон.

В сессии Wayland дерево окон приложению недоступно; перечень окажется
пустым, и оверлей продолжит работать без автоматического выделения.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QPoint, QRect

# Наименьший размер объекта, попадающего в перечень. Отсекает служебные
# окна нулевого размера и точечные элементы, выделять которые бессмысленно.
MINIMUM_SIZE = 24

# Предельная глубина обхода дерева. Современные наборы виджетов рисуют
# содержимое окна сами и вложенных окон почти не создают, поэтому большая
# глубина лишь замедлила бы сбор.
MAXIMUM_DEPTH = 4

# Состояние свёрнутого окна согласно соглашению ICCCM.
ICONIC_STATE = 3
# Состояние видимого окна согласно соглашению ICCCM.
NORMAL_STATE = 1
# Состояние скрытого/отозванного окна согласно соглашению ICCCM.
WITHDRAWN_STATE = 0

# Глубина, до которой проверяется состояние окна. Менеджер окон помещает
# свойства на окно приложения, которое при наличии рамки оказывается её
# потомком; глубже искать незачем.
STATE_DEPTH = 2

# Обозначение окна, отображаемого на всех рабочих столах согласно EWMH.
ALL_DESKTOPS = 0xFFFFFFFF


@dataclass(frozen=True)
class InterfaceObject:
    """Прямоугольник объекта интерфейса в координатах экрана."""

    rect: QRect
    # Глубина вложенности: ноль соответствует окну верхнего уровня.
    depth: int = 0
    # Порядок наложения окон верхнего уровня (Z-порядок): выше значение — ближе к зрителю.
    group: int = 0

    @property
    def area(self) -> int:
        """Площадь объекта, применяется при сравнении совпадений."""
        return self.rect.width() * self.rect.height()


def list_objects(limit_rect: QRect | None = None) -> list[InterfaceObject]:
    """
    Снимок видимых объектов интерфейса.

    Возвращается перечень в порядке отрисовки. Пустой перечень означает,
    что дерево окон недоступно: в этом случае автоматическое выделение не
    применяется, а ручное продолжает работать.
    """
    try:
        from Xlib import X, display as xdisplay
    except ImportError:
        return []

    try:
        connection = xdisplay.Display()
    except Exception:  # noqa: BLE001 - отсутствие X11 не является ошибкой
        return []

    objects: list[InterfaceObject] = []
    try:
        any_prop = X.AnyPropertyType
        current_desktop_atom = connection.intern_atom("_NET_CURRENT_DESKTOP")
        atoms = _Atoms(
            wm_state=connection.intern_atom("WM_STATE"),
            net_state=connection.intern_atom("_NET_WM_STATE"),
            hidden=connection.intern_atom("_NET_WM_STATE_HIDDEN"),
            any_property=any_prop,
            net_desktop=connection.intern_atom("_NET_WM_DESKTOP"),
            net_client_list=connection.intern_atom("_NET_CLIENT_LIST"),
            net_window_type=connection.intern_atom("_NET_WM_WINDOW_TYPE"),
            net_window_type_desktop=connection.intern_atom(
                "_NET_WM_WINDOW_TYPE_DESKTOP"
            ),
            wm_transient_for=connection.intern_atom("WM_TRANSIENT_FOR"),
            wm_name=connection.intern_atom("WM_NAME"),
        )
        root = connection.screen().root
        current_desktop = _current_desktop(root, current_desktop_atom, any_prop)
        hidden_clients, hidden_frames = _build_hidden_sets(
            connection, root, atoms, current_desktop
        )
        _collect(
            root,
            0,
            0,
            0,
            objects,
            limit_rect,
            X.IsViewable,
            atoms,
            current_desktop,
            hidden_clients,
            hidden_frames,
            connection,
        )
    except Exception:  # noqa: BLE001 - неполный обход лучше отказа
        pass
    finally:
        try:
            connection.close()
        except Exception:  # noqa: BLE001 - соединение уже закрыто
            pass
    return objects


@dataclass(frozen=True)
class _Atoms:
    """Обозначения свойств X11, нужные для проверки состояния окна."""

    wm_state: int
    net_state: int
    hidden: int
    any_property: int
    net_desktop: int = 0
    net_client_list: int = 0
    net_window_type: int = 0
    net_window_type_desktop: int = 0
    wm_transient_for: int = 0
    wm_name: int = 0


def _current_desktop(root: object, atom: int, any_property: int) -> int | None:
    """Индекс текущего активного рабочего стола согласно EWMH."""
    try:
        prop = root.get_full_property(atom, any_property)  # type: ignore[attr-defined]
        if prop is not None and len(prop.value):
            return int(prop.value[0])
    except Exception:  # noqa: BLE001 - свойство может отсутствовать
        pass
    return None


def _build_hidden_sets(
    connection: object,
    root: object,
    atoms: _Atoms,
    current_desktop: int | None,
) -> tuple[set[int], set[int]]:
    """
    Формирование множеств идентификаторов скрытых клиентских окон и их рамок.

    Менеджер окон по спецификации EWMH ведёт список клиентов верхнего уровня
    в свойстве _NET_CLIENT_LIST корневого окна. Для каждого клиента проверяется
    состояние (WM_STATE != NormalState, наличие _NET_WM_STATE_HIDDEN или
    неактивный рабочий стол). Если клиент скрыт, он заносится в hidden_clients,
    а цепочка его предков-рамок вплоть до root — в hidden_frames.
    """
    hidden_clients: set[int] = set()
    hidden_frames: set[int] = set()

    if not atoms.net_client_list:
        return hidden_clients, hidden_frames

    try:
        prop = root.get_full_property(  # type: ignore[attr-defined]
            atoms.net_client_list, atoms.any_property
        )
        if prop is None or not prop.value:
            return hidden_clients, hidden_frames
        client_xids = prop.value
    except Exception:
        return hidden_clients, hidden_frames

    root_id = getattr(root, "id", 0)

    for client_xid in client_xids:
        try:
            create_res = getattr(connection, "create_resource_object", None)
            if create_res is None:
                continue
            win = create_res("window", client_xid)
        except Exception:
            continue

        is_client_hidden = False

        # Проверка состояния по соглашению ICCCM.
        try:
            state = win.get_property(atoms.wm_state, atoms.any_property, 0, 4)
            if state is not None and len(state.value):
                if state.value[0] != NORMAL_STATE:
                    is_client_hidden = True
        except Exception:
            pass

        # Проверка перечня состояний по соглашению EWMH.
        if not is_client_hidden:
            try:
                listed = win.get_full_property(atoms.net_state, atoms.any_property)
                if listed is not None and atoms.hidden in listed.value:
                    is_client_hidden = True
            except (TypeError, ValueError):
                pass
            except Exception:
                pass

        # Проверка рабочего стола.
        if not is_client_hidden and current_desktop is not None and atoms.net_desktop:
            try:
                desktop_prop = win.get_full_property(
                    atoms.net_desktop, atoms.any_property
                )
                if desktop_prop is not None and len(desktop_prop.value):
                    win_desktop = desktop_prop.value[0]
                    if win_desktop != ALL_DESKTOPS and win_desktop != current_desktop:
                        is_client_hidden = True
            except Exception:
                pass

        if is_client_hidden:
            hidden_clients.add(client_xid)
            # Подъём по дереву родителей к корневому окну для пометки внешних рамок.
            current = win
            while True:
                try:
                    tree = current.query_tree()
                    parent = tree.parent
                    if parent is None:
                        break
                    parent_id = getattr(parent, "id", 0)
                    if parent_id == root_id or parent_id == 0:
                        break
                    hidden_frames.add(parent_id)
                    current = parent
                except Exception:
                    break

    return hidden_clients, hidden_frames


def _is_hidden(
    window: object,
    atoms: _Atoms,
    current_desktop: int | None = None,
    depth: int = 0,
    hidden_clients: set[int] | None = None,
    hidden_frames: set[int] | None = None,
    connection: object | None = None,
) -> bool:
    """
    Признак свёрнутого или скрытого окна.

    Проверяются свойства состояния согласно ICCCM и EWMH:
    - совпадение с идентификаторами скрытых окон или их внешних рамок;
    - тип окна _NET_WM_WINDOW_TYPE (служебные окна рабочего стола отсекаются);
    - служебные полноэкранные окна-заглушки менеджера окон;
    - привязка WM_TRANSIENT_FOR к скрытому родительскому окну;
    - состояние WM_STATE: видимым является только NormalState (1);
      IconicState (3) и WithdrawnState (0) означают, что окно скрыто;
    - наличие атома _NET_WM_STATE_HIDDEN в перечне _NET_WM_STATE;
    - рабочий стол _NET_WM_DESKTOP: окно на неактивном рабочем столе не видно.

    Свойства могут располагаться как на самом окне, так и на окне приложения
    внутри рамки, поэтому при отсутствии признаков скрытия на текущем окне
    проверка продолжается для его потомков.
    """
    window_id = getattr(window, "id", None)
    if window_id is not None:
        if hidden_frames is not None and window_id in hidden_frames:
            return True
        if hidden_clients is not None and window_id in hidden_clients:
            return True

    try:
        # Отсечение окон рабочего стола (_NET_WM_WINDOW_TYPE_DESKTOP)
        if atoms.net_window_type and atoms.net_window_type_desktop:
            wtype = window.get_full_property(  # type: ignore[attr-defined]
                atoms.net_window_type, atoms.any_property
            )
            if wtype is not None:
                try:
                    if atoms.net_window_type_desktop in wtype.value:
                        return True
                except (TypeError, ValueError):
                    pass

        # Отсечение служебных защитных окон менеджера окон (guard window)
        if atoms.wm_name:
            name_prop = window.get_property(  # type: ignore[attr-defined]
                atoms.wm_name, atoms.any_property, 0, 32
            )
            if name_prop is not None and name_prop.value:
                val = name_prop.value
                if isinstance(val, (bytes, bytearray)) and b"guard window" in val.lower():
                    return True
                if isinstance(val, str) and "guard window" in val.lower():
                    return True

        # Проверка диалоговых/вспомогательных окон WM_TRANSIENT_FOR
        if atoms.wm_transient_for:
            trans_prop = window.get_full_property(  # type: ignore[attr-defined]
                atoms.wm_transient_for, atoms.any_property
            )
            if trans_prop is not None and len(trans_prop.value):
                parent_xid = trans_prop.value[0]
                if hidden_clients is not None and parent_xid in hidden_clients:
                    return True
                if hidden_frames is not None and parent_xid in hidden_frames:
                    return True
                if connection is not None:
                    try:
                        create_res = getattr(connection, "create_resource_object", None)
                        if create_res is not None:
                            parent_win = create_res("window", parent_xid)
                            attrs = parent_win.get_attributes()
                            if getattr(attrs, "map_state", 2) != 2:
                                return True
                    except Exception:
                        pass

        # Проверка состояния по соглашению ICCCM. Запрос выполняется с
        # any_property для устойчивости к различиям типов у менеджеров окон.
        state = window.get_property(  # type: ignore[attr-defined]
            atoms.wm_state, atoms.any_property, 0, 4
        )
        if state is not None and len(state.value):
            # Значение 1 соответствует NormalState. Любое иное значение
            # (в частности, 3 — IconicState или 0 — WithdrawnState) означает,
            # что окно свёрнуто либо скрыто.
            if state.value[0] != NORMAL_STATE:
                return True

        # Проверка перечня состояний по соглашению EWMH. Наличие
        # _NET_WM_STATE_HIDDEN указывает на свёрнутое окно.
        listed = window.get_full_property(  # type: ignore[attr-defined]
            atoms.net_state, atoms.any_property
        )
        if listed is not None:
            try:
                if atoms.hidden in listed.value:
                    return True
            except (TypeError, ValueError):
                pass

        # Проверка текущего рабочего стола: окно на другом рабочем столе
        # не должно считаться видимым на текущем экране.
        if current_desktop is not None and atoms.net_desktop:
            desktop_prop = window.get_full_property(  # type: ignore[attr-defined]
                atoms.net_desktop, atoms.any_property
            )
            if desktop_prop is not None and len(desktop_prop.value):
                win_desktop = desktop_prop.value[0]
                if win_desktop != ALL_DESKTOPS and win_desktop != current_desktop:
                    return True
    except Exception:  # noqa: BLE001 - окно могло исчезнуть между запросами
        return False

    if depth >= STATE_DEPTH:
        return False

    try:
        children = window.query_tree().children  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - окно исчезло во время обхода
        return False

    # Рамка окна может не нести собственных признаков скрытия либо иметь
    # NormalState, в то время как вложенное окно приложения свёрнуто.
    # Если хотя бы один потомок скрыт, вся рамка считается скрытой.
    return any(
        _is_hidden(
            child,
            atoms,
            current_desktop,
            depth + 1,
            hidden_clients,
            hidden_frames,
            connection,
        )
        for child in children
    )


def _collect(
    window: object,
    offset_x: int,
    offset_y: int,
    depth: int,
    objects: list[InterfaceObject],
    limit_rect: QRect | None,
    viewable: int,
    atoms: _Atoms,
    current_desktop: int | None,
    hidden_clients: set[int] | None = None,
    hidden_frames: set[int] | None = None,
    connection: object | None = None,
    group: int = 0,
) -> None:
    """Рекурсивный обход дерева окон с накоплением границ."""
    if depth > MAXIMUM_DEPTH:
        return

    try:
        children = window.query_tree().children  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - окно могло исчезнуть во время обхода
        return

    # Потомки перечисляются снизу вверх в порядке наложения Z-order.
    for idx, child in enumerate(children):
        try:
            attributes = child.get_attributes()
            if attributes.map_state != viewable:
                # Неотображённые окна выделять незачем.
                continue
            geometry = child.get_geometry()
        except Exception:  # noqa: BLE001 - окно исчезло между запросами
            continue

        if _is_hidden(
            child,
            atoms,
            current_desktop,
            0,
            hidden_clients,
            hidden_frames,
            connection,
        ):
            # Свёрнутое окно пропускается вместе со всем содержимым:
            # выделять то, чего не видно на экране, бессмысленно.
            continue

        x = offset_x + geometry.x
        y = offset_y + geometry.y
        rect = QRect(x, y, geometry.width, geometry.height)

        # Окна верхнего уровня (прямые потомки root при depth == 0) получают
        # порядковый номер наложения Z-order из индекса перечисления X11.
        # Все дочерние элементы наследуют группу своего окна верхнего уровня.
        current_group = idx if depth == 0 else group

        if geometry.width >= MINIMUM_SIZE and geometry.height >= MINIMUM_SIZE:
            if limit_rect is None or rect.intersects(limit_rect):
                objects.append(InterfaceObject(rect, depth, current_group))

        _collect(
            child,
            x,
            y,
            depth + 1,
            objects,
            limit_rect,
            viewable,
            atoms,
            current_desktop,
            hidden_clients,
            hidden_frames,
            connection,
            current_group,
        )


def object_at(objects: list[InterfaceObject], point: QPoint) -> QRect | None:
    """
    Границы объекта под указанной точкой.

    Среди окон верхнего уровня выбирается самое верхнее в порядке наложения
    (наибольший group), накрывающее указанную точку. Окна, лежащие ниже в
    стеке, считаются перекрытыми и исключаются из рассмотрения.

    Внутри выбранного окна верхнего уровня выбирается объект с наименьшей
    площадью (наиболее специфичный дочерний элемент интерфейса). При равной
    площади предпочитается объект с большей глубиной вложенности либо
    отрисованный позже.
    """
    matching: list[InterfaceObject] = [
        item for item in objects if item.rect.contains(point)
    ]
    if not matching:
        return None

    topmost_group = max(item.group for item in matching)
    candidates = [item for item in matching if item.group == topmost_group]

    best: InterfaceObject | None = None
    for item in candidates:
        if (
            best is None
            or item.area < best.area
            or (item.area == best.area and item.depth >= best.depth)
        ):
            best = item
    return QRect(best.rect) if best is not None else None
