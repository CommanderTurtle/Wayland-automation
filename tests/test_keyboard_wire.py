"""Protocol fixtures run without a compositor; real XKB checks need libxkbcommon."""

import array
import ctypes.util
import os
import socket
import struct
import tempfile
import unittest
from unittest.mock import Mock, patch

from wayland_automation.utils.virtual_keyboard import (
    KeyboardError,
    SeatKeymap,
    VirtualKeyboard,
    desktop_state,
)


def packet(obj, opcode, *values):
    payload = struct.pack("=" + "I" * len(values), *values)
    return struct.pack("=II", obj, ((8 + len(payload)) << 16) | opcode) + payload


class WireSocket:
    def __init__(self, incoming, fd):
        self.incoming = incoming
        self.fd = fd
        self.sent = []
        self.upload = None
        self.closed = False

    def settimeout(self, value):
        pass

    def connect(self, address):
        self.address = address

    def sendall(self, data):
        if data:
            self.sent.append(data)

    def sendmsg(self, buffers, controls):
        data = b"".join(buffers)
        fd = controls[0][2][0]
        os.lseek(fd, 0, os.SEEK_SET)
        self.upload = os.read(fd, 4096)
        self.sent.append(data)
        return len(data)

    def recvmsg(self, size, space, flags):
        # Fragment every event, with a descriptor arriving before its message,
        # as permitted when a server sends several events in one sendmsg().
        data, self.incoming = (
            self.incoming[: min(size, 3)],
            self.incoming[min(size, 3) :],
        )
        controls = []
        if self.fd is not None:
            controls = [
                (
                    socket.SOL_SOCKET,
                    socket.SCM_RIGHTS,
                    array.array("i", [self.fd]).tobytes(),
                )
            ]
            self.fd = None
        return data, controls, 0, None

    def close(self):
        self.closed = True


class PhysicalMap:
    def __init__(self, *args):
        self.modifiers = (0, 0, 16, 0)

    def snapshot(self):
        return self.modifiers

    def resolve(self, name):
        return {
            "Print": 99,
            "a": 30,
            "q": 16,
            "F13": 183,
            "Escape": 1,
            "Control_L": 29,
            "Control_R": 97,
            "Shift_L": 42,
            "Shift_R": 54,
        }[name], ()

    def update(self, code, down):
        self.modifiers = (4 if code == 29 and down else 0, 0, 16, 0)

    def close(self):
        pass


