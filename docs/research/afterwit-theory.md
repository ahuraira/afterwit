# afterwit — Theory Research
**Generated**: 2026-07-05
**Lens**: theory

## Summary
- **Memory type dictates storage, not the reverse.** The CoALA taxonomy (episodic / semantic / procedural, over working vs. long-term) is the canonical frame every serious system (Letta, Mem0, Zep) builds on. Transcripts are *episodic raw material*; the durable value comes from *consolidating* them into semantic facts (decisions, invariants) and procedural workflows (error→fix recipes). Store raw and consolidated separately.
- **Graph beats plain vector only for multi-hop and temporal-evolution queries; hybrid wins overall.** Temporal knowledge graphs with explicit fact-invalidation (Zep/Graphiti) are the single most defensible architectural choice for our domain, because code decisions get *superseded* — a fact isn't deleted, it stops being valid at time T. Pure vector RAG cannot represent "we used X until we switched to Y in March."
- **Irrelevant injected context measurably degrades output — precision matters more than recall for proactive injection.** "Lost in the middle" costs 15–25 points of accuracy; distractors surrounding the right answer actively mislead. This is the strongest argument for *on-demand retrieval tools (MCP)* as the default and *narrow, high-precision proactive injection* as the exception.

## Key principles

1. **Adopt CoALA memory types as the schema backbone.** Classify every stored item as episodic (a specific event/session moment), semantic (a durable fact/decision/preference), or procedural (a reusable how-to / error→fix). *Testable:* every memory row has a non-null `type` in {episodic, semantic, procedural} and consolidated items link back to source episodes. Source: CoALA (Sumers et al., arXiv 2309.02427); reused by Mem0, Letta, LangChain.

2. **Consolidate, don't hoard.** Raw transcripts are the low-value substrate; a reflection/consolidation pass converts them into compact semantic + procedural memories (Generative Agents' "reflection", Mem0's extract→consolidate, Agent Workflow Memory's rule induction). *Testable:* retrieval serves consolidated memories first; raw transcript chunks are a fallback, fetched by ID only when a consolidated memory points at them. Sources: Generative Agents (arXiv 2304.03442), Mem0 (arXiv 2504.19413).

3. **Use a bi-temporal knowledge graph for facts that evolve.** Store both *event time* (when the fact became true in the world) and *ingestion time*, and invalidate edges rather than deleting them. This is exactly how Zep/Graphiti handles superseded facts and is the right model for "decision D held from commit A to commit B." *Testable:* a query "what did we decide about auth?" returns the *currently valid* decision plus its predecessors on request, never a silently stale one. Source: Zep (arXiv 2501.13956).

4. **Default to on-demand retrieval (MCP tools); reserve proactive injection for high-precision, small payloads.** Anthropic's own context-engineering guidance favors *just-in-time* retrieval (load lightweight identifiers, fetch on need) over preloading, precisely because of "context rot" (n² attention pressure). *Testable:* proactive injection is capped (e.g. top-3 memories, hard token budget) and every injected item must clear a relevance threshold; everything else is behind a tool call. Sources: Anthropic "Effective context engineering for AI agents" (2025); Liu et al. "Lost in the Middle" (arXiv 2307.03172).

5. **Hybrid retrieval, then rerank — never single-stage.** BM25 + dense embeddings for recall, cross-encoder or late-interaction (ColBERT MaxSim) reranker for precision. Two-stage pipelines beat single-stage by double-digit Recall@5 in the literature. *Testable:* the pipeline is retrieve(BM25 ∪ dense) → rerank → top-k; k is small (≤5–8) into the model. Sources: HippoRAG 2 (arXiv 2502.14802); general RAG benchmarking.

6. **Chunk code by AST/symbol boundaries, not fixed lines.** Line/char chunking splits functions and merges unrelated code, hurting both retrieval and generation. AST-aware chunking (tree-sitter, keeping scope/imports/signature with each chunk) is the code-specific requirement. *Testable:* no code chunk crosses a function/class boundary mid-symbol; each carries its enclosing signature + imports. Source: cAST (arXiv 2506.15655).

7. **Verify before you write; supersede, don't duplicate.** Transcripts contain wrong intermediate conclusions ("I think the bug is X" — then it wasn't). Extraction must prefer *confirmed* outcomes (the fix that landed, the decision that stuck) over speculation, and new facts should supersede/dedup against existing ones rather than piling up. Persistent memory is also an attack/poisoning surface. *Testable:* extraction only promotes a memory to semantic/procedural when there's evidence of resolution (a passing test, a merged change, an explicit user confirmation); a write that contradicts an existing memory triggers supersede, not append. Sources: Mem0 (arXiv 2504.19413); memory-poisoning literature (MINJA, NeurIPS 2025; OWASP Agent Memory Guard).

## Specific examples / cases

