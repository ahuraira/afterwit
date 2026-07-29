# afterwit — Domain Research
**Generated**: 2026-07-05
**Lens**: domain

## Summary
- **Both harnesses only offer two real injection modes**: additive text at session/prompt boundaries (CLAUDE.md/AGENTS.md + hooks) and pull-based tools/resources (MCP, skills). Claude Code has a genuine *per-prompt* push channel (`UserPromptSubmit` hook → `additionalContext`, 10k-char cap); Codex has **no per-prompt push** — only session-start docs + MCP tools. This asymmetry decides the architecture: push a small budget on Claude Code, expose everything as MCP tools for parity with Codex.
- **The data is huge but mostly retrievable, and the bloat is NOT what you'd expect.** ~24 Claude top-level transcripts (249 MB, median 3 MB, one 64 MB) + ~1000 nested subagent logs (715 MB tree); 48 Codex rollouts (159 MB, one 69 MB). `file-history-snapshot` is only ~0.5% of bytes — the real weight is large `tool_result` payloads (bash stdout, file reads) stored inside `user` records = **83% of bytes**. Filter tool outputs, not snapshots.
- **High-value knowledge already lives in structured signals**: user corrections ("no, actually…"), `tool_result` `is_error:true` → next successful retry (error→fix pairs), and the user already hand-curates 85 memory files with typed frontmatter (`feedback`/`project`/`user`/`reference`). Mine the transcripts to *propose* memories in that exact format; the schema is the contract.

## Injection surfaces — Claude Code

| Surface | When it fires | How content reaches the model | Limits / caveats |
|---|---|---|---|
| **CLAUDE.md hierarchy** | Session start (+ `InstructionsLoaded` on nested traversal, includes, compact) | Preloaded verbatim into system context; enterprise → user (`~/.claude/CLAUDE.md`) → project → subdir, all concatenated | Always-on cost every turn. No per-turn selectivity. Big files burn budget for the whole session. |
| **`@file` reference** | When the referenced path appears in a loaded instruction/message | File contents inlined at load time | Static; resolved once. Good for stable docs, bad for "relevant-right-now" knowledge. |
| **SessionStart hook** | startup / resume / clear / compact | stdout + `hookSpecificOutput.additionalContext` injected silently before first prompt (v2.1.0+: no user-visible msg) | **10k-char cap** (overflow → written to file, model gets path+preview). Per-session, not per-prompt. |
| **UserPromptSubmit hook** | Every prompt, before model sees it | stdout + `additionalContext` added *alongside* the prompt | **10k-char cap**, 30s timeout. THE per-prompt push channel. Can't replace prompt; exit 2 blocks+erases it. |
| **PreToolUse / PostToolUse / PostToolUseFailure hook** | Around each tool call | `additionalContext` placed next to the tool result | Reactive to tool activity, not prompt intent. Good for "you just edited X, here's the gotcha". |
| **Stop hook** | Model finishes a turn | `additionalContext` (exit 2 or `decision:block` forces continuation) | Can inject "you forgot Y" and keep the loop going. |
| **MCP tools** | Model chooses to call | Tool schema (name+desc) preloaded into context; result returned on call | Schemas cost context always-on. Result cap ~25k tokens default. Pull-based = model must decide to fetch. |
| **MCP resources** | Model/host references a resource URI | Content returned on read | Lower schema overhead than tools; less reliably invoked by the model. |
| **Skills (SKILL.md)** | name+desc preloaded; body loaded on activation | Progressive disclosure: ~30–50 tokens/skill at start → ~5k-token body when triggered → referenced files loaded on demand | Cheapest always-on footprint per unit of knowledge. 20–50 skills run with no measurable overhead. Ideal for "knowledge packs" gated by description-match. |
| **Output styles** | Session config | Alters system prompt framing | Not a knowledge channel; ignore for injection. |

