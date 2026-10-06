"""Pick which favorites to reserve, and retry only the ones that can still succeed."""

from datetime import datetime, time
from zoneinfo import ZoneInfo

from aws_events.client import MAX_SESSIONS_PER_REQUEST

# AWS releases interactive seats in two waves on this day.
RESERVE_OPEN_DATE = datetime(2026, 10, 6).date()
RESERVE_TIMEZONE = ZoneInfo("America/Los_Angeles")
RESERVE_WAVE_HOURS = (9, 17)

# Chalk talks, workshops, builders' sessions, and the other limited-seat types.
INTERACTIVE_TYPES = {
    "Bootcamp",
    "Builders' session",
    "Chalk talk",
    "Code talk",
    "Exam prep",
    "Gamified learning",
    "Lab",
    "Workshop",
}

# A later wave will not change these outcomes.
GIVE_UP_CODES = {
    "alreadyScheduled",
    "insufficientAccess",
    "scheduleConflict",
    "sessionNotReservable",
    "timePassed",
}


def reserve_wave_times(on_date=RESERVE_OPEN_DATE):
    """Return the 9 AM and 5 PM Pacific datetimes when seats are released."""
    waves = []
    for hour in RESERVE_WAVE_HOURS:
        waves.append(datetime.combine(on_date, time(hour, 0), tzinfo=RESERVE_TIMEZONE))
    return waves


def is_interactive(session):
    """Return whether the session is a limited-seat interactive type."""
    return session.get("type") in INTERACTIVE_TYPES


def session_ids_waiting_to_book(schedule, catalog=None, interactive_only=True):
    """Return favorite IDs that are not booked yet.

    Args:
        schedule: A schedule dict with "reserved" and "favorites".
        catalog: Used to skip breakouts when interactive_only is True. It is
            required in that mode so a missing catalog cannot silently make
            every favorite eligible.
        interactive_only: If True, keep chalk talks, workshops, and the other
            limited-seat types. Sessions missing from the catalog are kept.

    Returns:
        Session IDs in favorite order.

    Raises:
        ValueError: If interactive_only is True but no catalog was provided.
    """
    if interactive_only and catalog is None:
        raise ValueError("A session catalog is required for interactive-only booking")

    reserved = set(schedule.get("reserved") or [])
    waiting = []
    for session_id in schedule.get("favorites") or []:
        if session_id in reserved:
            continue
        if interactive_only and catalog is not None:
            session = catalog.get(session_id)
            if session is not None and not is_interactive(session):
                continue
        waiting.append(session_id)
    return waiting


def should_retry_failure(failure):
    """Return whether a failed reserve is worth trying again."""
    code = failure.get("code")
    return code not in GIVE_UP_CODES


def ids_to_retry(failed):
    """Return session IDs from a failed list that should be tried again.

    Failures without a session ID are skipped, since they cannot be retried.
    """
    retry = []
    for failure in failed:
        session_id = failure.get("sessionId")
        if session_id is not None and should_retry_failure(failure):
            retry.append(session_id)
    return retry


def reserve_in_batches(client, event_id, session_ids):
    """Reserve any number of sessions, at most 10 per API call.

    Returns:
        A dict with combined "successful" and "failed" lists. An empty
        session_ids list returns empty lists without calling the API.
    """
    successful = []
    failed = []
    for start in range(0, len(session_ids), MAX_SESSIONS_PER_REQUEST):
        batch = session_ids[start : start + MAX_SESSIONS_PER_REQUEST]
        result = client.reserve_sessions(event_id, batch)
        successful.extend(result["successful"])
        failed.extend(result["failed"])
    return {"successful": successful, "failed": failed}