class WireTests(unittest.TestCase):
    def setup_keyboard(self):
        text = b"the supplied seat map\0"
        file = tempfile.TemporaryFile()
        self.addCleanup(file.close)
        file.write(text)
        file.flush()
        fd = os.dup(file.fileno())

        def global_event(name, interface):
            value = interface.encode() + b"\0"
            payload = (
                struct.pack("=II", name, len(value))
                + value
                + b"\0" * (-len(value) % 4)
                + struct.pack("=I", 1)
            )
            return struct.pack("=II", 2, (8 + len(payload)) << 16) + payload

        incoming = (
            global_event(11, "wl_seat")
            + global_event(12, "zwp_virtual_keyboard_manager_v1")
            + global_event(13, "zwlr_virtual_pointer_manager_v1")
            + packet(3, 0, 0)
            + packet(1, 1, 3)
            + packet(6, 0, 1, len(text))
            + packet(7, 0, 0)
            + b"".join(packet(callback, 0, 0) for callback in range(9, 80))
        )
        wire = WireSocket(incoming, fd)
        self.addCleanup(lambda: os.close(wire.fd) if wire.fd is not None else None)
        for name, value in (
            ("AF_UNIX", 1),
            ("SCM_RIGHTS", 1),
            ("MSG_CTRUNC", 8),
            ("CMSG_SPACE", lambda size: size + 16),
        ):
            override = patch.object(
                socket, name, getattr(socket, name, value), create=True
            )
            override.start()
            self.addCleanup(override.stop)
        with (
            patch(
                "wayland_automation.utils.virtual_keyboard.socket.socket",
                return_value=wire,
            ),
            patch("wayland_automation.utils.virtual_keyboard.SeatKeymap", PhysicalMap),
            patch(
                "wayland_automation.utils.virtual_keyboard.desktop_state",
                return_value=None,
            ),
            patch.dict(
                os.environ,
                {"WAYLAND_DISPLAY": "wayland-fixture", "XDG_RUNTIME_DIR": "/fixture"},
            ),
        ):
            keyboard = VirtualKeyboard()
        self.addCleanup(keyboard.close)
        return keyboard, wire, text

    def keys(self, wire, keyboard):
        result = []
        for message in wire.sent:
            obj, header = struct.unpack("=II", message[:8])
            if obj == keyboard.keyboard and header & 0xFFFF == 1:
                _, code, state = struct.unpack("=III", message[8:])
                result.append((code, state))
        return result

    def test_real_wire_preserves_print_letters_function_and_escape_identity(self):
        keyboard, wire, text = self.setup_keyboard()
        for name in ("Print", "a", "q", "F13", "Escape"):
            keyboard.down(name)
            keyboard.up(name)
        self.assertEqual(
            self.keys(wire, keyboard),
            [edge for code in (99, 30, 16, 183, 1) for edge in ((code, 1), (code, 0))],
        )
        self.assertEqual(wire.upload, text)  # Full supplied map, not <K1> remapping.
        self.assertEqual(keyboard.keyboard, 8)  # IDs 2..8 allocated sequentially.
        # No pointer manager was bound/created despite being advertised.
        bindings = [
            m for m in wire.sent if struct.unpack("=II", m[:8]) == (2, len(m) << 16)
        ]
        self.assertEqual(len(bindings), 2)
        self.assertFalse(any(b"virtual_pointer" in m for m in wire.sent))

    def test_wire_has_real_modifier_keys_and_balanced_close(self):
        keyboard, wire, _ = self.setup_keyboard()
        keyboard.down("Control_L")
        keyboard.down("q")
        keyboard.close()
        self.assertEqual(
            self.keys(wire, keyboard), [(29, 1), (16, 1), (16, 0), (29, 0)]
        )
        self.assertTrue(wire.closed)
        keyboard.close()
        mods = [
            struct.unpack("=IIII", m[8:])
            for m in wire.sent
            if struct.unpack("=II", m[:8])[0] == 8
            and struct.unpack("=II", m[:8])[1] & 0xFFFF == 2
        ]
        self.assertIn((4, 0, 16, 0), mods)
        self.assertEqual(mods[-1], (0, 0, 16, 0))

    def test_shared_implicit_shift_does_not_release_explicit_shift(self):
        keyboard, wire, _ = self.setup_keyboard()
        keyboard.down("capital", (30, (42,)))
        keyboard.down("Shift_L")
        keyboard.up("capital")
        self.assertEqual(self.keys(wire, keyboard), [(42, 1), (30, 1), (30, 0)])
        keyboard.up("Shift_L")
        self.assertEqual(self.keys(wire, keyboard)[-1], (42, 0))

    def test_right_hand_modifiers_are_distinct(self):
        keyboard, wire, _ = self.setup_keyboard()
        for name in ("Control_R", "Shift_R"):
            keyboard.down(name)
            keyboard.up(name)
        self.assertEqual(
            self.keys(wire, keyboard), [(97, 1), (97, 0), (54, 1), (54, 0)]
        )

    def test_broken_connection_cleanup_closes_map_and_pending_descriptors(self):
        keyboard, wire, _ = self.setup_keyboard()
        keyboard.down("Control_L")
        mapping = keyboard.keymap
        mapping.close = Mock()
        with tempfile.TemporaryFile() as file:
            fd = os.dup(file.fileno())
            keyboard.pending_fds.append(fd)
            wire.sendall = Mock(side_effect=BrokenPipeError())
            keyboard.close()
            with self.assertRaises(OSError):
                os.fstat(fd)
        mapping.close.assert_called_once_with()
        self.assertFalse(keyboard.held)
        self.assertTrue(wire.closed)

    def test_invalid_wire_header_and_eof_fail_closed(self):
        keyboard, wire, _ = self.setup_keyboard()
        wire.incoming = struct.pack("=II", 1, 4 << 16)
        with self.assertRaises(KeyboardError):
            keyboard.roundtrip()
        wire.incoming = b""
        with self.assertRaises(KeyboardError):
            keyboard.roundtrip()

    @patch("wayland_automation.utils.virtual_keyboard.subprocess.run")
    def test_active_group_query_accepts_only_bare_none_not_quoted_text(self, run):
        run.return_value.stdout = '{"keyboards":[{"main":false,"active_layout_index":none,"name":"none"},{"main":true,"active_layout_index":1,"capsLock":false,"numLock":true}]}'
        with patch.dict(
            os.environ, {"HYPRLAND_INSTANCE_SIGNATURE": "fixture"}, clear=True
        ):
            self.assertEqual(desktop_state(), {"group": 1, "caps": False, "num": True})
        self.assertEqual(run.call_args.args[0], ["hyprctl", "-j", "devices"])

    @patch("wayland_automation.utils.virtual_keyboard.subprocess.run")
    def test_missing_active_layout_never_defaults_to_us_or_zero(self, run):
        run.return_value.stdout = (
            '{"keyboards":[{"main":true,"active_layout_index":none}]}'
        )
        with patch.dict(
            os.environ, {"HYPRLAND_INSTANCE_SIGNATURE": "fixture"}, clear=True
        ):
            with self.assertRaises(KeyboardError):
                desktop_state()

    def test_key_name_lookup_tries_exact_case_before_alias_recovery(self):
        mapping = SeatKeymap.__new__(SeatKeymap)
        mapping.keysym_from_name = Mock(
            side_effect=lambda name, flags: 65 if not flags else 97
        )
        mapping._plan = Mock(return_value=(30, (42,)))
        self.assertEqual(mapping.resolve("A"), (30, (42,)))
        mapping.keysym_from_name.assert_called_once_with(b"A", 0)
        mapping._plan.assert_called_once_with(65)

    def test_missing_keymap_fd_is_not_replaced_by_a_guessed_map(self):
        keyboard, wire, _ = self.setup_keyboard()
        wire.incoming = packet(6, 0, 1, 4)
        with self.assertRaisesRegex(KeyboardError, "descriptor is missing"):
            keyboard.roundtrip()

    def test_invalid_keymap_still_closes_its_received_descriptor(self):
        keyboard, wire, _ = self.setup_keyboard()
        with tempfile.TemporaryFile() as file:
            fd = os.dup(file.fileno())
            wire.fd = fd
            wire.incoming = packet(6, 0, 0, 4)
            with self.assertRaisesRegex(KeyboardError, "Unsupported seat"):
                keyboard.roundtrip()
            with self.assertRaises(OSError):
                os.fstat(fd)

    def test_continuous_events_cannot_extend_the_response_deadline(self):
        keyboard, wire, _ = self.setup_keyboard()
        wire.incoming = packet(6, 5)  # Repeat info/unknown event cannot complete sync.
        with patch(
            "wayland_automation.utils.virtual_keyboard.time.monotonic",
            side_effect=[0, 0, 6],
        ):
            with self.assertRaisesRegex(KeyboardError, "timed out"):
                keyboard.roundtrip()


