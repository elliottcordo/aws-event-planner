"""Tests for opening browser links on Linux and WSL."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aws_events.browser import is_wsl, open_url


class WslDetectionTests(unittest.TestCase):
    """Detect WSL from the Linux kernel version."""

    def test_microsoft_kernel_is_wsl(self):
        """Return true when the kernel version names Microsoft."""
        with tempfile.TemporaryDirectory() as directory:
            version_path = Path(directory) / "version"
            version_path.write_text(
                "Linux version 6.6.87.2-microsoft-standard-WSL2",
                encoding="utf-8",
            )

            self.assertTrue(is_wsl(version_path))

    def test_regular_kernel_is_not_wsl(self):
        """Return false for a regular Linux kernel."""
        with tempfile.TemporaryDirectory() as directory:
            version_path = Path(directory) / "version"
            version_path.write_text(
                "Linux version 6.8.0-generic",
                encoding="utf-8",
            )

            self.assertFalse(is_wsl(version_path))

    def test_missing_version_file_is_not_wsl(self):
        """Return false when the kernel version cannot be read."""
        self.assertFalse(is_wsl("/path/that/does/not/exist"))


class OpenUrlTests(unittest.TestCase):
    """Choose the browser opener appropriate to the environment."""

    def test_wsl_uses_windows_explorer(self):
        """Pass the URL to Windows Explorer when running under WSL."""
        run = mock.Mock()

        opened = open_url("https://example.com/a?b=c", run=run, wsl=True)

        self.assertTrue(opened)
        run.assert_called_once_with(
            ["explorer.exe", "https://example.com/a?b=c"],
            check=False,
        )

    @mock.patch("aws_events.browser.webbrowser.open")
    def test_missing_explorer_falls_back_to_python_browser(self, browser_open):
        """Use Python's browser support if Explorer cannot be started."""

        def missing_explorer(*_args, **_kwargs):
            """Simulate explorer.exe not being available."""
            raise OSError("not found")

        browser_open.return_value = True

        opened = open_url(
            "https://example.com",
            run=missing_explorer,
            wsl=True,
        )

        self.assertTrue(opened)
        browser_open.assert_called_once_with("https://example.com")

    @mock.patch("aws_events.browser.webbrowser.open")
    def test_regular_linux_uses_python_browser(self, browser_open):
        """Use Python's configured browser outside WSL."""
        browser_open.return_value = True
        run = mock.Mock()

        opened = open_url("https://example.com", run=run, wsl=False)

        self.assertTrue(opened)
        run.assert_not_called()
        browser_open.assert_called_once_with("https://example.com")


if __name__ == "__main__":
    unittest.main()
