#!/usr/bin/env python3
"""
Эмуляция мыши и клавиатуры для живой проверки приложения в Wayland.

Инструмент разработчика, в приложение не входит. Создаёт виртуальные
устройства ввода через /dev/uinput: указатель с абсолютными координатами
и клавиатуру. Требуется модуль evdev (пакет python3-evdev) и право записи
в /dev/uinput. Запускается системным интерпретатором python3.

Команды выполняются по порядку:
    move X Y          перемещение указателя
    click X Y         щелчок левой кнопкой
    rclick X Y        щелчок правой кнопкой
    dclick X Y        двойной щелчок
    drag X1 Y1 X2 Y2  протягивание с нажатой левой кнопкой
    scroll N          прокрутка колеса: положительное N - от себя
    key CTRL+SHIFT+S  нажатие сочетания
    type 1200         набор цифр и латинских букв (зависит от раскладки)
    wait 0.5          пауза в секундах

Системные сочетания (Ctrl+Alt+F1..F12, Ctrl+Alt+Backspace/Delete, SysRq)
запрещены: первые переключают виртуальный терминал и уводят сеанс на
пустой экран, вторые завершают сеанс. После работы проверяется, что сеанс
остался активным; иначе выполняется попытка вернуть его.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

from evdev import AbsInfo, UInput, ecodes

# Размер системы координат команд. Абсолютные координаты устройства
# композитор растягивает на весь рабочий стол, поэтому координаты можно
# брать прямо со снимка экрана, указав его размер в переменной окружения
# EMULATE_SIZE (например 4320x1350).
WIDTH, HEIGHT = (int(value) for value in os.environ.get("EMULATE_SIZE", "1920x1080").split("x"))
AXIS_MAX = 65535

_ALIASES = {"CTRL": "LEFTCTRL", "SHIFT": "LEFTSHIFT", "ALT": "LEFTALT", "SUPER": "LEFTMETA"}


def forbidden(combo: str) -> bool:
    """Признак системного сочетания, нажимать которое нельзя."""
    parts = {part.upper() for part in combo.split("+")}
    ctrl_alt = {"CTRL", "ALT"} <= parts
    function_key = any(part.startswith("F") and part[1:].isdigit() for part in parts)
    dangerous = function_key or "BACKSPACE" in parts or "DELETE" in parts
    return (ctrl_alt and dangerous) or "SYSRQ" in parts


def main(arguments: list[str]) -> int:
    """Разбор и выполнение команд."""
    # Проверка выполняется до создания устройств: запрещённое сочетание
    # не должно быть нажато даже частично.
    for index, word in enumerate(arguments):
        if word == "key" and index + 1 < len(arguments) and forbidden(arguments[index + 1]):
            print("ЗАПРЕЩЕНО: системное сочетание " + arguments[index + 1], file=sys.stderr)
            return 2

    pointer = UInput(
        {
            ecodes.EV_KEY: [ecodes.BTN_LEFT, ecodes.BTN_RIGHT, ecodes.BTN_MIDDLE],
            ecodes.EV_REL: [ecodes.REL_WHEEL],
            # Заглушки типов evdev не описывают пары (ось, параметры).
            ecodes.EV_ABS: [
                (ecodes.ABS_X, AbsInfo(0, 0, AXIS_MAX, 0, 0, 0)),  # type: ignore[list-item]
                (ecodes.ABS_Y, AbsInfo(0, 0, AXIS_MAX, 0, 0, 0)),  # type: ignore[list-item]
            ],
        },
        name="linscreen-test-pointer",
    )
    keys = [code for name, code in ecodes.ecodes.items() if name.startswith("KEY_") and code < 256]
    keyboard = UInput({ecodes.EV_KEY: keys}, name="linscreen-test-keyboard")
    # Композитору нужно время, чтобы подключить новые устройства.
    time.sleep(1.0)

    def move(x: float, y: float) -> None:
        pointer.write(ecodes.EV_ABS, ecodes.ABS_X, round(x * AXIS_MAX / (WIDTH - 1)))
        pointer.write(ecodes.EV_ABS, ecodes.ABS_Y, round(y * AXIS_MAX / (HEIGHT - 1)))
        pointer.syn()
        time.sleep(0.01)

    def button(code: int, value: int) -> None:
        pointer.write(ecodes.EV_KEY, code, value)
        pointer.syn()
        time.sleep(0.05)

    def drag(points: list[tuple[float, float]]) -> None:
        move(*points[0])
        time.sleep(0.1)
        button(ecodes.BTN_LEFT, 1)
        for start, end in zip(points, points[1:]):
            for step in range(1, 11):
                move(
                    start[0] + (end[0] - start[0]) * step / 10,
                    start[1] + (end[1] - start[1]) * step / 10,
                )
        time.sleep(0.1)
        button(ecodes.BTN_LEFT, 0)

    def press(combo: str) -> None:
        codes = [
            getattr(ecodes, "KEY_" + _ALIASES.get(part.upper(), part.upper()))
            for part in combo.split("+")
        ]
        for code in codes:
            keyboard.write(ecodes.EV_KEY, code, 1)
            keyboard.syn()
            time.sleep(0.02)
        for code in reversed(codes):
            keyboard.write(ecodes.EV_KEY, code, 0)
            keyboard.syn()
            time.sleep(0.02)
        time.sleep(0.05)

    try:
        index = 0
        while index < len(arguments):
            command = arguments[index]
            index += 1
            if command == "move":
                move(float(arguments[index]), float(arguments[index + 1]))
                index += 2
            elif command in ("click", "rclick", "dclick"):
                move(float(arguments[index]), float(arguments[index + 1]))
                index += 2
                time.sleep(0.1)
                code = ecodes.BTN_RIGHT if command == "rclick" else ecodes.BTN_LEFT
                for _ in range(2 if command == "dclick" else 1):
                    button(code, 1)
                    button(code, 0)
            elif command == "drag":
                x1, y1, x2, y2 = (float(value) for value in arguments[index:index + 4])
                index += 4
                drag([(x1, y1), (x2, y2)])
            elif command == "scroll":
                clicks = int(arguments[index])
                index += 1
                for _ in range(abs(clicks)):
                    pointer.write(ecodes.EV_REL, ecodes.REL_WHEEL, 1 if clicks > 0 else -1)
                    pointer.syn()
                    time.sleep(0.08)
            elif command == "key":
                press(arguments[index])
                index += 1
            elif command == "type":
                for char in arguments[index]:
                    press("SHIFT+" + char if char.isupper() else {" ": "SPACE"}.get(char, char))
                index += 1
            elif command == "wait":
                time.sleep(float(arguments[index]))
                index += 1
            else:
                print("Неизвестная команда: " + command, file=sys.stderr)
                return 2
    finally:
        time.sleep(0.2)
        pointer.close()
        keyboard.close()
        _ensure_session_active()
    return 0


def _ensure_session_active() -> None:
    """Проверка, что графический сеанс остался активным, с попыткой возврата."""
    session = os.environ.get("XDG_SESSION_ID", "")
    if not session:
        return
    state = subprocess.run(
        ["loginctl", "show-session", session, "-p", "Active", "--value"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    if state != "yes":
        print("ВНИМАНИЕ: сеанс неактивен, выполняется возврат", file=sys.stderr)
        subprocess.run(["loginctl", "activate", session], check=False)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