**Additive-per-session**: CLAUDE.md, @file, SessionStart, skill metadata, MCP schemas.
**Additive-per-prompt**: UserPromptSubmit (the one true dynamic push), Pre/PostToolUse (per-tool).
**Pull-on-demand**: MCP tools/resources, skill bodies.

## Injection surfaces — Codex CLI

| Surface | When it fires | How content reaches the model | Limits / caveats |
|---|---|---|---|
| **AGENTS.md** | Session start; discovered by walking up from cwd to project root | Loaded verbatim into instructions | `project_doc_max_bytes` (this machine: 262144). `project_doc_fallback_filenames` = `["CLAUDE.md",".claude/CLAUDE.md"]` — **Codex already reads the user's Claude memory** (and `~/.codex/AGENTS.md` is a symlink to `~/.claude/CLAUDE.md`). |
| **config.toml `developer_instructions`** | Session start | Injected as developer message | Static per session. |
| **MCP servers** `[mcp_servers.<id>]` | Model chooses to call | stdio (`command`/`args`/`env`) or HTTP (`url`); `enabled_tools` allow-list; tools namespaced `<server>:<tool>` | **Tools documented; resources not**. `supports_parallel_tool_calls` plumbed through (v0.140.0). Project-scoped `.codex/config.toml` only for trusted projects. |
| **Hooks** (`hooks.json` or inline `[hooks]`) | PreToolUse/PostToolUse/SessionStart etc. | Same event schema as Claude-style hooks | Project-local hooks load only if `.codex/` layer trusted. **No documented per-prompt `additionalContext` push** — hooks here are gate/notify, not context injection. |
| **`notify` command** | On notifications | Receives JSON payload | Outbound only; not a model-context channel. |

**Verdict**: Codex = **session-start docs + MCP tools only**. No per-prompt injection. So the portable design is MCP-tool-first; the Claude-only `UserPromptSubmit` push is a bonus layer.

