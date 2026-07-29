# afterwit

Personal knowledge system: mines Claude Code + Codex session transcripts, docs, memories, and DB schemas into distilled knowledge cards (markdown wiki + SQLite index), then serves them back to both harnesses via MCP tools and bounded hook injection.

## Read first (in this order)

1. `docs/research/afterwit-MANIFESTO.md` — 10 binding principles. Implementations are scored against them; violations don't ship.
2. `docs/SPEC.md` — the full system spec (v0.2). Section numbers below refer to it.
3. `docs/ADR.md` — locked decisions. Do NOT re-litigate; append new ADRs for new decisions.

## Hard rules (violations = rejected PR)

- **Never index/embed code bodies.** Only distilled knowledge (SPEC §2, Manifesto P2).
- **Never serve raw transcript chunks.** Cards only; provenance links back to raw (P1).
- **Injection caps are hard limits**: ≤3 cards, ≤600 tokens per prompt; threshold-gated; emitting nothing is the common case (P3).
- **Wiki markdown is source of truth; SQLite is a rebuildable cache.** Any schema change must keep `afterwit index --rebuild` lossless (P4).
- **Every card cites sources** (file + line range). Agent/LLM writes always land review-gated — nothing silently becomes trusted (P6).
- **Supersede, don't duplicate**; quarantine, don't delete (P5, P6).
- **Distillation promotes only resolved outcomes** — a fix that worked, a decision that stuck, an explicit user confirmation (SPEC §7).
- **No new heavy deps.** stdlib first; approved: `fastembed` (P4+), `mcp`, `pyyaml`. Anything else needs an ADR.

## Toolchain

- Python 3.12, `uv`. Run: `uv run afterwit <cmd>`. Tests: `uv run pytest`. Lint/type: `uv run ruff check && uv run mypy src/` (strict — fix types, never loosen).
- Package layout is fixed in SPEC §13a — create files exactly there, no new top-level dirs.
- The distillation prompt lives at `prompts/distill.md` — versioned, load-bearing; changes require re-running `afterwit eval`.

## Conventions

- One card = one markdown file; frontmatter contract in SPEC §5.1 — never write a card missing `id`, `type`, `status`, `sources`.
- Adapters emit normalized events (SPEC §6); they must skip-and-log unknown record types, never crash on schema drift.
- Hook path (`afterwit inject`) is latency-critical: p95 < 200ms; no network, no embedding-model load unless vectors are precomputed.
- Every non-obvious decision → append ADR; every surprising behavior → ADR Gotchas Reference.
- User-visible changes → `CHANGELOG.md` (Keep-a-Changelog).

## Phase discipline

Build in SPEC §14 order (P1→P5); each phase has explicit done-criteria — verify against them (run the command, paste output) before calling a phase complete. Ranking-weight or prompt changes require `afterwit eval` re-run (§12 gates).
