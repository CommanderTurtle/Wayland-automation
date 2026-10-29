"""
tests/test_package.py — Tests for package metadata and public API.
"""

import unittest
import contextlib
import io
from unittest.mock import patch
import wayland_automation as wa
from test_input import FakeKeyboard


class TestPackage(unittest.TestCase):
    def test_version_present(self):
        self.assertTrue(hasattr(wa, "__version__"))
        parts = wa.__version__.split(".")
        self.assertGreaterEqual(len(parts), 2)

    def test_all_exports(self):
        for name in wa.__all__:
            self.assertTrue(
                hasattr(wa, name), f"Export '{name}' in __all__ not found in package"
            )

    def test_convenience_functions_exist(self):
        expected = [
            "click",
            "swipe",
            "auto_click",
            "typewrite",
            "press",
            "keyDown",
            "keyUp",
            "hotkey",
            "get_position",
            "position",
            "mouse_position_generator",
            "cleanup",
            "scroll",
        ]
        for func in expected:
            self.assertTrue(callable(getattr(wa, func)), f"{func} should be callable")

    @patch("wayland_automation.mouse_controller.Mouse")
    def test_move_only_default_and_scroll_keep_scoped_pointer_lifetimes(self, factory):
        pointer = factory.return_value.__enter__.return_value
        wa.click(-20, 100)
        pointer.click.assert_called_once_with(-20, 100, None)
        wa.scroll(-1, 2)
        pointer.scroll.assert_called_once_with(-1, 2)
        self.assertEqual(factory.return_value.__exit__.call_count, 2)

    def test_top_level_holds_and_cleanup_keep_the_physical_backend(self):
        wa.cleanup()
        self.addCleanup(wa.cleanup)
        native = FakeKeyboard()
        with patch("wayland_automation.keyboard_controller.VirtualKeyboard", return_value=native):
            wa.keyDown("ctrl")
            wa.press("a")
            self.assertEqual(list(native.held), ["Control_L"])
            wa.keyUp("ctrl")
            self.assertFalse(native.held)
            wa.keyDown("shift")
            wa.cleanup()
            self.assertFalse(native.held)
            self.assertIsNone(wa._default_keyboard)

    @patch("wayland_automation.click")
    def test_cli_default_is_move_only(self, click):
        from wayland_automation.__main__ import main
        self.assertEqual(main(["click", "-20", "100"]), 0)
        click.assert_called_once_with(-20, 100, "nothing")

    @patch("ctypes.util.find_library", return_value="libxkbcommon.so.0")
    @patch("shutil.which", return_value=None)
    def test_diagnostics_keep_unicode_and_raw_device_backends_optional(self, which, library):
        from wayland_automation.__main__ import check_environment
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(check_environment(), 0)
        self.assertIn("Unicode text fallback", output.getvalue())
        self.assertNotIn("Missing (Required)", output.getvalue())


if __name__ == "__main__":
    unittest.main()
