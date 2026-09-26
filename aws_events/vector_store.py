"""Embed sessions and keep them in a local vector store for semantic search."""

from pathlib import Path

from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_core.vectorstores import InMemoryVectorStore

from aws_events.catalog import DATA_DIRECTORY


DEFAULT_EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"

# Session fields that hold lists of labels worth searching on.
LABEL_FIELDS = [
    ("topics", "Topics"),
    ("services", "Services"),
    ("areasOfInterest", "Areas of interest"),
    ("roles", "Roles"),
    ("industries", "Industries"),
    ("tracks", "Tracks"),
]


def default_index_path(event_id):
    """Return where the vector store for an event is saved by default."""
    return DATA_DIRECTORY / f"{event_id}-vectors.json"


def session_to_text(session):
    """Return the searchable text for a session: title, code, abstract and labels.

    The same text is embedded for semantic search and tokenized for keyword
    search, so both halves of hybrid search see the same content.
    """
    lines = [
        session.get("title", ""),
        session.get("abbreviation", ""),
        session.get("abstract", ""),
    ]
    for key in ("type", "level"):
        if session.get(key):
            lines.append(session[key])

    for field, label in LABEL_FIELDS:
        values = session.get(field) or []
        if values:
            lines.append(f"{label}: {', '.join(values)}")

    speaker_names = [speaker["name"] for speaker in session.get("speakers") or []]
    if speaker_names:
        lines.append(f"Speakers: {', '.join(speaker_names)}")

    non_empty_lines = [line for line in lines if line]
    return "\n".join(non_empty_lines)


def session_to_document(session):
    """Return a LangChain Document for a session, keyed by its session ID."""
    return Document(
        id=session["sessionId"],
        page_content=session_to_text(session),
        metadata={
            "sessionId": session["sessionId"],
            "title": session.get("title"),
            "abbreviation": session.get("abbreviation"),
        },
    )


class FastEmbedEmbeddings(Embeddings):
    """LangChain Embeddings that run a FastEmbed model locally on the CPU."""

    def __init__(self, model_name=DEFAULT_EMBEDDING_MODEL):
        """Create the embeddings; the model is loaded on first use."""
        self.model_name = model_name
        self._model = None

    def embed_documents(self, texts):
        """Return one embedding (a list of floats) per text."""
        vectors = self._get_model().passage_embed(texts)
        return [vector.tolist() for vector in vectors]

    def embed_query(self, text):
        """Return the embedding for a search query."""
        vectors = self._get_model().query_embed(text)
        return next(iter(vectors)).tolist()

    def _get_model(self):
        """Load the model the first time it is needed."""
        if self._model is None:
            # Imported here because loading fastembed takes a few seconds and
            # most commands never need it.
            # pylint: disable-next=import-outside-toplevel
            from fastembed import TextEmbedding
            self._model = TextEmbedding(model_name=self.model_name)
        return self._model


class SessionVectorStore:
    """Session embeddings held in a LangChain InMemoryVectorStore.

    Usage:
        store = SessionVectorStore.build(catalog, FastEmbedEmbeddings())
        store.save(default_index_path("reinvent2026"))
        store = SessionVectorStore.load(path, FastEmbedEmbeddings())
        matches = store.search("serverless data pipelines", limit=5)
    """

    def __init__(self, vector_store):
        """Wrap an existing LangChain vector store."""
        self.vector_store = vector_store

    @classmethod
    def build(cls, catalog, embeddings):
        """Embed every session in a SessionCatalog and return a new store."""
        vector_store = InMemoryVectorStore(embeddings)
        documents = [session_to_document(session) for session in catalog.sessions]
        session_ids = [document.id for document in documents]
        vector_store.add_documents(documents, ids=session_ids)
        return cls(vector_store)

    @classmethod
    def load(cls, path, embeddings):
        """Read a store written by `save`.

        `embeddings` must be the same model that built the store, since it is
        used to embed search queries.
        """
        return cls(InMemoryVectorStore.load(str(path), embeddings))

    def save(self, path):
        """Write the store to `path` as JSON, creating folders as needed."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.vector_store.dump(str(path))

    def search(self, query, limit=10):
        """Return up to `limit` (session_id, similarity) pairs, best first."""
        matches = self.vector_store.similarity_search_with_score(query, k=limit)
        return [(document.id, score) for document, score in matches]
