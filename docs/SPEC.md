# afterwit — System Specification

**Version**: 0.2
**Date**: 2026-07-05
**Status**: Design complete — research integrated (see `docs/research/`)
**Governing doc**: `docs/research/afterwit-MANIFESTO.md` (10 principles — binding)

---

## 1. Purpose

Every Claude Code / Codex session produces paid-for intelligence — decisions, bug fixes, gotchas, preferences — that currently evaporates when the session ends. afterwit converts that exhaust into a durable, compounding knowledge base and feeds it back into both harnesses:

- **Proactively**: tiny, high-precision context injection via hooks (per prompt / per session).
- **On demand**: MCP retrieval tools agents call when they decide they need history.
- **Statically**: generated, managed blocks in CLAUDE.md / AGENTS.md pointing at the system.

Sources ingested: Claude Code session JSONL, Codex session JSONL, existing memory files, all project `docs/`, project database schemas, and (as sources of truth to search live, not index) the codebases themselves.

## 2. Non-goals

- **No codebase embedding/indexing.** Harnesses grep live code better than any index (Manifesto P2). Precisely: no code *bodies* are ever stored, embedded, or served. The existence, location, and purpose of code IS indexed — see §7a (capability cards + project maps); the distinction is pointer-vs-copy, and it is load-bearing: pointers degrade gracefully (drift-lint catches dead paths), copies go stale silently.
- **No raw-transcript RAG.** Retrieval serves distilled cards only (P1, P3).
- **No multi-user/server deployment.** Single machine, localhost only.
- **No graph database.** The graph is wikilinks + a SQLite edge table (P4, P10).
- **No real-time ingestion.** Sessions distill on schedule; live session context is the harness's job.

## 3. Data inventory (measured 2026-07-05)

| Source | Location | Volume |
|---|---|---|
| Claude sessions | `~/.claude/projects/<slug>/<uuid>.jsonl` | 24 top-level files (~249MB, median 3MB) + ~1000 nested subagent logs (715MB tree). **83% of bytes = large `tool_result` payloads inside `user` records**; `file-history-snapshot` is only ~0.5% |
| Codex sessions | `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` | 48 files, ~159MB (median 0.43MB, max 69MB) |
| Memory files | `~/.claude/projects/*/memory/*.md` | ~90 files, typed frontmatter (`feedback`/`project`/`user`/`reference`/`memory`) — this format is the card contract's ancestor |
| Codex history | `~/.codex/history.jsonl` | 196 prompts |
| Project docs | `~/Desktop/Projects/*/docs/**/*.md` | 441 files, 16 projects |
| Codebases | `~/Desktop/Projects/` (19GB) | searched live, never indexed |
| Databases | per-project (connection strings in config) | schemas only |

Claude JSONL record types (sampled): `user`, `assistant`, `attachment`, `file-history-snapshot`, `ai-title`, `last-prompt`, `mode`, `permission-mode`, `queue-operation`. Messages carry `tool_use` / `tool_result` blocks; `tool_result.is_error` marks failures; sidechains hold subagent transcripts.

Codex JSONL record types (sampled): `session_meta` (cwd, model, instructions), `response_item`, `event_msg`, `turn_context`.

Scale verdict: a few thousand distilled cards. SQLite + markdown files. No heavier infra permitted without measured need (P10).

## 4. Architecture

