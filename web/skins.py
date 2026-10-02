"""Winamp 2 ("classic") skins: the list of available skins and a .wsz reader.

A classic skin is a zip file (.wsz) of BMP sprite sheets plus a few text
files. The web UI uses:

- main.bmp, titlebar.bmp, cbuttons.bmp, shufrep.bmp and text.bmp for the
  header, drawn to look like Winamp's main window;
- pledit.bmp for the frames of the session and schedule windows, drawn to
  look like Winamp's playlist window;
- pledit.txt for the colors and font inside those windows;
- viscolor.txt for the colors of the spectrum analyzer in the main window.

Skins are listed in web/skins.json. A skin missing any of these files borrows
it from the default (base) skin, as Winamp itself does.
"""

import io
import json
import re
import threading
import zipfile
from dataclasses import dataclass
from pathlib import Path

import requests

from aws_events.catalog import DATA_DIRECTORY


REGISTRY_PATH = Path(__file__).resolve().parent / "skins.json"
SKIN_CACHE_DIRECTORY = DATA_DIRECTORY / "skins"
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# The sprite sheets the UI draws from; nothing else is served out of a skin.
SPRITE_SHEETS = (
    "main.bmp",
    "titlebar.bmp",
    "cbuttons.bmp",
    "shufrep.bmp",
    "text.bmp",
    "pledit.bmp",
)

# Guards against broken or hostile archives.
MAX_SKIN_BYTES = 5_000_000
MAX_FILE_BYTES = 2_000_000
DOWNLOAD_TIMEOUT_SECONDS = 30

# Winamp's own defaults, used when a skin has no pledit.txt or leaves a value out.
DEFAULT_PLAYLIST_STYLE = {
    "normal": "#00FF00",
    "current": "#FFFFFF",
    "normal_bg": "#000000",
    "selected_bg": "#0000C6",
    "font": "Arial",
}

# Winamp's default viscolor.txt: 0 is the background, 1 the grid dots, 2-17
# the spectrum bars from top to bottom, 18-22 the oscilloscope, 23 the peaks.
DEFAULT_VIS_COLORS = (
    (0, 0, 0),
    (24, 33, 41),
    (239, 49, 16),
    (206, 41, 16),
    (214, 90, 0),
    (214, 102, 0),
    (214, 115, 0),
    (198, 123, 8),
    (222, 165, 24),
    (214, 181, 33),
    (189, 222, 41),
    (148, 222, 33),
    (41, 206, 16),
    (50, 190, 16),
    (57, 181, 16),
    (49, 156, 8),
    (41, 148, 0),
    (24, 132, 8),
    (255, 255, 255),
    (214, 214, 222),
    (181, 189, 189),
    (160, 170, 175),
    (148, 156, 165),
    (150, 150, 150),
)
# A viscolor.txt line starts with "red,green,blue"; a comment may follow.
VIS_COLOR_LINE = re.compile(r"^\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})")

# pledit.txt key (lowercase) to our style name.
PLEDIT_KEYS = {
    "normal": "normal",
    "current": "current",
    "normalbg": "normal_bg",
    "selectedbg": "selected_bg",
    "font": "font",
}

HEX_COLOR = re.compile(r"^#(?:[0-9a-fA-F]{3}){1,2}$")
# WCAG AA contrast for normal-size text.
MINIMUM_CONTRAST = 4.5
# Font names end up inside CSS, so only plain characters are allowed.
SAFE_FONT_NAME = re.compile(r"^[A-Za-z0-9 ._-]{1,40}$")


class SkinError(Exception):
    """Raised when a skin cannot be found, downloaded or read."""


@dataclass
class SkinInfo:
    """One entry from skins.json.

    Attributes:
        id: Short identifier used in URLs, for example "garfield".
        name: Display name.
        url: Where to download the .wsz from, if it is not a local file.
        file: Path to a local .wsz, relative to the project root.
        year: Year the skin was made, if known.
        likes: Likes on the Winamp Skin Museum, if known.
        museum_url: The skin's page on the Winamp Skin Museum, if any.
    """

    id: str
    name: str
    url: str = None
    file: str = None
    year: int = None
    likes: int = None
    museum_url: str = None


