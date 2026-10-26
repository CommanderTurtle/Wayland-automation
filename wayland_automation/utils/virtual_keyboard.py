"""Keyboard-only Wayland connection using the seat's existing evdev XKB map.

Unlike wtype's private symbol numbering, these codes retain their physical
identity in compositor bindings and input observers. No input-device access,
compositor configuration writes, pointer objects, or compositor-side code is used.
"""

import array
from collections import OrderedDict
import ctypes as C
import ctypes.util
import json
import mmap
import os
import re
import socket
import struct
import subprocess
import sys
import tempfile
import time


class KeyboardError(RuntimeError):
    """The compositor, protocol, or seat keymap is unavailable."""


def desktop_state():
    """Read active group/locks where the compositor exposes them.

    A surface-less wl_keyboard receives a keymap but need not receive modifiers.
    Never steal focus to obtain them. This is one read per keyboard operation,
    not a monitor refresh or an input observer.
    """
    if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
        command, field = ["hyprctl", "-j", "devices"], "keyboards"
    elif os.environ.get("SWAYSOCK"):
        command, field = ["swaymsg", "-r", "-t", "get_inputs"], None
    else:
        return None
    try:
        output = subprocess.run(
            command, capture_output=True, text=True, check=True, timeout=3
        ).stdout
        if len(output) > 2 * 1024 * 1024:
            raise ValueError("Device response is too large")
        # Hyprland 0.56.2 emits bare `none` for inactive virtual layouts.
        # Normalize only unquoted tokens; strings/device names remain untouched.
        output = re.sub(
            r'"(?:\\.|[^"\\])*"|\bnone\b',
            lambda match: "null" if match.group() == "none" else match.group(),
            output,
        )
        value = json.loads(output)
        devices = (
            value[field] if field else [d for d in value if d.get("type") == "keyboard"]
        )
        if field:
            devices = [d for d in devices if d.get("main")]
        if not devices or (field and len(devices) != 1):
            raise ValueError("Active keyboard not reported")
        if not field and len({d.get("xkb_active_layout_index") for d in devices}) != 1:
            raise ValueError("Ambiguous keyboard groups")
        device = devices[0]
        group = (
            device.get("active_layout_index")
            if field
            else device.get("xkb_active_layout_index")
        )
        if type(group) is not int or group < 0:
            raise ValueError("Active keyboard group not reported")
        return {
            "group": group,
            "caps": device.get("capsLock"),
            "num": device.get("numLock"),
        }
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.SubprocessError,
    ) as error:
        raise KeyboardError("Cannot read the active keyboard group/locks") from error


