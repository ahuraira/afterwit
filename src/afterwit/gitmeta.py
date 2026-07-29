"""Git metadata for projects: identity (remote URL), position (HEAD commit),
and drift (files changed since a commit). ADR-018.

Everything here is LOCAL git only — no network, no GitHub API. The serving path
(`afterwit inject`, p95 < 200ms) must never import this module: staleness is computed
at lint time and persisted in the `cards.stale` column, and the hook reads only
that flag.

Every function degrades to None rather than raising: a project may be a plain
directory, a shallow clone, or absent on this device. Callers fall back to the
existence check (SPEC §8a) — they never crash on a non-repo.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

_TIMEOUT = 10


def _git(repo: Path, *args: str) -> str | None:
    """Run git in `repo`. None when it isn't a checkout, git is missing, or the
    command fails. Success with no output returns "" (falsy but not None)."""
    try:
        r = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, timeout=_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip()


def is_repo(repo: Path) -> bool:
    return _git(repo, "rev-parse", "--git-dir") is not None


def head_commit(repo: Path) -> str | None:
    return _git(repo, "rev-parse", "HEAD") or None


def normalize_url(url: str) -> str:
    """`git@github.com:o/r.git` and `https://github.com/o/r.git` both become
    `https://github.com/o/r` — one stable identity per repo, whatever the
    clone protocol was on this device."""
    u = url.strip()
    if u.startswith("git@"):
        host, _, path = u[4:].partition(":")
        u = f"https://{host}/{path}"
    # Strip any userinfo (`https://user:token@host/...`). A credentialed origin
    # would otherwise ride into every anchored card's repo_url and sync to the
    # remote in the clear (audit: outside-the-10 finding). Identity is host+path.
    u = re.sub(r"(https?://)[^/@\s]+@", r"\1", u)
    if u.endswith(".git"):
        u = u[:-4]
    return u


def remote_url(repo: Path) -> str | None:
    url = _git(repo, "remote", "get-url", "origin")
    return normalize_url(url) if url else None


def changed_files(repo: Path, since: str) -> set[str] | None:
    """Repo-relative paths changed between `since` and HEAD.

    None means "cannot tell" — unknown commit (rewritten history, shallow clone,
    a sha authored on another device and never fetched) — and the caller must
    fall back rather than assume nothing changed."""
    if not since or _git(repo, "cat-file", "-e", f"{since}^{{commit}}") is None:
        return None
    out = _git(repo, "diff", "--name-only", f"{since}..HEAD")
    if out is None:
        return None
    return {line for line in out.splitlines() if line}


def anchor(projects_root: Path, project: str,
           cache: dict[str, tuple[str | None, str | None]] | None = None,
           aliases: dict[str, str] | None = None,
           ) -> tuple[str | None, str | None]:
    """`(source_commit, repo_url)` for a project on this device — the ONE place a
    card gets stamped (ADR-020). distill, `afterwit queue` and MCP `save_insight` all
    call this; when they each rolled their own, save_insight silently shipped
    unanchored cards. `global` and non-git projects yield `(None, None)`."""
    if not project or project == "global":
        return (None, None)
    if cache is not None and project in cache:
        return cache[project]
    # A slug is not always its folder name (ADR-039); git lives in the folder.
    from .config import project_dir_name
    base = projects_root / project_dir_name(project, aliases)
    got = (head_commit(base), remote_url(base))
    if cache is not None:
        cache[project] = got
    return got


def commit_at(repo: Path, when: str) -> str | None:
    """Last commit on or before `when` (ISO date) — the commit that was HEAD when
    a card was written. None when the card predates the repo's first commit, or
    this isn't a checkout."""
    out = _git(repo, "rev-list", "-1", f"--before={when} 23:59:59", "HEAD")
    return out or None


def discover(projects_root: Path) -> dict[str, Path]:
    """repo_url → local checkout path for every repo under projects_root.

    This is what makes a card portable across devices: a project is identified
    by its remote, not by the folder name it happens to have on this machine."""
    found: dict[str, Path] = {}
    if not projects_root.is_dir():
        return found
    for child in sorted(projects_root.iterdir()):
        if not child.is_dir():
            continue
        url = remote_url(child)
        if url:
            found.setdefault(url, child)
    return found
