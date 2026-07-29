"""SQLite index over the wiki. SPEC §5.2.

The wiki is the source of truth; this DB is a derived cache — rebuild() must
always regenerate it losslessly (Manifesto P4, ADR-001).
"""

from __future__ import annotations

import atexit
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

from . import cards as cards_mod

SCHEMA = """
CREATE TABLE IF NOT EXISTS cards(
  id TEXT PRIMARY KEY, type TEXT, title TEXT, body TEXT,
  project TEXT, status TEXT, confidence REAL, verified INTEGER,
  created TEXT, updated TEXT, superseded_by TEXT,
  usefulness REAL DEFAULT 0,
  files TEXT,
  last_used TEXT, path TEXT,
  source_commit TEXT, stale INTEGER DEFAULT 0, repo_url TEXT);
CREATE TABLE IF NOT EXISTS projects(
  slug TEXT PRIMARY KEY, repo_url TEXT, head_commit TEXT, updated TEXT);
CREATE VIRTUAL TABLE IF NOT EXISTS cards_fts USING fts5(
  id UNINDEXED, title, body, tags);
CREATE TABLE IF NOT EXISTS links(src TEXT, dst TEXT, kind TEXT);
CREATE TABLE IF NOT EXISTS checkpoints(
  source TEXT PRIMARY KEY, mtime REAL, bytes_done INTEGER, content_hash TEXT);
CREATE TABLE IF NOT EXISTS servings(
  id INTEGER PRIMARY KEY, ts TEXT, harness TEXT, session_id TEXT,
  mode TEXT, query TEXT, card_ids TEXT, outcome TEXT);
CREATE TABLE IF NOT EXISTS review_queue(
  card_json TEXT, reason TEXT, created TEXT);
CREATE INDEX IF NOT EXISTS idx_cards_project ON cards(project, status);
"""

_WORD = re.compile(r"[A-Za-z0-9_]{2,}")
_STOP = frozenset(
    "the a an and or not is are was were be been being do does did to of in on at "
    "for with from by as it this that these those i you we they he she can could "
    "should would will shall may might must have has had please fix add make get "
    "set use my our your how what why when where which yes no ok okay".split()
)


