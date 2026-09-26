"""Local JSON copy of an event's full session catalog."""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


# Saved catalogs and search indexes live in data/ at the project root, so the
# CLI finds them no matter which directory it is run from.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIRECTORY = PROJECT_ROOT / "data"


def default_catalog_path(event_id):
    """Return where the session catalog for an event is saved by default."""
    return DATA_DIRECTORY / f"{event_id}-sessions.json"


def session_date(session):
    """Return the day a session starts as "YYYY-MM-DD", or None if unscheduled."""
    session_time = session.get("sessionTime") or {}
    return session_time.get("date")


def session_start_time(session):
    """Return a session's local start time as "HH:MM", or None if not set."""
    session_time = session.get("sessionTime") or {}
    return session_time.get("time")


def chronological_key(session):
    """Sort key that orders sessions by date, then start time, then title.

    Sessions without a date or time sort after those that have one.
    """
    return (
        session_date(session) or "9999-99-99",
        session_start_time(session) or "99:99",
        session.get("title", ""),
    )


@dataclass
class SessionFilter:
    """Criteria a session must meet; a criterion left as None matches anything.

    Attributes:
        venue: Exact venue name, for example "MGM Grand".
        date: The day the session starts, as "YYYY-MM-DD".
    """

    venue: str = None
    date: str = None

    def is_empty(self):
        """Return True if no criteria are set, so every session matches."""
        return self.venue is None and self.date is None

    def matches(self, session):
        """Return True if the session meets every criterion that is set."""
        if self.venue is not None and session.get("venue") != self.venue:
            return False
        if self.date is not None and session_date(session) != self.date:
            return False
        return True


class SessionCatalog:
    """All sessions of one event, downloaded once and kept on disk.

    Usage:
        catalog = SessionCatalog.download(client, "reinvent2026")
        catalog.save(default_catalog_path("reinvent2026"))
        catalog = SessionCatalog.load(default_catalog_path("reinvent2026"))
    """

    def __init__(self, event_id, sessions, downloaded_at=None):
        """Create a catalog.

        Args:
            event_id: The event the sessions belong to.
            sessions: A list of session dicts as returned by the API.
            downloaded_at: ISO 8601 UTC time the sessions were fetched.
        """
        self.event_id = event_id
        self.sessions = sessions
        self.downloaded_at = downloaded_at
        self._sessions_by_id = {session["sessionId"]: session for session in sessions}

    @classmethod
    def download(cls, client, event_id, locale=None):
        """Fetch every session of an event with an EventsClient."""
        sessions = client.list_sessions(event_id, locale=locale)
        downloaded_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return cls(event_id, sessions, downloaded_at)

    @classmethod
    def load(cls, path):
        """Read a catalog previously written by `save`.

        Raises:
            FileNotFoundError: If there is no catalog at `path`.
        """
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(data["eventId"], data["sessions"], data.get("downloadedAt"))

    def save(self, path):
        """Write the catalog to `path` as JSON, creating folders as needed."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "eventId": self.event_id,
            "downloadedAt": self.downloaded_at,
            "sessions": self.sessions,
        }
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def venues(self):
        """Return the distinct venue names, sorted; sessions without one are ignored."""
        names = {session["venue"] for session in self.sessions if session.get("venue")}
        return sorted(names)

    def dates(self):
        """Return the distinct session dates ("YYYY-MM-DD"), sorted."""
        days = {session_date(session) for session in self.sessions}
        days.discard(None)
        return sorted(days)

    def filter_sessions(self, session_filter):
        """Return the sessions matching a SessionFilter, in chronological order."""
        matching = [
            session for session in self.sessions if session_filter.matches(session)
        ]
        return sorted(matching, key=chronological_key)

    def get(self, session_id):
        """Return the session with this ID, or None if it is not in the catalog."""
        return self._sessions_by_id.get(session_id)

    def __len__(self):
        """Return the number of sessions."""
        return len(self.sessions)
