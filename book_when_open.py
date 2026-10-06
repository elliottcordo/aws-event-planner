#!/usr/bin/env python3
"""Wait for re:Invent reserve-open waves and book interactive favorites.

Seats for interactive sessions are released at 9 AM PT and 5 PM PT on
6 October 2026. This script sleeps until each wave, then retries until the
wave window ends or every remaining favorite is booked or refused for good.

Usage:
    python3 book_when_open.py reinvent2026
    python3 book_when_open.py reinvent2026 --all
"""

import argparse
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

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

# Start hitting the API just before the published time, in case clocks differ.
LEAD_SECONDS = 30
# Keep trying through the first minutes after each wave.
WINDOW_SECONDS = 30 * 60
# Poll quickly while seats are going, then back off to avoid being throttled.
FAST_POLL_SECONDS = 3
FAST_POLL_WINDOW_SECONDS = 5 * 60
SLOW_POLL_SECONDS = 30


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


def attempt_wave(run):
    """Reserve waiting favorites once. Return True if nothing is left to try.

    API errors are logged rather than raised: at release time the API is
    busy, and one failed request must not end the run.
    """
    try:
        session_ids = remaining_ids(run)
        if not session_ids:
            run.log("Nothing left to book.")
            return True
        run.log(f"Booking {len(session_ids)} session(s)...")
        result = reserve_in_batches(run.client, run.event_id, session_ids)
    except FeatureDisabledError:
        run.log("Booking is not enabled yet.")
        return False
    except SignInError as error:
        run.log(
            f"{error} Run 'venv/bin/python events_cli.py login' in another "
            "terminal; this script keeps retrying."
        )
        return False
    except EventsError as error:
        run.log(f"Request failed, will retry: {error}")
        return False
    return record_result(run, result)


def poll_seconds(seconds_since_wave):
    """Return how long to wait before the next attempt in a wave."""
    if seconds_since_wave < FAST_POLL_WINDOW_SECONDS:
        return FAST_POLL_SECONDS
    return SLOW_POLL_SECONDS


def sleep_until(deadline, clock, sleeper, log):
    """Sleep in short steps until `deadline`."""
    while True:
        remaining = (deadline - clock()).total_seconds()
        if remaining <= 0:
            return
        log(f"Waiting {int(remaining)}s until {deadline.isoformat()}")
        sleeper(min(remaining, 60))


def try_through_wave(run, wave, clock, sleeper):
    """Retry until the wave window ends. Return True if nothing is left to try."""
    window_end = wave + timedelta(seconds=WINDOW_SECONDS)
    while clock() <= window_end:
        if attempt_wave(run):
            return True
        seconds_since_wave = (clock() - wave).total_seconds()
        sleeper(poll_seconds(seconds_since_wave))
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
        start_at = wave - timedelta(seconds=LEAD_SECONDS)
        sleep_until(start_at, clock, sleeper, run.log)
        run.log(f"Trying wave {wave.isoformat()}")
        ran_a_wave = True
        if try_through_wave(run, wave, clock, sleeper):
            run.log("Done.")
            return 0
    if not ran_a_wave:
        run.log("Both published waves have passed; trying once.")
        if attempt_wave(run):
            run.log("Done.")
            return 0
    run.log("Some sessions are still unbooked.")
    return 1


def load_optional_catalog(event_id):
    """Return the saved catalog, or None if it has not been downloaded."""
    try:
        return SessionCatalog.load(default_catalog_path(event_id))
    except FileNotFoundError:
        return None


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
    run = BookingRun(
        client=EventsClient(transport, authenticator),
        event_id=args.event_id,
        catalog=load_optional_catalog(args.event_id),
        interactive_only=not args.all,
    )
    return run_waves(run)


if __name__ == "__main__":
    raise SystemExit(main())
