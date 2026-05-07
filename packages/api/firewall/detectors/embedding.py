"""Embedding similarity matcher — Slice 3 PR I / spec §6.6.

Lets operators build a small corpus of "known bad" text (jailbreak
prompts they want to block, internal docs they want to prevent
exfil of, customer-list strings they don't want leaking) and match
incoming agent traffic against it via cosine similarity in an
embedding space.

This is the third detector slot (after regex_pack, presidio,
prompt_guard, llama_guard) and the only one that's *operator-
authored*: the corpus is data, not code. That makes it the
right tool for org-specific signals the generic detectors can't
cover — internal codenames, customer-specific phrasings,
brand-mention requirements, etc.

Architecture:

  - **Corpora** are stored in DuckDB as ``firewall_corpora`` (id,
    name, description, created_at) with members in
    ``firewall_corpus_entries`` (corpus_id, text, embedding BLOB,
    created_at).
  - **Embeddings** are produced by ``sentence-transformers``
    (default model: ``all-MiniLM-L6-v2`` — 22MB, 384-dim, CPU
    inference <10ms). Optional dep — when missing, the detector
    is a silent no-op (Rule 7).
  - **Query path** at policy-eval time: embed incoming text →
    cosine-similarity vs all corpus entries → return max score.
  - **Index**: linear scan today (corpora are typically <500
    entries; the 384-dim dot-product is microseconds). FAISS lands
    when an operator builds a corpus large enough to need it.

Why no FAISS yet despite the spec mentioning it: the in-memory
linear scan for <500 entries is faster than FAISS index lookup
overhead, and FAISS adds a 30MB transitive (faiss-cpu). We add
it the first time an operator builds a corpus that warrants it.

Optional dep — install with::

    pip install sentence-transformers

When the dep isn't installed:
  - ``available = False`` at module load
  - ``similar_to_corpus(text, name, threshold)`` returns False
  - Any rule referencing it is a silent no-op (Rule 7)

Configuration:

  LUMIN_EMBEDDING_MODEL — sentence-transformers model id.
                          Defaults to ``all-MiniLM-L6-v2``.
  LUMIN_EMBEDDING_DEVICE — ``cpu`` (default) or ``cuda``.
"""

from __future__ import annotations

import logging
import math
import os
import struct
import threading
from typing import Any, List, Optional, Tuple

logger = logging.getLogger("lumin.api.firewall.detectors.embedding")


try:
    import importlib.util as _ispec
    available = _ispec.find_spec("sentence_transformers") is not None
except Exception:
    available = False


_MODEL_NAME = os.environ.get("LUMIN_EMBEDDING_MODEL", "all-MiniLM-L6-v2")
_DEVICE = os.environ.get("LUMIN_EMBEDDING_DEVICE", "cpu")

_model = None
_model_lock = threading.Lock()
_load_failed = False

# In-process corpus cache: {corpus_name: [(text, vec)]}
# Built lazily on first query; invalidated when an operator
# adds / removes entries (the CRUD endpoints call invalidate()).
_CORPUS_CACHE: dict[str, List[Tuple[str, List[float]]]] = {}
_corpus_lock = threading.Lock()


# ---- model loading --------------------------------------------------------


def _ensure_loaded() -> bool:
    """Lazy-load the sentence-transformers model. Idempotent."""
    global _model, _load_failed
    if _model is not None:
        return True
    if _load_failed:
        return False
    if not available:
        return False
    with _model_lock:
        if _model is not None:
            return True
        if _load_failed:
            return False
        try:
            from sentence_transformers import (  # type: ignore[import-not-found]
                SentenceTransformer,
            )
            _model = SentenceTransformer(_MODEL_NAME, device=_DEVICE)
            logger.info(
                "embedding: loaded %s on %s", _MODEL_NAME, _DEVICE
            )
            return True
        except Exception:
            logger.exception(
                "embedding: failed to load %s — detector disabled. "
                "Install via: pip install sentence-transformers",
                _MODEL_NAME,
            )
            _load_failed = True
            return False


