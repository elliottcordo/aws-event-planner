"""Formatting helpers that turn sessions and schedules into display values."""

from datetime import date

from aws_events import session_date, session_start_time


RESERVED_ICON = "✅"
FAVORITE_ICON = "❤️"


def format_day(day):
    """Return "YYYY-MM-DD" as a readable day such as "Wed 2 Dec"."""
    if not day:
        return "Date not set"
    parsed = date.fromisoformat(day)
    return f"{parsed:%a} {parsed.day} {parsed:%b}"


NO_LEVEL = "N/A"


def format_when(session):
    """Return a session's day and start time, such as "Wed 2 Dec 13:30"."""
    when = format_day(session_date(session))
    start_time = session_start_time(session)
    if start_time:
        when = f"{when} {start_time}"
    return when


def short_level(level):
    """Return just the number of a level, such as "300" for "300 - Advanced".

    "No Level" and missing levels become "N/A"; any other text is kept.
    """
    if not level or level.strip().lower() == "no level":
        return NO_LEVEL
    number = level.split(" - ", 1)[0].strip()
    if number.isdigit():
        return number
    return level


def schedule_status(schedule):
    """Return a dict mapping each scheduled session ID to its status.

    Each status is a dict with "reserved" and "favorite" booleans.
    """
    reserved = set(schedule["reserved"])
    favorites = set(schedule["favorites"])
    statuses = {}
    for session_id in reserved | favorites:
        statuses[session_id] = {
            "reserved": session_id in reserved,
            "favorite": session_id in favorites,
        }
    return statuses


def session_row(session, statuses):
    """Return the values the results table shows for one session.

    Args:
        session: A session dict from the catalog.
        statuses: Dict of session ID to status, from schedule_status.
    """
    status = statuses.get(session["sessionId"], {})
    # "or" rather than a get() default: the API sends some fields as null.
    level = session.get("level") or ""
    return {
        "id": session["sessionId"],
        "reserved": status.get("reserved", False),
        "favorite": status.get("favorite", False),
        "when": format_when(session),
        "code": session.get("abbreviation") or "",
        "title": session.get("title") or "",
        "abstract": session.get("abstract") or "",
        "venue": session.get("venue") or "",
        "type": session.get("type") or "",
        "level": short_level(level),
        "level_full": level,
    }


# Why the API refused a session in a bulk request, as the end of a sentence
# that starts with the session's code. Unknown codes get a generic reason.
FAILURE_REASONS = {
    "sessionNotReservable": "can't be booked (booking isn't open for it)",
    "scheduleConflict": "clashes with a session already on your schedule",
    "alreadyScheduled": "is already booked",
    "sessionFull": "is full",
    "insufficientAccess": "isn't included in your pass",
    "timePassed": "has already started",
    "alreadyFavorited": "is already a favorite",
    "notFavorited": "isn't a favorite",
    "other": "was refused",
}


def session_label(session_id, catalog):
    """Return a session's code (for example "ANT301"), or its ID if unknown."""
    session = catalog.get(session_id) if catalog is not None else None
    if session is not None and session.get("abbreviation"):
        return session["abbreviation"]
    return session_id


def describe_failure(failure, catalog):
    """Return one sentence explaining why a session was refused."""
    label = session_label(failure["sessionId"], catalog)
    code = failure["code"]
    if code == "scheduleConflict" and failure.get("conflictsWith"):
        clashing = [session_label(other, catalog) for other in failure["conflictsWith"]]
        return f"{label} clashes with {', '.join(clashing)} on your schedule."
    reason = FAILURE_REASONS.get(code, f"was refused ({code})")
    return f"{label} {reason}."


def describe_bulk_result(result, done_message, catalog):
    """Summarize a bulk API result as (kind, message) for the page.

    Args:
        result: A dict with "successful" and "failed" lists, as the API's
            reserve and favorite calls return.
        done_message: Words for the successes, with {count} for how many, for
            example "Booked {count} session(s)."
        catalog: SessionCatalog used to name sessions, or None.

    Returns:
        ("success", ...) if all succeeded, ("error", ...) if all failed, else
        ("warning", ...).
    """
    sentences = []
    if result["successful"]:
        sentences.append(done_message.format(count=len(result["successful"])))
    for failure in result["failed"]:
        sentences.append(describe_failure(failure, catalog))

    if not result["failed"]:
        kind = "success"
    elif not result["successful"]:
        kind = "error"
    else:
        kind = "warning"
    return kind, " ".join(sentences)
