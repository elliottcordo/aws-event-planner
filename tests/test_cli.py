"""Tests for the events_cli command-line interface."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from langchain_core.embeddings import DeterministicFakeEmbedding

import events_cli
from aws_events.client import EventsClient
from aws_events.errors import ApiError, EventsError
from tests.fakes import FakeAuthenticator, FakeTransport, make_session


def run(argv, *responses):
    """Parse argv, run it against fakes, and return (result, transport)."""
    transport = FakeTransport(*responses)
    authenticator = FakeAuthenticator()
    client = EventsClient(transport, authenticator)
    args = events_cli.build_parser().parse_args(argv)
    embeddings = DeterministicFakeEmbedding(size=16)
    result = events_cli.run_command(args, client, authenticator, embeddings)
    return result, transport


class CommandTests(unittest.TestCase):
    """Each subcommand calls the right client method."""

    def test_events(self):
        """events prints the event list."""
        result, _ = run(["events"], {"items": [{"eventId": "e1"}]})
        self.assertEqual(result, [{"eventId": "e1"}])

    def test_sessions_without_abstracts(self):
        """--no-abstracts turns off abstracts in the request."""
        result, transport = run(
            ["sessions", "e1", "--no-abstracts"], {"items": [], "totalCount": 0}
        )
        self.assertEqual(result, [])
        self.assertIn("includeAbstracts=false", transport.requests[0]["url"])

    def test_reserve_passes_all_ids(self):
        """reserve sends every session ID given."""
        _, transport = run(
            ["reserve", "e1", "s1", "s2"],
            {"result": {"successful": ["s1", "s2"], "failed": []}},
        )
        self.assertEqual(
            transport.requests[0]["json_body"], {"sessionIds": ["s1", "s2"]}
        )

    def test_schedule_details_fetches_sessions(self):
        """schedule --details replaces session IDs with sessions."""
        schedule = {"reserved": ["s1"], "favorites": [], "personalTime": []}
        result, transport = run(
            ["schedule", "e1", "--details"],
            {"schedule": schedule},
            {"session": {"sessionId": "s1"}},
        )
        self.assertEqual(result["reserved"], [{"sessionId": "s1"}])
        self.assertEqual(len(transport.requests), 2)

    def test_book_and_unbook_aliases(self):
        """ "book" and "unbook" run the reserve and cancel commands."""
        _, transport = run(
            ["book", "e1", "s1"], {"result": {"successful": ["s1"], "failed": []}}
        )
        self.assertEqual(transport.requests[0]["method"], "POST")
        self.assertTrue(transport.requests[0]["url"].endswith("/reservations"))

        result, transport = run(["unbook", "e1", "s1"])
        self.assertEqual(result, "Reservation cancelled.")
        self.assertEqual(transport.requests[0]["method"], "DELETE")

    def test_cancel_returns_message(self):
        """cancel sends DELETE and returns a confirmation."""
        result, transport = run(["cancel", "e1", "s1"])
        self.assertEqual(result, "Reservation cancelled.")
        self.assertEqual(transport.requests[0]["method"], "DELETE")

    def test_book_favorites_all_reserves_unbooked_ids(self):
        """book-favorites --all books favorites that are not already reserved."""
        result, transport = run(
            ["book-favorites", "e1", "--all"],
            {
                "schedule": {
                    "reserved": ["s1"],
                    "favorites": ["s1", "s2"],
                    "personalTime": [],
                }
            },
            {"result": {"successful": ["s2"], "failed": []}},
        )
        self.assertEqual(result["successful"], ["s2"])
        self.assertEqual(transport.requests[1]["json_body"], {"sessionIds": ["s2"]})

    def test_add_personal_time(self):
        """add-personal-time sends the block, including location."""
        argv = [
            "add-personal-time",
            "e1",
            "--title",
            "Lunch",
            "--description",
            "Team lunch",
            "--start",
            "2026-12-02T19:00:00",
            "--end",
            "2026-12-02T20:00:00",
            "--location",
            "Venetian",
        ]
        result, transport = run(argv)
        self.assertEqual(result, "Personal time added.")
        self.assertEqual(transport.requests[0]["json_body"]["location"], "Venetian")


class SearchCommandTests(unittest.TestCase):
    """download-sessions, build-index and search, end to end with fakes."""

    def test_download_index_and_search(self):
        """The three commands chain together through files on disk."""
        page = {
            "items": [
                make_session(
                    "s1", "Serverless patterns", "Lambda and SQS", abbreviation="SVS301"
                ),
                make_session("s2", "Vector databases", "Embeddings"),
            ],
            "totalCount": 2,
        }
        with tempfile.TemporaryDirectory() as directory:
            paths = [
                "--catalog",
                str(Path(directory) / "sessions.json"),
                "--index",
                str(Path(directory) / "vectors.json"),
            ]
            downloaded, _ = run(["download-sessions", "e1", *paths[:2]], page)
            indexed, _ = run(["build-index", "e1", *paths])
            found, _ = run(["search", "e1", "lambda", "--mode", "keyword", *paths])

        self.assertIn("Saved 2 sessions", downloaded)
        self.assertIn("Indexed 2 sessions", indexed)
        self.assertIn("[SVS301] Serverless patterns", found)
        self.assertNotIn("Vector databases", found)

    def test_search_without_catalog_explains_next_step(self):
        """A missing catalog raises an error that names download-sessions."""
        with tempfile.TemporaryDirectory() as directory:
            missing = str(Path(directory) / "none.json")
            with self.assertRaisesRegex(EventsError, "download-sessions"):
                run(["search", "e1", "x", "--catalog", missing])


class MainTests(unittest.TestCase):
    """Exit codes and error printing."""

    def test_disabled_feature_message(self):
        """A disabled operation prints the friendly message and exits with 1."""
        disabled = ApiError("This operation is currently disabled", 409)
        with (
            mock.patch("aws_events.HttpTransport.request", side_effect=disabled),
            mock.patch(
                "aws_events.Authenticator.get_access_token", return_value="token"
            ),
            mock.patch("sys.stderr") as stderr,
        ):
            self.assertEqual(events_cli.main(["book", "e1", "s1"]), 1)
        stderr.write.assert_any_call("Error: This feature is not yet enabled")

    def test_error_returns_exit_code_one(self):
        """Errors are printed to stderr and give exit code 1."""
        with (
            mock.patch("events_cli.run_command", side_effect=ValueError("bad")),
            mock.patch("sys.stderr") as stderr,
        ):
            self.assertEqual(events_cli.main(["schedule", "e1"]), 1)
        stderr.write.assert_any_call("Error: bad")


if __name__ == "__main__":
    unittest.main()
