"""Проверки поиска объекта интерфейса под курсором."""

from __future__ import annotations

import unittest

from PySide6.QtCore import QPoint, QRect

from capture.windows import ICONIC_STATE, InterfaceObject, _Atoms, _is_hidden, object_at


def make(x: int, y: int, width: int, height: int, depth: int = 0) -> InterfaceObject:
    """Объект интерфейса с заданными границами."""
    return InterfaceObject(QRect(x, y, width, height), depth)


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

    def __init__(self, value: list[int]) -> None:
        self.value = value


class FakeWindow:
    """Окно X11 с заданными свойствами и потомками."""

    def __init__(
        self,
        wm_state: int | None = None,
        net_state: list[int] | None = None,
        desktop: int | None = None,
        children: list["FakeWindow"] | None = None,
    ) -> None:
        self._wm_state = wm_state
        self._net_state = net_state
        self._desktop = desktop
        self._children = children or []

    def get_property(
        self, kind: int, _type: int, _offset: int, _length: int
    ) -> FakeProperty | None:
        """Значение свойства состояния по соглашению ICCCM."""
        if kind == ATOMS.wm_state and self._wm_state is not None:
            return FakeProperty([self._wm_state, 0])
        return None

    def get_full_property(self, kind: int, _type: int) -> FakeProperty | None:
        """Значение перечня состояний по соглашению EWMH."""
        if kind == ATOMS.net_state and self._net_state is not None:
            return FakeProperty(self._net_state)
        if kind == ATOMS_WITH_DESKTOP.net_desktop and self._desktop is not None:
            return FakeProperty([self._desktop])
        return None

    def query_tree(self) -> object:
        """Потомки окна."""
        return type("Tree", (), {"children": self._children})()


ATOMS = _Atoms(wm_state=1, net_state=2, hidden=3, any_property=0)
ATOMS_WITH_DESKTOP = _Atoms(
    wm_state=1, net_state=2, hidden=3, any_property=0, net_desktop=4
)


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


if __name__ == "__main__":
    unittest.main()