```
                        ┌─────────────────────────────────────────────┐
 SOURCES                │  INGEST (adapters, checkpointed, redacting) │
 ~/.claude/projects ───▶│  claude_jsonl │ codex_jsonl │ memory_md     │
 ~/.codex/sessions  ───▶│  docs_md      │ db_schema                   │
 project docs/      ───▶└──────────────┬──────────────────────────────┘
 DB conn strings ───────┘              │ normalized "events"
                                       ▼
                        ┌──────────────────────────────┐
                        │  DISTILL (LLM, batched)      │
                        │  events → knowledge cards    │
                        │  dedupe · supersede · queue  │
                        └──────────────┬───────────────┘
                                       ▼
        ┌───────────────────────────────────────────────┐
        │  KNOWLEDGE WIKI  ~/knowledge/  (git, markdown)│  ← source of truth
        │  cards + concept pages + project briefs       │
        └──────────────┬────────────────────────────────┘
                       ▼ (derived, rebuildable)
        ┌───────────────────────────────────────────────┐
        │  INDEX  ~/.afterwit/index.db  (SQLite)  │
        │  FTS5 · embeddings · links · usage · queue    │
        └───────┬──────────────────────┬────────────────┘
                ▼                      ▼
   ┌─────────────────────┐   ┌──────────────────────────┐
   │ MCP SERVER (stdio)  │   │ HOOK CLIENT  `afterwit inject` │
   │ recall/why/lookup…  │   │ ≤3 cards, ≤600 tok, thr. │
   └──────┬───────┬──────┘   └──────┬───────────┬───────┘
          ▼       ▼                 ▼           ▼
     Claude Code  Codex        UserPromptSubmit SessionStart
                              (both harnesses; Codex hooks ≥ v0.124)

   NIGHTLY CONSOLIDATION: ingest → distill → merge → index →
   lint → usage mining → decay → regenerate briefs & managed blocks
```

Single Python package `afterwit` (Python 3.12, `uv`). Long-running components: none required — MCP server spawned by harness (stdio), hook client is a short-lived CLI, consolidation is a systemd user timer.

## 5. Storage

### 5.1 Knowledge wiki (source of truth)

`~/knowledge/` — a git repository, Obsidian-compatible.

```
~/knowledge/
  schema.md                  # wiki's own rules: card types, naming, frontmatter contract
  index.md                   # generated catalog (per-category one-liners)
  log.md                     # append-only operation journal
  global/
    preferences.md           # user preferences (single page, sectioned)
    patterns/<slug>.md       # cross-project patterns/concepts
  projects/<project>/
    brief.md                 # generated project brief (see §9.3)
    decisions/<slug>.md      # one card per file
    gotchas/<slug>.md
    errors/<slug>.md         # error→fix cards
    facts/<slug>.md
    snippets/<slug>.md
    db/<dbname>.md           # database schema cards
  review/                    # cards pending human approval (below confidence bar)
```

**Card = one markdown file.** Frontmatter contract:

```markdown
---
id: 01J8ZK...            # ULID, stable
type: decision | gotcha | error_fix | preference | fact | snippet | concept | db_schema | doc_ref | capability
title: JSONB for audit payloads
project: acme_hr   # or "global"
status: active | superseded | deprecated | quarantined
superseded_by: <id>        # optional
tags: [postgres, audit]
files: [src/audit/store.ts]  # code paths this knowledge concerns (enables for_file lookup)
confidence: 0.9            # extractor confidence
sources:
  - path: ~/.claude/projects/-home-...-acme_hr/58d0....jsonl
    lines: 1042-1101
    harness: claude          # optional: claude | codex | doc | db | memory
    model: claude-opus-4-8   # optional: model that produced the resolving turn (from JSONL message.model)
    kind: assistant          # optional origin: user | assistant | thinking | doc | schema
created: 2026-07-05
verified: false            # flips true on human approval or feedback(+)
usefulness: 3.5            # optional usage checkpoint (ADR-008); absent until earned
last_used: 2026-07-01T...  # optional, with usefulness
source_commit: 9aed852...  # optional commit the claims were true at (ADR-018); drift anchor
repo_url: https://github.com/o/r  # optional normalized origin (ADR-020); the CROSS-DEVICE key
---
Body: the fact itself, ≤300 tokens. For error_fix: fenced error signature block,
then the fix. Wikilinks [[like-this]] to related cards.
**Why:** rationale (for decisions).
```

Rationale: one-file-per-card gives trivial incremental indexing, clean git diffs, Obsidian graph view for free, and lets any agent edit knowledge with Write/Edit (P4).

### 5.2 Index database (rebuildable cache)

`~/.afterwit/index.db` (SQLite, WAL). `afterwit index --rebuild` regenerates it from the wiki losslessly.

