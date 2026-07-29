# afterwit — Benchmarks Research
**Generated**: 2026-07-05
**Lens**: benchmarks

## Summary
- The memory market has split into three camps that rarely talk to each other: **agent-memory layers** (Zep, Mem0, Letta, Cognee) obsessed with graph-vs-vector and LoCoMo scores; **coding-tool built-ins** (Cursor/Windsurf memories, CLAUDE.md, AGENTS.md) that are plaintext-file-based and per-workspace; and **code-context engines** (Aider repo-map, Sourcegraph Cody, Continue.dev) where the industry is actively *abandoning* embeddings for agentic grep. No one product spans all three.
- The published benchmark numbers (LoCoMo 94%+ etc.) are largely discredited — an independent audit found 6.4% of the answer key wrong and the LLM judge accepting 63% of intentionally-vague answers; Zep and Mem0 publicly accused each other of misconfiguring the other's system. Treat any "SOTA memory" claim as marketing until reproduced in-repo.
- The real, repeated user complaints are not accuracy-on-benchmark but **irrelevant recall, stale/contradictory memories, memory poisoning, latency on the write path, and maintenance burden**. These are what afterwit must design against — the scoreboard is a trap.

## Product-by-product

### 1. Zep / Graphiti (temporal knowledge graph)
Graphiti is a bi-temporal knowledge-graph engine (Neo4j-backed): every edge carries validity intervals `(t_valid, t_invalid)` plus an ingestion timestamp, so conflicting facts *invalidate* old edges rather than deleting them — history is preserved and queryable "as of" a point in time. Extraction is LLM-driven entity/relationship resolution at write time; retrieval combines semantic + BM25 + graph traversal.
- **Right**: temporal invalidation is the single best answer to the stale-memory problem — you can model "user was on Python 3.10, now on 3.12" as two edges instead of one overwritten fact. Bi-temporal audit trail is genuinely differentiated.
- **Wrong**: heavy write-path cost (LLM extraction per episode), Neo4j operational burden, and its own benchmark claims are contested (see controversy below). Overkill for small personal corpora.
- **Claims**: 94.7% LoCoMo @155ms retrieval; 94.8% vs MemGPT's 93.4% on DMR. Both now disputed.

### 2. Mem0 / OpenMemory MCP
Mem0 is a memory layer that extracts salient "facts" from conversations via an LLM, stores them in a vector DB (+ optional graph mode), and does selective retrieval at query time. OpenMemory is its **local-first MCP server** — runs on-device, exposes memory to Claude Desktop, Cursor, Windsurf, VS Code via MCP, with a dashboard to browse/edit stored memories. This is the closest existing thing to "shared cross-tool memory."
- **Right**: MCP-native cross-tool sharing and a *visible, editable* memory store (users can delete bad memories — direct mitigation for poisoning/staleness). Sub-second p95 selective retrieval; claims 91% latency cut vs full-context.
- **Wrong**: LLM fact-extraction on write adds cost/latency and drops nuance; "fact" granularity loses relational context; accused by Zep of running competitors' systems wrong to win benchmarks. Vector similarity ≠ factual relevance (irrelevant recall).
- **Claims**: LoCoMo leadership — heavily contested by Zep's rebuttal.

### 3. Letta (MemGPT) — memory blocks + sleep-time agents
Letta models memory as **memory blocks**: labeled, character-bounded sections of the context window (e.g. a `human` block, a `persona` block) that live *in-context*, plus archival memory in external storage the agent pages in/out (the original MemGPT "OS-for-LLMs" idea). **Sleep-time agents** are background agents sharing the primary agent's blocks; they rewrite/consolidate memory during idle time — the primary agent has no memory-edit tools, the sleep-time agent does.
- **Right**: sleep-time consolidation is the best fit for afterwit's "mine transcripts after the session" pattern — do expensive extraction off the hot path. In-context blocks give deterministic, inspectable memory (no retrieval lottery for core facts).
- **Wrong**: block character limits force hard eviction decisions; running a persistent stateful server + background agents is infra-heavy; the paging model adds tool-call round-trips.

