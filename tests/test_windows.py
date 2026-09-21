"""Проверки поиска объекта интерфейса под курсором."""

from __future__ import annotations

import unittest

from PySide6.QtCore import QPoint, QRect

from capture.windows import (
    ICONIC_STATE,
    InterfaceObject,
    _Atoms,
    _build_hidden_sets,
    _is_hidden,
    object_at,
)


def make(
    x: int, y: int, width: int, height: int, depth: int = 0, group: int = 0
) -> InterfaceObject:
    """Объект интерфейса с заданными границами."""
    return InterfaceObject(QRect(x, y, width, height), depth, group)


class ObjectSearchTest(unittest.TestCase):
    """Выбор объекта среди накрывающих указанную точку."""

    def test_smallest_object_wins(self) -> None:
        """Из вложенных объектов выбирается наименьший."""
        objects = [make(0, 0, 1920, 1080), make(100, 100, 800, 600), make(200, 200, 300, 200)]
        found = object_at(objects, QPoint(250, 250))
        self.assertEqual(found, QRect(200, 200, 300, 200))

    def test_full_screen_object_does_not_win(self) -> None:
        """Служебное окно во весь экран уступает обычному окну."""
        objects = [make(50, 50, 400, 300), make(0, 0, 1920, 1080)]
        self.assertEqual(object_at(objects, QPoint(100, 100)), QRect(50, 50, 400, 300))

    def test_foreground_window_occludes_smaller_background_window(self) -> None:
        """Окно переднего плана перекрывает окно меньшего размера на заднем плане."""
        background = make(80, 80, 800, 550, group=0)
        foreground = make(0, 0, 1920, 1040, group=1)
        found = object_at([background, foreground], QPoint(100, 150))
        self.assertEqual(found, QRect(0, 0, 1920, 1040))

    def test_overlapping_windows_respect_stacking_order(self) -> None:
        """При частичном перекрытии окон в зоне пересечения побеждает верхнее."""
        win_bottom = make(100, 100, 400, 400, group=0)
        win_top = make(300, 100, 400, 400, group=1)
        objects = [win_bottom, win_top]
        # В зоне пересечения выбирается верхнее окно.
        self.assertEqual(object_at(objects, QPoint(350, 200)), QRect(300, 100, 400, 400))
        # В видимой части нижнего окна выбирается нижнее окно.
        self.assertEqual(object_at(objects, QPoint(150, 200)), QRect(100, 100, 400, 400))
        # В свободной части верхнего окна выбирается верхнее окно.
        self.assertEqual(object_at(objects, QPoint(550, 200)), QRect(300, 100, 400, 400))

    def test_foreground_child_widget_wins_over_background_window(self) -> None:
        """Виджет внутри окна переднего плана выбирается без пробития фона."""
        background = make(80, 80, 800, 550, group=0)
        foreground_frame = make(0, 0, 1920, 1040, depth=0, group=1)
        foreground_widget = make(100, 100, 200, 50, depth=1, group=1)
        objects = [background, foreground_frame, foreground_widget]
        found = object_at(objects, QPoint(150, 120))
        self.assertEqual(found, QRect(100, 100, 200, 50))

    def test_point_outside_returns_nothing(self) -> None:
        """Вне всех объектов совпадения нет."""
        objects = [make(100, 100, 200, 200)]
        self.assertIsNone(object_at(objects, QPoint(50, 50)))

    def test_equal_area_prefers_later(self) -> None:
        """При равной площади предпочитается отрисованный позже."""
        objects = [make(0, 0, 100, 100), make(0, 0, 100, 100, depth=1)]
        found = object_at(objects, QPoint(10, 10))
        self.assertEqual(found, QRect(0, 0, 100, 100))

    def test_empty_list_is_safe(self) -> None:
        """Пустой перечень не приводит к ошибке."""
        self.assertIsNone(object_at([], QPoint(10, 10)))

    def test_result_is_a_copy(self) -> None:
        """Возвращается копия: изменение результата не трогает перечень."""
        objects = [make(10, 10, 100, 100)]
        found = object_at(objects, QPoint(20, 20))
        assert found is not None
        found.setWidth(5)
        self.assertEqual(objects[0].rect.width(), 100)

    def test_area_is_computed(self) -> None:
        """Площадь объекта считается по его границам."""
        self.assertEqual(make(0, 0, 40, 30).area, 1200)


