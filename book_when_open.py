#!/usr/bin/env python3
"""Wait for re:Invent reserve-open waves and book interactive favorites.

Seats for interactive sessions are released at 9 AM PT and 5 PM PT on
6 October 2026. A minute before each wave this script starts probing with a
one-session reserve call. As soon as booking is open it books every remaining
favorite, then retries full sessions until the wave window ends. Failed
requests are retried with exponential backoff.

Usage:
    python3 book_when_open.py reinvent2026
    python3 book_when_open.py reinvent2026 --all
"""

import argparse
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import backoff

from aws_events import (
    Authenticator,
    EventsClient,
    EventsError,
    FeatureDisabledError,
    HttpTransport,
    SessionCatalog,
    SignInError,
    TokenStore,
    default_catalog_path,
)
from aws_events.booking import (
    ids_to_retry,
    reserve_in_batches,
    reserve_wave_times,
    session_ids_waiting_to_book,
)

# Start probing this long before the published time, in case clocks differ.
PROBE_LEAD_SECONDS = 60
# Keep trying through the first minutes after each wave.
WINDOW_SECONDS = 30 * 60
# Poll quickly while seats are going, then slow down to avoid being throttled.
FAST_POLL_SECONDS = 2
FAST_POLL_WINDOW_SECONDS = 5 * 60
SLOW_POLL_SECONDS = 30
# After failed requests, wait 2, 4, 8, ... seconds, never more than a minute.
BACKOFF_BASE_SECONDS = 2
BACKOFF_MAX_SECONDS = 60


@dataclass
class BookingRun:
    """What to book, and where to report progress.

    Attributes:
        client: An EventsClient.
        event_id: The event to book, e.g. "reinvent2026".
        catalog: A SessionCatalog used to skip breakouts, or None.
        interactive_only: If True, book only limited-seat session types.
        log: Called with one progress message at a time.
        blocked: Session IDs refused for good; never sent again.
    """

    client: EventsClient
    event_id: str
    catalog: SessionCatalog = None
    interactive_only: bool = True
    log: object = print
    blocked: set = field(default_factory=set)


@dataclass
class WaveState:
    """Progress through one wave.

    Attributes:
        probe_id: The favorite reserved on its own to test whether booking
            is open; chosen at the first step of the wave.
        is_open: Whether a probe has shown that booking is open.
    """

    probe_id: str = None
    is_open: bool = False


def remaining_ids(run):
    """Return favorite IDs still worth sending to reserve."""
    schedule = run.client.get_schedule(run.event_id)
    waiting = session_ids_waiting_to_book(
        schedule, catalog=run.catalog, interactive_only=run.interactive_only
    )
    still_worth_trying = []
    for session_id in waiting:
        if session_id not in run.blocked:
            still_worth_trying.append(session_id)
    return still_worth_trying


def record_result(run, result):
    """Log a reserve result and block refusals. Return True if none can retry."""
    booked = result["successful"]
    if booked:
        booked_text = ", ".join(str(session_id) for session_id in booked)
        run.log(f"Booked {len(booked)}: {booked_text}")
    retry_ids = ids_to_retry(result["failed"])
    for failure in result["failed"]:
        session_id = failure.get("sessionId", "?")
        code = failure.get("code", "unknown")
        if session_id in retry_ids:
            run.log(f"Failed {session_id} ({code}), will retry")
        else:
            run.log(f"Failed {session_id} ({code}), giving up")
            run.blocked.add(session_id)
    return not retry_ids


def probe_is_open(run, session_id):
    """Reserve one favorite on its own. Return False while booking is off.

    This is the cheapest request that shows whether booking is open: no
    schedule lookup, one session ID. Any answer other than HTTP 409 means
    it is open, and the probe may already have booked that session.

    Raises:
        EventsError: If the request fails for another reason.
    """
    try:
        result = run.client.reserve_sessions(run.event_id, [session_id])
    except FeatureDisabledError:
        return False
    run.log("Booking is open.")
    record_result(run, result)
    return True


def book_remaining(run):
    """Reserve every waiting favorite once. Return True if none is left to try.

    Raises:
        EventsError: If a request fails.
    """
    session_ids = remaining_ids(run)
    if not session_ids:
        run.log("Nothing left to book.")
        return True
    run.log(f"Booking {len(session_ids)} session(s)...")
    result = reserve_in_batches(run.client, run.event_id, session_ids)
    return record_result(run, result)


def log_failure(run, error):
    """Log a failed request in words that say what to do about it."""
    if isinstance(error, SignInError):
        run.log(
            f"{error} Run 'venv/bin/python events_cli.py login' in another "
            "terminal; this script keeps retrying."
        )
    else:
        run.log(f"Request failed, will retry: {error}")


def poll_seconds(seconds_since_wave):
    """Return how long to wait before the next probe or booking attempt."""
    if seconds_since_wave < FAST_POLL_WINDOW_SECONDS:
        return FAST_POLL_SECONDS
    return SLOW_POLL_SECONDS


def is_feature_disabled(error):
    """Return True for HTTP 409, which the wave loop handles, not backoff."""
    return isinstance(error, FeatureDisabledError)


