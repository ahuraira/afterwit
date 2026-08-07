"""Database schema adapter. Introspects metadata only; never reads row data."""

from __future__ import annotations

import sqlite3
import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import unquote, urlparse, urlunparse

from afterwit import config as config_mod
from afterwit.events import Event


def _sqlite_path(url: str) -> Path:
    if url.startswith("sqlite://"):
        parsed = urlparse(url)
        return Path(unquote(parsed.path))
    return Path(url).expanduser()


def _sqlite_events(project: str, url: str) -> list[Event]:
    path = _sqlite_path(url)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )]
        events: list[Event] = []
        for table in tables:
            quoted = table.replace('"', '""')
            cols = conn.execute(f'PRAGMA table_info("{quoted}")').fetchall()
            fks = conn.execute(f'PRAGMA foreign_key_list("{quoted}")').fetchall()
            column_text = ", ".join(
                f"{c[1]} {c[2] or 'ANY'}" + (" NOT NULL" if c[3] else "")
                for c in cols
            ) or "(no columns)"
            fk_text = "; ".join(f"{f[3]} -> {f[2]}.{f[4]}" for f in fks)
            body = f"Columns: {column_text}" + (f"\nForeign keys: {fk_text}" if fk_text else "")
            events.append(Event(
                source_path=f"db://{project}/{path.name}", lines=(1, 1),
                project=project, ts=None, role="schema", kind="schema",
                text=f"{table} table\n\n{body}",
                meta={"harness": "db", "kind": "schema", "card_type": "db_schema"},
            ))
        return events
    finally:
        conn.close()


def _postgres_events(project: str, url: str) -> list[Event]:
    psql = shutil.which("psql")
    if not psql:
        raise RuntimeError(f"database {project}: psql is required for PostgreSQL introspection")
    parsed = urlparse(url)
    host = parsed.hostname or ""
    netloc = ((f"{parsed.username}@" if parsed.username else "") + host
              + (f":{parsed.port}" if parsed.port else ""))
    safe_url = urlunparse((parsed.scheme, netloc, parsed.path, "", parsed.query, ""))
    env = os.environ.copy()
    if parsed.password:
        env["PGPASSWORD"] = unquote(parsed.password)
    query = """
SELECT table_schema, table_name, column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_schema NOT IN ('pg_catalog','information_schema')
ORDER BY table_schema, table_name, ordinal_position
"""
    result = subprocess.run(
        [psql, safe_url, "-X", "-At", "-F", "\t", "-c", query],
        capture_output=True, text=True, timeout=60, env=env,
        stdin=subprocess.DEVNULL,
    )
    if result.returncode:
        raise RuntimeError(f"database {project}: psql failed: {result.stderr[:200]}")
    grouped: dict[tuple[str, str], list[str]] = {}
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 5:
            continue
        schema, table, column, data_type, nullable = parts
        grouped.setdefault((schema, table), []).append(
            f"{column} {data_type}" + (" NOT NULL" if nullable == "NO" else "")
        )
    return [Event(
        source_path=f"db://{project}/{schema}.{table}", lines=(1, 1), project=project,
        ts=None, role="schema", kind="schema",
        text=f"{schema}.{table} table\n\nColumns: {', '.join(columns)}",
        meta={"harness": "db", "kind": "schema", "card_type": "db_schema"},
    ) for (schema, table), columns in grouped.items()]


def iter_config_events(cfg: config_mod.Config) -> Iterator[tuple[str, list[Event]]]:
    for i, item in enumerate(cfg.databases):
        project = str(item.get("project") or "global")
        url = str(item.get("url") or "")
        if not url:
            continue
        if url.startswith(("postgres://", "postgresql://")):
            events = _postgres_events(project, url)
        else:
            events = _sqlite_events(project, url)
        label = f"db-{project}-{i}.schema"
        yield label, events