```sql
CREATE TABLE cards(
  id TEXT PRIMARY KEY, type TEXT, title TEXT, body TEXT,
  project TEXT, status TEXT, confidence REAL, verified INTEGER,
  created TEXT, updated TEXT, superseded_by TEXT,
  usefulness REAL DEFAULT 0,       -- feedback-driven score (§10)
  files TEXT,                      -- JSON array from frontmatter files: (for_file lookup)
  last_used TEXT, path TEXT);
CREATE VIRTUAL TABLE cards_fts USING fts5(title, body, tags, content=cards);
CREATE TABLE embeddings(card_id TEXT PRIMARY KEY, vec BLOB);   -- 384-d f32
CREATE TABLE links(src TEXT, dst TEXT, kind TEXT);             -- from wikilinks
CREATE TABLE checkpoints(source TEXT PRIMARY KEY, mtime REAL,
  bytes_done INTEGER, content_hash TEXT);                      -- ingest resume points
CREATE TABLE servings(id INTEGER PRIMARY KEY, ts TEXT, harness TEXT,
  session_id TEXT, mode TEXT,      -- 'inject' | 'mcp'
  query TEXT, card_ids TEXT, outcome TEXT);                    -- outcome mined later (§10)
```

Embedding search is brute-force cosine over ≤10k vectors (numpy, milliseconds). No ANN index until cards exceed ~50k (P10).

## 6. Ingestion

`afterwit ingest [--source claude|codex|memory|docs|db|all]` — incremental, idempotent, resumable.

Per-adapter contract: read source → emit normalized **events** `{source_path, lines, project, ts, role, kind, text, meta}`. Checkpoint after each file (mtime + byte offset + hash); unchanged files skip.

**Redaction (before anything is persisted)**: regex scrub of API keys/tokens/JWTs/private-key blocks/passwords in URLs (patterns from gitleaks default set). Redacted spans become `[REDACTED:<type>]`.

### 6.1 claude_jsonl adapter
- Keep: real `user` messages (exclude `<local-command-caveat>` synthetic / `isMeta`), `assistant` text blocks, `tool_use` name+key args (truncated), `tool_result` with `is_error: true` or non-empty `toolUseResult.stderr` (full), **`isCompactSummary` records (pre-distilled session summaries — highest-ROI input)**, `ai-title`, `cwd`/`gitBranch` → project+branch attribution, `parentUuid` chain for turn threading.
- Drop: **successful `tool_result` payloads over 500 chars — this is 83% of all bytes**; `attachment` records; `file-history-snapshot` (cheap ~0.5%, drop anyway); `mode`/`permission-mode`/`queue-operation`/`last-prompt`.
- Nested subagent logs (~1000 files under `<uuid>/` dirs) and `isSidechain:true` turns: ingest with `meta.sidechain=true`, lowest distill priority (summaries of their findings already surface in the parent transcript).

### 6.2 codex_jsonl adapter
- `session_meta` → project (cwd), model; skip `base_instructions`.
- `event_msg`: keep `user_message` + `agent_message`; drop `token_count` (heavy churn), `turn_aborted` turns.
- `response_item`: keep `message`, `function_call` + `function_call_output` (errors full, successes truncated), `reasoning` (Codex chain-of-thought — unique decision-rationale signal, keep truncated).
- **`compacted.replacement_history` — pre-digested summaries, mine first** (Codex twin of `isCompactSummary`).
- `~/.codex/history.jsonl` → prompt log events (cheap signal for "what user works on").

### 6.3 memory_md adapter
Existing 95 memory files already ARE cards. Parse frontmatter (`name`, `description`, `metadata.type`), map to card types (`user→preference`, `feedback→preference`, `project→fact`, `reference→doc_ref`), convert in place to card contract, keep originals untouched; wiki copy becomes canonical.

### 6.4 docs_md adapter
441 project docs are human/agent-authored — already distilled. No LLM pass. Each doc → one `doc_ref` card: title, path, headings outline, first paragraph, extracted wikilink-able entities. ADR files (`docs/ADR.md`, `DECISIONS.md`) get section-level extraction → `decision` cards with `sources: {path, heading}`. Retrieval returns the pointer + outline; the agent Reads the real file (code-search principle applied to docs).