### 4. Cognee
Treats memory as a data-engineering problem: an **ECL pipeline (Extract → Cognify → Load)** builds a knowledge graph with optional RDF/OWL ontology validation. Retrieval blends cosine + BM25 + breadth-first graph traversal with pluggable rerankers, and deliberately **skips LLM summarization at query time** to stay sub-second. A `memify` step post-processes the graph: prunes stale nodes, reweights edges by usage, adds derived facts.
- **Right**: `memify` usage-based reweighting is a smart maintenance story; skipping query-time LLM calls keeps reads fast; ontology validation reduces graph garbage.
- **Wrong**: ingestion burns LLM calls to extract/resolve nodes — cost lands entirely on write; ontology setup is a config burden most users skip; graph-building complexity may be unjustified for personal-scale data.
- **Claims**: 0.93 human-level on HotPotQA multi-hop.

### 5. Microsoft GraphRAG → LightRAG → nano-graphrag lineage
GraphRAG builds a KG from a **static corpus in one batch pass**, detects communities, and generates hierarchical community summaries for global queries. LightRAG strips this to essentials (simpler entity extraction, flat graph, dual-mode retrieval) and supports incremental inserts. nano-graphrag is a ~1,000-line teaching reimplementation.
- **Right (GraphRAG)**: genuinely better for "global" sensemaking questions over a whole corpus than flat vector RAG.
- **Wrong (GraphRAG)**: brutal cost (~610k tokens/retrieval vs <100 for LightRAG; ~$50–200 to index a 500-page corpus) and **adding a document requires rebuilding the whole graph** — fatal for a system ingesting new transcripts daily. Slow, rate-limit-prone.
- **Takeaway**: for an incrementally-growing personal corpus, LightRAG/nano-graphrag's cheap incremental model is the right lineage — full GraphRAG's batch-recompute is disqualifying.

### 6. Claude-Code-transcript-mining OSS (the direct competitors)
A crowded, immature field, all built on Claude Code hooks (`Stop`, `PreCompact`, `SessionEnd`) + an MCP server:
- **claude-mem** (thedotmack): captures the session, AI-compresses it, stores in ChromaDB, injects relevant context at startup + semantic search. Explicitly multi-harness (Claude Code, Codex, Gemini, Copilot, OpenCode).
- **claude-self-reflect** (ramakay): Docker-based, local, monitors for new conversations, installs its own MCP.
- **claude-memory-compiler** (coleam00): hooks capture the transcript on session-end/compact, a background Claude Agent SDK process extracts decisions/lessons/gotchas, an "LLM compiler" organizes them into cross-referenced knowledge articles (Karpathy LLM-knowledge-base inspired). **Closest in spirit to afterwit.**
- **ClawMem** (yoloshii): on-device, hooks + MCP + hybrid RAG, explicitly shares memory across Claude Code / OpenClaw / Hermes runtimes simultaneously.
- **agentmemory / memory-mcp / itsjwill's claude-memory**: variations, some with Supabase cloud backup.
- **Right**: they've proven the hook→extract→inject loop works and that transcript mining is wanted. Local-first is the norm.
- **Wrong**: all are single-author, thin, and undifferentiated; none mine **error→fix pairs** specifically; none unify a knowledge graph across *codebases + transcripts + memories*; injection is usually crude "dump top-k at startup" (context bloat). Maintenance/abandonment risk is high.

### 7. Coding-tool built-ins (the baseline to beat)
- **Windsurf Cascade Memories**: auto-generated during conversation when Cascade judges something worth remembering, stored locally per-workspace in `~/.codeium/windsurf/memories/`, retrieved when deemed relevant, free (no credits). Docs themselves recommend Rules/AGENTS.md over auto-memories for anything you want *reliably* reused — an admission that auto-recall is unreliable.
- **Cursor memories**: similar auto-capture-to-rules model; users often mimic Windsurf by having the AI write `.cursor/rules`.
- **Claude Code CLAUDE.md / Codex AGENTS.md**: not memory — plaintext instruction files read at session start via a directory-walk (Claude additive/multi-file walking up-tree; Codex first-non-empty with `.override.md` precedence, global `~/.codex` → project root → cwd). Anthropic recommends **<200 lines** — models reliably follow ~150–200 instructions and the system prompt already eats ~50 slots. `/memory` lets Claude auto-record inferred conventions.
- **Right**: dead simple, inspectable, version-controllable, zero infra, deterministic (always in context, no retrieval lottery). This is why they dominate in practice.
- **Wrong**: don't scale past ~200 lines; per-workspace silos (Windsurf memories don't cross projects); no semantic retrieval; manual curation burden; the "always in context" model wastes tokens on irrelevant rules.

