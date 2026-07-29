# afterwit Manifesto

**Generated**: 2026-07-05
**Source documents**: docs/research/afterwit-{theory,domain,benchmarks}.md + lead's own research (Karpathy gist, Chroma context-rot study, Anthropic agentic-search stance, claude-self-reflect / llm-wiki / openwiki / Zep-Graphiti architectures)

The non-negotiable principles for building afterwit. Implementing agents read this before writing code. Reviewers score against it.

## The Principles

### 1. Distill, don't hoard

The unit of storage is the **knowledge card** — a distilled, self-contained fact (decision + rationale, error→fix pair, gotcha, preference, project fact, reusable snippet) with provenance. Raw transcripts are kept immutable as source, but retrieval NEVER serves raw transcript chunks.

**Right**: "ADR: acme_hr uses JSONB for audit payloads because schema churns weekly — superseded 2026-05-12 by typed columns (perf)." (card, 40 tokens, provenance link)
**Wrong**: injecting a 2,000-token transcript excerpt where that decision was discussed across 14 messages.

### 2. Code is searched live, knowledge is indexed

Never embed/index code bodies for retrieval — Anthropic removed vector search from Claude Code because agentic grep outperformed it ("by a lot"; Amazon AAAI 2026: agentic keyword search ≈94.5% of RAG faithfulness, zero infra). Harnesses already grep code brilliantly. Index only what grep CANNOT find: knowledge that lives in no file — decisions, rationale, error→fix history, cross-project patterns, DB schemas.

**Right**: MCP tool `why_decision("JSONB audit")` → card with rationale.
**Wrong**: a Qdrant index of every function in 19GB of projects.

### 3. Injection is a scalpel, not a firehose

Chroma's context-rot study: topically-related-but-wrong context (distractors) measurably degrades model output, and the effect compounds. Proactive per-prompt injection is capped (≤3 cards, ≤600 tokens), similarity-thresholded (silence beats noise), and labeled with provenance. Everything bigger goes through on-demand MCP tools the agent calls when IT decides it needs history.

**Right**: hook injects nothing on "fix this typo".
**Wrong**: hook injects 5 "possibly related" memories on every prompt.

### 4. Markdown wiki is the source of truth; the database is a rebuildable cache

Karpathy's LLM-wiki pattern: three layers — immutable raw sources → LLM-maintained markdown wiki (entity/concept/project pages, wikilinks, frontmatter) → schema doc. Human-auditable, git-diffable, Obsidian-compatible, editable by any agent. SQLite (FTS5 + vectors + link graph) is derived; `afterwit index --rebuild` regenerates it from markdown at any time. No opaque graph DB.

**Right**: `wiki/projects/acme_hr/decisions.md` under git; SQLite deleted → rebuilt losslessly.
**Wrong**: knowledge living only in Neo4j where nobody can review or diff it.

### 5. Facts have lifecycles (temporal supersede)

Decisions get reversed; fixes get obsoleted. Every card carries `status: active|superseded|deprecated` and `superseded_by`. Ingestion detects contradictions with existing cards and supersedes rather than duplicates (Zep/Graphiti bi-temporal insight). Retrieval defaults to active facts; history is reachable on request.

**Right**: "moved from JSONB to typed columns" → old card marked superseded, linked.
**Wrong**: both "use JSONB" and "never use JSONB" served side by side.

### 6. Provenance or it didn't happen

Memory poisoning is OWASP ASI06; MINJA shows >95% injection success against production agents. Every card cites source (session file + line range, or doc path). LLM-extracted cards land in a review queue below a confidence threshold; nothing silently becomes "trusted fact". A `feedback` tool lets agents/user mark cards wrong — marked cards are quarantined, not deleted.

### 7. One backbone, two harnesses

A single MCP server serves Claude Code and Codex. Harness-specific parts are thin adapters: Claude Code hooks (SessionStart, UserPromptSubmit) and generated CLAUDE.md pointers; Codex config.toml MCP registration, AGENTS.md pointers, and hooks (stable since v0.124). No logic lives in adapters.

### 8. Incremental everything

Ingestion is checkpointed (file + offset), idempotent, and resumable. Re-running any command is safe. New sessions are distilled on a schedule (nightly), not by reprocessing 715MB each time. Content hashing skips unchanged sources.

### 9. It must prove helpful (measured, not assumed)

Log every injection and every MCP retrieval. A consolidation pass mines later transcripts to check whether served cards were actually used. Cards accrue usefulness scores; unused cards decay in rank (never silently deleted). If injection can't demonstrate value, it gets turned off — the user asked for helpfulness, not ceremony.

### 10. Scale honestly

Corpus: ~72 sessions (~900MB raw → a few thousand cards), 441 docs, 95 memories, 16 projects. This is SQLite + files territory. stdlib + FTS5 first; local embeddings (fastembed, 384-dim) as the second stage; rerankers/graph databases only if measured retrieval quality demands them.

## Anti-patterns (banned)

- **Embedding the codebase** — harnesses grep better; index maintenance burden with no lift (Principle 2).
- **Raw-chunk RAG over transcripts** — serves noise, triggers context rot (Principles 1, 3).
- **Unbounded proactive injection** — distractors degrade output; caps are hard limits (Principle 3).
- **Opaque storage as source of truth** — un-reviewable memory is a poisoning vector and kills trust (Principles 4, 6).
- **Append-only memory** — without supersede/decay the store rots into contradictions (Principle 5).
- **Auto-trusting LLM extractions** — hallucinated "facts" compound forever (Principle 6).
- **Full reprocessing pipelines** — 715MB re-reads make the nightly job unaffordable (Principle 8).
- **Heavy infra at personal scale** — Qdrant/Neo4j/Kafka for a few thousand cards (Principle 10).

## How to apply

1. Implementing agents read this file before writing code.
2. Every retrieval-path PR states which principle it serves.
3. Reviewer scores implementation against the 10 principles at wave end; failures don't ship.