### 6.5 db_schema adapter
Config lists connections (`~/.afterwit/config.toml`):
```toml
[[databases]]
project = "acme_hr"
url = "postgresql://..."        # or sqlite path
```
Introspect: tables, columns+types, FKs, row counts, low-cardinality column value samples (enums). Emit one `db_schema` card per table group (≤300 tokens) + a per-DB overview card. Never ingest row data (privacy + staleness). Re-run diffs schema hash; changed tables refresh their cards.

## 7. Distillation (events → cards)

`afterwit distill [--budget N]` — LLM pass over undistilled session events, batched per session.

- **Driver A (default)**: `claude -p` headless with a fixed extraction prompt — reuses the existing subscription, zero marginal cost.
- **Driver B**: Anthropic Batch API (`claude-haiku-4-5`, 50% batch discount) for backlog blasts.

Extraction prompt (fixed, versioned in repo) instructs: given a compacted session transcript, emit JSON array of candidate cards `{type, title, body, why, tags, confidence, source_lines}` for ONLY:
1. **Decisions with rationale** — "we chose X over Y because Z".
2. **Error→fix pairs** — `tool_result.is_error` (or non-zero exit) followed by a successful retry; capture error signature + the fix that worked.
3. **User corrections/preferences** — "no, actually…", "always…", "never…", repeated style demands.
4. **Gotchas/invariants** — surprising behavior, workarounds, "turns out that…".
5. **Reusable snippets** — non-trivial code the user approved, with its purpose.

Hard rules in prompt: no speculation; every card must cite line ranges; skip anything already obvious from the code; ≤300-token bodies. **Extract-on-resolution**: promote a card only when the transcript shows the outcome stuck — a passing test/command, a merged edit, or explicit user confirmation. "I think the bug is X" without resolution never becomes a card (kills transcript-replay entrenchment, the failure mode where agents re-ingest their own past wrong guesses).

Processing order (ROI-sorted): compaction summaries (`isCompactSummary`, Codex `compacted`) first — they are already distilled; then error→fix pair candidates (mechanically pre-detected from `is_error` chains, LLM only confirms/writes); then full-transcript sweep, newest sessions first.

Card types map onto the CoALA memory taxonomy (episodic/semantic/procedural — the frame Letta/Mem0/Zep converge on): `decision`/`fact`/`preference` = semantic, `error_fix`/`snippet`/`gotcha` = procedural, raw transcripts stay episodic (by-provenance access only, never served as chunks).

**Post-processing (deterministic code, not LLM):**
1. **Dedupe**: embedding similarity ≥0.92 against existing same-project cards → merge (union sources, keep newer body).
2. **Supersede**: similarity 0.75–0.92 + contradiction heuristic (LLM yes/no on the pair) → mark old card `superseded`, link (P5).
3. **Gate**: confidence ≥0.8 → wiki as `verified: false` active card; <0.8 → `review/` queue. `afterwit review` = interactive approve/edit/reject.
4. Wiki write + `log.md` append + index update.

## 7a. Code awareness: capability cards + project maps (ADR-014)

Distillation (§7) captures *what happened*. This layer captures *what exists* — and it exists because of a failure mode nothing else covers: **an agent cannot grep what it does not know exists.** Within one repo, live exploration beats any index; across N repos, an agent starting fresh in project A silently reinvents what project B solved. LLM-wikis attack this by describing code and inherit the staleness treadmill. We attack it with two artifacts that never copy code:

### 7a.1 Capability cards (the reuse layer)

A `capability` card asserts that a reusable, working solution exists, where it lives, and what it is for — never how it is implemented.

```markdown
---
id: <ulid>
type: capability
title: Battle-tested Smartsheet chunked writer — 4k-char truncation + 429 backoff
project: smartsheet-manufacturing-ops
files: [src/lib/smartsheet.ts]          # ≥1 path REQUIRED — a capability without a path is a rumor
tags: [smartsheet, rate-limits, io]
confidence: 0.75
sources: [{path: src/lib/smartsheet.ts, kind: schema}]
---
Chunks writes to respect the 4000-char cell limit, retries 429 with exponential
backoff, validates nothing was silently truncated. Public surface:
`writeRows(sheet, rows, opts)`. In production use since 2025-11.
**Reuse:** read the file first — adapt current code, never this description.
```

