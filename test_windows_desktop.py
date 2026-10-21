import unittest
from unittest.mock import patch

from windows_desktop import require_visible_desktop


class DesktopTests(unittest.TestCase):
    def test_hidden_desktop_is_rejected(self):
        with patch('windows_desktop.current_desktop', return_value='CodexSandboxDesktop-test'):
            with self.assertRaisesRegex(RuntimeError, 'Default desktop'):
                require_visible_desktop()

    def test_default_and_non_windows_are_allowed(self):
        for desktop in ('Default', 'default', None):
            with self.subTest(desktop=desktop), patch('windows_desktop.current_desktop', return_value=desktop):
                require_visible_desktop()

    def test_desktop_lookup_failure_is_not_ignored(self):
        with patch('windows_desktop.current_desktop', side_effect=OSError('access denied')):
            with self.assertRaises(OSError):
                require_visible_desktop()