- **Zep / Graphiti (arXiv 2501.13956):** temporal KG agent memory. Beats MemGPT on Deep Memory Retrieval (94.8% vs 93.4%) and, more importantly, on the harder LongMemEval: up to **+18.5% accuracy** and **~90% lower latency** vs baselines. Mechanism: bi-temporal edges, edge invalidation on contradiction. This is the closest published analog to what we need for evolving code decisions.
- **Mem0 (arXiv 2504.19413):** production memory layer; extract → consolidate → retrieve with a graph variant (`Mem0g`). Multi-signal retrieval = semantic + BM25 + entity match. On LOCOMO it wins single-hop/multi-hop/temporal/open-domain while cutting tokens and latency vs full-context. Directly validates our "extract-and-consolidate + hybrid retrieval" plan.
- **HippoRAG 2 / "From RAG to Memory" (arXiv 2502.14802):** KG + Personalized PageRank over an LLM-built graph. **+7%** on associative memory vs the SOTA embedding model; MuSiQue multi-hop F1 44.8→51.9, 2Wiki Recall@5 76.5%→90.4%. Evidence that graph traversal specifically helps *multi-hop* "connect the dots across sessions" queries — our exact use case for linking a bug to the decision that caused it.
- **A-MEM (arXiv 2502.12110):** Zettelkasten-style atomic notes with LLM-generated keywords/tags, dense embedding, and evolving links between notes. Good model for our "one memory = one fact/decision, linked" design (mirrors the user's existing `[[name]]` memory-linking convention).
- **GraphRAG (Microsoft, arXiv 2404.16130):** distinguishes *local search* (entity-anchored, precise facts — "what did we decide about X") from *global search* (community-summary, corpus-wide sensemaking — "what are the recurring gotchas in this codebase"). Lesson: route query type → retrieval mode; don't force one index to do both.
- **"Lost in the Middle" (Liu et al., arXiv 2307.03172):** for 20-doc retrieval, accuracy drops **15–20 points** when the relevant doc sits at positions 5–15 vs 1–3; U-shaped curve. Follow-ups show length *alone* degrades performance (13.9%→85% as input grows even with irrelevant tokens blanked). Hard cap on how much we should ever inject.
- **cAST (arXiv 2506.15655):** AST structural chunking lifts RepoEval Recall@5 by **+4.3** and SWE-bench Pass@1 by **+2.67** over line-based chunking. The concrete number behind principle 6.
- **HyDE + reranking:** generate a hypothetical answer, embed *that* (document-to-document similarity beats question-to-document); nDCG@10 ≈ 61.3 on DL-20. Useful because agent queries are often terse ("why did auth break") and benefit from expansion before embedding.

## What this means for our implementation

- **Two-tier store:** (a) append-only raw transcript/episodic layer (cheap, by-ID access), (b) consolidated semantic+procedural layer that is what retrieval actually serves. The user's existing markdown memory files map cleanly onto tier (b).
- **Bi-temporal graph for decisions/facts, vector+BM25 for snippets/prose.** Don't build a graph for everything — build it for entities that *evolve* (decisions, libraries chosen, invariants, files' roles). Keep code/prose in a hybrid vector+BM25 index with a reranker.
- **Retrieval pipeline:** query → (optional HyDE expansion) → BM25 ∪ dense → cross-encoder rerank → top ≤5. For "connect the dots" questions, add a graph-traversal expansion step (à la HippoRAG PPR) before rerank.
- **Injection is opt-in and bounded.** Ship the MCP on-demand tool first (safe, precise). Add proactive injection only with a hard token budget and a relevance floor; measure whether it helps or hurts before trusting it.
- **Write policy = extract-on-resolution + supersede.** Prefer merged/confirmed outcomes over in-progress speculation; dedup and invalidate rather than append; timestamp everything (event time + ingest time).
- **AST-aware code chunking with tree-sitter** is non-negotiable for the code side; carry signature + imports + scope on each chunk.
- **Treat memory as a trust boundary.** Since transcripts and any shared memory can carry bad conclusions (or, in multi-user settings, injected ones), gate promotion to durable memory behind verification.

## Things to verify before relying on this
- Several cited surveys/attack papers surfaced with 2026 arXiv IDs (e.g. 2601.*, 2603.*, 2605.*). The load-bearing systems papers here (Zep 2501.13956, Mem0 2504.19413, HippoRAG2 2502.14802, A-MEM 2502.12110, GraphRAG 2404.16130, cAST 2506.15655, Lost-in-the-Middle 2307.03172) are well-established; treat the 2026-dated security papers as directional until IDs are confirmed.
- Benchmark numbers (Zep +18.5%, HippoRAG2 +7%, cAST +4.3/+2.67) come from the authors' own papers on conversational/QA benchmarks (LOCOMO, LongMemEval, RepoEval, SWE-bench). Our domain (personal dev transcripts + codebases) is adjacent but not identical — validate the graph-vs-hybrid tradeoff on a small slice of the user's actual transcripts before committing to graph complexity.
- "Graph beats vector" is corpus- and model-dependent (GraphRAG's win used GPT-4). Confirm the lift holds with our chosen embedding/rerank models before paying the KG-construction cost.
- Consolidation/reflection cost (LLM calls per session) may dominate the budget for hundreds of MB of transcripts — verify unit economics before batch-processing the full backlog.

## Sources
- CoALA: https://arxiv.org/abs/2309.02427
- Generative Agents (reflection / memory stream): https://arxiv.org/abs/2304.03442
- MemGPT: https://arxiv.org/abs/2310.08560
- Mem0: https://arxiv.org/abs/2504.19413
- Zep / Graphiti temporal KG: https://arxiv.org/abs/2501.13956
- HippoRAG 2 / From RAG to Memory: https://arxiv.org/abs/2502.14802
- A-MEM: https://arxiv.org/abs/2502.12110
- GraphRAG (local→global): https://arxiv.org/abs/2404.16130
- Lost in the Middle: https://arxiv.org/abs/2307.03172
- cAST (AST code chunking): https://arxiv.org/abs/2506.15655
- Position: Episodic Memory is the Missing Piece: https://arxiv.org/pdf/2502.06975
- Anthropic — Effective context engineering for AI agents: https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
- OWASP Agent Memory Guard: https://owasp.org/www-project-agent-memory-guard/
- HippoRAG (v1, NeurIPS'24) repo: https://github.com/OSU-NLP-Group/HippoRAG
