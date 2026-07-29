# Architecture Decision Records — afterwit

Append-only. New decisions get the next number. Never edit an accepted ADR; supersede it.

## ADR-001: Markdown wiki is source of truth; SQLite is a rebuildable cache
**Date**: 2026-07-05 | **Status**: Accepted | **Tags**: `storage`, `wiki`
### Context
Knowledge must be human-auditable (anti-poisoning), git-diffable, agent-editable, and survive tooling churn.
### Options Considered
Neo4j/graph DB; SQLite-only; markdown + derived SQLite index.
### Decision
One markdown file per card in `~/knowledge/` (git repo, Obsidian-compatible); SQLite (`FTS5` + vectors + links) derived via `afterwit index --rebuild`, losslessly regenerable.
### Consequences
Slightly slower bulk writes; trivial review/rollback; zero DB ops burden.

## ADR-002: No code embedding — index only un-greppable knowledge
**Date**: 2026-07-05 | **Status**: Accepted | **Tags**: `retrieval`, `scope`
### Context
Anthropic removed vector search from Claude Code (agentic grep won); Amazon AAAI 2026: agentic keyword search ≈94.5% of RAG faithfulness with zero infra.
### Decision
Codebases are searched live by the harness. afterwit indexes only distilled knowledge: decisions, error→fix pairs, gotchas, preferences, DB schemas, doc pointers.
### Consequences
No index staleness/maintenance for 19GB of code; the system's value concentrates in transcript mining.

## ADR-003: MCP-first serving; per-prompt push is Claude-only bonus
**Date**: 2026-07-05 | **Status**: Accepted | **Tags**: `serving`, `harness`
### Context
Codex has no verified per-prompt injection channel (hooks exist ≥v0.124 but `additionalContext` semantics undocumented). Claude Code `UserPromptSubmit` caps at 10k chars.
### Decision
One stdio MCP server registered in both harnesses carries all retrieval. Claude Code additionally gets bounded `UserPromptSubmit` push (≤3 cards/600 tokens, threshold-gated).
### Consequences
Feature parity across harnesses; push layer can be disabled (kill-switch) without losing the system.

## ADR-004: Distill-on-resolution, review-gated writes
**Date**: 2026-07-05 | **Status**: Accepted | **Tags**: `distill`, `security`
### Context
Transcripts contain wrong intermediate guesses; memory poisoning is OWASP ASI06; auto-trusted extraction entrenches errors.
### Decision
Cards are promoted only with resolution evidence (passing command, merged edit, user confirmation). Confidence <0.8 and ALL agent `save_insight` writes go to `review/` queue. Wrong cards are quarantined, never deleted.
### Consequences
Fewer cards, higher trust; human review step exists but is async and non-blocking.

## ADR-005: Distillation driver = `claude -p` headless (subscription), Batch API optional
**Date**: 2026-07-05 | **Status**: Accepted | **Tags**: `distill`, `cost`
### Context
Backlog is ~900MB raw (→ ~5-10% after filtering); user already pays for Claude subscription.
### Decision
Default driver shells out to `claude -p` with `prompts/distill.md`; `--driver batch-api` (claude-haiku-4-5, 50% batch discount) for bulk backlog. `--budget N` caps sessions per run, newest first.
### Consequences
Zero marginal cost by default; distillation speed bounded by subscription limits.

## ADR-006: Ranking = weighted hybrid with hard relevance floor
**Date**: 2026-07-05 | **Status**: Accepted | **Tags**: `retrieval`, `ranking`
### Context
Chroma context-rot study: distractors actively degrade output; returning nothing must be first-class.
### Decision
`score = 0.5·bm25_norm + 0.3·cosine + 0.1·recency(τ=90d) + 0.1·usefulness`, ×0 if not active, ×0.5 if unverified; +0.15 same-project. Absolute floor tuned on golden set; below floor → return nothing. P1 ships BM25-only.
### Consequences
Weights are eval-gated (§12); reranker/HyDE only if gates fail (ADR required to add them).

## ADR-007: Own-transcript eval only; public memory benchmarks banned
**Date**: 2026-07-05 | **Status**: Accepted | **Tags**: `eval`
### Context
LoCoMo audit: 6.4% of answer key wrong, judge accepts 63% of vague answers; vendors dispute each other's scores.
### Decision
`eval/golden.yaml` built from the user's real history (incl. 10 must-return-nothing queries). Gates: recall@3 ≥ 0.7, no-answer precision ≥ 0.9 before prompt injection enables. North-star: served knowledge prevents re-solved bugs (usage mining, SPEC §10).

## ADR-008: Usage counters checkpoint into frontmatter; git is the sync layer
**Date**: 2026-07-06 | **Status**: Accepted | **Tags**: `sync`, `schema`, `consolidation`
### Context
`usefulness`/`last_used` lived only in SQLite → `afterwit index --rebuild` zeroed earned scores (P4 violation) and a second device could never receive them. Ranking, kill-switch, and graph node size all depend on these signals.
### Options Considered
(a) rebuild preserves DB values in-place — device-local only, still not synced; (b) separate usage.json sidecar — second source of truth, merge pain; (c) checkpoint into card frontmatter.
### Decision
(c). Cards carry optional `usefulness`/`last_used` frontmatter (omitted until earned — no churn on fresh cards). DB is the live counter between checkpoints; `consolidate.write_back_usage()` moves DB → frontmatter (idempotent); `upsert_card` inserts them for new rows but never updates on conflict (DB wins between write-backs). `afterwit sync` = write-back → git add/commit → pull --rebase/push (if remote) → rebuild. One card = one small file + ULIDs → merge conflicts rare and human-resolvable.
### Consequences
Cross-device sync is `git remote add` + `afterwit sync` on each machine. `servings` stay device-local by design (raw measurement; the distilled signal syncs). Nightly runner (P4 wave) must call write_back_usage after mine/decay.

## ADR-009: Thinking blocks are rationale evidence, never card sources of truth
**Date**: 2026-07-06 | **Status**: Accepted | **Tags**: `ingest`, `distill`
### Context
Claude JSONL assistant records contain `thinking` blocks; Codex records reasoning summaries. They hold the *why* behind decisions (the most valuable, least-greppable content) but also abandoned paths and speculation.
### Decision
Adapters emit thinking content as normalized events (`kind="thinking"`) so the distiller sees it. The distiller may use thinking text to fill a card's **Why** — but only for outcomes with independent resolution evidence in non-thinking turns (a fix that ran, a user confirmation). A card must never be promoted from thinking alone; extract-on-resolution (SPEC §7) is judged on visible turns only.
### Consequences
Wave-1 adapter agents include thinking events; distill prompt already demands cite-or-drop, so thinking-only claims fail the sources requirement naturally. Slight ingest volume increase; acceptable (thinking is a small fraction of bytes vs tool_results).

## ADR-010: Source dicts carry origin — harness, model, kind
**Date**: 2026-07-06 | **Status**: Accepted | **Tags**: `schema`, `provenance`, `ingest`
### Context
`sources: [{path, lines}]` identifies *where* but not *who*: which model authored the resolving turn (Claude JSONL carries `message.model` per assistant record), and whether the claim originated from the user, the assistant, a thinking block, a doc, or a DB schema. "User explicitly stated" vs "Haiku inferred" is a real trust difference; retrofitting after thousands of cards exist would be lossy.
### Decision
Source dicts gain optional keys: `harness` (claude|codex|doc|db|memory), `model` (verbatim model ID from the record), `kind` (user|assistant|thinking|doc|schema). Adapters populate them when the record provides them; absent keys are legal (only `path` is mandatory — validate() unchanged, YAML round-trips extras already). Ranking does NOT consume them in P1–P3; origin-aware trust weighting is a P4+ eval-gated option. `kind: thinking` sources are corroborating only, per ADR-009.
### Consequences
Zero code change in the frozen core (contract was already open); Wave-1 adapters must emit the keys from day one. Review UI can later surface "user-stated" badges cheaply.

## ADR-011: Push surfaces serve verified cards only
**Date**: 2026-07-06 | **Status**: Accepted | **Tags**: `security`, `injection`
### Context
Postprocess writes high-confidence distilled cards as `active + unverified`; rank served them at ×0.5 everywhere — including UserPromptSubmit push. Push is a zero-consent surface: the user never asked, the agent never asked, content lands straight in the prompt. That was the open window for memory poisoning (OWASP ASI06/MINJA class): one bad extraction auto-propagates into every future session before any human sees it.
### Decision
`afterwit inject` (prompt AND session modes) serves `verified=1` only. Pull tools (recall/lookup_error/why/for_file) keep serving unverified at UNVERIFIED_FACTOR 0.5 — the agent explicitly requested, and results are labeled. Human approval in the review UI remains the only path to verified.
### Consequences
Cold start: injection is silent until the user reviews some cards — acceptable; review UI is keyboard-fast and silence is first-class (P3). Slightly slower time-to-value, strictly better trust story. Tests pin both modes.
**Amendment (2026-07-06)**: explicit opt-out exists — `push_unverified = true` in config.toml (prompt mode only). High-risk, disclosed in config.py; opted-in unverified cards are labeled "UNVERIFIED — not human-reviewed" in the injected block so the downstream agent can weight them. Default remains false.

