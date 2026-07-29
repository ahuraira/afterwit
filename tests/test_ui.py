import json
import threading
import urllib.request
from pathlib import Path

import pytest

from afterwit import cards, config, index_db, ui
from tests.test_cards import make_card
from tests.test_inject import setup_env


@pytest.fixture
def server(tmp_path, monkeypatch):
    setup_env(tmp_path, monkeypatch, [
        make_card(title="Prisma P1001 fix use 127.0.0.1", type="error_fix",
                  body="Fix: 127.0.0.1", verified=True, updated="2026-07-05"),
    ])
    cfg = config.load()
    conn = index_db.connect(cfg.db_path)
    ui.queue_insert(conn, make_card(title="Queued gotcha", type="gotcha",
                                    confidence=0.7, verified=False), "low-confidence")
    conn.close()
    srv = ui.serve(port=0, cfg=cfg)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", cfg
    srv.shutdown()
    srv.server_close()  # the fixture is per-test; without this every one leaks a listener


def get(url):
    with urllib.request.urlopen(url) as r:
        return json.loads(r.read())


def post(url, body=None):
    base = url.split("/api/", 1)[0]
    token = get(base + "/api/config")["csrf_token"]
    req = urllib.request.Request(url, method="POST",
                                 data=json.dumps(body or {}).encode(),
                                 headers={"Content-Type": "application/json",
                                          "X-Afterwit-CSRF": token})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def test_html_served(server):
    base, _ = server
    with urllib.request.urlopen(base + "/") as r:
        assert b"afterwit" in r.read()


def test_post_requires_csrf_token(server):
    base, _ = server
    rowid = get(base + "/api/review")[0]["rowid"]
    req = urllib.request.Request(
        f"{base}/api/review/{rowid}/reject", method="POST", data=b"{}",
        headers={"Content-Type": "application/json"},
    )
    with pytest.raises(urllib.error.HTTPError) as exc:  # noqa: F821
        urllib.request.urlopen(req)
    assert exc.value.code == 403


def test_review_list_and_approve_sets_verified(server):
    base, cfg = server
    items = get(base + "/api/review")
    assert len(items) == 1 and items[0]["reason"] == "low-confidence"
    res = post(f"{base}/api/review/{items[0]['rowid']}/approve",
               {"card": {"title": "Queued gotcha (edited)"}})
    assert get(base + "/api/review") == []
    conn = index_db.connect(cfg.db_path, readonly=True)
    row = conn.execute("SELECT title, verified, status FROM cards WHERE id=?",
                       (res["id"],)).fetchone()
    assert row["title"] == "Queued gotcha (edited)"
    assert row["verified"] == 1 and row["status"] == "active"
    assert (cfg.wiki_root / "projects").exists()


def test_reject_logs_and_clears(server):
    base, cfg = server
    items = get(base + "/api/review")
    post(f"{base}/api/review/{items[0]['rowid']}/reject")
    assert get(base + "/api/review") == []
    assert "review-reject: Queued gotcha" in config.log_path(cfg.wiki_root).read_text()


def test_double_action_is_safe(server):
    base, _ = server
    rowid = get(base + "/api/review")[0]["rowid"]
    post(f"{base}/api/review/{rowid}/reject")
    with pytest.raises(urllib.error.HTTPError) as e:  # noqa: F821
        post(f"{base}/api/review/{rowid}/approve")
    assert e.value.code == 410


