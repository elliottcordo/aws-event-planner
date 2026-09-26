"""Tests for aws_events.client."""

import unittest

from aws_events.client import (
    EventsClient,
    PersonalTime,
    build_path,
)
from aws_events.errors import ApiError
from tests.fakes import FakeAuthenticator, FakeTransport


BASE = "https://api.awsevents.com/v1"


def make_client(*responses):
    """Return a client wired to a FakeTransport, and the transport."""
    transport = FakeTransport(*responses)
    return EventsClient(transport, FakeAuthenticator()), transport


class EventTests(unittest.TestCase):
    """Public event calls."""

    def test_list_events_is_anonymous_and_unwraps_items(self):
        """list_events sends no token and returns the items list."""
        client, transport = make_client({"items": [{"eventId": "a"}]})

        events = client.list_events()

        self.assertEqual(events, [{"eventId": "a"}])
        request = transport.requests[0]
        self.assertEqual(request["method"], "GET")
        self.assertEqual(request["url"], f"{BASE}/events")
        self.assertNotIn("Authorization", request["headers"])

    def test_list_events_include_past(self):
        """include_past adds the includePast query parameter."""
        client, transport = make_client({"items": []})

        client.list_events(include_past=True)

        self.assertEqual(transport.requests[0]["url"],
                         f"{BASE}/events?includePast=true")

    def test_public_calls_work_without_authenticator(self):
        """Public calls succeed when no authenticator is given."""
        transport = FakeTransport({"event": {"eventId": "reinvent2026"}})
        client = EventsClient(transport)

        event = client.get_event("reinvent2026")

        self.assertEqual(event["eventId"], "reinvent2026")

    def test_signed_in_call_without_authenticator_raises(self):
        """Signed-in calls fail clearly when no authenticator is given."""
        client = EventsClient(FakeTransport())

        with self.assertRaises(ValueError):
            client.get_schedule("reinvent2026")


class SessionTests(unittest.TestCase):
    """Session catalog calls."""

    def test_list_sessions_follows_next_token(self):
        """list_sessions keeps fetching pages until nextToken is absent."""
        client, transport = make_client(
            {"items": [{"sessionId": "1"}], "totalCount": 2, "nextToken": "abc"},
            {"items": [{"sessionId": "2"}], "totalCount": 2},
        )

        sessions = client.list_sessions("reinvent2026")

        self.assertEqual([s["sessionId"] for s in sessions], ["1", "2"])
        self.assertEqual(transport.requests[0]["url"],
                         f"{BASE}/events/reinvent2026/sessions")
        self.assertEqual(transport.requests[1]["url"],
                         f"{BASE}/events/reinvent2026/sessions?nextToken=abc")

    def test_list_sessions_page_options(self):
        """locale and include_abstracts become query parameters."""
        client, transport = make_client({"items": [], "totalCount": 0})

        client.list_sessions_page("e1", locale="pt-BR", include_abstracts=False)

        self.assertEqual(
            transport.requests[0]["url"],
            f"{BASE}/events/e1/sessions?locale=pt-BR&includeAbstracts=false",
        )

    def test_get_session_tries_without_token_first(self):
        """Public sessions are fetched anonymously and unwrapped."""
        client, transport = make_client({"session": {"sessionId": "s1"}})

        session = client.get_session("e1", "s1")

        self.assertEqual(session, {"sessionId": "s1"})
        request = transport.requests[0]
        self.assertEqual(request["url"], f"{BASE}/events/e1/sessions/s1")
        self.assertNotIn("Authorization", request["headers"])

    def test_session_call_signs_in_after_401(self):
        """A 401 on a session call is retried once with the access token."""
        client, transport = make_client(
            ApiError("Sign in to continue", 401),
            {"items": [], "totalCount": 0},
        )

        client.list_sessions("e1")

        first, second = transport.requests
        self.assertNotIn("Authorization", first["headers"])
        self.assertEqual(second["headers"]["Authorization"], "Bearer test-token")

    def test_session_call_does_not_retry_other_errors(self):
        """Errors other than 401 are raised without signing in."""
        client, transport = make_client(ApiError("No such session", 404))

        with self.assertRaises(ApiError):
            client.get_session("e1", "missing")
        self.assertEqual(len(transport.requests), 1)


