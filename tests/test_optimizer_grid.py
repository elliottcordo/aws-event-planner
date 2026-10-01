"""Tests for web.optimizer, the schedule optimizer's grid."""

import unittest

from aws_events.catalog import SessionCatalog
from aws_events.schedule_optimizer import PlannedSession, plan_day
from tests.fakes import make_session
from web.optimizer import (
    CSV_COLUMNS,
    build_grid,
    describe_moves,
    favorites_csv,
    parse_max_venues,
)


def timed_session(session_id, venue, date, time, length="60"):
    """Return a catalog session at a venue, date and time."""
    return make_session(session_id, f"Talk {session_id}",
                        abbreviation=f"CODE-{session_id}", venue=venue,
                        sessionTime={"date": date, "time": time, "length": length})


class GridTests(unittest.TestCase):
    """Building the grid from a schedule."""

    def setUp(self):
        """Create two days of sessions; day two has a clash."""
        self.catalog = SessionCatalog("e1", [
            timed_session("a", "Venetian", "2026-12-01", "09:00"),
            timed_session("b", "Venetian", "2026-12-02", "09:00"),
            timed_session("c", "Venetian", "2026-12-02", "09:30"),
            timed_session("d", "MGM Grand", "2026-12-02", "13:00"),
            make_session("untimed", "No time yet"),
        ])
        self.schedule = {
            "reserved": ["d"],
            "favorites": ["a", "b", "c", "untimed"],
            "personalTime": [],
        }

    def test_fresh_grid_shows_everything(self):
        """Before optimizing, every timed session is shown and none removed."""
        grid = build_grid(self.schedule, self.catalog, set(), {}, optimize=False)
        self.assertEqual([day.label for day in grid.days], ["Tue 1 Dec", "Wed 2 Dec"])
        self.assertEqual(grid.total_count, 4)
        self.assertEqual(grid.kept_count(), 4)
        self.assertEqual(grid.remove_ids, [])
        self.assertEqual(grid.book_ids, ["a", "b", "c"])
        self.assertEqual(grid.unplanned_count, 1)
        self.assertEqual(grid.days[0].note, "")

    def test_rows_show_times_and_flags(self):
        """Each row has its start and end time and the must-attend tick."""
        grid = build_grid(self.schedule, self.catalog, {"a"}, {}, optimize=False)
        row = grid.days[0].rows[0]
        self.assertEqual((row["start"], row["end"]), ("09:00", "10:00"))
        self.assertTrue(row["must_attend"])
        self.assertTrue(row["favorite"])
        self.assertEqual(row["venue"], "Venetian")

    def test_optimizing_drops_clashes(self):
        """The optimizer drops the clashing favorite; it is listed for removal."""
        grid = build_grid(self.schedule, self.catalog, set(), {}, optimize=True)
        second_day = [row["id"] for row in grid.days[1].rows]
        self.assertEqual(second_day, ["b", "d"])
        self.assertEqual(grid.remove_ids, ["c"])
        self.assertEqual(grid.kept_count(), 3)
        self.assertIn("1 move: Venetian → MGM Grand", grid.days[1].note)

    def test_must_attend_steers_the_plan(self):
        """Ticking c keeps it instead of the clashing b."""
        grid = build_grid(self.schedule, self.catalog, {"c"}, {}, optimize=True)
        second_day = [row["id"] for row in grid.days[1].rows]
        self.assertEqual(second_day, ["c", "d"])
        self.assertEqual(grid.remove_ids, ["b"])

    def test_booked_sessions_are_locked_must_attend(self):
        """A booked session is kept and locked, even with nothing ticked."""
        grid = build_grid(self.schedule, self.catalog, set(),
                          {"2026-12-02": 1}, optimize=True)
        rows = grid.days[1].rows
        self.assertEqual([row["id"] for row in rows], ["d"])
        self.assertTrue(rows[0]["locked"])
        self.assertTrue(rows[0]["must_attend"])
        self.assertEqual(grid.remove_ids, ["b", "c"])
        self.assertEqual(grid.days[1].max_venues, 1)
        self.assertEqual(grid.days[1].note, "Stays at MGM Grand")

    def test_unbooked_rows_are_not_locked(self):
        """Only booked sessions are locked."""
        grid = build_grid(self.schedule, self.catalog, set(), {}, optimize=False)
        self.assertFalse(grid.days[0].rows[0]["locked"])
        self.assertFalse(grid.days[0].rows[0]["must_attend"])

    def test_booked_favorite_is_never_removed(self):
        """If two bookings clash, the one left out stays a favorite."""
        self.schedule["reserved"] = ["b", "c"]
        grid = build_grid(self.schedule, self.catalog, set(), {}, optimize=True)
        self.assertEqual(grid.missed_must_attend, ["CODE-c"])
        self.assertNotIn("c", grid.remove_ids)

    def test_clashing_must_attends_are_named(self):
        """Must-attend sessions that could not fit are named by code."""
        grid = build_grid(self.schedule, self.catalog, {"b", "c"}, {}, optimize=True)
        self.assertEqual(grid.missed_must_attend, ["CODE-c"])


