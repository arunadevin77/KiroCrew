"""Knowledge search speed fixes keep their answers.

Three changes cut a knowledge search on a 22k-item, 59k-entity library from
~0.9 s to ~0.2 s: the alias fallback in ``find_entity`` skips rows that have no
aliases, two indexes serve the graph leg, and the vector leg reuses one cached
matrix until the database files change. These tests pin that each change
returns what the code returned before it.
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from kiro_crew.knowledge import retrieval
from kiro_crew.knowledge.embedder import floats_to_bytes
from kiro_crew.knowledge.retrieval import ANY_EMBEDDING_SPACE, HybridRetriever
from kiro_crew.knowledge.store import KnowledgeStore

pytest.importorskip("numpy")

_DIM = 8


@pytest.fixture()
def store(tmp_path: Path):
    retrieval.clear_vector_matrix_cache()
    s = KnowledgeStore(str(tmp_path / "kb.db"))
    yield s
    s.close()
    retrieval.clear_vector_matrix_cache()


def _vec(rng: random.Random) -> list[float]:
    return [rng.gauss(0.0, 1.0) for _ in range(_DIM)]


def _retriever(store: KnowledgeStore, query_vec: list[float]) -> HybridRetriever:
    return HybridRetriever(store, embedder=lambda _q: query_vec, embed_sig=ANY_EMBEDDING_SPACE)


def _reference(store: KnowledgeStore, query_vec: list[float], monkeypatch) -> list:
    """The vector leg's answer with the cache out of the picture."""
    with monkeypatch.context() as m:
        m.setattr(retrieval, "_cached_matrix", lambda *a, **k: None)
        return _retriever(store, query_vec)._vector_search("q", limit=20)


class TestFindEntity:
    def test_alias_still_resolves_beside_alias_less_rows(self, store):
        for i in range(5):
            store.add_entity(f"plain {i}", "concept")
        eid = store.add_entity("Kubernetes", "tool", aliases=["k8s"])
        assert store.find_entity("K8S")["id"] == eid

    def test_name_match_is_case_insensitive(self, store):
        eid = store.add_entity("FastAPI", "tool")
        assert store.find_entity("fastapi")["id"] == eid

    def test_miss_returns_none(self, store):
        store.add_entity("FastAPI", "tool")
        assert store.find_entity("django") is None

    def test_graph_indexes_exist(self, store):
        names = {
            r[0] for r in store.db.execute("SELECT name FROM sqlite_schema WHERE type = 'index'")
        }
        assert {"idx_entities_name_lower", "idx_mentions_entity_item"} <= names


def _index_names(store) -> set[str]:
    return {r[0] for r in store.db.execute("SELECT name FROM sqlite_schema WHERE type = 'index'")}


class TestSearchIndexesOffConstruction:
    """A populated library must not build the O(data) indexes while opening.

    The gateway opens the store before its listener binds; on a cold 59k-entity
    library these two indexes took ~11 s, which landed on time-to-ready.
    """

    @staticmethod
    def _populated_without_indexes(tmp_path: Path) -> str:
        path = str(tmp_path / "kb.db")
        s = KnowledgeStore(path)
        eid = s.add_entity("widget", "concept")
        s.add_mention(s.add_item("doc", "c", "doc"), eid)
        for name in ("idx_entities_name_lower", "idx_mentions_entity_item"):
            s.db.execute(f"DROP INDEX {name}")
        s.db.commit()
        return path

    def test_reopen_of_populated_store_defers_them(self, tmp_path):
        reopened = KnowledgeStore(self._populated_without_indexes(tmp_path))
        assert not {"idx_entities_name_lower", "idx_mentions_entity_item"} & _index_names(reopened)

    def test_entities_without_mentions_still_defer_them(self, tmp_path):
        path = self._populated_without_indexes(tmp_path)
        s = KnowledgeStore(path)
        s.db.execute("DELETE FROM mentions")
        s.db.commit()
        s.close()
        reopened = KnowledgeStore(path)
        assert not {"idx_entities_name_lower", "idx_mentions_entity_item"} & _index_names(reopened)

    def test_ensure_search_indexes_builds_them_and_is_idempotent(self, tmp_path):
        reopened = KnowledgeStore(self._populated_without_indexes(tmp_path))
        reopened.ensure_search_indexes()
        reopened.ensure_search_indexes()
        assert {"idx_entities_name_lower", "idx_mentions_entity_item"} <= _index_names(reopened)

    def test_search_results_do_not_depend_on_them(self, tmp_path):
        reopened = KnowledgeStore(self._populated_without_indexes(tmp_path))
        before = (reopened.find_entity("WIDGET"), HybridRetriever(reopened)._graph_search("widget"))
        reopened.ensure_search_indexes()
        after = (reopened.find_entity("WIDGET"), HybridRetriever(reopened)._graph_search("widget"))
        assert before == after


class TestGraphTieOrder:
    def test_equal_counts_rank_by_item_id(self, store):
        eid = store.add_entity("widget", "concept")
        items = [store.add_item(f"doc {i}", "c", "doc") for i in range(4)]
        for item in items:
            store.add_mention(item, eid)
        ranked = HybridRetriever(store)._graph_search("widget")
        assert [i for i, _ in ranked] == sorted(items)


