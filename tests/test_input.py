import json
import socket
import struct
import subprocess
import unittest
from unittest.mock import Mock, patch

from wayland_automation import Keyboard
from wayland_automation.mouse_controller import (
    Mouse, WaylandConnectionError, WaylandProtocolError,
)
from wayland_automation.utils.screen_resolution import get_geometry, get_resolution


class KeyboardTests(unittest.TestCase):
    @patch("wayland_automation.keyboard_controller.subprocess.run")
    def test_text_is_one_checked_process(self, run):
        Keyboard().typewrite("-- text λ", 0.02)
        self.assertEqual(run.call_args.args[0],
                         ["wtype", "-d", "20", "-"])
        self.assertTrue(run.call_args.kwargs["check"])
        self.assertEqual(run.call_args.kwargs["input"], "-- text λ")

    @patch("wayland_automation.keyboard_controller.subprocess.run")
    def test_zero_delay_and_super_alias(self, run):
        Keyboard().typewrite("hello")
        self.assertEqual(run.call_args.args[0], ["wtype", "-"])
        Keyboard().hotkey("super", "altgr", "e")
        self.assertEqual(run.call_args.args[0], ["wtype", "-M", "logo",
                         "-M", "altgr", "-k", "e", "-m", "altgr", "-m", "logo"])

    @patch("wayland_automation.keyboard_controller.subprocess.run")
    def test_enter_is_named_key_not_duplicate_newline(self, run):
        Keyboard().press("enter")
        self.assertEqual(run.call_args.args[0], ["wtype", "-k", "Return"])
        self.assertEqual(run.call_count, 1)

    @patch("wayland_automation.keyboard_controller.subprocess.run")
    def test_chord_keeps_same_keyboard(self, run):
        Keyboard().sequence([(0, "down", "ctrl"), (0, "down", "c"),
                             (125, "up", "c")])
        self.assertEqual(run.call_args.args[0], ["wtype", "-M", "ctrl",
                         "-P", "c", "-s", "125", "-p", "c", "-m", "ctrl"])

    @patch("wayland_automation.keyboard_controller.subprocess.run")
    def test_hotkey_alias_and_failure(self, run):
        run.side_effect = subprocess.CalledProcessError(1, "wtype")
        with self.assertRaises(subprocess.CalledProcessError):
            Keyboard().hotkey("ctrl", "enter")
        self.assertEqual(run.call_args.args[0],
                         ["wtype", "-M", "ctrl", "-k", "Return", "-m", "ctrl"])

    def test_invalid_events_rejected_before_input(self):
        for events in [[(-1, "down", "a")], [(0, "bad", "a")],
                       [(20, "down", "a"), (10, "up", "a")]]:
            with self.assertRaises(ValueError):
                Keyboard().sequence(events)
        with self.assertRaises(ValueError):
            Keyboard().typewrite("test", float("nan"))


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

    def test_scroll_opcodes_and_fixed_values(self):
        mouse = self.mouse()
        mouse.send_message = Mock()
        mouse.send_sync_request = Mock()
        mouse.handle_events = Mock()
        mouse.scroll(vertical=-2, horizontal=1)
        calls = mouse.send_message.call_args_list
        self.assertEqual([call.args[1] for call in calls], [5, 7, 7, 4])
        self.assertEqual(struct.unpack("<IIii", calls[1].args[2])[1:],
                         (0, -2 * 15 * 256, -2))

    @patch("wayland_automation.mouse_controller.get_geometry",
           return_value=(-1920, 0, 3840, 1080))
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
    def test_absolute_wayland_socket(self):
        self.assertEqual(self.mouse().get_socket_path(), "/custom/wayland")


class GeometryTests(unittest.TestCase):
    @patch("wayland_automation.utils.screen_resolution.shutil.which", return_value="hyprctl")
    @patch("wayland_automation.utils.screen_resolution.subprocess.run")
    def test_scaled_rotated_monitors(self, run, which):
        run.return_value.stdout = json.dumps([
            {"x": -1080, "y": 0, "width": 1920, "height": 1080, "transform": 1, "scale": 1},
            {"x": 0, "y": 0, "width": 3840, "height": 2160, "scale": 2},
        ])
        self.assertEqual(get_geometry(), (-1080, 0, 3000, 1920))

    @patch("wayland_automation.utils.screen_resolution.subprocess.run")
    def test_missing_resolution_raises(self, run):
        run.return_value.stdout = "no outputs"
        with self.assertRaises(RuntimeError):
            get_resolution(use_hyprland=False)


if __name__ == "__main__":
    unittest.main()
