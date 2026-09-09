"""Milestone M4-04 Real Local Model Enrichment Smoke Test Runner.

Executes real grounded entity/topic enrichment against local LM Studio:
- Video asset: douyin_7681603850364521734 (62 merged units)
- Album asset: douyin_7682038498466993905 (6 merged units)
- Identity audit (frozen fields byte-identical)
- Idempotency & cache verification
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import time
import urllib.request

# Prevent Windows GBK console encoding crashes on Unicode characters
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Ensure project root in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.knowledge.enrichment import (
    ENRICHED_CANDIDATES_FILENAME,
    EnrichmentConfig,
    audit_identity_preservation,
    enrich_knowledge_candidates,
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
    if code == 0:
        return True
    # Model may already be loaded under this identifier.
    if "already exists" in err or "already exists" in out:
        print("[+] Model already loaded.")
        return True
    return False


def unload_lms_model():
    print(f"[*] Unloading model {MODEL_IDENTIFIER}...")
    code, out, err = run_cmd([LMS_CLI, "unload", MODEL_IDENTIFIER], timeout=60)
    print(f"    Unload returncode={code}: {out}")


def summarize_enrichment(artifact: dict, processed_dir: Path) -> dict:
    units_with_entities = sum(1 for u in artifact["units"] if u.get("entities"))
    total_entity_mentions = sum(len(u.get("entities", [])) for u in artifact["units"])
    units_with_topics = sum(1 for u in artifact["units"] if u.get("topics"))
    total_topics = sum(len(u.get("topics", [])) for u in artifact["units"])

    source = json.loads(
        (processed_dir / "knowledge" / "merged_knowledge_candidates.json").read_text(encoding="utf-8")
    )
    identity = audit_identity_preservation(source, artifact)

    return {
        "input_unit_count": artifact["input_unit_count"],
        "output_unit_count": artifact["output_unit_count"],
        "enriched_unit_count": artifact["enriched_unit_count"],
        "failed_unit_count": artifact["failed_unit_count"],
        "units_with_entities": units_with_entities,
        "total_entity_mentions": total_entity_mentions,
        "units_with_topics": units_with_topics,
        "total_topics": total_topics,
        "rejected_enrichment_proposals": len(artifact["audit"]["rejections"]),
        "identity_audit_valid": identity["valid"],
        "identity_violations": identity["violations"],
        "cache_hit": artifact.get("cache_hit", False),
        "llm_calls": artifact["audit"]["llm_call_count"],
    }


def main():
    print("==================================================")
    print("M4-04 Real Local Model Enrichment Smoke Test")
    print("==================================================")

    if not ensure_lms_server():
        print("[-] FATAL: LM Studio server could not be started.")
        sys.exit(1)

    if not load_lms_model():
        print("[-] FATAL: Failed to load model.")
        sys.exit(1)

    config = EnrichmentConfig(
        backend="openai_compatible",
        model=MODEL_IDENTIFIER,
        base_url=BASE_URL,
        temperature=0.1,
        max_tokens=4096,
        batch_size=10,
        timeout_seconds=300,
        force=True,
    )

    results: dict = {}

    try:
        # ----------------------------------------------------
        # 1. C10 Video Enrichment: douyin_7681603850364521734 (62 units)
        # ----------------------------------------------------
        video_dir = PROJECT_ROOT / "data" / "processed" / "douyin_7681603850364521734"
        print(f"\n[*] Enriching C10 Video: {video_dir.name} (62 units)...")
        start_time = time.time()
        video_res = enrich_knowledge_candidates(video_dir, config=config)
        video_duration = time.time() - start_time
        video_stats = summarize_enrichment(video_res, video_dir)
        print(f"[+] C10 Video enriched in {video_duration:.2f}s:")
        print(json.dumps(video_stats, indent=2, ensure_ascii=False))
        if not video_stats["identity_audit_valid"]:
            print("    IDENTITY VIOLATIONS:")
            for v in video_stats["identity_violations"]:
                print(f"      ! {v}")

        # ----------------------------------------------------
        # 2. C10 Album Enrichment: douyin_7682038498466993905 (6 units)
        # ----------------------------------------------------
        album_dir = PROJECT_ROOT / "data" / "processed" / "douyin_7682038498466993905"
        print(f"\n[*] Enriching C10 Album: {album_dir.name} (6 units)...")
        start_time = time.time()
        album_res = enrich_knowledge_candidates(album_dir, config=config)
        album_duration = time.time() - start_time
        album_stats = summarize_enrichment(album_res, album_dir)
        print(f"[+] C10 Album enriched in {album_duration:.2f}s:")
        print(json.dumps(album_stats, indent=2, ensure_ascii=False))
        if not album_stats["identity_audit_valid"]:
            print("    IDENTITY VIOLATIONS:")
            for v in album_stats["identity_violations"]:
                print(f"      ! {v}")

        # ----------------------------------------------------
        # 3. Idempotency & Cache Hit Verification
        # ----------------------------------------------------
        print("\n[*] Testing Idempotency (Cache Hit with force=False)...")
        cache_config = EnrichmentConfig(
            backend="openai_compatible",
            model=MODEL_IDENTIFIER,
            base_url=BASE_URL,
            temperature=0.1,
            max_tokens=4096,
            batch_size=10,
            timeout_seconds=300,
            force=False,
        )
        start_time = time.time()
        video_cache_res = enrich_knowledge_candidates(video_dir, config=cache_config)
        video_cache_duration = time.time() - start_time
        album_cache_res = enrich_knowledge_candidates(album_dir, config=cache_config)

        video_ids_1 = [u["knowledge_unit_id"] for u in video_res["units"]]
        video_ids_2 = [u["knowledge_unit_id"] for u in video_cache_res["units"]]
        assert video_ids_1 == video_ids_2, "Cache hit produced different knowledge_unit_ids!"
        assert video_cache_res["cache_hit"] is True
        assert album_cache_res["cache_hit"] is True

        print(f"[+] Video cache run took {video_cache_duration:.3f}s "
              f"(video cache_hit={video_cache_res['cache_hit']}, "
              f"album cache_hit={album_cache_res['cache_hit']})")

        results = {
            "backend": "lm_studio",
            "model": MODEL_KEY,
            "identifier": MODEL_IDENTIFIER,
            "base_url": BASE_URL,
            "video_duration_s": round(video_duration, 2),
            "video": video_stats,
            "album_duration_s": round(album_duration, 2),
            "album": album_stats,
            "cache_hit_verified": video_cache_res["cache_hit"] and album_cache_res["cache_hit"],
        }

        print("\n==================================================")
        print("M4-04 Real Smoke Test Execution SUCCESS")
        print(json.dumps(results, indent=2, ensure_ascii=False))
        print("==================================================")

    finally:
        unload_lms_model()


if __name__ == "__main__":
    main()