"""Tests for make_mac_app, which builds the macOS .app bundle."""

import os
import plistlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from make_mac_app import (
    DIST_DIRECTORY,
    ICON_SOURCE,
    build_app,
    build_icns,
    info_plist,
    launcher_script,
    main,
)


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


class PortableBuildTests(unittest.TestCase):
    """Bundle assembly without macOS icon tools."""

    def test_build_icns_resizes_each_size_then_compiles(self):
        """Every icon size is drawn at normal and double scale, then compiled."""
        commands = []

        def record(command, check, capture_output):
            """Record one sips or iconutil command."""
            commands.append(command)
            self.assertTrue(check)
            self.assertTrue(capture_output)

        with tempfile.TemporaryDirectory() as temporary:
            icns_path = Path(temporary) / "app.icns"
            with mock.patch("make_mac_app.subprocess.run", side_effect=record):
                build_icns(Path("/icons/app-icon.png"), icns_path)

        self.assertEqual(len(commands), 11)
        self.assertEqual(
            commands[0][:6],
            ["sips", "-z", "16", "16", "/icons/app-icon.png", "--out"],
        )
        self.assertTrue(str(commands[0][-1]).endswith("icon_16x16.png"))
        self.assertTrue(str(commands[1][-1]).endswith("icon_16x16@2x.png"))
        self.assertEqual(commands[-1][0], "iconutil")
        self.assertEqual(commands[-1][-1], str(icns_path))

    def test_build_app_replaces_an_old_bundle(self):
        """Building writes the launcher and icon, removing any previous app."""
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary)
            old_marker = destination / "AWS Event AMP.app" / "old.txt"
            old_marker.parent.mkdir()
            old_marker.write_text("old", encoding="utf-8")

            with mock.patch("make_mac_app.build_icns") as build_icon:
                app_path = build_app(destination, Path("/projects/amp"))

            contents = app_path / "Contents"
            self.assertFalse(old_marker.exists())
            with open(contents / "Info.plist", "rb") as plist_file:
                self.assertEqual(plistlib.load(plist_file), info_plist())
            launcher = contents / "MacOS" / "launch"
            self.assertTrue(os.access(launcher, os.X_OK))
            self.assertIn("/projects/amp", launcher.read_text(encoding="utf-8"))
            build_icon.assert_called_once_with(
                ICON_SOURCE, contents / "Resources" / "app-icon.icns"
            )


class MainTests(unittest.TestCase):
    """The command entry point."""

    def test_refuses_to_run_off_macos(self):
        """Other systems get a clear exit instead of a half-built bundle."""
        with mock.patch.object(sys, "platform", "linux"):
            with self.assertRaises(SystemExit) as raised:
                main()

        self.assertEqual(str(raised.exception), "make_mac_app.py only works on macOS.")

    def test_builds_into_dist_on_macos(self):
        """On macOS, main builds the app and tells the user how to open it."""
        app_path = Path("/dist/AWS Event AMP.app")
        with (
            mock.patch.object(sys, "platform", "darwin"),
            mock.patch("make_mac_app.build_app", return_value=app_path) as build,
            mock.patch("builtins.print") as printed,
        ):
            main()

        build.assert_called_once_with(DIST_DIRECTORY)
        printed.assert_any_call(f"Built {app_path}")


if __name__ == "__main__":
    unittest.main()
