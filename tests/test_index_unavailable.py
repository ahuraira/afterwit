"""An unreachable index must never reach an agent as a traceback.

An agent shelled `aw recall` and got a raw
`sqlite3.OperationalError: unable to open database file` — no path, no cause, no
remedy. It concluded "the knowledge base is unavailable on this machine" and
reasoned on without 463 cards it could have had. Both doors an agent comes
through (the CLI and the MCP server) must fail with a self-diagnosing message.
"""

import os
import stat
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from afterwit import cli, index_db, mcp_server
from afterwit.config import Config
from tests.test_cards import make_card
from tests.conftest import toml_config

# chmod-based permission tests are POSIX-only, and root ignores the bits anyway.
# `os.getuid` does not exist on Windows, so this cannot be evaluated inline in a
# decorator — that raised AttributeError at COLLECTION time and took the whole
# suite down with it.
_PERMS_ARE_ADVISORY = os.name != "posix" or getattr(os, "getuid", lambda: 1)() == 0
_PERMS_REASON = "chmod does not restrict here (Windows, or running as root)"


def _no_tempdir() -> str:
    """What `codex --sandbox read-only` really does: gettempdir() RAISES, because
    /tmp, /var/tmp, /usr/tmp and cwd are every one of them read-only."""
    raise FileNotFoundError(2, "No usable temporary directory found")


def _unwritable_dir(tmp_path):
    d = tmp_path / "locked"
    d.mkdir()
    db = d / "index.db"
    index_db.connect(db).close()          # a real index exists...
    os.chmod(d, stat.S_IREAD | stat.S_IEXEC)   # ...in a directory we cannot write
    return db, d


def test_connect_raises_self_diagnosing_error(tmp_path):
    db = tmp_path / "nope" / "index.db"
    with pytest.raises(index_db.IndexUnavailable) as ei:
        index_db.connect(db, readonly=True)
    msg = str(ei.value)
    assert str(db) in msg                      # WHICH path
    assert "file exists: False" in msg         # WHY
    assert "HOME=" in msg                      # the usual culprit: a sandbox HOME
    assert "afterwit doctor" in msg            # the remedy
    assert "not an empty knowledge base" in msg.lower()   # the load-bearing line


def test_cli_never_tracebacks_at_an_agent(tmp_path, monkeypatch, capsys):
    """The actual regression. Note the db_path EXISTS — a missing file already had a
    clean message; the agent's traceback came from a path that got PAST that guard
    and blew up inside connect(). Reproduced deterministically here by pointing at
    something sqlite cannot open as a database, which raises the very same
    `OperationalError: unable to open database file`. `aw recall` must exit 1 with a
    readable diagnosis, never a traceback."""
    (tmp_path / "index.db").mkdir()            # exists, but is not a database
    monkeypatch.setenv("AFTERWIT_CONFIG", str(tmp_path / "cfg.toml"))
    (tmp_path / "cfg.toml").write_text(toml_config(db_path=tmp_path / "index.db"))
    rc = cli.main(["recall", "anything"])      # must not raise
    err = capsys.readouterr().err
    assert rc == 1
    assert "sqlite:" in err                        # sqlite's own text, preserved
    assert "UNREACHABLE" in err and "afterwit doctor" in err
    assert "HOME=" in err                          # + everything sqlite never tells you


def test_mcp_returns_the_diagnosis_not_an_empty_result(tmp_path):
    cfg = Config(wiki_root=tmp_path, db_path=tmp_path / "gone" / "index.db",
                 projects_root=tmp_path)
    out = mcp_server.dispatch("recall", {"query": "x"}, cfg=cfg)
    assert "UNREACHABLE" in out
    assert mcp_server._EMPTY not in out        # never "proceed normally"


@pytest.mark.skipif(_PERMS_ARE_ADVISORY, reason=_PERMS_REASON)
def test_readonly_dir_can_still_be_QUERIED_not_just_opened(tmp_path):
    """The codex bug, pinned.

    A WAL db writes `-shm`/`-wal` beside itself even to answer a SELECT, so a
    read-only DIRECTORY breaks reads. connect() SUCCEEDS and the first QUERY dies —
    which is why guarding only the open never fixed it, and why every chmod-on-the-
    FILE test passed. Assert an actual query, not an open.
    """
    from afterwit import cards as cards_mod
    from tests.test_cards import make_card

    wiki, d = tmp_path / "wiki", tmp_path / "state"
    d.mkdir()
    db = d / "index.db"
    conn = index_db.connect(db)
    cards_mod.save(make_card(title="Prisma P1001 fix", verified=True), wiki)
    index_db.rebuild(conn, wiki)
    conn.close()

    os.chmod(d, stat.S_IREAD | stat.S_IEXEC)      # the codex sandbox: dir W_OK False
    try:
        assert not os.access(d, os.W_OK)          # precondition actually holds
        c = index_db.connect(db, readonly=True)
        rows = index_db.search(c, "prisma", project=None, k=5)   # <-- where it died
        assert any("P1001" in r["title"] for r in rows)
    finally:
        os.chmod(d, stat.S_IRWXU)


@pytest.mark.skipif(_PERMS_ARE_ADVISORY, reason=_PERMS_REASON)
def test_permission_fault_is_diagnosed_not_reported_as_empty(tmp_path):
    """The shape I could not reproduce from the agent's environment, pinned anyway:
    a real index that the process lacks the rights to open must say so, naming the
    permissions — not fall through to 'no known history'."""
    db, d = _unwritable_dir(tmp_path)
    try:
        with pytest.raises(index_db.IndexUnavailable) as ei:
            index_db.connect(db)               # rw connect into an unwritable dir
        msg = str(ei.value)
        assert "dir writable: False" in msg
        assert "afterwit doctor" in msg
    finally:
        os.chmod(d, stat.S_IRWXU)              # so tmp cleanup can remove it


