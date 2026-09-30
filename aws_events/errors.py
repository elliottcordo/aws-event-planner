"""Exceptions raised by the aws_events package."""


class EventsError(RuntimeError):
    """Base class for every error raised by this package."""


class ApiError(EventsError):
    """Raised when the AWS Events API returns an error or cannot be reached.

    Attributes:
        status_code: The HTTP status code, or None if no response was received.
        message: A human-readable description of the problem.
    """

    def __init__(self, message, status_code=None):
        """Store the message and status code."""
        self.status_code = status_code
        self.message = message
        if status_code is None:
            super().__init__(message)
        else:
            super().__init__(f"HTTP {status_code}: {message}")


class FeatureDisabledError(ApiError):
    """Raised when the API has switched an operation off (HTTP 409).

    The API uses 409 only for operations it has intentionally disabled, such
    as booking before it opens; retrying won't help until it's switched on.

    Attributes:
        api_message: The API's own wording, kept for troubleshooting.
    """

    MESSAGE = "This feature is not yet enabled"

    def __init__(self, api_message=None):
        """Store the API's message; the user-facing message is fixed."""
        super().__init__(self.MESSAGE, 409)
        self.api_message = api_message

    def __str__(self):
        """Return the plain message, without the "HTTP 409:" prefix."""
        return self.MESSAGE


class SignInError(EventsError):
    """Raised when the AWS Builder ID sign-in flow fails."""
