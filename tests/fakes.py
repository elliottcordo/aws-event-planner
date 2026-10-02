"""Test doubles shared by the test modules."""

import urllib.parse


class FakeTransport:
    """Record requests and return canned responses in order.

    A response that is an exception is raised instead of returned.
    """

    def __init__(self, *responses):
        """Queue the responses to return, one per request."""
        self.responses = list(responses)
        self.requests = []

    def request(
        self, method, url, params=None, json_body=None, form=None, headers=None
    ):
        """Record the request and return the next canned response.

        Like `requests`, query parameters set to None are left out; the rest
        are appended to the recorded URL so tests can compare whole URLs.
        """
        present_params = {}
        for name, value in (params or {}).items():
            if value is not None:
                present_params[name] = value
        if present_params:
            url = f"{url}?{urllib.parse.urlencode(present_params)}"
        self.requests.append(
            {
                "method": method,
                "url": url,
                "json_body": json_body,
                "form": form,
                "headers": headers or {},
            }
        )
        if not self.responses:
            return None
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeAuthenticator:
    """Return a fixed access token without any network calls."""

    SIGN_IN_URL = "https://signin.example.test/authorize"

    def __init__(self, access_token="test-token", signed_in=True):
        """Store the token to hand out and whether a sign-in is saved."""
        self.access_token = access_token
        self.signed_in = signed_in
        self.signed_out = False

    def get_access_token(self):
        """Return the fixed access token."""
        return self.access_token

    def has_saved_sign_in(self):
        """Return whether the fake is signed in."""
        return self.signed_in

    def sign_in(self, open_browser=None):
        """Pretend to sign in, showing the sign-in URL as the real one does."""
        if open_browser is not None:
            open_browser(self.SIGN_IN_URL)
        self.signed_in = True
        return self.access_token

    def sign_out(self):
        """Record that sign-out was requested."""
        self.signed_out = True
        self.signed_in = False


class MemoryTokenStore:
    """Keep tokens in memory instead of on disk."""

    def __init__(self, tokens=None):
        """Start with the given tokens, or none."""
        self.tokens = tokens

    def load(self):
        """Return the stored tokens."""
        return self.tokens

    def save(self, tokens):
        """Replace the stored tokens."""
        self.tokens = tokens

    def clear(self):
        """Forget the stored tokens."""
        self.tokens = None


def make_session(session_id, title, abstract="", **fields):
    """Return a minimal session dict like the API returns."""
    session = {"sessionId": session_id, "title": title, "abstract": abstract}
    session.update(fields)
    return session


class FakeVectorStore:
    """Return a fixed semantic ranking, whatever the query."""

    def __init__(self, ranked_ids):
        """Store the session IDs to return, best first."""
        self.ranked_ids = ranked_ids
        self.queries = []

    def search(self, query, limit=10):
        """Record the query and return (session_id, score) pairs."""
        self.queries.append(query)
        pairs = []
        for rank, session_id in enumerate(self.ranked_ids[:limit]):
            pairs.append((session_id, 1.0 - rank * 0.1))
        return pairs
