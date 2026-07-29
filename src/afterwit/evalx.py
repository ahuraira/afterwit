"""`afterwit eval` — golden-set retrieval eval. SPEC §12, ADR-007.

Metrics on eval/golden.yaml (built from the user's OWN history; public memory
benchmarks banned — ADR-007):
  recall@3            fraction of hit queries whose expected card is in top-3
  MRR                 mean reciprocal rank of the first expected hit
  no-answer precision fraction of trap queries that return NOTHING at the floor

Gates (exit nonzero on failure):
  recall@3 ≥ 0.7            (only when hit queries exist — they are filled by the
                            lead from real distilled cards; skipped while empty)
  no-answer precision ≥ 0.9 (always — silence on traps is first-class, P3)

Everything runs through the real serving path (index_db.search → rank.rank at
cfg.floor) so the numbers reflect what the harness would actually surface.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml  # type: ignore[import-untyped]

RECALL_AT = 3
RECALL_GATE = 0.7
NO_ANSWER_GATE = 0.9


def _golden_path() -> Path:
    return Path(__file__).resolve().parents[2] / "eval" / "golden.yaml"


def _retrieve(cfg, conn, query: str, project: str | None, k: int = 10):
    from . import index_db, rank
    rows = index_db.search(conn, query, project=project, k=20)
    return rank.rank(rows, project, floor=cfg.floor, k=k, query_text=query)


def _hit_rank(scored, q: dict) -> int | None:
    """1-based rank of the first result matching this hit query, else None."""
    want_id = q.get("expected_id")
    want_sub = (q.get("expected_title_contains") or "").lower()
    for i, s in enumerate(scored, start=1):
        if want_id and s.id == want_id:
            return i
        if want_sub and want_sub in s.title.lower():
            return i
    return None


def evaluate(cfg, golden: dict) -> dict:
    from . import index_db

    queries = golden.get("queries", [])
    hits = [q for q in queries if q.get("kind") == "hit"]
    traps = [q for q in queries if q.get("kind") == "no_answer"]

    conn = index_db.connect(cfg.db_path) if cfg.db_path.exists() else None
    try:
        recalled = 0
        rr_sum = 0.0
        hit_detail = []
        for q in hits:
            scored = _retrieve(cfg, conn, q["query"], q.get("project")) if conn else []
            rk = _hit_rank(scored, q)
            found_top = rk is not None and rk <= RECALL_AT
            recalled += found_top
            rr_sum += (1.0 / rk) if rk else 0.0
            hit_detail.append({"query": q["query"], "rank": rk, "in_top3": found_top})

        clean = 0
        trap_detail = []
        for q in traps:
            scored = _retrieve(cfg, conn, q["query"], q.get("project")) if conn else []
            empty = len(scored) == 0
            clean += empty
            trap_detail.append({"query": q["query"], "returned": len(scored),
                                "titles": [s.title[:60] for s in scored[:3]]})
    finally:
        if conn:
            conn.close()

    recall_at3 = (recalled / len(hits)) if hits else None
    mrr = (rr_sum / len(hits)) if hits else None
    no_answer_precision = (clean / len(traps)) if traps else 1.0

    recall_pass = recall_at3 is None or recall_at3 >= RECALL_GATE
    no_answer_pass = no_answer_precision >= NO_ANSWER_GATE
    return {
        "n_hits": len(hits), "n_traps": len(traps),
        "recall_at3": recall_at3, "mrr": mrr,
        "no_answer_precision": no_answer_precision,
        "recall_pass": recall_pass, "no_answer_pass": no_answer_pass,
        "passed": recall_pass and no_answer_pass,
        "hit_detail": hit_detail, "trap_detail": trap_detail,
    }


def _report(r: dict) -> str:
    def pct(x):
        return "—" if x is None else f"{x:.0%}"

    lines = [
        "afterwit eval (SPEC §12)",
        f"  hit queries:  {r['n_hits']}",
        f"    recall@{RECALL_AT}: {pct(r['recall_at3'])}  "
        f"(gate ≥ {RECALL_GATE:.0%}) {'PASS' if r['recall_pass'] else 'FAIL'}"
        + ("  [skipped — no hit queries yet]" if r["recall_at3"] is None else ""),
        f"    MRR:       {'—' if r['mrr'] is None else f'{r['mrr']:.3f}'}",
        f"  no-answer traps: {r['n_traps']}",
        f"    precision: {pct(r['no_answer_precision'])}  "
        f"(gate ≥ {NO_ANSWER_GATE:.0%}) {'PASS' if r['no_answer_pass'] else 'FAIL'}",
    ]
    leaked = [d for d in r["trap_detail"] if d["returned"]]
    for d in leaked:
        lines.append(f"      LEAK: {d['returned']} result(s) for {d['query']!r}"
                     + (f" -> {d['titles']}" if d.get("titles") else ""))
    missed = [d for d in r["hit_detail"] if not d["in_top3"]]
    for d in missed:
        lines.append(f"      MISS: {d['query']!r} (rank {d['rank']})")
    lines.append(f"  OVERALL: {'PASS' if r['passed'] else 'FAIL'}")
    return "\n".join(lines)


def main(golden_path: str | None = None) -> int:
    from . import config as config_mod

    cfg = config_mod.load()
    path = Path(golden_path) if golden_path else _golden_path()
    if not path.exists():
        print(f"golden set missing: {path}", file=sys.stderr)
        return 1
    golden = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    result = evaluate(cfg, golden)
    print(_report(result))
    return 0 if result["passed"] else 1
