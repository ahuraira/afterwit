"""CLI dispatch. Implemented so far: inject, recall, index. Remaining commands
(ingest, distill, serve-mcp, install, consolidate, review, eval, stats, doctor)
are P1-P5 work — see SPEC §13/§14; they must plug in here, not grow new entrypoints.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys

from .index_db import IndexUnavailable


def _cmd_recall(args) -> int:
    from . import config as config_mod
    from . import embed, index_db, rank

    cfg = config_mod.load()
    if not cfg.db_path.exists():
        print("no index yet — run: afterwit index --rebuild", file=sys.stderr)
        return 1
    conn = index_db.connect(cfg.db_path, readonly=True)
    rows = index_db.search(conn, args.query, project=args.project, k=20)
    cosines = embed.cosines(conn, args.query, [r["id"] for r in rows])
    scored = rank.rank(rows, args.project, floor=cfg.floor if not args.all else 0.0,
                       k=args.k, query_text=args.query, cosines=cosines)
    if not scored:
        print("(nothing above relevance floor)")
        return 0
    for s in scored:
        print(f"{s.score:.2f}  [{s.type}] {s.title}  ({s.project}, {(s.updated or '')[:10]}, {s.id})")
        if args.verbose:
            print("      " + " ".join(s.body.split())[:400])
    return 0


def _cmd_index(args) -> int:
    from . import config as config_mod
    from . import embed, index_db, ui

    cfg = config_mod.load()
    if not cfg.wiki_root.exists():
        print(f"wiki root missing: {cfg.wiki_root}", file=sys.stderr)
        return 1
    conn = index_db.connect(cfg.db_path)
    n = index_db.rebuild(conn, cfg.wiki_root)
    restored = ui.restore_queue_from_wiki(conn, cfg.wiki_root)
    vectors = embed.reindex(conn)
    stale = _restale(conn, cfg)
    conn.close()
    print(f"indexed {n} cards from {cfg.wiki_root} -> {cfg.db_path}")
    print(f"vectorized {vectors} active cards")
    print(f"drift flagged on {stale} cards")
    print(f"restored {restored} synced reviews")
    return 0


def _restale(conn, cfg) -> int:
    """`index_db.rebuild()` DELETEs the cards table, so every derived `stale`
    flag is reset to 0. Recompute immediately or the whole ranking effect of
    ADR-018 silently evaporates after any rebuild — including the one `afterwit sync`
    runs at the END of every nightly, after lint (ADR-020 D1)."""
    from . import consolidate
    try:
        return len(consolidate.mark_stale(conn, cfg.projects_root, cfg.project_aliases))
    except Exception as e:  # noqa: BLE001 — drift is advisory; never break a rebuild
        print(f"warning: drift recompute failed: {e!r}", file=sys.stderr)
        return 0


def _cmd_queue(args) -> int:
    """Agent write path: card JSON on stdin → review queue. Never writes the wiki
    directly — human approval in `afterwit ui` is the only path to verified (P6).
    Confidence is capped at AGENT_CONFIDENCE_CAP regardless of what the agent claims."""
    import json
    from datetime import datetime, timezone

    from . import cards as cards_mod
    from . import config as config_mod
    from . import index_db, postprocess, ui

    data = json.loads(sys.stdin.read())
    today = datetime.now(timezone.utc).date().isoformat()
    card = cards_mod.Card(
        id=cards_mod.new_ulid(),
        type=data["type"], title=data["title"], project=data["project"],
        status="active", body=data["body"], sources=data["sources"],
        tags=data.get("tags", []), files=data.get("files", []),
        confidence=min(float(data.get("confidence", 0.7)),
                       postprocess.AGENT_CONFIDENCE_CAP),
        verified=False, created=today, updated=today,
        # Stamped, never read from `data`: attribution an agent supplies about
        # itself is unverifiable. "agent" is what we actually know (ADR-035).
        distilled_by="agent",
    )
    cfg = config_mod.load()
    from . import gitmeta
    # anchor to what the agent observed (ADR-018/020); (None, None) for non-git
    card.source_commit, card.repo_url = gitmeta.anchor(cfg.projects_root, card.project,
                                                     aliases=cfg.project_aliases)
    card.validate()
    conn = index_db.connect(cfg.db_path)
    ui.queue_insert(conn, card, data.get("reason", "agent-proposed"), cfg.wiki_root)
    conn.close()
    print(f"queued for review: {card.id}  {card.title}")
    return 0


def _remote_visibility(url: str) -> str:
    """"public" | "private" | "unknown" for a git remote URL.

    Only GitHub can be checked, and only when `gh` is installed and authed —
    everything else is "unknown". We block on PROOF of public, never on absence
    of proof: refusing every unverifiable remote would lock out GitLab, a bare
    SSH host, and anyone without `gh`, and they'd disable the check entirely.
    """
    import shutil
    import subprocess

    # case-insensitive host, tolerate an explicit :port and embedded userinfo
    # (audit claim 8: `GitHub.com` and `github.com:443` previously fell through
    # to "unknown" and the push proceeded).
    m = re.search(r"(?i)github\.com(?::\d+)?[:/]([^/\s]+/[^/\s]+?)(?:\.git)?/?$", url)
    if not m or not shutil.which("gh"):
        return "unknown"
    try:
        r = subprocess.run(["gh", "repo", "view", m.group(1), "--json", "isPrivate",
                            "-q", ".isPrivate"], capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    out = r.stdout.strip().lower()
    if r.returncode or out not in {"true", "false"}:
        return "unknown"
    return "private" if out == "true" else "public"


def _cmd_sync(args) -> int:
    """Write-back usage → git snapshot → pull/push (if remote) → reindex.

    The wiki IS the sync unit (ADR-001/ADR-008): markdown carries everything,
    the DB is rebuilt after pull. servings stay device-local by design."""
    import socket
    import subprocess

    from . import config as config_mod
    from . import cards as cards_mod
    from . import consolidate, embed, index_db, ui

    cfg = config_mod.load()
    if not cfg.wiki_root.exists():
        print(f"wiki root missing: {cfg.wiki_root}", file=sys.stderr)
        return 1

    def git(*a: str):
        return subprocess.run(["git", "-C", str(cfg.wiki_root), *a],
                              capture_output=True, text=True, stdin=subprocess.DEVNULL)

    if cfg.db_path.exists():
        conn = index_db.connect(cfg.db_path)
        n = consolidate.write_back_usage(conn, cfg.wiki_root)
        queued = ui.enqueue_unverified(conn, cfg.wiki_root)
        conn.close()
        print(f"usage write-back: {n} cards updated; {queued} legacy reviews queued")
    sanitized = 0
    for path, card in list(cards_mod.iter_cards(cfg.wiki_root)):
        before = path.read_text(encoding="utf-8")
        cards_mod.sanitize(card)
        if len(card.sources) > 16:
            card.sources = card.sources[:8] + card.sources[-8:]
        after = cards_mod.render(card)
        if after != before:
            path.write_text(after, encoding="utf-8")
            sanitized += 1
    print(f"privacy normalization: {sanitized} cards rewritten")
    if not (cfg.wiki_root / ".git").exists():
        git("init")
        print(f"initialized git repo at {cfg.wiki_root}")
    git("add", "-A")
    # hostname = device id in the sync history — `git log` shows which device
    # produced each snapshot when multiple machines share the remote
    git("commit", "-m", f"afterwit sync @{socket.gethostname()}")  # no-op when clean
    remotes = git("remote").stdout.split()
    if remotes:
        # The wiki is distilled from private transcripts. Pushing it to a public
        # repo publishes the user's work, their employer's code paths, and
        # whatever a redaction pattern missed — irreversibly, to a crawled index.
        # Refuse when we can PROVE the remote is public (see _remote_visibility).
        # Check the PUSH url specifically (a remote can have a separate pushurl),
        # and later push to THIS remote by name — bare `git push` can resolve to
        # a different remote via pushDefault/branch.pushRemote (audit claim 8).
        target = remotes[0]
        url = (git("remote", "get-url", "--push", target).stdout.strip()
               or git("remote", "get-url", target).stdout.strip())
        vis = _remote_visibility(url)
        if vis == "public" and not cfg.allow_public_wiki_remote:
            print(f"REFUSING TO PUSH: {url} is a PUBLIC repository.\n"
                  "  Your knowledge wiki is distilled from your private coding sessions.\n"
                  "  Move it to a private repo:  gh repo create <name> --private\n"
                  "  If this is genuinely intended, set allow_public_wiki_remote = true "
                  "in ~/.afterwit/config.toml", file=sys.stderr)
            return 1
        if vis == "unknown":
            print(f"note: could not verify {url} is private — check it yourself")
        r = git("pull", "--rebase")
        if r.returncode:
            # Never leave a half-rebased wiki: the next run's blind `git add -A`
            # would commit conflict markers into cards, and they'd be indexed and
            # served as knowledge. Abort ONLY if a rebase actually started — a
            # pull can fail with no rebase in progress (no upstream, network
            # down, unrelated histories), and a blind `--abort` would fail too
            # and mask the real error (ADR-020).
            in_rebase = any(
                git("rev-parse", "--git-path", d).stdout.strip()
                and (cfg.wiki_root / git("rev-parse", "--git-path", d).stdout.strip()).exists()
                for d in ("rebase-merge", "rebase-apply")
            )
            if in_rebase:
                git("rebase", "--abort")
            state = "rebase aborted" if in_rebase else "no rebase was in progress"
            print(f"git pull --rebase failed ({state}; local snapshot kept):\n"
                  f"{r.stderr.strip()}", file=sys.stderr)
            return 1
        r = git("push", target)  # the remote we verified, not push.default's pick
        if r.returncode:
            print(f"git push failed:\n{r.stderr.strip()}", file=sys.stderr)
            return 1
        print("pulled + pushed")
    else:
        print("no remote — local snapshot only. Add one: "
              f"git -C {cfg.wiki_root} remote add origin <url>")
    conn = index_db.connect(cfg.db_path)
    n = index_db.rebuild(conn, cfg.wiki_root)
    restored = ui.restore_queue_from_wiki(conn, cfg.wiki_root)
    stale = _restale(conn, cfg)  # rebuild zeroed `stale`; recompute (ADR-020 D1)
    vectors = embed.reindex(conn)
    conn.close()
    print(f"reindexed {n} cards, {vectors} vectors, {restored} reviews restored "
          f"({stale} drift-flagged)")
    return 0


def _cmd_review(args) -> int:
    """Drain the review queue with the independent auto-reviewer (ADR-021)."""
    from . import config as config_mod
    from . import index_db, review

    cfg = config_mod.load()
    if not cfg.auto_review and not args.dry_run:
        print("auto-review is off. Set `auto_review = true` in "
              "~/.afterwit/config.toml, or preview with --dry-run.",
              file=sys.stderr)
        return 1
    conn = index_db.connect(cfg.db_path)
    review.ui.enqueue_unverified(conn, cfg.wiki_root)
    pending = conn.execute("SELECT COUNT(*) FROM review_queue").fetchone()[0]
    if not pending:
        print("review queue is empty")
        return 0
    print(f"{'previewing' if args.dry_run else 'reviewing'} {min(pending, args.limit or pending)}"
          f" of {pending} queued cards with {review.reviewer_name(cfg)}...")

    def show(rowid, card, v):
        mark = {"approve": "+", "reject": "-", "abstain": "?"}[v.verdict]
        print(f" {mark} [{card.type}] {card.title[:60]}\n     {v.reason}")

    counts = review.review_queue(cfg, conn, limit=args.limit,
                                 dry_run=args.dry_run, on_verdict=show)
    conn.close()
    print(f"\n{counts}" + ("  (dry run — nothing changed)" if args.dry_run else ""))
    print("abstained cards stay queued for you: afterwit ui" if counts["abstained"] else "")
    return 0


def _cmd_stats(args) -> int:
    import json
    from . import config as config_mod, index_db

    cfg = config_mod.load()
    conn = index_db.connect(cfg.db_path, readonly=True)
    out = {
        "cards": dict(conn.execute(
            "SELECT status, COUNT(*) FROM cards GROUP BY status"
        ).fetchall()),
        "types": dict(conn.execute(
            "SELECT type, COUNT(*) FROM cards WHERE status='active' GROUP BY type"
        ).fetchall()),
        "pending_review": conn.execute("SELECT COUNT(*) FROM review_queue").fetchone()[0],
        "servings": conn.execute("SELECT COUNT(*) FROM servings").fetchone()[0],
        "distilled": _distill_ledger_stats(conn, cfg),
    }
    conn.close()
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


def _distill_ledger_stats(conn, cfg) -> dict:
    """What the LLM has already been paid for, and what a run would still cost.

    The ledger (ADR-015) is what stops a nightly re-driving a session it already
    distilled — including the ones that yielded ZERO cards, which are the majority and
    would otherwise be re-bought every night forever. It was doing its job silently;
    this just makes the spend legible.
    """
    from .adapters import _iter_paths

    try:
        row = conn.execute(
            "SELECT COUNT(*) n, SUM(cards) c, MAX(ts) last, "
            "SUM(CASE WHEN cards=0 THEN 1 ELSE 0 END) barren FROM distilled"
        ).fetchone()
    except sqlite3.Error:
        return {"sessions": 0, "note": "no ledger yet — nothing has been distilled"}

    done = {r[0] for r in conn.execute("SELECT source FROM distilled")}
    on_disk = [p for src in ("claude", "codex") for p in _iter_paths(src, cfg.projects_root)]
    remaining = [p for p in on_disk if str(p) not in done]
    return {
        "sessions": row["n"] or 0,
        "cards_produced": row["c"] or 0,
        "zero_card_sessions": row["barren"] or 0,   # recorded, so never re-driven
        "last_distilled": row["last"],
        "transcripts_on_disk": len(on_disk),
        "not_yet_distilled": len(remaining),        # what the next run would spend on
    }


def _cmd_lint(args) -> int:
    import json
    from . import config as config_mod, consolidate, index_db

    cfg = config_mod.load()
    conn = index_db.connect(cfg.db_path)
    findings = consolidate.lint(conn, projects_root=cfg.projects_root,
                            aliases=cfg.project_aliases)
    conn.close()
    print(json.dumps(findings, indent=2, sort_keys=True))
    return 1 if any(findings.values()) else 0


def _cmd_relink(args) -> int:
    from . import config as config_mod, distill, index_db, relink

    cfg = config_mod.load()
    conn = index_db.connect(cfg.db_path)
    try:
        if args.strip:
            print(f"stripped related links from {relink.strip(conn, cfg.wiki_root)} cards")
            return 0
        driver = distill.make_driver(args.driver or cfg.distill_driver,
                                     model=cfg.distill_model, effort=cfg.distill_effort)
        out = relink.relink(conn, driver, budget=args.limit, dry_run=args.dry_run)
        for p in out.pop("proposals", []):
            title = conn.execute("SELECT title FROM cards WHERE id=?",
                                 (p["id"],)).fetchone()["title"]
            print(f"{p['id']}  {title}")
            for kid in p["kept"]:
                krow = conn.execute("SELECT title FROM cards WHERE id=?", (kid,)).fetchone()
                print(f"    -> {kid}  {krow['title'] if krow else '?'}")
            if not p["kept"]:
                print(f"    (rejected all {len(p['candidates'])} candidates)")
        print(out)
        return 0
    finally:
        conn.close()


def _toml_str(line: str) -> str:
    """The value of a `key = "..."` TOML line, unescaped.

    A TOML basic string and a JSON string share their escape rules, and install
    writes these with `json.dumps` — so json.loads round-trips exactly where
    `.strip('"')` silently leaves `\\\\` doubled."""
    import json
    try:
        return str(json.loads(line.split("= ", 1)[1].strip()))
    except (IndexError, ValueError):
        return ""


def _cmd_doctor(args) -> int:
    """Verify the whole path an agent actually travels to reach afterwit.

    Both real outages were SILENT config faults, not data faults: the MCP server
    stayed registered under a dead name (`harness_helper` → removed `hh serve-mcp`,
    spawn-failed every session) while the skills shelled a bare `aw` that was never
    on PATH. Both doors locked; the DB sat healthy and unreachable with 463 cards.
    Nothing could answer "is afterwit reachable?" until an agent failed and said so.
    That is what this answers. Exits nonzero if any door is shut.
    """
    import json
    import shutil
    import sqlite3
    import subprocess
    from pathlib import Path

    from . import config as config_mod
    from . import distill, harness, index_db, install

    fails: list[str] = []

    def chk(ok: bool, label: str, detail: str = "", fix: str = "") -> None:
        """detail = what we observed (shown always). fix = the remedy (shown ONLY when
        broken — printing "ok … run: afterwit install claude" reads as an instruction
        and sends people to re-run installs that were already fine)."""
        note = detail if ok else " — ".join(x for x in (detail, fix) if x)
        print(f"{'ok  ' if ok else 'FAIL'}  {label}" + (f" — {note}" if note else ""))
        if not ok:
            fails.append(label)

    cfg = config_mod.load()
    repo = install._repo_root()

    if getattr(args, "scan_secrets", False) and cfg.wiki_root.is_dir():
        from . import redact
        leaked = [str(p) for p in cfg.wiki_root.rglob("*.md")
                  if redact.contains_raw_secret(p.read_text(encoding="utf-8", errors="replace"))]
        chk(not leaked, "wiki secret scan", f"{len(leaked)} files",
            "run afterwit sync to normalize cards, then rotate any exposed credential")

    # ---- data: is there anything to serve?
    chk(cfg.wiki_root.is_dir(), "wiki", str(cfg.wiki_root), "run: afterwit init")
    if not cfg.db_path.exists():
        chk(False, "index db", f"missing {cfg.db_path}", "run: afterwit index --rebuild")
    else:
        try:
            c = index_db.connect(cfg.db_path, readonly=True)
            integrity = c.execute("pragma integrity_check").fetchone()[0]
            total = c.execute("select count(*) from cards").fetchone()[0]
            active = c.execute("select count(*) from cards where status='active'").fetchone()[0]
            fts = c.execute("select count(*) from cards_fts").fetchone()[0]
            chk(integrity == "ok", "db integrity", integrity, "restore from the wiki: afterwit index --rebuild")
            chk(active > 0, "active cards", str(active), "nothing to serve")
            # A drifted FTS returns zero hits for cards that plainly exist — the
            # failure that most looks like "the db is down" while integrity says ok.
            chk(fts == total, "fts in sync", f"{fts}/{total} rows",
                "run: afterwit index --rebuild")
            has_vectors = c.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='vectors'"
            ).fetchone()
            vector_count = (c.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]
                            if has_vectors else 0)
            chk(vector_count == active, "embedding coverage",
                f"{vector_count}/{active} active cards",
                "run: afterwit sync")
            c.close()
        except sqlite3.Error as e:
            chk(False, "index db", str(e), "run: afterwit index --rebuild")

    # ---- claude wiring. Paths via `harness`, so a CLAUDE_CONFIG_DIR/CODEX_HOME
    # install is checked where it actually lives instead of reported as missing.
    mcp = install._load_json(harness.claude_json_path()).get("mcpServers", {})
    chk(install.MCP_NAME in mcp, "claude mcp entry", "", "run: afterwit install claude")

    # ---- relocation. A checkout install bakes an absolute repo path into the mcp
    # args, the hook command AND the systemd unit. Move or rename the folder and all
    # three die at once — including the hook that would have warned you, because it
    # is itself referenced by the dead path. Nothing inside afterwit can self-heal
    # that, so the only defence is to notice. No new state file needed: the
    # registered argv still carries the OLD path, and _repo_root() knows the new one.
    # (A packaged install has no --project at all and is immune — see doctor --fix.)
    argv_reg = mcp.get(install.MCP_NAME, {}).get("args", [])
    if "--project" in argv_reg:
        baked = Path(argv_reg[argv_reg.index("--project") + 1])
        chk(baked == repo, "registered repo path is current",
            str(repo) if baked == repo else f"registered {baked}",
            f"the checkout moved to {repo} — run: afterwit doctor --fix")
    chk(install._LEGACY_MCP_NAME not in mcp, "no dead legacy mcp entry",
        "", f"a stale `{install._LEGACY_MCP_NAME}` server invokes a removed command and "
            "spawn-fails every session, leaving agents with no afterwit tools — "
            "run: afterwit install claude")
    settings = install._load_json(harness.settings_path("claude"))
    hooks_json = json.dumps(settings.get("hooks", {}))
    chk("SessionStart" in settings.get("hooks", {}) and "--mode session" in hooks_json,
        "claude session hook",
        "", "run: afterwit install claude")
    chk("UserPromptSubmit" in settings.get("hooks", {}) and "--mode prompt" in hooks_json,
        "claude prompt hook", "", "run: afterwit install claude")

    # Registered is not the same as runnable. Every check above reads config; the
    # hook, uniquely, is stored as a SHELL STRING that Claude Code hands to bash —
    # on Windows too. A path quoted for cmd.exe reaches bash as escape sequences
    # (`C:UsersE112323scoop...: command not found`) and the hook dies on every
    # prompt while this very check printed "ok". Run the string the way the harness
    # runs it, and let a nonzero exit say so. inject fails open, so a nonzero here
    # means the SHELL could not even spawn it.
    hook_cmd = next((h.get("command", "")
                     for group in settings.get("hooks", {}).get("UserPromptSubmit", [])
                     for h in group.get("hooks", [])
                     if install._is_afterwit_inject(h.get("command", ""), "prompt")), "")
    bash = shutil.which("bash")

    def _spawn_hook(cmd: str, harness_name: str) -> None:
        """Run a stored hook string the way its harness runs it."""
        if not (cmd and bash):
            return
        label = f"{harness_name} prompt hook actually runs (via bash, as the harness runs it)"
        try:
            r = subprocess.run(
                [bash, "-c", cmd],
                input=json.dumps({"prompt": "afterwit doctor smoke test", "cwd": str(repo)}),
                # The hook prints CARD TEXT, which is full of em dashes. Decoding
                # that as cp1252 makes this check fail on the content it exists to
                # prove works (Gotcha #75).
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=180)
            err = (r.stderr.strip().splitlines() or [f"exit {r.returncode}"])[-1][:160]
            chk(r.returncode == 0, label, "" if r.returncode == 0 else err,
                "the registered hook command does not spawn — "
                f"run: afterwit install {harness_name}")
        except (OSError, subprocess.TimeoutExpired) as e:
            chk(False, label, str(e), f"run: afterwit install {harness_name}")

    _spawn_hook(hook_cmd, "claude")

    try:
        driver_path = distill.preflight_driver(cfg.distill_driver)
        # Name the model that will actually run: unset config means "inherit the
        # harness's own", and "which model is this spending?" should not require
        # reading three files to answer.
        model = (cfg.distill_model
                 or harness.default_model(harness.harness_of(cfg.distill_driver))
                 or "driver default")
        chk(True, "distill driver executable", f"{driver_path} · model {model}")
    except RuntimeError as e:
        chk(False, "distill driver executable", str(e),
            "set AFTERWIT_CODEX_BIN/AFTERWIT_CLAUDE_BIN to an absolute executable")

    # The exact fault that killed the reader-app agent. Do NOT gate this on
    # shutil.which("aw"): doctor normally runs under `uv run`, whose PATH carries
    # .venv/bin, so `aw` resolves for US and not for the plain shell an agent shells
    # out from — that check reports a false green precisely when it matters. A
    # correct install rewrites the placeholder via install._skillify in EVERY install
    # mode, so the placeholder surviving means the install is stale. Full stop.
    stale = 'skill still shells bare `aw`, which is not on an agent\'s PATH'
    skill = harness.skills_dir("claude") / "aw-knowledge" / "SKILL.md"
    if not skill.exists():
        chk(False, "claude skills", "", "run: afterwit install claude")
    else:
        chk('AW="aw"' not in skill.read_text(encoding="utf-8"),
            "skill AW prefix resolved", "", f"{stale} — run: afterwit install claude")

    # ---- codex wiring
    def _read(p: Path) -> str:
        try:
            return p.read_text(encoding="utf-8")
        except OSError:
            return ""

    ct = _read(harness.settings_path("codex"))
    at = _read(harness.agents_path("codex"))
    chk(f"[mcp_servers.{install.MCP_NAME}]" in ct, "codex mcp entry",
        "", "run: afterwit install codex")
    chk(install.MD_BEGIN in at, "codex agent block", "", "run: afterwit install codex")
    chk('AW="aw"' not in at, "codex AW prefix resolved", "",
        f"codex {stale} — run: afterwit install codex")
    registered = all(f"[[hooks.{e}]]" in ct and f"--mode {m} --harness codex" in ct
                     for e, m in install._CODEX_HOOK_MODES)
    for event, mode in install._CODEX_HOOK_MODES:
        chk(f"[[hooks.{event}]]" in ct and f"--mode {mode} --harness codex" in ct,
            f"codex {mode} hook", "", "run: afterwit install codex")
    # Registered is not the same as running. Codex skips an untrusted hook with no
    # warning at all, and rewriting a hook re-breaks its trust (Gotcha #69) — so
    # ask Codex itself, which is the only thing that knows. Skipped entirely when
    # the hooks are not registered: the two checks above already said so, and a
    # second failure line about a hook that does not exist is noise, not signal.
    # Same lesson as the Claude hook above, and the one that bit hardest: the
    # Codex command carries flags Claude's does not (`--harness codex`), so a
    # green Claude spawn proves nothing about it. Shipping that flag without
    # teaching the CLI to accept it made every Codex prompt exit 2 — Blocked —
    # while every config-reading check still said ok (Gotcha #71).
    # json.loads, not .strip('"'): install writes this value with json.dumps, so a
    # Windows path arrives escaped (`C:\\Users\\...`). Stripping the quotes leaves
    # the backslashes DOUBLED and spawns a string that is not the one on disk —
    # in the one check whose entire purpose is running exactly what is on disk.
    # It happens to survive (Windows collapses duplicate separators), which is
    # what makes it worth fixing: it would keep passing while being wrong.
    _spawn_hook(next((_toml_str(ln) for ln in ct.splitlines()
                      if ln.startswith("command = ")
                      and install._is_afterwit_inject(ln, "prompt")), ""), "codex")

    cfg_path = harness.settings_path("codex")
    hooks = install._codex_hooks_list(cfg_path, cfg_path.parent) if registered else None
    if hooks is None:
        if registered:
            chk("trusted_hash" in ct, "codex hook trust (codex not reachable to confirm)",
                "", "could not run `codex app-server` — trust state unread")
    else:
        ours = [h for h in hooks
                if h.get("sourcePath") == str(cfg_path)
                and install._CODEX_EVENT_MODE.get(str(h.get("eventName") or ""))]
        bad = [h for h in ours if h.get("trustStatus") != "trusted"]
        chk(len(ours) == len(install._CODEX_HOOK_MODES) and not bad, "codex hooks trusted",
            f"{len(ours)} hook(s), all trusted" if not bad else "",
            "; ".join(f"{h.get('eventName')}={h.get('trustStatus')}" for h in bad)
            + " — codex silently skips these; run: afterwit install codex")

    # ---- the one that matters: actually walk the path an agent shells.
    # Every check above only READS config. This one proves the command spawns and
    # exits 0. "The config looks right" and "the command runs" are different claims,
    # and every outage so far lived in the gap between them.
    argv = install._server_argv("recall", repo) + ["afterwit doctor smoke test", "-k", "1"]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=180, stdin=subprocess.DEVNULL)
        err = r.stderr.strip().splitlines()[-1][:160] if r.stderr.strip() else f"exit {r.returncode}"
        chk(r.returncode == 0, "cli reachable (what agents shell out to)",
            install._join(argv) if r.returncode == 0 else err,
            "agents that fall back from MCP to the CLI will get nothing")
    except (OSError, subprocess.TimeoutExpired) as e:
        chk(False, "cli reachable (what agents shell out to)", str(e),
            "agents that fall back from MCP to the CLI will get nothing")

    print()
    if not fails:
        print("all good — agents can reach afterwit via MCP, hook, skills and CLI")
        return 0

    print(f"{len(fails)} broken: " + ", ".join(fails))
    if not getattr(args, "fix", False):
        print("re-point every baked path at this checkout with: afterwit doctor --fix")
        return 1

    # The installers rewrite each surface from _repo_root(), i.e. from wherever
    # afterwit is running RIGHT NOW — so running this from the moved checkout is
    # what repairs a rename. Idempotent, and it is the only recovery available:
    # a relocated checkout cannot warn you through its own (dead) hook.
    print("re-running installers from", repo)
    for target in ("claude", "codex", "cron"):
        try:
            install.main(target)
        except Exception as e:  # noqa: BLE001 — one target failing must not block the others
            print(f"  {target}: FAILED — {e!r}", file=sys.stderr)
    print("\nre-check with: afterwit doctor   (restart Claude Code to reload the MCP server)")
    return 1


def _cmd_init(args) -> int:
    """One-command setup for a fresh machine. Idempotent — safe to re-run."""
    import os
    import shutil
    import subprocess
    from pathlib import Path

    from . import config as config_mod

    def ask(q: str) -> bool:
        if args.yes:
            return True
        return (input(f"{q} [Y/n] ").strip().lower() or "y").startswith("y")

    home = Path.home()
    cfg_path = Path(os.environ.get("AFTERWIT_CONFIG") or home / ".afterwit" / "config.toml")

    # An existing config WINS over the candidate scan. Without this, init told you to
    # set projects_root and then bailed on the same line when you had — the error was
    # a dead end you could not follow.
    existing = config_mod.load(cfg_path) if cfg_path.exists() else None
    candidates = [home / "Desktop" / "Projects", home / "Projects", home / "projects",
                  home / "code", home / "src", home / "dev",
                  # OneDrive redirects Desktop on managed Windows machines, so
                  # home/Desktop does not exist at all there.
                  *sorted(home.glob("OneDrive*/Desktop/Projects"))]
    projects = (existing.projects_root if existing and existing.projects_root.is_dir()
                else next((p for p in candidates if p.is_dir()), None))
    if projects is None:
        print("could not find your projects folder. Set projects_root in "
              f"{cfg_path} and re-run.", file=sys.stderr)
        return 1
    wiki = existing.wiki_root if existing else home / "knowledge"
    print(f"projects: {projects}\nwiki:     {wiki}\nconfig:   {cfg_path}\n")

    wiki.mkdir(parents=True, exist_ok=True)
    gi = wiki / ".gitignore"
    if not gi.exists():  # derived files must never be synced (ADR-019)
        gi.write_text("index.md\nprojects/*/brief.md\nlog.md\n", encoding="utf-8")
    if not (wiki / ".git").exists():
        subprocess.run(["git", "init", "-q", str(wiki)], check=False, stdin=subprocess.DEVNULL)

    if not cfg_path.exists():
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        # _toml_value, not an f-string: a Windows path is full of backslashes and
        # `C:\Users\...` interpolated raw into a TOML basic string makes `\U` an
        # escape — tomllib then rejects the config init just wrote, so afterwit
        # could not read its own first-run output on Windows at all.
        cfg_path.write_text(
            f"projects_root = {config_mod._toml_value(projects)}\n"
            f"wiki_root = {config_mod._toml_value(wiki)}\n\n"
            "# An independent model clears cards for serving. Off by default;\n"
            "# turn it on if you will not review the queue yourself (ADR-021).\n"
            "auto_review = false\n", encoding="utf-8")
        print(f"wrote {cfg_path}")

    has_remote = bool(subprocess.run(["git", "-C", str(wiki), "remote"],
                                     capture_output=True, text=True, stdin=subprocess.DEVNULL).stdout.strip())
    if not has_remote and shutil.which("gh"):
        name = args.repo or "afterwit-knowledge"
        # DEVICE TWO. The wiki repo usually already exists — this is the second
        # machine, and the whole "follows you between machines" promise is this
        # branch. `gh repo create` fails outright on a name already taken, and init
        # used to shrug that off and carry on with an EMPTY local wiki while every
        # card sat on GitHub. Adopt before create.
        view = subprocess.run(["gh", "repo", "view", name, "--json", "url,isPrivate",
                               "-q", ".url + \" \" + (.isPrivate|tostring)"],
                              capture_output=True, text=True, stdin=subprocess.DEVNULL)
        remote_url, _, is_private = view.stdout.strip().partition(" ")
        # Only into a wiki with no history of its own: `checkout -f` discards local
        # edits, and a wiki with commits is one this device has already written to.
        virgin = subprocess.run(["git", "-C", str(wiki), "rev-parse", "--verify", "HEAD"],
                                capture_output=True, text=True, stdin=subprocess.DEVNULL).returncode != 0
        if view.returncode == 0 and remote_url and virgin:
            if is_private != "true":
                print(f"WARNING: {name} is PUBLIC. Your wiki is distilled from your own\n"
                      "         sessions — make it private before the first sync.",
                      file=sys.stderr)
            subprocess.run(["git", "-C", str(wiki), "remote", "add", "origin", remote_url],
                           check=False, capture_output=True, stdin=subprocess.DEVNULL)
            subprocess.run(["git", "-C", str(wiki), "fetch", "-q", "origin"], check=False, stdin=subprocess.DEVNULL)
            head = subprocess.run(["git", "-C", str(wiki), "symbolic-ref",
                                   "--short", "refs/remotes/origin/HEAD"],
                                  capture_output=True, text=True, stdin=subprocess.DEVNULL).stdout.strip()
            branch = head.split("/")[-1] if head else "main"
            r = subprocess.run(["git", "-C", str(wiki), "checkout", "-f", "-B", branch,
                                f"origin/{branch}"], capture_output=True, text=True, stdin=subprocess.DEVNULL)
            print(f"adopted existing wiki {name} ({branch})" if not r.returncode
                  else f"git: {r.stderr.strip()}")
        elif view.returncode != 0 and ask(
                "Create a PRIVATE GitHub repo to sync your knowledge across devices?"):
            # --private is not optional: this wiki holds your mined session history.
            r = subprocess.run(["gh", "repo", "create", name, "--private",
                                "--source", str(wiki), "--remote", "origin"],
                               capture_output=True, text=True, stdin=subprocess.DEVNULL)
            print(f"created private repo {name}" if not r.returncode
                  else f"gh: {r.stderr.strip()}")

    # Wire the harnesses so `afterwit init` really is one command: MCP server +
    # hooks + skills for both, and the nightly schedule. Each installer is
    # backup-first and idempotent (safe to re-run). `--no-install` stops here for
    # anyone who wants to inspect config before touching their harness setup.
    if args.no_install:
        print("\nskipped harness wiring (--no-install). Run when ready:\n"
              "  afterwit install claude\n  afterwit install codex\n  afterwit install cron")
        return 0
    from . import install

    # 1. Put `aw` on PATH. The skills we are about to copy shell `$AW`, and a git
    #    checkout has NO console script — that is the `aw: command not found` every
    #    clone-and-run user hits. A packaged install already has it; skip then.
    #    Best-effort: a failure here must never abort the install.
    if not (shutil.which("aw") or shutil.which("afterwit")):
        if shutil.which("uv"):
            print("\ninstalling the `aw` command on PATH...")
            r = subprocess.run(["uv", "tool", "install", "--editable", str(install._repo_root())],
                               capture_output=True, text=True, stdin=subprocess.DEVNULL)
            print("  aw: ok" if not r.returncode
                  else f"  aw: skipped ({r.stderr.strip().splitlines()[-1][:110] if r.stderr.strip() else r.returncode})")
        else:
            print("\n  note: `uv` not found, so the `aw` command is not on PATH.\n"
                  "        Skills fall back to a full-path invocation; install with:\n"
                  "        pip install afterwit", file=sys.stderr)

    print("\nwiring harnesses (MCP + hooks + skills) and the nightly run...")
    for target, label in (("claude", "Claude Code"), ("codex", "Codex"), ("cron", "nightly")):
        try:
            res = install.main(target)
            print(f"  {label}: {'ok' if res == 0 else 'see output above'}")
        except Exception as e:  # noqa: BLE001 — one harness absent must not abort the rest
            print(f"  {label}: skipped ({e!r})", file=sys.stderr)

    # 2. Build the index. Without it `recall` answers "no index yet" and every agent
    #    concludes the knowledge base is EMPTY rather than un-built — the same
    #    unreachable-vs-empty confusion behind every outage this system has had.
    print("\nbuilding the index...")
    try:
        _cmd_index(argparse.Namespace())
    except Exception as e:  # noqa: BLE001
        print(f"  index: FAILED {e!r}", file=sys.stderr)

    cfg = config_mod.load(cfg_path)
    if cfg.wiki_root != wiki:
        print(f"warning: config wiki_root is {cfg.wiki_root}, not {wiki}", file=sys.stderr)

    # 3. Prove it. Every failure this project has shipped was an install that
    #    REPORTED success and left agents unable to reach a healthy index. An
    #    installer that cannot verify its own work is how that happens, so init
    #    ends by walking the path an agent walks, and exits nonzero if any door is
    #    shut. "done" now means checked, not attempted.
    print("\nverifying that agents can actually reach afterwit...\n")
    rc = _cmd_doctor(argparse.Namespace(fix=False))
    if rc == 0:
        print("Next:\n"
              "  afterwit ui                # review queue at http://127.0.0.1:8377\n"
              "  afterwit run               # force a nightly distill now (optional)\n"
              "  restart Claude Code/Codex  # so they load the new MCP server\n")
    return rc


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    # A Windows console is cp1252, and our own --help text contains an arrow: printing
    # it raised UnicodeEncodeError before argparse ever returned. `afterwit --help`
    # crashing is the first thing a new user runs.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    # The hook path never touches argparse. inject.py's whole contract is FAIL
    # OPEN — empty stdout, exit 0 — but argparse enforces its own contract first
    # and exits 2 on anything it does not recognise, before a line of inject.py
    # runs. Exit 2 from a prompt hook is not a no-op: Claude Code erases the
    # user's prompt (Gotcha #5) and Codex reports the turn Blocked. Adding
    # `--harness codex` to the installed command hit exactly that on 2026-07-27
    # (Gotcha #71). inject.py parses its own flags and tolerates unknown ones.
    if argv and argv[0] == "inject":
        from . import inject
        return inject.main(argv[1:])

    p = argparse.ArgumentParser(prog="afterwit")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_inj = sub.add_parser("inject", help="hook entrypoint (reads hook JSON on stdin)")
    p_inj.add_argument("--mode", choices=["prompt", "session", "error"], default="prompt")
    p_inj.add_argument("--harness", choices=["claude", "codex"], default="claude")

    p_rec = sub.add_parser("recall", help="search knowledge from the terminal")
    p_rec.add_argument("query")
    p_rec.add_argument("-p", "--project", default=None)
    p_rec.add_argument("-k", type=int, default=5)
    p_rec.add_argument("-v", "--verbose", action="store_true")
    p_rec.add_argument("--all", action="store_true", help="ignore relevance floor")

    p_idx = sub.add_parser("index", help="(re)build SQLite index from the wiki")
    p_idx.add_argument("--rebuild", action="store_true")

    p_ui = sub.add_parser("ui", help="local review/search/health UI (127.0.0.1)")
    p_ui.add_argument("--port", type=int, default=8377)

    sub.add_parser("sync", help="sync wiki across devices (git) after usage write-back")
    sub.add_parser("queue", help="propose a card (JSON on stdin) into the review queue")

    p_ing = sub.add_parser("ingest", help="parse sources into events (checkpointed)")
    p_ing.add_argument("--source", choices=["claude", "codex", "memory", "docs", "db", "all"], required=True)
    p_ing.add_argument("--limit", type=int, default=None)

    p_dis = sub.add_parser("distill", help="LLM-extract knowledge cards from sessions")
    p_dis.add_argument("--budget", type=int, default=None)
    p_dis.add_argument("--source", default="claude")
    # default MUST stay None (Gotcha #22, again): a flag that shadows a config key and
    # is forwarded unconditionally makes the config key dead. `--driver claude-p` was
    # sent on every run, so `distill_driver = "codex"` never took effect here.
    p_dis.add_argument("--driver", default=None)
    p_dis.add_argument("--model", default=None, help="default: config distill_model")
    p_dis.add_argument("--effort", default=None, help="codex only; default: config distill_effort")
    p_dis.add_argument("--project", default=None)

    sub.add_parser("serve-mcp", help="stdio MCP server (registered in both harnesses)")

    p_run = sub.add_parser("run", help="nightly runner: ingest→distill→mine→decay→… (ADR-015)")
    p_run.add_argument("--budget", type=int, default=None, help="max sessions driven this run")
    # default MUST stay None: `runner.run` falls back to cfg.distill_driver only
    # when this is None, and the systemd unit passes no --driver. A default here
    # silently overrode every user's configured driver.
    p_run.add_argument("--driver", choices=["claude-p", "codex"], default=None)
    p_run.add_argument("--timeout", type=int, default=50, help="overall soft cap in minutes")
    p_run.add_argument("--remine", action="store_true",
                       help="re-judge every past serving from scratch (after a miner change)")

    p_rev = sub.add_parser("review", help="auto-review the queue with an independent model (ADR-021)")
    p_rev.add_argument("--limit", type=int, default=None, help="review at most N cards")
    p_rev.add_argument("--dry-run", action="store_true",
                       help="print verdicts, change nothing (works even with auto_review off)")

    p_ini = sub.add_parser("init", help="one-command setup: config, wiki, sync repo, harness wiring")
    p_ini.add_argument("--yes", action="store_true", help="non-interactive; accept defaults")
    p_ini.add_argument("--repo", default=None, help="name for the private knowledge repo")
    p_ini.add_argument("--no-install", action="store_true",
                       help="set up config/wiki only; don't touch harness config or the scheduler")

    p_ins = sub.add_parser("install", help="register afterwit in Claude Code / Codex, or schedule the nightly run")
    p_ins.add_argument("harness", choices=["claude", "codex", "cron"])

    p_ev = sub.add_parser("eval", help="golden-set retrieval eval (SPEC §12)")
    p_ev.add_argument("--golden", default=None, help="path to golden.yaml (default: eval/golden.yaml)")

    p_doc = sub.add_parser("doctor",
                           help="can agents actually reach afterwit? checks db, mcp, hook, skills, cli")
    p_doc.add_argument("--fix", action="store_true",
                       help="re-run the installers, re-pointing every baked path at this checkout "
                            "(the recovery path after moving or renaming the folder)")
    p_doc.add_argument("--scan-secrets", action="store_true",
                       help="scan synced markdown for raw credential patterns")

    sub.add_parser("stats", help="cards, review queue, and serving digest")
    sub.add_parser("lint", help="report broken links, stale knowledge, and drift")
    p_rel = sub.add_parser("relink",
                           help="curated card links: kNN candidates + LLM judge (ADR-045)")
    p_rel.add_argument("--limit", type=int, default=10, help="cards to judge this run")
    p_rel.add_argument("--dry-run", action="store_true",
                       help="print proposals for hand-judging; write nothing, memo nothing")
    p_rel.add_argument("--strip", action="store_true",
                       help="remove every auto-written related link and reset the memo")
    p_rel.add_argument("--driver", default=None,
                       help="LLM driver for the judge (default: config distill_driver)")

    args = p.parse_args(argv)
    try:
        return _dispatch(args)
    except IndexUnavailable as e:
        # An agent shells `aw recall` and gets a raw Python traceback ending in
        # `sqlite3.OperationalError: unable to open database file`. It has no path,
        # no cause, no remedy — so the agent reports "the knowledge base is
        # unavailable" and reasons on without it. Same silent-wrong-answer class we
        # closed in mcp_server.dispatch; the CLI is the other door agents come
        # through, and it was still handing out tracebacks.
        print(f"\n{e}\n", file=sys.stderr)
        return 1
    except sqlite3.Error as e:
        # sqlite can also blow up AFTER a successful open — a WAL database on a
        # read-only filesystem fails at the first QUERY, not at connect(). Guarding
        # only the open is what let codex keep tracebacking through _dispatch.
        # Catch the whole command, not one call.
        from . import config as config_mod
        from .index_db import _diagnose
        print(f"\n{_diagnose(config_mod.load().db_path, e)}\n", file=sys.stderr)
        return 1


def _dispatch(args) -> int:
    if args.cmd == "inject":  # unreachable via main(); kept for direct-call callers
        from . import inject
        return inject.main(["--mode", args.mode, "--harness", args.harness])
    if args.cmd == "recall":
        return _cmd_recall(args)
    if args.cmd == "index":
        return _cmd_index(args)
    if args.cmd == "ui":
        from . import ui
        return ui.main(args.port)
    if args.cmd == "sync":
        return _cmd_sync(args)
    if args.cmd == "queue":
        return _cmd_queue(args)
    if args.cmd == "ingest":
        from . import adapters
        return adapters.main(["--source", args.source]
                             + (["--limit", str(args.limit)] if args.limit else []))
    if args.cmd == "distill":
        from . import distill
        return distill.main(["--source", args.source]
                            + (["--driver", args.driver] if args.driver else [])
                            + (["--model", args.model] if args.model else [])
                            + (["--effort", args.effort] if args.effort else [])
                            + (["--budget", str(args.budget)] if args.budget else [])
                            + (["--project", args.project] if args.project else []))
    if args.cmd == "serve-mcp":
        from . import mcp_server
        return mcp_server.main()
    if args.cmd == "review":
        return _cmd_review(args)
    if args.cmd == "init":
        return _cmd_init(args)
    if args.cmd == "run":
        from . import runner
        return runner.main(["--timeout", str(args.timeout)]
                           + (["--remine"] if args.remine else [])
                           + (["--driver", args.driver] if args.driver else [])
                           + (["--budget", str(args.budget)] if args.budget is not None else []))
    if args.cmd == "install":
        from . import install
        return install.main(args.harness)
    if args.cmd == "eval":
        from . import evalx
        return evalx.main(args.golden)
    if args.cmd == "doctor":
        return _cmd_doctor(args)
    if args.cmd == "stats":
        return _cmd_stats(args)
    if args.cmd == "lint":
        return _cmd_lint(args)
    if args.cmd == "relink":
        return _cmd_relink(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
