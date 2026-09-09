"""M5-06 End-to-End Acceptance Runner (deterministic, fully offline).

Reads the real M4 final artifacts, validates them, rebuilds a disposable temp
store, runs store/round-trip/retrieval/golden/determinism gates, and only in
``--finalize`` mode creates the official production store
``data/knowledge/knowledge_store.sqlite3`` via the safe rebuild semantic
(temp DB build -> validate -> atomic replace).

No LLM, no embeddings, no reranker, no network, no runtime probing.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.knowledge.evaluation import (  # noqa: E402
    DEFAULT_GOLDEN_PATH,
    evaluate_suite,
    load_golden_suite,
)
from src.knowledge.models import (  # noqa: E402
    CanonicalKnowledgeUnit,
    CanonicalKnowledgeUnitsDocument,
)
from src.knowledge.retrieval import (  # noqa: E402
    RetrievalQuery,
    RETRIEVAL_PATH_FTS,
    RETRIEVAL_PATH_MIXED,
    RETRIEVAL_PATH_SHORT,
    check_retrieval_invariant,
    retrieve,
)
from src.knowledge.store import (  # noqa: E402
    DEFAULT_STORE_PATH,
    create_store,
    compute_store_revision,
    discover_final_artifacts,
    ingest_knowledge_document,
    list_ingested_assets,
    list_units_for_asset,
    rebuild_store,
    validate_store,
)

REPORT_PATH = ROOT / "evaluation" / "m5" / "reports" / "m5_06_acceptance.json"

REPRESENTATIVE_QUERIES = [
    ("Vulkan", {}, RETRIEVAL_PATH_FTS),
    ("RDNA", {}, RETRIEVAL_PATH_FTS),
    ("Thinking", {}, RETRIEVAL_PATH_FTS),
    ("27B", {}, RETRIEVAL_PATH_FTS),
    ("模型", {}, RETRIEVAL_PATH_SHORT),
    ("速度", {}, RETRIEVAL_PATH_SHORT),
    ("logitech", {}, RETRIEVAL_PATH_FTS),
    ("AGON", {}, RETRIEVAL_PATH_FTS),
    ("SMILEY", {}, RETRIEVAL_PATH_FTS),
    ("Vulkan 模型", {}, RETRIEVAL_PATH_MIXED),
]

FILTER_AUDITS = [
    ("canonical_id_album", "logitech", {"canonical_ids": ["douyin_7682038498466993905"]}),
    ("canonical_id_video_neg", "logitech", {"canonical_ids": ["douyin_7681603850364521734"]}),
    ("unit_type_claim", "Vulkan", {"unit_types": ["claim"]}),
    ("verification_not_checked", "Vulkan", {"verification_statuses": ["not_checked"]}),
    ("topic_模型优化", "Vulkan", {"topics": ["模型优化"]}),
    ("entity_logitech", "logitech", {"entity_names": ["logitech"]}),
]

BASELINE = {
    "mean_hit_at_k": 0.8235,
    "mean_mrr": 0.8235,
    "mean_precision_at_k": 0.6467,
    "mean_recall_at_k": 0.9417,
    "mean_f1_at_k": 0.7144,
}


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _asset_fingerprint(path: Path) -> dict:
    import hashlib

    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    doc = json.loads(path.read_text(encoding="utf-8"))
    return {"canonical_id": doc["canonical_id"], "sha256": sha, "unit_count": len(doc["units"])}


def _load_document(path: Path) -> CanonicalKnowledgeUnitsDocument:
    artifact = json.loads(path.read_text(encoding="utf-8"))
    return CanonicalKnowledgeUnitsDocument.from_dict(artifact)


def _unit_payloads(db_path: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for asset in list_ingested_assets(db_path):
        for payload in list_units_for_asset(db_path, asset["canonical_id"]):
            out[payload["knowledge_unit_id"]] = payload
    return out


def run_source_validation(artifacts: list[Path]) -> dict:
    sources = []
    failures = []
    for p in sorted(artifacts):
        info = _asset_fingerprint(p)
        try:
            _load_document(p)
            info["valid"] = True
        except Exception as exc:  # noqa: BLE001
            info["valid"] = False
            info["error"] = str(exc)
            failures.append({"path": str(p), "error": str(exc)})
        sources.append(info)
    return {"artifact_count": len(artifacts), "sources": sources, "failures": failures}


def run_roundtrip_audit(db_path: Path, artifacts: list[Path]) -> dict:
    checked = 0
    mismatches = []
    by_asset: dict[str, dict[str, dict]] = {}
    for p in artifacts:
        doc = _load_document(p)
        by_asset[doc.canonical_id] = {u.knowledge_unit_id: u for u in doc.units}
    stored = _unit_payloads(db_path)
    for canonical_id, units in by_asset.items():
        for ku_id, original in units.items():
            checked += 1
            if ku_id not in stored:
                mismatches.append({"ku_id": ku_id, "reason": "missing in store"})
                continue
            payload = stored[ku_id]
            if payload["canonical_id"] != canonical_id:
                mismatches.append({"ku_id": ku_id, "reason": "canonical_id mismatch"})
                continue
            stored_unit = CanonicalKnowledgeUnit.from_dict(payload)
            if stored_unit.to_dict() != original.to_dict():
                mismatches.append({"ku_id": ku_id, "reason": "field mismatch"})
    return {"checked_units": checked, "mismatch_count": len(mismatches), "mismatches": mismatches}


def _field_match(hit: dict, field: str) -> bool:
    mi = hit.get("match_info", {})
    return bool(mi.get(f"{field}_match", False))


def run_retrieval_audit(db_path: Path) -> dict:
    results = []
    failures = []
    for qtext, filters, expected_path in REPRESENTATIVE_QUERIES:
        query = RetrievalQuery(query_text=qtext, top_k=10, **filters)
        result = retrieve(db_path, query)
        diag = result.diagnostics
        actual_path = diag.get("retrieval_path")
        path_ok = actual_path == expected_path
        hits = [h.to_dict() for h in result.hits]
        evidence_ok = all(len(h["evidence_refs"]) > 0 for h in hits)
        provenance_ok = all(
            h.get("source_artifact", {}).get("path") and h.get("source_artifact", {}).get("fingerprint")
            for h in hits
        )
        diag_ok = all(h.get("ranking_diagnostics", {}).get("ranking_policy_version") == "lexical-ranking-v1" for h in hits)
        coverage_ok = all(abs(h["match_info"]["term_coverage"] - 1.0) < 1e-9 for h in hits)
        top_k_ok = len(hits) <= query.top_k
        invariant_violations = check_retrieval_invariant(result)
        ok = path_ok and evidence_ok and provenance_ok and diag_ok and coverage_ok and top_k_ok and not invariant_violations
        entry = {
            "query_text": qtext,
            "top_k": query.top_k,
            "filters": filters,
            "expected_path": expected_path,
            "actual_path": actual_path,
            "path_correct": path_ok,
            "result_count": result.result_count,
            "top_k_respected": top_k_ok,
            "evidence_complete": evidence_ok,
            "provenance_complete": provenance_ok,
            "diagnostics_complete": diag_ok,
            "term_coverage_valid": coverage_ok,
            "invariant_violations": invariant_violations,
            "candidate_count_before_limit": diag.get("candidate_count_before_limit"),
            "ok": ok,
        }
        results.append(entry)
        if not ok:
            failures.append({"query_text": qtext, **{k: v for k, v in entry.items() if k != "query_text"}})
    return {"query_count": len(results), "results": results, "failures": failures}


def run_short_query_audit(db_path: Path) -> dict:
    entries = []
    for qtext in ("模型", "速度"):
        query = RetrievalQuery(query_text=qtext, top_k=5)
        result = retrieve(db_path, query)
        diag = result.diagnostics
        path = diag.get("retrieval_path")
        entries.append(
            {
                "query_text": qtext,
                "retrieval_path": path,
                "expected_path": RETRIEVAL_PATH_SHORT,
                "path_correct": path == RETRIEVAL_PATH_SHORT,
                "result_count": result.result_count,
                "top_k_respected": result.result_count <= 5,
                "method": result.retrieval_method,
                "short_query_fallback": diag.get("short_query_fallback"),
            }
        )
    # 1-char smoke: only needs no-error + top_k + correct diagnostics.
    query = RetrievalQuery(query_text="模", top_k=3)
    result = retrieve(db_path, query)
    diag = result.diagnostics
    entries.append(
        {
            "query_text": "模",
            "retrieval_path": diag.get("retrieval_path"),
            "expected_path": RETRIEVAL_PATH_SHORT,
            "path_correct": diag.get("retrieval_path") == RETRIEVAL_PATH_SHORT,
            "result_count": result.result_count,
            "top_k_respected": result.result_count <= 3,
            "short_query_fallback": diag.get("short_query_fallback"),
            "no_error": True,
        }
    )
    return {"query_count": len(entries), "entries": entries}


def run_filter_audit(db_path: Path) -> dict:
    entries = []
    failures = []
    for fid, qtext, filters in FILTER_AUDITS:
        query = RetrievalQuery(query_text=qtext, top_k=10, **filters)
        result = retrieve(db_path, query)
        hits = [h.to_dict() for h in result.hits]
        violations = []
        if "canonical_ids" in filters:
            allowed = set(filters["canonical_ids"])
            if any(h["canonical_id"] not in allowed for h in hits):
                violations.append("canonical_id filter violated")
        if "unit_types" in filters:
            allowed = set(filters["unit_types"])
            if any(h["unit_type"] not in allowed for h in hits):
                violations.append("unit_type filter violated")
        if "verification_statuses" in filters:
            allowed = set(filters["verification_statuses"])
            if any(h["verification_status"] not in allowed for h in hits):
                violations.append("verification_status filter violated")
        if "topics" in filters:
            allowed = set(filters["topics"])
            if any(not (set(h["topics"]) & allowed) for h in hits):
                violations.append("topic filter violated")
        if "entity_names" in filters:
            allowed = set(filters["entity_names"])
            if any(not ({e["entity_name"] for e in h["entities"]} & allowed) for h in hits):
                violations.append("entity_name filter violated")
        zero_expected = fid.endswith("_neg")
        zero_ok = result.result_count == 0 if zero_expected else True
        if zero_expected and result.result_count != 0:
            violations.append("expected zero results but got hits")
        ok = not violations and zero_ok
        entries.append(
            {
                "filter_id": fid,
                "query_text": qtext,
                "filters": filters,
                "result_count": result.result_count,
                "filter_correct": ok,
                "zero_result_ok": zero_ok,
                "violations": violations,
            }
        )
        if not ok:
            failures.append({"filter_id": fid, "violations": violations})
    return {"audit_count": len(entries), "entries": entries, "failures": failures}


def run_ranking_audit(db_path: Path) -> dict:
    entries = []
    failures = []
    # FTS path: statement-match must rank above evidence-only (frozen
    # lexical-ranking-v1 invariant). 27B has both tiers in one result set.
    query = RetrievalQuery(query_text="27B", top_k=5)
    result = retrieve(db_path, query)
    for h in [x.to_dict() for x in result.hits]:
        rd = h["ranking_diagnostics"]
        entries.append(
            {
                "ku_id": h["knowledge_unit_id"],
                "rank": h["rank"],
                "statement_match": _field_match(h, "statement"),
                "evidence_match": _field_match(h, "evidence"),
                "evidence_only_match": h["match_info"].get("evidence_only_match"),
                "bm25": rd.get("raw_bm25"),
                "bm25_direction": rd.get("bm25_direction"),
                "policy": rd.get("ranking_policy_version"),
            }
        )
    first_statement_rank = next(
        (e["rank"] for e in entries if e["statement_match"]), None
    )
    first_evidence_only_rank = next(
        (e["rank"] for e in entries if e["evidence_only_match"]), None
    )
    bm25_direction_ok = all(e["bm25_direction"] == "lower_is_better" for e in entries if e["bm25_direction"])
    policy_ok = all(e["policy"] == "lexical-ranking-v1" for e in entries)
    if first_statement_rank is None or first_evidence_only_rank is None:
        failures.append("27B audit: missing statement-match or evidence-only hit to compare")
    elif first_statement_rank > first_evidence_only_rank:
        failures.append("27B audit: evidence-only outranks statement-match")
    entries.append(
        {
            "audit": "27B",
            "first_statement_match_rank": first_statement_rank,
            "first_evidence_only_rank": first_evidence_only_rank,
            "bm25_direction_ok": bm25_direction_ok,
            "policy_ok": policy_ok,
        }
    )
    # Short path: higher score = better, no probability interpretation.
    query = RetrievalQuery(query_text="模型", top_k=5)
    result = retrieve(db_path, query)
    short = []
    for h in [x.to_dict() for x in result.hits]:
        rd = h["ranking_diagnostics"]
        short.append(
            {
                "ku_id": h["knowledge_unit_id"],
                "rank": h["rank"],
                "score": rd.get("weighted_substring_score"),
                "score_direction": rd.get("score_direction"),
                "policy": rd.get("ranking_policy_version"),
            }
        )
    scores = [s["score"] for s in short if s["score"] is not None]
    score_direction_ok = all(s["score_direction"] == "higher_is_better" for s in short if s["score_direction"])
    if scores and scores != sorted(scores, reverse=True):
        failures.append("short path: scores not monotonically non-increasing")
    entries.append(
        {
            "audit": "模型(short)",
            "scores": scores,
            "score_direction_ok": score_direction_ok,
            "monotonic": scores == sorted(scores, reverse=True) if scores else True,
        }
    )
    return {"entries": entries, "failures": failures}


def run_golden(db_path: Path, suite) -> dict:
    summary = evaluate_suite(db_path, suite, store_revision=compute_store_revision(db_path))
    return summary.to_dict()


def run_determinism(db_path: Path, suite) -> dict:
    first = evaluate_suite(db_path, suite, store_revision=compute_store_revision(db_path))
    second = evaluate_suite(db_path, suite, store_revision=compute_store_revision(db_path))
    strip = lambda d: {k: v for k, v in d.items() if k != "generated_at"}  # noqa: E731
    return {
        "deterministic": strip(first.to_dict()) == strip(second.to_dict()),
        "store_revision": first.store_revision,
    }


def run_rebuild_determinism(processed_root: Path, suite) -> dict:
    revisions = []
    golden_results = []
    for _ in range(2):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "store.sqlite3"
            rebuild_store(db, processed_root)
            revisions.append(compute_store_revision(db))
            summary = evaluate_suite(db, suite, store_revision=compute_store_revision(db))
            golden_results.append([(e["query_id"], e["passed"], e["mrr"]) for e in summary.to_dict()["per_query"]])
    return {
        "revisions": revisions,
        "revision_identical": revisions[0] == revisions[1],
        "golden_identical": golden_results[0] == golden_results[1],
    }


def run_failure_safety(processed_root: Path) -> dict:
    """rebuild fail-fast on an invalid artifact; existing store untouched."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        proc = root / "processed"
        (proc / "bad_asset" / "knowledge").mkdir(parents=True)
        bad = proc / "bad_asset" / "knowledge" / "knowledge_units.json"
        bad.write_text(json.dumps({"schema_version": "knowledge-units-v1", "canonical_id": "x"}), encoding="utf-8")
        good_db = root / "good.sqlite3"
        rebuild_store(good_db, processed_root)
        before = compute_store_revision(good_db)
        try:
            rebuild_store(root / "would_fail.sqlite3", proc)
            failed = False
        except Exception:  # noqa: BLE001
            failed = True
        after = compute_store_revision(good_db)
    return {"fail_fast_on_invalid": failed, "existing_store_untouched": before == after}


