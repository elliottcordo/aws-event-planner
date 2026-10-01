"""Tests for web.views."""

import unittest

from aws_events.catalog import SessionCatalog
from tests.fakes import make_session
from web.views import (
    describe_bulk_result,
    describe_failure,
    event_year,
    session_row,
    short_level,
)


CATALOG = SessionCatalog("e1", [
    make_session("s1", "One", abbreviation="ANT301"),
    make_session("s2", "Two", abbreviation="DAT317"),
])


class DescribeTests(unittest.TestCase):
    """Plain-language messages for bulk results."""

    def test_known_codes_read_as_sentences(self):
        """Failure codes become sentences that start with the session code."""
        failure = {"sessionId": "s1", "code": "sessionFull"}
        self.assertEqual(describe_failure(failure, CATALOG), "ANT301 is full.")

    def test_unknown_code_and_session(self):
        """Unrecognized codes and sessions still give a sensible sentence."""
        failure = {"sessionId": "zz", "code": "somethingNew"}
        self.assertEqual(describe_failure(failure, CATALOG),
                         "zz was refused (somethingNew).")

    def test_conflict_lists_clashing_sessions(self):
        """A clash names the sessions it overlaps."""
        failure = {"sessionId": "s1", "code": "scheduleConflict",
                   "conflictsWith": ["s2"]}
        self.assertEqual(describe_failure(failure, CATALOG),
                         "ANT301 clashes with DAT317 on your schedule.")

    def test_kind_reflects_outcome(self):
        """All good is success, all failed is error, a mix is warning."""
        done = "Booked {count} session(s)."
        refused = {"sessionId": "s1", "code": "timePassed"}
        self.assertEqual(
            describe_bulk_result({"successful": ["s2"], "failed": []}, done, CATALOG),
            ("success", "Booked 1 session(s)."))
        self.assertEqual(
            describe_bulk_result({"successful": [], "failed": [refused]}, done,
                                 CATALOG)[0], "error")
        kind, message = describe_bulk_result(
            {"successful": ["s2"], "failed": [refused]}, done, CATALOG)
        self.assertEqual(kind, "warning")
        self.assertEqual(message, "Booked 1 session(s). ANT301 has already started.")


class TableFormatTests(unittest.TestCase):
    """Compact values for the results table."""

    def test_when_gives_day_and_time_separately(self):
        """The row has the day ("Wed 2 Dec") and start time apart, for two lines."""
        session = make_session("s1", "T", sessionTime={"date": "2026-12-02",
                                                       "time": "13:30"})
        row = session_row(session, {})
        self.assertEqual((row["day"], row["time"]), ("Wed 2 Dec", "13:30"))
        unscheduled = session_row(make_session("s2", "T"), {})
        self.assertEqual((unscheduled["day"], unscheduled["time"]), ("Date not set", ""))

    def test_short_level_keeps_only_the_number(self):
        """"300 - Advanced" becomes "300"; other text is kept as it is."""
        self.assertEqual(short_level("300 - Advanced"), "300")
        self.assertEqual(short_level("100 - Foundational"), "100")
        self.assertEqual(short_level("All levels"), "All levels")

    def test_no_level_becomes_not_applicable(self):
        """"No Level" (any case) and a missing level show as "N/A"."""
        self.assertEqual(short_level("No Level"), "N/A")
        self.assertEqual(short_level("no level"), "N/A")
        self.assertEqual(short_level(""), "N/A")

    def test_row_keeps_full_level_for_hover(self):
        """The row has the short level for display and the full one for hover."""
        row = session_row(make_session("s1", "T", level="400 - Expert"), {})
        self.assertEqual(row["level"], "400")
        self.assertEqual(row["level_full"], "400 - Expert")

    def test_null_fields_show_as_empty(self):
        """Fields the API sends as null show as empty (level as N/A), not "None"."""
        row = session_row(make_session("s1", "T", level=None, type=None), {})
        self.assertEqual(row["level"], "N/A")
        self.assertEqual(row["type"], "")



class EventYearTests(unittest.TestCase):
    """The year shown in the kbps and kHz readouts."""

    def test_year_from_first_session_date(self):
        """The catalog's first session date gives the year."""
        catalog = SessionCatalog("e1", [
            make_session("s1", "T", sessionTime={"date": "2025-12-01", "time": "09:00"}),
        ])
        self.assertEqual(event_year("reinvent2026", catalog), "2025")

    def test_year_from_event_id(self):
        """Without session dates, a year in the event ID is used."""
        self.assertEqual(event_year("reinvent2026"), "2026")
        self.assertEqual(event_year("summit-2027-nyc", SessionCatalog("e", [])), "2027")

    def test_no_year(self):
        """IDs without a plausible year give None."""
        self.assertIsNone(event_year("reinvent"))
        self.assertIsNone(event_year("event120261"))


if __name__ == "__main__":
    unittest.main()
