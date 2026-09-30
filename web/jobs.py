"""Run slow work (sign-in, rebuilding the search index) in the background.

The web page starts a job and then polls its status, so no request has to
wait minutes for an answer.
"""

import threading


RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"


class BackgroundJob:
    """One piece of work running in its own thread.

    The work function receives the job, so it can report progress with
    `job.report("...")` and share values such as a sign-in link through
    `job.details`.
    """

    def __init__(self, work):
        """Create a job for `work`, a function that takes the job."""
        self.work = work
        self.status = RUNNING
        self.message = "Starting..."
        self.details = {}
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        """Start the work in a background thread and return the job."""
        self._thread.start()
        return self

    def report(self, message):
        """Record a progress message for the status display."""
        self.message = message

    def is_running(self):
        """Return True until the work has finished, successfully or not."""
        return self.status == RUNNING

    def wait(self, timeout_seconds=None):
        """Block until the work finishes; used by tests."""
        self._thread.join(timeout_seconds)

    def _run(self):
        """Run the work and record how it ended."""
        try:
            result = self.work(self)
        except Exception as error:  # pylint: disable=broad-exception-caught
            # Any failure, expected or not, must reach the page as a message
            # rather than vanish with the thread.
            self.status = FAILED
            self.message = str(error) or error.__class__.__name__
            return
        self.status = SUCCEEDED
        if result:
            self.message = result


class JobBoard:
    """At most one job per name, for example "sign-in" or "rebuild:reinvent2026"."""

    def __init__(self):
        """Start with no jobs."""
        self._jobs = {}
        self._lock = threading.Lock()

    def start(self, name, work):
        """Start a job under `name`, unless one with that name is still running.

        Returns:
            The running job: the existing one if there is one, else a new one.
        """
        with self._lock:
            existing = self._jobs.get(name)
            if existing is not None and existing.is_running():
                return existing
            job = BackgroundJob(work).start()
            self._jobs[name] = job
            return job

    def get(self, name):
        """Return the latest job with this name, or None."""
        return self._jobs.get(name)
