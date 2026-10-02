#!/usr/bin/env python3
"""Build "AWS Event AMP.app", a macOS app that opens the desktop window.

Started from the .app, the Dock and menu bar show "AWS Event AMP" with its own
icon instead of "python3.12". The app runs this project's venv/bin/python on
events_desktop.py, so it only works on this machine and must be rebuilt if the
project moves.

Usage:
    python3 make_mac_app.py      # writes dist/AWS Event AMP.app

Then double-click it, or drag it to Applications or the Dock.
"""

import plistlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from events_desktop import APP_NAME


PROJECT_ROOT = Path(__file__).resolve().parent
ICON_SOURCE = PROJECT_ROOT / "web" / "static" / "app-icon.png"
DIST_DIRECTORY = PROJECT_ROOT / "dist"
LAUNCHER_NAME = "launch"
ICON_NAME = "app-icon"
# The sizes iconutil expects in an .iconset, each also drawn at double size.
ICON_SIZES = [16, 32, 128, 256, 512]


def info_plist():
    """Return the app's Info.plist settings as a dict."""
    return {
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleIdentifier": "local.aws-event-amp",
        "CFBundleExecutable": LAUNCHER_NAME,
        "CFBundleIconFile": ICON_NAME,
        "CFBundlePackageType": "APPL",
        "CFBundleVersion": "1",
        # The launcher is a shell script, which has no CPU type of its own.
        # Without these, Apple silicon Macs refuse to open the app unless
        # Rosetta is installed (error -10669).
        "LSArchitecturePriority": ["arm64", "x86_64"],
        "LSRequiresNativeExecution": True,
    }


def launcher_script(project_root):
    """Return the shell script that starts the desktop window."""
    python = project_root / "venv" / "bin" / "python"
    entry_point = project_root / "events_desktop.py"
    return (
        "#!/bin/sh\n"
        f'cd "{project_root}" || exit 1\n'
        # exec keeps the process macOS launched, so it stays this app.
        f'exec "{python}" "{entry_point}"\n'
    )


def build_icns(icon_source, icns_path):
    """Convert a large PNG into a macOS .icns icon with sips and iconutil.

    Raises:
        subprocess.CalledProcessError: If either tool fails.
    """
    with tempfile.TemporaryDirectory() as temporary:
        iconset = Path(temporary) / f"{ICON_NAME}.iconset"
        iconset.mkdir()
        for size in ICON_SIZES:
            for scale in (1, 2):
                pixels = size * scale
                suffix = "@2x" if scale == 2 else ""
                output = iconset / f"icon_{size}x{size}{suffix}.png"
                subprocess.run(
                    [
                        "sips",
                        "-z",
                        str(pixels),
                        str(pixels),
                        str(icon_source),
                        "--out",
                        str(output),
                    ],
                    check=True,
                    capture_output=True,
                )
        subprocess.run(
            ["iconutil", "-c", "icns", str(iconset), "-o", str(icns_path)],
            check=True,
            capture_output=True,
        )


def build_app(destination_directory, project_root=PROJECT_ROOT):
    """Write the .app bundle into destination_directory, replacing any old one.

    Returns:
        The path of the new .app.
    """
    app_path = destination_directory / f"{APP_NAME}.app"
    if app_path.exists():
        shutil.rmtree(app_path)
    macos_directory = app_path / "Contents" / "MacOS"
    resources_directory = app_path / "Contents" / "Resources"
    macos_directory.mkdir(parents=True)
    resources_directory.mkdir(parents=True)

    with open(app_path / "Contents" / "Info.plist", "wb") as plist_file:
        plistlib.dump(info_plist(), plist_file)
    launcher = macos_directory / LAUNCHER_NAME
    launcher.write_text(launcher_script(project_root), encoding="utf-8")
    launcher.chmod(0o755)
    build_icns(ICON_SOURCE, resources_directory / f"{ICON_NAME}.icns")
    return app_path


def main():
    """Build the app, or explain why not."""
    if sys.platform != "darwin":
        sys.exit("make_mac_app.py only works on macOS.")
    app_path = build_app(DIST_DIRECTORY)
    print(f"Built {app_path}")
    print("Double-click it to start, or drag it to Applications or the Dock.")


if __name__ == "__main__":
    main()