### 8. Code-context engines + the grep-vs-RAG debate
- **Aider repo-map**: parses every file with tree-sitter, extracts defining symbols, builds a graph (files=nodes, dependency references=edges), runs **personalized PageRank** (personalized on files in the current chat) to rank symbols, renders top-ranked elided definitions into a ~1k-token budget map. No embeddings. Deterministic, cheap, re-ranks as chat context changes.
- **Sourcegraph Cody**: hybrid — embeddings + Sourcegraph's search API across multiple repos; enterprise-scale remote code intelligence.
- **Continue.dev @codebase**: local embeddings (transformers.js) + keyword search in `~/.continue/index`, with optional LLM re-ranking (nRetrieve → nFinal).
- **The pivot**: In May 2025 **Anthropic removed vector search from Claude Code entirely** — no embedding pipeline, no local vector DB, no chunking — and replaced it with grep/glob/read + iterative agentic refinement, citing accuracy, operational simplicity, and avoiding staleness/privacy/security of a maintained index. Cursor, Windsurf, Cline, Devin, and Sourcegraph Amp followed. An Amazon Science paper (AAAI 2026) measured agentic keyword search at 94.5% of RAG faithfulness with zero vector store.
- **Takeaway for us**: for *code itself*, don't build an embedding index — the whole industry just walked away from that. grep + tree-sitter structural maps (Aider-style) is the winning pattern. Reserve semantic/graph memory for the things grep *can't* find: transcripts, decisions, error→fix pairs, cross-project lessons.

## Universal patterns
- **Local-first / on-device storage** is now table stakes (OpenMemory, all Claude-Code OSS, Windsurf, Continue). Privacy + no-network-latency.
- **Hooks/MCP as the injection substrate** — every transcript-mining tool uses Claude Code hooks to capture and an MCP server to expose retrieval. This is the de facto integration contract.
- **Extraction on write, not raw storage** — everyone runs an LLM pass to distill facts/decisions rather than storing raw text (Mem0 facts, Cognee ECL, memory-compiler articles).
- **Hybrid retrieval** — semantic + keyword/BM25 + (optionally) graph traversal. Pure vector is out of fashion.
- **Inspectable/editable memory** — dashboards (OpenMemory) or plaintext files (CLAUDE.md) so users can delete bad memories. A response to poisoning/staleness distrust.

