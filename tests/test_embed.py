import argparse
import builtins
import sys
import threading
import time
import types
from array import array

from afterwit import cards, cli, config, embed, index_db, mcp_server, rank
from tests.test_cards import make_card
from tests.test_inject import payload, setup_env


class FakeTextEmbedding:
    calls: list[list[str]] = []

    def __init__(self, *, model_name: str):
        self.model_name = model_name

    def embed(self, texts):
        batch = list(texts)
        self.calls.append(batch)
        for text in batch:
            yield [float(len(text)), 1.0, 0.5]


def install_fake_fastembed(monkeypatch):
    FakeTextEmbedding.calls = []
    mod = types.ModuleType("fastembed")
    mod.TextEmbedding = FakeTextEmbedding
    monkeypatch.setitem(sys.modules, "fastembed", mod)


def build_db(tmp_path, cardlist):
    wiki = tmp_path / "wiki"
    for c in cardlist:
        cards.save(c, wiki)
    conn = index_db.connect(tmp_path / "index.db")
    index_db.rebuild(conn, wiki)
    return conn


def vector_count(conn):
    return conn.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]


def test_reindex_populates_skips_updates_and_deletes(tmp_path, monkeypatch):
    install_fake_fastembed(monkeypatch)
    keep = make_card(title="Smartsheet truncation", body="Chunk writes before API limits.")
    gone = make_card(title="Prisma P1001", body="Use 127.0.0.1.")
    conn = build_db(tmp_path, [keep, gone])

    assert embed.reindex(conn) == 2
    assert vector_count(conn) == 2
    assert len(FakeTextEmbedding.calls) == 1
    assert len(FakeTextEmbedding.calls[0]) == 2

    FakeTextEmbedding.calls = []
    assert embed.reindex(conn) == 2
    assert FakeTextEmbedding.calls == []

    before = conn.execute(
        "SELECT body_hash FROM vectors WHERE id=?", (keep.id,)
    ).fetchone()["body_hash"]
    conn.execute("UPDATE cards SET body=? WHERE id=?", ("Chunk writes and retry.", keep.id))
    assert embed.reindex(conn) == 2
    after = conn.execute(
        "SELECT body_hash FROM vectors WHERE id=?", (keep.id,)
    ).fetchone()["body_hash"]
    assert before != after
    assert FakeTextEmbedding.calls == [["Smartsheet truncation\n\nChunk writes and retry."]]

    conn.execute("UPDATE cards SET status='superseded' WHERE id=?", (gone.id,))
    assert embed.reindex(conn) == 1
    assert conn.execute("SELECT id FROM vectors").fetchall()[0]["id"] == keep.id


def test_cosines_returns_none_when_fastembed_unavailable(tmp_path, monkeypatch):
    conn = build_db(tmp_path, [make_card()])
    embed.ensure_schema(conn)
    arr = array("f", [1.0, 0.0, 0.0])
    conn.execute(
        "INSERT INTO vectors(id, body_hash, vec) VALUES(?,?,?)",
        ("c1", "h1", arr.tobytes()),
    )
    conn.commit()
    sys.modules.pop("fastembed", None)
    real_import = builtins.__import__

    def blocked_import(name, *args, **kwargs):
        if name == "fastembed":
            raise ImportError("blocked")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)
    assert embed.cosines(conn, "query", ["c1"]) is None


def test_inject_run_never_imports_fastembed(tmp_path, monkeypatch):
    for name in list(sys.modules):
        if name == "fastembed" or name.startswith("fastembed."):
            sys.modules.pop(name)
    # sys.modules is process-global: an earlier test (consolidate.mark_stale)
    # already imported gitmeta. Drop it so the assertion below measures what
    # *inject* imports, not what the suite happened to load first.
    sys.modules.pop("afterwit.gitmeta", None)
    root = setup_env(tmp_path, monkeypatch, [
        make_card(title="Prisma P1001 fix", body="Use 127.0.0.1.",
                  type="error_fix", verified=True),  # push_types: decision is not pushed
    ])

    out = __import__("afterwit.inject").inject.run(
        ["--mode", "prompt"],
        payload("prisma P1001 cannot reach database", root / "acme_hr"),
    )

    assert "P1001" in out
    assert not any(
        name == "fastembed" or name.startswith("fastembed.")
        for name in sys.modules
    )
    # ADR-018: staleness is precomputed into cards.stale at lint time. The hook
    # must read the flag, never shell out to git — that is subprocess latency on
    # a p95 < 200ms path.
    assert "afterwit.gitmeta" not in sys.modules


def test_cli_recall_passes_cosines_to_rank(tmp_path, monkeypatch):
    setup_env(tmp_path, monkeypatch, [
        make_card(title="Smartsheet truncation", body="smartsheet truncation", verified=True),
    ])
    seen = {}

    monkeypatch.setattr("afterwit.embed.cosines", lambda conn, query, ids: {"C": 0.8})

    def spy(rows, project, **kwargs):
        seen["cosines"] = kwargs["cosines"]
        return []

    monkeypatch.setattr(rank, "rank", spy)
    args = argparse.Namespace(query="smartsheet truncation", project=None, all=False, k=5)

    assert cli._cmd_recall(args) == 0
    assert seen["cosines"] == {"C": 0.8}


