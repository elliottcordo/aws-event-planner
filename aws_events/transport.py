"""Minimal HTTP transport that sends requests and decodes JSON responses."""

import json

import requests

from aws_events.errors import ApiError


class HttpTransport:
    """Send HTTP requests with `requests` and return decoded JSON.

    The API client and the authenticator both talk to the network only through
    this class, so tests can swap in a fake with the same `request` method.
    """

    def __init__(self, timeout_seconds=30, session=None):
        """Create a transport.

        Args:
            timeout_seconds: How long to wait for a response before giving up.
            session: A requests.Session to reuse; a new one is made if omitted.
        """
        self.timeout_seconds = timeout_seconds
        self.session = session or requests.Session()

    def request(
        self, method, url, params=None, json_body=None, form=None, headers=None
    ):
        """Send one HTTP request and return the decoded JSON body.

        Args:
            method: The HTTP method, such as "GET" or "POST".
            url: The full URL to call.
            params: Optional dict of query parameters; None values are left out.
            json_body: Optional dict sent as a JSON body.
            form: Optional dict sent as a URL-encoded form body.
            headers: Optional dict of extra request headers.

        Returns:
            The decoded JSON body, or None when the response has no body.

        Raises:
            ApiError: If the server returns an error status or cannot be reached.
        """
        try:
            response = self.session.request(
                method,
                url,
                params=params,
                json=json_body,
                data=form,
                headers=headers,
                timeout=self.timeout_seconds,
            )
        except requests.RequestException as error:
            raise ApiError(f"Request failed: {error}") from error

        if not response.ok:
            message = error_message_from_body(response.text)
            raise ApiError(message, response.status_code)
        if not response.content:
            return None
        return response.json()


def error_message_from_body(body):
    """Return the most useful error message found in an error response body.

    The API returns errors as {"message": "..."}; anything else is returned
    as-is so no detail is lost.
    """
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return body
    if isinstance(parsed, dict) and "message" in parsed:
        return parsed["message"]
    return body