## Divergent bets
- **Graph vs vector vs plaintext files**: Zep/Cognee/GraphRAG bet on graphs (relational reasoning, temporal invalidation) at high write cost; Mem0/Continue bet on vectors (cheap, fuzzy, irrelevant-recall-prone); Cursor/Windsurf/CLAUDE.md bet on **plaintext files** (dumb, deterministic, unscalable-but-reliable). The file camp quietly wins in daily coding use.
- **Proactive injection vs on-demand tools**: auto-inject-at-startup (claude-mem, CLAUDE.md, Windsurf) risks context bloat and irrelevant recall; on-demand MCP retrieval tools (OpenMemory, agentic grep) let the agent pull only what it needs but require the agent to know to ask. Emerging consensus leans **on-demand tools** for large corpora, static injection for a small always-relevant core.
- **Hot-path vs sleep-time extraction**: extract-during-conversation (Mem0, Windsurf) adds latency; extract-in-background (Letta sleep-time agents, memory-compiler's background SDK process) keeps sessions fast. Sleep-time is the better bet for a system mining large transcripts.
- **Batch vs incremental graph**: GraphRAG (batch, rebuild-to-update) vs LightRAG/Graphiti/Cognee (incremental). Incremental is mandatory for daily transcript ingestion.

## Failure modes to design against
1. **Irrelevant recall** — embedding similarity ≠ factual relevance ("Python memory management" retrieves "Python memory profiling tools"). Mitigate with re-ranking, keyword/structural filters, and letting the agent pull on-demand rather than force-feeding top-k.
2. **Stale / contradictory memory** — old preferences (Python 3.10) confidently override current truth. Mitigate with **temporal validity (Graphiti-style)** and explicit versioning/conflict resolution, not overwrite.
3. **Memory poisoning** — injected malicious/wrong content recalled weeks later as ground truth (Oct 2025 PoC; 4-stage enterprise damage chain). Mitigate with provenance on every memory, inspectable/editable store, and not auto-trusting agent self-outputs.
4. **Transcript-replay error entrenchment** — agents re-ingest their own past mistakes; early errors become "internally consistent" and hard to dislodge. Mitigate by mining **verified** outcomes (did the fix actually work?) not just what was said.
5. **Write-path cost/latency** — LLM extraction per episode (Zep, Mem0, Cognee) is expensive; batch-rebuild (GraphRAG) is worse. Mitigate with sleep-time/background extraction and incremental graphs.
6. **Context bloat / "more context makes agents worse"** — dumping memories degrades attention. Mitigate with strict token budgets (Aider's ~1k map) and relevance-gated injection.
7. **Maintenance burden & abandonment** — Neo4j ops, ontology config, or a thin single-author OSS project that dies. Mitigate by preferring plaintext/SQLite + grep over heavy graph infra unless the reasoning payoff is proven.
8. **Benchmark self-deception** — building to LoCoMo will optimize for a broken judge. Evaluate on *our* real transcripts and real retrieval outcomes.

## Gap analysis — our opening
None of the surveyed products do these well; each is afterwit's differentiator:
- **Cross-harness unification (Claude Code + Codex)**: only claude-mem/ClawMem gesture at multi-runtime, and shallowly. A single knowledge layer reading both Claude Code JSONL *and* Codex transcripts/AGENTS.md, and injecting into both, is genuinely unclaimed.
- **Mining error→fix pairs from transcripts**: everyone extracts "decisions/facts"; nobody specifically harvests *"this failed, then this fixed it"* — the single highest-value signal in a coding transcript. This is our killer feature.
- **Codebases + transcripts + memories in one graph**: code-context engines index code, memory tools index conversations — none join them (link a lesson to the file/symbol it concerns, Aider-repo-map-style, so a code query surfaces the transcript where that function last broke).
- **Two-tier per-project + global memory**: CLAUDE.md is per-repo, Windsurf memories are per-workspace, Mem0 is per-user-flat. A deliberate global-lessons / project-specific split with promotion between tiers is missing.
- **Grep-first for code, semantic/graph only for the un-greppable**: respect Anthropic's finding — don't embed the code, embed only the transcripts/decisions grep can't reach. No competitor draws this line cleanly.
- **Verified-outcome memory**: store only lessons whose fix was confirmed to work, sidestepping transcript-replay entrenchment. Nobody does provenance-of-correctness.

## What this means for our implementation
- **Do NOT build a code embedding index.** Use grep + tree-sitter/Aider-style PageRank repo-map for code retrieval. The entire industry abandoned code embeddings in 2025 for good reasons (staleness, ops, privacy, accuracy).
- **Reserve the semantic/graph layer for transcripts, decisions, and error→fix pairs** — the un-greppable knowledge. Prefer SQLite + a light incremental graph (LightRAG/Graphiti-lineage) over Neo4j+full-GraphRAG unless temporal reasoning proves necessary.
- **Extract in sleep-time/background** (Letta pattern) on session-end/PreCompact hooks, never on the hot path.
- **Inject on-demand via MCP tools** for the large corpus; keep only a tiny, high-signal core in always-in-context files (<200 lines, respecting CLAUDE.md limits).
- **Bake in provenance + temporal validity + editability** from day one — these are the mitigations for the failure modes that actually kill these products in production.
- **Evaluate on our own transcripts**, not LoCoMo. Metric = "did injected knowledge prevent a re-solved bug / repeated mistake," measured on real sessions.

## Things to verify
- Whether Codex actually exposes session transcripts in a parseable format comparable to Claude Code's JSONL (hooks parity) — assumed yes based on AGENTS.md/skills parity, but the transcript format needs confirming. **[assumption]**
- Current Claude Code hook set and whether `PreCompact`/`SessionEnd` reliably fire with full transcript access (the OSS projects rely on this; verify against current hook docs — the harness-mechanics research agent covers this).
- Whether Graphiti/temporal-KG's write cost is tolerable at personal scale, or whether a simpler "invalidate-by-timestamp in SQLite" replicates 80% of the value.
- Real latency/cost numbers for LightRAG vs plain SQLite-FTS on a personal-sized corpus (the LightRAG "1/100th cost" figure is vs GraphRAG, not vs no-graph).

## Sources
- Zep / Graphiti: [Zep arXiv 2501.13956](https://arxiv.org/abs/2501.13956), [Neo4j Graphiti blog](https://neo4j.com/blog/developer/graphiti-knowledge-graph-memory/), [Zep docs](https://help.getzep.com/graphiti/getting-started/overview)
- Benchmark controversy: [Zep "Lies, Damn Lies & Statistics"](https://blog.getzep.com/lies-damn-lies-statistics-is-mem0-really-sota-in-agent-memory/), [Penfield Labs LoCoMo audit (DEV)](https://dev.to/penfieldlabs/we-audited-locomo-64-of-the-answer-key-is-wrong-and-the-judge-accepts-up-to-63-of-intentionally-33lg), [getzep/zep-papers issue #5](https://github.com/getzep/zep-papers/issues/5)
- Mem0 / OpenMemory: [Mem0 arXiv 2504.19413](https://arxiv.org/html/2504.19413v1), [Mem0 memory benchmarks repo](https://github.com/mem0ai/memory-benchmarks), [Zep vs Mem0 (Atlan)](https://atlan.com/know/zep-vs-mem0/)
- Letta / MemGPT: [Letta sleep-time agents docs](https://docs.letta.com/guides/agents/architectures/sleeptime/), [Letta memory blocks blog](https://www.letta.com/blog/memory-blocks/), [Letta sleep-time compute](https://www.letta.com/blog/sleep-time-compute/)
- Cognee: [topoteretes/cognee GitHub](https://github.com/topoteretes/cognee), [Cognee "how it builds AI memory"](https://www.cognee.ai/blog/fundamentals/how-cognee-builds-ai-memory)
- GraphRAG lineage: [You probably don't need GraphRAG (Medium)](https://medium.com/@amrwrites/you-probably-dont-need-graphrag-0bc9cf671db1), [LightRAG cost analysis](https://www.ragdollai.io/blog/lightrag-vector-rags-speed-meets-graph-reasoning-at-1-100th-the-cost), [nano-graphrag breakdown](https://gonamlui.com/blog/brief-breakdown-of-nano-graphrag-a-lightweight-alternative-to-graphrag)
- Claude-Code OSS: [claude-mem](https://github.com/thedotmack/claude-mem), [claude-self-reflect](https://github.com/ramakay/claude-self-reflect), [claude-memory-compiler](https://github.com/coleam00/claude-memory-compiler), [ClawMem](https://github.com/yoloshii/ClawMem)
- Coding-tool built-ins: [Windsurf Cascade Memories docs](https://docs.windsurf.com/windsurf/cascade/memories), [Codex AGENTS.md guide](https://developers.openai.com/codex/guides/agents-md), [CLAUDE.md vs AGENTS.md (MindStudio)](https://www.mindstudio.ai/blog/codex-agents-md-vs-claude-code-claude-md-comparison)
- Code-context / grep-vs-RAG: [Aider repo-map (tree-sitter)](https://aider.chat/2023/10/22/repomap.html), [Why Claude Code doesn't use RAG (HarrisonSec)](https://harrisonsec.com/blog/agent-retrieval-cost-curve-claude-code-grep-vs-rag/), [Anthropic replaced RAG with agentic search](https://robertheubanks.substack.com/p/anthropic-replaced-their-rag-pipeline), [Continue.dev @codebase docs](https://docs.continue.dev/customize/context/codebase), [How Cody understands your codebase](https://sourcegraph.com/blog/how-cody-understands-your-codebase)
- Failure modes: [Agent memory poisoning 4-stage chain (DEV)](https://dev.to/mjmirza/agent-memory-poisoning-the-4-stage-enterprise-damage-chain-20fi), [AI agent memory guide (SitePoint)](https://www.sitepoint.com/ai-agent-memory-guide/), [Memory rot in long-running agents (Medium)](https://medium.com/@milesk_33/how-i-fixed-memory-rot-in-long-running-ai-agents-263a7a014dda)