def parse_pledit_txt(text):
    """Return the playlist colors and font from a pledit.txt file.

    Keys are matched case-insensitively and bad values are ignored, so a
    sloppy file still gives a usable style. Anything missing comes from
    DEFAULT_PLAYLIST_STYLE. The result also has a "text" color, chosen by
    readable_text_color, for body text.
    """
    style = dict(DEFAULT_PLAYLIST_STYLE)
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        name = PLEDIT_KEYS.get(key.strip().lower())
        value = value.strip()
        if name is None:
            continue
        if name == "font":
            if SAFE_FONT_NAME.match(value):
                style["font"] = value
        elif HEX_COLOR.match(value):
            style[name] = value.upper()
    style["text"] = readable_text_color(style)
    return style


def parse_viscolor_txt(text):
    """Return the 24 visualizer colors from a viscolor.txt file, as "#RRGGBB".

    Line N gives color N as "red,green,blue", often followed by a comment.
    Lines that don't start that way, values over 255, and any colors after
    the 24th are ignored; colors the file doesn't give come from Winamp's
    defaults.
    """
    colors = list(DEFAULT_VIS_COLORS)
    index = 0
    for line in text.splitlines():
        if index >= len(colors):
            break
        match = VIS_COLOR_LINE.match(line)
        if match is None:
            continue
        channels = tuple(int(value) for value in match.groups())
        if max(channels) <= 255:
            colors[index] = channels
        index += 1
    return [rgb_to_hex(color) for color in colors]


def rgb_to_hex(color):
    """Return a (red, green, blue) tuple as "#RRGGBB"."""
    red, green, blue = color
    return f"#{red:02X}{green:02X}{blue:02X}"


def relative_luminance(color):
    """Return the WCAG relative luminance of a "#RGB" or "#RRGGBB" color."""
    digits = color.lstrip("#")
    if len(digits) == 3:
        digits = "".join(digit * 2 for digit in digits)
    channels = []
    for start in (0, 2, 4):
        value = int(digits[start : start + 2], 16) / 255
        if value <= 0.03928:
            channels.append(value / 12.92)
        else:
            channels.append(((value + 0.055) / 1.055) ** 2.4)
    red, green, blue = channels
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast_ratio(first, second):
    """Return the WCAG contrast ratio (1 to 21) between two colors."""
    lighter, darker = sorted(
        [relative_luminance(first), relative_luminance(second)], reverse=True
    )
    return (lighter + 0.05) / (darker + 0.05)


def readable_text_color(style):
    """Pick a text color that is readable on the skin's background.

    Many classic skins use colors that suit a short track name but are hard
    to read in paragraphs, such as dark red on black. Prefer the skin's own
    "normal" color, then its "current" color, then plain white or black.
    """
    background = style["normal_bg"]
    for candidate in (style["normal"], style["current"]):
        if contrast_ratio(candidate, background) >= MINIMUM_CONTRAST:
            return candidate
    if contrast_ratio("#FFFFFF", background) >= contrast_ratio("#000000", background):
        return "#FFFFFF"
    return "#000000"


class WinampSkin:
    """The files of one classic skin, read from a .wsz archive."""

    def __init__(self, files):
        """Create a skin from a dict of lowercase file name to bytes."""
        self.files = files

    @classmethod
    def from_bytes(cls, data):
        """Read a .wsz archive held in memory.

        Files are looked up by name alone, because many skins keep them in a
        subfolder inside the zip.

        Raises:
            SkinError: If the data is not a readable zip archive.
        """
        wanted = set(SPRITE_SHEETS) | {"pledit.txt", "viscolor.txt"}
        files = {}
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                for entry in archive.infolist():
                    name = entry.filename.replace("\\", "/").split("/")[-1].lower()
                    if name in wanted and entry.file_size <= MAX_FILE_BYTES:
                        files[name] = archive.read(entry)
        except zipfile.BadZipFile as error:
            raise SkinError(f"Not a valid skin archive: {error}") from error
        return cls(files)

    def sprite_sheet(self, name):
        """Return the bytes of a sprite sheet such as "main.bmp", or None."""
        return self.files.get(name)

    def playlist_style(self):
        """Return the playlist colors and font, or None if there is no pledit.txt."""
        text = self.files.get("pledit.txt")
        if text is None:
            return None
        return parse_pledit_txt(text.decode("latin-1"))

    def vis_colors(self):
        """Return the visualizer colors, or None if there is no viscolor.txt."""
        text = self.files.get("viscolor.txt")
        if text is None:
            return None
        return parse_viscolor_txt(text.decode("latin-1"))


