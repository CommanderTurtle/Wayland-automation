"""
keyboard_controller.py — Keyboard automation for Wayland via ``wtype``.

Provides the ``Keyboard`` class for typing text, pressing keys, and
triggering hotkey combinations on any Wayland compositor that supports wtype.

Usage::

    from wayland_automation import Keyboard

    kb = Keyboard()
    kb.typewrite("Hello Wayland!", interval=0.05)
    kb.press("enter")
    kb.hotkey("ctrl", "s")

Requires ``wtype`` to be installed (see README for install instructions).
"""

import subprocess
import time
import math

class Keyboard:
    """
    A simple keyboard automation module for Wayland using `wtype`.

    Methods:
    - typewrite(text, interval=0): Types text with an optional delay between characters.
    - press(key): Presses and releases a key.
    - keyDown(key): Simulates pressing a key down.
    - keyUp(key): Simulates releasing a key.
    - hotkey(*keys): Simulates a key combination (e.g., hotkey('ctrl', 's')).
    """

    def __init__(self):
        # Mapping common modifier names to what wtype expects.
        self.modifier_map = {
            "ctrl": "ctrl",
            "control": "ctrl",
            "alt": "alt",
            "shift": "shift",
            "super": "super"
        }
        # A mapping for some common keys to their special characters.
        self.key_map = {
            "enter": "Return", "return": "Return",
            "backspace": "BackSpace", "tab": "Tab", "space": "space",
            "esc": "Escape", "escape": "Escape", "delete": "Delete",
            "left": "Left", "right": "Right", "up": "Up", "down": "Down"
        }

    def _key(self, key):
        key = str(key)
        if not key or "\x00" in key:
            raise ValueError("A key name must be nonempty and contain no NUL")
        return self.modifier_map.get(key.lower(), self.key_map.get(key.lower(), key))

    def _run(self, args, timeout=30):
        result = subprocess.run(["wtype", *args], capture_output=True,
                                text=True, timeout=timeout, check=True)
        return result

    def typewrite(self, text: str, interval: float = 0):
        """Types the given text with an optional interval between characters."""
        if not math.isfinite(interval) or interval < 0:
            raise ValueError("interval must be finite and nonnegative")
        if text:
            self._run(["-d", str(round(interval * 1000)), "--", text],
                      timeout=max(30, len(text) * interval + 10))

    def press(self, key: str):
        """Presses and releases a single key."""
        self._run(["-k", self._key(key)])

    def keyDown(self, key: str):
        """Press a key in one wtype invocation.

        wtype releases keys when its process exits. For holds spanning several
        events use sequence(), rather than separate keyDown()/keyUp() calls.
        """
        flag = "-M" if key.lower() in self.modifier_map else "-P"
        self._run([flag, self._key(key)])

    def keyUp(self, key: str):
        """Simulates releasing a held key."""
        flag = "-m" if key.lower() in self.modifier_map else "-p"
        self._run([flag, self._key(key)])

    def sequence(self, events):
        """Run (absolute milliseconds, 'down'|'up', key) events in one keyboard.

        Times must be nonnegative and ordered. Any keys still held at the end
        are released. Errors from wtype propagate instead of looking successful.
        """
        args, held, previous = [], [], 0
        for timestamp, state, key in events:
            if (not isinstance(timestamp, int) or timestamp < previous
                    or state not in ("down", "up")):
                raise ValueError("Events need ordered integer times and down/up states")
            if timestamp > previous:
                args.extend(["-s", str(timestamp - previous)])
            previous = timestamp
            name = self._key(key)
            modifier = str(key).lower() in self.modifier_map
            args.extend([("-M" if modifier else "-P") if state == "down"
                         else ("-m" if modifier else "-p"), name])
            entry = (name, modifier)
            if state == "down" and entry not in held:
                held.append(entry)
            elif state == "up" and entry in held:
                held.remove(entry)
        for name, modifier in reversed(held):
            args.extend(["-m" if modifier else "-p", name])
        if args:
            self._run(args, timeout=max(30, previous / 1000 + 10))

    def hotkey(self, *keys):
        """
        Simulates a key combination by constructing a single wtype command that:
          - Presses all modifier keys down
          - Presses non-modifier key(s)
          - Releases the modifier keys in reverse order.
        Example:
            hotkey('ctrl', 'b') will simulate Ctrl+B.
        """
        modifiers = []
        non_modifiers = []

        for key in keys:
            if key.lower() in self.modifier_map:
                modifiers.append(self.modifier_map[key.lower()])
            else:
                non_modifiers.append(self._key(key))

        # Build the command list in one go.
        cmd = ["wtype"]
        # Press modifiers down
        for mod in modifiers:
            cmd.extend(["-M", mod])
        # Press non-modifiers
        for key in non_modifiers:
            cmd.extend(["-k", key])
        # Release modifiers (in reverse order)
        for mod in reversed(modifiers):
            cmd.extend(["-m", mod])

        if keys:
            self._run(cmd[1:])

# Example Usage
if __name__ == "__main__":
    kb = Keyboard()
    time.sleep(2)  # Gives time to switch to another window
    kb.typewrite("Hello, Wayland!", interval=0.05)
    kb.press("enter")
    kb.hotkey("ctrl", "a")
