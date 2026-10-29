import json
import socket
import struct
import unittest
from unittest.mock import Mock, patch

from wayland_automation import Keyboard
from wayland_automation.mouse_controller import (
    Mouse,
    WaylandConnectionError,
    WaylandProtocolError,
)
from wayland_automation.utils.screen_resolution import get_geometry, get_resolution


class FakeKeyboard:
    def __init__(self):
        self.held = {}
        self.events = []
        self.keymap = Mock()
        self.keymap.resolve.side_effect = lambda name: (
            {
                "Print": 99,
                "a": 30,
                "q": 16,
                "Control_L": 29,
                "Shift_L": 42,
                "Super_L": 125,
                "Return": 28,
            }.get(name, 40),
            (),
        )
        self.keymap.character.side_effect = lambda char: (
            None if ord(char) > 127 else (ord(char), ())
        )

    def down(self, name, plan=None):
        if name not in self.held:
            self.held[name] = plan or self.keymap.resolve(name)
            self.events.append((name, 1))

    def up(self, name):
        if name in self.held:
            del self.held[name]
            self.events.append((name, 0))

    def close(self):
        for name in reversed(list(self.held)):
            self.up(name)


class KeyboardTests(unittest.TestCase):
    def setUp(self):
        self.native = FakeKeyboard()
        self.factory = patch(
            "wayland_automation.keyboard_controller.VirtualKeyboard",
            return_value=self.native,
        )
        self.create = self.factory.start()
        self.addCleanup(self.factory.stop)
        self.sleep = patch("wayland_automation.keyboard_controller.time.sleep")
        self.sleep.start()
        self.addCleanup(self.sleep.stop)

    @patch("wayland_automation.keyboard_controller.subprocess.run")
    def test_named_keys_never_use_wtype_private_codes(self, run):
        for name in ("Print", "a", "q", "F13", "Escape"):
            Keyboard().press(name)
        run.assert_not_called()
        self.assertEqual(
            self.native.events[:6],
            [("Print", 1), ("Print", 0), ("a", 1), ("a", 0), ("q", 1), ("q", 0)],
        )

    @patch("wayland_automation.keyboard_controller.subprocess.run")
    def test_layout_text_uses_physical_keys(self, run):
        Keyboard().typewrite("hello")
        run.assert_not_called()
        self.assertEqual(len(self.native.events), 10)

    @patch("wayland_automation.keyboard_controller.subprocess.run")
    def test_unicode_fallback_has_no_partially_typed_prefix(self, run):
        Keyboard().typewrite("-- text λ", 0.02)
        self.assertEqual(self.native.events, [])
        self.assertEqual(run.call_args.args[0], ["wtype", "-d", "20", "-"])
        self.assertTrue(run.call_args.kwargs["check"])
        self.assertEqual(run.call_args.kwargs["input"], "-- text λ")
        Keyboard().typewrite("🐢")
        self.assertEqual(run.call_args.args[0], ["wtype", "-"])

    def test_real_modifier_edges_and_timed_sequence(self):
        Keyboard().sequence([(0, "down", "ctrl"), (0, "down", "a"), (125, "up", "a")])
        self.assertEqual(
            self.native.events, [("Control_L", 1), ("a", 1), ("a", 0), ("Control_L", 0)]
        )
        self.assertFalse(self.native.held)
        self.create.assert_called_once()

    def test_holds_survive_between_calls_and_release_on_close(self):
        keyboard = Keyboard()
        keyboard.keyDown("ctrl")
        keyboard.press("a")
        self.assertEqual(list(self.native.held), ["Control_L"])
        self.create.assert_called_once()
        keyboard.close()
        self.assertFalse(self.native.held)

    def test_hotkey_keeps_preexisting_holds_and_aliases(self):
        keyboard = Keyboard()
        keyboard.keyDown("super")
        keyboard.hotkey("ctrl", "enter")
        self.assertEqual(list(self.native.held), ["Super_L"])
        self.assertEqual(
            self.native.events,
            [
                ("Super_L", 1),
                ("Control_L", 1),
                ("Return", 1),
                ("Return", 0),
                ("Control_L", 0),
            ],
        )
        keyboard.keyUp("logo")
        self.assertFalse(self.native.held)

    def test_failure_releases_owned_sequence_keys(self):
        original = self.native.down

        def fail(name):
            original(name)
            if name == "q":
                raise RuntimeError("connection failed")

        self.native.down = fail
        with self.assertRaises(RuntimeError):
            Keyboard().sequence([(0, "down", "ctrl"), (1, "down", "q")])
        self.assertFalse(self.native.held)
        self.assertEqual(self.native.events[-2:], [("q", 0), ("Control_L", 0)])

    def test_cancellation_releases_existing_and_new_holds(self):
        keyboard = Keyboard()
        keyboard.keyDown("ctrl")
        original = self.native.down

        def cancel(name):
            original(name)
            raise KeyboardInterrupt()

        self.native.down = cancel
        with self.assertRaises(KeyboardInterrupt):
            keyboard.press("q")
        self.assertFalse(self.native.held)
        self.assertEqual(self.native.events[-2:], [("q", 0), ("Control_L", 0)])

    @patch("wayland_automation.keyboard_controller.subprocess.run")
    def test_protocol_errors_never_fallback_to_wtype(self, run):
        self.create.side_effect = RuntimeError("missing virtual keyboard")
        with self.assertRaises(RuntimeError):
            Keyboard().press("Print")
        run.assert_not_called()

    def test_unavailable_key_is_rejected_before_input(self):
        self.native.keymap.resolve.side_effect = ValueError("Unavailable")
        with self.assertRaises(ValueError):
            Keyboard().sequence([(0, "down", "ctrl"), (1, "down", "nonsense")])
        self.assertEqual(self.native.events, [])

    def test_invalid_events_rejected_before_connection(self):
        for events in [
            [(-1, "down", "a")],
            [(0, "bad", "a")],
            [(20, "down", "a"), (10, "up", "a")],
            [(True, "down", "a")],
        ]:
            with self.assertRaises(ValueError):
                Keyboard().sequence(events)
        for interval in (float("nan"), -1):
            with self.assertRaises(ValueError):
                Keyboard().typewrite("test", interval)
        self.create.assert_not_called()

    def test_empty_operations_and_unmatched_release_do_not_connect(self):
        keyboard = Keyboard()
        keyboard.sequence([])
        keyboard.hotkey()
        keyboard.typewrite("")
        keyboard.keyUp("ctrl")
        keyboard.close()
        self.create.assert_not_called()


