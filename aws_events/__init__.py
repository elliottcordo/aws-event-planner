"""Python client for the AWS Events API attendee flow.

Usage:
    from aws_events import Authenticator, EventsClient, HttpTransport, TokenStore

    transport = HttpTransport()
    authenticator = Authenticator(TokenStore(), transport)
    client = EventsClient(transport, authenticator)
    schedule = client.get_schedule("reinvent2026")

Session search:
    from aws_events import (
        FastEmbedEmbeddings, SessionCatalog, SessionSearch, SessionVectorStore,
    )

    catalog = SessionCatalog.download(client, "reinvent2026")
    vector_store = SessionVectorStore.build(catalog, FastEmbedEmbeddings())
    results = SessionSearch(catalog, vector_store).hybrid_search("serverless")
"""

from aws_events.auth import Authenticator, TokenStore
from aws_events.catalog import (
    SessionCatalog,
    SessionFilter,
    chronological_key,
    default_catalog_path,
    session_date,
    session_start_time,
)
from aws_events.client import EventsClient, PersonalTime
from aws_events.errors import (
    ApiError,
    EventsError,
    FeatureDisabledError,
    SignInError,
)
from aws_events.search import SEARCH_MODES, SearchResult, SessionSearch
from aws_events.transport import HttpTransport
from aws_events.vector_store import (
    FastEmbedEmbeddings,
    SessionVectorStore,
    default_index_path,
)

__all__ = [
    "SEARCH_MODES",
    "ApiError",
    "Authenticator",
    "EventsClient",
    "EventsError",
    "FastEmbedEmbeddings",
    "FeatureDisabledError",
    "HttpTransport",
    "PersonalTime",
    "SearchResult",
    "SessionCatalog",
    "SessionFilter",
    "SessionSearch",
    "SessionVectorStore",
    "SignInError",
    "TokenStore",
    "chronological_key",
    "default_catalog_path",
    "default_index_path",
    "session_date",
    "session_start_time",
]