def _embed(text: str) -> Optional[List[float]]:
    """Return a list[float] embedding for ``text``, or None on
    failure. Truncated to 8000 chars defensively."""
    if not text or not isinstance(text, str):
        return None
    if not _ensure_loaded():
        return None
    try:
        snippet = text[:8000]
        # encode() returns numpy array; we convert to plain list so
        # the BLOB serializer is numpy-free.
        vec = _model.encode(snippet, convert_to_numpy=True)  # type: ignore[union-attr]
        return [float(x) for x in vec.tolist()]
    except Exception:
        logger.exception("embedding: encode failed")
        return None


# ---- BLOB (de)serialization ----------------------------------------------
#
# DuckDB stores binary as BLOB — we pack floats with struct rather
# than json so a 384-dim vector takes 1.5KB instead of ~3KB and
# round-trips bit-exact. Format: ``<I`` (uint32 dim) + ``<f`` * dim.


def _pack_vec(vec: List[float]) -> bytes:
    n = len(vec)
    return struct.pack(f"<I{n}f", n, *vec)


def _unpack_vec(blob: bytes) -> List[float]:
    if not blob or len(blob) < 4:
        return []
    n = struct.unpack_from("<I", blob, 0)[0]
    if len(blob) < 4 + 4 * n:
        return []
    return list(struct.unpack_from(f"<{n}f", blob, 4))


# ---- DuckDB schema --------------------------------------------------------


_SCHEMA_CREATED = False
_schema_lock = threading.Lock()


