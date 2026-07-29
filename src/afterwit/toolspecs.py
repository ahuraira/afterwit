"""MCP tool contracts. SPEC §9.1.

The descriptions are load-bearing: they are the ONLY thing that makes an agent
decide to call these tools mid-task (pull-based discovery, Manifesto P3/P7).
Written to trigger on the moments that matter — an unfamiliar error, a
"why is it like this" question, opening a risky file. The server module just
registers these; do not rewrite descriptions without an eval re-run.
"""

TOOLS: list[dict] = [
    {
        "name": "recall",
        "description": (
            "Search this user's distilled knowledge from ALL past Claude Code and Codex "
            "sessions: decisions with rationale, bugs fixed, gotchas, preferences, project "
            "facts. Call when history could save you work — before re-deriving a decision, "
            "re-debugging something that may have failed before, or guessing a convention. "
            "Returns ranked cards with provenance; may return nothing (that means no known "
            "history — proceed normally)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What you want to know, in plain words"},
                "project": {"type": "string", "description": "Project slug to prioritize (optional)"},
                "type": {"type": "string", "enum": ["decision", "gotcha", "error_fix", "preference", "fact", "snippet", "capability"], "description": "Restrict to one card type (optional). 'capability' = reusable code that already exists across the user's projects — check before building something new."},
                "k": {"type": "integer", "default": 5, "maximum": 10},
            },
            "required": ["query"],
        },
    },
    {
        "name": "lookup_error",
        "description": (
            "Look up an error in the user's history of fixed errors. Call IMMEDIATELY when a "
            "command, test, or build fails with an error you don't instantly recognize — if "
            "this user hit it before, the exact fix that worked is here. Paste the distinctive "
            "part of the error message verbatim. Cheaper than re-debugging."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "error_text": {"type": "string", "description": "The error message, verbatim (trim paths/noise)"},
                "project": {"type": "string", "description": "Project slug (optional; other-project fixes still returned)"},
            },
            "required": ["error_text"],
        },
    },
    {
        "name": "why",
        "description": (
            "Get the recorded decision and rationale for a design choice in this user's "
            "projects — including superseded history ('we used X until May, then switched to "
            "Y because Z'). Call before changing architecture, replacing a library, or when "
            "code looks wrong but might be deliberate. Prevents re-litigating settled decisions."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "The choice in question, e.g. 'JSONB audit payloads'"},
                "project": {"type": "string"},
            },
            "required": ["topic"],
        },
    },
    {
        "name": "for_file",
        "description": (
            "Everything known about a specific file from past sessions: decisions made in it, "
            "times it broke and how it was fixed, gotchas. Call before non-trivial edits to a "
            "file you haven't touched this session — especially config, auth, migrations, or "
            "anything that looks fragile."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Repo-relative file path (fragments ok)"},
                "project": {"type": "string"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "project_brief",
        "description": (
            "One-page distilled brief for a project: what it is, active decisions, top "
            "gotchas, toolchain facts. Call at the start of substantial work in a project "
            "you lack context on."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"project": {"type": "string"}},
            "required": ["project"],
        },
    },
    {
        "name": "related",
        "description": (
            "Cards connected to a given card, 1 hop: wikilinks, the supersede chain "
            "(what this decision replaced and why), curated related links, and cards "
            "touching the same files. "
            "Call after recall/why when a returned card references [[other-cards]] or "
            "when you need the lineage behind a decision."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"card_id": {"type": "string"}},
            "required": ["card_id"],
        },
    },
    {
        "name": "save_insight",
        "description": (
            "Propose a durable knowledge card from THIS session: a decision the user "
            "confirmed, an error you fixed (include the error text and the verified fix), a "
            "stated preference, a gotcha that cost real time. Goes to the user's review "
            "queue — it is NOT trusted automatically, so do not expect immediate recall. "
            "Only propose resolved, confirmed knowledge; never speculation."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ["decision", "gotcha", "error_fix", "preference", "fact", "snippet"]},
                "title": {"type": "string", "maxLength": 80},
                "body": {"type": "string", "description": "Self-contained, ≤300 tokens; error_fix must contain the error signature"},
                "why": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "files": {"type": "array", "items": {"type": "string"}},
                "project": {"type": "string"},
            },
            "required": ["type", "title", "body", "project"],
        },
    },
    {
        "name": "feedback",
        "description": (
            "Rate a card you received from recall/why/lookup_error/for_file or saw injected. "
            "Call 'helpful' when it saved you work, 'wrong' when it was incorrect (quarantines "
            "it), 'stale' when outdated. This trains the ranking — use it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "card_id": {"type": "string"},
                "verdict": {"type": "string", "enum": ["helpful", "wrong", "stale"]},
                "note": {"type": "string"},
            },
            "required": ["card_id", "verdict"],
        },
    },
]