class TestVectorMatrixCache:
    def test_cached_ranking_matches_reference_scorer(self, store):
        rng = random.Random(7)
        for i in range(40):
            store.add_item(f"doc {i}", "c", "doc", embedding=floats_to_bytes(_vec(rng)))
        q = _vec(rng)
        r = _retriever(store, q)
        first = r._vector_search("q", limit=40)
        assert len(retrieval._matrix_cache) == 1
        second = r._vector_search("q", limit=40)
        rows = store.db.execute(
            "SELECT id, embedding FROM items WHERE embedding IS NOT NULL AND status = 'active'"
        ).fetchall()
        scored, _ = HybridRetriever._score_rows_python(rows, q)
        scored.sort(key=lambda x: x[1], reverse=True)
        reference = [(i, n + 1) for n, (i, _) in enumerate(scored[:40])]
        assert first == second == reference

    def test_write_invalidates_the_cache(self, store):
        rng = random.Random(3)
        for i in range(10):
            store.add_item(f"doc {i}", "c", "doc", embedding=floats_to_bytes(_vec(rng)))
        q = _vec(rng)
        r = _retriever(store, q)
        r._vector_search("q")
        new_id = store.add_item("exact", "c", "doc", embedding=floats_to_bytes(q))
        assert r._vector_search("q")[0][0] == new_id

    def test_a_wal_reset_changes_the_fingerprint_beyond_the_stat(self, store):
        # After a checkpoint, the next write rewrites the WAL from its start, so
        # its size can repeat and a coarse mtime can too; the header salts cannot.
        rng = random.Random(4)
        store.add_item("a", "c", "doc", embedding=floats_to_bytes(_vec(rng)))
        before = retrieval._db_fingerprint(store._db_path)
        assert before is not None and before[2] is not None
        store.db.execute("PRAGMA wal_checkpoint(RESTART)").fetchall()
        store.add_item("b", "c", "doc", embedding=floats_to_bytes(_vec(rng)))
        after = retrieval._db_fingerprint(store._db_path)
        assert after is not None and after[2] is not None
        assert after[2] != before[2]

    def test_ids_count_against_the_cap(self, store, monkeypatch):
        rng = random.Random(8)
        for i in range(10):
            store.add_item(f"doc {i}", "c", "doc", embedding=floats_to_bytes(_vec(rng)))
        matrix_and_norms = 10 * (_DIM * 4 + 8)
        # Room for the matrix and norms, not for the ids beside them.
        monkeypatch.setattr(retrieval, "_MATRIX_CACHE_MAX_BYTES", matrix_and_norms + 100)
        q = _vec(rng)
        expected = _reference(store, q, monkeypatch)
        rows = _retriever(store, q)._vector_search("q", limit=10)
        assert rows == expected  # answered by the reference path
        (entry,) = retrieval._matrix_cache.values()
        assert entry[1] is None  # remembered as too big, nothing held

    def test_an_over_cap_result_is_not_rebuilt_until_the_db_changes(self, store, monkeypatch):
        rng = random.Random(9)
        for i in range(5):
            store.add_item(f"doc {i}", "c", "doc", embedding=floats_to_bytes(_vec(rng)))
        monkeypatch.setattr(retrieval, "_MATRIX_CACHE_MAX_BYTES", 1)
        builds: list[int] = []
        real_build = retrieval._build_matrix

        def counting_build(*a, **k):
            builds.append(1)
            return real_build(*a, **k)

        monkeypatch.setattr(retrieval, "_build_matrix", counting_build)
        q = _vec(rng)
        r = _retriever(store, q)
        expected = _reference(store, q, monkeypatch)
        for _ in range(3):
            assert r._vector_search("q") == expected
        assert len(builds) == 1
        store.add_item("new", "c", "doc", embedding=floats_to_bytes(q))
        assert r._vector_search("q") == _reference(store, q, monkeypatch)
        assert len(builds) == 2

    def test_entries_share_one_byte_budget(self, store, monkeypatch):
        rng = random.Random(10)
        for i in range(10):
            store.add_item(f"doc {i}", "c", "doc", embedding=floats_to_bytes(_vec(rng)))
        sql = "SELECT id, embedding FROM items WHERE embedding IS NOT NULL"
        assert retrieval._cached_matrix(store, sql, [], _DIM) is not None
        (first,) = retrieval._matrix_cache.values()
        # Room for one entry of this size, not two.
        monkeypatch.setattr(retrieval, "_MATRIX_CACHE_MAX_BYTES", first[4] * 3 // 2)
        assert retrieval._cached_matrix(store, sql + " AND 1 = 1", [], _DIM) is not None
        assert len(retrieval._matrix_cache) == 1
        assert sum(e[4] for e in retrieval._matrix_cache.values()) <= first[4] * 3 // 2

    def test_mismatched_width_uses_reference_path(self, store, monkeypatch):
        rng = random.Random(5)
        q = _vec(rng)
        store.add_item("ok", "c", "doc", embedding=floats_to_bytes(q))
        store.add_item("short", "c", "doc", embedding=floats_to_bytes([1.0, 2.0, 3.0, 4.0]))
        builds: list[int] = []
        real_build = retrieval._build_matrix

        def counting_build(*a, **k):
            builds.append(1)
            return real_build(*a, **k)

        monkeypatch.setattr(retrieval, "_build_matrix", counting_build)
        r = _retriever(store, q)
        for _ in range(3):
            assert len(r._vector_search("q")) == 1
        # Read once by the build, then remembered as uncacheable: no matrix held.
        assert len(builds) == 1
        assert [e[1] for e in retrieval._matrix_cache.values()] == [None]

    def test_in_memory_store_is_not_cached(self):
        retrieval.clear_vector_matrix_cache()
        s = KnowledgeStore(":memory:")
        try:
            rng = random.Random(1)
            s.add_item("a", "c", "doc", embedding=floats_to_bytes(_vec(rng)))
            assert _retriever(s, _vec(rng))._vector_search("q")
            assert retrieval._matrix_cache == {}
        finally:
            s.close()