class PointerTests(unittest.TestCase):
    def mouse(self):
        mouse = Mouse.__new__(Mouse)
        mouse.endianness = "<"
        mouse.sock = Mock()
        mouse.current_virtual_pointer_id = 7
        mouse.callback_id = 3
        mouse.next_id = 8
        return mouse

    def test_fragmented_stream(self):
        mouse = self.mouse()
        packet = struct.pack("<IHHI", 7, 0, 12, 99)
        mouse.sock.recv.side_effect = [packet[:2], packet[2:8], packet[8:9], packet[9:]]
        self.assertEqual(mouse.receive_message(), (7, 0, struct.pack("<I", 99)))

    def test_eof_and_invalid_size(self):
        mouse = self.mouse()
        mouse.sock.recv.return_value = b""
        with self.assertRaises(WaylandConnectionError):
            mouse.receive_message()
        mouse.sock.recv.return_value = struct.pack("<IHH", 1, 0, 4)
        with self.assertRaises(WaylandProtocolError):
            mouse.receive_message()

    def test_timeouts_and_broken_pipe_are_library_errors(self):
        mouse = self.mouse()
        mouse.sock.recv.side_effect = socket.timeout("timeout")
        with self.assertRaises(WaylandConnectionError):
            mouse.receive_message()
        mouse.sock.sendall.side_effect = BrokenPipeError()
        with self.assertRaises(WaylandConnectionError):
            mouse.send_message(7, 4, b"")

    def test_sync_objects_are_not_reused(self):
        mouse = self.mouse()
        mouse.send_sync_request()
        first = mouse.callback_id
        mouse.send_sync_request()
        self.assertNotEqual(first, mouse.callback_id)

    @patch.object(Mouse, "get_socket_path", return_value="/fixture/wayland")
    @patch.object(Mouse, "connect_to_wayland")
    def test_initial_pointer_object_ids_remain_sequential(self, connect, path):
        from wayland_automation.mouse_controller import encode_wayland_string
        payload = struct.pack("<I", 10) + encode_wayland_string("zwlr_virtual_pointer_manager_v1") + struct.pack("<I", 2)
        registry = struct.pack("<IHH", 2, 0, 8 + len(payload)) + payload
        done = struct.pack("<IHHI", 3, 0, 12, 0)
        sock = connect.return_value
        sock.recv.side_effect = [registry[:8], registry[8:], done[:8], done[8:]]
        with Mouse() as mouse:
            sent = [call.args[0] for call in sock.sendall.call_args_list]
            self.assertEqual(struct.unpack("<I", sent[0][-4:])[0], 2)
            self.assertEqual(struct.unpack("<I", sent[1][-4:])[0], 3)
            self.assertEqual(struct.unpack("<I", sent[2][-4:])[0], 4)
            self.assertEqual(struct.unpack("<I", sent[3][-4:])[0], 5)
            mouse.send_sync_request()
            self.assertEqual(mouse.callback_id, 6)

    @patch("wayland_automation.mouse_controller.get_geometry", return_value=(0, 0, 1920, 1080))
    def test_move_only_requests_never_emit_a_click(self, geometry):
        mouse = self.mouse()
        mouse.send_motion_absolute = Mock()
        mouse.send_click = Mock()
        mouse.send_sync_request = Mock()
        mouse.handle_events = Mock()
        for button in (None, "nothing", "none", "move"):
            mouse.click(100, 200, button)
        mouse.send_click.assert_not_called()
        self.assertEqual(mouse.send_motion_absolute.call_count, 4)

    def test_scroll_opcodes_and_fixed_values(self):
        mouse = self.mouse()
        mouse.send_message = Mock()
        mouse.send_sync_request = Mock()
        mouse.handle_events = Mock()
        mouse.scroll(vertical=-2, horizontal=1)
        calls = mouse.send_message.call_args_list
        self.assertEqual([call.args[1] for call in calls], [5, 7, 7, 4])
        self.assertEqual(
            struct.unpack("<IIii", calls[1].args[2])[1:], (0, -2 * 15 * 256, -2)
        )

    @patch(
        "wayland_automation.mouse_controller.get_geometry",
        return_value=(-1920, 0, 3840, 1080),
    )
    def test_negative_desktop_origin_and_middle_button(self, geometry):
        mouse = self.mouse()
        mouse.send_motion_absolute = Mock()
        mouse.send_click = Mock()
        mouse.send_sync_request = Mock()
        mouse.handle_events = Mock()
        mouse.click(-100, 50, "middle")
        mouse.send_motion_absolute.assert_called_once_with(1820, 50, 3840, 1080)
        mouse.send_click.assert_called_once_with(0x112)

    def test_close_is_idempotent(self):
        mouse = self.mouse()
        sock = mouse.sock
        mouse.close()
        mouse.close()
        sock.close.assert_called_once()

    def test_button_holds_and_releases(self):
        mouse = self.mouse()
        mouse.send_message = Mock()
        mouse.button("middle", True)
        mouse.button("middle", False)
        calls = mouse.send_message.call_args_list
        self.assertEqual(struct.unpack("<III", calls[0].args[2])[1:], (0x112, 1))
        self.assertEqual(struct.unpack("<III", calls[2].args[2])[1:], (0x112, 0))

    @patch.dict("os.environ", {"WAYLAND_DISPLAY": "/custom/wayland"})
    @patch("wayland_automation.mouse_controller.os.path.isabs", return_value=True)
    def test_absolute_wayland_socket(self, isabs):
        self.assertEqual(self.mouse().get_socket_path(), "/custom/wayland")


class GeometryTests(unittest.TestCase):
    @patch(
        "wayland_automation.utils.screen_resolution.shutil.which",
        return_value="hyprctl",
    )
    @patch("wayland_automation.utils.screen_resolution.subprocess.run")
    def test_scaled_rotated_monitors(self, run, which):
        run.return_value.stdout = json.dumps(
            [
                {
                    "x": -1080,
                    "y": 0,
                    "width": 1920,
                    "height": 1080,
                    "transform": 1,
                    "scale": 1,
                },
                {"x": 0, "y": 0, "width": 3840, "height": 2160, "scale": 2},
            ]
        )
        self.assertEqual(get_geometry(), (-1080, 0, 3000, 1920))

    @patch("wayland_automation.utils.screen_resolution.subprocess.run")
    def test_missing_resolution_raises(self, run):
        run.return_value.stdout = "no outputs"
        with self.assertRaises(RuntimeError):
            get_resolution(use_hyprland=False)


if __name__ == "__main__":
    unittest.main()
