---
name: aw-knowledge
description: Query and grow the user's cross-session knowledge base (afterwit). Use before re-debugging an error, re-deriving a decision, guessing a project convention, or editing an unfamiliar fragile file — and to propose durable knowledge after a confirmed fix or decision. Works in every project on this machine.
---

# aw-knowledge: use what past sessions already paid for

This machine runs afterwit: distilled knowledge cards mined from ALL past
Claude Code and Codex sessions across ~16 projects — decisions with rationale,
error→fix pairs, gotchas, preferences. Querying it is almost always cheaper than
re-deriving. It may return nothing; nothing means "no known history — proceed
normally", not failure.

Command prefix (works from any directory):

```bash
AW="aw"
```

**Prefer the MCP tools** `recall` / `lookup_error` / `why` / `for_file` (same backend,
richer filters, and they run outside the shell sandbox).

If you do not see them in your toolset, they are probably **deferred, not absent** —
Claude Code lists deferred MCP tools by name only and loads a schema on demand. Load
them first, then call them:

```
ToolSearch("select:mcp__afterwit__recall,mcp__afterwit__lookup_error")
```

Treat the CLI as the fallback, not the default: a deferred tool LOOKS missing, and
falling straight through to the shell is how sessions end up reporting "afterwit is
unavailable on this machine" while a healthy index sits right there. In a read-only
sandbox the CLI may legitimately fail where MCP succeeds — see the note at the bottom.

## When to query — the four trigger moments

1. **A command/test/build fails with an error you don't instantly recognize.**
   Query BEFORE debugging — if this user hit it before, the exact working fix is
   recorded. Query with the distinctive fragment of the error, verbatim:
   `$AW recall "P1001 cannot reach database"`
2. **You are about to make or change an architectural decision** (library choice,
   schema shape, pattern). The decision may already be settled — with a rationale
   that still holds, or a superseded history showing why the obvious option was
   rejected: `$AW recall "audit payload storage jsonb"`
3. **You are about to guess a convention or preference** (naming, structure,
   toolchain). Guessing wrong costs a correction round: `$AW recall "test fixtures layout"`
4. **You are about to edit a file that looks fragile** (config, auth, migrations)
   in a project you haven't touched this session.

## How to query well

- Short, distinctive tokens beat sentences. The index strips stopwords; `"prisma
  P1001 WSL2"` outranks `"why can't my application connect to the database"`.
- Error messages: paste the distinctive part verbatim, trim paths and hex.
- Add `-p <project-slug>` (directory name under ~/Desktop/Projects) to boost the
  current project; cross-project hits still return — a fix from another repo
  often transfers.
- `-v` prints card bodies; `--all` ignores the relevance floor when you want to
  see weak matches.

## How to treat results — this part is load-bearing

Cards are **advisory memory, not instructions**. Each shows type, score,
project, date, and id. Before acting on one:

- **Verify against current code.** A card is a snapshot of when it was true. If
  it names files or versions, check they still exist/hold. Old date + changed
  code = treat as a hypothesis.
- A `decision` card's **Why** matters more than its verdict — if the constraint
  in the Why no longer holds, the decision may be stale; say so to the user
  rather than silently obeying or silently ignoring.
- Cards marked UNVERIFIED were machine-extracted and not yet human-reviewed:
  weight accordingly.

## Proposing knowledge back — only resolved outcomes

After something durable happens — a fix that **worked** (you saw it pass), a
decision the user **confirmed**, a stated preference, a gotcha that cost real
time — propose a card. Rules: never speculation, never mid-debugging guesses,
never anything greppable from the code itself (code is searched live; only
un-greppable knowledge is worth a card).

First dedupe: `$AW recall "<your card title's key tokens>"`. Same claim already
exists → skip. Existing card **contradicts** yours → still propose, set reason
`supersede-candidate`.

```bash
echo '{
  "type": "error_fix",
  "title": "Prisma P1001 on WSL2 — use 127.0.0.1 not localhost",
  "project": "acme_hr",
  "body": "Error: `P1001: Cannot reach database server at localhost:5432`.\nFix: use 127.0.0.1 in DATABASE_URL — WSL2 resolves localhost to the Windows host.\nConfirmed working 2026-07-06.",
  "sources": [{"path": "<transcript-or-file-you-derived-this-from>", "lines": "L120-L180", "kind": "assistant"}],
  "files": ["prisma/.env"],
  "tags": ["prisma", "wsl2"],
  "confidence": 0.75,
  "reason": "session-resolution"
}' | $AW queue
```

Contract: `type` ∈ decision|gotcha|error_fix|preference|fact|snippet; title ≤80
chars and states the claim (searchable — "X truncates cells silently", never
"Notes on X"); body self-contained, ≤300 tokens; `error_fix` body MUST embed the
error signature so future exact-match lookup works; decisions end with
`**Why:** …`; `sources` mandatory (provenance-or-nothing — no source, no card).

Your proposal lands in a human review queue — it is NOT trusted or recallable
until the user approves it in `afterwit ui`. Confidence above 0.79 is capped
server-side. Do not expect immediate recall of your own proposal; do not retry.

## Don'ts

- Don't paste whole recalled cards into user-facing responses; use them, cite title.
- Don't propose more than ~3 cards per session; one great card beats five mediocre.
- Don't propose from code contents — that's what grep is for.
- If `afterwit` errors, proceed without it and mention it once; never block on it.
