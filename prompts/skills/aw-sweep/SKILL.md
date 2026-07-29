---
name: aw-sweep
description: Sweep one project's documentation artifacts (ADRs, research docs, CLAUDE.md, rules, memory files — never source code) and propose distilled knowledge cards into the afterwit review queue. Use to seed or refresh the knowledge base for a repo. One project per run.
---

# aw-sweep: distill a repo's documents into knowledge cards

You are seeding the user's cross-session knowledge base from ONE project's
documents. The purpose shapes every choice you make: these cards will be
**injected into future agent prompts** (≤3 cards, ≤600 tokens, ranked by
relevance). A vague card wastes that budget forever; a sharp card saves a
future agent an hour. Extract like every card costs the reader something —
because it does.

Command prefix:

```bash
AW="aw"
```

## Scope — read these, in this order

1. `docs/ADR.md` / `DECISIONS.md` — richest source. Each ADR section → one
   `decision` card; each Gotchas-Reference entry → one `gotcha` card.
2. `docs/research/*.md`, `docs/*.md` (SPEC, architecture, schema) — findings
   with evidence → `fact`; surprising constraints → `gotcha`.
3. `CLAUDE.md`, `.claude/rules/*.md` — hard rules and conventions → `preference`
   (project) — only ones a future agent could plausibly violate.
4. `README.md` — usually only toolchain facts ("build with X not Y because Z").
5. Memory dirs (`~/.claude/projects/<slug>/memory/*.md`) — often pre-distilled;
   convert, keep their provenance.
6. Skip: `CHANGELOG.md` (release notes rot), generated docs, licenses, code
   comments.

**Never open source files to extract knowledge.** Code is searched live by
future agents (grep beats stale snippets); indexing it is this system's #1
banned anti-pattern. The test for every candidate card: *could a future agent
answer this by grepping the repo?* If yes — no card. Knowledge earns a card
only when it lives in a past conversation, a tradeoff, a failure, or a rule.

## Per-card quality bar

- **Title states the claim, searchably.** Future retrieval is BM25 over title +
  body — distinctive tokens in the title are the retrieval surface.
  - YES: "Smartsheet API truncates cells >4000 chars silently — chunk before write"
  - NO: "Smartsheet notes", "API limitations"
- **Body self-contained, ≤300 tokens.** Reader has no other context. One insight
  per card — a doc section with three insights = three cards (or fewer: only the
  durable ones).
- **Decisions end with `**Why:** …`** — the constraint that drove the choice.
  The Why is what lets a future agent detect the decision went stale. A decision
  card without a recoverable why is not worth proposing.
- **Wikilinks**: when two cards you're proposing relate, reference `[[the-other
  -card-title-slug]]` in the body — this builds the knowledge graph.
- **Sources mandatory**: `{"path": "docs/ADR.md", "heading": "ADR-004", "kind": "doc"}`
  (or `lines`). No traceable source → drop the card, no exceptions.
- **files**: list concrete repo paths the knowledge concerns when the doc names
  them — this powers "what's known about this file" lookups.
- **confidence**: 0.75 human-authored ADR/manifesto content · 0.65 your synthesis
  across sections · 0.6 anything you inferred. Never higher — docs are trusted
  but your extraction isn't reviewed yet. Below 0.6, don't propose.
- **project**: the repo's directory name under `~/Desktop/Projects/`. Truly
  cross-project preferences (from user-level configs) → `"global"`.

## Dedupe before every proposal

```bash
$AW recall "<3-4 distinctive title tokens>" -p <project>
```
- Same claim exists → skip, count it.
- Existing card **contradicts** the doc (doc is newer truth) → propose anyway
  with `"reason": "supersede-candidate"` and name the conflicting card id in the body.

## Propose

```bash
echo '{"type": "decision", "title": "...", "project": "<slug>", "body": "...\n**Why:** ...", "sources": [{"path": "docs/ADR.md", "heading": "ADR-004", "kind": "doc"}], "files": [], "tags": [], "confidence": 0.75, "reason": "doc-sweep"}' | $AW queue
```

Everything lands in a human review queue (`afterwit ui`) — nothing you propose is
trusted automatically. That is by design; do not try to write the wiki directly.

## Volume discipline

≤15 cards per project sweep; 8 sharp cards beat 20 mediocre ones (injected
context competes with the user's actual task — mediocre cards actively degrade
future sessions). If a project's docs are thin, 2 cards is a fine result; if
they're empty, 0 cards and say so. Never pad.

## Sweeping many projects — fan out, don't widen

One project per run is the unit of QUALITY (fresh context per repo, correct
project slugs, reviewable batch sizes), not a coverage limit. To cover many
repos: spawn one subagent per project in parallel, each given this skill and
exactly one repo. Never sweep several repos in one context — extraction
sharpness decays and cards get filed under the wrong project. Cross-project
patterns ("same preference in many repos") are consolidation-layer work after
approval — do not reach outside your assigned project to hunt for them.

## Done contract

Don't stop to ask questions; make the safest assumption and note it. Finish
with a report:

| type | proposed | skipped (dup) | supersede-candidates |
|---|---|---|---|

plus one line per supersede-candidate (old card id → why), and remind the user:
review with `afterwit ui` (a=approve e=edit r=reject).
