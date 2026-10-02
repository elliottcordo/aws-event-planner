"""Tests for events_desktop, the desktop window around the web app."""

import socket
import sys
import unittest
import urllib.request
from unittest import mock

from events_desktop import (
    DRAG_REGION_SELECTOR,
    WINDOWS_APP_ID,
    WindowControls,
    app_icon_path,
    free_port,
    main,
    set_app_identity,
    start_server,
    window_options,
    window_size,
)


class FakeWindow:
    """Record the window actions called on it."""

    def __init__(self):
        """Start with no actions."""
        self.actions = []

    def minimize(self):
        """Record a minimize."""
        self.actions.append("minimize")

    def destroy(self):
        """Record a close."""
        self.actions.append("destroy")


class FakeTimer:
    """Stand in for threading.Timer, recording instead of waiting."""

    def __init__(self, delay, action, timers):
        """Store the delay and action, and add this timer to `timers`."""
        self.delay = delay
        self.action = action
        self.daemon = False
        self.started = False
        timers.append(self)

    def start(self):
        """Record that the timer was started."""
        self.started = True


async def hello_app(scope, receive, send):
    """Answer every HTTP request with "hello" (a minimal ASGI app)."""
    if scope["type"] != "http":
        return
    await receive()
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/plain")],
        }
    )
    await send({"type": "http.response.body", "body": b"hello"})


class WindowControlsTests(unittest.TestCase):
    """The title-bar buttons."""

    def test_minimize(self):
        """Minimize acts on the window straight away."""
        window = FakeWindow()
        WindowControls(window).minimize()
        self.assertEqual(window.actions, ["minimize"])

    def test_close_waits_until_the_call_has_returned(self):
        """Close returns first and destroys the window from a daemon timer.

        Destroying it during the call would leave pywebview waiting forever to
        send the result to a page that is gone, so the app would never quit.
        """
        window = FakeWindow()
        timers = []
        controls = WindowControls(
            window, start_timer=lambda delay, action: FakeTimer(delay, action, timers)
        )
        controls.close()
        self.assertEqual(window.actions, [])
        self.assertEqual(len(timers), 1)
        self.assertTrue(timers[0].started)
        self.assertTrue(timers[0].daemon)
        timers[0].action()
        self.assertEqual(window.actions, ["destroy"])


class WindowOptionsTests(unittest.TestCase):
    """How the window is set up."""

    def test_frameless_but_usable(self):
        """No frame, dragged only by the title bar, and text can be selected."""
        options = window_options(1512, 982)
        self.assertTrue(options["frameless"])
        self.assertFalse(options["easy_drag"])
        self.assertTrue(options["text_select"])

    def test_window_fills_most_of_the_screen(self):
        """The window leaves a margin on a laptop screen and is capped on big ones."""
        self.assertEqual(window_size(1512, 982), (1432, 902))
        self.assertEqual(window_size(2560, 1440), (1600, 1000))

    def test_window_never_smaller_than_minimum(self):
        """On a small screen the window keeps its minimum size."""
        self.assertEqual(window_size(1024, 700), (1280, 700))


class AppIdentityTests(unittest.TestCase):
    """The name shown in the Dock or taskbar."""

    def test_macos_renames_the_running_bundle(self):
        """macOS takes the menu bar name from the bundle info dictionary."""
        info = {}
        bundle = mock.Mock()
        bundle.infoDictionary.return_value = info
        foundation = mock.Mock()
        foundation.NSBundle.mainBundle.return_value = bundle
        with (
            mock.patch.object(sys, "platform", "darwin"),
            mock.patch.dict(sys.modules, {"Foundation": foundation}),
        ):
            set_app_identity("AWS Event AMP")

        self.assertEqual(info["CFBundleName"], "AWS Event AMP")
        self.assertEqual(info["CFBundleDisplayName"], "AWS Event AMP")

    def test_windows_sets_its_own_taskbar_identity(self):
        """Windows groups the window separately from other Python programs."""
        ctypes_module = mock.Mock()
        with (
            mock.patch.object(sys, "platform", "win32"),
            mock.patch.dict(sys.modules, {"ctypes": ctypes_module}),
        ):
            set_app_identity("AWS Event AMP")

        set_identity = (
            ctypes_module.windll.shell32.SetCurrentProcessExplicitAppUserModelID
        )
        set_identity.assert_called_once_with(WINDOWS_APP_ID)


class AppIconTests(unittest.TestCase):
    """The Dock and taskbar icon."""

    def test_icon_per_system(self):
        """Windows gets the .ico, other systems the PNG; both files exist."""
        windows_icon = app_icon_path("win32")
        mac_icon = app_icon_path("darwin")
        self.assertEqual(windows_icon.suffix, ".ico")
        self.assertEqual(mac_icon.suffix, ".png")
        self.assertTrue(windows_icon.is_file())
        self.assertTrue(mac_icon.is_file())


class ServerTests(unittest.TestCase):
    """Running the web app in the background."""

    def test_free_port_can_be_bound(self):
        """The port returned is free to listen on."""
        port = free_port()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", port))

    def test_start_server_serves_the_app_until_stopped(self):
        """The app answers on the port, and should_exit stops it."""
        port = free_port()
        server = start_server(hello_app, port)
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/") as response:
                self.assertEqual(response.read(), b"hello")
        finally:
            server.should_exit = True

    def test_start_server_reports_a_server_that_never_starts(self):
        """A thread that exits before serving raises instead of waiting forever."""

        class SilentServer:
            """A server whose run method returns without serving."""

            def __init__(self, config):
                """Remember the config and stay unstarted."""
                self.config = config
                self.started = False

            def run(self):
                """Return immediately, as a server that failed to bind would."""

        with (
            mock.patch("events_desktop.uvicorn.Config"),
            mock.patch("events_desktop.uvicorn.Server", SilentServer),
        ):
            with self.assertRaisesRegex(RuntimeError, "did not start on port 9"):
                start_server(hello_app, 9)


class MainTests(unittest.TestCase):
    """Starting and closing the desktop window."""

    def test_main_shows_the_app_until_the_window_closes(self):
        """The window opens on the local app, then the server is asked to stop."""
        server = mock.Mock()
        window = mock.Mock()
        screen = mock.Mock(width=1512, height=982)
        icon = "/tmp/app-icon.png"
        with (
            mock.patch("events_desktop.set_app_identity") as identity,
            mock.patch("events_desktop.free_port", return_value=4321),
            mock.patch(
                "events_desktop.Services.create_default", return_value="services"
            ),
            mock.patch("events_desktop.create_app", return_value="app") as create_app,
            mock.patch("events_desktop.start_server", return_value=server) as start,
            mock.patch("events_desktop.app_icon_path", return_value=icon),
            mock.patch("events_desktop.webview") as webview,
        ):
            webview.screens = [screen]
            webview.settings = {}
            webview.create_window.return_value = window
            main()

        identity.assert_called_once_with("AWS Event AMP")
        create_app.assert_called_once_with("services")
        start.assert_called_once_with("app", 4321)
        self.assertTrue(webview.settings["ALLOW_DOWNLOADS"])
        self.assertEqual(webview.settings["DRAG_REGION_SELECTOR"], DRAG_REGION_SELECTOR)
        webview.create_window.assert_called_once()
        title, url = webview.create_window.call_args.args
        self.assertEqual(title, "AWS Event AMP")
        self.assertEqual(url, "http://127.0.0.1:4321/")
        self.assertEqual(len(window.expose.call_args.args), 2)
        webview.start.assert_called_once_with(icon=str(icon))
        self.assertTrue(server.should_exit)


if __name__ == "__main__":
    unittest.main()
