"""Tests for aws_events.auth."""

import socket
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

from aws_events.auth import (
    Authenticator,
    TokenStore,
    authorization_code_from_callback,
    build_authorization_url,
    make_pkce_pair,
    wait_for_callback,
)
from aws_events.errors import ApiError, SignInError
from tests.fakes import FakeTransport, MemoryTokenStore


NOW = 1_000_000.0


def fixed_clock():
    """Return a fixed time so expiry checks are predictable."""
    return NOW


class TokenStoreTests(unittest.TestCase):
    """Saving and loading tokens on disk."""

    def test_round_trip_and_clear(self):
        """Tokens can be saved, loaded, and cleared; the file is private."""
        with tempfile.TemporaryDirectory() as directory:
            store = TokenStore(Path(directory) / "tokens.json")
            self.assertIsNone(store.load())

            store.save({"access_token": "abc"})
            self.assertEqual(store.load(), {"access_token": "abc"})
            self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)

            store.clear()
            self.assertIsNone(store.load())


class AuthenticatorTests(unittest.TestCase):
    """Choosing between the saved token, a refresh, and a new sign-in."""

    def test_fresh_token_is_reused(self):
        """A token that is not about to expire is used without a network call."""
        store = MemoryTokenStore({"access_token": "saved", "expires_at": NOW + 600})
        transport = FakeTransport()
        authenticator = Authenticator(store, transport, clock=fixed_clock)

        self.assertEqual(authenticator.get_access_token(), "saved")
        self.assertEqual(transport.requests, [])

    def test_expired_token_is_refreshed(self):
        """An expiring token is refreshed and the refresh token is kept."""
        store = MemoryTokenStore({
            "access_token": "old",
            "refresh_token": "refresh-me",
            "expires_at": NOW + 30,
        })
        transport = FakeTransport({"access_token": "new", "expires_in": 3600})
        authenticator = Authenticator(store, transport, clock=fixed_clock)

        self.assertEqual(authenticator.get_access_token(), "new")

        form = transport.requests[0]["form"]
        self.assertEqual(form["grant_type"], "refresh_token")
        self.assertEqual(form["refresh_token"], "refresh-me")
        # The old refresh token is kept when the server does not send a new one.
        self.assertEqual(store.tokens["refresh_token"], "refresh-me")
        self.assertEqual(store.tokens["expires_at"], NOW + 3600)

    def test_missing_token_triggers_sign_in(self):
        """With no saved token, the browser flow runs and tokens are saved."""
        store = MemoryTokenStore()
        transport = FakeTransport({"access_token": "signed-in"})
        authenticator = Authenticator(store, transport, clock=fixed_clock)

        def fake_callback(authorization_url, open_browser):
            query = urllib.parse.urlparse(authorization_url).query
            state = urllib.parse.parse_qs(query)["state"][0]
            return {"code": ["the-code"], "state": [state]}

        with mock.patch("aws_events.auth.wait_for_callback", fake_callback):
            self.assertEqual(authenticator.get_access_token(), "signed-in")

        form = transport.requests[0]["form"]
        self.assertEqual(form["grant_type"], "authorization_code")
        self.assertEqual(form["code"], "the-code")
        self.assertEqual(store.tokens["access_token"], "signed-in")

    def test_rejected_refresh_token_falls_back_to_sign_in(self):
        """An invalid_grant reply clears the tokens and starts a new sign-in."""
        store = MemoryTokenStore({
            "access_token": "old", "refresh_token": "dead", "expires_at": NOW - 10,
        })
        transport = FakeTransport(ApiError("invalid_grant", 400))
        authenticator = Authenticator(store, transport, clock=fixed_clock)

        with mock.patch.object(authenticator, "sign_in",
                               return_value="fresh") as sign_in:
            self.assertEqual(authenticator.get_access_token(), "fresh")
        sign_in.assert_called_once()
        self.assertIsNone(store.tokens)

    def test_refresh_server_error_is_raised(self):
        """A server error during refresh is not treated as an expired sign-in."""
        store = MemoryTokenStore({
            "access_token": "old", "refresh_token": "ok", "expires_at": NOW - 10,
        })
        transport = FakeTransport(ApiError("unavailable", 503))
        authenticator = Authenticator(store, transport, clock=fixed_clock)

        with self.assertRaises(ApiError):
            authenticator.get_access_token()
        self.assertIsNotNone(store.tokens)

    def test_no_implicit_sign_in_when_disabled(self):
        """With sign_in_when_needed=False, a missing token raises, no browser."""
        authenticator = Authenticator(
            MemoryTokenStore(), FakeTransport(), sign_in_when_needed=False
        )
        with mock.patch.object(authenticator, "sign_in") as sign_in:
            with self.assertRaisesRegex(SignInError, "not signed in"):
                authenticator.get_access_token()
        sign_in.assert_not_called()

    def test_expired_sign_in_message_when_disabled(self):
        """A rejected refresh with sign-in disabled says the sign-in expired."""
        store = MemoryTokenStore({
            "access_token": "old", "refresh_token": "dead", "expires_at": NOW - 10,
        })
        authenticator = Authenticator(
            store, FakeTransport(ApiError("invalid_grant", 400)),
            clock=fixed_clock, sign_in_when_needed=False,
        )
        with self.assertRaisesRegex(SignInError, "expired"):
            authenticator.get_access_token()

    def test_has_saved_sign_in(self):
        """has_saved_sign_in reflects whether tokens are stored."""
        self.assertFalse(Authenticator(MemoryTokenStore(), None).has_saved_sign_in())
        store = MemoryTokenStore({"access_token": "x"})
        self.assertTrue(Authenticator(store, None).has_saved_sign_in())

    def test_sign_out_clears_tokens(self):
        """sign_out removes the saved tokens."""
        store = MemoryTokenStore({"access_token": "x"})
        Authenticator(store, FakeTransport()).sign_out()
        self.assertIsNone(store.tokens)


