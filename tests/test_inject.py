import io
import json

from afterwit import cards, config, index_db, inject
from tests.test_cards import make_card
from tests.conftest import toml_config


def setup_env(tmp_path, monkeypatch, cardlist):
    wiki = tmp_path / "wiki"
    for c in cardlist:
        cards.save(c, wiki)
    db = tmp_path / "index.db"
    conn = index_db.connect(db)
    index_db.rebuild(conn, wiki)
    conn.close()
    cfg_file = tmp_path / "config.toml"
    projects_root = tmp_path / "Projects"
    (projects_root / "acme_hr").mkdir(parents=True)
    cfg_file.write_text(toml_config(wiki_root=wiki, db_path=db, projects_root=projects_root))
    monkeypatch.setenv("AFTERWIT_CONFIG", str(cfg_file))
    return projects_root


def payload(prompt, cwd):
    return json.dumps({"prompt": prompt, "cwd": str(cwd), "session_id": "s1"})


def test_prompt_injects_relevant_within_budget(tmp_path, monkeypatch):
    root = setup_env(tmp_path, monkeypatch, [
        make_card(title="Prisma P1001 fix use 127.0.0.1", type="error_fix",
                  body="P1001 fix " * 300, verified=True, updated="2026-07-05"),
    ])
    out = inject.run(["--mode", "prompt"],
                     payload("prisma P1001 cannot reach database", root / "acme_hr"))
    assert "P1001" in out and "verify before relying" in out
    cfg = config.load()
    assert len(out) // 4 <= cfg.inject_max_tokens + 20  # hard budget holds even for huge bodies


def test_irrelevant_prompt_injects_nothing(tmp_path, monkeypatch):
    # push-eligible type on purpose: with a `decision` here the type filter would
    # return "" on its own and this would pass with the relevance floor deleted.
    root = setup_env(tmp_path, monkeypatch, [make_card(type="gotcha", verified=True)])
    assert inject.run(["--mode", "prompt"],
                      payload("write a haiku about kubernetes ingress", root)) == ""


def test_trivial_prompt_injects_nothing(tmp_path, monkeypatch):
    root = setup_env(tmp_path, monkeypatch, [make_card(type="gotcha", verified=True)])
    assert inject.run(["--mode", "prompt"], payload("yes", root)) == ""


def test_unverified_never_pushed(tmp_path, monkeypatch):
    # ADR-011: push is zero-consent — relevant but unreviewed cards stay out.
    root = setup_env(tmp_path, monkeypatch, [
        make_card(title="Prisma P1001 fix use 127.0.0.1", type="error_fix",
                  body="use 127.0.0.1", verified=False, updated="2026-07-05"),
    ])
    assert inject.run(["--mode", "prompt"],
                      payload("prisma P1001 cannot reach database",
                              root / "acme_hr")) == ""
    assert inject.run(["--mode", "session"], payload("", root / "acme_hr")) == ""


def test_push_unverified_optin_labels_risk(tmp_path, monkeypatch):
    root = setup_env(tmp_path, monkeypatch, [
        make_card(title="Prisma P1001 fix use 127.0.0.1", type="error_fix",
                  body="P1001: cannot reach database server — use 127.0.0.1 in DATABASE_URL",
                  verified=False, updated="2026-07-05"),
    ])
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(cfg_file.read_text() + "push_unverified = true\n")
    out = inject.run(["--mode", "prompt"],
                     payload("prisma P1001 cannot reach database", root / "acme_hr"))
    assert "P1001" in out and "UNVERIFIED" in out  # served, but risk is disclosed


def test_serving_logged(tmp_path, monkeypatch):
    root = setup_env(tmp_path, monkeypatch, [
        make_card(title="Prisma P1001 fix", type="error_fix", verified=True,
                  body="use 127.0.0.1", updated="2026-07-05"),
    ])
    inject.run(["--mode", "prompt"], payload("prisma P1001 error again", root / "acme_hr"))
    conn = index_db.connect(config.load().db_path, readonly=True)
    assert conn.execute("SELECT COUNT(*) FROM servings").fetchone()[0] == 1


