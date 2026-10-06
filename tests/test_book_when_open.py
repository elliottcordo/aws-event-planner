"""Tests for waiting until reserve-open waves."""

import unittest
from datetime import timedelta

import book_when_open
from aws_events.booking import reserve_wave_times
from aws_events.catalog import SessionCatalog
from aws_events.client import EventsClient
from aws_events.errors import ApiError, FeatureDisabledError, SignInError
from tests.fakes import FakeAuthenticator, FakeTransport, make_session


def schedule_reply(reserved=(), favorites=("talk",)):
    """Return a canned GET schedule response."""
    return {
        "schedule": {
            "reserved": list(reserved),
            "favorites": list(favorites),
            "personalTime": [],
        }
    }


def reserve_reply(successful=(), failed=()):
    """Return a canned POST reservations response."""
    return {"result": {"successful": list(successful), "failed": list(failed)}}


class FakeClock:
    """Advance a frozen clock when sleep is called."""

    def __init__(self, moment):
        """Start at `moment`."""
        self.moment = moment
        self.sleeps = []

    def now(self):
        """Return the current frozen time."""
        return self.moment

    def sleep(self, seconds):
        """Record the sleep and move the clock forward."""
        self.sleeps.append(seconds)
        self.moment = self.moment + timedelta(seconds=seconds)


def make_run(transport, logs):
    """Return a BookingRun for one chalk talk favorite, logging into `logs`."""
    catalog = SessionCatalog("e1", [make_session("talk", "Talk", type="Chalk talk")])
    return book_when_open.BookingRun(
        client=EventsClient(transport, FakeAuthenticator()),
        event_id="e1",
        catalog=catalog,
        interactive_only=True,
        log=logs.append,
    )


def reserve_calls(transport):
    """Return the POST requests the transport received."""
    calls = []
    for request in transport.requests:
        if request["method"] == "POST":
            calls.append(request)
    return calls