def _ensure_schema(db) -> None:
    """Create the corpus tables on first use. Idempotent."""
    global _SCHEMA_CREATED
    if _SCHEMA_CREATED:
        return
    with _schema_lock:
        if _SCHEMA_CREATED:
            return
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS firewall_corpora (
                id INTEGER PRIMARY KEY,
                name TEXT UNIQUE NOT NULL,
                description TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        db.execute(
            """
            CREATE SEQUENCE IF NOT EXISTS firewall_corpora_seq START 1
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS firewall_corpus_entries (
                id INTEGER PRIMARY KEY,
                corpus_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                embedding BLOB NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        db.execute(
            """
            CREATE SEQUENCE IF NOT EXISTS firewall_corpus_entries_seq START 1
            """
        )
        _SCHEMA_CREATED = True


# ---- corpus CRUD ----------------------------------------------------------


def create_corpus(db, name: str, description: Optional[str] = None) -> int:
    """Create a new (empty) corpus. Returns its id. Raises
    ValueError if a corpus with this name already exists."""
    _ensure_schema(db)
    name = (name or "").strip()
    if not name:
        raise ValueError("corpus name cannot be empty")
    existing = db.fetchone(
        "SELECT id FROM firewall_corpora WHERE name = ?", [name]
    )
    if existing:
        raise ValueError(f"corpus '{name}' already exists")
    new_id = db.fetchone(
        "SELECT nextval('firewall_corpora_seq')"
    )[0]
    db.execute(
        "INSERT INTO firewall_corpora (id, name, description) VALUES (?, ?, ?)",
        [new_id, name, description],
    )
    return int(new_id)


def list_corpora(db) -> List[dict]:
    _ensure_schema(db)
    rows = db.fetchall_dict(
        """
        SELECT c.id, c.name, c.description, c.created_at,
               COUNT(e.id) AS entry_count
        FROM firewall_corpora c
        LEFT JOIN firewall_corpus_entries e ON e.corpus_id = c.id
        GROUP BY c.id, c.name, c.description, c.created_at
        ORDER BY c.name
        """
    )
    return rows


def add_entry(db, corpus_name: str, text: str) -> Optional[int]:
    """Embed ``text`` and add it to ``corpus_name``. Returns the
    new entry id, or None when the embedding model isn't available."""
    _ensure_schema(db)
    corpus = db.fetchone(
        "SELECT id FROM firewall_corpora WHERE name = ?", [corpus_name]
    )
    if not corpus:
        raise ValueError(f"corpus '{corpus_name}' not found")
    vec = _embed(text)
    if vec is None:
        return None
    new_id = db.fetchone(
        "SELECT nextval('firewall_corpus_entries_seq')"
    )[0]
    db.execute(
        "INSERT INTO firewall_corpus_entries (id, corpus_id, text, embedding) "
        "VALUES (?, ?, ?, ?)",
        [new_id, corpus[0], text, _pack_vec(vec)],
    )
    invalidate_cache(corpus_name)
    return int(new_id)


def list_entries(db, corpus_name: str) -> List[dict]:
    _ensure_schema(db)
    return db.fetchall_dict(
        """
        SELECT e.id, e.text, e.created_at
        FROM firewall_corpus_entries e
        JOIN firewall_corpora c ON c.id = e.corpus_id
        WHERE c.name = ?
        ORDER BY e.created_at DESC
        """,
        [corpus_name],
    )


def delete_entry(db, entry_id: int) -> bool:
    """Remove an entry. Returns True if it existed and was deleted."""
    _ensure_schema(db)
    row = db.fetchone(
        "SELECT c.name FROM firewall_corpus_entries e "
        "JOIN firewall_corpora c ON c.id = e.corpus_id "
        "WHERE e.id = ?",
        [entry_id],
    )
    if not row:
        return False
    db.execute(
        "DELETE FROM firewall_corpus_entries WHERE id = ?", [entry_id]
    )
    invalidate_cache(row[0])
    return True


def delete_corpus(db, name: str) -> bool:
    _ensure_schema(db)
    row = db.fetchone(
        "SELECT id FROM firewall_corpora WHERE name = ?", [name]
    )
    if not row:
        return False
    db.execute(
        "DELETE FROM firewall_corpus_entries WHERE corpus_id = ?", [row[0]]
    )
    db.execute("DELETE FROM firewall_corpora WHERE id = ?", [row[0]])
    invalidate_cache(name)
    return True


def invalidate_cache(corpus_name: str) -> None:
    """Drop the cached corpus vectors so the next query reloads
    from DB. Called on every CRUD that touches the corpus."""
    with _corpus_lock:
        _CORPUS_CACHE.pop(corpus_name, None)


def _load_corpus_cached(db, corpus_name: str) -> List[Tuple[str, List[float]]]:
    """Load all (text, vec) entries for a corpus, cached in-process."""
    with _corpus_lock:
        cached = _CORPUS_CACHE.get(corpus_name)
        if cached is not None:
            return cached
    _ensure_schema(db)
    rows = db.fetchall(
        """
        SELECT e.text, e.embedding
        FROM firewall_corpus_entries e
        JOIN firewall_corpora c ON c.id = e.corpus_id
        WHERE c.name = ?
        """,
        [corpus_name],
    )
    entries = [(text, _unpack_vec(blob)) for text, blob in rows]
    with _corpus_lock:
        _CORPUS_CACHE[corpus_name] = entries
    return entries


# ---- similarity computation ----------------------------------------------


def _cosine(a: List[float], b: List[float]) -> float:
    """Cosine similarity for two equal-length vectors. Returns 0.0
    on empty / mismatched inputs (Rule 7)."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))


def max_similarity(
    db, text: Optional[str], corpus_name: Optional[str]
) -> float:
    """Return the maximum cosine similarity between ``text`` and any
    entry in ``corpus_name``. Returns 0.0 on:

      - falsy inputs
      - missing corpus
      - empty corpus
      - embedding model unavailable
      - DB / encode failure

    (Rule 7 — never let the detector crash the policy engine.)
    """
    if not text or not isinstance(text, str):
        return 0.0
    if not corpus_name or not isinstance(corpus_name, str):
        return 0.0
    if not _ensure_loaded():
        return 0.0
    try:
        entries = _load_corpus_cached(db, corpus_name)
        if not entries:
            return 0.0
        query_vec = _embed(text)
        if query_vec is None:
            return 0.0
        best = 0.0
        for _, entry_vec in entries:
            score = _cosine(query_vec, entry_vec)
            if score > best:
                best = score
        return best
    except Exception:
        logger.exception("embedding: max_similarity failed")
        return 0.0


def similar_to_corpus(
    db, text: Optional[str], corpus_name: Optional[str],
    threshold: float = 0.85,
) -> bool:
    """True iff ``text`` has cosine-similarity >= ``threshold`` with
    any entry in ``corpus_name``. ``threshold`` defaults to 0.85
    which is a reasonable "very similar" cutoff for MiniLM. Operators
    tune via the rule editor."""
    return max_similarity(db, text, corpus_name) >= threshold


def reset_for_tests() -> None:
    """Test helper — clear the model + corpus cache + schema flag."""
    global _model, _load_failed, _SCHEMA_CREATED
    _model = None
    _load_failed = False
    _SCHEMA_CREATED = False
    with _corpus_lock:
        _CORPUS_CACHE.clear()