Contract (all enforced by the survey skill, spot-checked by audit):
- **Pointer, not copy.** Body ≤120 tokens: purpose, public surface (names/signatures at most), maturity signal. NEVER implementation logic, algorithms, or code blocks — if the body teaches how it works, it is wrong.
- **Path-anchored.** `files` non-empty; every claim must survive the reader opening those paths. Dead paths are caught by the existing code-drift lint (§10) and trigger re-survey, not deletion.
- **Reusable means proven.** Only code that demonstrably works (shipped, tested, or long-lived) earns a card. Half-finished experiments do not — a capability card is a recommendation.
- **No secret shapes.** Survey reads configs/schemas for existence, never values. A capability card body must pass the same redaction bar as any card.
- **Capability ≠ snippet.** `snippet` stores small approved code verbatim (≤40 lines, self-contained); `capability` points at living code of any size. When in doubt: if it needs the repo to run, it is a capability.

Serving: capability cards are ordinary cards — indexed, ranked, cross-project by design (recall's +0.15 project boost, not a filter), usefulness-mined, review-gated. The discovery moments are already wired: `recall` before building anything (aw-knowledge trigger #2), `type=capability` filter on the recall tool, session-start brief.

### 7a.2 Project maps (the orientation layer)

One generated page per project: `projects/<project>/map.md`. This is the single llm-wiki artifact worth having — the index page — without the page-per-concept sprawl that rots fastest.

```markdown
# <project> — map
<!-- generated: 2026-07-06 · repo: a3ecc63 · survey: v1 -->
## Purpose            (2–3 sentences: what this project is FOR)
## Stack & entry points  (bullets; every item path-anchored)
## Modules            (table: path | role | key exports — names only)
## Data flow          (≤10 lines: where data enters, transforms, lands)
## Invariants         (constraints an agent must not break, path-anchored)
## Interfaces         (what other projects/systems this talks to)
```

Contract:
- **≤150 lines. Every claim path-anchored.** A map line that can't name its file doesn't ship.
- **Names and shapes, never bodies.** Signatures and module roles, no implementation prose.
- **Stamped**: generation date + repo commit hash in the header comment. A reader (human or agent) can judge staleness at a glance.
- Maps are **regenerable artifacts, not cards**: no card frontmatter, excluded from the card index (like `brief.md`), served verbatim by `project_brief` alongside the knowledge brief. Source of truth for maps is the generator, not the file — hence no provenance/review cycle; trust comes from path-anchoring + the stamp.

### 7a.3 Survey process & refresh model

`aw-survey` skill (agent-driven, one project per run — same quality discipline as aw-sweep): reads the repo's *structure* — entry points, exports, public APIs, schema files, README/CLAUDE.md — never implementation internals beyond signatures. Emits capability card proposals via `afterwit queue` (review-gated like everything) and writes/overwrites `map.md` directly (regenerable artifact). Volume: ≤15 capability cards per project; a thin project yields 2 and that is correct.

Refresh is event-driven, not a treadmill:
1. **Drift-triggered**: code-drift lint (§10) flags a capability card whose paths all vanished → re-survey that project.
2. **Manual**: `/aw-survey <project>` after a large refactor.
3. Never scheduled-regenerate-everything — that is the llm-wiki disease this design exists to avoid.

### 7a.4 Why this beats an llm-wiki for agents (falsifiable claims)

1. Cross-project by construction (wikis are single-repo).
2. Claims degrade gracefully: a stale pointer is detectably dead (drift-lint); a stale description is silently wrong.
3. Agent always reads current source before reuse — served content can't inject stale code.
4. Same trust machinery as all knowledge: review gate, provenance, usefulness mining. If capability cards don't get used, their scores fall and the kill-switch/eval story reports it — the claim is measured, not asserted.

## 8. Retrieval

One ranking function used by both MCP tools and hook injection:

```
score = 0.5·bm25_norm + 0.3·cosine + 0.1·recency_decay(updated, τ=90d)
      + 0.1·usefulness   [× 0 if status ≠ active]  [× 0.5 if verified = false]
```

- BM25 via FTS5; cosine via fastembed `all-MiniLM-L6-v2` (384-d, local, no API).
- Project scoping: same-project cards get +0.15; `global` cards always eligible.
- Threshold: results below absolute floor (tuned on golden set, §12) are dropped — returning nothing is a first-class result (P3).
- Phase 1 ships BM25-only; embeddings land in Phase 4. Weights re-tuned on the golden set when embeddings arrive.
- Conditional upgrades, only if `afterwit eval` gates fail at P4: cross-encoder reranker over top-20 (two-stage beats single-stage by double-digit Recall@5 in the literature), HyDE-style query expansion for terse agent queries. Not shipped by default (P10 — scale honestly).

## 9. Serving

### 9.1 MCP server — `afterwit serve-mcp` (stdio; registered in both harnesses)

| Tool | Args | Returns |
|---|---|---|
| `recall` | query, project?, type?, k=5 | ranked cards (title, body, status, provenance) |
| `why` | topic, project? | decision cards + rationale + supersede chain |
| `lookup_error` | error_text, project? | error_fix cards matched on error signature (FTS on fenced error block, boosted) |
| `project_brief` | project | brief.md content |
| `related` | card_id | link-graph neighbors (1 hop) |
| `for_file` | path, project? | cards whose `files:` touch this path — "what do we know about this file" (decisions made in it, times it broke, fixes). No competitor joins code↔transcript knowledge; this is the join |
| `save_insight` | type, title, body, tags | writes card via review gate (confidence=agent-supplied, capped 0.79 → always queued) |
| `feedback` | card_id, verdict: helpful\|wrong\|stale | updates usefulness; `wrong` → quarantine |

Tool descriptions explicitly tell agents *when* to call (e.g. `lookup_error`: "call when a command/test fails with an unfamiliar error — returns fixes that worked before in this or other projects"). Discovery beats injection for bulk knowledge (P3, P7).

`save_insight` is always review-gated: agents cannot mint trusted facts (P6, anti-poisoning).

### 9.2 Hooks

**Claude Code** (`~/.claude/settings.json`, installed by `afterwit install claude`):
- `SessionStart` → `afterwit inject --mode session --cwd $CWD`: emits project brief digest (≤300 tokens): active decision count, top-3 gotchas by usefulness, "deep history: use recall/why/lookup_error/for_file MCP tools". Cap: **10k chars** (harness limit; overflow spills to file).
- `UserPromptSubmit` → `afterwit inject --mode prompt`: reads prompt JSON from stdin, hybrid-retrieves, emits **≤3 cards, ≤600 tokens total, threshold-gated — usually emits nothing**. This is the only true per-prompt push channel in either harness (10k-char cap, 30s timeout). Format per card: one line title + body + `(source: <project>/<type>, <date>; verify before relying)`.
- `SessionEnd`/`Stop` → `afterwit enqueue-session <transcript_path>`: appends to distill queue file. No processing at hook time (sleep-time extraction — Letta pattern; never on hot path).
- Latency budget for inject: **p95 < 200ms** (SQLite read-only + FTS; embeddings loaded lazily only if index has them precomputed — query embedding via cached ONNX session, ~30ms).

**Codex** (`afterwit install codex`): registers `[mcp_servers.afterwit]` plus `[[hooks.SessionStart]]` and `[[hooks.UserPromptSubmit]]` in `~/.codex/config.toml`, and the `[hooks.state.…]` trust entries those hooks need to run at all. Codex hook payloads are identical to Claude Code's and both events render context, verified end-to-end on codex-cli 0.145.0 (ADR-040) — so Codex is a first-class push surface, not a bonus. Its `PostToolUse` is **not** used: it fires on success and failure alike with no exit code (Gotcha #65), so error lookup stays a pull tool there.

### 9.3 Managed static blocks

`afterwit sync-briefs` (part of nightly) regenerates:
- `projects/<p>/brief.md` in the wiki.
- A fenced managed block (`<!-- afterwit:begin -->…<!-- afterwit:end -->`) in each project's `CLAUDE.md`: 5-10 lines — top active decisions, top gotchas, MCP tool usage hints. Never touches content outside the fence, and respects the ~200-line total-file guidance (models follow ~150-200 instructions reliably).
- **One block serves both harnesses**: this machine's Codex already reads `CLAUDE.md` via `project_doc_fallback_filenames = ["CLAUDE.md", ".claude/CLAUDE.md"]` (and `~/.codex/AGENTS.md` symlinks to `~/.claude/CLAUDE.md`). Separate `AGENTS.md` blocks only written where a project has its own `AGENTS.md`.

**Optional (P5) — per-project knowledge-pack skills**: a generated SKILL.md per project (name+description ≈30-50 idle tokens, body loaded only on description match) carrying the stable distilled pack. Progressive disclosure makes this the cheapest always-available surface; ship only if brief-block + MCP prove insufficient.

## 10. Feedback & consolidation (the "prove helpful" loop)

Nightly systemd user timer → `afterwit consolidate`:
1. `ingest` + `distill` new sessions (queue from §9.2).
2. **Usage mining**: for each `servings` row, inspect the subsequent transcript window — was a served card's content echoed/acted on, or did the agent call `feedback`? Update `usefulness` (+1 used, −0.2 served-and-ignored, hard quarantine on `wrong`).
3. **Decay**: cards unserved+unused 180d → rank penalty (never deleted; P9).
4. **Lint** (Karpathy): orphan cards, broken wikilinks, contradiction candidates, stale `verified:false` older than 30d → `review/` + `log.md`.
5. Regenerate `index.md`, briefs, managed blocks.
6. Emit `afterwit stats` digest: cards by type/status, injection hit-rate, top/bottom usefulness. **Kill-switch rule**: if 30-day injection hit-rate <20%, disable `UserPromptSubmit` injection automatically and log it (P9 — helpfulness is measured, not assumed).

## 11. Security & privacy

- Everything local; MCP over stdio; no network listeners.
- Redaction at ingest (§6); `afterwit doctor --scan-secrets` audits the wiki with gitleaks patterns.
- Wiki is a private git repo; remote push is user-opt-in only.
- Poisoning surface: all LLM/agent writes review-gated (§7, §9.1); provenance mandatory; quarantine instead of delete preserves forensics (OWASP ASI06).

## 12. Evaluation

`eval/golden.yaml`: ~40 hand-written queries with expected card hits, built from known history (e.g. "why JSONB in acme_hr audit", "playwright timeout fix", "user's commit message format"). `afterwit eval` reports recall@3, MRR, and false-positive rate on 10 no-answer queries (must return nothing). Gates: recall@3 ≥ 0.7, no-answer precision ≥ 0.9 before enabling prompt injection. Re-run on every ranking change.

Code-awareness queries (§7a) get their own golden entries once surveys land: need-shaped hit queries ("chunked writer for smartsheet rate limits" → the capability card) plus no-answer traps for capabilities that don't exist ("kafka consumer" when no repo has one) — reinvention prevention is only real if both directions hold. Cards must never quote trap queries verbatim (Gotcha #10).

Public memory benchmarks (LoCoMo etc.) are explicitly NOT used — an independent audit found 6.4% of LoCoMo's answer key wrong and its LLM judge accepting 63% of intentionally vague answers; vendors dispute each other's scores. We evaluate on our own transcripts only; the north-star metric is **"did served knowledge prevent a re-solved bug / repeated mistake"** (measured via §10 usage mining).

## 13. CLI surface

```
afterwit init                 # scaffold ~/knowledge + config + index
afterwit ingest [--source X]  # incremental
afterwit distill [--budget N] [--driver claude-p|batch-api]
afterwit index [--rebuild]
afterwit recall "query" [-p project]      # human CLI search
afterwit review               # approve/reject queued cards
afterwit inject --mode prompt|session     # hook entrypoint
afterwit serve-mcp
afterwit install claude|codex # hooks + MCP registration + managed blocks
afterwit consolidate          # nightly composite
afterwit lint | stats | eval | doctor
```

## 13a. Package layout (fixed — implementers create files exactly here)

```
afterwit/
  pyproject.toml            # uv; deps: mcp, pyyaml; P4 adds fastembed. Nothing else without ADR.
  CLAUDE.md                 # agent contract (exists)
  prompts/distill.md        # versioned extraction prompt (exists — do not rewrite, only ADR'd edits)
  docs/                     # SPEC, ADR, research (exist)
  eval/golden.yaml          # golden queries (P1 seeds 10, grows to ~40 by P4)
  src/afterwit/
    __init__.py
    cli.py                  # argparse dispatch; every §13 command
    config.py               # ~/.afterwit/config.toml (tomllib); paths, databases, thresholds
    events.py               # Event dataclass: source_path, lines, project, ts, role, kind, text, meta
    redact.py               # secret patterns + home-path scrub. Runs in every adapter AND in
                            # cards.save() — the card, not the transcript, is what git push publishes (ADR-022)
    adapters/
      __init__.py           # adapter registry
      claude_jsonl.py       # §6.1
      codex_jsonl.py        # §6.2
      memory_md.py          # §6.3
      docs_md.py            # §6.4
      db_schema.py          # §6.5 (P5)
    distill.py              # session → cards via driver; drivers: claude_p (subprocess), batch_api
    postprocess.py          # dedupe / supersede / confidence gate (§7 post-processing)
    cards.py                # card contract: frontmatter parse/write, validation (reject if missing id/type/status/sources)
    gitmeta.py              # §8a: local-git identity/position/drift. No network, no GitHub API. Never imported by inject.
    review.py               # §9.2 auto-review: independent, non-authoring model clears cards (ADR-021)
    wiki.py                 # card files, index.md, log.md, briefs, managed CLAUDE.md blocks
    index_db.py             # SQLite schema (§5.2), incremental update, --rebuild
    rank.py                 # §8 scoring; single function used by MCP + inject
    inject.py               # hook entrypoint; fail-open (any exception → empty stdout, exit 0)
    mcp_server.py           # §9.1 tools via `mcp` SDK (stdio)
    consolidate.py          # §10 nightly composite
    evalx.py                # §12 harness
    install.py              # afterwit install claude|codex — settings.json / config.toml edits, managed blocks
  tests/                    # pytest; per-file fixtures; golden JSONL samples under tests/fixtures/
```

Ownership rule: whoever implements `foo.py` implements `tests/test_foo.py` in the same change. `inject.py` and `rank.py` are the latency/quality hot spots — smallest possible import graphs (no adapter/distill imports).

## 14. Implementation plan

| Phase | Delivers | Done when |
|---|---|---|
| **P1 Core loop** | scaffold, config, wiki init, claude_jsonl + memory_md adapters, distill (driver A), card writer, FTS5 index, `afterwit recall` | 24 Claude sessions distilled; recall answers 5 known-history questions from CLI |
| **P2 Serve** | MCP server (all read tools), codex_jsonl + docs_md adapters, `afterwit install` both harnesses | Claude Code and Codex both answer "why did we choose X in <project>" via MCP mid-session |
| **P3 Inject** | hook client, SessionStart + UserPromptSubmit, managed blocks, enqueue-on-stop | p95 <200ms; golden no-answer queries inject nothing |
| **P4 Learn** | embeddings + hybrid ranking, feedback tool, usage mining, nightly consolidation, decay, lint, kill-switch | `afterwit eval` gates pass; nightly runs green 7 days |
| **P5 Extend** | db_schema adapter, `save_insight`, review UX polish, stats digest | DB schemas queryable; poisoning test (malicious save_insight) lands in queue, not wiki |
| **P6 Code awareness** | §7a: `capability` type, aw-survey skill, map.md generation, drift-triggered re-survey, golden capability hit-queries | survey of all repos yields review-gated capability cards + stamped maps; `afterwit recall --type capability` answers "do I already have X"; eval gates stay green |

Each phase independently shippable; system is useful from P1 (CLI recall) onward.

## 15. Risks

| Risk | Mitigation |
|---|---|
| Distillation hallucinates facts | line-cited provenance mandatory, confidence gate + review queue, verified flag halves rank |
| Injection annoys > helps | hard caps, threshold, eval gate before enable, auto kill-switch (§10) |
| Codex hooks API drift | MCP + AGENTS.md path works without hooks; hooks are enhancement |
| Claude/Codex JSONL schema changes | adapters versioned, unknown record types logged + skipped, never crash |
| Backlog distillation cost | driver A uses subscription; `--budget` caps; newest-first order |
| Wiki sprawl/rot | nightly lint, supersede-not-duplicate, decay, single `schema.md` contract |
```