## Anthropic's own guidance (what to inject vs expose)
- **Just-in-time > pre-computed**: keep lightweight identifiers (paths, queries, links) and load at runtime via tools rather than front-loading everything. Claude Code itself is the reference hybrid: preload CLAUDE.md, `grep`/`glob` for the rest.
- **Hybrid for stable domains**: some upfront context is fine where content is slow-changing.
- **Memory tool / structured notes**: persist state in files across sessions (the user's `memory/*.md` is exactly this pattern, done manually).
- **Tools must be minimal, non-overlapping, token-efficient**; paginate/filter/truncate — Claude Code caps tool responses at ~25k tokens.
- **Sub-agents return 1–2k-token distilled summaries**, keeping search context isolated — pattern for a "deep-recall" retrieval agent.

## Local data ground truth

**Claude sessions** — `~/.claude/projects/<slug>/<uuid>.jsonl`. First line is a `mode` record, not a message. Record types (one 14.8k-line session): `assistant` 5848, `user` 2782, `mode`/`permission-mode`/`ai-title` ~1145 each, `last-prompt` 1143, `attachment` 677, `file-history-snapshot` 285, `system` 249. Also seen: `queue-operation`, `bridge-session`.

```jsonc
// user with a tool_result (this is where bash/file output lives — the bulk of bytes)
{"type":"user","message":{"role":"user","content":[
  {"type":"tool_result","tool_use_id":"...","is_error":false,"content":"total 8\n..."}]},
  "toolUseResult":{"stdout":"...","stderr":"...","interrupted":false,"isImage":false},
  "cwd":"...","gitBranch":"...","isSidechain":false,"promptId":"...","timestamp":"...","uuid":"...","parentUuid":"..."}
// assistant with tool_use
{"type":"assistant","message":{"role":"assistant","content":[
  {"type":"tool_use","name":"Bash","input":{"command":"...","description":"..."}}],
  "usage":{"input_tokens":5163,"output_tokens":7452,"cache_read_input_tokens":16394,...}},"uuid":"...","parentUuid":"..."}
// file-history-snapshot — a POINTER, not file content (~247 bytes): {messageId, trackedFileBackups, timestamp}
```
- **Extractable**: user prompts (real ones vs `<local-command-caveat>` synthetic), assistant text, tool calls + inputs, `toolUseResult.stdout/stderr`, `is_error` flags, `usage` (cost), `cwd`/`gitBranch` (project+branch context), `parentUuid` chain (turn threading), `isCompactSummary` records (session TL;DRs), `ai-title`.
- **Noise / drop**: 83% of bytes are `user` records dominated by large `tool_result` payloads (dir listings, file reads, base64 images via `imagePasteIds`); `attachment` `deferred_tools_delta` spam; `mode`/`permission-mode` churn. `file-history-snapshot` is cheap (~0.5%) — don't over-invest in stripping it.
- **Sidechains/subagents**: `isSidechain:true` marks subagent turns (0 in the sampled solo session, but present in team runs); the ~1000 nested `.jsonl` under each project's `<uuid>/` dir are teammate/scratchpad logs.

**Memory files** — `~/.claude/projects/<slug>/memory/*.md`, 85 files. Frontmatter `name` / `description` / `type` where type ∈ `feedback`(28), `memory`(30), `project`(23), `user`(3), `reference`(1). Body is prose + `[[wikilinks]]`. **This is the target output format** — the user already maintains it by hand; the system should populate it.

**Todos**: `~/.claude/todos` empty. **History**: `~/.claude/history.jsonl` (1.05 MB) — flat prompt log.

**Codex sessions** — `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`, 48 files, 159 MB (median 0.43 MB, one 69 MB).

```jsonc
{"type":"session_meta","payload":{"id":"...","cwd":"/home/user/Desktop/Projects/acme_flow","cli_version":"0.110.0","originator":"codex_cli_rs"}}
{"type":"turn_context","payload":{"cwd":"...","current_date":"...","approval_policy":"on-request","sandbox_policy":"..."}}
{"type":"response_item","payload":{"type":"message"|"reasoning"|"function_call"|"function_call_output"|"web_search_call"|"custom_tool_call",...}}
{"type":"event_msg","payload":{"type":"user_message"|"agent_message"|"token_count"|"task_started"|"turn_aborted"|"context_compacted",...}}
{"type":"compacted","payload":{"message":"","replacement_history":[...]}}  // full compaction summary preserved
```
- Record dist (one 1296-line session): `response_item` 968, `event_msg` 309, `turn_context` 16, `compacted` 2, `session_meta` 1. `response_item` payloads: `reasoning` 229, `function_call`/`function_call_output` 260 each, `message` 107, `web_search_call` 72, `custom_tool_call` 20.
- **Extractable**: `event_msg.user_message` (clean user turns), `agent_message` (assistant text), `function_call` + `function_call_output` (tool I/O), `reasoning` (Codex chain-of-thought — unique signal), `compacted.replacement_history` (pre-digested summaries), `session_meta.cwd` for project attribution.
- **Noise**: `token_count` events (186 in one session), `reasoning` bulk if not wanted, aborted turns (`turn_aborted`).

**Config**: `~/.codex/config.toml` — model `gpt-5.5`, `project_doc_fallback_filenames=["CLAUDE.md",".claude/CLAUDE.md"]`, one MCP server (`chrome-devtools`), `[features] memories/goals` on. **History**: `~/.codex/history.jsonl` — `{session_id, ts, text}` prompt log.

## Extraction targets

| Knowledge | Where in schema | Detection heuristic |
|---|---|---|
| **User corrections / preferences** | Claude `user.message.content` (string); Codex `event_msg.user_message` | Regex on real user turns (exclude `<local-command-caveat>`, `isMeta`, `isCompactSummary`): `no,? actually`, `don't`, `stop`, `I said`, `use X not Y`, `prefer`. High-signal for `feedback`/`user` memories. |
| **Error → fix pairs** | Claude `user` `tool_result.is_error:true` (or `toolUseResult.stderr` non-empty) → later `is_error:false` for same tool/target; Codex `function_call_output` with error → next successful call | Pair a failing tool_result with the next successful call on the same file/command within the parentUuid chain; the diff between the two inputs is the fix. |
| **Decisions + rationale** | assistant text following a user question; `isCompactSummary` records; Codex `compacted.replacement_history` | Compaction summaries already distill "architectural decisions, unresolved bugs" — mine these first, cheapest ROI. Assistant paragraphs containing "because/so that/instead of/trade-off". |
| **Gotchas / surprises** | assistant text; existing `memory/*.md` `feedback` files | "turns out", "gotcha", "watch out", "the trick is", errors that recurred across sessions. |
| **Project facts** | `session_meta.cwd` / `user.cwd` + `gitBranch`; file edit tool_uses (Write/Edit inputs) | Group by cwd → per-project fact set. Edited paths reveal structure; commands reveal toolchain (build/test/lint). |
| **Cost/effort hotspots** | `assistant.usage`, Codex `token_count` | Which tasks/files burned the most tokens → candidates for a cached knowledge pack. |

## What this means for our implementation
- **Two-tier delivery, one knowledge store.** Store mined knowledge as memory-format `.md` (matching the user's 85-file convention). Expose it via **(a) an MCP server** (works in both harnesses, pull-based, namespaced tools like `harness:recall`) and **(b) a Claude Code `UserPromptSubmit` hook** that pushes the top-k relevant snippets under the 10k-char cap. Codex gets tool-only; Claude Code gets tool + push.
- **Skills for stable knowledge packs, hook/MCP for dynamic recall.** Per-project or per-domain "knowledge packs" fit the skill model (progressive disclosure, ~30–50 tokens/skill idle). Session-specific relevant recall goes through the per-prompt hook.
- **Ingest cheaply**: parse line-delimited JSON, keep user/assistant text + tool inputs + error flags + cwd, **drop tool_result payloads over N bytes** (that's the 83%), skip file-history-snapshots and token_count. Codex `compacted` and Claude `isCompactSummary` are pre-summarized — index them first.
- **Don't fight staleness by front-loading.** Follow Anthropic's just-in-time model: index → lightweight IDs → load on demand. A deep-recall sub-agent returning 1–2k-token summaries fits the MCP-tool shape.

## Things to verify
- Exact `UserPromptSubmit` overflow behavior (does the model reliably read the spilled file path?) and whether 10k chars is pre- or post-truncation of the k snippets.
- Whether Codex MCP **resources** (not just tools) are usable in the installed `0.110.0`/`0.140.0` build — docs only mention tools; test empirically.
- Real prevalence of `isSidechain` and nested subagent transcripts in this machine's team runs (sampled solo session had 0) — affects dedup strategy.
- Confirm `history.jsonl` (both harnesses) is a strict superset of prompts in transcripts, or if it captures prompts that never reached a saved session.
- Skill `description`-match reliability as a routing mechanism for per-project knowledge packs (false-trigger rate at 20–50 skills).

## Sources
- [Claude Code Hooks reference](https://code.claude.com/docs/en/hooks)
- [Codex Configuration Reference](https://developers.openai.com/codex/config-reference)
- [Codex MCP](https://developers.openai.com/codex/mcp) / [Codex Config basics](https://developers.openai.com/codex/config-basic)
- [Anthropic — Effective context engineering for AI agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
- [Anthropic — Writing effective tools for AI agents](https://www.anthropic.com/engineering/writing-tools-for-agents)
- [Anthropic — Equipping agents with Agent Skills](https://www.anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills)
- [Claude Skill authoring best practices](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices) / [Agent Skills overview](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview)
- Local ground truth: sampled `~/.claude/projects/*/*.jsonl`, `~/.claude/projects/*/memory/*.md`, `~/.codex/sessions/**/rollout-*.jsonl`, `~/.codex/config.toml`, `~/.codex/history.jsonl` (2026-07-05).
