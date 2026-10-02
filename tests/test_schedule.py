"""Tests for aws_events.schedule."""

import unittest

from aws_events.catalog import SessionCatalog
from aws_events.schedule import group_schedule_by_date
from tests.fakes import make_session


def scheduled_session(session_id, date, time):
    """Return a session starting at the given date and time."""
    return make_session(
        session_id, f"Talk {session_id}", sessionTime={"date": date, "time": time}
    )


class ScheduleTests(unittest.TestCase):
    """Grouping and labelling the attendee's schedule."""

    def setUp(self):
        """Create a catalog and a schedule spread over two days."""
        self.catalog = SessionCatalog(
            "e1",
            [
                scheduled_session("b", "2026-12-02", "14:00"),
                scheduled_session("a", "2026-12-02", "09:00"),
                scheduled_session("c", "2026-12-01", "10:00"),
            ],
        )
        self.schedule = {
            "reserved": ["a", "c"],
            "favorites": ["a", "b", "missing"],
            "personalTime": [],
        }

    def test_groups_by_day_in_time_order(self):
        """Days are in order, sessions by start time, unknown IDs last."""
        groups = group_schedule_by_date(self.schedule, self.catalog)

        days = [day for day, _ in groups]
        self.assertEqual(days, ["2026-12-01", "2026-12-02", None])
        second_day_ids = [entry.session["sessionId"] for entry in groups[1][1]]
        self.assertEqual(second_day_ids, ["a", "b"])

    def test_statuses(self):
        """Each entry knows whether it is reserved, a favorite, or both."""
        groups = group_schedule_by_date(self.schedule, self.catalog)
        labels = {
            entry.session["sessionId"]: entry.status_label()
            for _, entries in groups
            for entry in entries
        }
        self.assertEqual(
            labels,
            {
                "a": "Reserved, favorite",
                "b": "Favorite",
                "c": "Reserved",
                "missing": "Favorite",
            },
        )

    def test_without_catalog_sessions_are_shown_by_id(self):
        """With no local catalog, every session is listed by ID, undated."""
        groups = group_schedule_by_date(self.schedule, catalog=None)
        self.assertEqual(len(groups), 1)
        self.assertIsNone(groups[0][0])
        self.assertIn("(not in the local catalog)", groups[0][1][0].session["title"])

    def test_empty_schedule(self):
        """An empty schedule gives no groups."""
        empty = {"reserved": [], "favorites": [], "personalTime": []}
        self.assertEqual(group_schedule_by_date(empty, self.catalog), [])


if __name__ == "__main__":
    unittest.main()
