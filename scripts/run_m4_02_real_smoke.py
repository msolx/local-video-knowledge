"""Milestone M4-02 Real Local Model Smoke Test Runner.

Executes real extraction against local LM Studio instance:
- Video asset: douyin_7681603850364521734 (4 chunks)
- Album asset: douyin_7682038498466993905 (1 chunk)
- Idempotency & cache verification
- Invariant audit
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
import urllib.error

# Prevent Windows GBK console encoding crashes on Unicode/Braille characters
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Ensure project root in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.knowledge.extractor import (
    ExtractionConfig,
    extract_knowledge_candidates,
    PERCEPTUAL_MODALITIES,
)
from src.knowledge.models import (
    UnitType,
    VerificationStatus,
    AttributionStatus,
    KNOWLEDGE_SCHEMA_VERSION,
)

LMS_CLI = r"C:\Users\Sean\.lmstudio\bin\lms.exe"
BASE_URL = "http://127.0.0.1:12345/v1"
MODEL_KEY = "qwen/qwen3-8b"
MODEL_IDENTIFIER = "qwen3-8b"


def run_cmd(args: list[str], timeout: int = 60) -> tuple[int, str, str]:
    res = subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    stdout = (res.stdout or "").strip()
    stderr = (res.stderr or "").strip()
    return res.returncode, stdout, stderr


def ensure_lms_server() -> bool:
    print(f"[*] Checking LM Studio server at {BASE_URL}...")
    for _ in range(3):
        try:
            with urllib.request.urlopen(f"{BASE_URL}/models", timeout=3) as resp:
                if resp.status == 200:
                    print("[+] LM Studio HTTP server is ready.")
                    return True
        except Exception:
            pass

        print("[*] Starting LM Studio server on port 12345...")
        code, out, err = run_cmd([LMS_CLI, "server", "start", "--port", "12345"], timeout=30)
        print(f"    Server start returncode={code}: {out} {err}")
        time.sleep(3)

    # Final check
    try:
        with urllib.request.urlopen(f"{BASE_URL}/models", timeout=5) as resp:
            if resp.status == 200:
                print("[+] LM Studio HTTP server is ready.")
                return True
    except Exception as e:
        print(f"[-] Could not reach LM Studio server at {BASE_URL}: {e}")
        return False
    return False


def load_lms_model() -> bool:
    print(f"[*] Loading model {MODEL_KEY} as {MODEL_IDENTIFIER}...")
    code, out, err = run_cmd([
        LMS_CLI, "load", MODEL_KEY,
        "--gpu", "max",
        "--identifier", MODEL_IDENTIFIER,
        "--yes"
    ], timeout=120)
    print(f"    Load returncode={code}: {out} {err}")
    return code == 0


def unload_lms_model():
    print(f"[*] Unloading model {MODEL_IDENTIFIER}...")
    code, out, err = run_cmd([LMS_CLI, "unload", MODEL_IDENTIFIER], timeout=60)
    print(f"    Unload returncode={code}: {out}")


def audit_artifact(data: dict[str, Any], processed_dir: Path) -> dict[str, Any]:
    manifest = json.loads((processed_dir / "evidence_manifest.json").read_text(encoding="utf-8"))
    chunks_doc = json.loads((processed_dir / "evidence_chunks.json").read_text(encoding="utf-8"))
    manifest_index = {item["evidence_id"]: item for item in manifest["evidence_items"]}
    chunk_map = {chk["chunk_id"]: chk for chk in chunks_doc["chunks"]}

    violations = []
    candidates = data.get("candidates", [])

    for c in candidates:
        ku_id = c.get("knowledge_unit_id")
        cid = c.get("canonical_id")
        utype = c.get("unit_type")
        statement = c.get("statement")
        refs = c.get("evidence_refs", [])
        v_status = c.get("verification_status")
        entities = c.get("entities", [])
        topics = c.get("topics", [])
        lineage = c.get("extraction_lineage", {})
        attr = c.get("attribution", {})

        # ID checks
        if not ku_id or not ku_id.startswith("ku_") or len(ku_id) != 19:
            violations.append(f"Invalid ku_id: {ku_id}")
        if cid != processed_dir.name:
            violations.append(f"Canonical ID mismatch: {cid} != {processed_dir.name}")
        if not statement or not statement.strip():
            violations.append(f"Empty statement in {ku_id}")
        if not refs:
            violations.append(f"No evidence_refs in {ku_id}")
        if v_status != "not_checked":
            violations.append(f"verification_status ({v_status}) != not_checked in {ku_id}")
        if entities != []:
            violations.append(f"Non-empty entities in {ku_id}")
        if topics != []:
            violations.append(f"Non-empty topics in {ku_id}")

        # Lineage check
        chunk_ids = lineage.get("input_chunk_ids", [])
        if not chunk_ids:
            violations.append(f"No input_chunk_ids in lineage for {ku_id}")
        chunk_id = chunk_ids[0]
        if chunk_id not in chunk_map:
            violations.append(f"Unknown chunk_id {chunk_id} in lineage for {ku_id}")
        else:
            chunk_eids = set(chunk_map[chunk_id]["evidence_ids"])
            for ref in refs:
                eid = ref["evidence_id"]
                if eid not in manifest_index:
                    violations.append(f"Referenced evidence {eid} not in manifest")
                if eid not in chunk_eids:
                    violations.append(f"Referenced evidence {eid} outside chunk {chunk_id}")

        # Observation gate check
        if utype == "observation":
            has_perceptual = any(
                manifest_index.get(ref["evidence_id"], {}).get("modality") in PERCEPTUAL_MODALITIES
                for ref in refs
            )
            if not has_perceptual:
                violations.append(f"Observation {ku_id} lacks perceptual evidence")

        # Attribution check
        if utype == "verification_question":
            if attr.get("attribution_status") != "system_derived":
                violations.append(f"Question {ku_id} attribution != system_derived")
        elif any(manifest_index.get(ref["evidence_id"], {}).get("modality") in PERCEPTUAL_MODALITIES for ref in refs):
            if attr.get("attribution_status") != "visual_media":
                violations.append(f"Visual {ku_id} attribution != visual_media")
        else:
            if attr.get("attribution_status") != "unverified_speaker":
                violations.append(f"Speech {ku_id} attribution != unverified_speaker")

    return {
        "total_candidates": len(candidates),
        "total_rejections": len(data.get("rejections", [])),
        "violations": violations,
        "valid": len(violations) == 0,
    }


def main():
    print("==================================================")
    print("M4-02 Real Local Model Extraction Smoke Test")
    print("==================================================")

    # 1. Ensure server and load model
    if not ensure_lms_server():
        print("[-] FATAL: LM Studio server could not be started.")
        sys.exit(1)

    if not load_lms_model():
        print("[-] FATAL: Failed to load model.")
        sys.exit(1)

    config = ExtractionConfig(
        backend="openai_compatible",
        model=MODEL_IDENTIFIER,
        base_url=BASE_URL,
        temperature=0.1,
        max_tokens=4096,
        timeout_seconds=300,
        force=True,
    )

    results = {}

    try:
        # ----------------------------------------------------
        # 2. C10 Video Smoke: douyin_7681603850364521734
        # ----------------------------------------------------
        video_dir = PROJECT_ROOT / "data" / "processed" / "douyin_7681603850364521734"
        print(f"\n[*] Processing C10 Video: {video_dir.name} (4 chunks)...")
        start_time = time.time()
        video_res = extract_knowledge_candidates(video_dir, config=config)
        video_duration = time.time() - start_time
        print(f"[+] C10 Video extracted in {video_duration:.2f}s:")
        print(f"    Total chunks: {video_res['total_chunks']}")
        print(f"    Raw candidates: {video_res['total_raw_candidates']}")
        print(f"    Accepted candidates: {video_res['total_accepted_candidates']}")
        print(f"    Rejected candidates: {video_res['total_rejected_candidates']}")
        for chk in video_res["chunk_summaries"]:
            print(f"    - {chk['chunk_id']}: accepted={chk['accepted_count']}, rejected={chk['rejected_count']}, error={chk['error']}")

        video_audit = audit_artifact(video_res, video_dir)
        print(f"    Audit valid: {video_audit['valid']}, Violations: {len(video_audit['violations'])}")
        if not video_audit["valid"]:
            for v in video_audit["violations"]:
                print(f"      ! {v}")

        # ----------------------------------------------------
        # 3. C10 Album Smoke: douyin_7682038498466993905
        # ----------------------------------------------------
        album_dir = PROJECT_ROOT / "data" / "processed" / "douyin_7682038498466993905"
        print(f"\n[*] Processing C10 Album: {album_dir.name} (1 chunk, 4 items)...")
        start_time = time.time()
        album_res = extract_knowledge_candidates(album_dir, config=config)
        album_duration = time.time() - start_time
        print(f"[+] C10 Album extracted in {album_duration:.2f}s:")
        print(f"    Total chunks: {album_res['total_chunks']}")
        print(f"    Raw candidates: {album_res['total_raw_candidates']}")
        print(f"    Accepted candidates: {album_res['total_accepted_candidates']}")
        print(f"    Rejected candidates: {album_res['total_rejected_candidates']}")
        for chk in album_res["chunk_summaries"]:
            print(f"    - {chk['chunk_id']}: accepted={chk['accepted_count']}, rejected={chk['rejected_count']}, error={chk['error']}")

        album_audit = audit_artifact(album_res, album_dir)
        print(f"    Audit valid: {album_audit['valid']}, Violations: {len(album_audit['violations'])}")
        if not album_audit["valid"]:
            for v in album_audit["violations"]:
                print(f"      ! {v}")

        # ----------------------------------------------------
        # 4. Idempotency & Cache Hit Verification
        # ----------------------------------------------------
        print(f"\n[*] Testing Idempotency (Cache Hit with force=False)...")
        cache_config = ExtractionConfig(
            backend="openai_compatible",
            model=MODEL_IDENTIFIER,
            base_url=BASE_URL,
            temperature=0.1,
            max_tokens=4096,
            timeout_seconds=300,
            force=False,
        )
        start_time = time.time()
        video_cache_res = extract_knowledge_candidates(video_dir, config=cache_config)
        video_cache_duration = time.time() - start_time

        album_cache_res = extract_knowledge_candidates(album_dir, config=cache_config)

        all_video_cached = all(chk["cache_hit"] for chk in video_cache_res["chunk_summaries"])
        all_album_cached = all(chk["cache_hit"] for chk in album_cache_res["chunk_summaries"])

        print(f"[+] Video cache run took {video_cache_duration:.3f}s (all cached: {all_video_cached})")
        print(f"[+] Album cache run (all cached: {all_album_cached})")

        video_ids_1 = [c["knowledge_unit_id"] for c in video_res["candidates"]]
        video_ids_2 = [c["knowledge_unit_id"] for c in video_cache_res["candidates"]]
        assert video_ids_1 == video_ids_2, "Cache hit produced different knowledge_unit_ids!"
        assert video_res["extraction_run_id"] == video_cache_res["extraction_run_id"]

        results = {
            "backend": "lm_studio",
            "model": MODEL_KEY,
            "identifier": MODEL_IDENTIFIER,
            "base_url": BASE_URL,
            "video_duration_s": round(video_duration, 2),
            "video_total_chunks": video_res["total_chunks"],
            "video_accepted": video_res["total_accepted_candidates"],
            "video_rejected": video_res["total_rejected_candidates"],
            "video_chunk_summaries": video_res["chunk_summaries"],
            "video_audit_valid": video_audit["valid"],
            "album_duration_s": round(album_duration, 2),
            "album_total_chunks": album_res["total_chunks"],
            "album_accepted": album_res["total_accepted_candidates"],
            "album_rejected": album_res["total_rejected_candidates"],
            "album_chunk_summaries": album_res["chunk_summaries"],
            "album_audit_valid": album_audit["valid"],
            "cache_hit_verified": all_video_cached and all_album_cached,
        }

        print("\n==================================================")
        print("M4-02 Real Smoke Test Execution SUCCESS")
        print(json.dumps(results, indent=2, ensure_ascii=False))
        print("==================================================")

    finally:
        # Always clean up GPU memory by unloading model
        unload_lms_model()


if __name__ == "__main__":
    main()