## ADR-012: bm25 min-max anchored by query-token coverage
**Date**: 2026-07-06 | **Status**: Accepted | **Tags**: `retrieval`, `ranking`
### Context
First real golden-set run: no-answer traps leaked. Min-max bm25 is relative-only — in a small candidate pool a lone incidental match normalizes to 1.0 and clears the floor. Raw |bm25| cannot serve as the absolute anchor because FTS5 idf collapses to 0.0 on small corpora (Gotcha #7).
### Options Considered
(a) raise the floor — kills legitimate hits at the same scale; (b) absolute |bm25| scaling — returns nothing on small corpora; (c) multiply min-max by query-token coverage (fraction of distinctive query tokens present in title+body, prefix-lenient).
### Decision
(c). `rank.rank()` gains optional `query_text`; all serving paths (inject, recall, ui, mcp recall/lookup_error/why, evalx) pass it. Coverage is corpus-size independent, cheap, and explainable. Verified by `afterwit eval`: recall@3 100%, MRR 1.0, no-answer precision 100%.
### Consequences
rank.rank remains the only scoring path; callers that omit query_text keep pure min-max (aggregate/legacy). Coverage uses index_db's stopword list — the two must stay consistent. Retune only via `afterwit eval`.

## ADR-013: Codex driver for bulk distillation; one worker per project
**Date**: 2026-07-06 | **Status**: Accepted | **Tags**: `distill`, `cost`, `quality`
### Context
Backlog is ~1,100 sessions. `claude -p` (driver A) burns the Claude subscription the user works on; Codex (GPT-5.5) has a separate quota pool and is strong at rubric-following extraction. distill.py's driver seam was designed for this (ADR-005).
### Decision
Driver B: `codex exec -s read-only -m gpt-5.5 -o <file> -` over the SAME frozen prompts/distill.md — the extraction judgment stays in the prompt, only the executor changes. Campaign discipline: parallelize ACROSS projects, serialize WITHIN a project (`afterwit distill --project X`) — the dedupe pool is project-scoped and two workers on one project would race it and mint duplicates. All output still flows through postprocess gates + review queue; driver choice never bypasses trust machinery.
### Consequences
Bulk campaigns cost zero Claude quota. Per-session distill tracking (skip already-distilled sessions) is P4 nightly-runner work — until then, re-runs re-distill newest sessions and rely on dedupe/merge (wasteful but safe).

## ADR-014: Code awareness via capability cards + project maps — never code copies
**Date**: 2026-07-06 | **Status**: Accepted | **Tags**: `scope`, `retrieval`, `schema`
### Context
ADR-002 (grep beats RAG) holds within a repo but leaves a real gap across repos: an agent cannot grep what it does not know exists, so new projects silently reinvent solved work from sibling projects. LLM-wikis attack this with generated code descriptions and inherit the staleness treadmill — descriptions rot silently as code moves, and refresh-everything schedules are expensive and still lag.
### Options Considered
(a) Full llm-wiki layer (page-per-concept descriptions) — staleness treadmill, duplicates live reading, human-oriented; (b) embed code for similarity search — banned by ADR-002 for cause; (c) pointer layer: `capability` cards (existence + location + purpose, never implementation) + one stamped `map.md` per project (structure orientation: modules, entry points, data flow, invariants — names and shapes only).
### Decision
(c), specified in SPEC §7a. Capability cards are ordinary cards — review-gated, provenance-carrying, usefulness-mined, cross-project by ranking (not filter); `files` non-empty is mandatory (a capability without a path is a rumor). Maps are regenerable artifacts, not cards: no review cycle, trust from path-anchoring + generation stamp (date + repo commit). Produced by the `aw-survey` skill (one project per run); refreshed event-driven only — code-drift lint flags dead capability paths → re-survey that project. Never scheduled regenerate-everything.
### Consequences
"Do I already have X?" becomes answerable across all repos while served content can never inject stale code (agent must read the live file the card points at). The superiority claim over llm-wikis is falsifiable: capability cards earn usefulness scores like everything else — if they go unused, the numbers say so. CARD_TYPES gains `capability` (wiki dir `capabilities/`); recall tool type enum gains it; `map.md` joins the non-card skip list.

## ADR-015: Nightly runner — per-session distill ledger, fail-soft stages, single lock
**Date**: 2026-07-06 | **Status**: Accepted | **Tags**: `runner`, `distill`, `ops`
### Context
Campaigns are manual and re-distill newest-first because nothing records what was already distilled (ADR-013 consequence). A nightly runner must advance through the backlog, survive partial failures, and never run twice concurrently.
### Decision
(1) New SQLite table `distilled(source TEXT PRIMARY KEY, content_hash TEXT, ts TEXT, cards INTEGER)` — operational state like `checkpoints`/`servings`, NOT wiped by `rebuild()` (wiki-truth rule governs cards, not operational ledgers). A session is distilled when a run processed it at that content hash — 0 cards still counts (absence of knowledge is a result). Hash change (live session grew) re-eligibilizes. Selection order stays newest-first among UNdistilled sessions.
(2) `afterwit run` sequencer: ingest(all sources) → distill(--budget, skip-distilled) → mine_servings → apply_decay → enforce_killswitch → lint(projects_root) → write_back_usage → regenerate briefs/index → local `afterwit sync` snapshot. Every stage fail-soft: exception logs to wiki log.md + stderr, later stages still run (only distill depends on ingest), exit code nonzero if any stage failed.
(3) Single-instance lock: `db_path.with_name("run.lock")` containing pid+ts; stale (>6h or dead pid) locks are broken with a log line; a live lock → exit 0 with "already running".
(4) Scheduling: systemd user timer (preferred) + cron fallback, installed by `afterwit install cron` — backup-first, idempotent, same discipline as ADR-011-era install.
(5) The runner NEVER flips serving posture: no UserPromptSubmit registration, no kill-switch reset, no auto-approve. It produces; humans and gates decide.
### Consequences
Backlog drains automatically (budget/night); re-runs are cheap no-ops; mine_servings finally gets wired with a real transcript lookup (session_id → transcript file via adapters). Tests must pin: skip-on-second-run, rehash-re-eligibility, stage isolation, lock behavior.

## ADR-016: Embeddings serve pull paths only; the hook path stays lexical
**Date**: 2026-07-06 | **Status**: Accepted | **Tags**: `retrieval`, `latency`
### Context
P4 adds fastembed cosine (rank.py already has the `cosines` parameter — frozen file needs no edit). But embedding a QUERY requires loading the ONNX model (hundreds of ms first-call), and the inject hook has a hard p95 < 200ms budget with "no model load" written into its contract.
### Decision
Card vectors are precomputed at index time into a `vectors` table (owned by embed.py, additive schema, untouched by rebuild's card wipe — regenerated alongside). Cosine contributes to ranking ONLY in pull paths (recall CLI, MCP tools) where latency is tolerable; `afterwit inject` remains BM25+coverage. Weights stay per ADR-006; enabling cosine must keep `afterwit eval` gates green (recall@3 may only improve; no-answer precision must hold) — regression = don't ship, tune first.
### Consequences
Two ranking flavors by surface, deliberately: push = fast+strict, pull = rich. Documented in rank call sites. fastembed dep lands (pre-approved). If eval shows no lift at this corpus size, cosine stays off and the vectors table is dormant — scale honestly (P10).

## ADR-017: One-time user-delegated batch review by a Fable agent
**Date**: 2026-07-06 | **Status**: Accepted | **Tags**: `security`, `review`
### Context
ADR-004/011 make human approval the only path to verified:true, guarding against SILENT trust promotion. The user explicitly delegated review of the standing batch (~190 queued cards + supersede/drift audit of ~157 active) to a Fable-tier agent — an explicit, logged instruction, not silent promotion.
### Decision
One-time delegation, bounded: the agent verifies every card against its cited sources before any decision; approves only grounded+resolved+clean cards; rejects only clearly wrong/unsupported/secret-bearing ones; anything uncertain STAYS QUEUED for the human. Every approve/reject is logged to wiki log.md with a `fable-review` prefix so the trust origin of this batch is permanently auditable. Supersede fixes and drift corrections follow the same verify-first rule. This ADR does not create a standing policy — future agent approvals require a new explicit user instruction.
### Consequences
Batch throughput without silently weakening the gate; the wiki records which verifications were machine-adjudicated. If spot-checks later show bad approvals, quarantine by log-prefix is one query.

---

## ADR-018: Commit-anchored staleness; project identity is the repo remote

**Date**: 2026-07-10 | **Status**: Accepted | **Tags**: `staleness`, `git`, `ranking`, `cross-device`

### Context
Code drift was an existence check (`consolidate.lint`): flag a card only when **none** of its cited files still exist. It cannot see the common case — the file still exists but was rewritten underneath the card. Such a card ranked identically to a fresh one and pointed agents at code that had moved. Separately, a project's identity was its directory name under `projects_root`, which differs per device, so drift detection did nothing at all on a second machine.

Drift is also not supersede: no replacement card exists when code moves, and none will until someone re-runs `afterwit survey`. Waiting for a successor would mean never demoting.

### Options Considered
- **Existence check only** (status quo) — blind to rewrites; the actual quality hole.
- **Content hash of the cited line range** — precise, but breaks on any reformat and needs the code body, which we must never store (P2).
- **Commit anchoring** (chosen) — store the commit a card's claims were true at; drift = `git diff --name-only <source_commit>..HEAD` ∩ `files`. Measured on the real corpus: catches 7 rewritten-file cards the existence check missed.
- **Commit anchoring as a _replacement_ for the existence check** — rejected after measurement. It silently un-flagged 3 real dead-pointer cards (citing `/tmp/...` scratch paths and a `docs/MANIFESTO.md` absent from that repo): a path that was never tracked can never appear in a diff. The two signals catch disjoint failures.
- **GitHub API / webhooks** — rejected: the serving path is offline and p95 < 200ms (§9), and it would make the tool a hosted service.

### Decision
- `Card.source_commit` (optional frontmatter) records the project HEAD at extraction time. Stamped in `distill.py` and `afterwit queue`; absent for `global` and non-git projects.
- New `src/afterwit/gitmeta.py` — **local git only**, no network, no GitHub API. Every call degrades to `None` (non-repo, shallow clone, unknown sha) and never raises.
- `consolidate.mark_stale()` is the sole writer of the new `cards.stale` column. It runs at lint time, never on the serving path; `afterwit inject` reads only the stored flag.
- A card is stale when **either** signal fires (union, not replacement): **moved** (a cited file appears in `<source_commit>..HEAD`) **or** **dead pointer** (none of the cited files exist now). The diff sees rewrites an existence check cannot; the existence check sees never-tracked paths a diff cannot.
- Fallback ladder, in order: unknown/absent `source_commit` → dead-pointer check alone; project not checked out here → **skip** (absence is ignorance, not drift).
- `consolidate.backfill_source_commits()` anchors pre-ADR-018 cards to the commit that was HEAD on their `created` date (`git rev-list -1 --before=<created>`), so drift works retroactively rather than only for cards written from now on. Idempotent; cards predating the repo's first commit keep the fallback.
- `rank()` multiplies a stale **capability** card by `STALE_FACTOR = 0.5` — same shape as the unverified factor. Demotion, never deletion. Independent of supersede: no replacement card exists when code moves, so waiting for one would mean never demoting.
- **Demotion is scoped to `capability` cards** (`DEMOTE_STALE_TYPES`). A capability card *is* a pointer to living code (ADR-014); if the code moved, the pointer may be wrong. Every other type is knowledge *about* code — editing a cited file does not falsify "we chose JSONB because the schema churns". Drift is still recorded on all types for lint/human review, just not for ranking.
- Project identity = normalized `origin` URL (`projects` table, `record_project`). `gitmeta.discover()` maps repo_url → local path so a clone under a different folder name still resolves.

### Consequences
Rewrites are now caught (23 drifted cards on the real corpus vs 8 under the existence check); drift became a ranking signal instead of a number in a log nobody reads. `stale` is derived and INSERT-only in `upsert_card` (a re-index must not un-flag drift); `rebuild()` drops it, and the next lint recomputes — acceptable, since `stale` is a cache of git, not knowledge (P4 holds). `rank._stale()` reads the column defensively because a readonly connection to a pre-migration index cannot `ALTER TABLE`.

Gate: ranking changed, so `afterwit eval` re-ran. **The first attempt demoted every type and failed in practice** — recall@3 fell 100% → 88% because a *verified, still-true* decision card (`AGENT_CONFIDENCE_CAP …`) cited `distill.py`, which had been edited for unrelated reasons, and its demotion pushed it under the floor. File-level drift is a coarse proxy: on an active repo, any edit to a hot file would demote every card citing it. Scoping demotion to `capability` restored recall@3 100%, MRR 1.000, trap precision 100%. The lesson generalizes: **drift invalidates pointers, not rationales.**

---

## ADR-019: Derived files are never synced; a failed sync must fail loudly

**Date**: 2026-07-10 | **Status**: Accepted | **Tags**: `sync`, `cross-device`, `correctness`

### Context
The knowledge repo tracked three files that every run rewrites: `index.md` and `projects/*/brief.md` (full rewrite in `wiki.regenerate`) and `log.md` (append in `wiki._log`). Two devices regenerate these from their own card sets and append at the same tail line, so `git pull --rebase` conflicts on the **first** nightly the second device runs.

The failure was silent. `cli._cmd_sync` returns `1` on a failed pull, but `runner` called it as `(cli._cmd_sync(None), "snapshot")[1]` — discarding the return value — so the run printed `[run] sync: snapshot` as success while the repo sat mid-rebase. The next nightly's blind `git add -A && git commit` would then commit conflict markers into card files, which get indexed and served to agents as knowledge.

### Options Considered
- **`merge=union` / custom merge driver** — duplicates log lines; still conflicts on `index.md`.
- **Commit derived files from one device only** — fragile convention, silently wrong if forgotten.
- **Stop syncing what is derivable** (chosen) — `index.md`/`brief.md` are losslessly regenerable (that is exactly the P4 guarantee), so they carry zero information across devices.

### Decision
- `.gitignore` in the wiki: `index.md`, `projects/*/brief.md`, and the legacy `log.md`. Untracked, kept on disk, regenerated locally after every pull.
- The run log becomes per-device: `wiki.log_path()` → `log-<hostname>.md`. Filenames never collide, so the audit trail (P6) still syncs.
- `cli._cmd_sync` aborts a conflicted rebase (`git rebase --abort`) rather than leaving a half-rebased wiki, keeps the local snapshot, and returns nonzero.
- `runner._sync_or_raise()` raises on a nonzero sync so `stage()` records a real failure and `afterwit run` exits nonzero.

### Consequences
A second device can run its nightly without corrupting the wiki. A fresh clone has no `index.md`/`brief.md` until its first run regenerates them — acceptable for derived files. Remaining softer seam: `write_back_usage` checkpoints `usefulness`/`last_used` into synced frontmatter (ADR-008), so two devices serving the same card can still conflict on that line; the write-back is churn-free so it is rare, usefulness is a soft ranking signal, and last-writer-wins is tolerable. Revisit with a max-merge driver if it bites.

---

## ADR-020: Staleness corrections after adversarial audit (amends ADR-018/019)

**Date**: 2026-07-10 | **Status**: Accepted | **Tags**: `staleness`, `sync`, `ranking`, `cross-device`, `audit`

### Context
A GPT-5.6-terra audit (read-only, xhigh effort) was run against commit `7586e59` with the nine ADR-018/019 claims stated as falsifiable assertions. It reproduced eight defects. Claims 2, 3, 8, 9 held. The rest did not.

The headline: **the demotion feature never ran in production.** The nightly order is `lint → … → sync`, and `cli._cmd_sync` ends with `index_db.rebuild()`, which `DELETE`s the cards table and re-inserts every row with `stale=0`. `lint` had already set the flags; `sync` erased them. Between nightlies — i.e. during all serving — every card read as fresh. ADR-018 asserted "rebuild drops it, the next lint recomputes — acceptable"; that reasoning never traced the stage order. Verified live: 23 flags, `afterwit sync`, 0 flags.

### Decision
- **D1** `rebuild()` zeroes derived flags, so every caller recomputes: `cli._restale()` runs `mark_stale` after `afterwit index --rebuild` and after `_cmd_sync`'s reindex. Fail-soft — drift is advisory, never breaks a rebuild.
- **D2** Cross-device identity moves into the card. `Card.repo_url` (frontmatter, synced) is the resolution key; `_resolve_project` prefers `by_url[card.repo_url]` from `gitmeta.discover()` over the folder slug. The ADR-018 version compared `path.name == slug`, which could only match when `projects_root/slug` already resolved — dead code. The `projects` table cannot serve this: it is device-local SQLite, never synced.
- **D3** `backfill_anchors()` (renamed from `backfill_source_commits`, now also fills `repo_url`) runs as a nightly stage before `lint`. It was unreachable production code — only tests and one manual invocation called it. It does not bump `updated`: anchoring is metadata, not a knowledge change, and bumping it would perturb recency ranking.
- **D4/D6** The dead-pointer test is now `_dead_pointer()`: **any** cited path that escapes the project (absolute, or `../`) or does not exist marks the card. `Path('/proj') / '/tmp/x'` is `/tmp/x`, so a live scratch file outside the repo previously read as a valid pointer — and being untracked, never appeared in a diff either, so it was permanently fresh. "All files missing" is replaced by "any file bad": a card is only as good as its weakest pointer.
- **D5** One stamping path for everything: `gitmeta.anchor(projects_root, project)` returns `(source_commit, repo_url)` and is called by `distill`, `afterwit queue`, and MCP `save_insight`. `save_insight` previously stamped nothing, so agent-proposed capability cards shipped unanchored.
- **D7** All four audit writers (`wiki`, `distill`, `ui`, `mcp_server`) route through `config.log_path()`. Three still wrote the now-gitignored `log.md`, so distill-skips, review rejections and quarantine events silently stopped syncing — a regression introduced by ADR-019. It lives in `config` because `wiki` imports `ui`, so `ui`/`distill`/`mcp` cannot import `wiki`.
- **D8** `config.device_id()` = persisted `hostname-<random6>` at `~/.afterwit/device_id`. Hostname alone is not a device identity; two machines sharing one recreate the ADR-019 log conflict.
- **Merge re-anchors.** `wiki.execute("merge", …)` now adopts the candidate's `source_commit`/`repo_url`. The auditor filed the old behaviour under "what is good"; a repro disproved it — the merged card takes the newer body describing current code but kept the old anchor, so it was flagged stale the instant it was refreshed and the ADR-014 drift-triggered refresh loop could never converge. Residual: `files` is a union, so old-only entries are not re-verified at the new commit; the next distill/survey re-checks them.

### Consequences
Drift on the real corpus: 23 → 35 flagged under the stricter predicate (7 capability cards demoted, up from 3). `repo_url` now on 202 cards (projects without a remote — e.g. `token-kit` — legitimately carry none and fall back to the slug). Eval re-run after the semantics change: recall@3 100%, MRR 1.000, trap precision 100%. 141 tests.

`Card.repo_url` is a frontmatter addition, so `afterwit index --rebuild` stays lossless (P4). `iter_cards` now skips `log-*.md` explicitly rather than relying on `CardError`.

The meta-lesson: **claims 2, 3, 8, 9 were the ones I had tests for; the five that broke were the ones I had only reasoned about.** A feature that is never called by production code passes every unit test it has.

---


## ADR-021: Auto-review — an independent model may clear cards (amends ADR-011)

**Date**: 2026-07-10 | **Status**: Accepted | **Tags**: `review-gate`, `trust`, `anti-poisoning`, `onboarding`

### Context
ADR-011 fixed the serving posture: push surfaces (`afterwit inject`) serve `verified: true` only, and **human approval in `afterwit ui` is the only path to `verified`**. That gate is the anti-poisoning boundary — without it a single bad extraction auto-propagates into every future prompt.

It also assumes a human who opens the review UI. The target user for the open-source release is someone whose only tool is Claude Code and who has never heard of BM25. They will never triage a 180-card queue. For them the gate is not a safeguard, it is a wall: the hook injects nothing, the system looks inert, and they uninstall it.

### Options Considered
1. **Auto-approve everything from the distiller.** Restores usefulness, deletes the entire anti-poisoning property. The distiller becomes its own approver. Rejected.
2. **`push_unverified = true` for non-technical users.** Already exists as a documented HIGH-RISK escape hatch. It doesn't review anything — it just lowers the bar. Rejected as the *default* answer.
3. **Ship without an answer.** Makes the OSS release either unusable (no cards served) or unsafe (flag flipped by users who don't read the warning).
4. **An independent model reviews on the user's behalf.** Chosen.

### Decision
The property ADR-011 was actually buying is **separation of duties**: the agent that WROTE a card must not be the one that CLEARS it. A human was the only reviewer available, not the only reviewer possible. Keep the property, drop the assumption.

`afterwit review` / `POST /api/review/autoreview-all` (`src/afterwit/review.py`), gated on `auto_review = true`, default **off**:

1. **A different reviewer.** `reviewer_name()` defaults to the driver the distiller is *not* using — codex distills → claude reviews, and vice versa. The reviewer sees the card and the rubric, never the distiller's reasoning.
2. **Abstain by default.** Malformed JSON, an unknown verdict, a timeout, a crashed CLI — every one is an abstain. An abstained card stays queued for a human. Silence is never consent.
3. **Deterministic vetoes the model cannot overrule** (`_gate()`, runs before the LLM is ever called): `preference` cards can never be auto-approved (they encode the user's own intent); a card whose title/body carries a `[REDACTED:…]` marker is rejected outright; `confidence < 0.5` or body > 1500 chars → abstain.
4. **One approval path.** Auto-approval calls the same `ui._approve()` a human presses. There is exactly one place `verified` becomes true; it now records **who** via `Card.reviewed_by`.
5. **Attributable.** `reviewed_by` (`"human"` | model id) lands in frontmatter and syncs; every auto-decision appends to the per-device audit log.
6. **`afterwit review --dry-run`** prints verdicts and changes nothing, and works even with `auto_review = false`.

### Consequences
- ADR-011's "human approval is the only path to `verified: true`" is **amended**: an *independent, non-authoring* reviewer is. The `[unverified] ×0.5` pull posture is unchanged.
- Auto-review costs one LLM call per queued card; it is not on the nightly path.
- A model that systematically approves junk is now possible. Mitigations: the rubric's asymmetry (approve is expensive, reject/abstain cheap), the deterministic vetoes, and the audit log. Real accepted risk — why the flag is off by default.

## ADR-022: Redaction moves to the card-write boundary; sync refuses public remotes

**Date**: 2026-07-10 | **Status**: Accepted | **Tags**: `security`, `redaction`, `sync`, `oss`

### Context
Preparing the OSS release, a live probe of `redact.py` against sixteen credential formats found **nine leaked**: Anthropic (`sk-ant-`), OpenAI (`sk-proj-`), GitHub (`ghp_`, `github_pat_`), Slack (`xoxb-`, webhook URLs), Google (`AIza`), npm, Stripe, `aws_secret_access_key` (its own docs use a space, and the generic rule required `:` or `=`), and a PEM header without its END marker (the common truncated paste).

Worse than the missing patterns was **where** redaction ran. `redact()` was called in the four adapters — at ingest. But cards are written by the distiller, by `save_insight`, and by `afterwit queue`, none of which pass through an adapter. And the wiki is what gets `git push`ed. Redaction defended the transcript; the artifact that leaves the machine is the card.

Third: `afterwit sync` pushes to whatever remote exists. A user who points the wiki at a public repo publishes their mined session history irreversibly.

### Decision
1. `redact.py` gains vendor patterns for all nine leaked classes, plus `scrub_home()` (`/home/alice`, `/Users/bob`, `C:\\Users\\carol` → `~`). `sanitize() = scrub_home(redact(text))`; both idempotent.
2. **`cards.save()` calls `sanitize()`**, mutating in place so the caller's `upsert_card` indexes exactly what hit disk. The one door every writer shares. Adapters keep redacting at ingest so the distilling LLM never *sees* a live key.
3. `redact.has_secret()` is a hard pre-LLM reject in auto-review (ADR-021).
4. `cli._remote_visibility()` asks `gh repo view --json isPrivate`. `afterwit sync` **refuses to push** a remote it can prove is public, unless `allow_public_wiki_remote = true`. It blocks on *proof* of public, never on absence of proof.

### Consequences
- Existing cards are sanitized lazily, on their next write (see ADR-023 correction re: `write_back_usage`).
- False-positive risk is tested: `git@github.com:owner/repo.git` (every SSH clone URL, and the `repo_url` cross-device key) and `foo@bar.py` survive.
- Redaction is lossy and now runs on human-edited card bodies. Accepted: safety over convenience at a boundary that publishes.
- The five properties are mutation-tested — each verified to fail when its guard was removed.

## ADR-023: Second adversarial audit of the redaction/auto-review seam (amends ADR-021/022)

**Date**: 2026-07-10 | **Status**: Accepted | **Tags**: `security`, `redaction`, `auto-review`, `sync`, `audit`

### Context
A GPT-5.6-terra audit (read-only, xhigh) ran 10 falsifiable claims against the ADR-021/022 seam. A second Opus reviewer independently re-verified. The two disagreed on the headline finding — usefully.

**Codex's claim 4 (CRITICAL: unsanitized secrets in SQLite via `queue_insert`/`upsert_card`) was WRONG at HEAD.** Codex captured that output from an exploratory probe taken *before* the `queue_insert`/`upsert_card` sanitize calls were committed; the reviewer re-ran the exact repros and got `[REDACTED:github_token]` on both paths. The ADR-020 lesson in reverse: the claim that broke was the one backed by stale captured output, not a fresh run. The three-boundary sanitize (`cards.save`, `ui.queue_insert`, `index_db.upsert_card`) is more robust than the single-boundary design the claim assumed.

### Decision — fixes for the seven claims that held
1. **Claim 5 (critical) — duplicate-JSON-key gate bypass.** `{"verdict":"reject","verdict":"approve"}` resolved to the last value and approved. `review._parse` now decodes with an `object_pairs_hook` that rejects duplicate keys, and abstains on any non-string/list/nested verdict. Ambiguity is never an approval.
2. **Claim 6 (high) — reviewer could equal distiller.** An explicit `auto_review_driver` equal to `distill_driver` silently voided separation of duties. `reviewer_name()` now overrides it with the opposite driver and warns.
3. **Claim 7 (medium, latent) — `apply_verdict` did not re-gate.** Only the reachable caller gates first, but the last step before a verified write now re-runs `_gate` by rowid, so a forged/mismatched approve cannot persist a `preference` or secret-bearing card.
4. **Claim 8 (medium-high) — public-remote check missed atypical URLs.** `_remote_visibility` regex is now case-insensitive and tolerates an explicit port and embedded userinfo; `afterwit sync` pushes to the verified remote by name (checking its push-url) instead of a bare `git push` that pushDefault/pushurl could redirect.
5. **Claim 1 (partial) — AWS session/refresh/id tokens.** The bare-keyword rule could not match inside `aws_session_token` (word-boundary on `token`). The generic pattern now accepts snake/kebab-prefixed secret words. Residual (accepted): unicode-escaped and percent-encoded secrets inside JSON string literals are not decoded before matching — a transcript would have to deliberately encode a secret for this to bite.
6. **Claim 2 (low) — cosmetic.** The `url_password` marker no longer re-matches its own pattern.
7. **Outside-the-10 — credentialed remote URL.** `gitmeta.normalize_url` stripped `.git` but preserved `https://user:token@host`, so a credentialed origin rode into every anchored card's synced `repo_url`. `normalize_url` now strips userinfo, and `cards.sanitize` redacts `repo_url` as defense-in-depth.

Claims 3 and 9 HELD. Claim 10 (sanitize changes slug then orphans a secret-bearing file) is mooted for go-forward flows: sanitizing at `queue_insert` means a card's title is already clean before any path is computed, and sanitize is idempotent, so the slug is stable across saves.

### Consequences
- **Correction to ADR-022:** its claim that `write_back_usage` would scrub the 274 legacy home-path cards was wrong — that function rewrites via `render()`, not `save()`, and never sanitized. It now calls `cards.sanitize()`, so the lazy `~` migration actually happens as a card's usage counters change. Cards whose usage never changes stay unsanitized until any other write or an explicit one-time scrub.
- All seven fixes are mutation-tested (guard removed then the test goes red). 238 tests, ruff clean, mypy at its 16-error baseline, eval recall@3 100% / MRR 1.000 / trap precision 100%.

### Gotchas added
See Gotchas Reference #24 (duplicate-key JSON verdicts) and #25 (an audit that quotes captured output can be stale — re-run against HEAD before accepting a CRITICAL).

# Gotchas Reference

### 1. Claude JSONL bloat is tool_result payloads, not snapshots `ingest`
83% of bytes are `user` records carrying `tool_result` stdout/file-reads/base64; `file-history-snapshot` is ~247-byte pointers (~0.5%). Filter oversized successful tool_results; don't over-invest in snapshot stripping.

### 2. Synthetic user messages pollute correction mining `distill`
Claude `user` records include `<local-command-caveat>` and `isMeta` synthetic turns. Exclude before applying correction regex ("no, actually", "don't", "prefer").

### 3. Compaction summaries are pre-distilled — mine them first `distill`
Claude `isCompactSummary` records and Codex `compacted.replacement_history` already contain distilled decisions/bugs. Cheapest extraction ROI in the whole corpus.

### 4. Codex reads CLAUDE.md on this machine `serving`
`~/.codex/config.toml` has `project_doc_fallback_filenames = ["CLAUDE.md", ".claude/CLAUDE.md"]` and `~/.codex/AGENTS.md` symlinks to `~/.claude/CLAUDE.md`. One managed block in CLAUDE.md serves both harnesses; separate AGENTS.md blocks only where a project has its own.

### 5. UserPromptSubmit is capped and can block `serving`
10k-char cap (overflow spills to a file the model may not read), 30s timeout, exit 2 blocks AND erases the user's prompt — hook must never exit 2, and must fail open (empty output) on any internal error.

### 6. sqlite3 CLI absent on this machine `env`
Use Python's stdlib `sqlite3` module everywhere, including scripts/tests.

### 7. FTS5 bm25 magnitude is corpus-size dependent — never use it as an absolute signal `retrieval`
On a 2-card corpus bm25() returns -0.0 (idf collapses when a term appears in half the docs); on a 4-card corpus a strong match reaches -5. Min-max alone leaks lone weak matches (1.0 by construction); absolute scaling returns nothing on small corpora. The anchor is query-token coverage (ADR-012, rank._coverage).

### 8. save_insight has no sources field — synthesized provenance `serving`
The frozen toolspec omits `sources` but cards require provenance, so mcp_server synthesizes `[{"path": "agent://save_insight", "kind": "assistant"}]`. Honest for an unreviewed proposal; the human attaches real sources on approval.

### 9. Workflow journals live inside session dirs and use their own record types `ingest`
`~/.claude/projects/*/subagents/workflows/*/journal.jsonl` contains `started`/`result` records; some transcripts contain truncated/invalid JSON lines. The claude adapter skip-and-logs all of these — noisy stderr on full backlog ingest is expected, not a failure.

### 10. Cards must never quote golden no-answer trap queries verbatim `eval`
Sessions working on afterwit itself discuss the eval traps; the distiller faithfully quoted one into a card body, which then permanently matched that trap (self-referential contamination, found 2026-07-06). Reworded the card; `afterwit eval` leak lines now print the leaking card titles so this is diagnosable in seconds. If a trap leaks right after distilling afterwit sessions, check for quotation before suspecting ranking.

### 11. LLM-distilling docs mostly yields zero cards — by design, not by bug `distill`
The prompt's not-in-the-code test drops anything recoverable "by reading the code or docs" — and for doc-sourced events, everything is in the doc. token-kit's 322-event ADR yielded 0 cards; only context-rich gotchas survive (acme_hr got 7). Rich doc extraction is the aw-sweep skill's job (doc-specific rubric) or SPEC §6.4's deterministic doc_ref path; don't burn LLM quota bulk-distilling docs.

### 12. Codex quota exhaustion mid-campaign = mass distill-skips, safe to re-run `distill`
`codex exec` exits 1 with "usage limit" in stderr; distill logs skip-and-continues (log.md `distill-skip` lines name each file). Nothing corrupted. Re-run the same command after quota reset, or switch `--driver claude-p` for small remainders. There is no per-session distill checkpoint yet, so re-runs re-process newest-first (dedupe absorbs it).

### 13. Embedding cold-start is per-PROCESS — CLI recall pays it every call, MCP doesn't `retrieval`
Every `afterwit recall` CLI invocation is a fresh process, so the ONNX/MiniLM load (~1.5-3.2s measured) is paid per call, not amortized. The MCP server is a long-lived stdio process per harness session — it loads once and stays warm, so agent-facing latency is fine. `afterwit inject` never imports the model at all (ADR-016, pinned by test). If interactive CLI recall latency ever matters: a tiny embedding daemon or vector-free CLI flag; don't move model load into the hook path.

### 14. `changed_files()` returning `None` is not `set()` `staleness`
`gitmeta.changed_files(repo, sha)` returns `None` for "cannot tell" (unknown sha — shallow clone, or a commit authored on another device and never fetched) and `set()` for "nothing changed". Conflating them makes every unresolvable card read as fresh. `consolidate.mark_stale` branches on `is not None` and falls back to the existence check; `test_unknown_commit_returns_none_not_empty` pins it.

### 15. A project absent from `projects_root` is ignorance, not drift `staleness`
On a device where a project was never cloned, `_resolve_project` returns `None` and `mark_stale` **skips** the card. Flagging would demote every card for every repo the device doesn't have. Corollary: drift is only ever computed on machines that actually hold the code.

### 16. A commit diff cannot see a path that was never tracked `staleness`
`git diff --name-only A..HEAD` lists only paths git knows about. A card citing `/tmp/scratch.css`, a typo'd path, or a file belonging to another project produces an **empty** intersection and reads as fresh. This silently un-flagged 3 real cards when commit-diff replaced the existence check. `consolidate.mark_stale` therefore unions `moved` with `dead` (no cited file exists); `test_anchored_card_with_dead_pointer_is_stale` pins it.

### 17. `git rev-list --before` filters on COMMITTER date, not author date `staleness`
Backdating a commit with `git commit --date=...` moves only the author date, so `gitmeta.commit_at()` (used by the backfill) will not find it. Tests that backdate must set `GIT_COMMITTER_DATE` **and** `GIT_AUTHOR_DATE` — see `tests/test_gitmeta.py::_run`. Cost: two initially-failing tests that looked like a code bug.

### 18. File-level drift demotes rationales unless scoped to pointers `staleness` `ranking`
`git diff` is file-granular, so a card citing `src/afterwit/distill.py` goes stale when *any* line of that file changes — including edits unrelated to the card's claim. Demoting all types on drift dropped a verified, still-true `decision` card below the relevance floor (eval recall@3 100% → 88%). Only `capability` cards are pointers to code (ADR-014); `rank.DEMOTE_STALE_TYPES` restricts demotion to them. Drift is still recorded on every type for lint.

### 19. A derived column dies on `rebuild()` — recompute at every call site `staleness`
`index_db.rebuild()` does `DELETE FROM cards`, so any column not stored in frontmatter (here `stale`) resets to its default. `afterwit sync` rebuilds at the END of the nightly, *after* `lint` computed the flags, so drift demotion was inert during all serving. Any future derived column must either live in frontmatter or be recomputed by every `rebuild()` caller (`cli._restale`).

### 20. `Path(base) / "/abs/path"` discards base `staleness` `security`
Python's `/` operator returns the absolute right-hand side. A card citing `/tmp/x.css` resolved to `/tmp/x.css`, so an existence check on a live scratch file said "valid pointer" while git's diff never mentioned it. `consolidate._dead_pointer()` resolves and requires `is_relative_to(base.resolve())`.

### 21. Redaction at ingest defends the wrong door `security` `redaction`
Adapters called `redact()`; the distiller, `mcp save_insight` and `afterwit queue` all write cards without touching an adapter, and the **card** is what `git push` publishes. `cards.save()` now calls `redact.sanitize()`. If you add a new card writer, you get this for free — do not bypass `cards.save()`.

### 22. `argparse` defaults silently override config `config`
`cli.py` gave `afterwit run --driver` a default of `"claude-p"` and always forwarded it, so `runner.run`'s `driver_name or cfg.distill_driver` fallback was dead code. Every nightly ran `claude-p` while `config.toml` said `codex`. Any flag that shadows a config key must default to `None`.

### 23. An email regex eats `git@github.com` `redaction`
`[^@]+@[^.]+\.[a-z]{2,}` matches every SSH clone URL and every `module@file.py` reference. Redacting them corrupts `repo_url`, which is the cross-device identity key (ADR-020). `redact.py` exempts a `git` local-part and a deny-list of code file extensions.

### 24. A model JSON verdict can carry duplicate keys `security` `auto-review`
`json.loads` of `{"verdict":"reject","verdict":"approve"}` returns approve — Python keeps the last of duplicate keys. A reviewer LLM (or a prompt-injected one) could exploit this to force approval. `review._parse` uses an object_pairs_hook that raises on any duplicate key, then abstains. Applies to any LLM-returns-JSON gate.

### 25. An audit that quotes captured output can be stale `security` `audit`
A read-only auditor reported a CRITICAL secret-in-SQLite leak with real command output. It was stale — captured from a probe before the sanitize calls were committed; a fresh re-run showed the redaction marker. Re-run the exact repro against current HEAD before accepting a CRITICAL. `git log <audited-sha>..HEAD -- <file>` shows whether the file even changed.

### 26. An unreachable index masquerades as an empty knowledge base `mcp` `silent-failure`
`index_db.connect()` in rw mode does `mkdir(parents=True)` + `executescript(SCHEMA)`. So a wrong or stale `db_path` does not raise — it **creates a blank database**, and every MCP tool then returns the normal "no known history — proceed normally" text. The agent concludes the user has no knowledge and stops asking, while the real 463-card index sits untouched elsewhere. A silent wrong answer, not an error. `mcp_server.dispatch` now checks `db_path.exists()` first and returns an explicit UNREACHABLE / run-`afterwit doctor` message; the guard is mutation-tested. Any rw-connect-on-user-supplied-path has this shape.

### 27. Config-dir rename strands long-lived servers on a dead path `migration` `mcp`
The one-time `~/.harness_helper` → `~/.afterwit` migration renames the directory out from under an already-running MCP server, which keeps resolving the old absolute path and fails every call with sqlite's bare `unable to open database file`. Agents reported "afterwit's SQLite database cannot be opened" while the DB was provably healthy (`integrity_check: ok`). Two lessons: a migration must assume a stale process is holding the old path, and the failure text an agent sees must name the path and the remedy — a raw sqlite error is unactionable and reads like corruption.

### 28. Shipped skills hardcoded `AW="aw"`, a command only packaged installs have `install` `path`
The three claude skills and the codex AGENTS.md block shelled a bare `aw`. `aw`/`afterwit` are console scripts, on PATH only after `pip`/`uv tool install` — never for the default clone-and-run. Every checkout user's `$AW recall` died with `aw: command not found` while MCP/hook/cron worked fine, because those resolve through `_server_argv` and the skills did not. `install._skillify` now rewrites the placeholder at copy time through the same resolver. Lesson: one resolver for "how do I invoke myself here", used by *every* surface.

## ADR-024: Rename-safety comes from packaging, not from cleverness

**Date**: 2026-07-13 | **Status**: Accepted | **Tags**: `install`, `resilience`, `paths`

### Context
`install` bakes an absolute repo path into three surfaces at once: the MCP args in `~/.claude.json`, the SessionStart hook command, and the systemd `ExecStart`. Move or rename the checkout and all three die simultaneously.

The property that makes this nasty: **afterwit cannot warn you.** Every entry point that could detect the breakage — the hook, the MCP server — is itself invoked through the dead path. The detector dies with the thing it would detect. This is the same shape as the `~/.harness_helper` → `~/.afterwit` rename that stranded a long-lived MCP server on a vanished path (Gotcha #27), and it is why that outage was silent for days.

### Options Considered
1. **A stable shim** at `~/.afterwit/bin/afterwit` that resolves the real entry point at call time. Adds a file, a platform story (Windows has no shebang), and a second thing to keep in sync. Rejected: machinery to paper over a packaging problem.
2. **Search for the checkout at runtime** if the baked path is missing. Guessing where the user moved their code. Rejected — a wrong guess is worse than a clean failure.
3. **Never reference the checkout: install the package.** Chosen as the answer for users.
4. **Detect + one-command repair** for the dev case. Chosen as the complement.

### Decision
Two paths, and be honest about which is which.

- **Packaged install (`uv tool install` / `pip install`, non-editable) is the rename-safe one, and is what OSS users get.** `_server_argv` finds no `pyproject.toml` beside the module and falls through to the console script, so the config contains **no repo path at all**. The source folder can be moved, renamed or deleted and nothing notices, because nothing points at it. Immune by construction, not by mechanism.
- **A dev checkout keeps `uv run --project <repo>`** — it buys a live working tree, and it pays for that with path-coupling. That is a real trade, not a defect. What it lacked was a recovery path.

`afterwit doctor` compares the `--project` path in the registered MCP args against `_repo_root()` — the old path is still sitting in the config, and the running module knows the new one, so no new state file is needed. `afterwit doctor --fix` re-runs the installers, which rewrite every surface from wherever afterwit is running *right now*; running it from the moved checkout is therefore the repair. The check is skipped entirely for packaged installs, which have no `--project` to go stale.

### Consequences
- The recovery for a moved checkout is one command, but it must be run **manually** — by construction nothing can trigger it for us, since a relocated install cannot reach its own hook. Accepted; the alternative is guessing.
- Doctor is the only thing standing between a silent breakage and a confused agent three days later. It should run in the nightly (open).
- README must state plainly that a git checkout is path-coupled and a packaged install is not, so users pick with their eyes open.

### 29. The nightly can catch a silent breakage — except the one that kills the nightly `resilience` `install`
`afterwit run` ends with a `doctor` stage, so an index that is healthy-but-unreachable fails the run (nonzero → systemd marks the unit failed) instead of quietly distilling into a database no agent can open. It runs LAST because `sync` rebuilds the index, so it validates the state the next session will meet. Inherent blind spot (ADR-024): it cannot catch a **moved checkout**, because relocating the repo kills the unit's own `ExecStart` — the nightly never runs to complain. That one needs `afterwit doctor --fix`, run by hand from the new location.

### 30. Per-session MCP servers are ~2.5 MB, not a leak `mcp` `perf`
20 concurrent `serve-mcp` processes (oldest ~2 days, one per open Claude/Codex session) totalled **56 MB** — 2.3–2.6 MB each. They are children of live sessions and exit with them. They stay small because `mcp_server` never eagerly loads the embedding model; `embed` is imported lazily on the paths that need it. Measure before optimising: the process *count* looks alarming and the *cost* is nil.

### 31. `sqlite3.OperationalError: unable to open database file` tells an agent nothing `mcp` `dx` `silent-failure`
An agent shelled `aw recall`, got a bare traceback ending in that line, concluded "the historical knowledge lookup is unavailable on this machine", and reasoned on without 463 cards it could have had. sqlite's text names no path, no cause and no remedy, so "unreachable" is indistinguishable from "empty" — the failure mode every afterwit outage has taken. `index_db.connect()` is now the single door: every open failure becomes an `IndexUnavailable` carrying path, file/dir existence, read+write permissions, `HOME`, `uid`, sqlite's own message, and "this is a BROKEN INSTALL, not an empty knowledge base". `cli.main` catches it (exit 1, no traceback) and `mcp_server.dispatch` returns it verbatim. **The trigger for the original report was never reproduced** — WAL/`-shm`, unwritable `-shm`, read-only directory and external-writer contention were each tested and each opened fine. That is exactly why the error must diagnose itself: the environment that produced it is usually not one you can re-enter.

### 32. `immutable=1` is not a fix for a read-only WAL open `sqlite` `data-integrity`
The obvious escape from a WAL/`-shm` permissions problem is `file:db?immutable=1`. It opens — and reads a stale pre-WAL snapshot: a test DB with a committed row returned `no such table: cards`. It would silently serve an EMPTY or outdated index rather than erroring. Never use it on a database anything else can write.

## ADR-025: An installer that cannot verify its own work is not an installer

**Date**: 2026-07-14 | **Status**: Accepted | **Tags**: `install`, `resilience`, `dx`

### Context
Every outage this project has shipped has the same shape: a **healthy index that no agent could reach**, while every surface reported success. The MCP server registered under a dead name and spawn-failed silently. The skills shelled an `aw` that was never on PATH. A WAL read died inside a sandbox. In each case `install` printed "ok" and the user found out days later, from a confused agent that concluded the knowledge base was *empty*.

The common root is not any one bug. It is that **install reported intent, not outcome** — it wrote config and never asked whether the config worked.

### Decision
`afterwit init` is the one command, and it now ends by proving itself:

1. Puts `aw` on PATH when missing (`uv tool install --editable`); a packaged install already has it. Best-effort — never aborts the install.
2. Wires both harnesses (MCP + hook + skills) and the nightly, as before.
3. **Builds the index.** Without it `recall` answers "no index yet", which agents read as "this user has no knowledge" — the unreachable-vs-empty confusion again, this time on a brand-new machine.
4. **Runs `doctor` and returns its exit code.** init walks the same path an agent walks and fails if any door is shut. "done" now means checked, not attempted.

The nightly (`afterwit run`) closes the same loop on an ongoing basis (Gotcha #29), and `doctor --fix` is the recovery after a move (ADR-024).

### Consequences
- `afterwit init` can now exit nonzero on a machine where the files were all written correctly — that is the intended behaviour, not a regression. The install is only as good as the reachability it can demonstrate.
- init costs one extra subprocess (doctor spawns the CLI) and one index build. Both are one-time.
- Verification is mutation-tested: making init swallow doctor's verdict turns a test red.

## ADR-026: Reliability gates must cover produced state, not attempted work

**Date**: 2026-07-14 | **Status**: Accepted | **Tags**: `reliability`, `review`, `sync`, `security`

### Context
The full-system audit found several paths that reported an attempted operation as success without checking the resulting state: a scheduler could not find the configured distill driver yet the nightly stayed green; sync rebuilt cards without vectors; high-confidence model output bypassed the review queue; and pending reviews existed only in a device-local cache. Concurrent sandbox readers also copied the WAL database into one shared temporary filename, corrupting one another's snapshots.

### Options Considered
1. Add recovery scripts and warnings around each symptom. Rejected: the same invalid state remains possible through another caller.
2. Validate each shared choke point and make the canonical wiki carry every cross-device state. Chosen.
3. Replace SQLite/git with a hosted service. Rejected: it violates the local-first and rebuildable-cache principles.

### Decision
- Driver execution resolves an absolute executable and the runner fails an all-skipped distill attempt.
- Every read-only fallback owns and verifies a unique SQLite snapshot.
- A rebuild is complete only when active cards and vectors have equal coverage.
- Model confidence never substitutes for verification. Novel LLM cards queue; legacy active-unverified cards are backfilled. Auto-review requires readable, located source excerpts and abstains otherwise.
- Pending review cards are markdown under `review/`; SQLite mirrors them and can be rebuilt on another device.
- Deterministic imports (existing memory/docs and schema metadata) may be verified at import because they copy user-controlled sources without model judgment.
- Mutation endpoints require a per-process CSRF token even though the server binds localhost.

### Consequences
- First sync after this change can rewrite legacy cards to scrub home paths, cap source lists, and create pending-review files. This is intentional migration churn.
- A new device restores both active cards and pending reviews from the wiki; device-local servings and distill ledgers remain caches by design.
- Cards with missing or synthetic provenance remain human-reviewable but cannot be auto-approved.
- Deterministic imports expand the corpus, so relevance is anchored quadratically by query-token coverage; the required eval remains recall@3 100%, MRR 1.000, and no-answer precision 100%.

### 33. A scheduler PATH is not an interactive PATH `distill` `systemd`
An nvm-installed `codex` was visible interactively and absent from the systemd user service. Never use a bare driver name in unattended execution; resolve the absolute executable or fail preflight before counting work.

### 34. One temp filename is shared mutable state `sqlite` `concurrency`
Independent read-only MCP processes copied the database and WAL over the same `/tmp/afterwit-ro-<uid>/index.db`, producing `database disk image is malformed` and `no such table: cards`. Every snapshot must have a unique directory and pass `quick_check` before serving.

### 35. Confidence is not review `review` `trust`
`confidence >= 0.8` wrote an active unverified card while the UI displayed only `review_queue`, stranding hundreds of claims outside the normal approval lifecycle. Confidence may prioritize a queue; it can never cross the trust boundary.

## ADR-027: Decision ledgers are records, not Markdown syntax trees

**Date**: 2026-07-14 | **Status**: Accepted | **Tags**: `ingest`, `docs`, `precision`, `migration`

### Context
SPEC §6.4 requires section-level extraction from `ADR.md` and `DECISIONS.md`. The first deterministic importer interpreted “section” as every Markdown heading. A live 540-file import consequently promoted generic nested headings such as `Context`, `Decision`, `Consequences`, and numbered gotchas into 1,422 decision cards. The import was structurally valid and fully embedded, but semantically noisy.

### Options Considered
1. Keep every heading and depend on ranking. Rejected: irrelevant cards still pollute graph, review, stats, and no-answer precision.
2. Select headings by depth. Rejected: ADR ledgers use H2 entries while build-decision ledgers use H3 entries.
3. Select explicit record identifiers and retain one card per identified decision. Chosen.

### Decision
Exact `ADR.md` / `DECISIONS.md` files promote only headings beginning with a recognized decision identifier: `ADR-*`, `BD-*`, `DD-*`, `DECISION-*`, or `D-*`. Provenance records both the file and heading. The materialization marker is versioned; the next docs ingest reparses only legacy decision ledgers, removes obsolete deterministic cards from the wiki and live index, and then returns to normal checkpoint skipping.

### Consequences
- The live corpus contracts substantially without deleting any model- or human-authored card.
- Heading depth remains free to match each project's documentation convention.
- A decision ledger that adopts a new identifier family requires extending one regex and its contract test.

### 36. A parsed section is not necessarily a knowledge record `docs` `precision`
Markdown parsers expose structural headings; knowledge retrieval needs semantic records. Never map every nested heading in an ADR ledger to a decision card merely because the parser emits an event for it.

### 37. A no-answer trap can become a real question as the corpus grows `eval` `precision`
The Stripe/billing capability trap became legitimately answerable when deterministic docs imported a notification-system report with a Billing & Subscription section. When a trap leaks after corpus expansion, inspect the result first: replace a stale trap if the knowledge is real; tune ranking only for an incidental match.

### 38. Read-only lint cannot refresh derived drift state `lint` `sqlite`
`afterwit lint` opened the index with `readonly=True` and then called `mark_stale()`, whose first operation is `UPDATE cards SET stale=0`. The command must use the writable connection because drift flags are a derived cache that lint explicitly recomputes and persists.

## ADR-028: Preserve memory-link identity across deterministic import

**Date**: 2026-07-14 | **Status**: Accepted | **Tags**: `graph`, `memory`, `ingest`, `wikilink`

### Context
Claude memory files link sibling pages by filename, while imported cards use the human-readable `name` frontmatter as their title. For example, `[[feedback_state_tracking]]` points to a file whose imported card is titled “State tracking for multi-agent handoff.” Title-only graph resolution therefore reported a broken edge even though the target source existed. Separately, literal `[[example]]` syntax inside code spans was indexed as a real edge.

### Options Considered
1. Add a permanent aliases table to SQLite. Rejected: the wiki remains canonical, and a derived schema is unnecessary for two source conventions.
2. Preserve filename titles instead of human titles. Rejected: it degrades retrieval and display.
3. Canonicalize sibling memory links during deterministic import and ignore code spans during link extraction. Chosen.

### Decision
The memory adapter resolves a local `[[filename]]` target to that sibling file's frontmatter `name` before materialization. A versioned marker rematerializes existing memory cards once. `Card.wikilinks()` strips inline and fenced code before extracting graph edges.

### Consequences
- Memory source files remain untouched and continue using their native link convention.
- Rebuilt indexes and cross-device graph views resolve the same canonical title edges.
- Missing siblings remain visible to lint as genuinely broken links.

## ADR-029: A read-only reader degrades in tiers; it never reports a healthy index as unreachable

**Date**: 2026-07-14 | **Status**: Accepted | **Tags**: `sqlite`, `sandbox`, `reliability`, `wal`

### Context
Reading a WAL database needs a writable *directory* (`-shm`/`-wal` are written beside the db
even to answer a SELECT), so sandboxed agents cannot read the index in place. ADR-024 solved
this by snapshotting db + `-wal` into TMPDIR, which replays the WAL and reads exact data.
That fix assumed TMPDIR always exists. Under `codex --sandbox read-only` it does not: /tmp,
/var/tmp, /usr/tmp *and* cwd are all mounted read-only, and `tempfile.gettempdir()` raises
`FileNotFoundError` rather than returning a bad path. Both the original and the hardened
snapshot code therefore failed, and `aw recall` reported `index UNREACHABLE` against a
perfectly healthy 1,497-card index — the exact failure this project exists to prevent.

### Options Considered
1. Require a writable TMPDIR and fail otherwise. Rejected: it fails on a real, common sandbox,
   and "afterwit is broken" is precisely the message that makes agents stop asking.
2. Always use `immutable=1`. Rejected: it cannot replay the WAL, so it silently serves stale —
   or empty — data (Gotcha #32). Correctness must not be traded for reach.
3. Tier the strategies, most exact first, and guard the inexact one. Chosen.

### Decision
`_readonly_conn` tries, in order: (1) direct read when the directory is writable; (2) a private
TMPDIR snapshot with the `-wal` copied so SQLite replays it — exact; (3) `immutable=1` when
nothing on the machine is writable — needs no `-shm` at all. Tier 3 cannot replay a WAL, so
writers now `PRAGMA wal_checkpoint(TRUNCATE)` at the end of `rebuild()`, leaving the db file
complete. If tier 3 nonetheless finds an un-replayable WAL *and* reads zero cards, it raises
rather than returning an empty result set.

### Consequences
- Tier 3 is faster than tier 2 (583ms vs ~1000ms): it copies nothing.
- Tier 3 can serve data staler than the last checkpoint. Bounded by the nightly rebuild, and
  strictly better than the alternative of reporting a healthy index as unreachable.
- The empty-WAL guard is the load-bearing one: stale-but-present degrades gracefully, whereas
  *empty* is indistinguishable from "this user has no history" and must always fail loud.

### 39. A close()-only cleanup leaks when nothing calls close() `sqlite` `tmp`
Per-reader snapshots were reaped in `_SnapshotConnection.close()`, but `recall`, `stats` and
`doctor` never close — they just exit. Each snapshot is a full copy of the private knowledge
db, so this stranded 188MB of `/tmp` in a single day. Cleanup is now `atexit`-registered, with
an age-based sweep for readers killed before `atexit` can run.

### 40. `gettempdir()` raises — it does not return a bad path `sandbox` `tempfile`
`codex --sandbox read-only` leaves no writable temp directory anywhere, so `tempfile` raises
`FileNotFoundError` from `gettempdir()` itself. Code that assumed "TMPDIR always exists" fails
at a line that never appears in the traceback of the feature it breaks. Probe with
`_tempdir() is None`, never `os.access(tempfile.gettempdir(), os.W_OK)`.

## ADR-030: The drift alarm must stay quiet enough to hear

**Date**: 2026-07-14 | **Status**: Accepted | **Tags**: `adapters`, `ingest`, `schema-drift`, `observability`

### Context
Adapters skip-and-log unknown record types (SPEC §6) so a harness schema change degrades
instead of crashing. The log line is the alarm that tells us a harness changed under us. It
was firing on every recent session, from two unrelated causes:

1. **Codex added record types.** `world_state` and `inter_agent_communication_metadata` first
   appear on 2026-07-09, so *every* session after that date warned — 83/265 of the corpus and
   100% of current sessions.
2. **We were parsing a file that is not a transcript.** The claude source globbed
   `projects/**/*.jsonl`, which swept in `subagents/workflows/wf_*/journal.jsonl` — the
   workflow runner's LEDGER, whose schema is `started`/`result`. A transcript parser fed a
   ledger will of course not recognise a single record.

An alarm that fires constantly is not an alarm. The next genuinely unannounced record type
would have landed in that noise, and we would have silently stopped learning from a harness.

### Options Considered
1. Lower the log level / mute the warning. Rejected: it removes the only drift detector.
2. Add every observed type to a drop set. Rejected for the journal: the file should never
   reach the parser at all, so dropping its records treats the symptom.
3. Drop reviewed-benign record types, and fix source selection so a non-transcript never
   reaches a transcript parser. Chosen.

### Decision
`world_state` (an AGENTS.md snapshot — real content, but the docs and memory adapters already
ingest those files, so parsing it here would feed the user's own instructions back as
findings) and `inter_agent_communication_metadata` (`{"trigger_turn": true}`) join an explicit
`_DROP_TYPES` set, as do claude's `frame-link`/`agent-name`/`pr-link`. Every entry is a
reviewed judgement that the record carries no knowledge — never a blanket mute. Source
selection excludes `journal.jsonl` by name.

### Consequences
- Warnings fall from 94 sessions to **0 across 1,164 files**, with 71,423 events still
  extracted — the drop removed plumbing, not content.
- The alarm is load-bearing again and mutation-tested: a new unknown type still warns.
- `rglob` is retained deliberately. `subagents/**/agent-*.jsonl` (554 files) ARE transcripts
  and carry real work; narrowing the glob to `<slug>/<uuid>.jsonl` would have silenced the
  warning by throwing that knowledge away, which is the failure this system exists to prevent.

### 41. A harness will add record types without telling you `adapters` `codex`
Codex shipped two new top-level record types mid-July. Adapters must treat unknown types as
news, not as errors — and the drop set must be an explicit list of types someone READ and
judged empty, so the difference between "known-benign" and "never seen before" survives.

### 42. Not every .jsonl under a transcript root is a transcript `ingest` `glob`
`~/.claude/projects` also holds the workflow runner's `journal.jsonl` (`started`/`result`).
Select transcript sources by what they ARE, not by extension: the codex adapter globs
`rollout-*.jsonl` and never had this bug.

### 43. A flag default that shadows a config key makes the config key dead `cli` `config`
Gotcha #22 recorded this for `afterwit run`; `afterwit distill` had the identical bug and it
survived the fix. `--driver` defaulted to `"claude-p"` and was forwarded unconditionally, so
`distill_driver = "codex"` never took effect: every manual distill silently ran the wrong
model, on the wrong quota pool, at the wrong price. When a flag shadows a config key its
default MUST be `None`, and it must be forwarded only when the user actually passed it. Grep
for the *pattern*, not the one command that reported it.

### 44. The ledger stops re-spend; it does not stop spend `distill` `cost`
`afterwit stats` now reports the `distilled` ledger (ADR-015): sessions driven, cards
produced, zero-card sessions (recorded precisely so they are never re-bought), last run, and
`not_yet_distilled` — the backlog the next run would pay for. The ledger was already correct
and already enabled in both the nightly and the CLI; what was missing was any way to SEE it,
which is why "am I paying twice?" was unanswerable without opening sqlite.

## ADR-031: Rediscovering approved knowledge corroborates it; it never re-asks

**Date**: 2026-07-14 | **Status**: Accepted | **Tags**: `review`, `dedupe`, `trust`, `ux`

### Context
`postprocess.process` sent any duplicate of an existing card to `merge`, EXCEPT when the
target was `verified` — those were queued as `possible-duplicate`. The intent was sound:
`wiki.execute`'s merge does `existing.body = cand.body` (SPEC §7.1, "newer body wins"), so
merging into an approved card would replace human-approved text with fresh model prose.
Queuing avoided that by asking the human.

The effect was perverse. Every approval became a permanent generator of queue noise: each
later session that rediscovered the same knowledge re-queued it, forever. Measured on the
live corpus: **116 of 371 queued cards were duplicates of cards the user had already
approved** — 31% of the queue was work the user had already done, waiting to be done again.
Approving must reduce future work. A review gate that punishes the person using it is a
review gate that stops being used.

### Options Considered
1. Keep queuing; dedupe in the UI. Rejected: the human still decides, which is the cost.
2. Merge unconditionally. Rejected: it silently rewrites approved claims — a far worse bug
   than the one being fixed, and it voids ADR-011's gate.
3. Split the two things merge conflates — the CLAIM (reviewed) and the EVIDENCE (not) —
   and let only evidence accrue to an approved card. Chosen.

### Decision
A NEAR-EXACT duplicate (>= DUP_THRESHOLD) of a verified card merges. `wiki.execute` keeps the approved body when
`existing.verified`, unioning sources/files and re-anchoring the commit; it logs
`corroborate` rather than `merge`. The body is what a human reviewed; sources are evidence
that the claim was independently rederived, which is a reason to trust it more, not a reason
to re-ask. `review_queue` runs the same check deterministically BEFORE the model, so queued
duplicates drain at zero token cost.

### Consequences
- The user's queue drops by the 116 cards they had already approved; the next review spends
  116 fewer model calls.
- Approved cards accumulate provenance over time and re-anchor to newer commits as they are
  rediscovered — corroboration now clears drift instead of generating review work.
- A verified card's wording can only ever be changed by a human or by supersede. Both are
  mutation-tested: letting merge touch an approved body turns a test red.
- Queued cards are inert and are never re-evaluated by the fixed logic, so the existing
  backlog needs one drain pass (`afterwit review`) to clear. Measured on the live queue:
  115 of 371 drained deterministically, 0 approved bodies rewritten.
- The REWORDED path (title-token overlap, bodies differ by definition) still queues when the
  target is verified. Merging there keeps the approved body, which silently DISCARDS the
  candidate's differing claim — safe when the two say the same thing, wrong when they do not,
  and a 0.75 title-token overlap cannot tell those apart. It costs exactly 1 card of queue
  noise on the live corpus and removes the only path that could destroy a claim unseen. An
  UNVERIFIED target still merges, so the audit-2026-07-06 fix (8 rewordings of one preference
  written as parallel cards) is unaffected.

## ADR-032: An installer's hooks need a key, or reinstall appends instead of converging

**Date**: 2026-07-14 | **Status**: Accepted | **Tags**: `install`, `hooks`, `idempotency`, `performance`

### Context
`_hook_present` tested for afterwit's hooks by exact command-string equality. When the
command changed — the repo moved, or `--no-sync` was added — the old hook matched nothing
and a second was appended. The live machine had **two** SessionStart hooks and **two**
UserPromptSubmit hooks, both firing on every prompt: double the injected tokens, double the
latency on a path speced at p95 < 200ms (SPEC §6), and two `servings` rows per injection,
which quietly corrupts the usage signal that ranking learns from. The MCP entry never had
this bug because it is keyed by name and converges.

### Decision
Identify afterwit's hooks by what they ARE — an `afterwit … inject … --mode <mode>`
invocation — not by how the argv was spelled. `_set_hook` removes every afterwit hook for a
mode and appends the current one, leaving hooks owned by anyone else untouched.

### Consequences
- Reinstall converges from any prior spelling, including one written before a repo move.
- The blast radius is deliberately narrow: only commands containing `afterwit … inject` are
  removed. The user's own hooks on the same events survive, and a test pins that.

### 45. A deferred MCP tool looks exactly like an absent one `mcp` `claude-code`
Claude Code lists MCP tools by name and loads schemas on demand; the tool is unusable until
`ToolSearch` fetches it. The operator block said the tools were "already warm" and to use the
shell CLI "only if the afterwit MCP tools are absent from this session" — so Claude read an
empty toolset, concluded absent, fell through to the sandboxed CLI, and reported "afterwit is
unavailable" with a healthy index one call away. Instructions must say: load them first, THEN
conclude they are missing.

### 46. Push and pull are not interchangeable, and only one of them is visible `hooks` `usage`
Claude has injection hooks (knowledge is pushed, threshold-gated, usually silent); Codex has
none and must pull via MCP (ADR-003). So Codex visibly calls `recall` and Claude appears never
to use afterwit at all. That asymmetry is by design, but it means Claude's pull path is
exercised far less — and its silent failure went unnoticed for far longer.

### 47. A throwaway diagnostic is not evidence `analysis` `discipline`
While auditing the drain I flagged a "false-positive merge" by printing each card's
best-lexical match — but `process` merges into `title_dup`, a different card. At 0.06
similarity the "best" match is noise, so the pairing was fiction: the card was a byte-identical
self-duplicate (`body_sim=1.000`, same id — a card that was both approved AND left pending by
the ADR-026 backfill). The conclusion drawn from the real code path was the opposite of the one
drawn from the ad-hoc script. Reproduce a finding through the code under test before believing
it, especially when it is about to justify a behaviour change.

## ADR-033: `auto_review = true` must drain the queue on the nightly, not just arm a button

**Date**: 2026-07-14 | **Status**: Accepted | **Tags**: `review`, `runner`, `automation`

### Context
The nightly runner distills new cards INTO the review queue every night. `cfg.auto_review`
was read in exactly two places — the manual `afterwit review` command and the UI button —
and nowhere in `runner.py`. So a user who set `auto_review = true` (expecting the queue to be
triaged automatically, as the README promises: "nobody triages 200 notes; if you won't, an
LLM can") got a queue that only ever grew. It reached 257 pending, of which 122 were the
legacy backfill (ADR-026) and 124 were freshly distilled — none reviewable without the user
hand-running a command they did not know they had to run.

### Options Considered
1. Document that you must run `afterwit review` yourself. Rejected: a feature you have to
   remember to run by hand is a feature that stays off; the flag already promised automation.
2. Always review in the nightly. Rejected: auto-review spends reviewer tokens; it must stay
   opt-in behind the flag the user already set for exactly this.
3. Add a gated, time-bounded review stage to the nightly. Chosen.

### Decision
`runner` gains a `review` stage that runs `review.review_queue` when `cfg.auto_review` is
true, placed before write_back/regenerate/sync so approvals are indexed and pushed in the
same run. It is bounded by the run deadline (like distill), not by a count: `review_queue`
gained a `deadline` parameter and stops when the clock is spent, reporting `stopped`. A large
backlog clears across several nights instead of blowing one run's timeout or spending an
unbounded pile of reviewer tokens at once.

### Consequences
- With `auto_review = true`, the queue now drains without manual intervention; abstains and
  anything past the deadline stay queued for the human.
- The reviewer is still the independent driver (ADR-021 separation of duties) and every
  deterministic veto still fires before the model.
- A user who wants to keep hand-reviewing simply leaves `auto_review = false` (the default),
  and the stage no-ops.

### 48. A config flag with no reader is a promise the system does not keep `config` `review`
`auto_review = true` gated the manual command and the UI but was never read by the nightly, so
the automated behaviour it implied never happened and the queue grew unbounded. When a flag
names an outcome ("auto review"), grep every entry point that should honour it — a flag read
in one path and ignored in the path that matters is worse than no flag, because the user
believes it is handled.

## ADR-034: Settings belong in the UI, and model lists belong to the harnesses

**Date**: 2026-07-23 | **Status**: Accepted | **Tags**: `config`, `ui`, `harness`, `cross-platform`

### Context
Every knob afterwit has — which harness CLI distills, which model it spends, the injection
caps, the paths — lived only in `~/.afterwit/config.toml`, a file the UI displayed four fields
of and could not change. The model ids in it were typed by hand against no list, and one of
them (`DEFAULT_CODEX_MODEL = "gpt-5.6-terra"`) was already stale versus the user's own
`~/.codex/config.toml` (`gpt-5.6-sol`), so the documented "fallback" silently ran a model the
user had migrated off. Meanwhile `distill_effort` was dropped on the `claude-p` path because
the CLI "has no effort flag" — true when that comment was written, false since
`claude --effort` shipped.

Underneath sat a portability bug: five call sites each spelled `Path.home() / ".claude"` by
hand and none honoured `CLAUDE_CONFIG_DIR` or `CODEX_HOME`. A user who relocates a harness
config gets an install written where nothing reads it, a doctor that reports the harness
missing, and an ingest that mines nothing — with no error anywhere.

### Options Considered
1. Document the TOML better. Rejected: the values that go stale fastest (model ids) are
   exactly the ones a human cannot check without opening two other config files.
2. Ship a curated model list in afterwit. Rejected: it is wrong the day a model ships, and
   afterwit would then be a third place to update.
3. Read each harness's own config for its models, and make config editable in the UI. Chosen.

### Decision
- New `harness.py` is the single source of truth for harness locations
  (`config_dir`/`settings_path`/`claude_json_path`/`sessions_dir`/`skills_dir`/`agents_path`),
  honouring `CLAUDE_CONFIG_DIR` and `CODEX_HOME`, home-relative on every OS. `install`,
  `doctor` and the adapters all route through it.
- `harness.models()` reads what each harness already knows: Claude Code's configured `model`
  plus its `additionalModelOptionsCache` in `.claude.json`; Codex's `model`, `profiles.*.model`,
  `tui.model_availability_nux` and `notice.model_migrations`. Nothing is rejected for being
  unrecognised — the UI field is free text and these only populate its datalist.
- `config.EDITABLE` describes what a human may change and what a legal value is; the UI renders
  that schema and ships no field list of its own. `config.save()` edits keys in place, keeping
  comments and tables byte-intact, backing up first, and refusing any write it cannot read back.
- `GET/POST /api/settings` (CSRF-gated, 127.0.0.1 only) read and write it, and a save reloads
  the server's `Config` so the running UI and its auto-reviewer see the new values immediately.
- Unset model/effort now means "inherit the harness's own", including the codex fallback;
  `claude -p` gets `--effort` when configured.

### Consequences
- Injection caps are bounded in the schema (≤3 cards, ≤600 tokens), so the UI cannot be used
  to violate Manifesto P3.
- Changing `wiki_root`/`db_path` from the UI does not move data; the UI says so and points at
  `afterwit index --rebuild`.
- afterwit still never writes a harness config outside `afterwit install` — the settings page
  reads them, and says so on the page.
- A stale hardcoded model id is now reachable only when Codex itself has no config at all.

### 49. Config lists that mirror another tool's are stale by construction `config` `harness`
`DEFAULT_CODEX_MODEL` mirrored a model id that Codex itself records in `~/.codex/config.toml`;
the copy drifted and quietly spent the wrong model. Where another tool already stores the
value (model ids, effort levels, config locations), read theirs — a mirrored list has no
mechanism that keeps it true, and it fails silently rather than loudly.

### 50. A default path that ignores the tool's own env override is invisible breakage `install` `cross-platform`
`~/.claude` and `~/.codex` were hardcoded in five places while both harnesses support
`CLAUDE_CONFIG_DIR` / `CODEX_HOME`. Installing into the wrong directory produces no error at
all: the files are written, the installer reports success, and the harness reads none of them.
Route every harness path through `harness.py` (`src/afterwit/harness.py`).

## ADR-035: Provenance names the model that ran, and the distiller signs its cards

**Date**: 2026-07-23 | **Status**: Accepted | **Tags**: `provenance`, `adapters`, `review`, `cards`

### Context
An audit of the live wiki (1,825 cards, 2026-07-23) asked a simple question — *which
model produced this card, on which harness, at what effort?* — and found four holes:

1. **Codex session model: never captured.** 79 cards carry `harness: codex` and **zero**
   carry a model. The adapter read `session_meta.model`, but Codex's `session_meta` has
   no model key at all (only `model_provider`); model and effort live in `turn_context`,
   emitted per turn. Claude cards had models (286 of them) purely because Claude Code
   puts `message.model` where the adapter happened to look.
2. **Effort: never captured, from either harness.** Both record it —
   Claude Code as a top-level `effort` on the record (3,718 occurrences in 8 sessions),
   Codex as `turn_context.effort` — and afterwit read neither.
3. **The distiller: not recorded at all.** Nothing on a card said which model extracted
   it, so "these cards are weak — which model wrote them?" was unanswerable, and the
   separation-of-duties property (ADR-021) was unprovable after the fact.
4. **The reviewer: recorded as a driver name.** `reviewed_by` took
   `cfg.auto_review_model or <driver>`, so with no model configured — the common case —
   223 cards say `reviewed_by: claude-p`. That names a CLI, not a model, and separation
   of duties is a claim about *models*.

### Options Considered
1. Leave it; the source path identifies the session. Rejected: the transcript is
   deletable and unindexed, and it does not survive to a second device.
2. Store a full provenance object per card. Rejected: nothing consumes structure yet,
   and a nested blob in frontmatter is harder to grep than a line you can read.
3. One resolved string per actor, plus the session's own values on each source. Chosen.

### Decision
- `distill.attribution(driver, model, effort) -> "driver:model[:effort]"`, **resolved**:
  an unset model is filled from that harness's own config (ADR-034), never left null or
  substituted with the driver name.
- New optional frontmatter field `distilled_by`, written by `distill_sessions` from the
  `Driver` object's own label. `reviewed_by` now carries the same shape.
- Agent write paths (`afterwit queue`, MCP `save_insight`) stamp `distilled_by: agent`;
  deterministic importers stamp `deterministic-import`. Both are **stamped, never read
  from the caller's payload** — self-reported attribution is unverifiable.
- Adapters capture `effort` (both harnesses) and Codex's model from `turn_context`;
  `_origin` carries `harness`/`model`/`effort` into every card source.
- The review UI shows both: a `distilled_by` chip on the card, and
  `harness · model · effort` on each source line.

### Consequences
- Every card written from now on answers all three questions; frontmatter round-trips
  through `afterwit index --rebuild` unchanged (the field is markdown-only, like
  `reviewed_by`).
- Existing cards are NOT backfilled: the distiller identity was never recorded, and the
  ledger means already-distilled sessions are not re-read. Codex cards distilled before
  today keep `model: null`.
- `reviewed_by` values change shape (`claude-p` → `claude-p:opus[1m]:high`). It is a free
  text audit field; `human` and `deterministic-import` are unchanged.

### 51. A harness can record the same fact in two places and only fill one `adapters` `codex`
Codex's `session_meta` looks like the session-level record, so the model was read from
there — and it is never present. The model and effort live in `turn_context`, per turn.
The adapter "worked" for three months and produced null models for every codex session,
silently, because a missing optional field is not an error. When a field you expect is
absent everywhere, check whether the tool writes it somewhere else before assuming it is
simply unset (`src/afterwit/adapters/codex_jsonl.py`).

### 52. A provenance field that records the tool instead of the model answers nothing `review` `provenance`
`reviewed_by: claude-p` reads like real attribution and passes every test. It names the
CLI binary — while the property it exists to prove (writer ≠ approver) is about models.
An audit field must record the resolved identity of the thing that made the decision, not
the name of the program that hosted it.

## ADR-036: Windows is a supported platform, so POSIX idioms need a declared Windows path

**Date**: 2026-07-26 | **Status**: Accepted | **Tags**: `cross-platform`, `runner`, `cli`, `ci`

### Context
`pyproject.toml` classifies afterwit `Operating System :: OS Independent` and CI runs a
`windows-latest` matrix leg, but that leg has never been green. The 2026-07-23 run is the
first that got past collection, and it exposed two defects that are not test artifacts:

1. **`runner._lock_is_live` probed with `os.kill(pid, 0)`.** On Windows signal 0 *is*
   `CTRL_C_EVENT`, so `os.kill` never reaches a liveness check — it calls
   `GenerateConsoleCtrlEvent` (CPython `Modules/posixmodule.c`, `os_kill_impl`). It cannot
   report a dead pid, so **no stale lock could ever be broken on Windows**: a crashed run
   would block `afterwit run` for the full `STALE_LOCK_HOURS`. It also raises Ctrl+C across
   the console, which is what aborted the pytest session mid-suite and hid every other
   failure behind a bare `KeyboardInterrupt`.
2. **`cli._cmd_init` wrote `config.toml` with an f-string.** `projects_root = "{projects}"`
   with `projects = C:\Users\x\Desktop\Projects` makes `\U` a TOML escape sequence, so
   `tomllib` rejected the file `init` had just written. The very first command a Windows
   user runs produced a config afterwit itself could not read. `config._toml_value` already
   existed for exactly this and `install.py` already used it; only this call site did not.

### Options Considered
1. Drop the `windows-latest` leg and the OS-Independent classifier. Rejected: the claim is
   already published on PyPI and in the README, and both defects are real user-facing bugs,
   not CI noise.
2. Keep `os.kill` and let the `STALE_LOCK_HOURS` TTL be the only backstop on Windows.
   Rejected: it leaves a user whose run crashed staring at "another run is in progress" for
   six hours with no remedy but deleting a file they were never told about.
3. Add `psutil`. Rejected — a new heavy dep for twelve lines, against the stdlib-first rule.
4. A declared Windows branch using `ctypes` + `kernel32`. Chosen.

### Decision
- `runner._pid_is_alive(pid)` is the single liveness probe. On win32 it is
  `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)` + `GetExitCodeProcess` ≟ `STILL_ACTIVE`,
  with `ERROR_ACCESS_DENIED` read as *alive, another user* (matching the POSIX
  `PermissionError` branch) and an out-of-range pid read as *stale* rather than crashing on
  a torn lock file. Argtypes and restype are declared — a `HANDLE` is a pointer and ctypes'
  default `c_int` restype truncates it on 64-bit. Everywhere else, `os.kill(pid, 0)`.
- Any filesystem path written into TOML goes through `config._toml_value`. No exceptions:
  a path is the one value in this system that is guaranteed to contain backslashes.
- Tests get `conftest.fake_home()`, which sets **HOME and USERPROFILE**, because
  `ntpath.expanduser` never consults HOME. Setting only HOME does not redirect `Path.home()`
  on Windows — it leaves the test reading, and anything persistent WRITING, the real profile.
- The CI job takes `timeout-minutes: 15`; the suite runs in about one minute, so anything
  near that ceiling is a hang and should be killed, not left to the 6-hour default.

### Consequences
- Windows is now a platform with an owner, not an aspiration in a classifier. The leg going
  red is a release blocker like any other.
- `_pid_is_alive`'s Windows branch cannot execute on the maintainer's machine, so
  `test_fresh_lock_with_a_dead_pid_is_broken` asserts both directions (`os.getpid()` alive,
  `999_999_999` dead) and CI is the only thing that proves that branch. Do not weaken it.
- Assertions comparing a `Path` to a literal string are separator-dependent by construction;
  compare `Path` objects, or `.as_posix()` when the wire format really is a POSIX relpath.

### 53. `os.kill(pid, 0)` is not a liveness probe on Windows — it is Ctrl+C `cross-platform` `runner`
Signal 0 is `CTRL_C_EVENT` there, so the call routes to `GenerateConsoleCtrlEvent` and
never reports a dead pid. Two consequences, both silent: a stale lock stays "live" forever,
and the console gets a real Ctrl+C — which surfaces as a `KeyboardInterrupt` raised in
whatever the main thread blocks on *next*, potentially many tests later, with no link back
to the call that caused it. Use `runner._pid_is_alive`.

### 54. `monkeypatch.setenv("HOME", ...)` does not move `Path.home()` on Windows `tests` `cross-platform`
`ntpath.expanduser` reads `USERPROFILE`, then `HOMEDRIVE`+`HOMEPATH`, and never `HOME`. A
test that sets only HOME keeps reading the real profile of whoever runs it — so it fails on
Windows and, worse, quietly *writes* there (`config.device_id` persists `~/.afterwit/device_id`).
Use `tests/conftest.py::fake_home`, which sets both.

### 55. `shutil.which` returns the PATHEXT-cased name, not the on-disk one `install` `cross-platform`
On Windows it builds candidates as `cmd + ext for ext in PATHEXT` — and PATHEXT is
uppercase — so a file named `uv.exe` comes back as `...\uv.EXE`. String-comparing that
against `str(path)` fails on a case-insensitive filesystem where the paths are equal.
Compare `Path` objects: `Path.__eq__` normcases on Windows and is exact on POSIX.

### 56. `redact.scrub_home` rewrites a Windows `tmp_path`, because it lives under `C:\Users` `tests` `redact`
`_HOME_WIN` turns `C:\Users\<name>` into `~`, and pytest's tmp_path on Windows is
`C:\Users\runneradmin\AppData\Local\Temp\...` — so any test asserting that a stored source
path round-trips unchanged passes on Linux (where `/tmp` is not under a home) and fails on
Windows. Assert on `scrub_home(str(p))`, not `str(p)`: the sanitizing is the intended
behaviour at the card-write boundary.

### 57. A per-process CSRF token silently breaks every tab you left open `ui` `csrf`
`Handler.csrf_token = secrets.token_urlsafe(32)` is a class attribute, so it is minted
once per SERVER PROCESS. The page fetches it once at boot. Restart `afterwit ui` — to
pick up a config edit, after an upgrade — and every tab still open is holding a token
that died with the old process: every POST 403s, and `saveSettings` caught the error,
toasted it, and never re-fetched, so the tab stayed broken until a manual reload nobody
was told to do. The 403 now carries `code: "csrf"` and the page retries once after
re-fetching. The marker is load-bearing, not cosmetic: `/autoreview` also answers 403
when auto-review is disabled, and retrying THAT would loop forever.

### 58. A content-addressed cache that omits the model can never be invalidated `embed` `retrieval`
`vectors(id, body_hash, vec)` keyed on a hash of the card body alone, and `reindex`
skips any card whose stored hash still matches. `MODEL_NAME` was therefore unobservable
to the cache: changing the embedding model re-embedded **nothing**, and recall scored
new-model query vectors against old-model card vectors. Nothing downstream could catch
it — the obvious candidate upgrade (`all-MiniLM-L6-v2` → `bge-small-en-v1.5`) is also
384-dimensional, so there is no length mismatch to raise. The hash is now salted with
`MODEL_NAME`, which makes a model change self-invalidating with no migration and no new
column. General form: a cache key must cover every input the value depends on, and the
model is an input.

## ADR-037: afterwit's own LLM children are not user sessions, and self-talk must never reach telemetry

**Date**: 2026-07-27 | **Status**: Accepted | **Tags**: `inject`, `distill`, `telemetry`, `install`

### Context
On 2026-07-26 at 22:43 the nightly wrote `~/.afterwit/inject.disabled`: *"hit rate 20%
over 76 servings (threshold 20%)"*. Prompt injection — the push surface the whole hook
path exists for — silenced itself, exactly as Manifesto P9 intends. The evidence it
judged on was not real.

Splitting the 40 injections in that window by their prompt text:

| source | card-servings | used | hit rate |
|---|---|---|---|
| afterwit's own auto-review prompts | 56 | 0 | **0.0%** |
| real human prompts | 20 | 15 | **75.0%** |
| combined — what the kill-switch saw | 76 | 15 | 19.7% |

All 28 self-review rows carry the identical prompt (`"You are reviewing ONE knowledge
card extracted from a developer's AI coding session…"`) across 14 sessions inside sixty
seconds, each pulling the same two cards.

The mechanism: `auto_review = true` with `distill_driver = "codex"` resolves the reviewer
to `claude-p` (ADR-021 separation of duties). `distill.claude_p` called `subprocess.run`
with no `env=`, so the child inherited the user's `~/.claude/settings.json` — including
afterwit's own `UserPromptSubmit` hook. **afterwit injected into afterwit.** A reviewer
then emits a JSON verdict about a different card and never echoes the injected one, so
`card_was_used` scored all 56 `ignored`.

Three consequences, all of which had to be fixed together:

1. The kill-switch fired on a sample that was 74% self-talk, and it fired **by one card
   hit**: 15/76 = 19.74%, where 16/76 = 21.1% survives.
2. `IGNORED_DELTA = -0.2` × 56 drove the two repeatedly-served cards to `usefulness:
   -5.6` — one of them `harness-helper-design`, the project's own design card. Because
   `usefulness` is a ranking input *and* is written back to card frontmatter, afterwit
   had durably demoted its most relevant card by talking to itself.
3. Deduplicating the residue of ADR-032's double-hook bug leaves 11 genuine
   card-servings, below `KILLSWITCH_MIN_SERVINGS = 20`. There was never enough real
   evidence to run the gate at all.

Separately, and found while fixing the above: the machine still carried a `SessionStart`
hook invoking the pre-rename `hh` binary, which no reinstall could remove (Gotcha #60).

### Options Considered
- **Filter self-review prompts by their text in `mine_servings`.** Rejected: a band-aid
  on the scoring stage that leaves the child still burning tokens on injected context it
  cannot use, still polluting `servings`, still decaying cards. It also breaks the moment
  the reviewer prompt is reworded.
- **Disable the hook while the nightly runs.** Rejected: mutates the user's settings from
  a background job, and races any interactive session running at the same time.
- **Have `inject` detect `claude -p` (non-tty, `-p` in the parent argv).** Rejected:
  infers afterwit's identity from a coincidence of process shape, and would also suppress
  a *user's* own legitimate `claude -p`.
- **Mark the child explicitly, and have inject honour the mark.** Chosen.

### Decision
`distill._child_env()` returns `{**os.environ, "AFTERWIT_INTERNAL": "1"}` and is passed
at both driver spawn sites; `inject.run` returns empty when it sees the marker, checked
before any other work so the cost on the p95<200ms path is one dict lookup. Merged onto
`os.environ`, never replacing it — a bare `env={...}` strips PATH and the driver does not
spawn at all, which is the failure mode the test asserts against.

Set for the `codex` driver too, even though ADR-003 gives Codex no hook today: a guard
that depends on another ADR not changing is not a guard.

The historical 28 rows are marked `outcome='internal'` — neither `used` nor `ignored`, so
`killswitch_status` skips them, and not NULL, so `mine_servings` will not re-score them.
The 56 bogus `-0.2` charges were reversed and written back to the wiki with
`write_back_usage`, so the repair survives `afterwit index --rebuild` (verified: 1,776
cards rebuilt from the wiki, both cards read 0.0). The kill-switch flag was then deleted.

The gate's own report is now `.1%` and says "card-servings" rather than "servings"
(Gotcha #61), and `_is_afterwit_inject` recognises the legacy `hh inject` spelling
(Gotcha #60).

### Consequences
- Kill-switch on the same window, after repair: **served 20, used 15, hit rate 75%,
  disable False**. Injection is re-enabled and verified end-to-end.
- afterwit's own LLM children now run with no injected context, which is also a small
  token saving on every distill and review call.
- `AFTERWIT_INTERNAL` becomes load-bearing public API between two modules that never
  otherwise touch. Any future hook-invoked surface — the `PostToolUse` error-lookup hook
  under consideration — must honour it, or it re-opens this exact hole.
- The deeper hazard is unchanged and deliberately not addressed here: `card_was_used`
  requires ≥60% of a card's title tokens to reappear verbatim downstream, so a card that
  *prevented* a mistake scores as a miss and is then charged `-0.2` for it. On clean data
  that is now the most likely way this gate misfires next. Revisiting it requires a
  measured precision claim on real transcripts (SPEC §12), not a guess.
- General form, and the reason this is an ADR rather than three bug fixes: **a system
  that measures itself must be able to tell its own traffic from its users'.** afterwit
  reads transcripts, writes cards, spawns agents and hooks itself into the harness — the
  loop was always available, and the only thing that made it visible was a gate tripping.

### 59. afterwit injected into its own reviewer, and the kill-switch believed it `inject` `distill` `telemetry`
`auto_review = true` runs the reviewer on the opposite driver (ADR-021), and
`distill.claude_p` spawned `claude -p` with no `env=`. A `claude -p` child inherits the
user's `~/.claude/settings.json`, hooks included — so afterwit's own UserPromptSubmit
hook fired on afterwit's own reviewer prompts and injected cards into them. A reviewer
emits a verdict about a *different* card and never echoes the injected one, so
`card_was_used` scored every one of them `ignored`. On 2026-07-26 that was **56 of the
76 card-servings** in the kill-switch window: measured 19.7%, actual 75% on human
prompts, and injection auto-disabled itself. The same 56 misses also charged the two
cards the reviewer kept matching `-0.2` apiece to `usefulness: -5.6` — one of them the
project's own design card, demoted to the bottom of the ranking by afterwit talking to
itself. Children now carry `AFTERWIT_INTERNAL=1` (`distill._child_env`) and `inject.run`
returns empty when it sees it, before any other work.

### 60. A renamed CLI leaves a hook no reinstall can ever remove `install` `hooks`
`_set_hook` identifies afterwit's hooks by `"afterwit" in command` (ADR-032). The
pre-rename hook reads `uv run --project .../harness_helper hh inject --mode session` —
no "afterwit" anywhere in it — so the dedupe never saw it as ours, every reinstall
appended the real hook *beside* it, and `hh` had been deleted at the rename. Result: a
`SessionStart` hook that fails to spawn on every single session, silently, forever.
Install already dropped the legacy **MCP** entry by name for exactly this reason
(`_LEGACY_MCP_NAME`); the hook path was the sibling nobody swept for. Found live two
weeks after the rename. `_is_afterwit_inject` now also matches `" hh inject"` — with the
leading space, so a project path containing those letters cannot eat a neighbour's hook.

### 61. A gate that rounds its own verdict cannot be audited `telemetry` `observability`
The kill-switch wrote `hit rate {rate:.0%}`. The real 2026-07-26 trip was 15/76 =
19.74%, which rendered as `hit rate 20% over 76 servings (threshold 20%)` — a message
that reads as a gate firing when it was *level* with its threshold, i.e. as a bug,
rather than a genuine miss by one card hit. `served` also counts card outcomes, not
prompts, so the "76" could not be reconciled against the 40 rows in `servings` either.
Now `.1%` and "card-servings". The lesson generalises past this gate: any automated
verdict that silences a subsystem has to print a number precise enough to be checked
against the table it came from.

## ADR-038: The failure hook is `PostToolUseFailure`, and its output needs an envelope

**Date**: 2026-07-27 | **Status**: Accepted | **Tags**: `inject`, `hooks`, `claude-code`, `retrieval`

### Context
ADR-037 restored prompt injection. The complementary surface — look up a recorded fix
at the moment a command actually fails — was proposed as "a `PostToolUse` hook on
`Bash`, fire when the exit code is non-zero, feed the stderr tail to `lookup_error`".

Every load-bearing assumption in that sentence is wrong, verified against the shipped
CLI (2.1.220) and 285 real failed-Bash records in the local transcripts:

1. **`PostToolUse` does not fire on failure.** The tool call is wrapped in try/catch;
   the success path dispatches `PostToolUse`, the catch path dispatches **only**
   `PostToolUseFailure`. They are mutually exclusive. A non-zero Bash exit throws, so
   a hook registered on `PostToolUse` would never once have fired — the feature would
   have shipped, installed cleanly, and done nothing, which is the exact silence it
   exists to break.
2. **There is no exit code and no stderr field.** `PostToolUseFailure` carries one flat
   `error` string, built as `["Exit code N", <interrupt>, stderr, stdout]` joined by
   newlines. Stderr leads. `tool_response` — the key that does hold streams — exists
   only on the success event.
3. **Plain stdout does not reach the model.** The renderer returns nothing unless the
   event is SessionStart / UserPromptSubmit / UserPromptExpansion. Context must go
   through `hookSpecificOutput.additionalContext`, and `hookEventName` must equal the
   firing event exactly or the CLI discards the output.
4. `error` is not only failures. It also carries permission denials, classifier
   refusals and user aborts (`is_interrupt`).

Also worth recording because it actively misleads: the plugin-dev *hook-development*
skill shipped with the CLI documents `tool_result` (the key is `tool_response`) and
claims settings need no `hooks` wrapper (they do). Do not code against that file.

### Options Considered
- **`PostToolUse` + inspect the result for failure.** Impossible: the event never fires.
- **`PreToolUse` and predict failure.** Absurd — nothing has failed yet.
- **Parse the transcript for `is_error` records out of band.** A polling design with no
  trigger, and it re-reads raw transcripts on a latency path (violates P1).
- **`PostToolUseFailure` + `additionalContext`.** Chosen — the only shape that works.

### Decision
`afterwit inject --mode error`, registered on `PostToolUseFailure` with `matcher: "Bash"`
(matched against `tool_name`, case-sensitive). It gates on `error.startswith("Exit code ")`
and skips `is_interrupt`, so denials and aborts cost nothing. The detail below the exit
line — stderr first — is the query, capped at 2000 chars.

Search, ranking, the P3 caps and serving-log now live in one shared `_serve()` used by
both prompt and error mode: **a hard cap that exists in two copies is a cap that will
disagree with itself the first time one is edited.** Error mode adds only an `error_fix`
sort bias (SPEC §9.1, same as the `lookup_error` pull tool) and its own header.

Servings log under `mode='error'`. `killswitch_status` only ever counts `mode='inject'`,
so the two surfaces are measured separately by construction — but the kill-switch *flag*
silences both: when the gate has decided push cannot prove its value, silence means
silence on every push surface.

### Consequences
- Fires only on red, so the common case costs nothing — it is not on the prompt path.
- Honours `AFTERWIT_INTERNAL` for free, since ADR-037's guard sits at the top of `run()`
  ahead of every mode. Any future hook surface inherits it the same way; one that reaches
  the index by another route would re-open ADR-037.
- **The `error_fix` sort bias is conditional, not decorative, and rarely observable.** At
  the default `floor = 0.35` a specific error signature usually leaves exactly one card
  standing, because min-max normalisation over a small candidate pool drives everything
  but the best match toward zero. The test therefore drops the floor to 0 and asserts a
  control — unbiased ranking must put the *decision* first — or it would pass identically
  with the sort deleted.
- Not execution-verified end to end: hooks are snapshotted at session start, so the CLI
  side (does the event fire, does the envelope render) cannot be observed without a
  restart. afterwit's side is verified — the real command, fed a real payload, returns
  the right card in the right envelope and stays silent on interrupts and denials.
- General form, and the reason this is an ADR: **an integration contract is not a thing
  to remember, it is a thing to read.** Three of the four assumptions here were plausible,
  conventional, and wrong, and every one of them fails silently rather than loudly.

### 62. `PostToolUse` never fires on a failed tool call `hooks` `claude-code`
The CLI wraps tool dispatch in try/catch: success dispatches `PostToolUse`, the catch
path dispatches **only** `PostToolUseFailure`. A non-zero Bash exit throws. So a hook
registered on `PostToolUse` to catch failures installs cleanly, validates, reports no
error, and never fires — the worst possible failure mode for a feature whose entire
purpose is to speak up when something breaks. The failure event also carries no exit
code and no stderr field: one flat `error` string, `["Exit code N", <interrupt>, stderr,
stdout]` newline-joined, and it also carries permission denials and user aborts. Gate on
`error.startswith("Exit code ")` and skip `is_interrupt`. (The plugin-dev
hook-development skill shipped with the CLI documents `tool_result` for the success
event; the real key is `tool_response`.)

### 63. Plain stdout from a hook is discarded on every event but three `hooks` `claude-code`
`print(text); exit(0)` reaches the model only from SessionStart, UserPromptSubmit and
UserPromptExpansion. Everywhere else the success renderer returns nothing and the text
is silently dropped — no warning, exit 0, hook "succeeded". Context must be emitted as
`{"hookSpecificOutput": {"hookEventName": "<the firing event>", "additionalContext":
"..."}}`, and the event name must match exactly: the validator throws
`Hook returned incorrect event name: expected 'X' but got 'Y'` and discards the output.
Stdout must also start with `{` or it is treated as plain text.

### 64. Claude Code runs hook commands through bash — on Windows too `hooks` `install` `windows`
A hook is stored as a shell string, and the shell is bash (git bash) on every platform.
`subprocess.list2cmdline` quotes for cmd.exe: it leaves `C:\Users\...\uv.EXE` bare, bash
reads each `\` as an escape, and the hook dies with
`C:UsersE112323scoopshimsuv.EXE: command not found` on every prompt. `shlex.join` on all
platforms is correct — inside bash single quotes a backslash is literal, so the Windows
path survives. MCP registration is unaffected: it stores `command` + `args` and never
touches a shell.

### 65. "The entry is registered" and "the command runs" are different claims `doctor` `install`
doctor checked hooks by substring-matching the settings JSON, so it printed `ok claude
prompt hook` for a command bash could not spawn — for as long as the hook had been dead.
Config-reading checks cannot catch a quoting bug, by construction. A check that spawns
the *stored string* the way the harness spawns it is the only kind that can. Note that
`inject` fails open (always exit 0), so a nonzero exit means the shell never reached it.

### 66. On managed Windows, `~/Desktop` does not exist `install` `windows` `paths`
OneDrive redirects Desktop to `~/OneDrive - <Org>/Desktop`, so a candidate scan for
`~/Desktop/Projects` finds nothing and `init` bails. Worse, it bailed *before* reading
the config, telling the user to set `projects_root` and then ignoring it on the re-run —
an error message that is a dead end. An existing config must win over any discovery scan.

### 67. Corporate TLS interception breaks the one-time model download `env` `index`
A MITM proxy makes `fastembed`'s HuggingFace fetch die with `CERTIFICATE_VERIFY_FAILED`
(and `uv` needs `--system-certs` to install at all). Nothing in afterwit is wrong and no
retry helps: Python's bundled certifi store does not contain the corporate root. Export
the Windows root store to a PEM and set `SSL_CERT_FILE`/`REQUESTS_CA_BUNDLE`. Symptom to
recognise: `index` completes, FTS is fine, and `doctor` reports embedding coverage 0/N —
semantic search silently degraded to keyword-only.

### 68. A command prefix in a shell variable cannot be quoted into working `install` `skills`
`AW="<path> run --project <path> afterwit"` then `$AW recall ...` breaks the moment
either path holds a space or a paren, and quoting the value does not save it: bash
word-splits the expansion but does not re-parse quotes, so `'C:\Program Files\uv.EXE'`
is looked up with the quote marks as part of the filename. The forms that do work are
`eval`, an array, or — least invasive for docs an agent copies — a function:
`aw() { <quoted argv> "$@"; }; AW=aw` keeps every `$AW ...` call site verbatim.

## ADR-039: A project slug is not its folder name

**Date**: 2026-07-27 | **Status**: Accepted | **Tags**: `config`, `retrieval`, `identity`, `adapters`

### Context
The tool was renamed `hh` → `afterwit` two weeks ago, but the project's identity in the
knowledge base was still `harness_helper`: 121 cards, the wiki directory, the `projects`
row, and 11 review-queue entries. The request was to finish the rename *without* moving
the working directory.

That is not expressible today. `project_from_cwd` is three lines and ends
`return rel.parts[0]` — **a slug IS a folder name**. Measured on the live index before
touching anything:

| | slug == folder (today) | slug renamed, folder unchanged |
|---|---|---|
| session-line cards (hard `WHERE project=?`) | **112** | **0** |
| error-hook result for a real signature | `error_fix 1.041`, `gotcha 0.413` | `error_fix 0.891` only |

The session line disappears outright. Ranking silently loses its `+0.15` same-project
boost, which is what carries the second card over `floor = 0.35` — so the hook I had
just verified would have answered with less and reported nothing wrong.

Worse, it does not even stay renamed: the adapters call `project_from_cwd` for every
event, so the next nightly stamps `harness_helper` on every newly distilled card and the
rename undoes itself with both slugs then live at once.

### Options Considered
- **Rename the directory.** The one-line fix, and explicitly out of scope. It is also the
  move that caused Gotcha #27 — renaming a path out from under a running MCP server.
- **Symlink `Projects/afterwit` → `Projects/harness_helper`.** Dead on arrival:
  `project_from_cwd` calls `.resolve()`, so the real path comes back either way.
- **Leave the slug.** Honest, but it is not the rename that was asked for, and it leaves
  `eval/golden.yaml` — which already declares `project: afterwit` on three hit queries —
  scoring against a project that does not exist.
- **Make the slug a config fact, independent of the folder.** Chosen.

### Decision
`[project_aliases]` in `config.toml` maps **folder name → slug**. `project_from_cwd`
gains an optional `aliases` argument, and a new `project_dir_name` inverts it for
everything that touches the working tree.

Both directions are needed, and missing either is silent:
- **folder → slug** on every path that *produces* or *queries* a slug: both inject modes,
  `docs_md`, and — critically — the claude and codex adapters, including the cwd-less
  fallback that recovers a slug from Claude Code's flattened transcript directory. Alias
  one adapter path and not the other and a single session mints cards under two projects.
- **slug → folder** on every path that *resolves* a slug to disk: `gitmeta.anchor` (the
  one place a card is stamped with its commit, so all three of its callers are fixed at
  once), and `consolidate`'s `_resolve_project`, `mark_stale` and `backfill_anchors` —
  two separate `projects_root / slug` constructions, not one.

Default `None` everywhere: a project whose folder is named what it is called needs no
entry and behaves exactly as before.

### Consequences
- Migration verified against a baseline captured first: session line 112 → **112**, cards
  in project 121 → **121**, total 1775 → **1775**, error-hook scores 1.041/0.413 →
  **1.041/0.412** (rebuild float noise), old slug **0** in cards, review queue and the
  `projects` table. All three hooks re-run live; `afterwit eval` PASS.
- `rebuild()` clears cards/fts/links but **not** `projects` or `review_queue` — both
  carry the slug independently and had to be migrated by hand. A wiki-only rename would
  have left the review queue minting cards under the dead slug.
- The alias survives a Settings-tab save. That is now a test: `save()` rejects unknown
  *keys*, and had it treated an unknown *table* the same way, one click would have
  un-renamed the project — a failure indistinguishable from a project with no history.
- This also fixes the recorded gotcha that project resolution cannot see differently
  named clones of the same repo: the folder is no longer the identity.
- Not addressed: the wiki still contains `book_reading`/`book-reading` and
  `ces_appscript`/`ces-appscript` as separate slugs. Same class of defect, different
  cause (separator drift, not renaming), and merging them is a data decision.
- General form: **identity that is derived from a filesystem path is not identity, it is
  a coincidence that has held so far.** It holds until someone renames a directory, or
  clones to a different name, and then it fails silently in the direction of "no history",
  which is the one failure this system cannot distinguish from working correctly.

### 69. Codex discovers a hook, then silently skips it unless it is trusted `hooks` `codex` `install`
codex-cli 0.145.0 gates hooks behind a persisted trust hash. An untrusted hook is
discovered, reported by `hooks/list`, and then **not run** — no warning, no diagnostic,
exit 0. Verified: a full `codex exec` turn with three registered hooks produced no hook
output and no payload file. Trust lives in the same config layer as the hook:
```toml
[hooks.state."<sourcePath>:<event_snake>:<groupIdx>:<hookIdx>"]
enabled = true
trusted_hash = "sha256:…"
```
Neither the key nor the hash can be derived by hand — read them from `codex app-server`
JSON-RPC `hooks/list`. The hash covers the whole handler config, not just the command
string: deleting one `matcher = "shell"` line flipped `trustStatus` from `trusted` to
`modified` and the hook went silent again. Any installer that edits a Codex hook must
re-read `hooks/list` and rewrite `trusted_hash` in the same run, or it silently disables
what it just installed. `codex --dangerously-bypass-hook-trust` skips the gate for one
invocation.

### 70. Codex's `PostToolUse` cannot tell a failed command from a successful one `hooks` `codex`
There is no `PostToolUseFailure` on Codex. Its `PostToolUse` fires on every tool call and
the two payloads are structurally identical — `tool_response` is a bare string with no
exit code, no status, and no stderr/stdout split (`"ls: cannot access …"` for the failure,
`"hello-zorblax\n"` for the success). `tool_name` is `"Bash"`, same as Claude, so matchers
port but the gate does not. Claude's error hook therefore has nothing to gate on here, and
an ungated port would fire on every shell call the model makes.

### 71. argparse can exit(2) before a fail-open hook ever runs `hooks` `cli`
`inject.py`'s contract is FAIL OPEN — empty stdout, exit 0, never block a prompt — and
it was reached through `cli.main`'s argparse, which enforces its own contract first:
unknown flag → usage on stderr → **exit 2**. Adding `--harness codex` to the installed
Codex command without adding it to the subparser produced
`afterwit: error: unrecognized arguments: --harness codex` and exit 2, live, on every
prompt. Symptom on Codex is the single word `Blocked` in the hook line; on Claude Code
exit 2 from `UserPromptSubmit` blocks *and erases* the prompt (Gotcha #5). `afterwit
inject` now bypasses argparse entirely and parses its own flags, so no future flag can
reintroduce this. A fail-open contract is only as good as the strictest layer above it.

### 72. A managed fence overwrites whatever you hand-edited inside it `install` `docs`
`~/.codex/AGENTS.md` is a symlink to `~/.claude/CLAUDE.md`, and `install_codex` rewrites
the fenced region from `prompts/skills/codex-aw.md`. A `<query_triggers>` block that had
been strengthened *in the live file only* was silently reverted to the repo's older text
by the next install — no warning, and the loss is invisible unless you diff against the
backup. The fence is managed content: an edit that is meant to last belongs in the repo
template, not in the installed copy. (Recovered from the timestamped backup install had
just taken, which is why that backup is non-negotiable.)


### 73. A running MCP server keeps stale modules forever, and blames the file `mcp` `imports`
afterwit edits its own source while its MCP servers are running. A server started before
a function existed holds that old module object in `sys.modules` for the life of the
process, so a lazy `from .config import project_dir_name` inside `gitmeta.anchor()` fails
with **`ImportError: cannot import name 'project_dir_name' from 'afterwit.config'`** —
and the message names the current file, which *does* define it. Reproduced exactly with
`del config_mod.project_dir_name; gitmeta.anchor(...)`.

It took out `save_insight` alone, because that is the only tool that anchors; `recall`
and friends kept working, so the symptom reads as "one tool is broken" rather than "this
process is stale". `afterwit queue` and `distill` call the same helper and cannot hit it
— they are short-lived. The fix is to restart the harness; the code change is that
save_insight now queues the card unanchored and says so rather than losing the insight.

## ADR-040: Codex gets the same push surfaces as Claude, because it grew hooks

**Date**: 2026-07-27 | **Status**: Accepted | **Tags**: `hooks`, `codex`, `injection`, `telemetry`
**Supersedes**: the Codex half of ADR-003

### Context
ADR-003 gave Codex a static AGENTS.md block and MCP pull, and no hook: *"its per-prompt
push semantics are undocumented, so we never make it load-bearing."* That was true of the
Codex of 2026-07-05. codex-cli 0.145.0 ships `hooks` as a **stable, on-by-default**
feature (`codex features list` → `hooks stable true`) with eleven events, and a decision
card's constraint disappearing is the signal to re-open it, not to keep obeying it.

Verified end-to-end against an isolated `CODEX_HOME`, real `codex exec`, gpt-5.6-sol —
not read from documentation:
- Hook payload keys are **identical to Claude Code's**: `session_id`, `cwd`, `prompt`,
  `hook_event_name`, `transcript_path`, `model`, `permission_mode`, plus a Codex-only
  `turn_id`. `inject.py` needed zero payload adaptation.
- SessionStart and UserPromptSubmit both deliver context to the model.
- Bare hook stdout is rendered as context, exactly as Claude does it — the JSON envelope
  is accepted but not required, so `main()` keeps one print for both harnesses. This was
  measured *after* a comment claiming the opposite had already been written.
- `PostToolUse` cannot distinguish red from green (Gotcha #70), so the error hook does
  not port.
- Hooks do not run until trusted (Gotcha #69).

### Options Considered
1. **Leave Codex on pull-only.** Zero work, and wrong: Codex sessions are roughly half
   this machine's traffic and get none of the recall Claude gets.
2. **Port all three hooks.** Rejected on evidence — the error hook would fire on every
   successful shell call.
3. **Port the two push surfaces, keep `lookup_error` as an MCP pull tool for Codex.** Taken.

### Decision
- `afterwit install codex` writes `[[hooks.SessionStart]]` and `[[hooks.UserPromptSubmit]]`
  into the fenced region of `~/.codex/config.toml`, then reads `hooks/list` and writes the
  matching `[hooks.state.…]` trust entries. Trust is refreshed on every install, because
  the hash changes whenever the command does.
- Trust is written **only** for hooks whose `sourcePath` is the file we manage *and* whose
  command `_is_afterwit_inject` recognises as ours. Auto-trusting on either criterion alone
  would let this installer bless a stranger's hook.
- The hook command carries `--harness codex`. Passed explicitly rather than sniffed: both
  harnesses send the same payload keys and neither sets a distinguishing env var.
- `log_serving` records that harness instead of the hardcoded `"claude"`, and
  `session_text_lookup` takes the harness so Codex servings resolve against
  `~/.codex/sessions/**/rollout-<ts>-<uuid>.jsonl` rather than `~/.claude/projects`.

### Consequences
- Codex servings are now measurable. Left hardcoded, every one of them would have resolved
  to a missing Claude transcript, been mined as `skipped`, and stayed `outcome IS NULL`
  forever — never judged, never counted by the kill-switch, and re-scanned every night.
- The kill-switch sample is now both harnesses. That matches the switch, which is global:
  `inject.disabled` silences every push surface, so it should be judged on every push
  surface.
- Install writes `~/.codex/config.toml` twice on a real change, because `hooks/list` can
  only read a file that already exists. Existing trust entries are carried through the
  first write, so an unchanged install is still a no-op with no second backup.
- The self-injection guard needed no work: `distill._child_env` already stamps
  `AFTERWIT_INTERNAL=1` on the `codex` child too, and `inject.run` checks it first.
- Not done: Codex's `PermissionRequest`, `PreCompact`/`PostCompact` and `SubagentStart`
  are unexplored. `PreCompact` in particular looks like the right place to re-inject what
  a compaction is about to drop.


### 74. A `which()` guard before an injected seam makes tests pass by accident `testing` `ci`
`_codex_hooks_list(config_path, cwd, run=None)` takes `run` as its transport, but checked
`shutil.which("codex")` first and returned None when the binary was missing — *before*
consulting `run`. Every test that injected a stub therefore exercised nothing at all on a
machine without codex installed. Locally all green, because the author's machine has it;
on CI, two hard failures (`assert 0 == 2`, no trust entries written) and three more tests
that "passed" only because the function bailed before reaching their stub.

Reproduce the CI environment in one line instead of guessing from a log:
```
env PATH="/tmp/empty:/usr/bin:/bin" uv run pytest tests/test_install.py -q
```
Fix: `if run is None and exe is None: return None`. Rule: **when a seam is injected, the
injection must come before any environment probe the seam is supposed to replace** — and
a test whose outcome depends on an unrelated binary being installed is not a test.

Windows exposed a second, separate one in the same test: the trust key embeds the absolute
config path, emitted through `json.dumps` (a TOML basic string), so `C:\Users\...` lands
escaped as `C:\\Users\\...` and an assertion string-matching the raw path never matches.
Assert by *parsing* the result — `tomllib.loads(text)["hooks"]["state"]` — which is
platform-independent and additionally proves the file is TOML the harness can read. The
companion unit test builds a `PureWindowsPath` key and round-trips it, so the Windows-only
case is covered on every runner.

### 75. `text=True` is not "text", it is the locale's encoding `subprocess` `windows` `distill`
`subprocess.run(..., input=prompt, text=True)` encodes stdin with
`locale.getpreferredencoding()` — cp1252 on Windows. `prompts/distill.md` carries `≤` at
position 90, so **every** distillation on Windows died with `'charmap' codec can't encode
character '\u2264'`, each session logged `distill-skip`, and the run ended
`RuntimeError: distillation attempted but every session failed (30 skipped)`. The nightly
had never distilled one session on that machine; its only trace was `Last Result: 1` in
Task Scheduler, because `doctor` checks reachability and reported all good throughout.
The decode direction is the same bug quieter: model output with an em dash comes back
mangled rather than raising. Always pass `encoding="utf-8", errors="replace"` — the
default is a property of the machine, not of the data, and a Linux CI box cannot see it.
Whole class, if it recurs: `PYTHONUTF8=1`.

### 76. A fidelity check that reads its input wrongly still passes `doctor` `windows`
doctor spawns the *stored* Codex hook string to prove it runs — the fix for Gotcha #65 —
but read it with `line.split("= ",1)[1].strip('"')`. install writes that value with
`json.dumps`, so a Windows path arrives escaped and stripping the quotes leaves
`C:\\Users\\...` with the backslashes doubled: it spawned a string that was not the one
on disk. It passed anyway, because Windows collapses duplicate separators — which is
exactly what makes it dangerous, a check that is wrong and green simultaneously. TOML
basic strings share JSON's escape rules, so `json.loads` round-trips; `.strip('"')` never
does. Gotcha #74 had already learned this for the tests; the runtime path kept the bug.

### 77. A configured model name is not the model that ran `distill` `provenance`
`model = "opus"` in settings.json is an ALIAS the Claude CLI resolves on its side, so
stamping the configured string wrote `distilled_by: claude-p:opus` — true today,
ambiguous the day opus-6 ships, and unable to answer the one question `distilled_by`
exists for (ADR-035). It also weakens ADR-021: separation of duties compares these
strings, and two different concrete models can both be `opus`. Resolve, per call:
Claude reports it in `--output-format json` under `modelUsage`; Codex reports it
nowhere in `--json` (usage only), so read `turn_context.model` from the rollout its own
run wrote, keyed by the `thread.started` id. Observed, not configured — `-m` states an
intent, the transcript states what answered. Both paths degrade to the configured name
rather than failing: never break a run over metadata (Gotcha #75).

### 78. A partly-read session is indistinguishable from a fully-read one `distill` `provenance`
A session over `MAX_TRANSCRIPT_CHARS` keeps its head and tail and drops the middle, then
leaves exactly what a complete session leaves: one ledger row and some cards. A card
resting on 53% of the evidence looked identical to one resting on all of it, and the
ledger marked the session distilled so it never came back. Truncation is a reasonable
policy; silent truncation is not. Now measured (`Coverage`) and reported in three
places: the run summary flags it, the synced device log names the session and the
percentage, and the ledger stores `coverage` (NULL on old rows means unknown, never
"full"). Watch the second cap too — a session far under the whole-session limit can
still lose 44% to per-turn clipping.

### 79. A ranking test cannot see whether the caller still passes the flag `testing` `rank`
`rank()` grew a `cross_project` parameter and two callers that pass `1.0` to opt out.
Two tests covered the parameter, both green, both worthless for the thing that actually
breaks: changing `inject._serve` and `mcp_server._lookup_error` to stop opting out left
**all 356 tests passing**. The tests proved the knob turns, never that anyone turns it.
The kill is a test at the CALLER, and the sharpest form is a paired assertion — the same
text through both modes, asserting opposite outcomes — because it fails if either the
flag or the branch that selects it is wrong. Whenever a fix is "pass X at the call site",
the guard belongs at the call site; mutate the call, not the function, to prove it.

### 80. `git checkout --` is not an undo for a mutation test `testing` `git`
A mutation harness that restores with `git checkout -- <file>` restores the file to
**HEAD**, not to its pre-mutation state — so run against uncommitted work it silently
deletes the very change under test. Four source edits vanished mid-run; the follow-on
mutations then reported "anchor missing" and one reported six unrelated failures, all of
which read as findings about the product. Restore from a copy taken immediately before
the edit (`cp file /tmp/bak` … `cp /tmp/bak file`), or commit first. The tell is a
mutation result that gets *less* coherent as the run proceeds: suspect the harness before
the system under test.

### 81. A signal that cannot fail is not a signal `measurement` `consolidate`
`consolidate.card_was_used` scored a served card USED when ≥60% of its distinctive title
tokens appeared later in the session. Run against a **random unrelated** session's text it
said USED just as readily: 95.7% own-session against 54.0% held-out, a lift of 1.8x. It
fed both the `usefulness` rank term and the kill switch, so the gate that exists to
disable injection when it stops earning its slot was structurally unable to fire, and
every hit-rate figure derived from it (including ADR-042's 74%/43% split) was noise. The
bias was also *inverted*: a vague title (`Project completion status`) auto-passed while a
precise one (`Never judge Playwright e2e on a loaded box`) was judged honestly. The test
that catches this is not "does it fire" but "does it stay silent when it must" — run the
same rule against text it should have nothing to do with, and require separation.

### 82. `_WORD` admits `.`, so `writes.` is a token that matches nothing `tokenizing`
`_WORD = [A-Za-z0-9_.]{3,}` has to accept dots — `127.0.0.1` and `pdf.ts` are exactly the
tokens worth matching on — but that also makes every sentence-final word come out with its
period attached, and `"writes." in text` is false for text containing `writes`. Card bodies
end sentences constantly, so a share of every card's evidence was silently dead. Found by a
mutation that *survived* for the wrong reason: the test meant to pin `MIN_NOVEL_TOKENS` was
passing on the period instead. `consolidate._norm` strips edge dots and keeps interior ones.
Retuning after the fix moved the chosen rule's measured lift from 12.6x to 7.5x — the bug
had been flattering the result, so the pre-fix numbers were wrong in the reassuring direction.

### 83. Skip-and-log must dedupe, or it buries the output it sits next to `adapters`
`claude_jsonl` warned once per **occurrence** of an unknown record type. One transcript
carrying `file-history-delta` on thousands of lines emitted 585 KB of identical warnings
and buried the stdout of the analysis that triggered it. The cause is one unhandled type
per file, not one per line — `adapters.warn_once` keys on `(path, type)`. The root fix was
separate: `file-history-delta` is the sibling of the already-dropped
`file-history-snapshot` (the CLI's per-edit undo journal) and belongs in `_DROP_TYPES`.

### 84. `pgrep -f <pattern>` matches the harness's own wrapper shell `tooling`
`pkill -f probe_rules` killed four unrelated background tasks and, later,
`kill $(pgrep -f "python3 /tmp/minerlift2.py")` killed the very shell that was about to
launch the replacement — because the agent harness's wrapper `bash -c` command line
*contains* the string being matched. Two probe runs were lost this way. Match on something
the wrapper cannot contain, or take the PID from the launch itself.

### 85. An unbounded dep breaks only where nobody is looking `deps` `packaging`
`mcp>=1.28.1` with no ceiling and NO tracked uv.lock (gitignored in both repos): every
developer venv sat on a long-ago-resolved 1.28.x and stayed green, while any fresh clone
free-resolves to whatever PyPI serves — which became mcp 2.0.0, removing the
`Server.list_tools`/`call_tool` decorator API, the day it shipped. Result: main checkout
383/383 green, fresh install broken on arrival (`AttributeError: 'Server' object has no
attribute 'list_tools'`). Found only because the publish flow re-ran the suite in a fresh
worktree venv. Fix: `mcp>=1.28.1,<2` in pyproject AND a committed uv.lock in the public
tree; lift the ceiling only together with an mcp-2 port of `mcp_server.py`. The general
rule: a fresh-resolve test run is part of publishing — the dev venv proves nothing about
a clone.

## ADR-041: A resolved insight outranks its own provenance

**Date**: 2026-07-27 | **Status**: Accepted | **Tags**: `mcp`, `resilience`, `provenance`

### Context
ADR-020 D5 made `gitmeta.anchor` the one place a card gets stamped, because agent-proposed
cards had been reaching the wiki unanchored. That made anchoring a hard precondition of
`save_insight`. Then a long-running MCP server hit Gotcha #73 and `save_insight` began
raising `ImportError` on every call — for the life of that process, with `recall` still
working. The reported symptom was "afterwit save_insight is down", and the insight the
agent was trying to record was simply lost.

### Decision
`save_insight` catches `ImportError`/`AttributeError` from the anchor step, queues the
card without an anchor, and appends the real diagnosis to its reply: the process is
running code older than the files on disk, restart the harness. Any other exception still
propagates — swallowing everything is exactly how ADR-020 D5 came to be written.

### Consequences
- A card that reaches a *human review queue* unanchored is a recoverable state; a card
  that was never captured is not. The reviewer sees provenance and can attach sources.
- The scope is deliberately narrow. `ImportError`/`AttributeError` from that one call is
  the stale-process signature; a genuine anchoring bug still fails loudly.
- The general form: **metadata collection must never be a precondition for capturing the
  thing it describes.** Both tests exist — one that the insight survives, one that a real
  bug still raises.
- Not addressed: nothing detects a stale server proactively. A version stamp compared at
  tool-dispatch time would, and every long-running MCP server has this exposure, not just
  this one call.


## ADR-042: A foreign project's card is demoted, except when the query is an error

**Date**: 2026-07-28 | **Status**: Accepted | **Tags**: `rank`, `injection`, `measurement`

> **Evidence retracted 2026-07-29 (ADR-043).** Every percentage below — 74%/43%, and the
> replay's used/ignored labels — came from `consolidate.card_was_used`, which was then
> measured at 1.8x separation against unrelated sessions, i.e. noise. The **decision stands**
> on the hand-inspected serving (a Smartsheet contributing guide pulled into a ProcessOS
> session by the prompt "make the public one commit") and on the principle that a stack
> trace is a property of the runtime, not of the repo it fired in. Do not cite the numbers;
> re-derive them after `afterwit run --remine`.

### Context
An agent working in one project reported cards from unrelated projects being injected,
and asked for cwd-scoping. Two of its other claims did not survive checking — there IS a
floor (`cfg.floor`, 0.35) and hook servings ARE mined for outcomes — but this one did,
and the cause was worse than reported: `index_db.search()` accepts a `project` argument
and **never filters on it**. It only widens the candidate LIMIT to `k*3`. The sole
project signal was rank's `+0.15` same-project boost, and a boost cannot do this job:
`floor` is absolute, so lifting the home project never pushes a foreign card below it.

### Options Considered
1. **Filter to `project IN (?, 'global')`** — what was asked for. Rejected on measurement:
   of this user's mined servings, foreign cards were used 3/7 = 43% against same-project
   32/43 = 74%. Worth less, not worthless — and one store spanning projects is the point.
2. **Demote foreign cards multiplicatively.** Chosen.
3. **Raise `PROJECT_BOOST`.** Rejected: it moves the home project, not the foreign one,
   so nothing new falls below an absolute floor.

### Decision
`CROSS_PROJECT_FACTOR = 0.75`, applied when a query names a project and the card belongs
to a different one; `global` is exempt because cross-cutting is what that project means.
**Error lookups pass `1.0`** — `inject._error_mode` and `mcp_server._lookup_error`.

That exemption is not symmetry-breaking for its own sake. Replaying every scored serving
in the real index under both weightings, the penalty dropped three cards: two noise
(`ignored`, from two unrelated projects, on the prompt "make the public one commit") and
one that had been mined as **USED** — a `reader-app` card about the Node ESM loader
ignoring `NODE_PATH`, matched from a different project on an `ERR_MODULE_NOT_FOUND`
trace. A stack trace is a property of the runtime, not of the repo it fired in. A vague
prompt is the opposite: that is where project context genuinely constrains the answer.
With the exemption the same replay drops 2 noise and 0 used cards.

### Consequences
- SPEC §12 gate re-run: recall@3 100%, MRR 1.000, no-answer precision 100%, OVERALL PASS.
- The tuning evidence is one user's ~50 scored exposures. `n=7` for the foreign arm is
  small; the factor is a starting point, and the replay script is the way to retune it.
- `index_db.search()` still does not filter by project. Left alone deliberately — ranking
  is the single scoring path (ADR-006), and moving the decision into SQL would put a
  second, invisible policy below it.
- Two smaller changes ship alongside: injected cards now carry their `id` (the `feedback`
  tool takes a `card_id`, and push is where most exposures happen, so the explicit channel
  was unreachable exactly where it mattered), and the error query is prefixed with the
  failing **command** — output-keyed matching dies to a `| tail -3`, and the command
  survives it. Prepended, because `rank._coverage` scores only the first 12 distinct
  tokens: ahead of the output the command is always inside that window.
- Not addressed: the deeper cause of weak absolute relevance is that `_normalize_bm25` is
  min-max, so the best candidate always scores 1.0 no matter how bad the field is.
  `_coverage` is the only absolute anchor and it is coarse on short queries — a 2-token
  prompt quantizes to {0, 0.5, 1.0}. That is a ranking rework with its own eval, not a
  weight change.

## ADR-043: Usage is measured by what a card ADDED, and every signal ships with a control

**Date**: 2026-07-29 | **Status**: Accepted | **Tags**: `measurement`, `consolidate`, `killswitch`

### Context
`card_was_used` decided whether a served card counted as USED, and through that the
`usefulness` rank term (`W_USE`), the 180-day decay, and the kill switch. It asked whether
≥60% of the card's distinctive **title** tokens appeared in the session's later text.

Run as a falsification control — the same rule, the same card, an **unrelated** session's
text — it answered yes almost as often: **95.7% own-session against 54.0% held-out, 1.8x**.
Against the session's own *pre-serving* text, 67 of 69 `used` verdicts were explained by
something the session had already said before the card arrived.

Consequences of that, all real and all previously reported as facts:
- the `70% inject hit rate` quoted on 2026-07-28 was meaningless;
- ADR-042's `74% same-project / 43% cross-project` tuning evidence was noise (the decision
  itself stands — see below);
- `usefulness` measured exposure, not value: 1648 of 1720 active cards sat at exactly 0.0,
  so `W_USE` could only re-rank incumbents;
- `KILLSWITCH_HIT_RATE = 0.20` could never trip against a 74% reading.

The bias was worse than random. A vague title (`Project completion status`, `STATE — where
we are / what's next`) matched any session; a precise one (`Never judge Playwright e2e on a
loaded box`) was judged honestly and usually lost 0.2. The metric was paying cards to say
nothing.

### Options Considered
1. **Extend `_COMMON`.** Rejected — whack-a-mole; the offending tokens are domain words
   (`epic`, `checklist`, `implementation`), not stopwords.
2. **IDF-weight by corpus rarity.** Rejected on measurement, not taste: **98% of active
   card titles already contain a token appearing in <1% of the corpus.** Rarity is not the
   axis; a title can be globally rare and still trivially present in the session that
   pulled it.
3. **Require title tokens to be NOVEL** (absent from query and pre-serving text). Rejected:
   0.5% recall. An `error_fix` card matched on a stack trace shares its title tokens with
   the query *by construction* — that is why it matched.
4. **Score what the card ADDED: body tokens, less title, less query, less pre-serving
   text.** Chosen.
5. **LLM judge.** Deferred. The module docstring's bar ("unless measured precision demands
   it") is now met, but 4 is 20 lines and offline-free; revisit if its recall proves too low.

### Decision
`card_was_used(title, body, later_text, prior_text, query)` — the last two **required**, so
a caller that drops them fails loudly (Gotcha #79). Candidate tokens are the card's first 25
body tokens minus its title tokens, minus anything already in the query or the pre-serving
text; ≥3 candidates must exist and ≥70% must appear afterwards. Measured (187 real
card-servings):

| rule | own | held-out | lift |
|---|---|---|---|
| title ≥60% (old) | 95.7% | 54.0% | 1.8x |
| body ≥3 | 44.4% | 11.8% | 3.8x |
| body ≥3 & frac ≥0.5 | 44.4% | 9.6% | 4.6x |
| **body ≥3 & frac ≥0.7** | **43.9%** | **5.9%** | **7.5x** |
| body ≥4 & frac ≥0.7 | 40.1% | 5.3% | 7.5x |
| body ≥5 & frac ≥0.7 | 33.7% | 4.3% | 7.9x |

Also measured and rejected: adding a title gate on top removed **no** own-session positives
and let **more** held-out ones through. The title carries no evidence here.

Three things ship with it:
- **`adapters.session_text_lookup` returns `(before, after)` from one parse.** Two lookups
  would mean parsing each transcript twice per serving; this user has a 170 MB session.
- **Explicit `feedback` overrides the mined guess** (`explicit_outcome`), scoped to the
  window between a serving and the next serving of the same card. **Substituted, never
  added** — those rows are positive-only in practice (28 of 28 are `helpful`), so adding
  them would bias a safety gate toward staying on.
- **`reset_usage` + `afterwit run --remine`.** `usefulness` accumulates (`usefulness + ?`),
  so without a reset the corrected miner stacks on top of the broken one's verdicts and the
  error is permanent. Explicit feedback rows survive the reset and replay.

### Consequences
- **Re-mining is required and has not been run yet.** 72 cards carry contaminated counters.
  Run `afterwit run --remine`; `write_back_usage` syncs frontmatter afterwards.
- **The kill switch may now trip, and that is the mechanism working.** The honest reading is
  no longer 74%. Ship ADR-044 (push narrowing) first so the next 30-day window measures the
  narrowed surface rather than condemning the old one.
- ADR-042's *decision* stands — on the hand-inspected noise (a Smartsheet contributing guide
  pulled into a ProcessOS session by the prompt "make the public one commit") and on the
  principle that a stack trace is a property of the runtime, not the repo. Its **percentages
  are retracted**; do not cite them.
- Gotcha #82: retuning after the `_norm` tokenizer fix moved the chosen lift from 12.6x to
  7.5x. The pre-fix numbers were wrong in the flattering direction.
- Nine mutants run against the new code; all nine killed. Four survived the first pass —
  including `mine_servings` calling the miner with `("", "")`, which is Gotcha #79 recurring
  inside the very change that cites it.
- `afterwit eval` re-run: recall@3 100%, MRR 1.000, trap precision 100%, OVERALL PASS.

## ADR-044: Push carries only what can change the next action

**Date**: 2026-07-29 | **Status**: Accepted | **Tags**: `injection`, `ranking`, `manifesto-p3`

### Context
Pull (`recall`/`why`/`for_file`/`lookup_error`) is asked for; push spends the agent's
attention without being asked. Both served every card type. Measured across 197 real
`mode='inject'` card-servings: `decision` 77, `fact` 43, `gotcha` 39, `doc_ref` 24,
`preference` 7, `error_fix` 6, `capability` 1 — **73% of the push budget went to reference
material.** "We chose JSONB because the schema churns" is worth looking up and worthless as
an interruption; "this API truncates silently" changes the next edit.

### Decision
`config.push_types`, default `{gotcha, error_fix, preference}` (248 of 1707 verified-active
cards), applied in `inject._serve` **before** ranking — filtering the top-k instead would
let three `doc_ref`s take the slots and leave the prompt silent. Widen via `push_types` in
`config.toml`; an explicitly empty list means "push nothing" and is honoured.

### Consequences
- Push goes silent far more often. That is Manifesto P3, not a regression.
- Pull is untouched; `test_recall_type_filter` is the standing proof.
- **The SPEC §12 eval gate cannot see this change.** `evalx` calls `rank.rank` directly, so
  it never exercises `inject._serve`. The paired test
  (`test_push_serves_behavioral_types_only_and_the_config_is_what_says_so`) is the only
  coverage; a push-surface arm for `eval` is the obvious follow-up.
- Three existing tests were fixtured on `decision` cards and would have passed vacuously;
  they now use a push-eligible type, with the reason recorded inline.

## ADR-045: Curated links are judged from a closed set, and land without per-link review

**Date**: 2026-07-29 | **Status**: Accepted | **Tags**: `graph`, `linking`, `manifesto-p6`, `manifesto-p4`

### Context
Measured on the live index (2026-07-29): 1720 active cards, **89% with no edge of any
kind**. The designed backbone — shared-file coupling — is capped at 23% by construction
(77% of cards cite no file), and of the files that are cited, 85% are cited by exactly one
card. Wikilinks number 12: the distiller cannot emit them honestly because it reads one
session and never knows what other cards exist. The one signal every card carries is its
text, and 1691 vectors are already stored — kNN over them is pure arithmetic, no model load.

### Options Considered
1. **Raw kNN `similar` edges** — full coverage, zero LLM cost. Rejected: cosine measures
   surface-text similarity, which clumps template-shaped cards (every `error_fix` shares the
   Error:/Fix: skeleton), elects near-centroid cards as hubs, and cannot tell *similar* from
   *related*. The user explicitly declined this noise source.
2. **Open link generation by the distiller** — rejected: reintroduces fabricated targets and
   slug-resolution guessing, the exact failure P6 exists to stop.
3. **Consuming-agent voting as link creation** — rejected: an agent inside a session has no
   corpus view either. As a *pruning* signal on existing links it is plausible — deferred
   until links exist and `--remine` has produced trustworthy usage data.
4. **Parent-directory file coupling** — measured: isolation falls only 11%→16%. Not worth
   the hairball risk.
5. **kNN proposes, an LLM judges, links land review-gated** — sound but self-defeating:
   the review queue already competes for attention, and a navigation edge is not worth a
   queue slot.

### Decision
kNN over stored vectors is a **candidate generator only** — its output is never surfaced
anywhere. A judge (the distill driver, `prompts/relink.md`) sees up to 8 candidates and
keeps at most 3; the kept set is intersected with the offered set, validated (target
exists, active, not self), written to a **machine-owned `related:` frontmatter key**, and
mirrored to `links(kind='related')` on upsert — so `index --rebuild` stays lossless (P4).

The P6 carve-out, scoped precisely: **a link is navigation metadata, not a knowledge
claim.** It lands unreviewed because (a) fabrication is structurally impossible — the judge
can only select ids it was handed; (b) it has zero serving-path impact — `rank` and
`inject` never read links; a wrong edge costs one bad row in `related`/the graph page;
(c) it is reversible as a class — `afterwit relink --strip` erases every auto-link in one
command; (d) the mechanism, not each link, is human-gated: `relink_budget` defaults to 0
and stays there until a hand-judged precision sweep over `afterwit relink --dry-run`
output passes on this corpus. Card *content* remains fully review-gated.

### Consequences
- **kNN recall is the ceiling on link recall.** A causally related pair with dissimilar
  text never becomes a candidate, so the judge never sees it. Offering 8 to keep 3 raises
  the ceiling cheaply; the residual is reachable only by hand-written wikilinks (still
  honoured) or a future co-serving signal.
- One LLM call per judged card, bounded by `relink_budget` per nightly run. "Judged,
  nothing kept" is memoed (`relinked` table — operational state like `servings`; losing it
  costs re-judging, never correctness) so a rejection is not re-bought every night.
- Edits to `prompts/relink.md` re-require the precision sweep, same discipline as
  `prompts/distill.md` and `afterwit eval`.

### Sweep results (2026-07-29, same day)
Two dry sweeps. (1) `--limit 10` via CLI: all imported ADR doc_refs (eligibility orders
`updated DESC`) — 14/14 links judged real, but relations explicit by construction, the easy
cases. (2) 36 cards random-stratified toward clumping-risk types (8 gotcha / 8 decision /
6 error_fix / 6 fact / 4 capability / 4 preference): **66 kept of 275 offered (24%)**, two
cards rejected all 8 candidates, zero template-clump keeps (no error_fix linked to an
unrelated error_fix). Hand-judged: ~60/66 genuine, ~3 weak-but-defensible, and the only
systematic failure was **near-duplicates linked instead of deduped** — kept pairs at cos
1.00/0.96/0.94 were the same knowledge as two or three cards. Fix: `DUP_CEILING = 0.92` —
a pair above it is never offered as a candidate; duplicates are postprocess/supersede
work, and linking them would paper over the real defect. The sample's strongest genuine
relation sat at 0.89 (a gotcha and the decision that softened it), so the split is
data-derived, not taste. Cross-project keeps (17) were mostly *slug-variant artifacts* —
`book_reading` vs `book-reading`, `ces_appscript` vs `ces-appscript` are the same projects
under two spellings (an ADR-039 `project_aliases` hygiene issue, out of scope here); the
genuinely-foreign keeps were the runtime/tooling kind the prompt allows. Enabling
(`relink_budget > 0`) remains the user's call.

## ADR-046: The nightly's time is a setting, and changing it reschedules in place

**Date**: 2026-07-29 | **Status**: Accepted | **Tags**: `scheduler`, `settings`, `install`

### Context
`install cron` hardcoded 02:30 in four scheduler dialects (systemd `OnCalendar`, launchd
`StartCalendarInterval`, schtasks `/ST`, crontab `30 2`). Changing the time meant editing
unit files by hand — on the right platform, in the right syntax. The Settings surface
(ADR-034) exists precisely so a knob like this is one field, not four dialects.

### Decision
`run_time` ("HH:MM", 24h, default 02:30) in config.toml, rendered as a native
`<input type="time">` via a new `time` kind in `config.EDITABLE`; one validator
(`install._parse_time`) shared by `coerce` and every scheduler branch, so a value that
passes Settings cannot fail at install. `afterwit install cron` reads it. Saving a changed
`run_time` calls `install.cron_scheduled()` — detection only — and reapplies an
ALREADY-INSTALLED scheduler with the new time; if none is installed, it saves the config
and says so, never installing a scheduler behind the user's back.

### Consequences
- schtasks idempotence had to learn about schedules: it compared the command only (a
  `ponytail:` comment predicted the bite), so a pure time change read as "unchanged".
  It now also compares `<StartBoundary>`; XML without a readable one degrades to the old
  command-only check rather than churning the task every install.
- The crontab/systemd/launchd branches were already content-compared, so a time change
  rewrites them naturally.
- Verified: 383 tests, 6/6 mutants killed (per-dialect hardcode restorations, schedule
  compare dropped, reschedule gate inverted, validation dropped).