class FakeProperty:
    """Ответ X-сервера на запрос свойства окна."""

    def __init__(self, value: list[int] | bytes | str) -> None:
        self.value = value


class FakeAttributes:
    """Атрибуты окна X11."""

    def __init__(self, map_state: int = 2) -> None:
        self.map_state = map_state


class FakeWindow:
    """Окно X11 с заданными свойствами и потомками."""

    def __init__(
        self,
        id: int = 0,
        wm_state: int | None = None,
        net_state: list[int] | None = None,
        desktop: int | None = None,
        window_type: list[int] | None = None,
        wm_name: bytes | str | None = None,
        transient_for: int | None = None,
        map_state: int = 2,
        children: list["FakeWindow"] | None = None,
        parent: "FakeWindow | None" = None,
    ) -> None:
        self.id = id
        self._wm_state = wm_state
        self._net_state = net_state
        self._desktop = desktop
        self._window_type = window_type
        self._wm_name = wm_name
        self._transient_for = transient_for
        self._map_state = map_state
        self._children = children or []
        self._parent = parent

    def get_property(
        self, kind: int, _type: int, _offset: int, _length: int
    ) -> FakeProperty | None:
        """Значение свойства состояния по соглашению ICCCM."""
        if kind == ATOMS.wm_state and self._wm_state is not None:
            return FakeProperty([self._wm_state, 0])
        if ATOMS.wm_name and kind == ATOMS.wm_name and self._wm_name is not None:
            return FakeProperty(self._wm_name)
        return None

    def get_full_property(self, kind: int, _type: int) -> FakeProperty | None:
        """Значение перечня состояний по соглашению EWMH."""
        if kind == ATOMS.net_state and self._net_state is not None:
            return FakeProperty(self._net_state)
        if kind == ATOMS_WITH_DESKTOP.net_desktop and self._desktop is not None:
            return FakeProperty([self._desktop])
        if (
            ATOMS.net_window_type
            and kind == ATOMS.net_window_type
            and self._window_type is not None
        ):
            return FakeProperty(self._window_type)
        if (
            ATOMS.wm_transient_for
            and kind == ATOMS.wm_transient_for
            and self._transient_for is not None
        ):
            return FakeProperty([self._transient_for])
        if (
            ATOMS.net_client_list
            and kind == ATOMS.net_client_list
            and self._children
        ):
            return FakeProperty([c.id for c in self._children])
        return None

    def get_attributes(self) -> FakeAttributes:
        """Атрибуты отображения окна."""
        return FakeAttributes(self._map_state)

    def query_tree(self) -> object:
        """Потомки и родитель окна."""
        return type("Tree", (), {"children": self._children, "parent": self._parent})()


class FakeConnection:
    """Имитация соединения с X-сервером."""

    def __init__(self, windows: dict[int, FakeWindow]) -> None:
        self._windows = windows

    def create_resource_object(self, _rtype: str, xid: int) -> FakeWindow:
        if xid in self._windows:
            return self._windows[xid]
        raise ValueError(f"Window {xid} not found")


ATOMS = _Atoms(
    wm_state=1,
    net_state=2,
    hidden=3,
    any_property=0,
    net_desktop=4,
    net_client_list=5,
    net_window_type=6,
    net_window_type_desktop=7,
    wm_transient_for=8,
    wm_name=9,
)
ATOMS_WITH_DESKTOP = ATOMS


