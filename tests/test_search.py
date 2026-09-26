"""Tests for aws_events.search."""

import unittest

from aws_events.catalog import SessionCatalog, SessionFilter
from aws_events.search import SessionSearch, reciprocal_rank_fusion, tokenize
from tests.fakes import FakeVectorStore, make_session


def sample_search(semantic_ranking):
    """Return a SessionSearch over four sessions with a fixed semantic ranking."""
    catalog = SessionCatalog("e1", [
        make_session("s1", "Serverless patterns", "Build with Lambda and SQS"),
        make_session("s2", "Vector databases", "Store embeddings for retrieval"),
        make_session("s3", "Cost optimization", "Spend less on compute"),
        make_session("s4", "Lambda performance", "Tune Lambda cold starts"),
    ])
    return SessionSearch(catalog, FakeVectorStore(semantic_ranking))


def result_ids(results):
    """Return the session IDs of search results, in order."""
    return [result.session["sessionId"] for result in results]


class HelperTests(unittest.TestCase):
    """Tokenizing and rank fusion."""

    def test_tokenize_lowercases_and_splits(self):
        """Punctuation is dropped and session codes stay whole."""
        self.assertEqual(tokenize("AIM3315: Gen-AI!"), ["aim3315", "gen", "ai"])

    def test_rank_fusion_rewards_agreement(self):
        """An ID ranked well in both lists beats one ranked first in only one."""
        merged = reciprocal_rank_fusion([["a", "b", "c"], ["b", "c", "a"]])
        self.assertEqual([item_id for item_id, _ in merged], ["b", "a", "c"])

    def test_rank_fusion_score(self):
        """Scores follow 1 / (constant + rank), summed over lists."""
        merged = dict(reciprocal_rank_fusion([["a"], ["a"]], rrf_constant=60))
        self.assertAlmostEqual(merged["a"], 2 / 61)


class SessionSearchTests(unittest.TestCase):
    """Keyword, semantic and hybrid search."""

    def test_keyword_search_ranks_matches_and_skips_others(self):
        """Sessions mentioning the term more rank higher; others are left out."""
        search = sample_search([])
        self.assertEqual(result_ids(search.keyword_search("lambda")), ["s4", "s1"])

    def test_semantic_search_uses_vector_store(self):
        """Semantic results follow the vector store's ranking."""
        search = sample_search(["s2", "s3"])
        self.assertEqual(result_ids(search.semantic_search("anything")), ["s2", "s3"])

    def test_hybrid_merges_both_rankings(self):
        """A session found by both searches comes first."""
        search = sample_search(["s2", "s1", "s3"])

        results = search.hybrid_search("lambda", limit=3)

        self.assertEqual(result_ids(results)[0], "s1")
        self.assertEqual(set(result_ids(results)), {"s1", "s2", "s4"})

    def test_unknown_ids_are_skipped(self):
        """IDs from a stale vector store that are not in the catalog are dropped."""
        search = sample_search(["gone", "s3"])
        self.assertEqual(result_ids(search.semantic_search("x")), ["s3"])

    def test_search_dispatches_by_mode(self):
        """search() picks the method by mode and rejects unknown modes."""
        search = sample_search(["s3"])
        self.assertEqual(result_ids(search.search("x", mode="semantic")), ["s3"])
        with self.assertRaises(ValueError):
            search.search("x", mode="fuzzy")


class FilteredSearchTests(unittest.TestCase):
    """Filters are applied before results are cut to the limit."""

    def setUp(self):
        """Create 12 Lambda sessions: odd numbers at MGM Grand, even at Venetian."""
        sessions = []
        for number in range(1, 13):
            venue = "MGM Grand" if number % 2 else "Venetian"
            sessions.append(make_session(
                f"s{number}", f"Lambda talk {number}", "Lambda",
                venue=venue, sessionTime={"date": "2026-12-02", "time": "09:00"},
            ))
        self.ranked_ids = [f"s{number}" for number in range(1, 13)]
        self.vector_store = FakeVectorStore(self.ranked_ids)
        catalog = SessionCatalog("e1", sessions)
        self.search = SessionSearch(catalog, self.vector_store)
        self.mgm_only = SessionFilter(venue="MGM Grand")

    def test_keyword_filter_fills_limit_with_matches(self):
        """Keyword search returns `limit` matching sessions, all at the venue."""
        results = self.search.keyword_search("lambda", 3, self.mgm_only)
        self.assertEqual(len(results), 3)
        for result in results:
            self.assertEqual(result.session["venue"], "MGM Grand")

    def test_semantic_filter_ranks_whole_catalog(self):
        """With a filter, semantic search looks past the first `limit` results."""
        results = self.search.semantic_search("x", 3, self.mgm_only)
        self.assertEqual(result_ids(results), ["s1", "s3", "s5"])

    def test_hybrid_filter(self):
        """Hybrid results all meet the filter."""
        results = self.search.hybrid_search("lambda", 10, self.mgm_only)
        self.assertEqual(len(results), 6)
        self.assertEqual({r.session["venue"] for r in results}, {"MGM Grand"})

    def test_filter_with_no_matches_returns_nothing(self):
        """A filter nothing matches gives an empty list, not an error."""
        no_match = SessionFilter(date="2030-01-01")
        self.assertEqual(self.search.search("lambda", session_filter=no_match), [])


if __name__ == "__main__":
    unittest.main()
