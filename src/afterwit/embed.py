"""Card embeddings for pull-path retrieval only (ADR-016).

Vectors are computed at index time with fastembed's MiniLM model and stored as
float32 blobs in SQLite. Query embedding has a cold start because it loads the
local ONNX model; first call may take hundreds of milliseconds, while warm
recall should be much faster. The inject hook must not import this module.
"""

from __future__ import annotations

import hashlib
import math
import sqlite3
import threading
from array import array
from collections.abc import Iterable
from typing import Any

MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"

# How long a query may wait for the embedding model before giving up on vectors
# and ranking lexically. Loading it means importing fastembed -> onnxruntime ->
# numpy, i.e. hundreds of MB of native extensions; if site-packages sits on a
# cloud-synced folder (OneDrive Files-On-Demand) every .pyd is a placeholder and
# the *first* load blocks on hydration. Measured at >50s on a corporate laptop,
# and `except Exception` is no defence — a slow import raises nothing, it just
# never returns, so the MCP client sat silent until its own idle timeout.
_LOAD_BUDGET = 5.0

DDL = """
CREATE TABLE IF NOT EXISTS vectors(
  id TEXT PRIMARY KEY,
  body_hash TEXT,
  vec BLOB
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(DDL)


def _text(title: str, body: str) -> str:
    return f"{title.strip()}\n\n{body.strip()}"


def _hash(text: str) -> str:
    """Fingerprint of (model, text) — NOT of the text alone.

    `reindex` skips a card whose stored hash still matches, so a hash over the body
    alone made MODEL_NAME unobservable: changing the model re-embedded NOTHING, and
    the index kept serving old-model vectors against new-model queries. Same-dimension
    swaps (384 -> 384) are the dangerous case; nothing downstream would have noticed.

    Salting with the model name makes the change self-invalidating: every hash differs,
    so every card re-embeds on the next `afterwit index --rebuild`, with no migration
    and no new column. The stored value is still opaque to every other reader.
    """
    return hashlib.sha256(f"{MODEL_NAME}\0{text}".encode()).hexdigest()


def _blob(values: Iterable[float]) -> bytes:
    arr = array("f", (float(v) for v in values))
    return arr.tobytes()


def _vec(blob: bytes) -> array:
    arr = array("f")
    arr.frombytes(blob)
    return arr


def reindex(conn: sqlite3.Connection) -> int:
    """Refresh vectors for every active card; return active vector count.

    fastembed is imported only here so normal module import remains cheap. Tests
    mock `fastembed.TextEmbedding`; the real model download happens during the
    explicit `afterwit index --rebuild` verification run.
    """
    from fastembed import TextEmbedding

    ensure_schema(conn)
    cards = conn.execute(
        "SELECT id, title, body FROM cards WHERE status='active' ORDER BY id"
    ).fetchall()
    active_ids = {r["id"] for r in cards}
    if active_ids:
        placeholders = ",".join("?" for _ in active_ids)
        conn.execute(
            f"DELETE FROM vectors WHERE id NOT IN ({placeholders})",
            tuple(active_ids),
        )
    else:
        conn.execute("DELETE FROM vectors")
        conn.commit()
        return 0

    existing = {
        r["id"]: r["body_hash"]
        for r in conn.execute("SELECT id, body_hash FROM vectors").fetchall()
    }
    pending: list[tuple[str, str, str]] = []
    for r in cards:
        text = _text(r["title"], r["body"])
        body_hash = _hash(text)
        if existing.get(r["id"]) != body_hash:
            pending.append((r["id"], body_hash, text))

    if pending:
        model = TextEmbedding(model_name=MODEL_NAME)
        texts = [p[2] for p in pending]
        for (card_id, body_hash, _), embedding in zip(pending, model.embed(texts)):
            conn.execute(
                "INSERT OR REPLACE INTO vectors(id, body_hash, vec) VALUES(?,?,?)",
                (card_id, body_hash, _blob(embedding)),
            )
    conn.commit()
    return conn.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]


def _vectors_table_exists(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='vectors'"
    ).fetchone() is not None


_model_obj: Any = None
_model_ready = threading.Event()
_model_started = False
_model_lock = threading.Lock()


def _load_model() -> None:
    global _model_obj
    try:
        from fastembed import TextEmbedding

        _model_obj = TextEmbedding(model_name=MODEL_NAME)
    except Exception:  # noqa: BLE001 — embeddings are an optional ranking boost
        _model_obj = None
    finally:
        _model_ready.set()


def model(budget: float = _LOAD_BUDGET) -> Any:
    """The embedding model, or None if it is not ready within `budget` seconds.

    Loaded ONCE, on a daemon thread, and never on the caller's. Two properties
    matter and neither is free:

    - A caller waits `budget`, not the load. A stuck load leaves the thread
      parked forever; the query returns and ranks lexically. `daemon=True` so a
      parked loader can never keep the process from exiting.
    - The load is attempted once per process. It used to be re-run per query —
      a fresh `TextEmbedding` per `recall`, each one paying a model-cache probe
      over the network (~0.8s warm, and on a proxy that 403s, two failed HTTP
      round-trips) before returning the same object.

    A load that fails is remembered as failed: retrying it every query is how a
    broken install turns into a slow one.
    """
    global _model_started
    with _model_lock:
        if not _model_started:
            _model_started = True
            threading.Thread(target=_load_model, name="afterwit-embed-load",
                             daemon=True).start()
    _model_ready.wait(budget)
    return _model_obj


def _cos(a: array, b: array) -> float:
    n = min(len(a), len(b))
    if n == 0:
        return 0.0
    dot = sum(float(a[i]) * float(b[i]) for i in range(n))
    amag = math.sqrt(sum(float(a[i]) * float(a[i]) for i in range(n)))
    bmag = math.sqrt(sum(float(b[i]) * float(b[i]) for i in range(n)))
    if amag == 0.0 or bmag == 0.0:
        return 0.0
    return dot / (amag * bmag)


def cosines(
    conn: sqlite3.Connection,
    query_text: str,
    ids: Iterable[str],
) -> dict[str, float] | None:
    """Cosine scores for candidate ids, or None when vectors/model are unavailable."""
    wanted = list(dict.fromkeys(ids))
    if not wanted or not query_text.strip():
        return {}
    if not _vectors_table_exists(conn):
        return None
    if conn.execute("SELECT 1 FROM vectors LIMIT 1").fetchone() is None:
        return None

    placeholders = ",".join("?" for _ in wanted)
    rows = conn.execute(
        f"SELECT id, vec FROM vectors WHERE id IN ({placeholders})", tuple(wanted)
    ).fetchall()
    if not rows:
        return {}

    m = model()
    if m is None:
        # Pull retrieval remains available lexically when a fresh device has not
        # downloaded the optional local model, the cache is unavailable, or the
        # model is still loading. None means "rank without vectors", never "no
        # results" — the caller must not treat this as an empty index.
        return None
    try:
        query_vec = _vec(_blob(next(iter(m.embed([query_text])))))
    except Exception:  # noqa: BLE001 — embeddings are an optional ranking boost
        return None
    return {r["id"]: _cos(query_vec, _vec(r["vec"])) for r in rows}