def test_graph_edges_resolved(tmp_path, monkeypatch):
    linked = make_card(title="Audit refactor", verified=True, body="Plain body.", files=[])
    linker = make_card(title="JSONB decision", verified=True,
                       body="See [[audit-refactor]] and [[ghost-page]].",
                       files=["src/audit/store.ts"])
    filemate = make_card(title="Audit store gotcha", type="gotcha", verified=True,
                         body="Plain body.", files=["src/audit/store.ts"])
    old = make_card(title="Old audit decision", status="superseded", verified=True,
                    body="Plain body.", files=[])
    old.superseded_by = linker.id
    setup_env(tmp_path, monkeypatch, [linked, linker, filemate, old])
    cfg = config.load()
    srv = ui.serve(port=0, cfg=cfg)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        g = get(base + "/api/graph")
        assert len(g["nodes"]) == 4
        kinds = {(e["kind"]): (e["source"], e["target"]) for e in g["edges"]}
        assert kinds["wikilink"] in ((linker.id, linked.id), (linked.id, linker.id))
        assert set(kinds["file"]) == {linker.id, filemate.id}
        assert set(kinds["supersede"]) == {old.id, linker.id}
        assert len(g["edges"]) == 3  # ghost-page unresolved → no edge
        d = get(base + f"/api/card?id={linker.id}")
        assert d["title"] == "JSONB decision" and d["files"] == ["src/audit/store.ts"]
    finally:
        srv.shutdown()


def test_search_and_stats(server):
    base, _ = server
    rs = get(base + "/api/search?q=" + urllib.parse.quote("prisma P1001"))  # noqa: F821
    assert rs and "P1001" in rs[0]["title"]
    st = get(base + "/api/stats")
    assert st["pending"] == 1 and st["totals"]["active"] == 1
    assert st["killswitch"]["served"] == 0


def test_graph_never_links_same_file_across_projects(tmp_path):
    wiki = tmp_path / "wiki"
    a = make_card(project="alpha", title="Alpha", files=["src/app.py"], verified=True)
    b = make_card(project="beta", title="Beta", files=["src/app.py"], verified=True)
    cards.save(a, wiki)
    cards.save(b, wiki)
    conn = index_db.connect(tmp_path / "index.db")
    index_db.rebuild(conn, wiki)
    graph = ui._graph(conn)
    assert not [e for e in graph["edges"] if e["kind"] == "file"]


# --------------------------------------------------------------- settings API

def post_err(url, body):
    """POST expecting a 4xx — returns (status, error message)."""
    base = url.split("/api/", 1)[0]
    token = get(base + "/api/config")["csrf_token"]
    req = urllib.request.Request(url, method="POST", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json",
                                          "X-Afterwit-CSRF": token})
    try:
        urllib.request.urlopen(req)
    except urllib.error.HTTPError as e:  # noqa: F821
        return e.code, json.loads(e.read())["error"]
    raise AssertionError("expected the request to be refused")


def test_settings_exposes_values_schema_and_harness_models(server, tmp_path, monkeypatch):
    base, cfg = server
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))  # noqa: F821
    (tmp_path / ".codex").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".codex" / "config.toml").write_text('model = "gpt-5.6-sol"\n', encoding="utf-8")

    s = get(base + "/api/settings")
    assert s["values"]["distill_driver"] == cfg.distill_driver
    assert s["values"]["wiki_root"] == str(cfg.wiki_root)
    keys = {f["key"] for f in s["schema"]}
    assert {"distill_model", "auto_review", "floor", "projects_root"} <= keys
    assert set(s["drivers"]) == {"claude-p", "codex"}
    # the model list is read from the harness's OWN config, not hardcoded here
    assert s["harnesses"]["codex"]["model"] == "gpt-5.6-sol"
    assert "gpt-5.6-sol" in s["harnesses"]["codex"]["models"]


def test_settings_save_persists_and_the_running_server_picks_it_up(server):
    base, _ = server
    out = post(base + "/api/settings",
               {"distill_driver": "codex", "distill_model": "gpt-5.6-sol",
                "auto_review": True, "inject_max_cards": 2})
    assert out["values"]["distill_driver"] == "codex"
    assert out["values"]["inject_max_cards"] == 2
    # a fresh GET proves the handler reloaded its Config, not just echoed the POST
    assert get(base + "/api/settings")["values"]["distill_model"] == "gpt-5.6-sol"
    # and the surface the rest of the UI reads agrees
    assert get(base + "/api/config")["auto_review"] is True
    assert config.load().distill_driver == "codex"


