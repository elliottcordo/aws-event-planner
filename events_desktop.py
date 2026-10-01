#!/usr/bin/env python3
"""Start AWS Event AMP in its own desktop window, like a Winamp player.

The web app runs on a private local port and pywebview shows it in a native,
frameless window: the skin's title bar is the window's title bar.

Usage:
    python3 events_desktop.py
"""

import socket
import sys
import threading
import time
from pathlib import Path

import uvicorn
import webview

from web.app import create_app
from web.services import Services


APP_NAME = "AWS Event AMP"
WINDOW_TITLE = APP_NAME
# Windows groups taskbar windows by this ID; without one they join python.exe.
WINDOWS_APP_ID = "AWSEventAMP.Desktop"
STATIC_DIRECTORY = Path(__file__).resolve().parent / "web" / "static"
# Dragging these moves the window: the skin's title bar, or the page heading
# when no skin is chosen.
DRAG_REGION_SELECTOR = ".main-title-bar, .toolbar h1"
SERVER_START_TIMEOUT_SECONDS = 10
MAX_WINDOW_SIZE = (1600, 1000)
# Narrower than 1000 pixels, the Sessions and My schedule panels stack.
MIN_WINDOW_SIZE = (1100, 700)
# Room left around the window for the menu bar and Dock (or taskbar).
SCREEN_MARGIN = 80


class WindowControls:
    """Window actions for the skin's title-bar buttons.

    main() exposes minimize and close to the page, which calls them as
    window.pywebview.api.minimize() and window.pywebview.api.close().
    """

    def __init__(self, window):
        """Act on the given pywebview window."""
        self.window = window

    def minimize(self):
        """Minimize the window."""
        self.window.minimize()

    def close(self):
        """Close the window, which also quits the app."""
        self.window.destroy()


def set_app_identity(name):
    """Show the app as `name` with its own icon, not as Python.

    macOS takes the menu bar and Dock name from the running program's bundle,
    which is Python's; changing it in memory works if done before the window
    opens. Windows groups taskbar buttons by app ID, so giving the app its own
    ID makes the taskbar show this window's icon and title.
    """
    if sys.platform == "darwin":
        # PyObjC builds its names at run time, which pylint cannot see.
        # pylint: disable-next=import-outside-toplevel,import-error,no-name-in-module
        from Foundation import NSBundle
        info = NSBundle.mainBundle().infoDictionary()
        info["CFBundleName"] = name
        info["CFBundleDisplayName"] = name
    elif sys.platform == "win32":
        import ctypes  # pylint: disable=import-outside-toplevel
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(WINDOWS_APP_ID)


def app_icon_path(platform=sys.platform):
    """Return the icon file for this system: Windows needs .ico, macOS takes PNG."""
    if platform == "win32":
        return STATIC_DIRECTORY / "app-icon.ico"
    return STATIC_DIRECTORY / "app-icon.png"


def free_port():
    """Return a local TCP port that no other program is using right now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def window_size(screen_width, screen_height):
    """Return a (width, height) that fills most of a screen of the given size.

    The page fits itself to the window, so a taller window shows more of each
    list. The window is never smaller than MIN_WINDOW_SIZE, below which the two
    panels would stack.
    """
    max_width, max_height = MAX_WINDOW_SIZE
    min_width, min_height = MIN_WINDOW_SIZE
    width = min(max_width, screen_width - SCREEN_MARGIN)
    height = min(max_height, screen_height - SCREEN_MARGIN)
    return max(width, min_width), max(height, min_height)


def window_options(screen_width, screen_height):
    """Return the pywebview create_window settings for the player window."""
    width, height = window_size(screen_width, screen_height)
    return {
        "width": width,
        "height": height,
        "min_size": MIN_WINDOW_SIZE,
        "frameless": True,
        # Drag only by the title bar, so clicks and text selection still work.
        "easy_drag": False,
        "text_select": True,
        # Matches the skinned page's background while the skin loads.
        "background_color": "#202225",
    }


def start_server(app, port):
    """Run the web app on 127.0.0.1:port in a background thread.

    Returns:
        The uvicorn Server; set its `should_exit` to stop it.

    Raises:
        RuntimeError: If the server hasn't started within the timeout.
    """
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + SERVER_START_TIMEOUT_SECONDS
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError(f"The web app did not start on port {port}")
        time.sleep(0.05)
    return server


def main():
    """Start the web app and show it in a desktop window until it is closed."""
    set_app_identity(APP_NAME)
    port = free_port()
    server = start_server(create_app(Services.create_default()), port)

    webview.settings["ALLOW_DOWNLOADS"] = True  # For "Backup favorites".
    webview.settings["DRAG_REGION_SELECTOR"] = DRAG_REGION_SELECTOR
    screen = webview.screens[0]
    window = webview.create_window(
        WINDOW_TITLE, f"http://127.0.0.1:{port}/",
        **window_options(screen.width, screen.height),
    )
    controls = WindowControls(window)
    window.expose(controls.minimize, controls.close)
    webview.start(icon=str(app_icon_path()))  # Returns once the window is closed.
    server.should_exit = True


if __name__ == "__main__":
    main()
