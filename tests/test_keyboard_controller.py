"""
tests/test_keyboard_controller.py — Tests for keyboard automation and command building.
"""

import unittest
from unittest.mock import MagicMock, call, patch
from wayland_automation.keyboard_controller import Keyboard, KeyboardError
from test_input import FakeKeyboard


class TestKeyboardController(unittest.TestCase):
    def setUp(self):
        self.kb = Keyboard()
        self.native = FakeKeyboard()
        factory = patch("wayland_automation.keyboard_controller.VirtualKeyboard", return_value=self.native)
        factory.start()
        self.addCleanup(factory.stop)
        self.addCleanup(self.kb.close)
        sleep = patch("wayland_automation.keyboard_controller.time.sleep")
        sleep.start()
        self.addCleanup(sleep.stop)

    @patch("shutil.which")
    def test_ensure_wtype_raises_if_missing(self, mock_which):
        mock_which.return_value = None
        with self.assertRaises(KeyboardError):
            self.kb._ensure_wtype()

    @patch.object(Keyboard, "is_available", return_value=True)
    @patch("subprocess.run")
    def test_typewrite_instant(self, mock_run, mock_avail):
        self.kb.typewrite("Hello Wayland!", interval=0)
        mock_run.assert_not_called()
        self.assertEqual(len(self.native.events), 2 * len("Hello Wayland!"))

    @patch.object(Keyboard, "is_available", return_value=True)
    @patch("subprocess.run")
    def test_typewrite_with_interval(self, mock_run, mock_avail):
        self.kb.typewrite("test", interval=0.05)
        mock_run.assert_not_called()
        self.assertEqual(len(self.native.events), 8)

    @patch.object(Keyboard, "is_available", return_value=True)
    @patch("subprocess.run")
    def test_press_named_key(self, mock_run, mock_avail):
        self.kb.press("enter")
        mock_run.assert_not_called()
        self.assertEqual(self.native.events, [("Return", 1), ("Return", 0)])

    @patch.object(Keyboard, "is_available", return_value=True)
    @patch("subprocess.run")
    def test_press_modifier(self, mock_run, mock_avail):
        self.kb.press("ctrl")
        mock_run.assert_not_called()
        self.assertEqual(self.native.events, [("Control_L", 1), ("Control_L", 0)])

    @patch.object(Keyboard, "is_available", return_value=True)
    @patch("subprocess.run")
    def test_key_down_and_up(self, mock_run, mock_avail):
        self.kb.keyDown("shift")
        self.assertIn("Shift_L", self.native.held)

        self.kb.keyUp("shift")
        self.assertFalse(self.native.held)

        self.kb.keyDown("a")
        self.assertIn("a", self.native.held)

        self.kb.keyUp("a")
        self.assertFalse(self.native.held)
        mock_run.assert_not_called()

    @patch.object(Keyboard, "is_available", return_value=True)
    @patch("subprocess.run")
    def test_hotkey_ordering(self, mock_run, mock_avail):
        self.kb.hotkey("ctrl", "alt", "t")
        self.assertEqual(self.native.events, [
            ("Control_L", 1), ("Alt_L", 1), ("t", 1), ("t", 0),
            ("Alt_L", 0), ("Control_L", 0),
        ])
        mock_run.assert_not_called()

    @patch.object(Keyboard, "is_available", return_value=False)
    def test_wtype_verification_is_explicit(self, available):
        with self.assertRaises(KeyboardError):
            Keyboard(verify_wtype=True)
        self.kb.press("Print")
        self.assertEqual(self.native.events, [("Print", 1), ("Print", 0)])

    def test_upstream_aliases_preserve_physical_key_names(self):
        for key, expected in (("windows", "Super_L"), ("meta", "Super_L"),
                              ("page_up", "Page_Up"), ("capslock", "Caps_Lock"),
                              ("f13", "F13"), ("\n", "Return")):
            self.assertEqual(self.kb._key(key), expected)


if __name__ == "__main__":
    unittest.main()
