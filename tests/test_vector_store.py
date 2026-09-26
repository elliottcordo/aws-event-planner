"""Tests for aws_events.vector_store."""

import tempfile
import unittest
from pathlib import Path

from langchain_core.embeddings import DeterministicFakeEmbedding

from aws_events.catalog import SessionCatalog
from aws_events.vector_store import (
    SessionVectorStore,
    session_to_document,
    session_to_text,
)
from tests.fakes import make_session


def sample_catalog():
    """Return a small catalog with three distinct sessions."""
    return SessionCatalog("e1", [
        make_session("s1", "Serverless patterns", "Lambda and queues"),
        make_session("s2", "Vector databases", "Embeddings at scale"),
        make_session("s3", "Cost optimization", "Save on compute"),
    ])


class SessionTextTests(unittest.TestCase):
    """Turning a session into searchable text."""

    def test_text_includes_labels_and_speakers(self):
        """Title, code, abstract, labels and speakers all appear in the text."""
        session = make_session(
            "s1", "Serverless patterns", "Lambda and queues",
            abbreviation="SVS301",
            level="300 - Advanced",
            services=["AWS Lambda", "Amazon SQS"],
            speakers=[{"name": "Ana Silva"}],
        )

        text = session_to_text(session)

        for expected in ["Serverless patterns", "SVS301", "Lambda and queues",
                         "300 - Advanced", "Services: AWS Lambda, Amazon SQS",
                         "Speakers: Ana Silva"]:
            self.assertIn(expected, text)

    def test_missing_fields_are_skipped(self):
        """Sessions with only a title produce just the title."""
        text = session_to_text({"sessionId": "s1", "title": "Only a title"})
        self.assertEqual(text, "Only a title")

    def test_document_is_keyed_by_session_id(self):
        """The document ID and metadata carry the session ID."""
        document = session_to_document(make_session("s1", "Title"))
        self.assertEqual(document.id, "s1")
        self.assertEqual(document.metadata["sessionId"], "s1")


class SessionVectorStoreTests(unittest.TestCase):
    """Building, saving, loading and searching the vector store."""

    def test_build_save_load_and_search(self):
        """An exact-text query finds its own session after a save and load."""
        embeddings = DeterministicFakeEmbedding(size=16)
        catalog = sample_catalog()
        store = SessionVectorStore.build(catalog, embeddings)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "vectors.json"
            store.save(path)
            loaded = SessionVectorStore.load(path, embeddings)

        query = session_to_text(catalog.get("s2"))
        matches = loaded.search(query, limit=3)

        self.assertEqual(len(matches), 3)
        self.assertEqual(matches[0][0], "s2")
        self.assertAlmostEqual(matches[0][1], 1.0, places=5)


if __name__ == "__main__":
    unittest.main()
