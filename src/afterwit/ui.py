"""Local review UI. SPEC §9.1 review gate — the human trust boundary.

Serves on 127.0.0.1 ONLY (never bind 0.0.0.0: the wiki is private memory and
approve-endpoints mutate it). Approval is the single place a card gains
verified=true — MCP save_insight and the distiller can only reach the queue.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import cards as cards_mod
from . import config as config_mod
from . import consolidate, index_db, rank

DEFAULT_PORT = 8377


def queue_insert(conn: sqlite3.Connection, card: cards_mod.Card, reason: str,
                 wiki_root: Path | None = None) -> None:
    """The single write path INTO the queue (used by wiki writer / save_insight).

    Sanitize here, not only in `cards.save()`: a queued card is stored as raw
    `card_json` in SQLite, shown in the review UI, and — with ADR-021 on — fed to
    the auto-reviewer LLM, all long before any approval reaches `save()`. Without
    this, a secret an agent pasted via `save_insight` sits in the queue in the
    clear and the auto-review secret-gate (`has_secret`) never fires, because a
    raw token carries no `[REDACTED:]` marker yet. Sanitizing now closes both.
    """
    cards_mod.sanitize(card)
    conn.execute(
        "INSERT INTO review_queue(card_json, reason, created) VALUES(?,?,?)",
        (json.dumps(card.__dict__), reason, datetime.now(timezone.utc).isoformat()),
    )
    if wiki_root is not None:
        pending = wiki_root / "review" / f"{card.id}.md"
        pending.parent.mkdir(parents=True, exist_ok=True)
        pending.write_text(cards_mod.render(card), encoding="utf-8")
    conn.commit()


def restore_queue_from_wiki(conn: sqlite3.Connection, wiki_root: Path) -> int:
    """Restore synced pending reviews into the device-local SQLite queue."""
    queued = set()
    for row in conn.execute("SELECT card_json FROM review_queue"):
        try:
            queued.add(json.loads(row[0]).get("id"))
        except (json.JSONDecodeError, AttributeError):
            continue
    restored = 0
    for path in sorted((wiki_root / "review").glob("*.md")):
        try:
            card = cards_mod.load(path)
        except (OSError, cards_mod.CardError):
            continue
        if card.id not in queued:
            queue_insert(conn, card, "synced-review")
            queued.add(card.id)
            restored += 1
    return restored


def _list_queue(conn) -> list[dict]:
    enqueue_unverified(conn)
    rows = conn.execute(
        "SELECT rowid, card_json, reason, created FROM review_queue ORDER BY created"
    ).fetchall()
    return [{"rowid": r["rowid"], "card": json.loads(r["card_json"]),
             "reason": r["reason"], "created": r["created"]} for r in rows]


def enqueue_unverified(conn: sqlite3.Connection, wiki_root: Path | None = None) -> int:
    """Backfill legacy active, unverified cards into the real review flow."""
    queued: set[str] = set()
    for row in conn.execute("SELECT card_json FROM review_queue"):
        try:
            queued.add(str(json.loads(row[0]).get("id", "")))
        except (json.JSONDecodeError, AttributeError):
            continue
    added = 0
    for row in conn.execute(
        "SELECT id, path FROM cards WHERE status='active' AND verified=0"
    ):
        if row["id"] in queued:
            continue
        try:
            card = cards_mod.load(Path(row["path"]).expanduser())
        except (OSError, cards_mod.CardError):
            continue
        queue_insert(conn, card, "legacy-unverified", wiki_root)
        queued.add(card.id)
        added += 1
    return added


def _approve(cfg, conn, rowid: int, edited: dict | None,
             reviewed_by: str = "human") -> dict:
    row = conn.execute(
        "SELECT card_json FROM review_queue WHERE rowid=?", (rowid,)
    ).fetchone()
    if row is None:
        raise KeyError(rowid)
    data = json.loads(row["card_json"])
    if edited:
        for k in ("title", "body", "type", "tags", "files", "project"):
            if k in edited:
                data[k] = edited[k]
    # The single place a card becomes servable. `reviewed_by` records WHO cleared
    # it — a human, or the independent auto-reviewer (ADR-021). Never the distiller.
    data["verified"] = True
    data["reviewed_by"] = reviewed_by
    data["status"] = "active"
    data["updated"] = datetime.now(timezone.utc).date().isoformat()
    card = cards_mod.Card(**{k: v for k, v in data.items() if k in cards_mod.Card.__dataclass_fields__})
    card.validate()
    path = cards_mod.save(card, cfg.wiki_root)
    index_db.upsert_card(conn, card, str(path))
    conn.execute("DELETE FROM review_queue WHERE rowid=?", (rowid,))
    (cfg.wiki_root / "review" / f"{card.id}.md").unlink(missing_ok=True)
    conn.commit()
    return {"id": card.id, "path": str(path)}


def _reject(cfg, conn, rowid: int) -> None:
    row = conn.execute(
        "SELECT card_json FROM review_queue WHERE rowid=?", (rowid,)
    ).fetchone()
    if row is None:
        raise KeyError(rowid)
    data = json.loads(row["card_json"])
    title = data.get("title", "?")
    # A legacy card may already exist in the wiki. Rejection must quarantine it,
    # not merely hide its queue row while leaving it servable.
    if data.get("id") and conn.execute(
        "SELECT 1 FROM cards WHERE id=?", (data["id"],)
    ).fetchone():
        card = cards_mod.Card(**{
            k: v for k, v in data.items() if k in cards_mod.Card.__dataclass_fields__
        })
        card.status = "quarantined"
        card.updated = datetime.now(timezone.utc).date().isoformat()
        saved = cards_mod.save(card, cfg.wiki_root)
        index_db.upsert_card(conn, card, str(saved))
    log = config_mod.log_path(cfg.wiki_root)  # per-device audit log (ADR-019/020)
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as f:
        f.write(f"- {datetime.now(timezone.utc).isoformat()} review-reject: {title}\n")
    conn.execute("DELETE FROM review_queue WHERE rowid=?", (rowid,))
    if data.get("id"):
        (cfg.wiki_root / "review" / f"{data['id']}.md").unlink(missing_ok=True)
    conn.commit()


def _search(cfg, conn, q: str, project: str | None) -> list[dict]:
    rows = index_db.search(conn, q, project=project, k=20)
    scored = rank.rank(rows, project, floor=0.0, k=10, query_text=q)
    return [s.__dict__ for s in scored]


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _graph(conn) -> dict:
    """Nodes = cards; edges = resolved wikilinks + curated related links +
    shared-file coupling.

    Wikilink targets are free text — resolve against title slugs. File edges
    connect cards touching the same path (pairwise ≤5 cards/file, else a star
    around the most useful card, to avoid hairball cliques)."""
    rows = conn.execute(
        "SELECT id, title, type, project, status, usefulness, files, updated FROM cards"
    ).fetchall()
    nodes = [dict(r) for r in rows]
    by_project_slug = {(r["project"], _slug(r["title"])): r["id"] for r in rows}
    slug_ids: dict[str, list[str]] = {}
    projects = {r["id"]: r["project"] for r in rows}
    for r in rows:
        slug_ids.setdefault(_slug(r["title"]), []).append(r["id"])
    edges: list[dict] = []
    seen: set[tuple] = set()

    def add(a: str, b: str, kind: str):
        key = (min(a, b), max(a, b), kind)
        if a != b and key not in seen:
            seen.add(key)
            edges.append({"source": a, "target": b, "kind": kind})

    for r in conn.execute("SELECT src, dst FROM links WHERE kind='wikilink'"):
        slug = _slug(r["dst"])
        project = projects.get(r["src"], "global")
        candidates = slug_ids.get(slug, [])
        dst = (by_project_slug.get((project, slug))
               or by_project_slug.get(("global", slug))
               or (candidates[0] if len(candidates) == 1 else None))
        if dst:
            add(r["src"], dst, "wikilink")
    for r in conn.execute(
            "SELECT id, superseded_by FROM cards WHERE superseded_by IS NOT NULL"):
        add(r["id"], r["superseded_by"], "supersede")
    # curated links (ADR-045): dst is a card id already — no slug resolution.
    # Validated at write time, but a card deleted since must not add a ghost node.
    for r in conn.execute("SELECT src, dst FROM links WHERE kind='related'"):
        if r["dst"] in projects:
            add(r["src"], r["dst"], "related")

    by_file: dict[tuple[str, str], list] = {}
    for r in rows:
        for f in json.loads(r["files"] or "[]"):
            by_file.setdefault((r["project"], f), []).append(r)
    for _, group in by_file.items():
        if len(group) < 2:
            continue
        if len(group) <= 5:
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    add(group[i]["id"], group[j]["id"], "file")
        else:
            hub = max(group, key=lambda r: r["usefulness"] or 0)
            for r in group:
                add(hub["id"], r["id"], "file")
    return {"nodes": nodes, "edges": edges}


def _card_detail(conn, card_id: str) -> dict:
    row = conn.execute("SELECT * FROM cards WHERE id=?", (card_id,)).fetchone()
    if row is None:
        raise KeyError(card_id)
    d = dict(row)
    d["files"] = json.loads(d.get("files") or "[]")
    return d


def _stats(cfg, conn) -> dict:
    by_type = [dict(r) for r in conn.execute(
        "SELECT type, COUNT(*) n FROM cards WHERE status='active' GROUP BY type ORDER BY n DESC")]
    totals = dict(conn.execute(
        "SELECT status, COUNT(*) FROM cards GROUP BY status").fetchall())
    pending = conn.execute("SELECT COUNT(*) FROM review_queue").fetchone()[0]
    ks = consolidate.killswitch_status(conn)
    top = [dict(r) for r in conn.execute(
        "SELECT title, usefulness FROM cards WHERE status='active' ORDER BY usefulness DESC LIMIT 5")]
    bottom = [dict(r) for r in conn.execute(
        "SELECT title, usefulness FROM cards WHERE status='active' AND usefulness < 0 "
        "ORDER BY usefulness ASC LIMIT 5")]
    return {"by_type": by_type, "totals": totals, "pending": pending,
            "killswitch": ks, "top": top, "bottom": bottom,
            "inject_disabled": cfg.db_path.with_name("inject.disabled").exists()}


def _settings_payload(cfg: config_mod.Config) -> dict:
    """Everything the Settings surface needs in one round trip: current values,
    the schema that describes them, and what each harness on THIS machine offers.

    The model/effort lists come from the harnesses' own config files
    (harness.models), so the dropdown tracks whatever the user actually has
    installed instead of a list that rots at the next model release."""
    from . import distill, harness

    values: dict = {}
    for key in config_mod.EDITABLE:
        value = getattr(cfg, key)
        values[key] = str(value) if isinstance(value, Path) else value
    return {
        "values": values,
        "schema": [{"key": k, **spec} for k, spec in config_mod.EDITABLE.items()],
        "drivers": sorted(distill.DRIVERS),
        "harnesses": harness.all_info(),
        "config_path": str(Path(os.environ.get("AFTERWIT_CONFIG")
                                or Path.home() / ".afterwit" / "config.toml")),
    }


def _save_settings(payload: dict) -> dict:
    """Validate, persist, and hand back the config as it now reads from disk.

    Returns the RELOADED config, not the request echoed back: a value that was
    clamped, expanded (`~`) or dropped must be visible as what it became."""
    from . import distill

    if not isinstance(payload, dict) or not payload:
        raise config_mod.ConfigError("no settings supplied")
    drivers = tuple(sorted(distill.DRIVERS))
    updates = {k: config_mod.coerce(k, v, drivers) for k, v in payload.items()}
    path = config_mod.save(updates)
    # The server holds ONE Config, read once at serve(). Without this reload the
    # UI keeps reporting — and auto-review keeps using — the pre-save values
    # until someone restarts `afterwit ui`.
    Handler.cfg = config_mod.load(path)
    out = _settings_payload(Handler.cfg)
    if "run_time" in updates and updates["run_time"]:
        # Reapply an EXISTING scheduler so the new time is live now — but never
        # install one for a user who chose not to schedule (ADR-046).
        from . import install

        if install.cron_scheduled():
            res = install.install_cron(at=Handler.cfg.run_time)
            out["note"] = (f"nightly rescheduled to {Handler.cfg.run_time} "
                           f"({res['mode']}: {res['note']})")
        else:
            out["note"] = ("nightly not scheduled on this machine — "
                           "run: afterwit install cron")
    return out


class Handler(BaseHTTPRequestHandler):
    cfg: config_mod.Config  # set by serve()
    csrf_token = secrets.token_urlsafe(32)

    def _conn(self):
        return index_db.connect(self.cfg.db_path)

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # quiet
        pass

    def do_GET(self):
        u = urlparse(self.path)
        try:
            if u.path == "/":
                html = (Path(__file__).parent / "ui.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(html)))
                self.end_headers()
                self.wfile.write(html)
            elif u.path == "/api/review":
                with self._conn() as c:
                    self._json(_list_queue(c))
            elif u.path == "/api/search":
                q = parse_qs(u.query)
                with self._conn() as c:
                    self._json(_search(self.cfg, c, q.get("q", [""])[0],
                                       q.get("project", [None])[0]))
            elif u.path == "/api/stats":
                with self._conn() as c:
                    self._json(_stats(self.cfg, c))
            elif u.path == "/api/graph":
                with self._conn() as c:
                    self._json(_graph(c))
            elif u.path == "/api/config":
                self._json({"auto_review": self.cfg.auto_review,
                            "auto_review_model": self.cfg.auto_review_model,
                            "wiki_root": str(self.cfg.wiki_root),
                            "csrf_token": self.csrf_token})
            elif u.path == "/api/settings":
                self._json(_settings_payload(self.cfg))
            elif u.path == "/api/card":
                cid = parse_qs(u.query).get("id", [""])[0]
                with self._conn() as c:
                    try:
                        self._json(_card_detail(c, cid))
                    except KeyError:
                        self._json({"error": "unknown card"}, 404)
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def _autoreview_one(self, conn, rowid: int) -> dict:
        """Advisory only: returns the reviewer's opinion, applies nothing. The
        human still presses approve/reject. Bulk apply is the separate route."""
        from . import review as review_mod
        row = conn.execute("SELECT card_json FROM review_queue WHERE rowid=?",
                           (rowid,)).fetchone()
        if row is None:
            raise KeyError(rowid)
        data = json.loads(row["card_json"])
        card = cards_mod.Card(**{k: v for k, v in data.items()
                                 if k in cards_mod.Card.__dataclass_fields__})
        v = review_mod.review_one(self.cfg, card)
        return {"rowid": rowid, "verdict": v.verdict, "reason": v.reason, "model": v.model}

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if self.headers.get("X-Afterwit-CSRF") != self.csrf_token:
                # `code` so the page can tell THIS 403 from the auto-review-disabled
                # one below and recover by re-fetching the token. The usual cause is
                # a restarted server: the token is per-process, so every open tab is
                # holding one that died with the old process.
                return self._json({"error": "missing or invalid CSRF token — "
                                            "reload the page", "code": "csrf"}, 403)
            if self.headers.get_content_type() != "application/json":
                return self._json({"error": "Content-Type must be application/json"}, 415)
            if not self.cfg.auto_review and path.endswith(("/autoreview", "autoreview-all")):
                return self._json(
                    {"error": "auto-review disabled — set auto_review = true in "
                              "~/.afterwit/config.toml"}, 403)
            if path == "/api/settings":
                # ConfigError is a ValueError — the handler below turns it into a
                # 400 carrying its message, which the form shows verbatim.
                return self._json(_save_settings(self._body()))
            with self._conn() as c:
                if path == "/api/review/autoreview-all":
                    from . import review as review_mod
                    return self._json(review_mod.review_queue(self.cfg, c))
                m = re.match(r"^/api/review/(\d+)/(approve|reject|autoreview)$", path)
                if not m:
                    return self._json({"error": "not found"}, 404)
                rowid, action = int(m.group(1)), m.group(2)
                if action == "autoreview":
                    return self._json(self._autoreview_one(c, rowid))
                payload = self._body()
                if action == "approve":
                    self._json(_approve(self.cfg, c, rowid, payload.get("card")))
                else:
                    _reject(self.cfg, c, rowid)
                    self._json({"ok": True})
        except KeyError:
            self._json({"error": "gone"}, 410)
        except Exception as e:
            self._json({"error": str(e)}, 400)


def serve(port: int = DEFAULT_PORT, cfg: config_mod.Config | None = None) -> ThreadingHTTPServer:
    Handler.cfg = cfg or config_mod.load()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)  # localhost only — see module docstring
    return srv


def main(port: int = DEFAULT_PORT) -> int:
    srv = serve(port)
    print(f"afterwit review UI: http://127.0.0.1:{srv.server_address[1]}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0
