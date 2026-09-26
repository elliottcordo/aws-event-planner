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


class SignInError(EventsError):
    """Raised when the AWS Builder ID sign-in flow fails."""