class WaveRunnerTests(unittest.TestCase):
    """Sleep until a wave, then book."""

    def run_first_wave(self, transport, start_offset=timedelta(minutes=-5)):
        """Run the 9 AM wave only, starting `start_offset` from it."""
        wave = reserve_wave_times()[0]
        clock = FakeClock(wave + start_offset)
        logs = []
        status = book_when_open.run_waves(
            make_run(transport, logs),
            clock=clock.now,
            sleeper=clock.sleep,
            waves=[wave],
        )
        return status, logs, clock

    def test_books_when_the_wave_opens(self):
        """After waiting, interactive favorites are reserved."""
        transport = FakeTransport(schedule_reply(), reserve_reply(successful=["talk"]))

        status, logs, _clock = self.run_first_wave(transport)

        self.assertEqual(status, 0)
        self.assertIn("Booked 1: talk", logs)
        self.assertEqual(len(reserve_calls(transport)), 1)

    def test_keeps_polling_while_booking_is_disabled(self):
        """HTTP 409 is retried inside the wave window."""
        transport = FakeTransport(
            schedule_reply(),
            FeatureDisabledError("not yet"),
            schedule_reply(),
            reserve_reply(successful=["talk"]),
        )

        status, _logs, _clock = self.run_first_wave(transport, timedelta(0))

        self.assertEqual(status, 0)
        self.assertEqual(len(reserve_calls(transport)), 2)

    def test_server_errors_do_not_end_the_run(self):
        """A 503 or a dropped connection is logged and retried."""
        transport = FakeTransport(
            ApiError("timed out"),
            schedule_reply(),
            ApiError("busy", status_code=503),
            schedule_reply(),
            reserve_reply(successful=["talk"]),
        )

        status, logs, _clock = self.run_first_wave(transport, timedelta(0))

        self.assertEqual(status, 0)
        self.assertIn("Request failed, will retry: timed out", logs)
        self.assertIn("Booked 1: talk", logs)

    def test_lost_sign_in_is_logged_and_retried(self):
        """A rejected refresh asks for login instead of opening a browser."""
        transport = FakeTransport(
            SignInError("Your sign-in has expired."),
            schedule_reply(),
            reserve_reply(successful=["talk"]),
        )

        status, logs, _clock = self.run_first_wave(transport, timedelta(0))

        self.assertEqual(status, 0)
        self.assertTrue(any("events_cli.py login" in line for line in logs))

    def test_conflicts_are_given_up_without_a_second_request(self):
        """A schedule conflict ends the wave after one reserve call."""
        conflict = {"sessionId": "talk", "code": "scheduleConflict"}
        transport = FakeTransport(schedule_reply(), reserve_reply(failed=[conflict]))

        status, logs, _clock = self.run_first_wave(transport, timedelta(0))

        self.assertEqual(status, 0)
        self.assertIn("Failed talk (scheduleConflict), giving up", logs)
        self.assertEqual(len(transport.requests), 2)

    def test_full_session_is_retried_at_the_next_wave(self):
        """A full session at 9 AM is tried again at 5 PM."""
        full = {"sessionId": "talk", "code": "sessionFull"}
        morning, evening = reserve_wave_times()
        clock = FakeClock(morning)
        responses = []
        # Enough "full" replies to outlast the whole 9 AM window.
        for _attempt in range(200):
            responses.append(schedule_reply())
            responses.append(reserve_reply(failed=[full]))
        responses.append(schedule_reply())
        responses.append(reserve_reply(successful=["talk"]))
        transport = FakeTransport(*responses)
        logs = []

        status = book_when_open.run_waves(
            make_run(transport, logs),
            clock=clock.now,
            sleeper=clock.sleep,
            waves=[morning, evening],
        )

        self.assertEqual(status, 0)
        self.assertIn(f"Trying wave {evening.isoformat()}", logs)
        self.assertIn("Booked 1: talk", logs)

    def test_polling_slows_after_the_first_minutes(self):
        """Fast polls give way to slow ones later in the window."""
        full = {"sessionId": "talk", "code": "sessionFull"}
        responses = []
        for _attempt in range(200):
            responses.append(schedule_reply())
            responses.append(reserve_reply(failed=[full]))
        transport = FakeTransport(*responses)

        status, _logs, clock = self.run_first_wave(transport, timedelta(0))

        self.assertEqual(status, 1)
        self.assertEqual(clock.sleeps[0], book_when_open.FAST_POLL_SECONDS)
        self.assertEqual(clock.sleeps[-1], book_when_open.SLOW_POLL_SECONDS)
        # About 100 fast polls in 5 minutes, then 50 slow ones in 25 minutes.
        self.assertLess(len(reserve_calls(transport)), 160)

    def test_ended_waves_fall_back_to_one_attempt(self):
        """After both windows close, the script still tries once."""
        transport = FakeTransport(schedule_reply(), reserve_reply(successful=["talk"]))

        status, logs, _clock = self.run_first_wave(transport, timedelta(hours=2))

        self.assertEqual(status, 0)
        self.assertIn("Both published waves have passed; trying once.", logs)


class PollIntervalTests(unittest.TestCase):
    """Back off once the first rush is over."""

    def test_fast_then_slow(self):
        """Polls are fast for five minutes after the wave, then slow."""
        self.assertEqual(book_when_open.poll_seconds(-30), 3)
        self.assertEqual(book_when_open.poll_seconds(299), 3)
        self.assertEqual(book_when_open.poll_seconds(300), 30)


class SignInCheckTests(unittest.TestCase):
    """The unattended waiter must not open a browser."""

    def test_signed_in_passes(self):
        """A usable token gives no problem message."""
        self.assertIsNone(book_when_open.check_signed_in(FakeAuthenticator()))

    def test_missing_sign_in_is_reported(self):
        """A sign-in error becomes a message pointing at login."""

        class SignedOutAuthenticator:
            """Raise as the real one does with sign_in_when_needed=False."""

            def get_access_token(self):
                """Refuse to hand out a token."""
                raise SignInError("You are not signed in.")

        problem = book_when_open.check_signed_in(SignedOutAuthenticator())

        self.assertIn("You are not signed in.", problem)
        self.assertIn("events_cli.py login", problem)


if __name__ == "__main__":
    unittest.main()
