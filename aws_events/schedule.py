"""Turn the attendee's schedule into sessions grouped by day for display."""

from dataclasses import dataclass

from aws_events.catalog import chronological_key, session_date


def status_label(reserved, favorite):
    """Return a short label for a session's place on the schedule, or ""."""
    if reserved and favorite:
        return "Reserved, favorite"
    if reserved:
        return "Reserved"
    if favorite:
        return "Favorite"
    return ""


def schedule_labels(schedule):
    """Return a dict mapping each scheduled session ID to its status label."""
    reserved = set(schedule["reserved"])
    favorites = set(schedule["favorites"])
    labels = {}
    for session_id in reserved | favorites:
        labels[session_id] = status_label(
            session_id in reserved, session_id in favorites
        )
    return labels


@dataclass
class ScheduledSession:
    """A session on the attendee's schedule, and whether it is reserved or favorited."""

    session: dict
    reserved: bool
    favorite: bool

    def status_label(self):
        """Return "Reserved", "Favorite" or "Reserved, favorite"."""
        return status_label(self.reserved, self.favorite)


def group_schedule_by_date(schedule, catalog=None):
    """Group reserved and favorite sessions by the day they start.

    Args:
        schedule: A schedule as returned by EventsClient.get_schedule, with
            session IDs in "reserved" and "favorites".
        catalog: A SessionCatalog used to look up session details. Sessions not
            found there (or all of them, if there is no catalog) are shown by ID.

    Returns:
        A list of (date, [ScheduledSession, ...]) pairs in date order, with
        sessions in start-time order. `date` is "YYYY-MM-DD", or None for
        sessions without a date, which come last.
    """
    reserved = set(schedule["reserved"])
    favorites = set(schedule["favorites"])

    entries = []
    for session_id in reserved | favorites:
        session = catalog.get(session_id) if catalog is not None else None
        if session is None:
            session = {
                "sessionId": session_id,
                "title": f"Session {session_id} (not in the local catalog)",
            }
        entries.append(ScheduledSession(
            session, session_id in reserved, session_id in favorites
        ))
    entries.sort(key=lambda entry: chronological_key(entry.session))

    groups = []
    for entry in entries:
        day = session_date(entry.session)
        if not groups or groups[-1][0] != day:
            groups.append((day, []))
        groups[-1][1].append(entry)
    return groups