def main() -> int:
    parser = argparse.ArgumentParser(description="M5-06 End-to-End Acceptance")
    parser.add_argument(
        "--finalize",
        action="store_true",
        help="After all gates pass, create the official production store "
        "data/knowledge/knowledge_store.sqlite3 (safe rebuild semantic). "
        "Default is dry-run (temp store only; production DB untouched).",
    )
    args = parser.parse_args()

    processed_root = ROOT / "data" / "processed"
    golden_path = ROOT / DEFAULT_GOLDEN_PATH
    report: dict = {"mode": "finalize" if args.finalize else "dry-run", "generated_at": _utc_now()}

    # 1. Discover + validate real final M4 assets.
    artifacts = discover_final_artifacts(processed_root)
    report["discovered_assets"] = run_source_validation(artifacts)
    if not artifacts or report["discovered_assets"]["failures"]:
        print("FATAL: source validation failed; HOLD.", file=sys.stderr)
        report["overall"] = "HOLD"
        write_report(report)
        return 1

    suite = load_golden_suite(golden_path)

    # 2. Disposable temp acceptance store (never production in any mode until finalize).
    with tempfile.TemporaryDirectory() as td:
        temp_db = Path(td) / "acceptance.sqlite3"
        rebuild_result = rebuild_store(temp_db, processed_root)
        report["temp_store"] = {
            "schema_version": rebuild_result.schema_version,
            "schema_policy_version": rebuild_result.schema_policy_version,
            "store_revision": rebuild_result.store_revision,
            "asset_count": rebuild_result.asset_count,
            "unit_count": rebuild_result.unit_count,
            "checks": rebuild_result.checks,
            "valid": rebuild_result.valid,
            "violations": rebuild_result.violations,
        }
        validation = validate_store(temp_db)
        report["temp_validation"] = {
            "valid": validation.valid,
            "checks": validation.checks,
            "violations": validation.violations,
        }

        # 3. Round-trip audit.
        report["roundtrip_audit"] = run_roundtrip_audit(temp_db, artifacts)

        # 4. Retrieval / short / filter / ranking audits.
        report["retrieval_audit"] = run_retrieval_audit(temp_db)
        report["short_query_audit"] = run_short_query_audit(temp_db)
        report["filter_audit"] = run_filter_audit(temp_db)
        report["ranking_audit"] = run_ranking_audit(temp_db)

        # 5. Golden suite.
        report["golden"] = run_golden(temp_db, suite)

        # 6. Determinism + rebuild determinism + failure safety.
        report["determinism"] = run_determinism(temp_db, suite)
        report["rebuild_determinism"] = run_rebuild_determinism(processed_root, suite)
        report["failure_safety"] = run_failure_safety(processed_root)

        gates = [
            report["temp_validation"]["valid"],
            report["roundtrip_audit"]["mismatch_count"] == 0,
            not report["retrieval_audit"]["failures"],
            not report["filter_audit"]["failures"],
            not report["ranking_audit"]["failures"],
            report["golden"]["corpus_fingerprint_ok"] is True,
            report["golden"]["failed_query_count"] == 0,
            report["golden"]["passed_query_count"] == report["golden"]["query_count"],
            report["determinism"]["deterministic"] is True,
            report["rebuild_determinism"]["revision_identical"] is True,
            report["rebuild_determinism"]["golden_identical"] is True,
            report["failure_safety"]["fail_fast_on_invalid"] is True,
            report["failure_safety"]["existing_store_untouched"] is True,
        ]
        report["gates"] = {f"gate_{i}": bool(g) for i, g in enumerate(gates)}
        report["all_gates_passed"] = all(gates)

        if not all(gates):
            report["overall"] = "HOLD"
            write_report(report)
            print("FAIL: one or more acceptance gates failed; HOLD. See report.", file=sys.stderr)
            return 1

        # 7. Production store creation ONLY in --finalize mode, after all gates.
        if args.finalize:
            prod = ROOT / DEFAULT_STORE_PATH
            prod.parent.mkdir(parents=True, exist_ok=True)
            prod_validation = rebuild_store(prod, processed_root)
            report["production_store"] = {
                "path": str(prod),
                "schema_version": prod_validation.schema_version,
                "store_revision": prod_validation.store_revision,
                "asset_count": prod_validation.asset_count,
                "unit_count": prod_validation.unit_count,
                "checks": prod_validation.checks,
                "valid": prod_validation.valid,
                "violations": prod_validation.violations,
            }
            # Re-open + re-validate the official DB.
            revalidation = validate_store(prod)
            report["production_revalidation"] = {
                "valid": revalidation.valid,
                "checks": revalidation.checks,
                "violations": revalidation.violations,
            }
            if not (prod_validation.valid and revalidation.valid):
                report["overall"] = "HOLD"
                write_report(report)
                print("FAIL: production store invalid; HOLD.", file=sys.stderr)
                return 1
            report["production_created"] = True
            report["overall"] = "PASS"
        else:
            report["production_created"] = False
            report["overall"] = "PASS (dry-run; production store not created)"

    write_report(report)
    print(f"overall: {report['overall']}")
    print(f"assets: {report['discovered_assets']['artifact_count']} | units: {report['temp_store']['unit_count']} | valid: {report['temp_validation']['valid']}")
    print(f"roundtrip mismatches: {report['roundtrip_audit']['mismatch_count']}")
    print(f"golden: {report['golden']['passed_query_count']}/{report['golden']['query_count']} | fingerprint_ok: {report['golden']['corpus_fingerprint_ok']}")
    print(f"deterministic: {report['determinism']['deterministic']} | rebuild_revision_identical: {report['rebuild_determinism']['revision_identical']}")
    return 0


def write_report(report: dict) -> None:
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(f"report written: {REPORT_PATH}")


if __name__ == "__main__":
    raise SystemExit(main())