class SignInHelperTests(unittest.TestCase):
    """PKCE, sign-in URL and callback validation."""

    def test_pkce_pair_is_unique_and_url_safe(self):
        """Each PKCE pair is new and the challenge has no padding."""
        verifier, challenge = make_pkce_pair()
        self.assertNotEqual(verifier, make_pkce_pair()[0])
        self.assertNotIn("=", challenge)
        self.assertEqual(len(challenge), 43)

    def test_authorization_url_contains_challenge_and_state(self):
        """The sign-in URL carries the PKCE challenge and state."""
        url = build_authorization_url("the-challenge", "the-state")
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(query["code_challenge"], ["the-challenge"])
        self.assertEqual(query["state"], ["the-state"])
        self.assertEqual(query["code_challenge_method"], ["S256"])

    def test_callback_returns_code(self):
        """A valid callback yields its authorization code."""
        params = {"code": ["abc"], "state": ["s"]}
        self.assertEqual(authorization_code_from_callback(params, "s"), "abc")

    def test_callback_errors(self):
        """Errors, a wrong state, or a missing code raise SignInError."""
        bad_callbacks = [
            {"error": ["access_denied"]},
            {"code": ["abc"], "state": ["wrong"]},
            {"state": ["s"]},
            {},
        ]
        for params in bad_callbacks:
            with self.subTest(params=params):
                with self.assertRaises(SignInError):
                    authorization_code_from_callback(params, "s")


def free_port():
    """Return a local TCP port that is currently free."""
    with socket.socket() as probe:
        probe.bind(("localhost", 0))
        return probe.getsockname()[1]


class WaitForCallbackTests(unittest.TestCase):
    """The local server that receives the sign-in redirect."""

    def test_returns_callback_params(self):
        """The redirect's query parameters are returned, ignoring other paths."""
        port = free_port()

        def fake_browser(_url):
            def visit():
                base = f"http://localhost:{port}"
                try:
                    urllib.request.urlopen(f"{base}/favicon.ico")
                except urllib.error.HTTPError as not_found:
                    not_found.close()
                urllib.request.urlopen(f"{base}/callback?code=abc&state=s").read()
            threading.Thread(target=visit).start()

        params = wait_for_callback("unused", fake_browser, port=port,
                                   timeout_seconds=5)
        self.assertEqual(params, {"code": ["abc"], "state": ["s"]})

    def test_times_out(self):
        """No redirect within the timeout raises SignInError."""
        with self.assertRaisesRegex(SignInError, "timed out"):
            wait_for_callback("unused", lambda _url: None, port=free_port(),
                              timeout_seconds=0.2)

    def test_busy_port_gives_clear_error(self):
        """A port already in use raises SignInError, not OSError."""
        with socket.socket() as blocker:
            blocker.bind(("localhost", 0))
            blocker.listen()
            port = blocker.getsockname()[1]
            with self.assertRaisesRegex(SignInError, f"port {port}"):
                wait_for_callback("unused", lambda _url: None, port=port)


if __name__ == "__main__":
    unittest.main()