class HiddenWindowTest(unittest.TestCase):
    """Определение свёрнутых и скрытых окон."""

    def test_normal_window_is_visible(self) -> None:
        """Окно в обычном состоянии считается видимым."""
        self.assertFalse(_is_hidden(FakeWindow(wm_state=1), ATOMS))

    def test_iconic_window_is_hidden(self) -> None:
        """Свёрнутое окно определяется по состоянию ICCCM."""
        self.assertTrue(_is_hidden(FakeWindow(wm_state=ICONIC_STATE), ATOMS))

    def test_withdrawn_window_is_hidden(self) -> None:
        """Скрытое/отозванное окно определяется по состоянию WithdrawnState."""
        self.assertTrue(_is_hidden(FakeWindow(wm_state=0), ATOMS))

    def test_ewmh_hidden_is_recognised(self) -> None:
        """Скрытое окно определяется по перечню состояний EWMH."""
        self.assertTrue(_is_hidden(FakeWindow(wm_state=1, net_state=[3]), ATOMS))

    def test_frame_inherits_child_state(self) -> None:
        """Рамка свёрнутого окна собственных состояний не несёт."""
        frame = FakeWindow(children=[FakeWindow(wm_state=ICONIC_STATE)])
        self.assertTrue(_is_hidden(frame, ATOMS))

    def test_frame_with_normal_state_inherits_iconic_child(self) -> None:
        """Рамка с NormalState скрывается, если клиентское окно свернуто."""
        frame = FakeWindow(
            wm_state=1, children=[FakeWindow(wm_state=ICONIC_STATE)]
        )
        self.assertTrue(_is_hidden(frame, ATOMS))

    def test_frame_with_normal_state_inherits_ewmh_hidden_child(self) -> None:
        """Рамка с NormalState скрывается, если клиент имеет _NET_WM_STATE_HIDDEN."""
        frame = FakeWindow(
            wm_state=1, children=[FakeWindow(net_state=[3])]
        )
        self.assertTrue(_is_hidden(frame, ATOMS))

    def test_frame_of_visible_window(self) -> None:
        """Рамка обычного окна видимой и остаётся."""
        frame = FakeWindow(children=[FakeWindow(wm_state=1)])
        self.assertFalse(_is_hidden(frame, ATOMS))

    def test_window_without_state_is_visible(self) -> None:
        """Окно без свойств состояния скрытым не считается."""
        self.assertFalse(_is_hidden(FakeWindow(), ATOMS))

    def test_search_does_not_go_too_deep(self) -> None:
        """Поиск состояния ограничен по глубине вложенности."""
        deep = FakeWindow(children=[FakeWindow(children=[
            FakeWindow(children=[FakeWindow(wm_state=ICONIC_STATE)])])])
        self.assertFalse(_is_hidden(deep, ATOMS))

    def test_window_on_other_desktop_is_hidden(self) -> None:
        """Окно на другом рабочем столе считается скрытым."""
        win = FakeWindow(desktop=1)
        self.assertTrue(
            _is_hidden(win, ATOMS_WITH_DESKTOP, current_desktop=0)
        )

    def test_window_on_same_desktop_is_visible(self) -> None:
        """Окно на текущем рабочем столе считается видимым."""
        win = FakeWindow(wm_state=1, desktop=0)
        self.assertFalse(
            _is_hidden(win, ATOMS_WITH_DESKTOP, current_desktop=0)
        )

    def test_sticky_window_on_all_desktops_is_visible(self) -> None:
        """Окно на всех рабочих столах (0xFFFFFFFF) считается видимым."""
        win = FakeWindow(wm_state=1, desktop=0xFFFFFFFF)
        self.assertFalse(
            _is_hidden(win, ATOMS_WITH_DESKTOP, current_desktop=0)
        )

    def test_child_on_other_desktop_makes_frame_hidden(self) -> None:
        """Рамка окна со скрытым рабочим столом у потомка скрывается."""
        frame = FakeWindow(children=[FakeWindow(desktop=2)])
        self.assertTrue(
            _is_hidden(frame, ATOMS_WITH_DESKTOP, current_desktop=0)
        )

    def test_build_hidden_sets_identifies_hidden_clients_and_frames(self) -> None:
        """Сбор скрытых клиентов и их внешних рамок из _NET_CLIENT_LIST."""
        root = FakeWindow(id=1)
        frame_iconic = FakeWindow(id=10, parent=root)
        client_iconic = FakeWindow(
            id=100, wm_state=ICONIC_STATE, parent=frame_iconic
        )
        frame_iconic._children = [client_iconic]

        frame_normal = FakeWindow(id=20, parent=root)
        client_normal = FakeWindow(
            id=200, wm_state=1, parent=frame_normal
        )
        frame_normal._children = [client_normal]

        root._children = [client_iconic, client_normal]
        conn = FakeConnection({100: client_iconic, 200: client_normal})

        hidden_clients, hidden_frames = _build_hidden_sets(
            conn, root, ATOMS, current_desktop=None
        )
        self.assertIn(100, hidden_clients)
        self.assertNotIn(200, hidden_clients)
        self.assertIn(10, hidden_frames)
        self.assertNotIn(20, hidden_frames)

    def test_build_hidden_sets_skips_when_empty(self) -> None:
        """При отсутствии _NET_CLIENT_LIST множества остаются пустыми."""
        atoms_no_client_list = _Atoms(
            wm_state=1, net_state=2, hidden=3, any_property=0
        )
        root = FakeWindow(id=1)
        conn = FakeConnection({})
        clients, frames = _build_hidden_sets(
            conn, root, atoms_no_client_list, current_desktop=None
        )
        self.assertEqual(len(clients), 0)
        self.assertEqual(len(frames), 0)

    def test_window_in_hidden_frames_is_hidden(self) -> None:
        """Окно-рамка из множества hidden_frames считается скрытым."""
        frame = FakeWindow(id=42)
        self.assertTrue(_is_hidden(frame, ATOMS, hidden_frames={42}))

    def test_window_in_hidden_clients_is_hidden(self) -> None:
        """Клиентское окно из множества hidden_clients считается скрытым."""
        win = FakeWindow(id=99)
        self.assertTrue(_is_hidden(win, ATOMS, hidden_clients={99}))

    def test_desktop_window_type_is_hidden(self) -> None:
        """Окно рабочего стола (_NET_WM_WINDOW_TYPE_DESKTOP) скрывается."""
        desktop_win = FakeWindow(window_type=[ATOMS.net_window_type_desktop])
        self.assertTrue(_is_hidden(desktop_win, ATOMS))

    def test_guard_window_is_hidden(self) -> None:
        """Служебное окно менеджера окон (guard window) скрывается."""
        guard_bytes = FakeWindow(wm_name=b"muffin guard window")
        self.assertTrue(_is_hidden(guard_bytes, ATOMS))
        guard_str = FakeWindow(wm_name="mutter guard window")
        self.assertTrue(_is_hidden(guard_str, ATOMS))

    def test_transient_for_hidden_parent_is_hidden(self) -> None:
        """Дочернее диалоговое окно скрытого родителя скрывается."""
        dialog = FakeWindow(transient_for=100)
        self.assertTrue(_is_hidden(dialog, ATOMS, hidden_clients={100}))
        self.assertTrue(_is_hidden(dialog, ATOMS, hidden_frames={100}))

    def test_transient_for_unmapped_parent_is_hidden(self) -> None:
        """Вспомогательное окно с неотображённым родителем скрывается."""
        unmapped_parent = FakeWindow(id=50, map_state=0)
        conn = FakeConnection({50: unmapped_parent})
        child = FakeWindow(transient_for=50)
        self.assertTrue(_is_hidden(child, ATOMS, connection=conn))


if __name__ == "__main__":
    unittest.main()