class SeatKeymap:
    """Resolve symbols against the supplied map, never a guessed US layout."""

    def __init__(self, text, modifiers=(0, 0, 0, 0), desktop=None):
        self.context = self.map = self.state = self.probe = None
        try:
            self.lib = C.CDLL(
                ctypes.util.find_library("xkbcommon") or "libxkbcommon.so.0"
            )
            signatures = {
                "context_new": (C.c_void_p, C.c_int),
                "context_unref": (None, C.c_void_p),
                "keymap_new_from_string": (
                    C.c_void_p,
                    C.c_void_p,
                    C.c_char_p,
                    C.c_int,
                    C.c_int,
                ),
                "keymap_unref": (None, C.c_void_p),
                "keymap_min_keycode": (C.c_uint, C.c_void_p),
                "keymap_max_keycode": (C.c_uint, C.c_void_p),
                "keymap_key_get_name": (C.c_char_p, C.c_void_p, C.c_uint),
                "keymap_key_by_name": (C.c_uint, C.c_void_p, C.c_char_p),
                "keymap_num_layouts_for_key": (C.c_uint, C.c_void_p, C.c_uint),
                "keymap_num_levels_for_key": (C.c_uint, C.c_void_p, C.c_uint, C.c_uint),
                "keymap_key_get_syms_by_level": (
                    C.c_int,
                    C.c_void_p,
                    C.c_uint,
                    C.c_uint,
                    C.c_uint,
                    C.POINTER(C.POINTER(C.c_uint)),
                ),
                "keymap_key_get_mods_for_level": (
                    C.c_size_t,
                    C.c_void_p,
                    C.c_uint,
                    C.c_uint,
                    C.c_uint,
                    C.POINTER(C.c_uint),
                    C.c_size_t,
                ),
                "keymap_mod_get_index": (C.c_uint, C.c_void_p, C.c_char_p),
                "keysym_from_name": (C.c_uint, C.c_char_p, C.c_int),
                "utf32_to_keysym": (C.c_uint, C.c_uint),
                "state_new": (C.c_void_p, C.c_void_p),
                "state_unref": (None, C.c_void_p),
                "state_update_key": (C.c_uint, C.c_void_p, C.c_uint, C.c_int),
                "state_update_mask": (C.c_uint, C.c_void_p, *([C.c_uint] * 6)),
                "state_serialize_mods": (C.c_uint, C.c_void_p, C.c_uint),
                "state_serialize_layout": (C.c_uint, C.c_void_p, C.c_uint),
                "state_key_get_one_sym": (C.c_uint, C.c_void_p, C.c_uint),
                "state_key_get_layout": (C.c_uint, C.c_void_p, C.c_uint),
            }
            for name, signature in signatures.items():
                function = getattr(self.lib, "xkb_" + name)
                function.restype, function.argtypes = signature[0], list(signature[1:])
                setattr(self, name, function)
            self.context = self.context_new(0)
            if not self.context:
                raise KeyboardError("Cannot create XKB context")
            self.map = self.keymap_new_from_string(self.context, text, 1, 0)
            if not self.map:
                raise KeyboardError("Cannot compile the seat's XKB keymap")
            # A symbol-only virtual map (e.g. wtype's <K1>) is not a physical
            # keyboard map. Fail explicitly instead of turning symbols into Esc.
            if self.keymap_key_get_name(self.map, 9) != b"ESC":
                raise KeyboardError(
                    "Seat has a private keymap; retry after the other virtual keyboard exits"
                )
            self.state, self.probe = self.state_new(self.map), self.state_new(self.map)
            if not self.state or not self.probe:
                raise KeyboardError("Cannot create XKB keyboard state")
            _, latched, locked, group = modifiers
            if desktop is not None:
                group = desktop["group"]
                for name, enabled in (
                    (b"Lock", desktop["caps"]),
                    (b"Mod2", desktop["num"]),
                ):
                    index = self.keymap_mod_get_index(self.map, name)
                    if index < 32 and enabled is not None:
                        locked = (
                            (locked | (1 << index))
                            if enabled
                            else (locked & ~(1 << index))
                        )
            self.state_update_mask(self.state, 0, latched, locked, 0, 0, group)
            self.symbols = {}
            for code in range(
                max(8, self.keymap_min_keycode(self.map)),
                min(775, self.keymap_max_keycode(self.map)) + 1,
            ):
                layouts = self.keymap_num_layouts_for_key(self.map, code)
                if not layouts:
                    continue
                layout = self.state_key_get_layout(self.state, code)
                for level in range(
                    self.keymap_num_levels_for_key(self.map, code, layout)
                ):
                    syms = C.POINTER(C.c_uint)()
                    count = self.keymap_key_get_syms_by_level(
                        self.map, code, layout, level, C.byref(syms)
                    )
                    masks = (C.c_uint * 64)()
                    length = self.keymap_key_get_mods_for_level(
                        self.map, code, layout, level, masks, len(masks)
                    )
                    for index in range(count):
                        for mask in masks[:length]:
                            self.symbols.setdefault(syms[index], []).append(
                                (code - 8, mask)
                            )
            self.modifiers = {}
            for mod, symbol in (
                ("Shift", "Shift_L"),
                ("Control", "Control_L"),
                ("Mod1", "Alt_L"),
                ("Mod4", "Super_L"),
                ("Mod5", "ISO_Level3_Shift"),
            ):
                index = self.keymap_mod_get_index(self.map, mod.encode())
                choices = self.symbols.get(
                    self.keysym_from_name(symbol.encode(), 0), []
                )
                if index < 32 and choices:
                    self.modifiers[1 << index] = min(
                        choices, key=lambda item: (item[1] != 0, item[0])
                    )[0]
        except BaseException:
            self.close()
            raise

    def _plan(self, symbol, literal=False):
        depressed, latched, locked, group = self.snapshot()
        shift = self.keymap_mod_get_index(self.map, b"Shift")
        shift = 1 << shift if shift < 32 else 0
        candidates = []
        for code, mask in self.symbols.get(symbol, []):
            for needed in (mask, mask ^ shift) if literal else (mask,):
                remaining = needed & ~(depressed | latched | locked)
                if remaining & ~sum(self.modifiers):
                    continue  # Never toggle Caps/NumLock to manufacture a symbol.
                remaining &= sum(self.modifiers)
                if literal:
                    self.state_update_mask(
                        self.probe, depressed | remaining, latched, locked, 0, 0, group
                    )
                    if self.state_key_get_one_sym(self.probe, code + 8) != symbol:
                        continue
                mods = tuple(
                    value for bit, value in self.modifiers.items() if remaining & bit
                )
                candidates.append((code, mods))
        if not candidates:
            return None
        return min(candidates, key=lambda item: (len(item[1]), item[0]))

    def resolve(self, name):
        # These names identify physical controls, including right-hand modifiers
        # and keypad keys even when NumLock is off or a modifier is remapped.
        controls = {
            "control_l": "LCTL",
            "control_r": "RCTL",
            "shift_l": "LFSH",
            "shift_r": "RTSH",
            "alt_l": "LALT",
            "alt_r": "RALT",
            "iso_level3_shift": "RALT",
            "super_l": "LWIN",
            "super_r": "RWIN",
            "kp_enter": "KPEN",
            "kp_decimal": "KPDL",
            "kp_divide": "KPDV",
            "kp_multiply": "KPMU",
            "kp_subtract": "KPSU",
            "kp_add": "KPAD",
        }
        controls.update(
            ("kp_" + str(number), "KP" + str(number)) for number in range(10)
        )
        physical = controls.get(name.lower())
        if physical:
            code = self.keymap_key_by_name(self.map, physical.encode())
            if 8 <= code <= 775:
                return code - 8, ()
        # Exact first: case-insensitive XKB lookup maps both "a" and "A" to
        # lowercase. Only use it to recover noncanonical names such as "print".
        symbol = self.keysym_from_name(name.encode(), 0) or self.keysym_from_name(
            name.encode(), 1
        )
        plan = self._plan(symbol)
        if not symbol or plan is None:
            raise ValueError("Key is not available in the active seat layout: " + name)
        return plan

    def character(self, char):
        symbol = self.utf32_to_keysym(ord(char))
        # Text control characters are key operations, not Unicode glyph names.
        if char in "\n\r\t\b":
            symbol = self.keysym_from_name(
                {"\n": b"Return", "\r": b"Return", "\t": b"Tab", "\b": b"BackSpace"}[
                    char
                ],
                0,
            )
        return self._plan(symbol, literal=True)

    def update(self, code, down):
        self.state_update_key(self.state, code + 8, int(down))

    def snapshot(self):
        return tuple(
            self.state_serialize_mods(self.state, flag) for flag in (1, 2, 4)
        ) + (self.state_serialize_layout(self.state, 128),)

    def close(self):
        for field, function in (
            ("probe", "state_unref"),
            ("state", "state_unref"),
            ("map", "keymap_unref"),
            ("context", "context_unref"),
        ):
            value = getattr(self, field, None)
            if value:
                getattr(self, function)(value)
                setattr(self, field, None)


