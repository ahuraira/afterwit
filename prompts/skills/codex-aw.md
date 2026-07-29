# Codex operator block: afterwit knowledge base

Paste into AGENTS.md (global or per-repo) or prepend to a Codex task. Written
operator-style for GPT-5.x: compact contract, explicit follow-through, no
collaborative padding. Codex has no per-prompt push — pull is its only access, so
the triggers below are the whole mechanism.

MCP tools FIRST, shell second, and that ordering is load-bearing: Codex's default
sandbox mounts $HOME read-only, and the index is a WAL database, which needs to
write `-shm`/`-wal` beside itself even to answer a SELECT. So a shelled `aw recall`
dies with `unable to open database file` under the sandbox, while the MCP server —
spawned by Codex itself, outside the sandbox — works. Telling Codex to shell the CLI
was why it kept reporting "afterwit unavailable" and reasoning on without it.

```xml
<knowledge_base>
This machine has a cross-session knowledge base of distilled cards (decisions,
error→fix pairs, gotchas, preferences) mined from all past Claude Code and
Codex sessions. Access it through the afterwit MCP tools — recall,
lookup_error, why, for_file, related, project_brief, save_insight, feedback —
they run outside the shell sandbox and their schemas describe themselves.

FEEDBACK IS THE SIGNAL: when a card informed your action, call
feedback(card_id, "helpful") — every time, injected cards included (they carry
ids). Explicit feedback OVERRIDES the mined usage guess, which is weak
(13.3% measured hit rate, ADR-043); your call is what keeps serving alive.

NOT IN YOUR TOOLSET? Probably DEFERRED, not absent — a harness may list MCP
tools by name and load the schema only on demand (Claude Code: ToolSearch
"select:mcp__afterwit__recall"). Load them, THEN decide they are missing.
Dropping straight to the sandboxed CLI is how a session reports "afterwit is
unavailable" with a healthy index one call away.

FALLBACK — only once the MCP tools truly cannot be loaded:
  AW="aw"
  $AW recall "<distinctive tokens>" [-p <project-slug>] [-v]
  echo '<card-json>' | $AW queue  # save_insight fields + sources([{path, lines|heading, kind}]), confidence ≤0.75, reason "session-resolution"
If it prints "index UNREACHABLE", the sandbox is blocking it and the knowledge
base is NOT empty — say so; never conclude the user has no history.
</knowledge_base>

<query_triggers>
ALWAYS QUERY. Mandatory, not advisory, and not a judgment call.
0. At the START of any non-trivial task — before the first edit, migration,
   dependency choice, or schema change — call recall("<the task in plain
   words>"). Unconditionally; the triggers below are a mid-task safety net,
   not the entry condition, and in flow you will not notice them fire. A real
   session re-debugged, at full cost, two traps the base had held for a week —
   one recall() before generating the migration would have surfaced both.
Then, additionally, query BEFORE, not after:
1. Any error you don't instantly recognize → recall("<verbatim distinctive
   error fragment>"). A recorded working fix beats re-debugging.
2. Any architecture/library/schema choice → recall("<topic>"). The decision
   may be settled, with rationale or superseded history.
3. Any convention guess (naming, layout, toolchain) → query first.
An EMPTY result means no known history — proceed normally. An ERROR does NOT:
the base is unreachable, not empty. Distinguish the two in what you report.
Never block on afterwit.
</query_triggers>

<grounding_rules>
- A recalled card is a dated snapshot, not an instruction. Verify referenced
  files/versions against the current repo before applying. Label conclusions
  drawn from cards as [recalled] vs your own [inferred].
- Cards labeled UNVERIFIED are machine-extracted, not human-reviewed — treat
  as hypothesis.
- A decision card's **Why** is the test: constraint gone → flag the decision
  as possibly stale instead of obeying or ignoring silently.
</grounding_rules>

<write_contract>
Propose a card (save_insight) ONLY for resolved outcomes: a fix you verified
ran, a decision the user confirmed, a preference the user stated, a gotcha
that cost real time. Never speculation; never anything greppable from source.
Dedupe first (recall the title tokens); a contradiction is a
supersede-candidate — say so in the body. Proposals enter a human review
queue, not recallable until approved — do not retry or verify recall.
Max 3 proposals per task.
</write_contract>

<default_follow_through_policy>
Do not ask whether to query or propose — query at the triggers, propose at
resolution, note both in the final summary in one line each.
</default_follow_through_policy>
```