def test_session_mode_brief(tmp_path, monkeypatch):
    root = setup_env(tmp_path, monkeypatch, [
        make_card(type="gotcha", title="Smartsheet API truncates silently", verified=True),
    ])
    out = inject.run(["--mode", "session"], payload("", root / "acme_hr"))
    assert "gotcha" in out and "recall" in out


def test_fail_open_on_garbage(monkeypatch, capsys):
    monkeypatch.setenv("AFTERWIT_CONFIG", "/nonexistent/config.toml")
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("{not json"))
    assert inject.main(["--mode", "prompt"]) == 0
    assert capsys.readouterr().out == ""


def test_afterwits_own_llm_children_get_nothing_injected(tmp_path, monkeypatch):
    """`claude -p`, spawned by distill/review, inherits the user's hooks — so
    without this guard afterwit injects into itself. A reviewer emits a verdict
    about a DIFFERENT card and never echoes the injected one, so every such
    serving scores `ignored`: 56 of the 76 card-servings that auto-disabled
    injection on 2026-07-26 were exactly this, and they also charged the two
    cards they kept pulling -0.2 apiece, 28 times, down to usefulness -5.6.

    Both modes, because the child fires SessionStart too. Positive controls
    first — without them this test passes against a `return ""` stub."""
    root = setup_env(tmp_path, monkeypatch, [
        make_card(title="Prisma P1001 fix use 127.0.0.1", type="error_fix",
                  body="Fix: 127.0.0.1", verified=True, updated="2026-07-05"),
    ])
    body = payload("prisma P1001 cannot reach database", root / "acme_hr")
    assert "P1001" in inject.run(["--mode", "prompt"], body)
    assert "afterwit knows this project" in inject.run(["--mode", "session"], body)

    monkeypatch.setenv("AFTERWIT_INTERNAL", "1")
    assert inject.run(["--mode", "prompt"], body) == ""
    assert inject.run(["--mode", "session"], body) == ""


def test_push_serves_behavioral_types_only_and_the_config_is_what_says_so(
        tmp_path, monkeypatch):
    """Push spends attention nobody asked for, so `decision`/`doc_ref`/`fact` —
    73% of the real inject budget before this — stay pull-only (config.push_types).

    Both halves are load-bearing. The first alone would pass if the decision
    merely failed to rank; the second proves it was RANKING FINE and the type
    filter is what dropped it, and that the filter reads config rather than a
    hardcoded set. `test_recall_type_filter` is the other side: pull still sees it.
    """
    root = setup_env(tmp_path, monkeypatch, [
        make_card(title="Smartsheet cell writes truncate silently", type="gotcha",
                  body="Chunk below 4000 chars.", verified=True, updated="2026-07-05"),
        make_card(title="Smartsheet cell writes go through the bulk endpoint",
                  type="decision", body="Bulk endpoint. **Why:** rate limits.",
                  verified=True, updated="2026-07-05"),
    ])
    cfg_file = tmp_path / "config.toml"
    base = cfg_file.read_text() + "floor = 0.0\n"  # isolate the type gate from the floor
    cfg_file.write_text(base)
    q = "smartsheet cell writes truncate silently bulk endpoint"

    out = inject.run(["--mode", "prompt"], payload(q, root / "acme_hr"))
    assert "[gotcha]" in out and "[decision]" not in out

    cfg_file.write_text(base + 'push_types = ["gotcha", "decision"]\n')
    out2 = inject.run(["--mode", "prompt"], payload(q, root / "acme_hr"))
    assert "[decision]" in out2, "the decision ranked fine — push_types dropped it"


# ------------------------------------------------------------------ error mode