class FavoritesCsvTests(unittest.TestCase):
    """The favorites backup."""

    def test_lists_favorites_only_in_time_order(self):
        """Every favorite is a row, booked or not; bookings alone are left out."""
        catalog = SessionCatalog("e1", [
            timed_session("late", "MGM Grand", "2026-12-02", "13:00"),
            timed_session("early", "Venetian", "2026-12-01", "09:00", length="120"),
            timed_session("booked-only", "Venetian", "2026-12-01", "11:00"),
        ])
        schedule = {
            "reserved": ["late", "booked-only"],
            "favorites": ["late", "early", "missing"],
            "personalTime": [],
        }
        lines = favorites_csv(schedule, catalog).splitlines()
        self.assertEqual(lines[0], ",".join(CSV_COLUMNS))
        self.assertEqual(
            lines[1], "early,CODE-early,Talk early,2026-12-01,09:00,120,Venetian,,no"
        )
        self.assertTrue(lines[2].startswith("late,") and lines[2].endswith(",yes"))
        self.assertTrue(lines[3].startswith("missing,,"))
        self.assertEqual(len(lines), 4)

    def test_quotes_commas_in_titles(self):
        """Titles with commas are quoted so the CSV still lines up."""
        catalog = SessionCatalog("e1", [make_session("x", "Fast, cheap")])
        schedule = {"reserved": [], "favorites": ["x"], "personalTime": []}
        self.assertIn('"Fast, cheap"', favorites_csv(schedule, catalog))


class ParseMaxVenuesTests(unittest.TestCase):
    """Reading the venue limits sent by the page."""

    def test_reads_valid_values_and_skips_others(self):
        """Good values are kept; malformed or out-of-range ones are skipped."""
        result = parse_max_venues([
            "2026-12-01:1", "2026-12-02:3", "2026-12-03:9", "2026-12-04:x", "junk",
        ])
        self.assertEqual(result, {"2026-12-01": 1, "2026-12-02": 3})


class DescribeMovesTests(unittest.TestCase):
    """The note under each optimized day."""

    def test_two_moves(self):
        """Every stop is listed with the total travel time."""
        sessions = [
            PlannedSession.from_session(
                timed_session("a", "Venetian", "2026-12-01", "09:00")),
            PlannedSession.from_session(
                timed_session("b", "Caesars Forum", "2026-12-01", "10:30")),
            PlannedSession.from_session(
                timed_session("c", "MGM Grand", "2026-12-01", "12:30")),
        ]
        note = describe_moves(plan_day(sessions, max_venues=3))
        self.assertEqual(
            note, "2 moves: Venetian → Caesars Forum → MGM Grand (90 min travel)"
        )

    def test_empty_day(self):
        """A day with nothing kept has no note."""
        self.assertEqual(describe_moves(plan_day([])), "")


if __name__ == "__main__":
    unittest.main()