class VirtualKeyboard:
    """A bounded, independent virtual-keyboard connection; never opens a pointer."""

    MAX_KEYMAP = 16 * 1024 * 1024

    def __init__(self):
        self.sock = self.keymap = None
        self.pending_fds = []
        self.objects = {1: "display"}
        self.next_id = 2
        self.globals = {}
        self.text = None
        self.initial_modifiers = (0, 0, 0, 0)
        self.keyboard = None
        self.held = OrderedDict()
        self.counts = {}
        self.endian = "<" if sys.byteorder == "little" else ">"
        try:
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(5)
            display = os.environ.get("WAYLAND_DISPLAY", "wayland-0")
            if not os.path.isabs(display):
                runtime = os.environ.get("XDG_RUNTIME_DIR")
                if not runtime:
                    raise KeyboardError("XDG_RUNTIME_DIR is not set")
                display = os.path.join(runtime, display)
            self.sock.connect(display)
            self.registry = self.allocate("registry")
            self.send(1, 1, self.pack(self.registry))
            self.roundtrip()
            seat = self.bind("wl_seat")
            manager = self.bind("zwp_virtual_keyboard_manager_v1")
            physical = self.allocate("physical")
            self.send(seat, 1, self.pack(physical))  # wl_seat.get_keyboard
            self.roundtrip()
            if self.text is None:
                raise KeyboardError("The Wayland seat did not supply an XKB keymap")
            self.keymap = SeatKeymap(self.text, self.initial_modifiers, desktop_state())
            self.keyboard = self.allocate("virtual")
            self.send(manager, 0, self.pack(seat, self.keyboard))
            with tempfile.TemporaryFile() as file:
                file.write(self.text)
                file.flush()
                self.send(self.keyboard, 0, self.pack(1, len(self.text)), file.fileno())
            self.send_modifiers()
            self.roundtrip()  # Protocol/keymap errors must precede any keypress.
        except BaseException:
            self.close()
            raise

    def pack(self, *values):
        return struct.pack(self.endian + "I" * len(values), *values)

    def unpack(self, data):
        return struct.unpack(self.endian + "I" * (len(data) // 4), data)

    def allocate(self, kind):
        value = self.next_id
        self.next_id += 1
        self.objects[value] = kind
        return value

    def send(self, obj, opcode, payload=b"", fd=None):
        message = self.pack(obj, ((len(payload) + 8) << 16) | opcode) + payload
        if fd is None:
            self.sock.sendall(message)
        else:
            count = self.sock.sendmsg(
                [message],
                [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [fd]))],
            )
            if count <= 0:
                raise KeyboardError("Wayland keyboard connection closed")
            self.sock.sendall(message[count:])

    def receive(self, size, deadline):
        result = bytearray()
        while len(result) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise KeyboardError("Wayland keyboard response timed out")
            self.sock.settimeout(remaining)
            flags = getattr(socket, "MSG_CMSG_CLOEXEC", 0)
            data, controls, status, _ = self.sock.recvmsg(
                size - len(result),
                socket.CMSG_SPACE(16 * array.array("i").itemsize),
                flags,
            )
            for level, kind, content in controls:
                if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                    fds = array.array("i")
                    fds.frombytes(content[: len(content) - len(content) % fds.itemsize])
                    self.pending_fds.extend(fds)
                    for fd in fds:
                        os.set_inheritable(fd, False)
            if status & socket.MSG_CTRUNC:
                raise KeyboardError("Truncated Wayland keyboard descriptor message")
            if len(self.pending_fds) > 16:
                raise KeyboardError("Unexpected Wayland keyboard descriptors")
            if not data:
                raise KeyboardError("Wayland keyboard connection closed")
            result.extend(data)
        return bytes(result)

    def roundtrip(self):
        callback = self.allocate("callback")
        self.send(1, 0, self.pack(callback))
        deadline = time.monotonic() + 5
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise KeyboardError("Wayland keyboard setup timed out")
            self.sock.settimeout(remaining)
            obj, header = self.unpack(self.receive(8, deadline))
            size, opcode = header >> 16, header & 0xFFFF
            if size < 8 or size % 4:
                raise KeyboardError("Invalid Wayland keyboard message")
            data = self.receive(size - 8, deadline)
            kind = self.objects.get(obj)
            if kind == "display" and opcode == 0:
                raise KeyboardError("Compositor rejected a virtual-keyboard request")
            if kind == "display" and opcode == 1:
                self.objects.pop(self.unpack(data)[0], None)
            elif kind == "registry" and opcode == 0:
                name, length = self.unpack(data[:8])
                if not length or length > len(data) - 12:
                    raise KeyboardError("Invalid Wayland registry string")
                interface = data[8 : 8 + length - 1].decode("ascii")
                self.globals.setdefault(interface, name)
            elif kind == "physical" and opcode == 0:
                fmt, size = self.unpack(data)
                if not self.pending_fds:
                    raise KeyboardError("Seat keymap descriptor is missing")
                fd = self.pending_fds.pop(0)
                try:
                    if fmt != 1 or not 1 <= size <= self.MAX_KEYMAP:
                        raise KeyboardError("Unsupported seat XKB keymap")
                    # The sender's descriptor offset is unspecified. mmap,
                    # unlike read(), does not depend on or move that offset.
                    with mmap.mmap(fd, size, access=mmap.ACCESS_READ) as mapping:
                        text = mapping[:]
                finally:
                    os.close(fd)
                if len(text) != size or text[-1:] != b"\0":
                    raise KeyboardError("Incomplete seat XKB keymap")
                # Keep the pre-injection snapshot. Later keymap notifications
                # caused by our own virtual keyboard must not reset held keys.
                if self.text is None:
                    self.text = text
            elif kind == "physical" and opcode == 4 and self.keyboard is None:
                self.initial_modifiers = self.unpack(data)[1:]
            elif obj == callback and opcode == 0:
                self.sock.settimeout(5)
                return

    def bind(self, interface):
        if interface not in self.globals:
            raise KeyboardError("Required Wayland protocol unavailable: " + interface)
        obj = self.allocate(interface)
        text = interface.encode() + b"\0"
        payload = (
            self.pack(self.globals[interface], len(text))
            + text
            + b"\0" * (-len(text) % 4)
        )
        self.send(self.registry, 0, payload + self.pack(1, obj))
        return obj

    def send_modifiers(self):
        self.send(self.keyboard, 2, self.pack(*self.keymap.snapshot()))

    def _hold(self, code):
        count = self.counts.get(code, 0)
        self.counts[code] = count + 1
        if not count:
            self.send(
                self.keyboard,
                1,
                self.pack(int(time.monotonic() * 1000) & 0xFFFFFFFF, code, 1),
            )
            self.keymap.update(code, True)
            self.send_modifiers()

    def _release(self, code):
        count = self.counts.get(code, 0)
        if count <= 1:
            if count:
                self.send(
                    self.keyboard,
                    1,
                    self.pack(int(time.monotonic() * 1000) & 0xFFFFFFFF, code, 0),
                )
                self.keymap.update(code, False)
                self.send_modifiers()
            self.counts.pop(code, None)
        else:
            self.counts[code] = count - 1

    def down(self, name, plan=None):
        if name in self.held:
            return
        plan = self.keymap.resolve(name) if plan is None else plan
        code, modifiers = plan
        # Record before writing so close() can balance partially sent chords.
        self.held[name] = plan
        for mod in modifiers:
            self._hold(mod)
        self._hold(code)
        self.roundtrip()

    def up(self, name):
        plan = self.held.get(name)
        if plan is not None:
            code, modifiers = plan
            self._release(code)
            for mod in reversed(modifiers):
                self._release(mod)
            self.held.pop(name, None)
            self.roundtrip()

    def close(self):
        try:
            if self.sock is not None:
                try:
                    for name in reversed(list(self.held)):
                        self.up(name)
                    if self.keyboard is not None:
                        self.send(self.keyboard, 3)  # destroy only our keyboard
                        self.roundtrip()
                except (OSError, KeyboardError):
                    pass  # A broken connection cannot be used to release keys.
                finally:
                    self.sock.close()
                    self.sock = None
                    self.held.clear()
                    self.counts.clear()
        finally:
            for fd in self.pending_fds:
                os.close(fd)
            self.pending_fds.clear()
            if self.keymap is not None:
                self.keymap.close()
                self.keymap = None