def err_payload(error, cwd, **over):
    """PostToolUseFailure/Bash. ONE flat `error` string — the CLI builds it as
    ["Exit code N", <interrupt>, stderr, stdout] joined by newlines. There is no
    exit-code field and no stdout/stderr split on this event."""
    d = {"hook_event_name": "PostToolUseFailure", "tool_name": "Bash",
         "tool_input": {"command": "pytest -q"}, "tool_use_id": "toolu_01",
         "error": error, "is_interrupt": False,
         "cwd": str(cwd), "session_id": "s-err"}
    d.update(over)
    return json.dumps(d)


def _err_env(tmp_path, monkeypatch):
    return setup_env(tmp_path, monkeypatch, [
        make_card(title="Prisma P1001 cannot reach database on WSL2", type="error_fix",
                  body="Fix: use 127.0.0.1 in DATABASE_URL, not localhost",
                  verified=True, updated="2026-07-05"),
    ])


def test_error_mode_injects_a_recorded_fix_when_a_command_fails(tmp_path, monkeypatch):
    root = _err_env(tmp_path, monkeypatch)
    out = inject.run(["--mode", "error"], err_payload(
        "Exit code 1\nError: P1001 Can't reach database server at localhost:5432",
        root / "acme_hr"))
    assert "127.0.0.1" in out and "seen this error before" in out


def test_error_mode_stays_silent_on_everything_that_is_not_a_real_failure(tmp_path, monkeypatch):
    """`error` also carries permission denials, classifier refusals and user
    aborts. None are bugs, none have a recorded fix, and injecting on them would
    spend the budget on noise every time a command needs approval.

    Each case must be rejected by the ONE gate it names — so the non-failure
    strings below all carry a newline and a long, card-matching second line. A
    single-line denial would be filtered by the detail-length check instead, and
    then deleting the `Exit code` gate entirely would leave this test still green.
    """
    root = _err_env(tmp_path, monkeypatch)
    cwd = root / "acme_hr"
    sig = "P1001 Can't reach database server at localhost:5432"
    real = f"Exit code 1\n{sig}"
    assert inject.run(["--mode", "error"], err_payload(real, cwd))          # control

    for payload, why in (
        # rejected by the is_interrupt gate ONLY — otherwise identical to the control
        (err_payload(real, cwd, is_interrupt=True), "user pressed escape"),
        # rejected by the "Exit code " gate ONLY — detail is long and matches a card
        (err_payload(f"Blocked: command matched a deny rule\n{sig}", cwd), "permission denial"),
        (err_payload(f"This command requires approval\n{sig}", cwd), "classifier refusal"),
        (err_payload(f"Claude Code is temporarily unavailable\n{sig}", cwd), "transient CLI error"),
        # rejected by the detail gate ONLY — prefix is a genuine "Exit code "
        (err_payload("Exit code 1", cwd), "no output at all to match on"),
        (err_payload("Exit code 1\nP1001", cwd), "detail too short to be a signature"),
    ):
        assert inject.run(["--mode", "error"], payload) == "", why


def test_error_mode_puts_a_recorded_fix_above_a_merely_topical_card(tmp_path, monkeypatch):
    """SPEC §9.1: when something just broke, a recorded fix outranks a discussion
    of the same topic.

    The floor is dropped to 0 deliberately. At the default 0.35 a specific error
    string usually leaves exactly ONE card standing — min-max normalisation over a
    small candidate pool sends everything but the best match toward zero — so the
    bias has nothing to reorder, and a version of this test written at the default
    floor passes identically whether or not the sort exists at all.

    The control is the point: unbiased ranking puts the DECISION first here,
    because its title is near-verbatim the error text. If `errors_first` is
    removed, the order flips and this goes red.
    """
    from afterwit import index_db, rank
    root = setup_env(tmp_path, monkeypatch, [
        # A `gotcha`, not a `decision`: since push_types, a decision is never
        # pushed at all, so ordering it below error_fix would be vacuously true.
        # The control has to be a card push actually serves.
        make_card(title="P1001 Can't reach database server at localhost:5432",
                  type="gotcha", body="Postgres refuses over the WSL2 bridge.",
                  verified=True),
        make_card(title="P1001 cannot reach the database server", type="error_fix",
                  body="Fix: use 127.0.0.1 in DATABASE_URL", verified=True),
    ])
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(cfg_file.read_text() + "floor = 0.0\n")

    query = "Error: P1001 Can't reach database server at localhost:5432"
    cfg = config.load()
    conn = index_db.connect(cfg.db_path, readonly=True)
    unbiased = rank.rank(index_db.search(conn, query, project="acme_hr", k=20),
                         "acme_hr", floor=cfg.floor, k=3, query_text=query)
    conn.close()
    assert [s.type for s in unbiased][:2] == ["gotcha", "error_fix"], \
        "control broken: the gotcha must win on relevance alone, or the bias is untested"

    out = inject.run(["--mode", "error"], err_payload("Exit code 1\n" + query,
                                                     root / "acme_hr"))
    assert out.index("[error_fix]") < out.index("[gotcha]")


