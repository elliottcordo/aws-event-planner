"""AWS Builder ID sign-in (OAuth 2.0 with PKCE) and token storage."""

import base64
import hashlib
import http.server
import json
import secrets
import time
import urllib.parse
import webbrowser
from pathlib import Path

from aws_events.errors import EventsError, SignInError


AUTHORIZE_URL = "https://oauth.awsevents.com/oauth2/authorize"
TOKEN_URL = "https://oauth.awsevents.com/oauth2/token"
CLIENT_ID = "7vmom55m1qstvq8i71ph127bfq"
CALLBACK_HOST = "localhost"
CALLBACK_PORT = 8484
REDIRECT_URI = f"http://{CALLBACK_HOST}:{CALLBACK_PORT}/callback"
SCOPE = "openid email events/access"
DEFAULT_TOKEN_PATH = Path.home() / ".aws-events-token.json"

# Refresh a little early so a token does not expire mid-request.
EXPIRY_MARGIN_SECONDS = 60


class TokenStore:
    """Save and load OAuth tokens as a JSON file readable only by the user."""

    def __init__(self, path=DEFAULT_TOKEN_PATH):
        """Create a store backed by the file at `path`."""
        self.path = Path(path)

    def load(self):
        """Return the saved tokens as a dict, or None if none are saved.

        Raises:
            EventsError: If the token file exists but cannot be read.
        """
        if not self.path.exists():
            return None
        try:
            return json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError) as error:
            raise EventsError(f"Could not read {self.path}: {error}") from error

    def save(self, tokens):
        """Write the tokens to disk, readable only by the current user."""
        self.path.touch(mode=0o600, exist_ok=True)
        self.path.chmod(0o600)
        self.path.write_text(json.dumps(tokens, indent=2) + "\n")

    def clear(self):
        """Delete the saved tokens, if any."""
        self.path.unlink(missing_ok=True)


class Authenticator:
    """Provide a valid access token, signing in or refreshing as needed.

    Usage:
        authenticator = Authenticator(TokenStore(), HttpTransport())
        token = authenticator.get_access_token()
    """

    def __init__(self, token_store, transport, open_browser=webbrowser.open,
                 clock=time.time):
        """Create an authenticator.

        Args:
            token_store: A TokenStore (or anything with load/save/clear).
            transport: An HttpTransport used to call the token endpoint.
            open_browser: Called with the sign-in URL to show it to the user.
            clock: Returns the current time in seconds; replaceable in tests.
        """
        self.token_store = token_store
        self.transport = transport
        self.open_browser = open_browser
        self.clock = clock

    def get_access_token(self):
        """Return a valid access token.

        Uses the saved token if it is still fresh, refreshes it if it has
        expired, and falls back to a full browser sign-in otherwise.
        """
        tokens = self.token_store.load()
        if not tokens:
            return self.sign_in()
        if tokens.get("expires_at", 0) > self.clock() + EXPIRY_MARGIN_SECONDS:
            return tokens["access_token"]
        refresh_token = tokens.get("refresh_token")
        if not refresh_token:
            return self.sign_in()
        return self.refresh(refresh_token)

    def sign_in(self):
        """Run the browser sign-in flow, save the tokens and return the access token.

        Raises:
            SignInError: If the user cancels or the response is not valid.
        """
        verifier, challenge = make_pkce_pair()
        state = secrets.token_urlsafe(32)
        authorization_url = build_authorization_url(challenge, state)

        callback_params = wait_for_callback(authorization_url, self.open_browser)
        code = authorization_code_from_callback(callback_params, state)

        tokens = self._request_tokens({
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "code": code,
            "code_verifier": verifier,
        })
        self._save(tokens)
        return tokens["access_token"]

    def refresh(self, refresh_token):
        """Exchange a refresh token for a new access token and save it."""
        tokens = self._request_tokens({
            "grant_type": "refresh_token",
            "client_id": CLIENT_ID,
            "refresh_token": refresh_token,
        })
        # The token endpoint does not always return a new refresh token.
        tokens.setdefault("refresh_token", refresh_token)
        self._save(tokens)
        return tokens["access_token"]

    def sign_out(self):
        """Forget the saved tokens."""
        self.token_store.clear()

    def _request_tokens(self, form_values):
        """POST form values to the token endpoint and return the JSON reply."""
        return self.transport.request("POST", TOKEN_URL, form=form_values)

    def _save(self, tokens):
        """Record when the tokens expire and save them."""
        tokens = dict(tokens)
        lifetime_seconds = int(tokens.get("expires_in", 3600))
        tokens["expires_at"] = self.clock() + lifetime_seconds
        self.token_store.save(tokens)


def make_pkce_pair():
    """Return a new PKCE (verifier, challenge) pair using the S256 method."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def build_authorization_url(code_challenge, state):
    """Return the AWS Builder ID sign-in URL for the given PKCE challenge."""
    query = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "identity_provider": "AWSBuilderID",
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
    })
    return f"{AUTHORIZE_URL}?{query}"


def authorization_code_from_callback(callback_params, expected_state):
    """Validate the sign-in callback and return its authorization code.

    Args:
        callback_params: Query parameters from the callback, as returned by
            urllib.parse.parse_qs (each value is a list).
        expected_state: The state value sent in the sign-in URL.

    Raises:
        SignInError: If sign-in failed, the state does not match, or no code
            was returned.
    """
    if "error" in callback_params:
        raise SignInError(f"Sign-in failed: {callback_params['error'][0]}")
    if callback_params.get("state", [None])[0] != expected_state:
        raise SignInError("Sign-in failed: OAuth state did not match")
    code = callback_params.get("code", [None])[0]
    if not code:
        raise SignInError("Sign-in failed: no authorization code was returned")
    return code


class CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Handle the single redirect the browser makes after sign-in."""

    def do_GET(self):  # pylint: disable=invalid-name
        """Store the callback query parameters on the server and reply."""
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_error(404)
            return
        self.server.callback_params = urllib.parse.parse_qs(parsed.query)
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Sign-in complete. You can close this window.\n")

    def log_message(self, format, *args):  # pylint: disable=redefined-builtin
        """Silence the default request logging."""


def wait_for_callback(authorization_url, open_browser):
    """Open the sign-in page and wait for the browser to redirect back.

    Returns:
        The callback query parameters, or an empty dict if none arrived.
    """
    server = http.server.HTTPServer((CALLBACK_HOST, CALLBACK_PORT), CallbackHandler)
    server.callback_params = {}
    try:
        open_browser(authorization_url)
        server.handle_request()
    finally:
        server.server_close()
    return server.callback_params
