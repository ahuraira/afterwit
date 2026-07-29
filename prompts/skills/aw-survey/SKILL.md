---
name: aw-survey
description: Survey one project's code STRUCTURE (entry points, exports, public APIs, schemas — never implementations) and produce capability cards + a stamped map.md, so agents in other projects discover reusable work instead of reinventing it. Use on new/changed repos or when code-drift lint flags dead capability paths. One project per run.
---

# aw-survey: map what exists, so no agent reinvents it

You are building the code-awareness layer (SPEC §7a, ADR-014) for ONE project.
Two artifacts: **capability cards** (what reusable things exist, where, what
for) and **map.md** (how the project hangs together). The consumer is an agent
in a DIFFERENT project months from now, asking "do I already have something
that does X?" — or an agent newly arrived in THIS project needing orientation
in 60 seconds.

The prime directive: **pointer, never copy.** You index that code exists and
what it's for — never how it works. A future agent must read the live file
before reusing anything; your card is the signpost, the repo is the truth.
This is why this layer beats llm-wikis: descriptions of implementations rot
silently; pointers fail loudly (drift-lint catches dead paths).

Command prefix: `AW="aw"`

## What to read (and what never to read)

READ: entry points (main/index/cli files), package manifests (package.json,
pyproject.toml — scripts + deps), public API surfaces (exports, `__init__`,
route tables, toolspecs), schema files (SQL, prisma, dataclasses used as
contracts), README/CLAUDE.md/docs headings, config SHAPES (keys, never values).

DO NOT READ: function bodies beyond their signature + docstring, test
internals, vendored code, node_modules/.venv, anything gitignored. If you find
yourself understanding an algorithm, you've read too deep — back out.

SECRETS: config/env files may contain credentials. Never quote values from
them — shapes only ("takes SMTP creds via env"). One leaked value poisons the
whole corpus's trustworthiness.

## Capability cards — what earns one

A capability = a solved, working, reusable thing: a module, pipeline, schema,
design system, prompt, integration. The bar is **would a future agent in
another project want to know this exists before building?**

- YES: "Battle-tested Smartsheet chunked writer — handles 4k-char truncation +
  429 backoff" (files: [src/lib/smartsheet.ts])
- YES: "Editorial minimalist UI token system — warm monochrome + pastel badges,
  single CSS file" (files: [src/styles/tokens.css])
- YES: "ULID generator, stdlib-only, Crockford base32" — small but exactly what
  gets reinvented.
- NO: "utils.ts has helper functions" (not a capability, a rumor)
- NO: half-finished experiments, dead branches, anything you wouldn't
  recommend an agent build on. A capability card is a recommendation.

Card contract (schema enforced; violations get rejected in review):
- `type`: "capability" · `files`: ≥1 repo-relative path — MANDATORY; a
  capability without a path is a rumor.
- Every public-surface name you put in the body must be reachable from a path
  in `files` — if you name `foo()`, the file that defines or re-exports `foo`
  belongs in `files`. A named symbol whose file you didn't list turns the
  pointer into a dead end for the grep-from-card reader.
- title states what it does + its distinguishing strength, searchable by NEED:
  a future agent queries "rate limit retry smartsheet", not your module name.
  Include the module name too — both query directions matter.
- body ≤120 tokens: purpose, public surface (names/signatures ONLY), maturity
  signal ("in production since…", "covered by tests", "used by 3 pages"), and
  end with: `**Reuse:** read the file first — adapt current code, never this
  description.`
- NO code blocks, NO implementation logic in the body. If the body teaches how
  it works, delete the how.
- `confidence`: 0.75 shipped/tested code · 0.65 works but unpolished · below
  that, don't propose.
- `sources`: `[{"path": "<the file itself>", "kind": "schema"}]`.

Propose via review queue (never write the wiki directly):

```bash
echo '{"type": "capability", "title": "...", "project": "<slug>", "body": "...", "files": ["src/lib/x.ts"], "sources": [{"path": "src/lib/x.ts", "kind": "schema"}], "tags": [], "confidence": 0.75, "reason": "code-survey"}' | $AW queue
```

Dedupe first: `$AW recall "<need-shaped tokens>" --all` — same capability
already carded → skip; carded but paths moved → propose with reason
`"survey-refresh"` naming the old card id in the body.

Volume: ≤15 per project. A thin project yields 2 and that is a correct result.
Rank by reuse likelihood; drop the tail. 8 sharp signposts beat 15 vague ones.

## map.md — the orientation page

Write DIRECTLY (not queued — maps are regenerable artifacts, not cards) to
`~/knowledge/projects/<project>/map.md`, overwriting any previous map:

```markdown
# <project> — map
<!-- generated: <YYYY-MM-DD> · repo: <git short-hash or "no-git"> · survey: v1 -->

## Purpose
<2–3 sentences: what this project is FOR and who uses it.>

## Stack & entry points
- <runtime/framework> — entry: `<path>` <one clause on what starts here>

## Modules
| path | role | key exports |
|---|---|---|
| src/lib/x.ts | <5-word role> | writeRows, validateCells |

## Data flow
<≤10 lines: where data enters, what transforms it, where it lands. Name files.>

## Invariants
- <constraint an agent must not break> (`<path>`)

## Interfaces
- <external system / sibling project it talks to, and through what>
```

Hard limits: ≤150 lines total; EVERY claim path-anchored (a line that can't
name its file doesn't ship); names and shapes only, no implementation prose;
the stamp line is mandatory — readers judge staleness by it.

## Discipline

- One project per run. Coverage across repos = one survey agent per project,
  in parallel — never widen a single run (same reason as aw-sweep: fresh
  context per repo, correct project slugs, reviewable batches).
- Re-survey (drift-lint flagged dead paths, or post-refactor): regenerate
  map.md fully; propose only NEW/CHANGED capabilities; name superseded card
  ids in your report.
- Don't stop to ask; safest assumption, noted in your report.

## Done contract

Report: capability cards proposed (title + files each), map.md line count +
stamp, skipped candidates (with the one-line reason), dedupe hits. Remind the
user: review the queue with `afterwit ui` (a=approve e=edit r=reject).
