"""Client for the AWS Events API (https://api.awsevents.com/v1)."""

import urllib.parse
from dataclasses import dataclass

from aws_events.errors import ApiError, FeatureDisabledError

# The API answers 409 only when an operation has been switched off.
FEATURE_DISABLED_STATUS = 409

API_BASE_URL = "https://api.awsevents.com/v1"

# The API accepts at most this many session IDs in one reserve or favorite call.
MAX_SESSIONS_PER_REQUEST = 10


@dataclass
class PersonalTime:
    """A block of time on the attendee's schedule that is not a session.

    Times are UTC and written as "YYYY-MM-DDTHH:MM:00", for example
    "2026-12-02T16:00:00". The length must be a multiple of 5 minutes.
    """

    title: str
    description: str
    start_date_time: str
    end_date_time: str
    location: str = None

    def to_api_body(self):
        """Return the entry as the JSON body the API expects."""
        body = {
            "title": self.title,
            "description": self.description,
            "startDateTime": self.start_date_time,
            "endDateTime": self.end_date_time,
        }
        if self.location:
            body["location"] = self.location
        return body


class EventsClient:
    """Call the AWS Events API: events, sessions, and the attendee's schedule.

    Listing and getting events never needs sign-in. Session calls are tried
    without sign-in first, since public events allow that, and signed in only
    if the API asks. Schedule calls always need an access token. Tokens are
    fetched from `authenticator` only when first needed.

    Usage:
        client = EventsClient(HttpTransport(), authenticator)
        for event in client.list_events():
            print(event["name"])
    """

    def __init__(self, transport, authenticator=None, base_url=API_BASE_URL):
        """Create a client.

        Args:
            transport: An HttpTransport (or a fake with the same `request`).
            authenticator: Provides `get_access_token()` for signed-in calls.
                May be None if only public calls are made.
            base_url: The API root URL.
        """
        self.transport = transport
        self.authenticator = authenticator
        self.base_url = base_url

    # Events

    def list_events(self, include_past=False):
        """Return ongoing and upcoming events, plus past ones if requested."""
        params = {"includePast": "true"} if include_past else None
        response = self._send("GET", "/events", params=params, signed_in=False)
        return response["items"]

    def get_event(self, event_id):
        """Return one event by ID, even if it has already ended."""
        path = build_path("events", event_id)
        return self._send("GET", path, signed_in=False)["event"]

    # Sessions

    def list_sessions_page(
        self, event_id, locale=None, include_abstracts=True, next_token=None
    ):
        """Return one page of an event's session catalog.

        Returns:
            A dict with "items", "totalCount" and, if more pages remain,
            "nextToken" to pass to the next call.
        """
        params = {"locale": locale, "nextToken": next_token}
        if not include_abstracts:
            params["includeAbstracts"] = "false"
        path = build_path("events", event_id, "sessions")
        return self._send_signing_in_if_needed("GET", path, params=params)

    def list_sessions(self, event_id, locale=None, include_abstracts=True):
        """Return every session in an event, fetching all pages."""
        sessions = []
        next_token = None
        while True:
            page = self.list_sessions_page(
                event_id, locale, include_abstracts, next_token
            )
            sessions.extend(page["items"])
            next_token = page.get("nextToken")
            if not next_token:
                return sessions

    def get_session(self, event_id, session_id, locale=None):
        """Return one session in an event."""
        path = build_path("events", event_id, "sessions", session_id)
        params = {"locale": locale}
        return self._send_signing_in_if_needed("GET", path, params=params)["session"]

    # Schedule

    def get_schedule(self, event_id, include_session_details=False, locale=None):
        """Return the attendee's schedule for an event.

        Args:
            event_id: The event whose schedule to return.
            include_session_details: If True, replace each session ID in
                "reserved" and "favorites" with the full session, fetched with
                get_session. This makes one extra request per session.
            locale: Language for session details, for example "en-US". Only
                used when include_session_details is True.

        Returns:
            A dict with "reserved", "favorites" and "personalTime" lists.
        """
        path = build_path("events", event_id, "schedule")
        schedule = self._send("GET", path)["schedule"]
        if not include_session_details:
            return schedule

        # A session can be both reserved and a favorite; fetch it only once.
        sessions_by_id = {}
        for session_id in schedule["reserved"] + schedule["favorites"]:
            if session_id not in sessions_by_id:
                sessions_by_id[session_id] = self.get_session(
                    event_id, session_id, locale=locale
                )

        for list_name in ("reserved", "favorites"):
            schedule[list_name] = [
                sessions_by_id[session_id] for session_id in schedule[list_name]
            ]
        return schedule

    def reserve_sessions(self, event_id, session_ids):
        """Reserve seats in up to 10 sessions.

        Some sessions can fail while others succeed, so always check the
        "failed" list in the result.

        Returns:
            A dict with "successful" and "failed" lists.
        """
        check_session_count(session_ids)
        path = build_path("events", event_id, "reservations")
        body = {"sessionIds": list(session_ids)}
        return self._send("POST", path, body=body)["result"]

    def cancel_reservation(self, event_id, session_id):
        """Cancel the reservation for one session."""
        path = build_path("events", event_id, "reservations", session_id)
        self._send("DELETE", path)

    def add_favorites(self, event_id, session_ids):
        """Mark up to 10 sessions as favorites. This does not reserve a seat.

        Returns:
            A dict with "successful" and "failed" lists.
        """
        check_session_count(session_ids)
        path = build_path("events", event_id, "favorites")
        body = {"sessionIds": list(session_ids)}
        return self._send("POST", path, body=body)["result"]

    def remove_favorite(self, event_id, session_id):
        """Remove one session from favorites."""
        path = build_path("events", event_id, "favorites", session_id)
        self._send("DELETE", path)

    def create_personal_time(self, event_id, personal_time):
        """Add a PersonalTime entry to the attendee's schedule."""
        path = build_path("events", event_id, "personal-time")
        self._send("POST", path, body=personal_time.to_api_body())

    def update_personal_time(self, event_id, personal_time_id, personal_time):
        """Replace a personal time entry. Fields left empty are cleared."""
        path = build_path("events", event_id, "personal-time", personal_time_id)
        self._send("PUT", path, body=personal_time.to_api_body())

    def delete_personal_time(self, event_id, personal_time_id):
        """Remove a personal time entry. Removing a missing entry succeeds."""
        path = build_path("events", event_id, "personal-time", personal_time_id)
        self._send("DELETE", path)

    # Internals

    def _send_signing_in_if_needed(self, method, path, params=None):
        """Send a request without a token, retrying signed in if the API says 401.

        Sessions of public events can be read anonymously; sessions of events
        that require attendee sign-in cannot.
        """
        try:
            return self._send(method, path, params=params, signed_in=False)
        except ApiError as error:
            if error.status_code != 401 or self.authenticator is None:
                raise
        return self._send(method, path, params=params, signed_in=True)

    def _send(self, method, path, params=None, body=None, signed_in=True):
        """Send a request to the API and return the decoded JSON reply.

        Args:
            method: The HTTP method.
            path: The path below the base URL, such as "/events".
            params: Optional query parameters; entries set to None are skipped.
            body: Optional dict sent as the JSON request body.
            signed_in: Whether to send the attendee's access token.
        """
        if not signed_in:
            return self._request(method, path, params, body, headers={})
        if self.authenticator is None:
            raise ValueError("This call needs an authenticator to sign in")

        try:
            return self._request(method, path, params, body, self._auth_headers())
        except ApiError as error:
            if error.status_code != 401:
                raise
        # The API rejected a token that had not reached its recorded expiry
        # (for example, it was revoked). Forget it and sign in again, once.
        self.authenticator.sign_out()
        return self._request(method, path, params, body, self._auth_headers())

    def _auth_headers(self):
        """Return the Authorization header for the current access token."""
        access_token = self.authenticator.get_access_token()
        return {"Authorization": f"Bearer {access_token}"}

    def _request(self, method, path, params, body, headers):
        """Send one request through the transport.

        Raises:
            FeatureDisabledError: If the API has switched the operation off.
            ApiError: For any other error response.
        """
        try:
            return self.transport.request(
                method,
                self.base_url + path,
                params=params,
                json_body=body,
                headers=headers,
            )
        except ApiError as error:
            if error.status_code == FEATURE_DISABLED_STATUS:
                raise FeatureDisabledError(error.message) from error
            raise


def build_path(*segments):
    """Join URL path segments, escaping each so IDs cannot change the path.

    Example:
        build_path("events", "reinvent2026") returns "/events/reinvent2026".
    """
    escaped = [urllib.parse.quote(str(segment), safe="") for segment in segments]
    return "/" + "/".join(escaped)


def check_session_count(session_ids):
    """Raise ValueError unless there are between 1 and 10 session IDs."""
    if not 1 <= len(session_ids) <= MAX_SESSIONS_PER_REQUEST:
        raise ValueError(
            f"Expected between 1 and {MAX_SESSIONS_PER_REQUEST} session IDs, "
            f"got {len(session_ids)}"
        )
