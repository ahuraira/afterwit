from afterwit import postprocess
from tests.test_cards import make_card


def test_duplicate_merges():
    ex = make_card(title="Prisma P1001 fix use 127.0.0.1",
                   body="On WSL2 localhost resolves to ::1; use 127.0.0.1 in DATABASE_URL.")
    cand = make_card(title="Prisma P1001 fix use 127.0.0.1",
                     body="On WSL2 localhost resolves to ::1; use 127.0.0.1 in DATABASE_URL!")
    actions = postprocess.process([cand], [ex])
    assert actions == [("merge", ex.id, cand)]


def test_rediscovering_an_approved_card_corroborates_it_instead_of_re_asking():
    """The invariant is still "an unreviewed duplicate cannot mutate a verified card" —
    but it is enforced at the merge site (wiki.py keeps the approved body), NOT by
    queuing.

    Queuing made every approval a permanent source of queue noise: the same knowledge
    came back for review every time a later session rediscovered it. 114 of 371 queued
    cards were duplicates of cards the user had ALREADY approved. Approving must reduce
    future work, never manufacture it, or the review gate punishes the person using it.
    """
    ex = make_card(title="Stable claim", body="Trusted body.", verified=True)
    cand = make_card(title="Stable claim", body="Trusted body!")
    assert postprocess.process([cand], [ex]) == [("merge", ex.id, cand)]


def test_near_match_queues_without_confirmer():
    ex = make_card(title="Audit payloads stored as JSONB in postgres",
                   body="Chose JSONB because audit schema churns weekly and typed columns need migrations.")
    cand = make_card(title="Audit payloads stored as typed columns in postgres",
                     body="Chose typed columns because audit schema stabilized and JSONB queries were slow.")
    actions = postprocess.process([cand], [ex])
    assert actions[0][0] == "queue" and actions[0][2].startswith("possible-supersede")


def test_near_match_supersedes_with_confirmer():
    ex = make_card(title="Audit payloads stored as JSONB in postgres",
                   body="Chose JSONB because audit schema churns weekly and typed columns need migrations.")
    cand = make_card(title="Audit payloads stored as typed columns in postgres",
                     body="Chose typed columns because audit schema stabilized and JSONB queries were slow.")
    actions = postprocess.process([cand], [ex], confirm_contradiction=lambda a, b: True)
    assert actions == [("supersede", ex.id, cand)]


def test_low_confidence_queues():
    cand = make_card(confidence=0.6)
    assert postprocess.process([cand], [])[0][0] == "queue"


def test_high_confidence_still_requires_review():
    cand = make_card(confidence=0.9)
    assert postprocess.process([cand], []) == [
        ("queue", cand, "high-confidence-unverified")
    ]


def test_agent_writes_always_queue():
    cand = make_card(confidence=0.99)
    actions = postprocess.process([cand], [], from_agent=True)
    assert actions[0][0] == "queue"  # capped to 0.79 < gate


def test_invalid_candidate_queued_not_crash():
    bad = make_card(sources=[])
    assert postprocess.process([bad], [])[0][0] == "queue"


def test_different_type_never_dedupes():
    ex = make_card(type="decision", title="Use JSONB for audit")
    cand = make_card(type="gotcha", title="Use JSONB for audit")
    assert postprocess.process([cand], [ex]) == [
        ("queue", cand, "high-confidence-unverified")
    ]


def test_reworded_duplicate_of_verified_card_still_asks_the_human():
    """The one place corroboration must NOT reach: bodies that differ.

    An exact dup (>=0.92) of an approved card is safe to fold in — the wording is the
    same, so keeping the approved body discards nothing. A title-overlap match is a
    heuristic and the bodies differ by definition, so folding it in would silently drop
    a claim. A live drain matched two genuinely different facts that shared vocabulary
    ("ML assets under ~/Data/pdf-bake" vs "libpdfium under Data/pdfium"), and measured,
    that false positive (0.34) and this true rewording (0.40) are too close to split
    with a threshold. So this goes to the human — and an UNVERIFIED target still merges,
    which is what the audit-2026-07-06 fix (8 rewordings of one preference) needed.
    """
    ex = make_card(title="Shapes must be dense nameable objects", type="preference",
                   body="Concept shapes read as real objects, not diagrams.", verified=True)
    cand = make_card(title="Concept shapes must be nameable dense objects", type="preference",
                     body="Abstract dot-field concepts must resolve into recognizable, "
                          "nameable organic objects within 200ms of viewing.")
    actions = postprocess.process([cand], [ex])
    assert actions[0] == ("queue", cand, f"possible-duplicate:{ex.id}")

    ex.verified = False          # unverified target: the audit fix still merges
    assert postprocess.process([cand], [ex])[0] == ("merge", ex.id, cand)


def test_title_overlap_does_not_merge_distinct_claims():
    ex = make_card(title="JSONB for audit payloads", type="decision", verified=True)
    cand = make_card(title="Audit retention window is 90 days", type="decision",
                     body="Retention: 90 days, then archive. **Why:** compliance.")
    actions = postprocess.process([cand], [ex])
    assert actions[0][0] in ("write", "queue") and actions[0][0] != "merge"