@pytest.mark.skipif(_PERMS_ARE_ADVISORY, reason=_PERMS_REASON)
def test_reads_when_NOTHING_on_the_machine_is_writable(tmp_path, monkeypatch):
    """`codex --sandbox read-only` mounts /tmp, /var/tmp AND cwd read-only, so there is
    no temp dir to snapshot into — `tempfile.gettempdir()` raises rather than returning
    a bad path. Both snapshot strategies died there and recall reported UNREACHABLE on a
    perfectly healthy index. Fall back to immutable=1, which needs no -shm.
    """
    from afterwit import cards as cards_mod
    from tests.test_cards import make_card

    wiki, d = tmp_path / "wiki", tmp_path / "state"
    d.mkdir()
    db = d / "index.db"
    conn = index_db.connect(db)
    cards_mod.save(make_card(title="Prisma P1001 fix", verified=True), wiki)
    index_db.rebuild(conn, wiki)          # checkpoints the WAL, so immutable sees all
    conn.close()

    monkeypatch.setattr(tempfile, "gettempdir", _no_tempdir)   # the real sandbox failure
    os.chmod(d, stat.S_IREAD | stat.S_IEXEC)
    try:
        c = index_db.connect(db, readonly=True)
        rows = index_db.search(c, "prisma", project=None, k=5)
        assert any("P1001" in r["title"] for r in rows)
    finally:
        os.chmod(d, stat.S_IRWXU)


@pytest.mark.skipif(_PERMS_ARE_ADVISORY, reason=_PERMS_REASON)
def test_unreplayable_wal_fails_loud_instead_of_reading_as_empty(tmp_path, monkeypatch):
    """The Gotcha #32 trap, pinned. immutable=1 cannot replay a WAL. If the cards are
    still IN the WAL and we cannot replay it, the index reads as EMPTY — and an agent
    told 'no results' concludes the user has no history. It must fail loud instead."""
    from afterwit import cards as cards_mod
    from tests.test_cards import make_card

    wiki, d = tmp_path / "wiki", tmp_path / "state"
    d.mkdir()
    db = d / "index.db"
    conn = index_db.connect(db)
    cards_mod.save(make_card(title="Prisma P1001 fix", verified=True), wiki)
    for path, card in cards_mod.iter_cards(wiki):
        index_db.upsert_card(conn, card, str(path))
    conn.commit()                            # committed, but NEVER checkpointed:
    assert Path(str(db) + "-wal").stat().st_size > 0     # every card lives in the WAL

    monkeypatch.setattr(tempfile, "gettempdir", _no_tempdir)
    os.chmod(d, stat.S_IREAD | stat.S_IEXEC)
    try:
        with pytest.raises(index_db.IndexUnavailable) as ei:
            index_db.connect(db, readonly=True)          # must NOT return "0 results"
        assert "afterwit" in str(ei.value)               # a diagnosis, not a shrug
    finally:
        os.chmod(d, stat.S_IRWXU)


@pytest.mark.skipif(_PERMS_ARE_ADVISORY, reason=_PERMS_REASON)
def test_snapshot_is_reaped_when_the_reader_exits_without_closing(tmp_path, monkeypatch):
    """Every sandboxed reader copies the WHOLE index to TMPDIR. `recall`, `stats` and
    `doctor` all just exit — none of them call close() — so a close()-only cleanup
    leaked a full copy of the private knowledge db per invocation (188MB of /tmp in
    one day). Cleanup must survive a plain interpreter exit, not just a polite one.
    """
    from afterwit import cards as cards_mod
    from tests.test_cards import make_card

    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))     # count OUR snapshots only
    (tmp_path / "tmp").mkdir()
    wiki, d = tmp_path / "wiki", tmp_path / "state"
    d.mkdir()
    db = d / "index.db"
    conn = index_db.connect(db)
    cards_mod.save(make_card(title="Prisma P1001 fix", verified=True), wiki)
    index_db.rebuild(conn, wiki)
    conn.close()

    os.chmod(d, stat.S_IREAD | stat.S_IEXEC)                # forces the snapshot path
    try:
        code = ("import os,sys;sys.path.insert(0,%r);from afterwit import index_db;"
                "c=index_db.connect(%r,readonly=True);"
                "print(index_db.search(c,'prisma',project=None,k=5)[0]['title'])"
                % (str(Path(index_db.__file__).parents[2]), str(db)))
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                             env={**os.environ, "TMPDIR": str(tmp_path / "tmp")})
        assert "P1001" in out.stdout, out.stderr        # it really did read via snapshot
    finally:
        os.chmod(d, stat.S_IRWXU)

    leaked = list((tmp_path / "tmp").glob("afterwit-ro-*"))
    assert leaked == [], f"reader exited without close() and stranded {leaked}"


def test_concurrent_readonly_snapshots_are_isolated(tmp_path, monkeypatch):
    from afterwit import cards

    wiki = tmp_path / "wiki"
    cards.save(make_card(verified=True), wiki)
    db = tmp_path / "index.db"
    writer = index_db.connect(db)
    index_db.rebuild(writer, wiki)
    monkeypatch.setattr(index_db.os, "access", lambda *a: False)

    def read_count(_):
        conn = index_db.connect(db, readonly=True)
        try:
            return conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=16) as pool:
        assert list(pool.map(read_count, range(64))) == [1] * 64
    writer.close()
