"""
tests/test_mouse_position.py — Tests for mouse position backends.
"""

import unittest
import os
from unittest.mock import MagicMock, patch
from wayland_automation.mouse_position import (
    NUM_RE,
    HyprlandBackend,
    KWinBackend,
    WlFindCursorBackend,
    XdotoolBackend,
    _find_qdbus,
    get_position,
)


class TestMousePosition(unittest.TestCase):
    def test_regex_number_extraction(self):
        txt = "Cursor pos: 512, 1024"
        nums = NUM_RE.findall(txt)
        self.assertEqual(nums, ["512", "1024"])

        txt_neg = "Coordinates -100 -250"
        nums_neg = NUM_RE.findall(txt_neg)
        self.assertEqual(nums_neg, ["-100", "-250"])

    @patch("shutil.which")
    @patch("subprocess.run")
    def test_hyprland_read_once(self, mock_run, mock_which):
        mock_which.return_value = "/usr/bin/hyprctl"
        mock_run.return_value = MagicMock(returncode=0, stdout="800, 600\n", stderr="")
        pos = HyprlandBackend.read_once()
        self.assertEqual(pos, (800, 600))

    @patch("shutil.which")
    def test_find_qdbus_variants(self, mock_which):
        def fake_which(cmd):
            if cmd == "qdbus6":
                return "/usr/bin/qdbus6"
            return None

        mock_which.side_effect = fake_which
        path = _find_qdbus()
        self.assertEqual(path, "/usr/bin/qdbus6")

    @patch("shutil.which")
    def test_backend_availability_when_missing(self, mock_which):
        mock_which.return_value = None
        self.assertFalse(HyprlandBackend.available())
        self.assertFalse(WlFindCursorBackend.available())
        self.assertFalse(KWinBackend.available())

    @patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "Hyprland"})
    @patch("shutil.which")
    @patch("subprocess.run")
    def test_kwin_discovery_leaves_hyprland_untouched(self, run, which):
        self.assertFalse(KWinBackend.available())
        run.assert_not_called()
        which.assert_not_called()

    @patch("wayland_automation.mouse_position.pick_backend_and_start")
    def test_snapshot_releases_its_tracking_backend(self, pick):
        controller = MagicMock()
        pick.return_value = ("fixture", lambda: (-50, 100), controller)
        self.assertEqual(get_position(), (-50, 100))
        controller.stop.assert_called_once()

    @patch("wayland_automation.mouse_position.time.monotonic", side_effect=[0, 2])
    @patch("wayland_automation.mouse_position.pick_backend_and_start")
    def test_snapshot_wait_is_bounded_when_data_never_arrives(self, pick, clock):
        controller = MagicMock()
        pick.return_value = ("fixture", lambda: None, controller)
        self.assertIsNone(get_position())
        controller.stop.assert_called_once()

    @patch("wayland_automation.mouse_position.pick_backend_and_start", return_value=(None, None, None))
    def test_snapshot_without_a_backend_returns_none(self, pick):
        self.assertIsNone(get_position())


if __name__ == "__main__":
    unittest.main()
