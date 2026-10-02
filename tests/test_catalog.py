"""Tests for aws_events.catalog."""

import tempfile
import unittest
from pathlib import Path

from aws_events.catalog import (
    SessionCatalog,
    SessionFilter,
    chronological_key,
    default_catalog_path,
)
from aws_events.client import EventsClient
from tests.fakes import FakeAuthenticator, FakeTransport, make_session


class SessionCatalogTests(unittest.TestCase):
    """Downloading, saving and loading the session catalog."""

    def test_download_fetches_all_pages(self):
        """download collects sessions from every page."""
        transport = FakeTransport(
            {
                "items": [make_session("s1", "One")],
                "totalCount": 2,
                "nextToken": "next",
            },
            {"items": [make_session("s2", "Two")], "totalCount": 2},
        )
        client = EventsClient(transport, FakeAuthenticator())

        catalog = SessionCatalog.download(client, "e1")

        self.assertEqual(len(catalog), 2)
        self.assertEqual(catalog.event_id, "e1")
        self.assertIsNotNone(catalog.downloaded_at)

    def test_save_and_load_round_trip(self):
        """A saved catalog loads back with the same sessions."""
        catalog = SessionCatalog("e1", [make_session("s1", "One")], "2026-09-26")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "sessions.json"
            catalog.save(path)
            loaded = SessionCatalog.load(path)

        self.assertEqual(loaded.event_id, "e1")
        self.assertEqual(loaded.downloaded_at, "2026-09-26")
        self.assertEqual(loaded.get("s1")["title"], "One")
        self.assertIsNone(loaded.get("missing"))

    def test_default_path_is_in_project_data_folder(self):
        """The default file is data/EVENT-sessions.json at the project root."""
        project_root = Path(__file__).resolve().parent.parent
        expected = project_root / "data" / "e1-sessions.json"
        self.assertEqual(default_catalog_path("e1"), expected)


def scheduled_session(session_id, venue, date, time, title="Talk"):
    """Return a session with a venue and a start date and time."""
    return make_session(
        session_id, title, venue=venue, sessionTime={"date": date, "time": time}
    )


class SessionFilterTests(unittest.TestCase):
    """Matching sessions by venue and date."""

    def setUp(self):
        """Create a catalog spread over two venues and two days."""
        self.catalog = SessionCatalog(
            "e1",
            [
                scheduled_session("late", "MGM Grand", "2026-12-02", "15:00"),
                scheduled_session("early", "MGM Grand", "2026-12-02", "09:00"),
                scheduled_session("other_day", "MGM Grand", "2026-12-03", "09:00"),
                scheduled_session("other_venue", "Venetian", "2026-12-02", "10:00"),
                make_session("unscheduled", "No time or venue yet"),
            ],
        )

    def test_empty_filter_matches_everything(self):
        """A filter with no criteria matches every session."""
        session_filter = SessionFilter()
        self.assertTrue(session_filter.is_empty())
        self.assertEqual(len(self.catalog.filter_sessions(session_filter)), 5)

    def test_venue_and_date_must_both_match(self):
        """With both criteria set, only sessions meeting both are returned."""
        session_filter = SessionFilter(venue="MGM Grand", date="2026-12-02")
        matching = self.catalog.filter_sessions(session_filter)
        self.assertEqual([s["sessionId"] for s in matching], ["early", "late"])

    def test_date_only(self):
        """A date filter alone ignores venue."""
        matching = self.catalog.filter_sessions(SessionFilter(date="2026-12-02"))
        self.assertEqual(
            [s["sessionId"] for s in matching], ["early", "other_venue", "late"]
        )

    def test_venues_and_dates_lists(self):
        """venues() and dates() list distinct values and skip missing ones."""
        self.assertEqual(self.catalog.venues(), ["MGM Grand", "Venetian"])
        self.assertEqual(self.catalog.dates(), ["2026-12-02", "2026-12-03"])

    def test_type_and_level(self):
        """Type and level filters match exactly; types() and levels() list them."""
        catalog = SessionCatalog(
            "e1",
            [
                make_session("a", "A", type="Workshop", level="300 - Advanced"),
                make_session("b", "B", type="Chalk talk", level="300 - Advanced"),
                make_session("c", "C", type="Workshop", level="100 - Foundational"),
                make_session("d", "D"),
            ],
        )
        self.assertEqual(catalog.types(), ["Chalk talk", "Workshop"])
        self.assertEqual(catalog.levels(), ["100 - Foundational", "300 - Advanced"])
        both = SessionFilter(session_type="Workshop", level="300 - Advanced")
        self.assertEqual([s["sessionId"] for s in catalog.filter_sessions(both)], ["a"])
        self.assertFalse(both.is_empty())
        self.assertTrue(SessionFilter().is_empty())

    def test_venues_named_only_in_the_room_count(self):
        """Sessions with no venue field are listed and filtered by their room's venue."""
        catalog = SessionCatalog(
            "e1",
            [
                make_session(
                    "wynn", "Wynn talk", room="Wynn/Encore | Level 1 | Latour 7"
                ),
                make_session("mgm", "MGM talk", venue="MGM Grand", room="Level 3"),
            ],
        )
        self.assertEqual(catalog.venues(), ["MGM Grand", "Wynn/Encore"])
        matching = catalog.filter_sessions(SessionFilter(venue="Wynn/Encore"))
        self.assertEqual([s["sessionId"] for s in matching], ["wynn"])

    def test_unscheduled_sessions_sort_last(self):
        """Sessions without a date sort after scheduled ones."""
        ordered = sorted(self.catalog.sessions, key=chronological_key)
        self.assertEqual(ordered[-1]["sessionId"], "unscheduled")


if __name__ == "__main__":
    unittest.main()