class ScheduleTests(unittest.TestCase):
    """Reservation, favorite and personal time calls."""

    def test_get_schedule_unwraps(self):
        """get_schedule returns the inner schedule dict."""
        schedule = {"reserved": [], "favorites": [], "personalTime": []}
        client, _ = make_client({"schedule": schedule})

        self.assertEqual(client.get_schedule("e1"), schedule)

    def test_get_schedule_with_session_details(self):
        """Session IDs are replaced by full sessions, each fetched once."""
        schedule = {"reserved": ["s1", "s2"], "favorites": ["s2"], "personalTime": []}
        client, transport = make_client(
            {"schedule": schedule},
            {"session": {"sessionId": "s1", "title": "One"}},
            {"session": {"sessionId": "s2", "title": "Two"}},
        )

        result = client.get_schedule("e1", include_session_details=True,
                                     locale="en-US")

        self.assertEqual([s["title"] for s in result["reserved"]], ["One", "Two"])
        self.assertEqual([s["title"] for s in result["favorites"]], ["Two"])
        self.assertEqual(
            [r["url"] for r in transport.requests[1:]],
            [
                f"{BASE}/events/e1/sessions/s1?locale=en-US",
                f"{BASE}/events/e1/sessions/s2?locale=en-US",
            ],
        )

    def test_reserve_sessions_posts_ids_and_returns_result(self):
        """reserve_sessions POSTs the IDs as JSON and returns the result."""
        result = {"successful": ["s1"], "failed": []}
        client, transport = make_client({"result": result})

        self.assertEqual(client.reserve_sessions("e1", ["s1"]), result)
        request = transport.requests[0]
        self.assertEqual(request["method"], "POST")
        self.assertEqual(request["url"], f"{BASE}/events/e1/reservations")
        self.assertEqual(request["json_body"], {"sessionIds": ["s1"]})

    def test_reserve_rejects_empty_and_too_many_ids(self):
        """Bulk calls reject 0 or more than 10 IDs before sending."""
        client, transport = make_client()

        with self.assertRaises(ValueError):
            client.reserve_sessions("e1", [])
        with self.assertRaises(ValueError):
            client.add_favorites("e1", [str(n) for n in range(11)])
        self.assertEqual(transport.requests, [])

    def test_cancel_and_remove_favorite_use_delete(self):
        """Cancelling and unfavoriting send DELETE to the right paths."""
        client, transport = make_client()

        client.cancel_reservation("e1", "s1")
        client.remove_favorite("e1", "s2")

        self.assertEqual(
            [(r["method"], r["url"]) for r in transport.requests],
            [
                ("DELETE", f"{BASE}/events/e1/reservations/s1"),
                ("DELETE", f"{BASE}/events/e1/favorites/s2"),
            ],
        )

    def test_personal_time_create_update_delete(self):
        """Personal time calls use POST, PUT and DELETE with a JSON body."""
        client, transport = make_client()
        block = PersonalTime(
            title="Lunch",
            description="Team lunch",
            start_date_time="2026-12-02T19:00:00",
            end_date_time="2026-12-02T20:00:00",
        )

        client.create_personal_time("e1", block)
        client.update_personal_time("e1", "pt1", block)
        client.delete_personal_time("e1", "pt1")

        create, update, delete = transport.requests
        self.assertEqual((create["method"], create["url"]),
                         ("POST", f"{BASE}/events/e1/personal-time"))
        self.assertEqual((update["method"], update["url"]),
                         ("PUT", f"{BASE}/events/e1/personal-time/pt1"))
        self.assertEqual((delete["method"], delete["url"]),
                         ("DELETE", f"{BASE}/events/e1/personal-time/pt1"))
        self.assertEqual(create["json_body"], {
            "title": "Lunch",
            "description": "Team lunch",
            "startDateTime": "2026-12-02T19:00:00",
            "endDateTime": "2026-12-02T20:00:00",
        })


class HelperTests(unittest.TestCase):
    """URL-building helpers."""

    def test_build_path_escapes_segments(self):
        """Slashes and spaces in IDs are escaped."""
        self.assertEqual(build_path("events", "a/b", "c d"), "/events/a%2Fb/c%20d")

    def test_personal_time_includes_location_when_set(self):
        """location is sent only when it has a value."""
        block = PersonalTime("T", "D", "2026-12-02T19:00:00",
                             "2026-12-02T20:00:00", location="Venetian")
        self.assertEqual(block.to_api_body()["location"], "Venetian")


if __name__ == "__main__":
    unittest.main()
