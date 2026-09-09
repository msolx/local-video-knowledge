"""M5-05 Retrieval Evaluation Runner (deterministic, fully offline).

Builds a temporary SQLite store (never the production store), ingests the real
C10 M4 final artifacts, runs the tracked golden query suite from
``evaluation/m5/c10_golden_queries.json``, computes aggregate metrics, prints a
concise report, and writes a machine-readable JSON report.

No LLM, no embeddings, no reranker, no network, no runtime probing.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.knowledge.evaluation import (  # noqa: E402
    DEFAULT_GOLDEN_PATH,
    load_golden_suite,
    evaluate_suite,
    write_evaluation_report,
)
from src.knowledge.store import (  # noqa: E402
    create_store,
    ingest_knowledge_document,
)

REPORT_PATH = ROOT / "evaluation" / "m5" / "reports" / "c10_retrieval_evaluation.json"


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def main() -> int:
    suite = load_golden_suite(DEFAULT_GOLDEN_PATH)

    # Corpus fixture integrity: 2 assets, 68 KU total, fingerprints match the
    # golden binding. Stop hard if anything is off -- never silently update.
    assets = list(suite.corpus_assets)
    if len(assets) != 2:
        print(f"FATAL: expected 2 corpus assets, got {len(assets)}", file=sys.stderr)
        return 1
    expected_units = 0
    for a in assets:
        p = ROOT / a["path"]
        if not p.is_file():
            print(f"FATAL: corpus artifact missing: {p}", file=sys.stderr)
            return 1
        doc = json.loads(p.read_text(encoding="utf-8"))
        expected_units += len(doc.get("units", []))
        print(f"  asset {a['canonical_id']}: {len(doc.get('units', []))} units")
    print(f"  corpus total: {expected_units} units")

    # Build the disposable store in a temp dir and ingest the real artifacts.
    with tempfile.TemporaryDirectory(prefix="m5_05_eval_") as tmp:
        db_path = Path(tmp) / "eval_store.sqlite3"
        create_store(db_path)
        for a in assets:
            res = ingest_knowledge_document(db_path, ROOT / a["path"])
            print(f"  ingested {a['canonical_id']}: {res.status} ({res.unit_count} units)")

        summary = evaluate_suite(db_path, suite)

    print()
    print(f"suite_version             : {summary.suite_version}")
    print(f"evaluation_policy_version : {summary.evaluation_policy_version}")
    print(f"ranking_policy_version    : {summary.ranking_policy_version}")
    print(f"corpus_fingerprint        : {summary.corpus_fingerprint}")
    print(f"corpus_fingerprint_ok     : {summary.corpus_fingerprint_ok}")
    print(f"store_revision            : {summary.store_revision}")
    print(f"query_count               : {summary.query_count}")
    print(f"exhaustive_query_count    : {summary.exhaustive_query_count}")
    print(f"partial_query_count       : {summary.partial_query_count}")
    print(f"passed_query_count        : {summary.passed_query_count}")
    print(f"failed_query_count        : {summary.failed_query_count}")
    print(f"mean_hit_at_k             : {summary.mean_hit_at_k:.4f}")
    print(f"mean_mrr                  : {summary.mean_mrr:.4f}")
    if summary.mean_precision_at_k is not None:
        print(f"mean_precision_at_k       : {summary.mean_precision_at_k:.4f} (exhaustive only)")
        print(f"mean_recall_at_k          : {summary.mean_recall_at_k:.4f} (exhaustive only)")
        print(f"mean_f1_at_k              : {summary.mean_f1_at_k:.4f} (exhaustive only)")
    else:
        print("mean_precision/recall/f1  : n/a (no exhaustive queries)")
    print(f"filter_accuracy           : "
          f"{summary.filter_accuracy if summary.filter_accuracy is not None else 'n/a'}")
    print(f"retrieval_path_accuracy   : {summary.retrieval_path_accuracy:.4f}")
    print(f"evidence_completeness     : {summary.evidence_completeness_rate:.4f}")
    print(f"provenance_completeness   : {summary.provenance_completeness_rate:.4f}")
    print(f"term_coverage_valid_rate  : {summary.term_coverage_valid_rate:.4f}")

    print()
    print("per-query:")
    for q in summary.per_query:
        rank = q["first_relevant_rank"] if q["first_relevant_rank"] is not None else "-"
        status = "PASS" if q["passed"] else "FAIL"
        print(
            f"  [{status}] {q['query_id']:32s} path={q['actual_retrieval_path']:36s} "
            f"first_relevant_rank={rank} hit={int(q['hit_at_k'])} mrr={q['mrr']:.3f}"
        )

    if summary.structural_failures:
        print()
        print("golden failures:")
        for f in summary.structural_failures:
            print(f"  {f['query_id']}: {f['failure_reasons']}")

    write_evaluation_report(summary, REPORT_PATH, generated_at=_utc_now())
    print()
    print(f"report written: {REPORT_PATH}")

    if not summary.corpus_fingerprint_ok:
        print("EVALUATION FAIL: stale corpus fingerprint (golden bound to a different artifact version)")
        return 1
    if summary.failed_query_count:
        print(f"EVALUATION FAIL: {summary.failed_query_count} golden query failures")
        return 1
    print("EVALUATION PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())