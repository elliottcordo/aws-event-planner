"""Tests for waiting until reserve-open waves."""

import unittest
from datetime import timedelta
from unittest import mock

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


def full_session_replies(count):
    """Return `count` schedule-then-"session full" response pairs."""
    full = {"sessionId": "talk", "code": "sessionFull"}
    responses = []
    for _attempt in range(count):
        responses.append(schedule_reply())
        responses.append(reserve_reply(failed=[full]))
    return responses


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


class TimedTransport(FakeTransport):
    """A FakeTransport that also records the clock time of each request."""

    def __init__(self, clock, *responses):
        """Queue responses and read times from `clock`."""
        super().__init__(*responses)
        self.clock = clock
        self.times = []

    def request(self, method, url, **options):
        """Record the time, then behave like FakeTransport."""
        self.times.append(self.clock.now())
        return super().request(method, url, **options)


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
    """Probe before a wave, then book."""

    def run_first_wave(self, responses, start_offset=timedelta(0)):
        """Run the 9 AM wave only, starting `start_offset` from it."""
        wave = reserve_wave_times()[0]
        clock = FakeClock(wave + start_offset)
        transport = TimedTransport(clock, *responses)
        logs = []
        # The backoff library sleeps with time.sleep, so route it to the clock.
        with mock.patch("time.sleep", new=clock.sleep):
            status = book_when_open.run_waves(
                make_run(transport, logs),
                clock=clock.now,
                sleeper=clock.sleep,
                waves=[wave],
            )
        return status, logs, clock, transport

    def test_probing_starts_a_minute_early_with_one_session(self):
        """Probes begin at T-60s and send a single session ID."""
        wave = reserve_wave_times()[0]
        status, logs, _clock, transport = self.run_first_wave(
            [
                schedule_reply(),
                FeatureDisabledError("not yet"),
                reserve_reply(successful=["talk"]),
                schedule_reply(reserved=["talk"]),
            ],
            start_offset=timedelta(minutes=-5),
        )

        self.assertEqual(status, 0)
        self.assertEqual(transport.times[0], wave - timedelta(seconds=60))
        probe = reserve_calls(transport)[0]
        self.assertEqual(probe["json_body"], {"sessionIds": ["talk"]})
        self.assertIn("Booking is open.", logs)
        self.assertIn("Booked 1: talk", logs)

    def test_probes_do_not_fetch_the_schedule_each_time(self):
        """While booking is off, only the one-session POST is repeated."""
        status, _logs, _clock, transport = self.run_first_wave(
            [
                schedule_reply(),
                FeatureDisabledError("not yet"),
                FeatureDisabledError("not yet"),
                FeatureDisabledError("not yet"),
                reserve_reply(successful=["talk"]),
                schedule_reply(reserved=["talk"]),
            ]
        )

        self.assertEqual(status, 0)
        methods = []
        for request in transport.requests:
            methods.append(request["method"])
        self.assertEqual(methods, ["GET", "POST", "POST", "POST", "POST", "GET"])

    def test_open_books_the_rest_after_the_probe(self):
        """Once the probe succeeds, the other favorites are booked together."""
        favorites = ("talk", "workshop")
        catalog = SessionCatalog(
            "e1",
            [
                make_session("talk", "Talk", type="Chalk talk"),
                make_session("workshop", "Workshop", type="Workshop"),
            ],
        )
        wave = reserve_wave_times()[0]
        clock = FakeClock(wave)
        transport = TimedTransport(
            clock,
            schedule_reply(favorites=favorites),
            reserve_reply(successful=["talk"]),
            schedule_reply(reserved=["talk"], favorites=favorites),
            reserve_reply(successful=["workshop"]),
        )
        run = book_when_open.BookingRun(
            client=EventsClient(transport, FakeAuthenticator()),
            event_id="e1",
            catalog=catalog,
            log=lambda _message: None,
        )

        status = book_when_open.run_waves(
            run, clock=clock.now, sleeper=clock.sleep, waves=[wave]
        )

        self.assertEqual(status, 0)
        second_reserve = reserve_calls(transport)[1]
        self.assertEqual(second_reserve["json_body"], {"sessionIds": ["workshop"]})

    def test_failed_requests_back_off_exponentially(self):
        """Timeouts, 503s and 429s wait 2, 4, then 8 seconds."""
        status, logs, clock, _transport = self.run_first_wave(
            [
                schedule_reply(),
                ApiError("timed out"),
                ApiError("busy", status_code=503),
                ApiError("slow down", status_code=429),
                reserve_reply(successful=["talk"]),
                schedule_reply(reserved=["talk"]),
            ]
        )

        self.assertEqual(status, 0)
        self.assertEqual(clock.sleeps, [2, 4, 8])
        self.assertIn("Request failed, will retry: timed out", logs)
        self.assertIn("Retrying in 8s", logs)

    def test_lost_sign_in_is_logged_and_retried(self):
        """A rejected refresh asks for login instead of opening a browser."""
        status, logs, _clock, _transport = self.run_first_wave(
            [
                SignInError("Your sign-in has expired."),
                schedule_reply(),
                reserve_reply(successful=["talk"]),
                schedule_reply(reserved=["talk"]),
            ]
        )

        self.assertEqual(status, 0)
        self.assertTrue(any("events_cli.py login" in line for line in logs))

    def test_conflicts_are_given_up(self):
        """A schedule conflict on the probe is not sent again."""
        conflict = {"sessionId": "talk", "code": "scheduleConflict"}
        status, logs, _clock, transport = self.run_first_wave(
            [schedule_reply(), reserve_reply(failed=[conflict]), schedule_reply()]
        )

        self.assertEqual(status, 0)
        self.assertIn("Failed talk (scheduleConflict), giving up", logs)
        self.assertEqual(len(reserve_calls(transport)), 1)

    def test_full_session_is_retried_at_the_next_wave(self):
        """A full session at 9 AM is tried again at 5 PM."""
        morning, evening = reserve_wave_times()
        clock = FakeClock(morning)
        responses = full_session_replies(300)
        responses.append(schedule_reply())
        responses.append(reserve_reply(successful=["talk"]))
        responses.append(schedule_reply(reserved=["talk"]))
        transport = TimedTransport(clock, *responses)
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
        self.assertGreater(transport.times[-1], evening - timedelta(seconds=61))

    def test_polling_slows_after_the_first_minutes(self):
        """Fast polls give way to slow ones later in the window."""
        status, _logs, clock, transport = self.run_first_wave(full_session_replies(400))

        self.assertEqual(status, 1)
        self.assertEqual(clock.sleeps[0], book_when_open.FAST_POLL_SECONDS)
        self.assertEqual(clock.sleeps[-1], book_when_open.SLOW_POLL_SECONDS)
        # About 150 fast polls in 5 minutes, then 50 slow ones in 25 minutes.
        self.assertLess(len(reserve_calls(transport)), 220)

    def test_ended_waves_fall_back_to_one_attempt(self):
        """After both windows close, the script still tries once."""
        status, logs, _clock, _transport = self.run_first_wave(
            [schedule_reply(), reserve_reply(successful=["talk"])],
            start_offset=timedelta(hours=2),
        )

        self.assertEqual(status, 0)
        self.assertIn("Both published waves have passed; trying once.", logs)


