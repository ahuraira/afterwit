# afterwit

*Afterwit: the clever thing you think of once the moment has passed.*

You and your AI assistant solve a hard problem on a Tuesday. Six weeks later, in
a different project, you hit the same problem — and solve it again from scratch,
because the transcript where you figured it out is one of four hundred JSONL
files you will never open. afterwit reads those transcripts overnight, distills
the outcomes that actually stuck (a fix that worked, a decision you made, a
convention you chose) into small reviewable notes, and hands the relevant ones
back to Claude Code and Codex the next time they come up. It is a memory for the
things you and your AI already figured out.

---

## Quick start (60 seconds)

You need [uv](https://docs.astral.sh/uv/getting-started/installation/). If you
don't have it, install it first — one command, no Python knowledge required.

```bash
uv tool install afterwit
afterwit init
```

`afterwit init` asks a few questions and then does everything else. When it
finishes, start a new Claude Code session and ask it something you solved months
ago. Nothing else to configure.

## What `afterwit init` does

1. Finds your projects directory (usually `~/Projects` or similar) and confirms it with you.
2. Creates your **knowledge wiki** — a folder of plain markdown files, one per note.
3. Offers to create a **private** GitHub repo for that wiki, so it follows you between machines.
4. Registers afterwit with Claude Code and Codex: an MCP server (so the assistant can search your knowledge on demand) and a session-start hook (so a few relevant notes arrive automatically when they clear the relevance bar).
5. Installs the skills, and puts the `aw` command on your PATH if it isn't already.
6. Schedules the nightly run — 02:30 local, via systemd on Linux, launchd on macOS, Task Scheduler on Windows.
7. Builds the index, then **verifies the whole thing** — it walks the same path an agent walks (index, MCP entry, hook, skills, CLI) and **exits nonzero if any door is shut**.

That last step is the point. An installer that can't check its own work is how you
end up with a healthy knowledge base that no agent can reach, reporting success the
whole time. `afterwit init` finishing green means *checked*, not *attempted*. You can
re-run the same check any time with `afterwit doctor` (and `afterwit doctor --fix` if
you ever move the checkout).

The nightly run is where the work happens: it reads new sessions, asks an LLM to
distill the resolved outcomes, and files what it finds into a review queue. It ends
with the same reachability check, so a broken install fails the run instead of
quietly filling an index nobody can open.

## The two-repo model

afterwit keeps two things apart:

- **This repo** — the program. Public, boring, no data.
- **Your knowledge wiki** — a *separate* git repo holding your notes.

> ### ⚠️ Your knowledge wiki must be a PRIVATE repository.
>
> The wiki is distilled from your real sessions. Those sessions contain your
> code, your database schemas, your internal service names, your bugs, and
> sometimes credentials you pasted at 2am and forgot about. afterwit redacts
> what it can recognise, but redaction is a filter, not a guarantee.
>
> If `afterwit init` creates the repo for you, it creates it private. If you
> point afterwit at a wiki repo you made yourself, **check the remote before
> the first sync.** A public knowledge wiki is a public dump of everything you
> have ever debugged.
>
> As a backstop, `afterwit sync` asks GitHub whether your wiki's remote is
> private and **refuses to push** if it can prove the answer is no. It can only
> check GitHub, and only when the `gh` CLI is installed and signed in —
> everywhere else it warns and proceeds. Do not rely on it as your only defence.

## What a note looks like

The four types you'll see most are `error_fix` (a failure and the fix that
worked), `decision` (a choice and why you made it), `gotcha` (surprising
behavior that cost you time), and `preference` (how you like things done).
There are a handful of others — `fact`, `snippet`, `db_schema`, `capability` —
that the nightly run produces less often.

Every note is one markdown file, and every note cites where it came from:

```markdown
---
id: 01KX3WFQ6BZEK6A7194317V3EX
type: error_fix
title: Node ESM loader ignores NODE_PATH — ERR_MODULE_NOT_FOUND for out-of-tree deps
project: reader-app
status: active
tags: [node, esm, node-path, module-resolution]
files:
  - e2e/fixtures/pdf/make-pdf-fixtures.mjs
confidence: 0.9
verified: true
sources:
  - path: ~/.claude/projects/reader-app/248c5aaa.jsonl
    lines: [267, 267]
---

`node script.mjs` fails with `ERR_MODULE_NOT_FOUND` for a package that is
installed, because the ESM loader ignores `NODE_PATH` entirely (unlike CJS
`require`). Fix: import via a relative path, or run the script from the
directory whose `node_modules` holds the dependency.
```

Because it's markdown, you can read it, edit it, `grep` it, and see its history
in `git log`. The SQLite index next to it is a cache — delete it and
`afterwit index --rebuild` reconstructs it from the markdown.

## The review gate

An LLM wrote those notes by reading your transcripts. It is sometimes wrong.
So nothing an LLM or an agent writes is trusted on arrival: it lands in a review
queue with `verified: false`, and it stays there until a human — you — approves
it.

