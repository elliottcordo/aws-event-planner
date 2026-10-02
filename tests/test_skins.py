"""Tests for web.skins."""

import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from web.skins import (
    SkinError,
    SkinRegistry,
    WinampSkin,
    contrast_ratio,
    parse_pledit_txt,
    parse_viscolor_txt,
)


def make_wsz(files):
    """Return the bytes of a zip archive holding the given {name: bytes}."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


BASE_WSZ = make_wsz(
    {
        "main.bmp": b"base-main",
        "pledit.bmp": b"base-pledit",
        "titlebar.bmp": b"base-titlebar",
        "text.bmp": b"base-text",
        "cbuttons.bmp": b"base-cbuttons",
        "pledit.txt": b"[Text]\r\nNormal=#00FF00\r\nNormalBG=#000000\r\n",
        "viscolor.txt": b"10,20,30, // background\r\n",
    }
)
# A skin with its files in a subfolder, no pledit.txt and no cbuttons.bmp.
PARTIAL_WSZ = make_wsz(
    {
        "Partial Skin/MAIN.BMP": b"partial-main",
        "Partial Skin/pledit.bmp": b"partial-pledit",
    }
)


class PleditTests(unittest.TestCase):
    """Reading colors and font from pledit.txt."""

    def test_keys_are_case_insensitive_and_values_trimmed(self):
        """Odd key case and trailing tabs, as found in real skins, still work."""
        style = parse_pledit_txt(
            "[Text]\r\nnormal=#bfbf00\t\t\r\nNORMALBG=#000000\r\n"
            "SelectedBG=#666600\r\nFont=MS UI Gothic\r\n"
        )
        self.assertEqual(style["normal"], "#BFBF00")
        self.assertEqual(style["normal_bg"], "#000000")
        self.assertEqual(style["selected_bg"], "#666600")
        self.assertEqual(style["font"], "MS UI Gothic")

    def test_bad_values_fall_back_to_defaults(self):
        """Invalid colors and fonts that could inject CSS are ignored."""
        style = parse_pledit_txt("Normal=red\nFont=Arial; } body { display: none\n")
        self.assertEqual(style["normal"], "#00FF00")
        self.assertEqual(style["font"], "Arial")

    def test_text_color_is_readable(self):
        """Low-contrast normal text is swapped for the current color."""
        style = parse_pledit_txt("Normal=#800000\nCurrent=#C0C0C0\nNormalBG=#000000\n")
        self.assertEqual(style["text"], "#C0C0C0")

    def test_text_color_falls_back_to_white_or_black(self):
        """If neither skin color is readable, white or black is used."""
        style = parse_pledit_txt("Normal=#111111\nCurrent=#222222\nNormalBG=#000000\n")
        self.assertEqual(style["text"], "#FFFFFF")

    def test_contrast_ratio(self):
        """Black on white is the maximum contrast of 21."""
        self.assertAlmostEqual(contrast_ratio("#000", "#FFFFFF"), 21.0)
        self.assertAlmostEqual(contrast_ratio("#777777", "#777777"), 1.0)


class ViscolorTests(unittest.TestCase):
    """Reading the spectrum analyzer colors from viscolor.txt."""

    def test_reads_colors_in_order_with_comments(self):
        """Each line gives the next color; comments and spacing are fine."""
        colors = parse_viscolor_txt("0,0,0, // background\r\n 24, 33, 41 // dots\r\n")
        self.assertEqual(colors[:2], ["#000000", "#182129"])
        self.assertEqual(len(colors), 24)

    def test_missing_and_bad_values_use_defaults(self):
        """An empty file gives Winamp's colors; a bad line is skipped."""
        defaults = parse_viscolor_txt("")
        self.assertEqual(defaults[2], "#EF3110")
        colors = parse_viscolor_txt("junk\n255,255,255\n999,0,0\n")
        self.assertEqual(colors[0], "#FFFFFF")
        # 999 is out of range: color 1 keeps its default, and the slot is used.
        self.assertEqual(colors[1], defaults[1])
        self.assertEqual(colors[2], defaults[2])

    def test_extra_lines_are_ignored(self):
        """Only the first 24 colors count."""
        text = "\n".join(["1,1,1"] * 30)
        self.assertEqual(parse_viscolor_txt(text), ["#010101"] * 24)