def test_mcp_pull_paths_pass_cosines_to_rank(tmp_path, monkeypatch):
    setup_env(tmp_path, monkeypatch, [
        make_card(title="Audit decision", type="decision", body="audit storage", verified=True),
    ])
    calls = []

    monkeypatch.setattr(mcp_server.embed, "cosines", lambda conn, query, ids: {"D": 0.7})

    def spy(rows, project, **kwargs):
        calls.append(kwargs["cosines"])
        return []

    monkeypatch.setattr(mcp_server.rank, "rank", spy)
    cfg = config.load()
    conn = index_db.connect(cfg.db_path)
    try:
        mcp_server._recall(cfg, conn, {"query": "audit"})
        mcp_server._lookup_error(cfg, conn, {"error_text": "audit"})
        mcp_server._why(cfg, conn, {"topic": "audit"})
    finally:
        conn.close()

    assert calls == [{"D": 0.7}, {"D": 0.7}, {"D": 0.7}]


def test_changing_the_model_re_embeds_every_card(tmp_path, monkeypatch):
    """A model swap must invalidate the cache, or the index serves old-model vectors.

    reindex skips a card whose stored hash matches, so hashing the body alone left
    MODEL_NAME unobservable: changing the model re-embedded nothing and recall scored
    new-model queries against old-model vectors. A 384 -> 384 swap raises no error.
    """
    install_fake_fastembed(monkeypatch)
    conn = build_db(tmp_path, [make_card(title="Smartsheet truncation", body="Chunk writes.")])

    assert embed.reindex(conn) == 1
    assert len(FakeTextEmbedding.calls) == 1          # embedded once
    embed.reindex(conn)
    assert len(FakeTextEmbedding.calls) == 1          # unchanged body -> no re-embed

    monkeypatch.setattr(embed, "MODEL_NAME", "BAAI/bge-small-en-v1.5")
    embed.reindex(conn)
    assert len(FakeTextEmbedding.calls) == 2          # different model -> re-embedded
    assert FakeTextEmbedding.calls[-1] == FakeTextEmbedding.calls[0]


def _reset_model(monkeypatch):
    """Undo the process-wide model cache so each test loads its own."""
    monkeypatch.setattr(embed, "_model_started", False)
    monkeypatch.setattr(embed, "_model_obj", None)
    monkeypatch.setattr(embed, "_model_ready", threading.Event())


def test_a_slow_model_load_costs_the_budget_not_the_load(tmp_path, monkeypatch):
    """Loading the model must never be able to hang a query.

    `from fastembed import TextEmbedding` drags in onnxruntime and numpy —
    hundreds of MB of native extensions. With site-packages on a cloud-synced
    folder (OneDrive Files-On-Demand marks every .pyd a placeholder) the first
    load blocks on file hydration: measured >50s here, unbounded in principle.

    It used to run inline in `cosines`, guarded by `except Exception` — which
    catches nothing, because a slow import does not raise, it just never
    returns. The MCP client had no response and no error until its own 1800s
    idle timeout fired, three calls in a row.
    """
    _reset_model(monkeypatch)
    started, release = threading.Event(), threading.Event()

    def never_finishes():
        started.set()
        release.wait(30)          # never sets _model_ready

    monkeypatch.setattr(embed, "_load_model", never_finishes)
    t0 = time.monotonic()
    try:
        assert embed.model(budget=0.2) is None      # degrade, do not block
        assert time.monotonic() - t0 < 5.0
        assert started.wait(5)                      # and it really did start
    finally:
        release.set()


def test_cosines_ranks_lexically_while_the_model_is_still_loading(tmp_path, monkeypatch):
    """A not-yet-ready model means "rank without vectors", never "no results"."""
    _reset_model(monkeypatch)
    conn = build_db(tmp_path, [make_card()])
    embed.ensure_schema(conn)
    conn.execute("INSERT INTO vectors(id, body_hash, vec) VALUES(?,?,?)",
                 ("c1", "h1", array("f", [1.0, 0.0, 0.0]).tobytes()))
    conn.commit()
    monkeypatch.setattr(embed, "model", lambda budget=0.0: None)
    assert embed.cosines(conn, "query", ["c1"]) is None


def test_the_model_is_loaded_once_not_once_per_query(tmp_path, monkeypatch):
    """`cosines` built a fresh TextEmbedding on every call — and each build
    probes the model cache over the network before handing back the same
    object (~0.8s warm; behind a proxy that 403s, two dead HTTP round-trips)."""
    _reset_model(monkeypatch)
    loads = []
    monkeypatch.setattr(embed, "_load_model", lambda: (
        loads.append(1), embed._model_ready.set()) and None)

    for _ in range(5):
        embed.model(budget=5.0)
    assert len(loads) == 1
