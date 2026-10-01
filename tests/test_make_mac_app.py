"""Tests for make_mac_app, which builds the macOS .app bundle."""

import os
import plistlib
import sys
import tempfile
import unittest
from pathlib import Path

from make_mac_app import build_app, info_plist, launcher_script


class InfoPlistTests(unittest.TestCase):
    """The bundle's settings."""

    def test_names_the_app_and_runs_natively(self):
        """The app has its own name and icon and needs no Rosetta."""
        info = info_plist()
        self.assertEqual(info["CFBundleName"], "AWS Event AMP")
        self.assertEqual(info["CFBundleExecutable"], "launch")
        self.assertEqual(info["CFBundleIconFile"], "app-icon")
        self.assertEqual(info["LSArchitecturePriority"][0], "arm64")
        self.assertTrue(info["LSRequiresNativeExecution"])


class LauncherTests(unittest.TestCase):
    """The script the app runs."""

    def test_runs_the_venv_python_on_the_desktop_app(self):
        """The script execs this project's venv Python on events_desktop.py."""
        script = launcher_script(Path("/projects/amp"))
        self.assertTrue(script.startswith("#!/bin/sh\n"))
        self.assertIn('cd "/projects/amp" || exit 1', script)
        self.assertIn(
            'exec "/projects/amp/venv/bin/python" "/projects/amp/events_desktop.py"',
            script,
        )


@unittest.skipUnless(sys.platform == "darwin", "needs macOS's sips and iconutil")
class BuildAppTests(unittest.TestCase):
    """Building the whole bundle."""

    def test_builds_a_complete_bundle(self):
        """The bundle has its Info.plist, a runnable launcher and an icon."""
        with tempfile.TemporaryDirectory() as temporary:
            app_path = build_app(Path(temporary), Path("/projects/amp"))
            contents = app_path / "Contents"
            self.assertEqual(app_path.name, "AWS Event AMP.app")
            with open(contents / "Info.plist", "rb") as plist_file:
                self.assertEqual(plistlib.load(plist_file), info_plist())
            self.assertTrue(os.access(contents / "MacOS" / "launch", os.X_OK))
            self.assertTrue((contents / "Resources" / "app-icon.icns").is_file())


if __name__ == "__main__":
    unittest.main()