class WinampSkinTests(unittest.TestCase):
    """Reading .wsz archives."""

    def test_files_found_in_subfolders_any_case(self):
        """MAIN.BMP inside a folder is found as main.bmp."""
        skin = WinampSkin.from_bytes(PARTIAL_WSZ)
        self.assertEqual(skin.sprite_sheet("main.bmp"), b"partial-main")
        self.assertIsNone(skin.sprite_sheet("cbuttons.bmp"))
        self.assertIsNone(skin.playlist_style())

    def test_not_a_zip(self):
        """Data that isn't a zip raises SkinError."""
        with self.assertRaises(SkinError):
            WinampSkin.from_bytes(b"not a zip")


class SkinRegistryTests(unittest.TestCase):
    """The skin list, downloads, caching and fallbacks."""

    def setUp(self):
        """Write a registry with a base skin and a partial skin."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.registry_path = self.directory / "skins.json"
        self.registry_path.write_text(
            json.dumps(
                {
                    "default_skin": "base",
                    "skins": [
                        {
                            "id": "base",
                            "name": "Base",
                            "url": "https://skins.test/base.wsz",
                        },
                        {
                            "id": "partial",
                            "name": "Partial",
                            "year": 2001,
                            "url": "https://skins.test/partial.wsz",
                        },
                    ],
                }
            )
        )
        self.downloads = []

    def fake_download(self, url):
        """Record the URL and return the matching test archive."""
        self.downloads.append(url)
        return BASE_WSZ if url.endswith("base.wsz") else PARTIAL_WSZ

    def make_registry(self):
        """Return a registry using the fake download and a temp cache."""
        return SkinRegistry(
            self.registry_path, self.directory / "cache", download=self.fake_download
        )

    def test_lists_skins_in_order(self):
        """skins() keeps the file's order and reads optional fields."""
        registry = self.make_registry()
        self.assertEqual([info.id for info in registry.skins()], ["base", "partial"])
        self.assertEqual(registry.get("partial").year, 2001)
        self.assertIsNone(registry.get("missing"))

    def test_vis_colors_are_borrowed_from_default_skin(self):
        """A skin without viscolor.txt uses the base skin's colors."""
        registry = self.make_registry()
        self.assertEqual(registry.vis_colors("base")[0], "#0A141E")
        self.assertEqual(registry.vis_colors("partial")[0], "#0A141E")

    def test_missing_files_are_borrowed_from_default_skin(self):
        """A missing sheet and missing pledit.txt come from the base skin."""
        registry = self.make_registry()
        self.assertEqual(registry.sprite_sheet("partial", "main.bmp"), b"partial-main")
        self.assertEqual(
            registry.sprite_sheet("partial", "cbuttons.bmp"), b"base-cbuttons"
        )
        self.assertEqual(registry.playlist_style("partial")["normal"], "#00FF00")

    def test_downloads_once_then_uses_cache(self):
        """Each skin is downloaded once, even across registry instances."""
        self.make_registry().sprite_sheet("partial", "main.bmp")
        self.make_registry().sprite_sheet("partial", "main.bmp")
        self.assertEqual(self.downloads, ["https://skins.test/partial.wsz"])
        self.assertTrue((self.directory / "cache" / "partial.wsz").exists())

    def test_local_file_skin(self):
        """A skin with a "file" entry is read from disk, not downloaded."""
        local_path = self.directory / "local.wsz"
        local_path.write_bytes(BASE_WSZ)
        self.registry_path.write_text(
            json.dumps(
                {
                    "default_skin": "local",
                    "skins": [
                        {"id": "local", "name": "Local", "file": str(local_path)}
                    ],
                }
            )
        )
        registry = self.make_registry()
        self.assertEqual(registry.sprite_sheet("local", "text.bmp"), b"base-text")
        self.assertEqual(self.downloads, [])

    def test_unknown_skin_or_sheet(self):
        """Unknown skins and sheet names raise SkinError."""
        registry = self.make_registry()
        with self.assertRaises(SkinError):
            registry.load("missing")
        with self.assertRaises(SkinError):
            registry.sprite_sheet("base", "../secret.txt")


class ShippedRegistryTests(unittest.TestCase):
    """The skins.json that ships with the app."""

    def test_shipped_registry_is_valid(self):
        """Every entry loads, IDs are unique, and the default skin exists."""
        registry = SkinRegistry(download=lambda url: self.fail("no downloads"))
        ids = [info.id for info in registry.skins()]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn(registry.default_skin_id, ids)
        self.assertEqual(len(ids), 11)  # The base skin plus the top 10.
        for info in registry.skins():
            self.assertTrue(info.url or info.file, info.id)


if __name__ == "__main__":
    unittest.main()
