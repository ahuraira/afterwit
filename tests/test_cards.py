import pytest

from afterwit import cards


def make_card(**over):
    base = dict(
        id=cards.new_ulid(), type="decision", title="JSONB for audit payloads",
        project="acme_hr", status="active",
        body="Use JSONB. **Why:** schema churns weekly. See [[audit-refactor]].",
        sources=[{"path": "~/.claude/projects/x/y.jsonl", "lines": "L10-L20"}],
        tags=["postgres"], files=["src/audit/store.ts"], confidence=0.9,
        created="2026-07-05", updated="2026-07-05",
    )
    base.update(over)
    return cards.Card(**base)


def test_roundtrip():
    c = make_card()
    text = cards.render(c)
    c2 = cards.parse(text)
    assert (c2.id, c2.type, c2.title, c2.project) == (c.id, c.type, c.title, c.project)
    assert c2.sources == c.sources
    assert c2.body == c.body.strip()


def test_missing_sources_rejected():
    with pytest.raises(cards.CardError, match="provenance"):
        make_card(sources=[]).validate()


def test_bad_type_and_status_rejected():
    with pytest.raises(cards.CardError):
        make_card(type="wisdom").validate()
    with pytest.raises(cards.CardError):
        make_card(status="maybe").validate()


def test_wikilinks_extracted():
    assert make_card().wikilinks() == ["audit-refactor"]


def test_wikilinks_ignore_inline_and_fenced_code():
    card = make_card(body=(
        "See [[real-link]], `[[inline-example]]`, and:\n"
        "```markdown\n[[fenced-example]]\n```"
    ))
    assert card.wikilinks() == ["real-link"]


def test_relpath_by_type_and_project():
    # as_posix, not str: `str(WindowsPath)` renders separators as backslashes.
    assert make_card().relpath().as_posix() == "projects/acme_hr/decisions/jsonb-for-audit-payloads.md"
    g = make_card(project="global", type="preference", title="No barrel files")
    assert g.relpath().as_posix() == "global/facts/no-barrel-files.md"


def test_save_load_iter(tmp_path):
    c = make_card()
    path = cards.save(c, tmp_path)
    assert path.exists()
    found = list(cards.iter_cards(tmp_path))
    assert len(found) == 1 and found[0][1].id == c.id
    # review/ and non-card files are skipped
    (tmp_path / "review").mkdir()
    (tmp_path / "review" / "pending.md").write_text(cards.render(make_card(title="queued")))
    (tmp_path / "index.md").write_text("# catalog")
    (tmp_path / "notes.md").write_text("no frontmatter here")
    assert len(list(cards.iter_cards(tmp_path))) == 1


def test_same_title_never_overwrites_a_different_card(tmp_path):
    first = make_card(title="Shared title")
    second = make_card(title="Shared title")
    p1 = cards.save(first, tmp_path)
    p2 = cards.save(second, tmp_path)
    assert p1 != p2
    assert {c.id for _, c in cards.iter_cards(tmp_path)} == {first.id, second.id}


def test_source_origin_keys_roundtrip():
    # ADR-010: harness/model/kind are optional, round-trip untouched; only path mandatory
    src = {"path": "~/.claude/projects/x/y.jsonl", "lines": "L10-L20",
           "harness": "claude", "model": "claude-opus-4-8", "kind": "assistant"}
    c = make_card(sources=[src])
    assert cards.parse(cards.render(c)).sources == [src]


def test_usage_checkpoint_roundtrip():
    c = make_card(usefulness=3.4, last_used="2026-07-01T10:00:00+00:00")
    c2 = cards.parse(cards.render(c))
    assert c2.usefulness == 3.4 and c2.last_used == "2026-07-01T10:00:00+00:00"
    # fresh cards keep clean frontmatter — usage keys absent until earned
    assert "usefulness" not in cards.render(make_card())


def test_ulid_sortable_and_unique():
    a, b = cards.new_ulid(ts=1000.0), cards.new_ulid(ts=2000.0)
    assert a < b and len(a) == 26
    assert cards.new_ulid() != cards.new_ulid()


def test_capability_type_and_dir():
    c = make_card(type="capability", title="Smartsheet chunked writer",
                  files=["src/lib/smartsheet.ts"],
                  body="Chunks writes, retries 429. **Reuse:** read the file first.")
    c.validate()
    assert c.relpath().as_posix() == "projects/acme_hr/capabilities/smartsheet-chunked-writer.md"
