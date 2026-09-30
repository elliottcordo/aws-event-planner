"""Tests for web.jobs."""

import threading
import unittest

from web.jobs import FAILED, SUCCEEDED, JobBoard


class JobTests(unittest.TestCase):
    """Running work in the background."""

    def test_success_records_result_message(self):
        """A job's return value becomes its message."""
        job = JobBoard().start("work", lambda job: "All done.")
        job.wait(5)
        self.assertEqual(job.status, SUCCEEDED)
        self.assertEqual(job.message, "All done.")

    def test_failure_records_error(self):
        """An exception marks the job failed with the error as its message."""
        def work(_job):
            raise RuntimeError("Download failed")

        job = JobBoard().start("work", work)
        job.wait(5)
        self.assertEqual(job.status, FAILED)
        self.assertEqual(job.message, "Download failed")

    def test_progress_and_details(self):
        """Work can report progress and share details."""
        release = threading.Event()

        def work(job):
            job.details["url"] = "https://example.test"
            job.report("Halfway")
            release.wait(5)

        job = JobBoard().start("work", work)
        for _ in range(100):
            if job.message == "Halfway":
                break
            threading.Event().wait(0.01)
        self.assertEqual(job.message, "Halfway")
        self.assertEqual(job.details["url"], "https://example.test")
        release.set()
        job.wait(5)

    def test_one_running_job_per_name(self):
        """Starting a name that is still running returns the running job."""
        release = threading.Event()
        board = JobBoard()
        first = board.start("rebuild", lambda job: release.wait(5))
        second = board.start("rebuild", lambda job: "never runs")
        self.assertIs(first, second)
        release.set()
        first.wait(5)
        third = board.start("rebuild", lambda job: "runs")
        self.assertIsNot(first, third)
        third.wait(5)
        self.assertIs(board.get("rebuild"), third)


if __name__ == "__main__":
    unittest.main()