def download_file(url):
    """Download a URL and return its bytes.

    Raises:
        SkinError: If the download fails or is too large.
    """
    try:
        response = requests.get(url, timeout=DOWNLOAD_TIMEOUT_SECONDS)
        response.raise_for_status()
    except requests.RequestException as error:
        raise SkinError(f"Could not download skin from {url}: {error}") from error
    if len(response.content) > MAX_SKIN_BYTES:
        raise SkinError(f"Skin at {url} is larger than {MAX_SKIN_BYTES} bytes")
    return response.content


class SkinRegistry:
    """The skins listed in skins.json, downloaded and read on first use.

    Usage:
        registry = SkinRegistry()
        for info in registry.skins():
            print(info.id, info.name)
        sheet = registry.sprite_sheet("garfield", "pledit.bmp")
    """

    def __init__(
        self,
        registry_path=REGISTRY_PATH,
        cache_directory=SKIN_CACHE_DIRECTORY,
        download=download_file,
    ):
        """Load the skin list.

        Args:
            registry_path: The skins.json file.
            cache_directory: Where downloaded .wsz files are kept.
            download: Function that takes a URL and returns bytes; tests
                replace it.
        """
        data = json.loads(Path(registry_path).read_text(encoding="utf-8"))
        self.default_skin_id = data["default_skin"]
        self._skins = {entry["id"]: SkinInfo(**entry) for entry in data["skins"]}
        self.cache_directory = Path(cache_directory)
        self.download = download
        self._loaded = {}
        self._lock = threading.Lock()

    def skins(self):
        """Return every SkinInfo, in skins.json order."""
        return list(self._skins.values())

    def get(self, skin_id):
        """Return the SkinInfo for an ID, or None if there is no such skin."""
        return self._skins.get(skin_id)

    def sprite_sheet(self, skin_id, name):
        """Return a sprite sheet's bytes, borrowing from the default skin if missing.

        Raises:
            SkinError: If the skin or sheet name is unknown, or loading fails.
        """
        if name not in SPRITE_SHEETS:
            raise SkinError(f"Unknown sprite sheet {name!r}")
        data = self.load(skin_id).sprite_sheet(name)
        if data is None and skin_id != self.default_skin_id:
            data = self.load(self.default_skin_id).sprite_sheet(name)
        if data is None:
            raise SkinError(f"Skin {skin_id!r} has no {name}")
        return data

    def playlist_style(self, skin_id):
        """Return the skin's playlist colors and font, falling back to the default."""
        style = self.load(skin_id).playlist_style()
        if style is None and skin_id != self.default_skin_id:
            style = self.load(self.default_skin_id).playlist_style()
        if style is None:
            style = parse_pledit_txt("")
        return style

    def vis_colors(self, skin_id):
        """Return the skin's visualizer colors, falling back to the default skin."""
        colors = self.load(skin_id).vis_colors()
        if colors is None and skin_id != self.default_skin_id:
            colors = self.load(self.default_skin_id).vis_colors()
        if colors is None:
            colors = parse_viscolor_txt("")
        return colors

    def load(self, skin_id):
        """Return the WinampSkin for an ID, downloading it the first time.

        Raises:
            SkinError: If the skin is unknown or cannot be read.
        """
        info = self.get(skin_id)
        if info is None:
            raise SkinError(f"Unknown skin {skin_id!r}")
        with self._lock:
            if skin_id not in self._loaded:
                self._loaded[skin_id] = WinampSkin.from_bytes(self._read_archive(info))
            return self._loaded[skin_id]

    def _read_archive(self, info):
        """Return the .wsz bytes for a skin from its local file or the cache.

        Downloaded skins are saved in the cache directory so each is fetched
        only once.
        """
        if info.file:
            try:
                return (PROJECT_ROOT / info.file).read_bytes()
            except OSError as error:
                raise SkinError(
                    f"Could not read skin file {info.file}: {error}"
                ) from error
        cached_path = self.cache_directory / f"{info.id}.wsz"
        if cached_path.exists():
            return cached_path.read_bytes()
        if not info.url:
            raise SkinError(f"Skin {info.id!r} has neither a url nor a file")
        data = self.download(info.url)
        self.cache_directory.mkdir(parents=True, exist_ok=True)
        cached_path.write_bytes(data)
        return data
