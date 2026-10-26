"""Physical-key automation via the Wayland seat's XKB map.

Named keys, chords and timed sequences retain evdev identity. wtype remains
the Unicode text fallback when a character has no key in the active layout.
"""

from contextlib import contextmanager
import math
import subprocess
import time

from .utils.virtual_keyboard import VirtualKeyboard


class Keyboard:
    def __init__(self):
        self._native = None
        self.modifier_map = {
            "ctrl": "Control_L",
            "control": "Control_L",
            "alt": "Alt_L",
            "shift": "Shift_L",
            "super": "Super_L",
            "win": "Super_L",
            "logo": "Super_L",
            "altgr": "ISO_Level3_Shift",
            "iso_level3_shift": "ISO_Level3_Shift",
        }
        self.key_map = {
            "enter": "Return",
            "return": "Return",
            "backspace": "BackSpace",
            "tab": "Tab",
            "space": "space",
            "esc": "Escape",
            "escape": "Escape",
            "delete": "Delete",
            "left": "Left",
            "right": "Right",
            "up": "Up",
            "down": "Down",
        }

    def _key(self, key):
        key = str(key)
        if not key or "\x00" in key:
            raise ValueError("A key name must be nonempty and contain no NUL")
        return self.modifier_map.get(key.lower(), self.key_map.get(key.lower(), key))

    def _backend(self):
        if self._native is None:
            self._native = VirtualKeyboard()
        return self._native

    def _finish(self):
        # Snapshot the current seat/layout again for the next independent
        # operation, but keep one keyboard throughout an explicit held chord.
        if self._native is not None and not self._native.held:
            self.close()

    @contextmanager
    def _operation(self):
        native = self._backend()
        existing = set(native.held)
        try:
            yield native
            for name in reversed(list(native.held)):
                if name not in existing:
                    native.up(name)
        except BaseException:
            self.close()
            raise
        finally:
            self._finish()

    def close(self):
        """Release this object's held keys and close only its own connection."""
        native, self._native = self._native, None
        if native is not None:
            native.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def typewrite(self, text: str, interval: float = 0):
        """Type through physical keys where possible; preserve Unicode fallback."""
        if not math.isfinite(interval) or interval < 0:
            raise ValueError("interval must be finite and nonnegative")
        if not isinstance(text, str) or "\x00" in text:
            raise ValueError("Text must be a string without NUL")
        if not text:
            return
        with self._operation() as native:
            # Plan before sending anything, so fallback cannot duplicate a
            # partially typed prefix. Never fallback for connection/keymap errors.
            plans = [native.keymap.character(char) for char in text]
            if any(plan is None for plan in plans):
                if native.held:
                    raise ValueError(
                        "Unicode text fallback cannot run while keys are held"
                    )
                self.close()  # wtype must not inherit our temporary seat map.
                delay = round(interval * 1000)
                subprocess.run(
                    ["wtype", *(["-d", str(delay)] if delay else []), "-"],
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=max(30, len(text) * interval + 10),
                    input=text,
                )
                return
            begin = time.monotonic()
            for index, plan in enumerate(plans):
                time.sleep(max(0, begin + index * interval - time.monotonic()))
                native.down("__text__", plan)
                native.up("__text__")

    def press(self, key: str):
        """Press and release a key without releasing a caller-owned hold."""
        name = self._key(key)
        with self._operation() as native:
            native.down(name)

    def keyDown(self, key: str):
        """Hold a key on this Keyboard until keyUp() or close()."""
        name = self._key(key)
        try:
            self._backend().down(name)
        except BaseException:
            self.close()
            raise

    def keyUp(self, key: str):
        """Release a key held by this Keyboard; unmatched releases are no-ops."""
        name = self._key(key)
        if self._native is not None:
            try:
                self._native.up(name)
            except BaseException:
                self.close()
                raise
            finally:
                self._finish()

    def sequence(self, events):
        """Execute ordered absolute-millisecond down/up events on one keyboard."""
        planned, previous = [], 0
        for timestamp, state, key in events:
            if (
                type(timestamp) is not int
                or timestamp < previous
                or state not in ("down", "up")
            ):
                raise ValueError("Events need ordered integer times and down/up states")
            previous = timestamp
            planned.append((timestamp, state, self._key(key)))
        if not planned:
            return
        with self._operation() as native:
            # Reject an unavailable key before injecting any part of the macro.
            for _, _, name in planned:
                native.keymap.resolve(name)
            begin = time.monotonic()
            for timestamp, state, name in planned:
                time.sleep(max(0, begin + timestamp / 1000 - time.monotonic()))
                (native.down if state == "down" else native.up)(name)

    def hotkey(self, *keys):
        """Hold modifiers, tap ordinary keys, then release owned modifiers."""
        names = [self._key(key) for key in keys]
        if not names:
            return
        modifiers = {
            "Control_L",
            "Control_R",
            "Shift_L",
            "Shift_R",
            "Alt_L",
            "Alt_R",
            "Super_L",
            "Super_R",
            "Meta_L",
            "Meta_R",
            "ISO_Level3_Shift",
            "ISO_Level5_Shift",
        }
        with self._operation() as native:
            existing = set(native.held)
            for name in names:
                native.keymap.resolve(name)
            for name in names:
                if name in modifiers:
                    native.down(name)
            for name in names:
                if name not in modifiers:
                    native.down(name)
                    if name not in existing:
                        native.up(name)
