"""Open web links in the user's browser, including from WSL."""

import subprocess
import webbrowser
from pathlib import Path


PROC_VERSION_PATH = Path("/proc/version")


def is_wsl(proc_version_path=PROC_VERSION_PATH):
    """Return whether the current Linux kernel is running under WSL."""
    try:
        version = Path(proc_version_path).read_text(encoding="utf-8")
    except OSError:
        return False
    return "microsoft" in version.lower()


def open_url(url, run=subprocess.run, wsl=None):
    """Open a URL in the user's default browser.

    WSL has its own Linux browser configuration, but users normally want links
    from a WSL-hosted app to open in their Windows browser. Windows Explorer
    delegates URLs to that browser. Some Explorer versions return a nonzero
    status after successfully handing off a URL, so only failure to start the
    process triggers the regular Python browser fallback.
    """
    running_in_wsl = is_wsl() if wsl is None else wsl
    if running_in_wsl:
        try:
            run(["explorer.exe", url], check=False)
            return True
        except OSError:
            pass
    return webbrowser.open(url)