def _migrate(conn: sqlite3.Connection) -> None:
    """`CREATE TABLE IF NOT EXISTS` never adds columns to a table that already
    exists, so a DB created before ADR-018 needs them grafted on. Readonly
    connections can't migrate — rank.py reads these columns defensively."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(cards)")}
    for name, ddl in (("source_commit", "TEXT"), ("stale", "INTEGER DEFAULT 0"),
                      ("repo_url", "TEXT")):
        if name not in cols:
            conn.execute(f"ALTER TABLE cards ADD COLUMN {name} {ddl}")


class IndexUnavailable(RuntimeError):
    """The index could not be opened.

    Distinct from "the index is empty" — and that distinction is the whole game.
    Every silent afterwit outage has been a healthy index that nobody could reach,
    reported to the agent as if the user simply had no history. sqlite's own text
    (`unable to open database file`) names no path, no cause and no remedy, so an
    agent reasonably concludes "afterwit is unavailable" and proceeds blind.
    This carries enough to tell a wrong path from a permissions fault from a
    genuinely missing index, WITHOUT the reader having to reproduce anything.
    """


def _user_token() -> str:
    """A per-user token for shared temp paths, on every OS.

    `os.getuid` does not exist on Windows, and this ran at IMPORT time — so
    `import afterwit.index_db` raised AttributeError there, which is every entry
    point this package has. Windows gives each account its own %TEMP%, so the
    username is a fine substitute for the isolation this provides on POSIX.
    """
    getuid = getattr(os, "getuid", None)
    if getuid is not None:
        return str(getuid())
    return re.sub(r"[^A-Za-z0-9_-]+", "", os.environ.get("USERNAME", "")) or "user"


_SNAP_PREFIX = f"afterwit-ro-{_user_token()}-"
_SNAP_TTL_SECONDS = 6 * 3600


def _tempdir() -> Path | None:
    """None when the machine offers no writable temp dir at all.

    `codex --sandbox read-only` mounts /tmp, /var/tmp, /usr/tmp AND cwd read-only, so
    `tempfile.gettempdir()` does not return an unusable path — it RAISES. Every caller
    that assumed "TMPDIR always exists" fails there.
    """
    try:
        d = Path(tempfile.gettempdir())
    except (OSError, AttributeError):
        return None
    return d if os.access(d, os.W_OK) else None


def _immutable_conn(p: Path) -> sqlite3.Connection:
    """Last resort: read a WAL db with nowhere on disk to write.

    `immutable=1` promises sqlite the file cannot change, so it opens without creating
    `-shm`. The price is that it CANNOT replay the `-wal`: any commit still sitting there
    is invisible. Writers checkpoint(TRUNCATE) so the WAL is normally empty and this read
    is exact — but if it is NOT empty we could be serving a stale, or worse an EMPTY,
    index while sounding confident. That is Gotcha #32, and it is the one failure this
    project exists to prevent: never let a reachable-but-wrong index read as 'no history'.
    """
    conn = sqlite3.connect(f"file:{p}?immutable=1", uri=True)
    wal = Path(str(p) + "-wal")
    unreplayed = wal.stat().st_size if wal.exists() else 0
    try:
        cards = conn.execute("SELECT count(*) FROM cards").fetchone()[0]
    except sqlite3.DatabaseError as e:
        conn.close()
        raise sqlite3.OperationalError(f"immutable read failed: {e}") from e
    if unreplayed and not cards:
        conn.close()
        raise sqlite3.OperationalError(
            f"index has {unreplayed}B of un-checkpointed WAL and no writable temp dir to "
            f"replay it into, so it reads as EMPTY — it is not. Run `afterwit index "
            f"--rebuild` (checkpoints the WAL), or grant this sandbox a writable TMPDIR."
        )
    return conn


def _sweep_orphan_snapshots() -> None:
    """Reap snapshots left by processes that died before atexit could run.

    A killed agent (or SIGKILL) skips both close() and atexit, stranding a full copy
    of the index. Age is the only signal available: a reader that has held one for
    hours is not one we can distinguish from a corpse. Unlinking an open sqlite file
    is safe on POSIX — the reader keeps its fd — and rmtree no-ops on Windows locks.
    """
    tmp = _tempdir()
    if tmp is None:
        return
    cutoff = time.time() - _SNAP_TTL_SECONDS
    for stale in tmp.glob(f"{_SNAP_PREFIX}*"):
        try:
            if stale.stat().st_mtime < cutoff:
                shutil.rmtree(stale, ignore_errors=True)
        except OSError:
            pass


class _SnapshotConnection(sqlite3.Connection):
    """Connection that removes its private read-only snapshot on close."""

    snapshot_dir: Path | None = None

    def close(self) -> None:
        directory = self.snapshot_dir
        try:
            super().close()
        finally:
            if directory is not None:
                shutil.rmtree(directory, ignore_errors=True)
                self.snapshot_dir = None


def _diagnose(db_path: Path | str, err: Exception) -> str:
    """Everything needed to name the cause on FIRST occurrence. Written because a
    bare OperationalError from a sandboxed agent cost hours and four falsified
    theories: the error must diagnose itself, since the environment that produced
    it is usually not one we can re-enter."""
    p = Path(db_path)
    par = p.parent
    bits = [f"afterwit index UNREACHABLE at {p}", f"sqlite: {err}"]
    bits.append(f"file exists: {p.exists()}" + (f" ({p.stat().st_size}b)" if p.exists() else ""))
    bits.append(f"dir exists: {par.is_dir()}, dir writable: {os.access(par, os.W_OK)}")
    if p.exists():
        bits.append(f"file readable: {os.access(p, os.R_OK)}, writable: {os.access(p, os.W_OK)}")
    # A wrong HOME silently relocates the whole config (see config._state_dir), and
    # a sandbox is the usual way one appears — so always say which HOME we used.
    home = os.environ.get("HOME") or os.environ.get("USERPROFILE") or "?"
    bits.append(f"HOME={home} user={_user_token()}")
    bits.append("this is a BROKEN INSTALL, not an empty knowledge base — "
                "do not conclude the user has no history. Run: afterwit doctor")
    return "\n  ".join(bits)


def _readonly_conn(p: Path) -> sqlite3.Connection:
    """Read the index WITHOUT needing write access to its directory.

    A WAL database creates and refreshes `-shm`/`-wal` NEXT TO the db file, so even a
    pure SELECT needs a WRITABLE DIRECTORY. A sandboxed agent (codex's default
    workspace-write mounts $HOME read-only) can read the index and cannot write its
    directory — so `connect(mode=ro)` SUCCEEDS and the FIRST QUERY dies with
    `unable to open database file`. Proven by running `aw recall` inside codex:
    `exists True, R_OK True, W_OK False, dir W_OK False`. This is why the failure
    landed in search() and not in connect(), and why chmod-based tests never
    reproduced it.

    Three tiers, most exact first:

    1. Directory writable — read it directly.
    2. Directory read-only, TMPDIR writable (codex `workspace-write`, which mounts $HOME
       read-only) — snapshot db + `-wal` so SQLite REPLAYS the WAL and we see current
       data, never a stale pre-WAL snapshot.
    3. Nothing writable anywhere (codex `--sandbox read-only`: /tmp, /var/tmp and cwd are
       all read-only, so `tempfile.gettempdir()` itself raises) — `immutable=1`, the only
       mode that needs no `-shm` at all. It cannot replay the WAL, so it is guarded below.
    """
    if os.access(p.parent, os.W_OK):
        return sqlite3.connect(f"file:{p}?mode=ro", uri=True)

    tmp = _tempdir()
    if tmp is None:
        return _immutable_conn(p)

    _sweep_orphan_snapshots()
    # Every reader owns its snapshot. A shared filename let concurrent MCP
    # processes overwrite each other's db/WAL pair, producing malformed images
    # and occasionally a pre-schema database.
    for _ in range(3):
        snap_dir = Path(tempfile.mkdtemp(prefix=_SNAP_PREFIX, dir=tmp))
        snap = snap_dir / p.name
        conn = None
        try:
            for suffix in ("", "-wal"):
                src, dst = Path(str(p) + suffix), Path(str(snap) + suffix)
                if src.exists():
                    shutil.copy2(src, dst)
            conn = sqlite3.connect(
                f"file:{snap}?mode=ro", uri=True, factory=_SnapshotConnection
            )
            ok = conn.execute("PRAGMA quick_check").fetchone()
            conn.execute("SELECT 1 FROM cards LIMIT 1").fetchone()
            if ok and ok[0] == "ok":
                conn.snapshot_dir = snap_dir
                # close() alone leaks: recall/stats/doctor just exit. Each snapshot is
                # a full copy of the private knowledge db, so a missed cleanup grows
                # /tmp without bound (Gotcha #39).
                atexit.register(shutil.rmtree, snap_dir, ignore_errors=True)
                return conn
        except (sqlite3.DatabaseError, OSError):
            pass
        if conn is not None:
            conn.close()
        shutil.rmtree(snap_dir, ignore_errors=True)
    raise sqlite3.OperationalError("could not create a consistent read-only index snapshot")


def connect(db_path: Path | str, readonly: bool = False) -> sqlite3.Connection:
    """The one door to the index. Every sqlite open failure is converted here into
    an IndexUnavailable carrying its own diagnosis — one guard in the shared
    function rather than a guess at each of the dozen call sites."""
    try:
        if readonly:
            conn = _readonly_conn(Path(db_path))
            conn.row_factory = sqlite3.Row
            return conn
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn
    except (sqlite3.OperationalError, OSError) as e:
        raise IndexUnavailable(_diagnose(db_path, e)) from e


def record_project(conn: sqlite3.Connection, slug: str,
                   repo_url: str | None, head: str | None) -> None:
    """Project identity + position (ADR-018). repo_url is the cross-device key:
    the folder name differs per machine, the remote does not."""
    conn.execute(
        """INSERT INTO projects(slug, repo_url, head_commit, updated)
           VALUES(?,?,?,datetime('now'))
           ON CONFLICT(slug) DO UPDATE SET
             repo_url=excluded.repo_url, head_commit=excluded.head_commit,
             updated=excluded.updated""",
        (slug, repo_url, head),
    )


def upsert_card(conn: sqlite3.Connection, card: cards_mod.Card, path: str) -> None:
    card.validate()
    # The `cards`/`cards_fts` tables are what recall, inject and the MCP tools
    # SERVE. Sanitizing here (idempotent — real callers pass a card already
    # cleaned by cards.save()) makes "nothing unsanitized is ever served" true by
    # construction, independent of the caller, and matches the invariant that the
    # index mirrors the sanitized on-disk card (ADR-022).
    cards_mod.sanitize(card)
    # usefulness/last_used: inserted for new rows (so rebuild() restores the
    # frontmatter checkpoint) but NOT updated on conflict — between write-backs
    # the DB is the live counter and frontmatter may be stale (ADR-008).
    # `stale` is derived (recomputed by consolidate.mark_stale from git) and is
    # therefore INSERT-only like the usage counters: re-indexing a card must not
    # silently un-flag drift that lint already found.
    conn.execute(
        """INSERT INTO cards(id,type,title,body,project,status,confidence,verified,
             created,updated,superseded_by,files,path,usefulness,last_used,
             source_commit,stale,repo_url)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?)
           ON CONFLICT(id) DO UPDATE SET
             type=excluded.type, title=excluded.title, body=excluded.body,
             project=excluded.project, status=excluded.status,
             confidence=excluded.confidence, verified=excluded.verified,
             updated=excluded.updated, superseded_by=excluded.superseded_by,
             files=excluded.files, path=excluded.path,
             source_commit=excluded.source_commit, repo_url=excluded.repo_url""",
        (card.id, card.type, card.title, card.body, card.project, card.status,
         card.confidence, int(card.verified), card.created, card.updated,
         card.superseded_by, json.dumps(card.files), path,
         card.usefulness, card.last_used, card.source_commit, card.repo_url),
    )
    conn.execute("DELETE FROM cards_fts WHERE id=?", (card.id,))
    conn.execute(
        "INSERT INTO cards_fts(id,title,body,tags) VALUES(?,?,?,?)",
        (card.id, card.title, card.body, " ".join(card.tags)),
    )
    conn.execute("DELETE FROM links WHERE src=?", (card.id,))
    for name in card.wikilinks():
        conn.execute("INSERT INTO links(src,dst,kind) VALUES(?,?,?)",
                     (card.id, name, "wikilink"))
    # curated links (ADR-045): dst is a card ID, not a title — readers need no
    # slug resolution. Mirrored from frontmatter, so rebuild() restores them.
    for rid in card.related:
        conn.execute("INSERT INTO links(src,dst,kind) VALUES(?,?,?)",
                     (card.id, rid, "related"))


def rebuild(conn: sqlite3.Connection, wiki_root: Path) -> int:
    """Drop derived rows and re-index every card in the wiki. Lossless by design."""
    conn.execute("DELETE FROM cards")
    conn.execute("DELETE FROM cards_fts")
    conn.execute("DELETE FROM links")
    n = 0
    for path, card in cards_mod.iter_cards(wiki_root):
        upsert_card(conn, card, str(path))
        n += 1
    conn.commit()
    # Fold the WAL back into the db file. A reader with nowhere writable (codex
    # --sandbox read-only) can only use immutable=1, which cannot replay a WAL — so
    # leaving commits there is what makes a full index read as stale or empty.
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return n


def build_match_query(text: str, max_terms: int = 12) -> str:
    """Turn free text (a user prompt) into a safe FTS5 OR-query.

    Raw prompts contain FTS operators and are too long for implicit-AND
    matching; distinctive OR'd tokens give recall, rank() restores precision.
    """
    seen: list[str] = []
    for w in _WORD.findall(text):
        lw = w.lower()
        if lw in _STOP or lw in seen:
            continue
        seen.append(lw)
        if len(seen) >= max_terms:
            break
    return " OR ".join(f'"{w}"' for w in seen)


def search(conn: sqlite3.Connection, query_text: str, project: str | None = None,
           k: int = 20) -> list[sqlite3.Row]:
    """FTS candidates with raw bm25 (smaller = better). Ranking happens in rank.py."""
    match = build_match_query(query_text)
    if not match:
        return []
    rows = conn.execute(
        """SELECT c.*, bm25(cards_fts) AS bm25_raw
           FROM cards_fts JOIN cards c ON c.id = cards_fts.id
           WHERE cards_fts MATCH ?
           ORDER BY bm25_raw LIMIT ?""",
        (match, k * 3 if project else k),
    ).fetchall()
    return rows[: k * 3]


def for_file(conn: sqlite3.Connection, path_fragment: str,
             project: str | None = None, k: int = 10) -> list[sqlite3.Row]:
    sql = "SELECT * FROM cards WHERE status='active' AND files LIKE ?"
    args: list = [f"%{path_fragment}%"]
    if project:
        sql += " AND project IN (?, 'global')"
        args.append(project)
    sql += " ORDER BY usefulness DESC, updated DESC LIMIT ?"
    args.append(k)
    return conn.execute(sql, args).fetchall()


def log_serving(conn: sqlite3.Connection, *, ts: str, harness: str, session_id: str,
                mode: str, query: str, card_ids: list[str]) -> None:
    conn.execute(
        "INSERT INTO servings(ts,harness,session_id,mode,query,card_ids) VALUES(?,?,?,?,?,?)",
        (ts, harness, session_id, mode, query[:500], json.dumps(card_ids)),
    )
    conn.commit()
