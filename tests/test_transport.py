"""Tests for aws_events.transport."""

import unittest

import requests

from aws_events.errors import ApiError
from aws_events.transport import HttpTransport, error_message_from_body


def make_response(status_code, body=b""):
    """Return a requests.Response with the given status and raw body."""
    response = requests.Response()
    response.status_code = status_code
    response._content = body  # pylint: disable=protected-access
    return response


class FakeSession:
    """Stand-in for requests.Session that returns one canned response."""

    def __init__(self, response=None, error=None):
        """Store the response to return, or the error to raise."""
        self.response = response
        self.error = error
        self.calls = []

    def request(self, method, url, **kwargs):
        """Record the call, then raise the error or return the response."""
        self.calls.append({"method": method, "url": url, **kwargs})
        if self.error is not None:
            raise self.error
        return self.response


class HttpTransportTests(unittest.TestCase):
    """Sending requests and handling responses."""

    def test_passes_arguments_to_requests_and_decodes_json(self):
        """Params, JSON body and headers reach requests; JSON is decoded."""
        session = FakeSession(make_response(200, b'{"ok": true}'))
        transport = HttpTransport(timeout_seconds=5, session=session)

        result = transport.request(
            "POST",
            "https://example.test/x",
            params={"a": "1"},
            json_body={"b": 2},
            headers={"H": "v"},
        )

        self.assertEqual(result, {"ok": True})
        call = session.calls[0]
        self.assertEqual(call["params"], {"a": "1"})
        self.assertEqual(call["json"], {"b": 2})
        self.assertEqual(call["headers"], {"H": "v"})
        self.assertEqual(call["timeout"], 5)

    def test_empty_body_returns_none(self):
        """A 204 No Content response returns None."""
        transport = HttpTransport(session=FakeSession(make_response(204)))
        self.assertIsNone(transport.request("DELETE", "https://example.test/x"))

    def test_error_status_raises_api_error_with_message(self):
        """Error statuses raise ApiError carrying the API's message and code."""
        response = make_response(404, b'{"message": "No such session"}')
        transport = HttpTransport(session=FakeSession(response))

        with self.assertRaises(ApiError) as caught:
            transport.request("GET", "https://example.test/x")

        self.assertEqual(caught.exception.status_code, 404)
        self.assertEqual(caught.exception.message, "No such session")

    def test_connection_failure_raises_api_error(self):
        """Network errors become ApiError with no status code."""
        error = requests.ConnectionError("unreachable")
        transport = HttpTransport(session=FakeSession(error=error))

        with self.assertRaises(ApiError) as caught:
            transport.request("GET", "https://example.test/x")

        self.assertIsNone(caught.exception.status_code)
        self.assertIn("unreachable", str(caught.exception))


class ErrorMessageTests(unittest.TestCase):
    """Extracting error messages from response bodies."""

    def test_uses_message_field(self):
        """The API's message field is used when present."""
        body = '{"message": "Sign in to continue"}'
        self.assertEqual(error_message_from_body(body), "Sign in to continue")

    def test_falls_back_to_raw_body(self):
        """Bodies without a message field are returned unchanged."""
        self.assertEqual(error_message_from_body("Bad Gateway"), "Bad Gateway")
        self.assertEqual(error_message_from_body('["x"]'), '["x"]')


if __name__ == "__main__":
    unittest.main()