def with_backoff(run, step, window_end, clock):
    """Wrap `step` so failed requests are retried with exponential backoff.

    Waits 2, 4, 8 ... seconds between tries, never more than a minute, and
    gives up by re-raising once the wave window has closed. The backoff
    library sleeps with time.sleep and measures its time limit itself.

    Args:
        run: The BookingRun, used to log each retry.
        step: The function to retry.
        window_end: When the wave window closes.
        clock: Returns the current time, used to work out the time left.
    """

    def seconds_left():
        """Return the seconds until the wave window closes."""
        return max((window_end - clock()).total_seconds(), 0)

    def log_retry(details):
        """Log the failure and how long backoff will wait."""
        log_failure(run, details["exception"])
        run.log(f"Retrying in {details['wait']:.0f}s")

    retrying = backoff.on_exception(
        backoff.expo,
        EventsError,
        giveup=is_feature_disabled,
        max_time=seconds_left,
        jitter=None,
        on_backoff=log_retry,
        logger=None,
        base=2,
        factor=BACKOFF_BASE_SECONDS,
        max_value=BACKOFF_MAX_SECONDS,
    )
    return retrying(step)


def sleep_until(deadline, clock, sleeper, log):
    """Sleep in short steps until `deadline`."""
    while True:
        remaining = (deadline - clock()).total_seconds()
        if remaining <= 0:
            return
        log(f"Waiting {int(remaining)}s until {deadline.isoformat()}")
        sleeper(min(remaining, 60))


def take_step(run, state):
    """Probe, or book once booking is open. Return True if none is left to try.

    Raises:
        EventsError: If a request fails.
    """
    if state.probe_id is None:
        waiting = remaining_ids(run)
        if not waiting:
            run.log("Nothing left to book.")
            return True
        state.probe_id = waiting[0]
        run.log(f"Probing with {state.probe_id}")
    if not state.is_open:
        state.is_open = probe_is_open(run, state.probe_id)
    if not state.is_open:
        return False
    return book_remaining(run)


def try_through_wave(run, wave, clock, sleeper):
    """Probe until booking opens, then book until the window ends.

    Returns:
        True if nothing is left to try, False if the window ran out first.
    """
    window_end = wave + timedelta(seconds=WINDOW_SECONDS)
    state = WaveState()
    step = with_backoff(run, take_step, window_end, clock)
    while clock() <= window_end:
        try:
            if step(run, state):
                return True
        except FeatureDisabledError:
            run.log("Booking was switched off again; probing.")
            state.is_open = False
        except EventsError as error:
            log_failure(run, error)
            run.log("Giving up on this wave: its window has closed.")
            return False
        sleeper(poll_seconds((clock() - wave).total_seconds()))
    return False


def run_waves(
    run, clock=lambda: datetime.now(timezone.utc), sleeper=time.sleep, waves=None
):
    """Book at each remaining reserve-open wave.

    Returns:
        0 if every favorite was booked or given up, else 1.
    """
    ran_a_wave = False
    for wave in waves or reserve_wave_times():
        window_end = wave + timedelta(seconds=WINDOW_SECONDS)
        if clock() > window_end:
            run.log(f"Skipping ended wave {wave.isoformat()}")
            continue
        start_at = wave - timedelta(seconds=PROBE_LEAD_SECONDS)
        sleep_until(start_at, clock, sleeper, run.log)
        run.log(f"Trying wave {wave.isoformat()}")
        ran_a_wave = True
        if try_through_wave(run, wave, clock, sleeper):
            run.log("Done.")
            return 0
    if not ran_a_wave:
        run.log("Both published waves have passed; trying once.")
        try:
            if book_remaining(run):
                run.log("Done.")
                return 0
        except EventsError as error:
            log_failure(run, error)
    run.log("Some sessions are still unbooked.")
    return 1


def load_required_catalog(event_id):
    """Return the saved catalog or raise an error explaining how to create it.

    Raises:
        EventsError: If no catalog has been downloaded for the event.
    """
    path = default_catalog_path(event_id)
    try:
        return SessionCatalog.load(path)
    except FileNotFoundError as error:
        raise EventsError(
            f"No sessions saved at {path}. Run "
            f"'venv/bin/python events_cli.py download-sessions {event_id}' first."
        ) from error


def check_signed_in(authenticator):
    """Return an error message if the saved sign-in cannot be used, else None.

    The waiter runs unattended, so it never opens a browser sign-in itself.
    """
    try:
        authenticator.get_access_token()
    except EventsError as error:
        return f"{error} Run 'venv/bin/python events_cli.py login' first."
    return None


def main(argv=None):
    """Parse arguments and book through the reserve-open waves."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("event_id", help="Event ID, for example reinvent2026")
    parser.add_argument(
        "--all",
        action="store_true",
        help="Book every unbooked favorite, including breakouts",
    )
    args = parser.parse_args(argv)
    transport = HttpTransport()
    authenticator = Authenticator(TokenStore(), transport, sign_in_when_needed=False)
    problem = check_signed_in(authenticator)
    if problem:
        print(problem, file=sys.stderr)
        return 2
    catalog = None
    if not args.all:
        try:
            catalog = load_required_catalog(args.event_id)
        except EventsError as error:
            print(error, file=sys.stderr)
            return 2
    run = BookingRun(
        client=EventsClient(transport, authenticator),
        event_id=args.event_id,
        catalog=catalog,
        interactive_only=not args.all,
    )
    return run_waves(run)


if __name__ == "__main__":
    raise SystemExit(main())