def test_error_mode_output_is_wrapped_in_the_envelope_the_cli_actually_reads(
        tmp_path, monkeypatch, capsys):
    """Plain stdout is DISCARDED on PostToolUseFailure — only SessionStart and
    UserPromptSubmit render it — so the envelope is the only thing that reaches
    the model. `hookEventName` must equal the firing event exactly; the CLI
    throws the output away on a mismatch. Prompt mode must stay plain text."""
    root = _err_env(tmp_path, monkeypatch)
    body = err_payload("Exit code 1\nError: P1001 Can't reach database server at localhost",
                       root / "acme_hr")
    monkeypatch.setattr("sys.stdin", io.StringIO(body))
    assert inject.main(["--mode", "error"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUseFailure"
    assert "127.0.0.1" in out["hookSpecificOutput"]["additionalContext"]

    monkeypatch.setattr("sys.stdin", io.StringIO(
        payload("prisma P1001 cannot reach database", root / "acme_hr")))
    assert inject.main(["--mode", "prompt"]) == 0
    plain = capsys.readouterr().out
    assert plain.startswith("Relevant knowledge") and not plain.startswith("{")


def test_the_killswitch_silences_error_mode_too(tmp_path, monkeypatch):
    """When the gate has decided push cannot prove its value, silence means
    silence on every push surface — not just the prompt one."""
    root = _err_env(tmp_path, monkeypatch)
    body = err_payload("Exit code 1\nError: P1001 Can't reach database server at localhost",
                       root / "acme_hr")
    assert inject.run(["--mode", "error"], body)  # control
    config.load().db_path.with_name("inject.disabled").write_text("off")
    assert inject.run(["--mode", "error"], body) == ""


def test_error_servings_are_logged_apart_from_prompt_servings(tmp_path, monkeypatch):
    """They must not feed the prompt kill-switch: different surface, different
    economics. killswitch_status only ever counts mode='inject'."""
    from afterwit import index_db
    root = _err_env(tmp_path, monkeypatch)
    inject.run(["--mode", "error"], err_payload(
        "Exit code 1\nError: P1001 Can't reach database server at localhost",
        root / "acme_hr"))
    conn = index_db.connect(config.load().db_path, readonly=True)
    modes = [r[0] for r in conn.execute("SELECT mode FROM servings")]
    assert modes == ["error"]


def test_injection_finds_cards_for_a_project_whose_folder_is_named_differently(
        tmp_path, monkeypatch):
    """The end-to-end reason ADR-039 exists. `_session_mode` filters on project
    with a hard equality, so an un-aliased rename takes it from 112 cards to 0 on
    the real index; prompt ranking loses its +0.15 same-project boost on top."""
    root = setup_env(tmp_path, monkeypatch, [
        make_card(title="Prisma P1001 fix use 127.0.0.1", type="error_fix",
                  project="afterwit", body="Fix: 127.0.0.1", verified=True,
                  updated="2026-07-05"),
    ])
    (root / "harness_helper").mkdir(exist_ok=True)
    cwd = root / "harness_helper"
    body = payload("prisma P1001 cannot reach database", cwd)

    # control: folder name != slug, no alias -> the session line is empty
    assert inject.run(["--mode", "session"], body) == ""

    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(cfg_file.read_text()
                        + '\n[project_aliases]\nharness_helper = "afterwit"\n')
    assert "afterwit knows this project (afterwit)" in inject.run(["--mode", "session"], body)
    assert "P1001" in inject.run(["--mode", "prompt"], body)


def test_codex_servings_are_attributed_to_codex_not_claude(tmp_path, monkeypatch):
    """`harness` was hardcoded "claude" in log_serving. Leave it and every Codex
    serving lands in the Claude column: usage mining then resolves the Codex
    session_id against ~/.claude/projects, finds nothing, and marks it skipped
    forever — unmeasurable, and re-scanned every night (ADR-040)."""
    from afterwit import index_db
    root = setup_env(tmp_path, monkeypatch, [
        make_card(title="Prisma P1001 fix use 127.0.0.1", type="error_fix",
                  body="P1001 fix", verified=True, updated="2026-07-05"),
    ])
    body = payload("prisma P1001 cannot reach database", root / "acme_hr")
    assert inject.run(["--mode", "prompt", "--harness", "codex"], body)
    assert inject.run(["--mode", "prompt"], body)  # default stays claude

    conn = index_db.connect(config.load().db_path, readonly=True)
    assert [r[0] for r in conn.execute(
        "SELECT harness FROM servings ORDER BY id")] == ["codex", "claude"]


def test_a_malformed_harness_flag_degrades_instead_of_raising(tmp_path, monkeypatch):
    """Same contract as --mode: the hook path fails open, so a trailing flag with
    nothing after it must fall back, never IndexError."""
    root = setup_env(tmp_path, monkeypatch, [
        make_card(title="Prisma P1001 fix use 127.0.0.1", type="error_fix",
                  body="P1001 fix", verified=True, updated="2026-07-05"),
    ])
    body = payload("prisma P1001 cannot reach database", root / "acme_hr")
    assert inject.run(["--mode", "prompt", "--harness"], body)
    assert inject._harness(["--harness"]) == "claude"


def test_the_hook_entrypoint_never_exits_nonzero_whatever_its_argv(tmp_path, monkeypatch,
                                                                   capsys):
    """The one guard that matters most in this file.

    inject.py's contract is FAIL OPEN — empty stdout, exit 0 — but it used to be
    reached through argparse, which enforces its own contract first and exits 2
    on any flag it does not know. Exit 2 from a prompt hook is not a no-op:
    Claude Code erases the user's prompt (Gotcha #5), Codex reports the turn
    Blocked. Adding `--harness codex` to the installed command did exactly that
    on the author's machine (Gotcha #71) — the hook was live, every Codex prompt
    was blocked, and the only symptom was the word "Blocked".
    """
    from afterwit import cli
    root = setup_env(tmp_path, monkeypatch, [
        make_card(title="Prisma P1001 fix use 127.0.0.1", type="error_fix",
                  body="P1001 fix", verified=True, updated="2026-07-05"),
    ])
    body = payload("prisma P1001 cannot reach database", root / "acme_hr")
    for argv in (["inject", "--mode", "prompt", "--harness", "codex"],
                 ["inject", "--mode", "session", "--harness", "codex"],
                 ["inject", "--mode", "prompt", "--flag-from-a-future-version", "x"],
                 ["inject", "--harness"],          # value missing
                 ["inject"]):
        monkeypatch.setattr("sys.stdin", io.StringIO(body))
        assert cli.main(argv) == 0, argv        # SystemExit here is the bug
    # and it still actually served through the CLI path, not just exited quietly
    monkeypatch.setattr("sys.stdin", io.StringIO(body))
    cli.main(["inject", "--mode", "prompt", "--harness", "codex"])
    assert "P1001" in capsys.readouterr().out


def test_an_injected_card_carries_its_id_so_feedback_can_name_it(tmp_path, monkeypatch):
    """`feedback(card_id, verdict)` needs an id, and push is where most exposures
    happen — a session can see a dozen injected cards and rate none of them.
    The mined used/ignored outcome still runs underneath; this adds the explicit
    channel for the turn where the agent KNOWS whether the card earned its place."""
    cardlist = [make_card(title="Prisma P1001 fix use 127.0.0.1", type="error_fix",
                          body="P1001 fix use 127.0.0.1", verified=True,
                          updated="2026-07-05")]
    root = setup_env(tmp_path, monkeypatch, cardlist)
    out = inject.run(["--mode", "prompt"],
                     payload("prisma P1001 cannot reach database", root / "acme_hr"))
    assert f"id: {cardlist[0].id}" in out, "an injected card cannot be rated: no id"


def test_the_error_query_carries_the_command_not_only_the_output(tmp_path, monkeypatch):
    """Output-keyed matching dies to a pipe. `| tail -3` drops the signature and
    this hook goes quiet on a failure it holds a card for; a real serving matched
    on nothing but "Command timed out after 2m 0s". The command survives both.
    """
    root = setup_env(tmp_path, monkeypatch, [
        make_card(title="npm run test:epic19-migration-replay times out without a reset",
                  type="error_fix", body="Fix: run prisma migrate reset before the replay",
                  verified=True, updated="2026-07-05"),
    ])
    err = "Exit code 1\nCommand timed out after 2m 0s"  # output says nothing matchable
    cmd = {"command": "npm run test:epic19-migration-replay"}
    assert "epic19" in inject.run(["--mode", "error"],
                                  err_payload(err, root / "acme_hr", tool_input=cmd))
    # The same failure with the command withheld: the boilerplate alone cannot
    # clear the floor, so the card is unreachable without it. That is the whole
    # claim — not "the command helps", but "this card is ONLY reachable via it".
    assert inject.run(["--mode", "error"],
                      err_payload(err, root / "acme_hr", tool_input={})) == ""


# The error signature and the command, verbatim in both modes below, so the ONLY
# difference between the two assertions is which mode read them.
_ESM_CMD = "node --test app.js"
_ESM_ERR = "ERR_MODULE_NOT_FOUND cannot find package esm loader NODE_PATH"


def test_error_mode_keeps_a_foreign_project_that_prompt_mode_would_drop(tmp_path,
                                                                       monkeypatch):
    """The cross-project penalty must not reach error lookups, and the seam that
    enforces that lives in `_serve`, not in rank — a rank-level test passes even
    when the caller stops passing the flag (that mutation survived once already).

    Found by replaying real servings: at every factor <= 0.9 the penalty killed a
    `reader-app` card, mined as USED, that matched an ERR_MODULE_NOT_FOUND trace
    from a different project. A stack trace is a property of the runtime, not of
    the repo it fired in. A vague prompt is not — so the same text, read as a
    prompt, is correctly demoted into silence.
    """
    root = setup_env(tmp_path, monkeypatch, [
        make_card(title="ERR_MODULE_NOT_FOUND node esm loader ignores NODE_PATH",
                  type="error_fix", project="reader-app", verified=True,
                  body="Fix: pass --import or use explicit relative specifiers."),
    ])
    # Straddles deliberately: this card scores 0.2425 exempt, 0.1818 demoted.
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(cfg_file.read_text() + "floor = 0.2\n")
    assert "ERR_MODULE_NOT_FOUND" in inject.run(
        ["--mode", "error"],
        err_payload(f"Exit code 1\n{_ESM_ERR}", root / "acme_hr",
                    tool_input={"command": _ESM_CMD}))
    assert inject.run(["--mode", "prompt"],
                      payload(f"{_ESM_CMD}\n{_ESM_ERR}", root / "acme_hr")) == ""