@unittest.skipUnless(
    ctypes.util.find_library("xkbcommon"), "libxkbcommon not installed on this host"
)
class NativeKeymapTests(unittest.TestCase):
    def keymap(self, layout="us", desktop=None):
        source = (
            'xkb_keymap { xkb_keycodes { include "evdev+aliases(qwerty)" }; '
            'xkb_types { include "complete" }; xkb_compatibility { include "complete" }; '
            'xkb_symbols { include "pc+' + layout + '+inet(evdev)" }; };\0'
        ).encode()
        result = SeatKeymap(source, desktop=desktop)
        self.addCleanup(result.close)
        return result

    def test_named_keys_match_real_evdev_codes(self):
        mapping = self.keymap()
        for name, code in (
            ("Print", 99),
            ("a", 30),
            ("q", 16),
            ("F13", 183),
            ("Escape", 1),
            ("Control_L", 29),
            ("Control_R", 97),
            ("Shift_L", 42),
            ("Shift_R", 54),
            ("KP_Enter", 96),
        ):
            self.assertEqual(mapping.resolve(name), (code, ()), name)

    def test_active_non_us_layout_is_not_replaced_by_us(self):
        mapping = self.keymap("fr")
        self.assertEqual(mapping.resolve("a"), (16, ()))
        self.assertEqual(mapping.resolve("q"), (30, ()))

    def test_text_case_punctuation_and_unicode_fallback(self):
        mapping = self.keymap()
        self.assertEqual(mapping.resolve("A"), (30, (42,)))
        self.assertEqual(mapping.resolve("KP_2"), (80, ()))
        self.assertEqual(mapping.character("a"), (30, ()))
        self.assertEqual(mapping.character("A"), (30, (42,)))
        self.assertEqual(mapping.character("!"), (2, (42,)))
        self.assertEqual(mapping.character("\n"), (28, ()))
        self.assertIsNone(mapping.character("🐢"))

    def test_caps_lock_and_num_lock_are_preserved_without_toggling(self):
        mapping = self.keymap(desktop={"group": 0, "caps": True, "num": True})
        self.assertEqual(mapping.character("a"), (30, (42,)))
        self.assertEqual(mapping.character("A"), (30, ()))
        self.assertEqual(mapping.character("2")[1], ())
        self.assertNotEqual(mapping.snapshot()[2], 0)

    def test_private_wtype_map_is_rejected_before_input(self):
        with self.assertRaises(KeyboardError):
            SeatKeymap(
                b'xkb_keymap { xkb_keycodes { minimum=8; maximum=10; <K1>=9; }; xkb_types { include "complete" }; xkb_compatibility { include "complete" }; xkb_symbols { key <K1> { [Print] }; }; };\0'
            )


if __name__ == "__main__":
    unittest.main()