class DelayTests(unittest.TestCase):
    """How long to wait between requests."""

    def test_poll_is_fast_then_slow(self):
        """Polls are fast until five minutes after the wave, then slow."""
        self.assertEqual(book_when_open.poll_seconds(-60), 2)
        self.assertEqual(book_when_open.poll_seconds(299), 2)
        self.assertEqual(book_when_open.poll_seconds(300), 30)


class BackoffTests(unittest.TestCase):
    """Failed requests are retried by the backoff library."""

    def make_failing_step(self, failures):
        """Return a step that raises ApiError `failures` times, then succeeds."""
        calls = []

        def step():
            """Fail until enough calls have been made."""
            calls.append(1)
            if len(calls) <= failures:
                raise ApiError("busy", status_code=503)
            return True

        return step, calls

    def test_waits_double_up_to_a_minute(self):
        """2, 4, 8, 16, 32, then capped at 60 seconds."""
        clock = FakeClock(reserve_wave_times()[0])
        window_end = clock.now() + timedelta(hours=1)
        run = book_when_open.BookingRun(
            client=None, event_id="e1", log=lambda _message: None
        )
        step, calls = self.make_failing_step(7)

        with mock.patch("time.sleep", new=clock.sleep):
            result = book_when_open.with_backoff(run, step, window_end, clock.now)()

        self.assertTrue(result)
        self.assertEqual(len(calls), 8)
        self.assertEqual(clock.sleeps, [2, 4, 8, 16, 32, 60, 60])

    def test_gives_up_when_the_window_has_closed(self):
        """No retries are made once the window is over; the error is raised."""
        clock = FakeClock(reserve_wave_times()[0])
        run = book_when_open.BookingRun(
            client=None, event_id="e1", log=lambda _message: None
        )
        step, calls = self.make_failing_step(5)
        retrying = book_when_open.with_backoff(run, step, clock.now(), clock.now)

        with mock.patch("time.sleep", new=clock.sleep):
            with self.assertRaises(ApiError):
                retrying()

        self.assertEqual(len(calls), 1)
        self.assertEqual(clock.sleeps, [])

    def test_booking_not_open_is_not_retried_by_backoff(self):
        """HTTP 409 goes straight back to the wave loop."""
        clock = FakeClock(reserve_wave_times()[0])
        run = book_when_open.BookingRun(client=None, event_id="e1")
        calls = []

        def step():
            """Answer as the API does before booking opens."""
            calls.append(1)
            raise FeatureDisabledError("not yet")

        retrying = book_when_open.with_backoff(
            run, step, clock.now() + timedelta(hours=1), clock.now
        )

        with self.assertRaises(FeatureDisabledError):
            retrying()

        self.assertEqual(len(calls), 1)


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


class CatalogLoadingTests(unittest.TestCase):
    """Interactive-only booking requires a downloaded session catalog."""

    def test_missing_catalog_has_download_instructions(self):
        """A missing catalog raises a clear error instead of booking everything."""
        with mock.patch.object(
            book_when_open.SessionCatalog,
            "load",
            side_effect=FileNotFoundError,
        ):
            with self.assertRaisesRegex(
                book_when_open.EventsError, "download-sessions e1"
            ):
                book_when_open.load_required_catalog("e1")


if __name__ == "__main__":
    unittest.main()
