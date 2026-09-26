"""Tests for the events_ui Streamlit app."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import streamlit as st
from langchain_core.embeddings import DeterministicFakeEmbedding
from streamlit.testing.v1 import AppTest

import events_ui
from aws_events.catalog import SessionCatalog
from aws_events.vector_store import SessionVectorStore
from tests.fakes import make_session


APP_PATH = str(Path(__file__).resolve().parent.parent / "events_ui.py")


def sample_sessions():
    """Return three sessions over two venues and two days."""
    return [
        make_session("s1", "Serverless patterns", "Lambda and SQS",
                     abbreviation="SVS301", venue="MGM Grand",
                     sessionTime={"date": "2026-12-01", "time": "10:00"}),
        make_session("s2", "Vector databases", "Embeddings",
                     abbreviation="AIM201", venue="Venetian",
                     sessionTime={"date": "2026-12-02", "time": "09:00"}),
        make_session("s3", "Lambda tuning", "Cold starts",
                     abbreviation="SVS402", venue="MGM Grand",
                     sessionTime={"date": "2026-12-02", "time": "13:00"}),
    ]


class HelperTests(unittest.TestCase):
    """Pure helper functions."""

    def test_format_day(self):
        """Dates read as short weekday, day and month."""
        self.assertEqual(events_ui.format_day("2026-12-02"), "Wed 2 Dec")
        self.assertEqual(events_ui.format_day(None), "Date not set")

    def test_session_rows_include_schedule_status(self):
        """Rows carry display columns and the session's schedule status."""
        rows = events_ui.session_rows(sample_sessions()[:1], {"s1": "Favorite"})
        self.assertEqual(rows[0]["On schedule"], "Favorite")
        self.assertEqual(rows[0]["Code"], "SVS301")
        self.assertEqual(rows[0]["When"], "Tue 1 Dec 10:00")
        self.assertEqual(rows[0]["Abstract"], "Lambda and SQS")

    def test_format_when_without_time(self):
        """Sessions with no start time show just the day."""
        undated = {"sessionId": "x", "title": "T"}
        self.assertEqual(events_ui.format_when(undated), "Date not set")

    def test_filter_from_choices(self):
        """"All" in a dropdown means that criterion is not filtered."""
        session_filter = events_ui.filter_from_choices(events_ui.ALL, "2026-12-02")
        self.assertIsNone(session_filter.venue)
        self.assertEqual(session_filter.date, "2026-12-02")


class AppTests(unittest.TestCase):
    """Render the whole app with temporary data and a mocked API."""

    def setUp(self):
        """Write a small catalog and index, and patch the API and model."""
        st.cache_resource.clear()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        catalog_path = Path(directory.name) / "sessions.json"
        index_path = Path(directory.name) / "vectors.json"

        embeddings = DeterministicFakeEmbedding(size=16)
        catalog = SessionCatalog("reinvent2026", sample_sessions())
        catalog.save(catalog_path)
        SessionVectorStore.build(catalog, embeddings).save(index_path)

        schedule = {"reserved": ["s3"], "favorites": ["s1"], "personalTime": []}
        patches = [
            mock.patch("aws_events.default_catalog_path", lambda _: catalog_path),
            mock.patch("aws_events.default_index_path", lambda _: index_path),
            mock.patch("aws_events.FastEmbedEmbeddings", lambda: embeddings),
            mock.patch("aws_events.EventsClient.get_schedule",
                       return_value=schedule),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_renders_schedule_grouped_by_day(self):
        """The right panel lists scheduled sessions under their days."""
        app = AppTest.from_file(APP_PATH, default_timeout=30).run()

        self.assertFalse(app.exception)
        headings = [element.value for element in app.markdown]
        self.assertIn("#### Tue 1 Dec", headings)
        self.assertIn("#### Wed 2 Dec", headings)
        schedule_text = " ".join(headings)
        self.assertIn("SVS301 Serverless patterns", schedule_text)
        self.assertIn("Reserved", schedule_text)

    def test_filters_and_search_narrow_the_grid(self):
        """Venue and date dropdowns and the search box filter the grid."""
        app = AppTest.from_file(APP_PATH, default_timeout=30).run()
        self.assertEqual(len(app.dataframe[0].value), 3)

        app.selectbox[0].set_value("MGM Grand").run()
        self.assertEqual(len(app.dataframe[0].value), 2)

        app.selectbox[1].set_value("2026-12-02").run()
        self.assertEqual(list(app.dataframe[0].value["Code"]), ["SVS402"])

        app.text_input[1].input("lambda").run()
        self.assertFalse(app.exception)
        self.assertEqual(list(app.dataframe[0].value["Code"]), ["SVS402"])


if __name__ == "__main__":
    unittest.main()
