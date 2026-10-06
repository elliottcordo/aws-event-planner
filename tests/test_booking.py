"""Tests for choosing which favorites to reserve."""

import unittest

from aws_events.booking import (
    ids_to_retry,
    is_interactive,
    reserve_in_batches,
    reserve_wave_times,
    session_ids_waiting_to_book,
    should_retry_failure,
)
from aws_events.catalog import SessionCatalog
from aws_events.client import EventsClient
from tests.fakes import FakeAuthenticator, FakeTransport, make_session


class InteractiveTests(unittest.TestCase):
    """Limited-seat session types."""

    def test_chalk_talk_is_interactive(self):
        """Chalk talks need a reservation."""
        self.assertTrue(is_interactive({"type": "Chalk talk"}))
        self.assertTrue(is_interactive({"type": "Workshop"}))
        self.assertTrue(is_interactive({"type": "Builders' session"}))
        self.assertFalse(is_interactive({"type": "Breakout session"}))


class WaitingToBookTests(unittest.TestCase):
    """Favorites that are not reserved yet."""

    def test_skips_already_reserved_and_breakouts(self):
        """Keep interactive favorites that are not already booked."""
        catalog = SessionCatalog(
            "e1",
            [
                make_session("a", "A", type="Chalk talk"),
                make_session("b", "B", type="Breakout session"),
                make_session("c", "C", type="Workshop"),
            ],
        )
        schedule = {
            "reserved": ["a"],
            "favorites": ["a", "b", "c"],
            "personalTime": [],
        }

        waiting = session_ids_waiting_to_book(schedule, catalog)

        self.assertEqual(waiting, ["c"])

    def test_all_favorites_when_not_interactive_only(self):
        """--all keeps breakouts that are not booked."""
        catalog = SessionCatalog(
            "e1", [make_session("b", "B", type="Breakout session")]
        )
        schedule = {"reserved": [], "favorites": ["b"], "personalTime": []}

        waiting = session_ids_waiting_to_book(schedule, catalog, interactive_only=False)

        self.assertEqual(waiting, ["b"])


class RetryTests(unittest.TestCase):
    """Which failures are worth another wave."""

    def test_full_sessions_are_retried_conflicts_are_not(self):
        """A full session may open later; a clash will not."""
        self.assertTrue(should_retry_failure({"code": "sessionFull"}))
        self.assertFalse(should_retry_failure({"code": "alreadyScheduled"}))
        self.assertFalse(should_retry_failure({"code": "scheduleConflict"}))
        self.assertEqual(
            ids_to_retry(
                [
                    {"sessionId": "full", "code": "sessionFull"},
                    {"sessionId": "held", "code": "alreadyScheduled"},
                ]
            ),
            ["full"],
        )

    def test_failures_without_an_id_are_skipped(self):
        """A failure missing sessionId cannot be retried and does not raise."""
        self.assertEqual(ids_to_retry([{"code": "sessionFull"}]), [])


class BatchReserveTests(unittest.TestCase):
    """More than ten IDs are split across API calls."""

    def test_empty_list_does_not_call_the_api(self):
        """No request is sent when there is nothing to book."""
        transport = FakeTransport()
        client = EventsClient(transport, FakeAuthenticator())

        result = reserve_in_batches(client, "e1", [])

        self.assertEqual(result, {"successful": [], "failed": []})
        self.assertEqual(transport.requests, [])

    def test_splits_after_ten_ids(self):
        """Eleven IDs become a batch of ten and a batch of one."""
        ids = [f"s{n}" for n in range(11)]
        transport = FakeTransport(
            {"result": {"successful": ids[:10], "failed": []}},
            {"result": {"successful": ids[10:], "failed": []}},
        )
        client = EventsClient(transport, FakeAuthenticator())

        result = reserve_in_batches(client, "e1", ids)

        self.assertEqual(result["successful"], ids)
        self.assertEqual(len(transport.requests), 2)
        self.assertEqual(len(transport.requests[0]["json_body"]["sessionIds"]), 10)
        self.assertEqual(transport.requests[1]["json_body"]["sessionIds"], ["s10"])


class WaveTimeTests(unittest.TestCase):
    """Published Pacific release times."""

    def test_nine_am_and_five_pm_pacific(self):
        """The two waves are 9:00 and 17:00 in America/Los_Angeles."""
        waves = reserve_wave_times()
        self.assertEqual(len(waves), 2)
        self.assertEqual(waves[0].hour, 9)
        self.assertEqual(waves[1].hour, 17)
        self.assertEqual(str(waves[0].tzinfo), "America/Los_Angeles")


if __name__ == "__main__":
    unittest.main()
