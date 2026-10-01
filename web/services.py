"""Everything the web app works with: API client, search indexes, jobs and skins.

Kept apart from the HTTP routes so the logic can be tested without a server.
"""

import threading
import webbrowser

from aws_events import (
    ApiError,
    Authenticator,
    EventsClient,
    FastEmbedEmbeddings,
    FeatureDisabledError,
    HttpTransport,
    SessionCatalog,
    SessionSearch,
    SessionVectorStore,
    TokenStore,
    default_catalog_path,
    default_index_path,
)
from aws_events.client import MAX_SESSIONS_PER_REQUEST
from web.jobs import JobBoard
from web.skins import SkinRegistry


SIGN_IN_JOB = "sign-in"


def rebuild_job_name(event_id):
    """Return the job name used for rebuilding an event's data."""
    return f"rebuild:{event_id}"


class Services:
    """Shared state for the web app, created once at startup.

    Usage:
        services = Services.create_default()
        search = services.search_for("reinvent2026")
    """

    def __init__(self, client, skins, embeddings_factory=FastEmbedEmbeddings,
                 catalog_path=default_catalog_path, index_path=default_index_path,
                 open_browser=webbrowser.open):
        """Create the services.

        Args:
            client: An EventsClient whose authenticator never signs in by
                itself (sign_in_when_needed=False).
            skins: A SkinRegistry.
            embeddings_factory: Makes the embedding model; called once, lazily.
            catalog_path: Function from event ID to the saved catalog path.
            index_path: Function from event ID to the saved index path.
            open_browser: Opens the sign-in page in a browser tab.
        """
        self.client = client
        self.authenticator = client.authenticator
        self.skins = skins
        self.jobs = JobBoard()
        self.embeddings_factory = embeddings_factory
        self.catalog_path = catalog_path
        self.index_path = index_path
        self.open_browser = open_browser
        self._embeddings = None
        self._searches = {}
        self._lock = threading.Lock()

    @classmethod
    def create_default(cls):
        """Return services wired to the real API, token file and skin list."""
        transport = HttpTransport()
        authenticator = Authenticator(
            TokenStore(), transport, sign_in_when_needed=False
        )
        return cls(EventsClient(transport, authenticator), SkinRegistry())

    def embeddings(self):
        """Return the embedding model, loading it on first use."""
        with self._lock:
            if self._embeddings is None:
                self._embeddings = self.embeddings_factory()
            return self._embeddings

    def search_for(self, event_id):
        """Return the SessionSearch for an event, or None if its data isn't saved.

        Loaded searches are kept in memory until the event is rebuilt.
        """
        with self._lock:
            if event_id in self._searches:
                return self._searches[event_id]
        try:
            catalog = SessionCatalog.load(self.catalog_path(event_id))
            vector_store = SessionVectorStore.load(
                self.index_path(event_id), self.embeddings()
            )
        except FileNotFoundError:
            return None
        search = SessionSearch(catalog, vector_store)
        with self._lock:
            self._searches[event_id] = search
        return search

    def remove_favorites(self, event_id, session_ids):
        """Remove several sessions from favorites, one API call each.

        Returns:
            A dict with "successful" and "failed" lists, in the same shape the
            API's bulk calls return, so the page can report it the same way.
        """
        successful = []
        failed = []
        for session_id in session_ids:
            try:
                self.client.remove_favorite(event_id, session_id)
            except FeatureDisabledError:
                # Switched off for every session, so stop and say so.
                raise
            except ApiError as error:
                # The API answers 404 for a session that isn't a favorite.
                code = "notFavorited" if error.status_code == 404 else "other"
                failed.append({"sessionId": session_id, "code": code})
            else:
                successful.append(session_id)
        return {"successful": successful, "failed": failed}

    def reserve_sessions_in_batches(self, event_id, session_ids):
        """Reserve any number of sessions, at most 10 per API call.

        Returns:
            A dict with "successful" and "failed" lists for all the calls.
        """
        successful = []
        failed = []
        for start in range(0, len(session_ids), MAX_SESSIONS_PER_REQUEST):
            batch = session_ids[start:start + MAX_SESSIONS_PER_REQUEST]
            result = self.client.reserve_sessions(event_id, batch)
            successful.extend(result["successful"])
            failed.extend(result["failed"])
        return {"successful": successful, "failed": failed}

    def is_signed_in(self):
        """Return True if a sign-in is saved (it may still turn out to be expired)."""
        return self.authenticator.has_saved_sign_in()

    def start_sign_in(self):
        """Start the browser sign-in in the background and return its job.

        The sign-in link is put in `job.details["url"]` so the page can show
        it, in case no browser tab opened.
        """
        def work(job):
            def show_and_open(url):
                job.details["url"] = url
                job.report("Waiting for you to finish signing in...")
                self.open_browser(url)

            self.authenticator.sign_in(open_browser=show_and_open)
            return "Signed in."

        return self.jobs.start(SIGN_IN_JOB, work)

    def start_rebuild(self, event_id):
        """Start downloading and indexing an event's sessions; return the job."""
        def work(job):
            job.report("Downloading sessions...")
            catalog = SessionCatalog.download(self.client, event_id)
            catalog.save(self.catalog_path(event_id))
            job.report(f"Building the search index for {len(catalog)} sessions "
                       "(this can take a few minutes)...")
            vector_store = SessionVectorStore.build(catalog, self.embeddings())
            vector_store.save(self.index_path(event_id))
            with self._lock:
                self._searches.pop(event_id, None)
            return f"Indexed {len(catalog)} sessions."

        return self.jobs.start(rebuild_job_name(event_id), work)
