"""Keyword, semantic and hybrid search over an event's sessions."""

import re
from dataclasses import dataclass

from rank_bm25 import BM25Plus

from aws_events.catalog import SessionFilter
from aws_events.vector_store import session_to_text


# Standard constant for reciprocal rank fusion; it damps the gap between the
# top few ranks so one list cannot dominate the merged result.
RRF_CONSTANT = 60

# How many results to take from each search before merging them.
HYBRID_CANDIDATES = 50

SEARCH_MODES = ("hybrid", "semantic", "keyword")


@dataclass
class SearchResult:
    """One matching session and its score (higher is better)."""

    session: dict
    score: float


def tokenize(text):
    """Split text into lowercase words and numbers for keyword search."""
    return re.findall(r"\w+", text.lower())


def reciprocal_rank_fusion(rankings, rrf_constant=RRF_CONSTANT):
    """Merge several ranked lists of IDs into one ranking.

    Each ID scores 1 / (rrf_constant + rank) for every list it appears in, and
    the scores are added up. IDs ranked well in several lists rise to the top.

    Args:
        rankings: A list of ranked ID lists, best first.
        rrf_constant: Damping constant; 60 is the usual choice.

    Returns:
        A list of (id, score) pairs, best first.
    """
    scores = {}
    for ranking in rankings:
        for rank, item_id in enumerate(ranking, start=1):
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (rrf_constant + rank)
    return sorted(scores.items(), key=lambda pair: pair[1], reverse=True)


class SessionSearch:
    """Search a SessionCatalog by keywords, by meaning, or both.

    Every search method takes an optional SessionFilter. The filter is applied
    before results are cut to `limit`, so a filtered search still returns up
    to `limit` matching sessions.

    Usage:
        search = SessionSearch(catalog, vector_store)
        only_mgm = SessionFilter(venue="MGM Grand", date="2026-12-02")
        for result in search.hybrid_search("vector databases", 10, only_mgm):
            print(result.session["title"], result.score)
    """

    def __init__(self, catalog, vector_store):
        """Build the keyword index for the catalog.

        Args:
            catalog: The SessionCatalog to search.
            vector_store: A SessionVectorStore built from the same catalog.
        """
        self.catalog = catalog
        self.vector_store = vector_store
        self._session_ids = [session["sessionId"] for session in catalog.sessions]
        tokenized_sessions = [
            tokenize(session_to_text(session)) for session in catalog.sessions
        ]
        self._session_words = [set(tokens) for tokens in tokenized_sessions]
        # BM25Plus rather than BM25Okapi: Okapi scores a word found in half or
        # more of the sessions as zero or less, which would hide real matches.
        self._bm25 = BM25Plus(tokenized_sessions)

    def search(self, query, limit=10, mode="hybrid", session_filter=None):
        """Run a search in the given mode: "hybrid", "semantic" or "keyword"."""
        if mode == "hybrid":
            return self.hybrid_search(query, limit, session_filter)
        if mode == "semantic":
            return self.semantic_search(query, limit, session_filter)
        if mode == "keyword":
            return self.keyword_search(query, limit, session_filter)
        raise ValueError(f"Unknown search mode {mode!r}; use one of {SEARCH_MODES}")

    def keyword_search(self, query, limit=10, session_filter=None):
        """Return sessions ranked by BM25 keyword score.

        Only sessions containing at least one query word are returned.
        """
        query_words = tokenize(query)
        scores = self._bm25.get_scores(query_words)
        scored_ids = []
        for index, session_id in enumerate(self._session_ids):
            if self._session_words[index].intersection(query_words):
                scored_ids.append((session_id, float(scores[index])))
        scored_ids.sort(key=lambda pair: pair[1], reverse=True)
        return self._to_results(scored_ids, limit, session_filter)

    def semantic_search(self, query, limit=10, session_filter=None):
        """Return sessions ranked by embedding similarity to the query."""
        if session_filter is None or session_filter.is_empty():
            candidate_count = limit
        else:
            # Rank every session so the filter cannot leave fewer than `limit`
            # results when enough matching sessions exist.
            candidate_count = len(self.catalog)
        scored_ids = self.vector_store.search(query, candidate_count)
        return self._to_results(scored_ids, limit, session_filter)

    def hybrid_search(self, query, limit=10, session_filter=None):
        """Return sessions ranked by merging keyword and semantic results.

        Scores are reciprocal rank fusion scores, so they rank results but are
        not comparable with keyword or semantic scores.
        """
        keyword_ids = [
            result.session["sessionId"]
            for result in self.keyword_search(query, HYBRID_CANDIDATES, session_filter)
        ]
        semantic_ids = [
            result.session["sessionId"]
            for result in self.semantic_search(query, HYBRID_CANDIDATES, session_filter)
        ]
        merged = reciprocal_rank_fusion([keyword_ids, semantic_ids])
        return self._to_results(merged, limit)

    def _to_results(self, scored_ids, limit, session_filter=None):
        """Turn the best (session_id, score) pairs into at most `limit` SearchResults.

        Sessions that do not match the filter are skipped, as are IDs missing
        from the catalog (which happens only if the vector store was built
        from an older catalog).
        """
        if session_filter is None:
            session_filter = SessionFilter()
        results = []
        for session_id, score in scored_ids:
            if len(results) == limit:
                break
            session = self.catalog.get(session_id)
            if session is not None and session_filter.matches(session):
                results.append(SearchResult(session, score))
        return results