```bash
afterwit ui     # opens a local review UI on 127.0.0.1
```

You'll see each proposed note and its sources, and you approve or reject it.
Approving writes the note into your wiki with `verified: true`; only verified
notes are pushed into your assistant's prompts. Rejecting drops it from the
queue and records the rejection in the audit log.

### Auto-review

Nobody triages 200 notes. If you won't, an LLM can — but the rule that keeps
this system safe is **the agent that wrote a note must not be the agent that
approves it**. A human was simply the only reviewer we had.

So auto-review uses a *different* model from the one that wrote the note (if
Codex distilled it, Claude reviews it), and:

- it **abstains** whenever it is unsure, and an abstained note stays in your queue;
- it can **never** approve a `preference` note — those record *your* intent, and no model can confirm that on your behalf;
- it **rejects outright** any note carrying a redaction marker, because that means a credential reached it;
- every approval records **who** approved it (`reviewed_by: human` or the model's name) in the note's frontmatter and in the audit log.

It is off until you turn it on:

```bash
afterwit review --dry-run    # print verdicts, change nothing. Works while it's off.
```

Watch it judge ten notes. If you agree with it, turn it on in the **Settings**
tab of `afterwit ui` (or set `auto_review = true` in `~/.afterwit/config.toml`)
and run `afterwit review`. If you don't agree with it, you have learned
something cheap.

## Settings

`afterwit ui` → **Settings** edits everything without opening a config file:
which harness CLI distills your sessions, which model and reasoning effort it
spends, the same three for the auto-reviewer, the injection caps, and the paths.

The model box offers the models **your** harnesses actually have — read from
`~/.claude/settings.json`, `~/.claude.json` and `~/.codex/config.toml` — so you
are picking from what is installed rather than from a list afterwit guessed at
build time. Leave a model or effort empty and afterwit inherits whatever that
harness is configured with, and pick **Other…** to type an id that shipped
today.

Saving writes `~/.afterwit/config.toml` in place: your comments and any
`[[databases]]` tables survive, a timestamped backup is taken first, and the
running UI picks up the change without a restart. If you moved a harness config
with `CLAUDE_CONFIG_DIR` or `CODEX_HOME`, afterwit follows it — on Linux, macOS
and Windows alike. afterwit only *reads* your harness config; the one command
that writes to it is `afterwit install`.

## Does it phone home?

No.

Everything runs on your machine. The wiki is files on your disk. The index is a
SQLite database on your disk. Search is local (SQLite FTS5 plus a small
embedding model that runs offline on your CPU).

There are exactly three network calls afterwit can make, and you control all three:

1. **`git push`/`git pull` to your own wiki remote** — only if you set one up, and only to the remote you chose.
2. **The LLM CLI you point it at** for the nightly distillation and for auto-review — the `claude` or `codex` CLI you already have installed, talking to your own account under your own terms.
3. **A one-time model download** (~90MB, `all-MiniLM-L6-v2`) the first time you build the index. After that, semantic search runs offline on your CPU forever.

There is no telemetry, no analytics, no crash reporting, no afterwit server. The
prompt hook is deliberately offline — it never makes a network call, never loads
the embedding model, and fails open (if afterwit breaks, your prompt goes through
untouched).

## Limitations

Being honest about what this is and isn't:

- **The distillation is an LLM reading transcripts.** It misses things. It occasionally promotes something that looked resolved but wasn't. That's what the review gate is for, and the review gate needs you.
- **Nightly runs cost tokens.** Distillation drives an LLM over your unread sessions. There is no default cap — pass `--budget N` to limit sessions per run. A `--timeout` (default 50 min) skips the LLM stage if the run overruns.
- **Notes go stale.** afterwit anchors each note to the commit it was written against and flags it when the referenced code moves, but a flag is not a fix. Stale knowledge that *looks* confident is the main failure mode of a system like this.
- **No published benchmark.** The retrieval quality is measured against a small internal golden set. That number means something to this repo and nothing to yours; there is no honest cross-user benchmark to quote, so this README quotes none.
- **Small-corpus assumption.** This is SQLite and markdown files, sized for one developer's few hundred sessions. It is not built for a team's shared transcript archive.
- **Windows is CI-tested, not battle-tested.** The scheduler, hooks and MCP registration run in CI on Windows. The author develops on Linux.
- **Reading your sessions means reading your sessions.** afterwit's whole premise is that it has access to your transcripts. If that isn't acceptable for your work, don't install it.

## Everyday commands

```bash
afterwit recall "prisma connection refused"   # search your knowledge from the terminal
afterwit ui                                   # review queue, search, health
afterwit review --dry-run                     # see how the auto-reviewer would judge your queue
afterwit run                                  # force the nightly run now
afterwit sync                                 # push/pull your wiki to its private remote
afterwit index --rebuild                      # rebuild the SQLite cache from markdown
```

`aw` is a shorter alias for `afterwit`; the two commands are identical.

## License

MIT. See [LICENSE](LICENSE).