def test_settings_save_unsets_with_an_empty_string(server):
    base, _ = server
    post(base + "/api/settings", {"distill_model": "opus"})
    assert config.load().distill_model == "opus"
    out = post(base + "/api/settings", {"distill_model": ""})
    assert out["values"]["distill_model"] is None
    assert config.load().distill_model is None


def test_settings_refuses_bad_input(server):
    base, _ = server
    for body, fragment in (
        ({"floor": "banana"}, "number"),
        ({"inject_max_cards": 9}, "between"),          # Manifesto P3 cap
        ({"distill_driver": "gpt-9"}, "must be one of"),
        ({"wiki_root": "not/absolute"}, "absolute"),
        ({"hooks": "rm -rf /"}, "unknown setting"),
        ({}, "no settings"),
    ):
        code, err = post_err(base + "/api/settings", body)
        assert code == 400 and fragment in err
    assert config.load().floor == 0.35  # nothing partial was written


def test_settings_post_still_needs_the_csrf_token(server):
    base, _ = server
    req = urllib.request.Request(base + "/api/settings", method="POST",
                                 data=b'{"floor": 0.9}',
                                 headers={"Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as exc:  # noqa: F821
        urllib.request.urlopen(req)
    assert exc.value.code == 403
    assert config.load().floor == 0.35


def test_a_stale_csrf_token_is_labelled_so_the_page_can_recover(server):
    """The token is per SERVER PROCESS. Restarting `afterwit ui` invalidates the one
    every open tab fetched at boot, so saving Settings 403s until a manual reload —
    with an error naming neither the cause nor the fix. The page retries once on
    `code == "csrf"`, and must be able to tell this 403 from the auto-review-disabled
    one (retrying THAT would loop), so the marker is load-bearing, not decoration."""
    base, _ = server
    req = urllib.request.Request(base + "/api/settings", method="POST",
                                 data=b'{"floor": 0.9}',
                                 headers={"Content-Type": "application/json",
                                          "X-Afterwit-CSRF": "a-token-from-a-dead-process"})
    with pytest.raises(urllib.error.HTTPError) as exc:  # noqa: F821
        urllib.request.urlopen(req)
    assert exc.value.code == 403
    body = json.loads(exc.value.read())
    assert body["code"] == "csrf"
    assert "reload" in body["error"]

    # The auto-review 403 must NOT carry the marker, or the retry loops forever.
    disabled = urllib.request.Request(base + "/api/review/autoreview-all", method="POST",
                                      data=b"{}",
                                      headers={"Content-Type": "application/json",
                                               "X-Afterwit-CSRF": get(base + "/api/config")["csrf_token"]})
    with pytest.raises(urllib.error.HTTPError) as exc2:  # noqa: F821
        urllib.request.urlopen(disabled)
    assert exc2.value.code == 403
    assert json.loads(exc2.value.read()).get("code") != "csrf"


def test_saving_run_time_reschedules_only_an_installed_nightly(tmp_path, monkeypatch):
    """Paired gate (ADR-046): scheduled -> install_cron reapplied with the new
    time; not scheduled -> config saved but NO scheduler installed behind the
    user's back, and the payload note says how to schedule one."""
    from afterwit import install

    setup_env(tmp_path, monkeypatch, [make_card(title="Seed", verified=True)])
    ui.Handler.cfg = config.load()
    applied = []
    monkeypatch.setattr(install, "install_cron",
                        lambda **kw: applied.append(kw) or
                        {"mode": "systemd", "note": "enabled", "changed": ["t"], "backed_up": []})

    monkeypatch.setattr(install, "cron_scheduled", lambda **kw: False)
    out = ui._save_settings({"run_time": "04:05"})
    assert applied == [] and "install cron" in out["note"]

    monkeypatch.setattr(install, "cron_scheduled", lambda **kw: True)
    out = ui._save_settings({"run_time": "05:10"})
    assert applied and applied[0]["at"] == "05:10"
    assert "rescheduled to 05:10" in out["note"]
