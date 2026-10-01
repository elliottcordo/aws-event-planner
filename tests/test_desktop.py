"""Tests for events_desktop, the desktop window around the web app."""

import socket
import unittest
import urllib.request

from events_desktop import (
    WindowControls,
    app_icon_path,
    free_port,
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
    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-type", b"text/plain")]})
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
        controls = WindowControls(window, start_timer=lambda delay, action:
                                  FakeTimer(delay, action, timers))
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
        self.assertEqual(window_size(1024, 700), (1100, 700))


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


if __name__ == "__main__":
    unittest.main()
